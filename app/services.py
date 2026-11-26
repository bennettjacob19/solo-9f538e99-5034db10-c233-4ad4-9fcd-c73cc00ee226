"""核心演练逻辑：时钟、作业、领段、实算摘要、正式结论、退机计划、延迟回报裁决。

所有跨进程正确性都落在 SQLite 写事务 + 条件更新上，而不是只靠内存状态。
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from . import config, db, volume


class ApiError(Exception):
    def __init__(self, status_code: int, reason: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body: dict[str, Any] = {"reason": reason, "message": message}
        if extra:
            self.body.update(extra)


# ---------------------------------------------------------------- 基础工具

def _tick(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT tick FROM clock WHERE id=1").fetchone()["tick"])


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _json_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _short(sha: str | None) -> str | None:
    return sha[:12] if sha else None


def _volume_file() -> str:
    manifest = volume.ensure_sample_volume(config.VOLUME_DIR)
    return str(volume.volume_path_of(config.VOLUME_DIR, manifest))


def clock_view() -> dict[str, Any]:
    with db.reading() as conn:
        row = conn.execute("SELECT * FROM clock WHERE id=1").fetchone()
        return {"tick": row["tick"], "updated_wall": row["updated_wall"]}


def advance_clock(*, ticks: int | None = None, to_tick: int | None = None) -> dict[str, Any]:
    with db.immediate_tx() as conn:
        old = _tick(conn)
        if ticks is not None:
            new = old + ticks
        elif to_tick is not None:
            if to_tick < old:
                raise ApiError(400, "clock_monotonic", "演练时钟不能回拨",
                               {"current_tick": old, "to_tick": to_tick})
            new = to_tick
        else:
            new = old + 1
        conn.execute(
            "UPDATE clock SET tick=?, updated_wall=? WHERE id=1",
            (new, db.wall_now()),
        )
        return {"old_tick": old, "tick": new, "advanced": new - old}


# ---------------------------------------------------------------- 作业

def create_job(req: Any) -> dict[str, Any]:
    manifest = volume.ensure_sample_volume(config.VOLUME_DIR)
    manifest_segs = {s["number"]: s for s in manifest["segments"]}

    if req.segment_numbers:
        numbers = list(dict.fromkeys(req.segment_numbers))
    else:
        numbers = list(range(1, req.segment_count + 1))

    bad = [n for n in numbers if n not in manifest_segs]
    if bad:
        raise ApiError(422, "segment_not_in_manifest",
                       f"段号 {bad} 不在清单（共 {len(manifest_segs)} 段）内")

    overrides = req.segment_durations or {}
    bad_dur_keys = [n for n in overrides if n not in numbers]
    if bad_dur_keys:
        raise ApiError(422, "duration_for_unselected_segment",
                       f"耗时覆盖包含未入选段号 {bad_dur_keys}")
    for n, d in overrides.items():
        if d < 1:
            raise ApiError(422, "bad_duration", f"第 {n} 段耗时必须 >= 1 拍")

    job_id = "job_" + uuid.uuid4().hex[:8]
    with db.immediate_tx() as conn:
        tick = _tick(conn)
        conn.execute(
            "INSERT INTO jobs(id, note, segment_count, default_duration_ticks, "
            "status, created_tick, created_wall) VALUES (?,?,?,?, 'active', ?,?)",
            (job_id, req.note, len(numbers), req.default_duration_ticks,
             tick, db.wall_now()),
        )
        for n in numbers:
            meta = manifest_segs[n]
            duration = int(overrides.get(n, req.default_duration_ticks))
            conn.execute(
                "INSERT INTO segments(job_id, seg_number, offset_bytes, size_bytes, "
                "expected_sha256, duration_ticks) VALUES (?,?,?,?,?,?)",
                (job_id, n, meta["offset"], meta["size"], meta["sha256"], duration),
            )
    return get_job(job_id)


def _job_progress(conn: sqlite3.Connection, job_id: str) -> dict[str, int]:
    row = conn.execute(
        "SELECT "
        "COUNT(*) AS total, "
        "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed, "
        "SUM(CASE WHEN status='leased' THEN 1 ELSE 0 END) AS leased, "
        "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending "
        "FROM segments WHERE job_id=?",
        (job_id,),
    ).fetchone()
    return {
        "total": row["total"],
        "completed": row["completed"] or 0,
        "leased": row["leased"] or 0,
        "pending": row["pending"] or 0,
    }


def job_view(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    progress = _job_progress(conn, row["id"])
    verdict_rows = conn.execute(
        "SELECT verdict, COUNT(*) AS c FROM conclusions WHERE job_id=? GROUP BY verdict",
        (row["id"],),
    ).fetchall()
    verdicts = {r["verdict"]: r["c"] for r in verdict_rows}
    return {
        "job_id": row["id"],
        "note": row["note"],
        "status": row["status"],
        "segment_count": row["segment_count"],
        "default_duration_ticks": row["default_duration_ticks"],
        "created_tick": row["created_tick"],
        "created_wall": row["created_wall"],
        "completed_tick": row["completed_tick"],
        "completed_wall": row["completed_wall"],
        "progress": progress,
        "verified_count": verdicts.get("verified", 0),
        "mismatch_count": verdicts.get("mismatch", 0),
    }


def list_jobs() -> list[dict[str, Any]]:
    with db.reading() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY rowid DESC").fetchall()
        return [job_view(conn, r) for r in rows]


def get_job(job_id: str) -> dict[str, Any]:
    with db.reading() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ApiError(404, "job_not_found", f"作业 {job_id} 不存在")
        return job_view(conn, row)


def update_duration(job_id: str, seg_number: int, duration: int) -> dict[str, Any]:
    with db.immediate_tx() as conn:
        seg = conn.execute(
            "SELECT * FROM segments WHERE job_id=? AND seg_number=?",
            (job_id, seg_number),
        ).fetchone()
        if not seg:
            raise ApiError(404, "segment_not_found",
                           f"作业 {job_id} 第 {seg_number} 段不存在")
        conn.execute(
            "UPDATE segments SET duration_ticks=? WHERE id=?", (duration, seg["id"])
        )
        active_lease = None
        if seg["status"] == "leased":
            active_lease = {
                "lease_machine": seg["lease_machine"],
                "claimed_tick": seg["claimed_tick"],
                "due_tick": seg["due_tick"],
                "note": "段耗时已更新；在途租约按领段时快照执行，due_tick 不变，"
                        "交回后再次被领才使用新耗时",
            }
        return {
            "job_id": job_id,
            "segment_number": seg_number,
            "duration_ticks": duration,
            "active_lease_unchanged": active_lease is not None,
            "active_lease": active_lease,
        }


def segment_views(job_id: str) -> dict[str, Any]:
    with db.reading() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise ApiError(404, "job_not_found", f"作业 {job_id} 不存在")
        segs = conn.execute(
            "SELECT * FROM segments WHERE job_id=? ORDER BY seg_number", (job_id,)
        ).fetchall()
        out: list[dict[str, Any]] = []
        for s in segs:
            comps = conn.execute(
                "SELECT * FROM computations WHERE job_id=? AND seg_number=? ORDER BY id",
                (job_id, s["seg_number"]),
            ).fetchall()
            conclusion = conn.execute(
                "SELECT * FROM conclusions WHERE job_id=? AND seg_number=?",
                (job_id, s["seg_number"]),
            ).fetchone()
            out.append({
                "segment_number": s["seg_number"],
                "status": s["status"],
                "offset_bytes": s["offset_bytes"],
                "size_bytes": s["size_bytes"],
                "duration_ticks": s["duration_ticks"],
                "claimed_tick": s["claimed_tick"],
                "due_tick": s["due_tick"],
                "completed_tick": s["completed_tick"],
                "current_lease": (
                    None if s["status"] != "leased"
                    else {
                        "machine_id": s["lease_machine"],
                        "generation": s["lease_generation"],
                        "lease_token": s["lease_token"],
                    }
                ),
                "expected_sha256": s["expected_sha256"],
                "expected_sha256_short": _short(s["expected_sha256"]),
                "computations": [
                    {
                        "computation_id": c["id"],
                        "machine_id": c["machine_id"],
                        "generation": c["generation"],
                        "computed_sha256": c["computed_sha256"],
                        "computed_sha256_short": _short(c["computed_sha256"]),
                        "digest_match_expected": bool(c["digest_match"]),
                        "byte_count": c["byte_count"],
                        "hash_wall_ms": round(c["hash_wall_ms"], 3),
                        "claimed_tick": c["claimed_tick"],
                        "computed_at_tick": c["computed_at_tick"],
                        "wall_ts": c["wall_ts"],
                    }
                    for c in comps
                ],
                "official_conclusion": (
                    None if conclusion is None
                    else {
                        "verdict": conclusion["verdict"],
                        "match": bool(conclusion["match"]),
                        "expected_sha256": conclusion["expected_sha256"],
                        "computed_sha256": conclusion["computed_sha256"],
                        "computed_sha256_short": _short(conclusion["computed_sha256"]),
                        "machine_id": conclusion["machine_id"],
                        "generation": conclusion["generation"],
                        "recorded_tick": conclusion["recorded_tick"],
                        "wall_ts": conclusion["wall_ts"],
                    }
                ),
            })
        return {"job_id": job_id, "job": job_view(conn, job), "segments": out}


# ---------------------------------------------------------------- 校验机视图 / 领段

def _machine_or_404(conn: sqlite3.Connection, machine_id: str) -> sqlite3.Row:
    m = conn.execute("SELECT * FROM machines WHERE id=?", (machine_id,)).fetchone()
    if not m:
        raise ApiError(404, "machine_not_found", f"校验机 {machine_id} 不存在")
    return m


def _lease_views(conn: sqlite3.Connection, machine_id: str, *, with_token: bool) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM segments WHERE lease_machine=? AND status='leased' "
        "ORDER BY seg_number",
        (machine_id,),
    ).fetchall()
    result = []
    for r in rows:
        item = {
            "job_id": r["job_id"],
            "segment_number": r["seg_number"],
            "claimed_tick": r["claimed_tick"],
            "due_tick": r["due_tick"],
            "remaining_ticks": max(0, (r["due_tick"] or 0) - _tick(conn)),
        }
        if with_token:
            item["lease_token"] = r["lease_token"]
            item["duration_snapshot_ticks"] = (
                (r["due_tick"] - r["claimed_tick"])
                if r["due_tick"] is not None and r["claimed_tick"] is not None else None
            )
        result.append(item)
    return result


def list_machines() -> list[dict[str, Any]]:
    with db.reading() as conn:
        rows = conn.execute("SELECT * FROM machines ORDER BY id").fetchall()
        tick = _tick(conn)
        out = []
        for m in rows:
            leases = _lease_views(conn, m["id"], with_token=True)
            out.append({
                "machine_id": m["id"],
                "name": m["name"],
                "status": m["status"],
                "generation": m["generation"],
                "pid": m["pid"],
                "exit_code": m["exit_code"],
                "stop_tick": m["stop_tick"],
                "max_inflight": m["max_inflight"],
                "active_lease_count": len(leases),
                "active_leases": leases,
                "last_heartbeat_wall": m["last_heartbeat_wall"],
                "started_wall": m["started_wall"],
                "exited_wall": m["exited_wall"],
                "clock_tick": tick,
            })
        return out


def get_machine(machine_id: str) -> dict[str, Any]:
    with db.reading() as conn:
        m = _machine_or_404(conn, machine_id)
        tick = _tick(conn)
        return {
            "machine_id": m["id"],
            "name": m["name"],
            "status": m["status"],
            "generation": m["generation"],
            "pid": m["pid"],
            "exit_code": m["exit_code"],
            "stop_tick": m["stop_tick"],
            "max_inflight": m["max_inflight"],
            "active_leases": _lease_views(conn, m["id"], with_token=True),
            "last_heartbeat_wall": m["last_heartbeat_wall"],
            "started_wall": m["started_wall"],
            "exited_wall": m["exited_wall"],
            "clock_tick": tick,
        }


def work_snapshot(machine_id: str, generation: int) -> dict[str, Any]:
    with db.immediate_tx() as conn:
        m = _machine_or_404(conn, machine_id)
        if generation != m["generation"]:
            raise ApiError(409, "generation_fenced",
                           f"进程代次 {generation} 已失效（当前 {m['generation']}），"
                           "陈旧进程必须退出",
                           {"current_generation": m["generation"]})
        conn.execute(
            "UPDATE machines SET last_heartbeat_wall=? WHERE id=?",
            (db.wall_now(), machine_id),
        )
        tick = _tick(conn)
        plan_row = conn.execute(
            "SELECT * FROM shutdown_plans WHERE machine_id=? AND feasible=1 "
            "AND status IN ('accepted','exit_confirmed') ORDER BY rowid DESC LIMIT 1",
            (machine_id,),
        ).fetchone()
        plan = None
        if plan_row:
            plan = {
                "shutdown_id": plan_row["id"],
                "status": plan_row["status"],
                "deadline_tick": plan_row["deadline_tick"],
                "planned_stop_tick": plan_row["planned_stop_tick"],
                "wait_refs": _json_loads(plan_row["wait_segments"], []),
                "requeue_refs": _json_loads(plan_row["requeue_segments"], []),
            }
        return {
            "machine_id": machine_id,
            "status": m["status"],
            "generation": m["generation"],
            "tick": tick,
            "poll_seconds": config.WORKER_POLL_SECONDS,
            "active_leases": _lease_views(conn, machine_id, with_token=True),
            "shutdown_plan": plan,
        }


def claim_segment(machine_id: str, generation: int, pid: int | None) -> tuple[int, dict]:
    with db.immediate_tx() as conn:
        m = _machine_or_404(conn, machine_id)
        if generation != m["generation"]:
            return 409, {"reason": "generation_fenced",
                         "message": "陈旧代次，禁止领段",
                         "current_generation": m["generation"]}
        conn.execute(
            "UPDATE machines SET last_heartbeat_wall=?, pid=COALESCE(?, pid) WHERE id=?",
            (db.wall_now(), pid, machine_id),
        )
        if m["status"] == "exited":
            return 200, {"leased": None, "reason": "machine_exited"}
        if m["status"] == "draining":
            # 受理退机后旧机停领新段 —— 核心规则之一
            return 200, {"leased": None, "reason": "draining",
                         "message": "该机已受理退机，停止领取新段"}

        count = conn.execute(
            "SELECT COUNT(*) AS c FROM segments WHERE lease_machine=? AND status='leased'",
            (machine_id,),
        ).fetchone()["c"]
        if count >= m["max_inflight"]:
            return 200, {"leased": None, "reason": "at_capacity",
                         "max_inflight": m["max_inflight"]}

        seg = conn.execute(
            "SELECT * FROM segments WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if seg is None:
            return 200, {"leased": None, "reason": "no_work"}

        tick = _tick(conn)
        token = secrets.token_hex(16)
        due = tick + int(seg["duration_ticks"])
        conn.execute(
            "UPDATE segments SET status='leased', lease_machine=?, lease_generation=?, "
            "lease_token=?, claimed_tick=?, due_tick=?, claim_wall=? WHERE id=?",
            (machine_id, generation, token, tick, due, db.wall_now(), seg["id"]),
        )
        return 200, {"leased": {
            "job_id": seg["job_id"],
            "segment_number": seg["seg_number"],
            "offset_bytes": seg["offset_bytes"],
            "size_bytes": seg["size_bytes"],
            "expected_sha256": seg["expected_sha256"],
            "volume_path": _volume_file(),
            "lease_token": token,
            "claimed_tick": tick,
            "due_tick": due,
            "duration_ticks": seg["duration_ticks"],
            "generation": generation,
        }}


# ---------------------------------------------------------------- 实算摘要

def add_computation(machine_id: str, payload: dict) -> tuple[int, dict]:
    """登记一次真实 SHA-256 实算。这是遥测/摘要，不是正式结论，因此允许保留旧机记录。"""
    with db.immediate_tx() as conn:
        m = _machine_or_404(conn, machine_id)
        job_id = payload["job_id"]
        seg_number = payload["seg_number"]
        seg = conn.execute(
            "SELECT * FROM segments WHERE job_id=? AND seg_number=?",
            (job_id, seg_number),
        ).fetchone()
        if not seg:
            return 404, {"reason": "segment_not_found", "message": "段不存在"}
        match = int(payload["computed_sha256"] == seg["expected_sha256"])
        tick = _tick(conn)
        cur = conn.execute(
            "INSERT INTO computations(job_id, seg_number, machine_id, generation, "
            "lease_token, computed_sha256, expected_sha256, digest_match, byte_count, "
            "hash_wall_ms, claimed_tick, computed_at_tick, wall_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, seg_number, machine_id, payload.get("generation", m["generation"]),
             payload.get("lease_token"), payload["computed_sha256"],
             seg["expected_sha256"], match, payload["byte_count"],
             payload["hash_wall_ms"], payload.get("claimed_tick"),
             tick, db.wall_now()),
        )
        return 201, {
            "computation_id": cur.lastrowid,
            "digest_match_expected": bool(match),
            "computed_at_tick": tick,
        }


# ---------------------------------------------------------------- 回报截获（延迟箱）

def hold_rule_exists(conn: sqlite3.Connection, machine_id: str, seg_number: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM result_holds WHERE machine_id=? AND (seg_number=? OR seg_number=-1) "
        "LIMIT 1",
        (machine_id, seg_number),
    ).fetchone()
    return row is not None


def create_hold_rule(machine_id: str, seg_number: int) -> dict:
    with db.immediate_tx() as conn:
        _machine_or_404(conn, machine_id)
        conn.execute(
            "INSERT OR IGNORE INTO result_holds(machine_id, seg_number, created_wall) "
            "VALUES (?,?,?)",
            (machine_id, seg_number, db.wall_now()),
        )
    return {"machine_id": machine_id, "segment_number": seg_number, "holding": True}


def delete_hold_rule(machine_id: str, seg_number: int) -> dict:
    with db.immediate_tx() as conn:
        cur = conn.execute(
            "DELETE FROM result_holds WHERE machine_id=? AND seg_number=?",
            (machine_id, seg_number),
        )
        if cur.rowcount == 0:
            raise ApiError(404, "hold_rule_not_found", "截获规则不存在")
    return {"machine_id": machine_id, "segment_number": seg_number, "holding": False}


def list_hold_rules(machine_id: str) -> list[dict]:
    with db.reading() as conn:
        _machine_or_404(conn, machine_id)
        rows = conn.execute(
            "SELECT seg_number, created_wall FROM result_holds WHERE machine_id=? "
            "ORDER BY seg_number",
            (machine_id,),
        ).fetchall()
        return [{"segment_number": r["seg_number"], "created_wall": r["created_wall"]}
                for r in rows]


def _insert_held_report(conn: sqlite3.Connection, machine_id: str, payload: dict) -> int:
    """在调用方已开启的事务内写延迟箱，避免嵌套写事务自锁。"""
    cur = conn.execute(
        "INSERT INTO held_reports(machine_id, generation, job_id, seg_number, "
        "lease_token, payload_json, status, created_wall) "
        "VALUES (?,?,?,?,?,?,'held',?)",
        (machine_id, int(payload.get("generation", -1)), payload["job_id"],
         int(payload["seg_number"]), payload.get("lease_token"),
         _json_dumps(payload), db.wall_now()),
    )
    return int(cur.lastrowid)


def buffer_held_report(machine_id: str, payload: dict) -> int:
    with db.immediate_tx() as conn:
        return _insert_held_report(conn, machine_id, payload)


def list_held_reports(machine_id: str | None = None, status: str | None = None) -> list[dict]:
    q = "SELECT * FROM held_reports WHERE 1=1"
    args: list[Any] = []
    if machine_id:
        q += " AND machine_id=?"; args.append(machine_id)
    if status:
        q += " AND status=?"; args.append(status)
    q += " ORDER BY id"
    with db.reading() as conn:
        rows = conn.execute(q, args).fetchall()
        return [{
            "held_id": r["id"],
            "machine_id": r["machine_id"],
            "generation": r["generation"],
            "job_id": r["job_id"],
            "segment_number": r["seg_number"],
            "status": r["status"],
            "reject_reason": r["reject_reason"],
            "release_http_status": r["release_http_status"],
            "created_wall": r["created_wall"],
            "released_wall": r["released_wall"],
        } for r in rows]


def list_rejected(job_id: str | None = None, limit: int = 100) -> list[dict]:
    q = "SELECT * FROM rejected_reports WHERE 1=1"
    args: list[Any] = []
    if job_id:
        q += " AND job_id=?"; args.append(job_id)
    q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
    with db.reading() as conn:
        rows = conn.execute(q, args).fetchall()
        return [{
            "id": r["id"],
            "machine_id": r["machine_id"],
            "generation": r["generation"],
            "job_id": r["job_id"],
            "segment_number": r["seg_number"],
            "reason": r["reason"],
            "detail": _json_loads(r["detail"], None),
            "at_tick": r["at_tick"],
            "wall_ts": r["wall_ts"],
        } for r in rows]


def _log_rejection(conn: sqlite3.Connection, machine_id: str | None,
                   payload: dict | None, reason: str, message: str,
                   tick: int, detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO rejected_reports(machine_id, generation, job_id, seg_number, "
        "lease_token, reason, detail, payload_json, at_tick, wall_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (machine_id,
         (payload or {}).get("generation"),
         (payload or {}).get("job_id"),
         (payload or {}).get("seg_number"),
         (payload or {}).get("lease_token"),
         reason,
         _json_dumps(detail or {"message": message}),
         _json_dumps(payload or {}),
         tick, db.wall_now()),
    )


# ---------------------------------------------------------------- 正式结论裁决

def apply_result(machine_id: str, p: dict) -> tuple[int, dict]:
    """回报进入正式账本的唯一入口。任何不满足围栏/唯一性/时序的回报都被拒绝并留痕。"""
    with db.immediate_tx() as conn:
        tick = _tick(conn)
        m = conn.execute("SELECT * FROM machines WHERE id=?", (machine_id,)).fetchone()
        if m is None:
            return 404, {"reason": "machine_not_found", "message": "校验机不存在"}

        job_id = p.get("job_id")
        seg_number = p.get("seg_number")
        gen = p.get("generation")
        seg = conn.execute(
            "SELECT * FROM segments WHERE job_id=? AND seg_number=?",
            (job_id, seg_number),
        ).fetchone()

        # 1) 代次围栏：停机检修后重新启动会 +1，旧进程/旧消息一律失效
        if gen != m["generation"]:
            body = {"reason": "generation_fenced",
                    "message": f"回报代次 {gen} 与当前代次 {m['generation']} 不符",
                    "current_generation": m["generation"]}
            _log_rejection(conn, machine_id, p, "generation_fenced", body["message"], tick)
            return 409, body

        # 2) 每段唯一正式结论：已有结论，任何重复/迟到回报不得再入账
        existing = None
        if seg is not None:
            existing = conn.execute(
                "SELECT * FROM conclusions WHERE job_id=? AND seg_number=?",
                (job_id, seg_number),
            ).fetchone()
        if existing is not None:
            body = {
                "reason": "conclusion_exists",
                "message": "该段已有正式结论，重复/迟到回报不得覆盖",
                "official_conclusion": {
                    "verdict": existing["verdict"],
                    "machine_id": existing["machine_id"],
                    "generation": existing["generation"],
                    "recorded_tick": existing["recorded_tick"],
                },
            }
            _log_rejection(conn, machine_id, p, "conclusion_exists", body["message"],
                           tick, {"official_machine_id": existing["machine_id"]})
            return 409, body

        if seg is None:
            body = {"reason": "segment_not_found", "message": "段不存在"}
            _log_rejection(conn, machine_id, p, "segment_not_found", body["message"], tick)
            return 404, body

        # 3) 进程状态围栏：已确认退出的机器，迟到结果不得入账
        if m["status"] == "exited":
            body = {"reason": "machine_exited",
                    "message": "校验机进程已退出并完成退机确认，迟到回报不得入账",
                    "machine_status": m["status"], "pid": m["pid"],
                    "exit_code": m["exit_code"]}
            _log_rejection(conn, machine_id, p, "machine_exited", body["message"], tick)
            return 409, body

        # 4) 租约围栏：段必须仍由同一代次、同一 token 租在该机名下
        lease_ok = (
            seg["status"] == "leased"
            and seg["lease_token"] == p.get("lease_token")
            and seg["lease_machine"] == machine_id
            and seg["lease_generation"] == gen
        )
        if not lease_ok:
            body = {
                "reason": "lease_superseded",
                "message": "租约已失效（段可能已交回队列并被另一台重领）",
                "current_segment_status": seg["status"],
                "current_lease_machine": seg["lease_machine"],
                "current_lease_generation": seg["lease_generation"],
            }
            _log_rejection(conn, machine_id, p, "lease_superseded", body["message"], tick,
                           {"current_owner": seg["lease_machine"]})
            return 409, body

        # 5) 演练耗时：未到 due_tick 不允许提前记结论
        if tick < seg["due_tick"]:
            body = {"reason": "not_due",
                    "message": f"该段预计第 {seg['due_tick']} 拍验完，当前第 {tick} 拍",
                    "due_tick": seg["due_tick"], "tick": tick}
            _log_rejection(conn, machine_id, p, "not_due", body["message"], tick)
            return 409, body

        # 6) 比对清单，写唯一正式结论
        computed = p["computed_sha256"]
        match = int(computed == seg["expected_sha256"])
        verdict = "verified" if match else "mismatch"
        try:
            conn.execute(
                "INSERT INTO conclusions(job_id, seg_number, verdict, match, "
                "expected_sha256, computed_sha256, machine_id, generation, "
                "source_computation_id, recorded_tick, wall_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, seg_number, verdict, match, seg["expected_sha256"],
                 computed, machine_id, gen, p.get("source_computation_id"),
                 tick, db.wall_now()),
            )
        except sqlite3.IntegrityError:
            # 并发兜底：唯一索引已经有结论
            body = {"reason": "conclusion_exists", "message": "并发回报输掉唯一约束"}
            _log_rejection(conn, machine_id, p, "conclusion_exists", body["message"], tick)
            return 409, body

        conn.execute(
            "UPDATE segments SET status='completed', completed_tick=? WHERE id=?",
            (tick, seg["id"]),
        )

        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM segments WHERE job_id=? AND status!='completed'",
            (job_id,),
        ).fetchone()["c"]
        if remaining == 0:
            conn.execute(
                "UPDATE jobs SET status='completed', completed_tick=?, completed_wall=? "
                "WHERE id=?",
                (tick, db.wall_now(), job_id),
            )

        return 200, {
            "status": "recorded",
            "job_id": job_id,
            "segment_number": seg_number,
            "verdict": verdict,
            "match": bool(match),
            "machine_id": machine_id,
            "generation": gen,
            "recorded_tick": tick,
            "official_conclusion_unique": True,
        }


def submit_result(machine_id: str, payload: dict) -> tuple[int, dict]:
    """Worker 回报入口：若配置了截获规则，先入延迟箱，不做任何裁决。"""
    with db.immediate_tx() as conn:
        m = conn.execute("SELECT 1 FROM machines WHERE id=?", (machine_id,)).fetchone()
        if m is None:
            return 404, {"reason": "machine_not_found", "message": "校验机不存在"}
        seg_number = int(payload.get("seg_number", -1))
        if hold_rule_exists(conn, machine_id, seg_number):
            # 同一事务内写延迟箱后提交；绝不在这里再开第二个写事务
            held_id = _insert_held_report(conn, machine_id, payload)
            return 202, {"status": "held", "held_id": held_id,
                         "message": "回报已被演练延迟箱截获，停机后可调用 release 投递"}
    return apply_result(machine_id, payload)


def release_held_report(held_id: int) -> tuple[int, dict]:
    with db.reading() as conn:
        row = conn.execute("SELECT * FROM held_reports WHERE id=?", (held_id,)).fetchone()
        if row is None:
            return 404, {"reason": "held_report_not_found", "message": "延迟回报不存在"}
        machine_id = row["machine_id"]
        payload = _json_loads(row["payload_json"], {})

    status_code, body = apply_result(machine_id, payload)

    with db.immediate_tx() as conn:
        delivered = 200 <= status_code < 300
        conn.execute(
            "UPDATE held_reports SET status=?, reject_reason=?, release_http_status=?, "
            "response_json=?, released_wall=? WHERE id=?",
            ("released_accepted" if delivered else "released_rejected",
             None if delivered else body.get("reason"),
             status_code, _json_dumps(body), db.wall_now(), held_id),
        )
    return status_code, {"held_id": held_id, "delivery_status": body,
                         "http_status": status_code}


# ---------------------------------------------------------------- 退机计划

def _ref(seg: sqlite3.Row) -> dict:
    return {"job_id": seg["job_id"], "segment_number": seg["seg_number"]}


def create_shutdown_plan(req: Any) -> tuple[int, dict]:
    with db.immediate_tx() as conn:
        tick = _tick(conn)
        m = _machine_or_404(conn, req.machine_id)

        if m["status"] == "exited":
            raise ApiError(409, "machine_already_exited", "该机进程已退出，无需再受理停机")
        if m["status"] == "draining":
            prior = conn.execute(
                "SELECT id FROM shutdown_plans WHERE machine_id=? AND feasible=1 "
                "AND status='accepted' ORDER BY rowid DESC LIMIT 1",
                (req.machine_id,),
            ).fetchone()
            raise ApiError(409, "shutdown_already_accepted",
                           "该机已有受理中的退机计划",
                           {"shutdown_id": prior["id"] if prior else None})
        if req.deadline_tick < tick:
            raise ApiError(422, "deadline_in_past",
                           f"最晚停机拍 {req.deadline_tick} 早于当前演练拍 {tick}",
                           {"current_tick": tick})

        leases = conn.execute(
            "SELECT * FROM segments WHERE lease_machine=? AND status='leased' "
            "ORDER BY seg_number",
            (req.machine_id,),
        ).fetchall()
        by_number = {s["seg_number"]: s for s in leases}

        decisions: dict[int, str] = {}
        if req.decisions:
            for key, val in req.decisions.items():
                n = int(key)
                if n not in by_number:
                    raise ApiError(422, "segment_not_inflight",
                                   f"第 {n} 段当前不是该机在途段，无法决定 wait/return",
                                   {"inflight_segment_numbers": list(by_number)})
                decisions[n] = val

        wait_refs: list[dict] = []
        return_refs: list[dict] = []
        blockers: list[dict] = []

        for s in leases:
            n = s["seg_number"]
            due = int(s["due_tick"])
            late = due > req.deadline_tick
            decision = decisions.get(n, "return" if late else "wait")
            item = {
                **_ref(s),
                "claimed_tick": s["claimed_tick"],
                "due_tick": due,
                "duration_ticks": s["duration_ticks"],
                "late_vs_deadline": late,
                "decision": decision,
            }
            if decision == "wait":
                if late:
                    blockers.append({
                        **item,
                        "reason": "wait_misses_deadline",
                        "detail": f"等到第 {due} 拍才能验完，晚于最晚停机拍 "
                                  f"{req.deadline_tick}；应改为 return",
                    })
                else:
                    wait_refs.append(_ref(s))
            else:  # return
                return_refs.append(_ref(s))

        quota = max(0, int(req.max_reverify))
        quota_exceeded = len(return_refs) > quota
        if quota_exceeded:
            for ref in return_refs:
                s = by_number[ref["segment_number"]]
                due = int(s["due_tick"])
                blockers.append({
                    **_ref(s),
                    "claimed_tick": s["claimed_tick"],
                    "due_tick": due,
                    "duration_ticks": s["duration_ticks"],
                    "late_vs_deadline": due > req.deadline_tick,
                    "decision": "return",
                    "reason": "reverify_quota_exceeded",
                    "detail": (
                        f"该段必须交回重验（第 {due} 拍才能验完，晚于截止拍 "
                        f"{req.deadline_tick}），但最多可重验 {quota} 段；"
                        f"本计划共需重验 {len(return_refs)} 段"
                    ),
                })

        plan_id = "sd_" + uuid.uuid4().hex[:8]
        now = db.wall_now()
        feasible = not blockers

        if not feasible:
            # 只留阻塞记录，不动机器/租约 —— 原作业继续
            conn.execute(
                "INSERT INTO shutdown_plans(id, machine_id, generation, deadline_tick, "
                "max_reverify, status, feasible, current_tick, wait_segments, "
                "requeue_segments, blocking_segments, reverify_segment_numbers, "
                "message, request_json, created_wall, updated_wall) "
                "VALUES (?,?,?,?,?, 'blocked', 0, ?,?,?,?,?,?, ?,?,?)",
                (plan_id, req.machine_id, m["generation"], req.deadline_tick,
                 req.max_reverify, tick, _json_dumps(wait_refs),
                 _json_dumps(return_refs), _json_dumps(blockers),
                 _json_dumps([r["segment_number"] for r in return_refs]),
                 "限额/截止拍无法兼顾，退机未受理，原作业继续",
                 _json_dumps(req.model_dump()), now, now),
            )
            return 409, {
                "status": "blocked",
                "plan_id": plan_id,
                "reason": "quota_cannot_meet_deadline",
                "message": "最晚停机拍与可重验限额无法兼顾；已列出阻塞段，"
                           "机器未停领、租约未变，原作业继续",
                "machine_id": req.machine_id,
                "current_tick": tick,
                "deadline_tick": req.deadline_tick,
                "max_reverify": req.max_reverify,
                "inflight_count": len(leases),
                "required_max_reverify": len(return_refs),
                "blocking_segments": blockers,
                "wait_refs": wait_refs,
                "requeue_refs": return_refs,
            }

        # 可受理：旧机进入 draining，交回段立即回到 pending 队列
        planned_stop_tick = tick
        for ref in wait_refs:
            s = by_number[ref["segment_number"]]
            planned_stop_tick = max(planned_stop_tick, int(s["due_tick"]))
        # 没有等待段时，当前拍即可停机

        conn.execute("UPDATE machines SET status='draining' WHERE id=?", (req.machine_id,))
        for ref in return_refs:
            conn.execute(
                "UPDATE segments SET status='pending', lease_machine=NULL, "
                "lease_generation=NULL, lease_token=NULL, claimed_tick=NULL, "
                "due_tick=NULL, claim_wall=NULL "
                "WHERE job_id=? AND seg_number=? AND status='leased'",
                (ref["job_id"], ref["segment_number"]),
            )

        reverify_numbers = [r["segment_number"] for r in return_refs]
        conn.execute(
            "INSERT INTO shutdown_plans(id, machine_id, generation, deadline_tick, "
            "max_reverify, status, feasible, current_tick, wait_segments, "
            "requeue_segments, planned_stop_tick, reverify_segment_numbers, pid, "
            "message, request_json, created_wall, updated_wall) "
            "VALUES (?,?,?,?,?, 'accepted', 1, ?,?,?,?, ?,?,?, ?,?,?)",
            (plan_id, req.machine_id, m["generation"], req.deadline_tick,
             req.max_reverify, tick, _json_dumps(wait_refs),
             _json_dumps(return_refs), planned_stop_tick,
             _json_dumps(reverify_numbers), m["pid"],
             "退机计划已受理：旧机停领新段，等待段验完，交回段已重排队列",
             _json_dumps(req.model_dump()), now, now),
        )
        return 202, {
            "status": "accepted",
            "shutdown_id": plan_id,
            "machine_id": req.machine_id,
            "generation": m["generation"],
            "current_tick": tick,
            "deadline_tick": req.deadline_tick,
            "planned_stop_tick": planned_stop_tick,
            "on_time": planned_stop_tick <= req.deadline_tick,
            "wait_refs": wait_refs,
            "requeue_refs": return_refs,
            "wait_segment_numbers": [r["segment_number"] for r in wait_refs],
            "reverify_segment_numbers": reverify_numbers,
            "max_reverify": req.max_reverify,
            "message": "旧机已停止领取新段；推进演练时钟并调用 confirm-exit "
                       "确认进程实际退出后，才可报检修",
        }


def list_shutdown_plans() -> list[dict]:
    with db.reading() as conn:
        rows = conn.execute("SELECT * FROM shutdown_plans ORDER BY rowid DESC").fetchall()
        return [_plan_list_item(r) for r in rows]


def _plan_list_item(r: sqlite3.Row) -> dict:
    return {
        "shutdown_id": r["id"],
        "machine_id": r["machine_id"],
        "generation": r["generation"],
        "status": r["status"],
        "feasible": bool(r["feasible"]),
        "deadline_tick": r["deadline_tick"],
        "max_reverify": r["max_reverify"],
        "planned_stop_tick": r["planned_stop_tick"],
        "stop_tick": r["stop_tick"],
        "exit_confirmed": bool(r["exit_confirmed"]),
        "maintenance_ready": bool(r["maintenance_ready"]),
        "wait_refs": _json_loads(r["wait_segments"], []),
        "requeue_refs": _json_loads(r["requeue_segments"], []),
        "reverify_segment_numbers": _json_loads(r["reverify_segment_numbers"], []),
        "blocking_segments": _json_loads(r["blocking_segments"], []),
        "created_wall": r["created_wall"],
    }


def get_shutdown_plan(plan_id: str) -> dict:
    with db.reading() as conn:
        r = conn.execute("SELECT * FROM shutdown_plans WHERE id=?", (plan_id,)).fetchone()
        if not r:
            raise ApiError(404, "shutdown_not_found", f"停机单 {plan_id} 不存在")
        item = _plan_list_item(r)
        item["message"] = r["message"]
        item["pid"] = r["pid"]
        item["exit_code"] = r["exit_code"]
        item["confirmed_wall"] = r["confirmed_wall"]
        if r["status"] == "exit_confirmed":
            item["maintenance_report"] = _maintenance_report(conn, r)
        return item


def _reverify_details(conn: sqlite3.Connection, refs: list[dict]) -> list[dict]:
    details = []
    for ref in refs:
        s = conn.execute(
            "SELECT * FROM segments WHERE job_id=? AND seg_number=?",
            (ref["job_id"], ref["segment_number"]),
        ).fetchone()
        c = conn.execute(
            "SELECT * FROM conclusions WHERE job_id=? AND seg_number=?",
            (ref["job_id"], ref["segment_number"]),
        ).fetchone()
        details.append({
            "job_id": ref["job_id"],
            "segment_number": ref["segment_number"],
            "queue_status": s["status"] if s else "missing",
            "current_owner": s["lease_machine"] if s else None,
            "due_tick": s["due_tick"] if s else None,
            "official_conclusion": (
                None if c is None else {
                    "verdict": c["verdict"],
                    "match": bool(c["match"]),
                    "machine_id": c["machine_id"],
                    "recorded_tick": c["recorded_tick"],
                    "computed_sha256_short": _short(c["computed_sha256"]),
                }
            ),
        })
    return details


def _maintenance_report(conn: sqlite3.Connection, r: sqlite3.Row) -> dict:
    refs = _json_loads(r["requeue_segments"], [])
    wait_refs = _json_loads(r["wait_segments"], [])
    tick = _tick(conn)
    return {
        "maintenance_ready": bool(r["maintenance_ready"]),
        "shutdown_id": r["id"],
        "machine_id": r["machine_id"],
        "pid": r["pid"],
        "exit_code": r["exit_code"],
        "process_exit_confirmed": bool(r["exit_confirmed"]),
        "stop_tick": r["stop_tick"],
        "planned_stop_tick": r["planned_stop_tick"],
        "deadline_tick": r["deadline_tick"],
        "on_time": (r["stop_tick"] is not None and r["stop_tick"] <= r["deadline_tick"]),
        "confirmed_at_tick": tick,
        "reverify_segment_numbers": _json_loads(r["reverify_segment_numbers"], []),
        "reverify_refs": refs,
        "reverify_segments": _reverify_details(conn, refs),
        "waited_segments": _reverify_details(conn, wait_refs),
        "headline": f"校验机 {r['machine_id']} 进程已实际退出，"
                    f"停机拍 {r['stop_tick']}，可以检修",
    }


def confirm_process_exit(plan_id: str, supervisor: Any) -> tuple[int, dict]:
    with db.reading() as conn:
        r = conn.execute("SELECT * FROM shutdown_plans WHERE id=?", (plan_id,)).fetchone()
        if not r:
            return 404, {"reason": "shutdown_not_found", "message": "停机单不存在"}
        if r["status"] == "blocked":
            return 409, {"reason": "plan_blocked",
                         "message": "该停机请求因限额冲突未受理，无退机流程可确认"}
        if r["exit_confirmed"]:
            return 200, _maintenance_report(conn, r)
        machine_id = r["machine_id"]
        pid = r["pid"]

    # 关键：必须确认 OS 进程真的退出（Popen.poll / waitpid），而不是只看状态字段
    exited, exit_code = supervisor.confirm_exit(machine_id)
    if not exited:
        alive = supervisor.is_pid_alive(pid)
        return 409, {
            "reason": "process_not_exited",
            "message": f"校验机 {machine_id} 进程（pid={pid}）仍在运行，不能报可检修",
            "machine_id": machine_id, "pid": pid, "pid_alive_os_check": alive,
            "hint": "推进时钟让等待段验完，Worker 会自行优雅退出；随后重试本接口",
        }

    with db.immediate_tx() as conn:
        conn.execute(
            "UPDATE machines SET status='exited', exit_code=?, exited_wall=?, "
            "stop_tick=? WHERE id=?",
            (exit_code, db.wall_now(), r["planned_stop_tick"], machine_id),
        )
        conn.execute(
            "UPDATE shutdown_plans SET status='exit_confirmed', exit_confirmed=1, "
            "maintenance_ready=1, exit_code=?, stop_tick=planned_stop_tick, pid=?, "
            "confirmed_wall=?, updated_wall=? WHERE id=?",
            (exit_code, pid, db.wall_now(), db.wall_now(), plan_id),
        )
        r2 = conn.execute("SELECT * FROM shutdown_plans WHERE id=?", (plan_id,)).fetchone()
        return 200, _maintenance_report(conn, r2)
