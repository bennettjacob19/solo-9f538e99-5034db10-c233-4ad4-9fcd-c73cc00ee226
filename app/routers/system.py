"""系统接口：健康检查与演练复位。"""
from __future__ import annotations

from fastapi import APIRouter

from .. import db
from ..supervisor import supervisor

router = APIRouter(tags=["系统"])


@router.get("/health", summary="健康检查：数据库、时钟、两个进程的真实存活状态")
def health() -> dict:
    with db.reading() as conn:
        tick = conn.execute("SELECT tick FROM clock WHERE id=1").fetchone()["tick"]
        rows = conn.execute("SELECT id, status, pid, generation, exit_code "
                            "FROM machines ORDER BY id").fetchall()
    machines = []
    for r in rows:
        alive = supervisor.is_pid_alive(r["pid"]) if r["status"] != "exited" else False
        machines.append({
            "machine_id": r["id"],
            "status": r["status"],
            "pid": r["pid"],
            "generation": r["generation"],
            "exit_code": r["exit_code"],
            "pid_alive_os": alive,
        })
    return {"status": "ok", "clock_tick": tick, "machines": machines}


@router.post("/admin/reset", summary="复位整场演练：停掉两个进程、清库、重新拉起")
def reset_drill() -> dict:
    supervisor.stop_all()
    db.recreate_db()
    started = supervisor.start_all(bump_generation=False)
    return {
        "status": "reset",
        "clock_tick": 0,
        "machines_started": started,
        "note": "数据库已重建，两台校验机以 generation=1 重新启动",
    }
