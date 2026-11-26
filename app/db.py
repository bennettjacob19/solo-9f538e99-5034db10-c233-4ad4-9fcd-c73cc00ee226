"""SQLite 连接与表结构。

设计要点：
- 所有写事务使用 ``BEGIN IMMEDIATE``，保证两个 Worker 进程并发领段时不重段；
- ``conclusions`` 对 (job_id, seg_number) 加唯一约束，并加禁止 UPDATE/DELETE
  的触发器，从数据库层保证“每段只有一个正式结论”；
- 租约字段（lease_token / lease_generation）用于对迟到、陈旧回报做围栏校验。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from . import config

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS clock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    tick INTEGER NOT NULL DEFAULT 0,
    updated_wall TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS machines (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    max_inflight INTEGER NOT NULL DEFAULT 2,
    generation INTEGER NOT NULL DEFAULT 1,   -- 每次（重新）启动 +1，陈旧进程回报会被围栏拒绝
    status TEXT NOT NULL DEFAULT 'starting', -- starting | active | draining | exited
    pid INTEGER,
    exit_code INTEGER,
    stop_tick INTEGER,
    last_heartbeat_wall TEXT,
    started_wall TEXT,
    exited_wall TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    note TEXT,
    segment_count INTEGER NOT NULL,
    default_duration_ticks INTEGER NOT NULL DEFAULT 3,
    status TEXT NOT NULL DEFAULT 'active',  -- active | completed
    created_tick INTEGER NOT NULL,
    created_wall TEXT NOT NULL,
    completed_tick INTEGER,
    completed_wall TEXT
);

CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    seg_number INTEGER NOT NULL,             -- 对操作员友好的段号，从 1 开始
    offset_bytes INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    expected_sha256 TEXT NOT NULL,
    duration_ticks INTEGER NOT NULL,         -- 该段配置的演练耗时（拍）
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | leased | completed
    lease_machine TEXT,
    lease_generation INTEGER,
    lease_token TEXT,
    claimed_tick INTEGER,
    due_tick INTEGER,
    claim_wall TEXT,
    completed_tick INTEGER,
    UNIQUE(job_id, seg_number)
);
CREATE INDEX IF NOT EXISTS idx_segments_pending ON segments(status, id);
CREATE INDEX IF NOT EXISTS idx_segments_machine ON segments(lease_machine, status);

-- 每次实算都追加一行（同一段被旧机算过、交回后另一台重算，会有多行）
CREATE TABLE IF NOT EXISTS computations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    seg_number INTEGER NOT NULL,
    machine_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    lease_token TEXT,
    computed_sha256 TEXT NOT NULL,
    expected_sha256 TEXT NOT NULL,
    digest_match INTEGER NOT NULL,
    byte_count INTEGER NOT NULL,
    hash_wall_ms REAL NOT NULL,
    claimed_tick INTEGER,
    computed_at_tick INTEGER NOT NULL,
    wall_ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_computations_segment
    ON computations(job_id, seg_number, id);

-- 正式结论：每段至多一行，且不可改、不可删
CREATE TABLE IF NOT EXISTS conclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    seg_number INTEGER NOT NULL,
    verdict TEXT NOT NULL,                   -- verified | mismatch
    match INTEGER NOT NULL,
    expected_sha256 TEXT NOT NULL,
    computed_sha256 TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    source_computation_id INTEGER,
    recorded_tick INTEGER NOT NULL,
    wall_ts TEXT NOT NULL,
    UNIQUE(job_id, seg_number)
);

CREATE TRIGGER IF NOT EXISTS trg_conclusions_no_update
BEFORE UPDATE ON conclusions
BEGIN
    SELECT RAISE(ABORT, 'conclusions are append-only: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_conclusions_no_delete
BEFORE DELETE ON conclusions
BEGIN
    SELECT RAISE(ABORT, 'conclusions are append-only: DELETE is forbidden');
END;

CREATE TABLE IF NOT EXISTS shutdown_plans (
    id TEXT PRIMARY KEY,
    machine_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    deadline_tick INTEGER NOT NULL,          -- 最晚停机拍
    max_reverify INTEGER NOT NULL,           -- 最多可重验的在途段数
    status TEXT NOT NULL,                    -- blocked | accepted | exit_confirmed
    feasible INTEGER NOT NULL,
    current_tick INTEGER NOT NULL,
    wait_segments TEXT NOT NULL DEFAULT '[]',
    requeue_segments TEXT NOT NULL DEFAULT '[]',
    blocking_segments TEXT NOT NULL DEFAULT '[]',
    planned_stop_tick INTEGER,
    stop_tick INTEGER,
    pid INTEGER,
    exit_code INTEGER,
    exit_confirmed INTEGER NOT NULL DEFAULT 0,
    maintenance_ready INTEGER NOT NULL DEFAULT 0,
    reverify_segment_numbers TEXT NOT NULL DEFAULT '[]',
    message TEXT,
    request_json TEXT NOT NULL DEFAULT '{}',
    created_wall TEXT NOT NULL,
    updated_wall TEXT NOT NULL,
    confirmed_wall TEXT
);
CREATE INDEX IF NOT EXISTS idx_shutdown_machine ON shutdown_plans(machine_id, created_wall);

-- 延迟回报箱：演练“网络拖住旧机回报，停机后才送达”
CREATE TABLE IF NOT EXISTS held_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    job_id TEXT NOT NULL,
    seg_number INTEGER NOT NULL,
    lease_token TEXT,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'held',     -- held | released_accepted | released_rejected
    reject_reason TEXT,
    release_http_status INTEGER,
    response_json TEXT,
    created_wall TEXT NOT NULL,
    released_wall TEXT
);

-- 所有被围栏/唯一性/时序拒绝的回报留痕
CREATE TABLE IF NOT EXISTS rejected_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id TEXT,
    generation INTEGER,
    job_id TEXT,
    seg_number INTEGER,
    lease_token TEXT,
    reason TEXT NOT NULL,
    detail TEXT,
    payload_json TEXT,
    at_tick INTEGER NOT NULL,
    wall_ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rejected_segment ON rejected_reports(job_id, seg_number);

-- 哪些 (机器, 段) 的回报要被延迟箱截获
CREATE TABLE IF NOT EXISTS result_holds (
    machine_id TEXT NOT NULL,
    seg_number INTEGER NOT NULL,             -- -1 表示该机全部段
    created_wall TEXT NOT NULL,
    PRIMARY KEY (machine_id, seg_number)
);
"""


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def immediate_tx() -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE 写事务：跨进程领段/受理停机的原子性靠它。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def reading() -> Iterator[sqlite3.Connection]:
    """只读查询上下文，退出时一定关闭连接，避免遗留读快照。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT OR IGNORE INTO clock(id, tick, updated_wall) "
            "VALUES (1, 0, ?)",
            (_wall_now(),),
        )
        for mid in config.MACHINE_IDS:
            conn.execute(
                "INSERT OR IGNORE INTO machines(id, name, max_inflight, generation, status) "
                "VALUES (?, ?, ?, 1, 'starting')",
                (mid, config.MACHINE_NAMES[mid], config.MAX_INFLIGHT_PER_MACHINE),
            )
        conn.commit()
    finally:
        conn.close()


def recreate_db() -> None:
    """演练复位：删库重建（结论表的触发器会随 DROP TABLE 一起消失）。"""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        tables = [
            "rejected_reports", "held_reports", "result_holds",
            "shutdown_plans", "conclusions", "computations",
            "segments", "jobs", "machines", "clock",
        ]
        for t in tables:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.commit()
    finally:
        conn.close()
    init_db()


def _wall_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def wall_now() -> str:
    return _wall_now()
