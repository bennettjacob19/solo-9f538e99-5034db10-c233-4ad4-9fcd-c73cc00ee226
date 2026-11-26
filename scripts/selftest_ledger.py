#!/usr/bin/env python3
"""账本级自检（不起 HTTP、不起 Worker）：

- 真实生成样例卷，篡改一个字节后 SHA-256 与清单不符；
- apply_result 对错误摘要给 verdict=mismatch；
- 同段第二次回报被 conclusion_exists 拒绝；
- SQLite 触发器禁止 UPDATE/DELETE conclusions。

运行：python scripts/selftest_ledger.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="drill-selftest-"))
os.environ["DRILL_DB_PATH"] = str(_TMP / "selftest.db")
os.environ["SAMPLE_VOLUME_DIR"] = str(_TMP / "volume")

import sqlite3  # noqa: E402

from app import db, services, volume  # noqa: E402
from app.schemas import JobCreateRequest  # noqa: E402

failures: list[str] = []


def expect(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def main() -> int:
    # 1) 真实哈希 + 篡改检测
    manifest = volume.generate_sample_volume(_TMP / "volume", segment_count=2,
                                             segment_size=4096, force=True)
    vol = _TMP / "volume" / "volume.bin"
    digest, n, ms = volume.hash_segment(vol, 0, 4096)
    expect("正常段 SHA-256 与清单一致", digest == manifest["segments"][0]["sha256"],
           f"{ms:.3f}ms / {n}B")

    tampered = _TMP / "tampered.bin"
    shutil.copyfile(vol, tampered)
    os.chmod(tampered, 0o600)
    with open(tampered, "r+b") as f:
        f.seek(10)
        f.write(bytes([f.read(1)[0] ^ 0xFF]))
    digest_bad, _, _ = volume.hash_segment(tampered, 0, 4096)
    expect("篡改 1 字节后摘要不同", digest_bad != manifest["segments"][0]["sha256"])

    # 2) 内存级临时库
    db.init_db()
    with db.immediate_tx() as conn:
        conn.execute("UPDATE machines SET status='active' WHERE id='m1'")

    req = JobCreateRequest(note="selftest", segment_count=1,
                           default_duration_ticks=1)
    job = services.create_job(req)
    jid = job["job_id"]

    code, body = services.claim_segment("m1", 1, None)
    lease = body["leased"]
    expect("领段成功", code == 200 and lease is not None)

    services.advance_clock(to_tick=lease["due_tick"])

    wrong = "0" * 64
    code, body = services.apply_result("m1", {
        "generation": 1, "lease_token": lease["lease_token"],
        "job_id": jid, "seg_number": 1,
        "computed_sha256": wrong, "byte_count": 4096, "hash_wall_ms": 0.1,
    })
    expect("错误摘要 -> mismatch 结论",
           code == 200 and body["verdict"] == "mismatch", str(body.get("verdict")))

    code, body = services.apply_result("m1", {
        "generation": 1, "lease_token": lease["lease_token"],
        "job_id": jid, "seg_number": 1,
        "computed_sha256": manifest["segments"][0]["sha256"],
        "byte_count": 4096, "hash_wall_ms": 0.1,
    })
    expect("第二次回报被唯一结论拒绝",
           code == 409 and body["reason"] == "conclusion_exists", body.get("reason", ""))

    # 3) 触发器不可改/不可删
    conn = db.connect()
    try:
        blocked_update = blocked_delete = False
        try:
            conn.execute("UPDATE conclusions SET verdict='verified' WHERE job_id=?", (jid,))
        except sqlite3.Error:
            blocked_update = True
            conn.rollback()
        try:
            conn.execute("DELETE FROM conclusions WHERE job_id=?", (jid,))
        except sqlite3.Error:
            blocked_delete = True
            conn.rollback()
        expect("触发器禁止 UPDATE 正式结论", blocked_update)
        expect("触发器禁止 DELETE 正式结论", blocked_delete)
    finally:
        conn.close()

    view = services.segment_views(jid)
    oc = view["segments"][0]["official_conclusion"]
    expect("查询仍只看到最初的 mismatch 结论",
           oc is not None and oc["verdict"] == "mismatch")

    print()
    if failures:
        print(f"SELFTEST FAILED: {failures}")
        return 1
    print("SELFTEST PASSED")
    shutil.rmtree(_TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
