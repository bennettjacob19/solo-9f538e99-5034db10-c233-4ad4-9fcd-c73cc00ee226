"""在 FastAPI 进程内监管两台校验机：它们是两个独立的 OS 进程。

职责：
- 用 ``sys.executable -m app.worker`` 拉起 m1/m2，记录 PID 与代次；
- 后台 reap 子进程退出码并回写 SQLite；
- ``confirm_exit`` 以 ``Popen.poll()``（waitpid）作为“进程实际退出”的唯一凭据；
- 演练复位时整体停掉再重启。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from typing import Optional

from . import config, db


def _log(msg: str) -> None:
    print(f"[supervisor] {msg}", flush=True)


def pid_alive_os(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Supervisor:
    def __init__(self) -> None:
        self._procs: dict[str, subprocess.Popen] = {}
        self._finished: dict[str, subprocess.Popen] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 启动/停止

    def _current_generation(self, machine_id: str) -> int:
        with db.reading() as conn:
            row = conn.execute(
                "SELECT generation FROM machines WHERE id=?", (machine_id,)
            ).fetchone()
            return int(row["generation"]) if row else 1

    def start_machine(self, machine_id: str, *, bump_generation: bool = False) -> dict:
        with self._lock:
            existing = self._procs.get(machine_id)
            if existing is not None and existing.poll() is None:
                from .services import ApiError
                raise ApiError(409, "machine_already_running",
                               f"{machine_id} 进程仍在运行 (pid={existing.pid})")
            self._procs.pop(machine_id, None)
            self._finished.pop(machine_id, None)

            with db.immediate_tx() as conn:
                row = conn.execute(
                    "SELECT generation FROM machines WHERE id=?", (machine_id,)
                ).fetchone()
                if row is None:
                    raise RuntimeError(f"machine {machine_id} not seeded")
                generation = int(row["generation"])
                if bump_generation:
                    generation += 1
                conn.execute(
                    "UPDATE machines SET generation=?, status='active', pid=NULL, "
                    "exit_code=NULL, stop_tick=NULL, started_wall=?, exited_wall=NULL "
                    "WHERE id=?",
                    (generation, db.wall_now(), machine_id),
                )

            env = os.environ.copy()
            env["DRILL_BASE_URL"] = config.BASE_URL
            env["WORKER_POLL_SECONDS"] = str(config.WORKER_POLL_SECONDS)
            cmd = [
                sys.executable, "-u", "-m", "app.worker",
                "--machine-id", machine_id,
                "--base-url", config.BASE_URL,
                "--generation", str(generation),
                "--poll-seconds", str(config.WORKER_POLL_SECONDS),
            ]
            _log(f"starting {machine_id} gen={generation}: {' '.join(cmd)}")
            proc = subprocess.Popen(
                cmd,
                cwd=str(config.PROJECT_ROOT),
                env=env,
                # 独立进程组，复位/退出时可以整组清理
                start_new_session=True,
            )
            self._procs[machine_id] = proc
            with db.immediate_tx() as conn:
                conn.execute(
                    "UPDATE machines SET pid=? WHERE id=?", (proc.pid, machine_id)
                )
            return {"machine_id": machine_id, "pid": proc.pid, "generation": generation}

    def start_all(self, *, bump_generation: bool = True) -> list[dict]:
        started = []
        for mid in config.MACHINE_IDS:
            try:
                started.append(self.start_machine(mid, bump_generation=bump_generation))
            except Exception as exc:  # noqa: BLE001
                _log(f"failed to start {mid}: {exc!r}")
        return started

    def stop_all(self, timeout: float = 6.0) -> None:
        with self._lock:
            procs = list(self._procs.items())
            for mid, proc in procs:
                if proc.poll() is None:
                    _log(f"terminating {mid} pid={proc.pid}")
                    try:
                        proc.terminate()  # Worker 安装了 SIGTERM handler，会优雅退出 0
                    except Exception:  # noqa: BLE001
                        pass
            deadline = time.time() + timeout
            for mid, proc in procs:
                remaining = max(0.1, deadline - time.time())
                try:
                    proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    _log(f"killing {mid} process group")
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:  # noqa: BLE001
                        proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except Exception:  # noqa: BLE001
                        pass
            self._procs.clear()
            self._finished.clear()

    # ------------------------------------------------------------ 运行状态

    def reap_once(self) -> None:
        with self._lock:
            for mid, proc in list(self._procs.items()):
                code = proc.poll()
                if code is None:
                    continue
                # 只在状态转换的这一次回写 DB / 打日志，避免对僵尸返回码重复开写事务
                self._procs.pop(mid, None)
                self._finished[mid] = proc
                try:
                    with db.immediate_tx() as conn:
                        row = conn.execute(
                            "SELECT status, pid FROM machines WHERE id=?", (mid,)
                        ).fetchone()
                        if row and row["status"] != "exited":
                            conn.execute(
                                "UPDATE machines SET status='exited', exit_code=?, "
                                "exited_wall=? WHERE id=?",
                                (code, db.wall_now(), mid),
                            )
                    _log(f"{mid} pid={proc.pid} exited code={code}")
                except Exception as exc:  # noqa: BLE001
                    _log(f"reap update failed for {mid}: {exc!r}")

    async def reaper_loop(self) -> None:
        import asyncio
        while True:
            try:
                await asyncio.to_thread(self.reap_once)
            except Exception as exc:  # noqa: BLE001
                _log(f"reaper iteration error: {exc!r}")
            await asyncio.sleep(0.25)

    def confirm_exit(self, machine_id: str) -> tuple[bool, Optional[int]]:
        """返回 (是否已被 waitpid 回收, 退出码)。这是“进程实际退出”的硬确认。"""
        with self._lock:
            proc = self._procs.get(machine_id)
            if proc is not None:
                code = proc.poll()
                if code is None:
                    return False, None
                self.reap_once()  # RLock 可重入：完成状态转换与回写
                return True, code

            finished = self._finished.get(machine_id)
            if finished is not None:
                return True, finished.returncode

            # 不由本监管器托管（例如独立部署的 Worker）：只能参考 DB 状态
            with db.reading() as conn:
                row = conn.execute(
                    "SELECT status, exit_code FROM machines WHERE id=?",
                    (machine_id,),
                ).fetchone()
            if row and row["status"] == "exited":
                return True, row["exit_code"]
            return False, None

    def is_pid_alive(self, pid: int | None) -> bool:
        return pid_alive_os(pid)

    def runtime_snapshot(self) -> dict[str, dict]:
        snap: dict[str, dict] = {}
        with self._lock:
            for mid, proc in self._procs.items():
                snap[mid] = {"pid": proc.pid, "poll": proc.poll()}
        return snap


# 路由与 lifespan 共用的单例
supervisor = Supervisor()
