"""校验机 Worker：两台机器各跑一个本进程（独立 OS 进程，PID 不同）。

循环：
1. 拉 /work 快照（代次围栏 + 当前有效租约 + draining 状态）；
2. 对新领段立即只读打开样例卷，逐段实算 SHA-256 并登记“实算摘要”；
3. 到 due_tick 后才提交正式回报；
4. 租约被退机计划收回时，做一次“交回附带回报”的最佳努力提交
   （若配置了延迟截获，它会进延迟箱，等停机后才投递并被围栏拒绝）；
5. draining 且本地没有未决段时优雅退出 0，由监管器 waitpid 确认。
"""
from __future__ import annotations

import argparse
import logging
import signal
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import volume  # 仅用 hash_segment 时不需要 manifest；导入保持包完整


@dataclass
class LocalTask:
    job_id: str
    segment_number: int
    token: str
    due_tick: int
    claimed_tick: int
    computed_sha256: str
    byte_count: int
    hash_wall_ms: float
    computation_id: int | None = None
    result_sent: bool = False
    revoke_flush_done: bool = False


def build_logger(machine_id: str) -> logging.Logger:
    logger = logging.getLogger(f"worker.{machine_id}")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            f"%(asctime)s [worker-{machine_id}] %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
        ))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def run(machine_id: str, base_url: str, generation: int, poll_seconds: float) -> int:
    log = build_logger(machine_id)
    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame: Any) -> None:
        log.info("received signal %s, finishing loop and exiting 0", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # 本机回环调用不走环境代理
    client = httpx.Client(base_url=base_url, timeout=6.0, trust_env=False)
    tasks: dict[str, LocalTask] = {}  # key = lease_token

    log.info("boot gen=%s base_url=%s max_inflight=2", generation, base_url)

    def post_result(task: LocalTask, *, revoked_flush: bool) -> bool:
        """提交回报。返回 True 表示该任务可以收尾（成功或被明确拒绝）。"""
        payload = {
            "generation": generation,
            "lease_token": task.token,
            "job_id": task.job_id,
            "seg_number": task.segment_number,
            "computed_sha256": task.computed_sha256,
            "byte_count": task.byte_count,
            "hash_wall_ms": task.hash_wall_ms,
            "source_computation_id": task.computation_id,
        }
        try:
            resp = client.post(f"/machines/{machine_id}/results", json=payload)
        except httpx.HTTPError as exc:
            log.warning("result post network error seg=%s: %r",
                        task.segment_number, exc)
            return False  # 网络错误，下轮重试

        if 200 <= resp.status_code < 300:
            body = resp.json()
            if resp.status_code == 202 and body.get("status") == "held":
                log.info("seg=%s result buffered in held-box id=%s (revoked_flush=%s)",
                         task.segment_number, body.get("held_id"), revoked_flush)
            else:
                log.info("seg=%s official result: %s",
                         task.segment_number, body.get("verdict") or body.get("status"))
            return True

        # 4xx：围栏/唯一性/时序明确拒绝，不重试，但打印原因
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {"message": resp.text[:200]}
        log.warning("seg=%s result REJECTED http=%s reason=%s (revoked_flush=%s)",
                    task.segment_number, resp.status_code,
                    body.get("reason") or body.get("detail"), revoked_flush)
        return True

    try:
        while not stop_event.is_set():
            # 1) 拉工作快照
            try:
                resp = client.get(
                    f"/machines/{machine_id}/work",
                    params={"generation": generation},
                )
            except httpx.HTTPError as exc:
                log.warning("work snapshot network error: %r; retry", exc)
                stop_event.wait(poll_seconds * 2)
                continue

            if resp.status_code == 409:
                body = resp.json()
                log.error("fenced by server (%s); stale process exits now",
                          body.get("reason"))
                return 0
            if resp.status_code >= 500:
                log.warning("work snapshot http %s; retry", resp.status_code)
                stop_event.wait(poll_seconds * 2)
                continue
            resp.raise_for_status()
            snap = resp.json()
            server_tick = int(snap["tick"])
            status = snap["status"]
            active_tokens = {l["lease_token"] for l in snap["active_leases"]}

            # 2) 租约被收回的本地任务：最佳努力交回一次（可能被延迟箱截获）
            for token, task in list(tasks.items()):
                if token in active_tokens:
                    continue
                if not task.revoke_flush_done and not task.result_sent:
                    task.revoke_flush_done = True
                    settled = post_result(task, revoked_flush=True)
                    if not settled:
                        # 网络失败也不继续占着：退机优先；真实场景由重验兜底
                        log.warning("revoke flush failed network-wise; drop seg=%s",
                                    task.segment_number)
                tasks.pop(token, None)

            # 3) 到期任务提交正式回报
            for token, task in list(tasks.items()):
                if task.result_sent:
                    tasks.pop(token, None)
                    continue
                if server_tick >= task.due_tick:
                    settled = post_result(task, revoked_flush=False)
                    if settled:
                        task.result_sent = True
                        tasks.pop(token, None)

            # 4) active 状态且有空槽才领新段（draining 时绝不领新段）
            if status == "active":
                while len(tasks) < 2 and not stop_event.is_set():
                    try:
                        cr = client.post(
                            f"/machines/{machine_id}/claim",
                            json={"generation": generation},
                        )
                    except httpx.HTTPError as exc:
                        log.warning("claim network error: %r", exc)
                        break
                    if cr.status_code == 409:
                        log.error("claim fenced: %s; exit", cr.text[:200])
                        return 0
                    cr.raise_for_status()
                    data = cr.json()
                    lease = data.get("leased")
                    if not lease:
                        reason = data.get("reason")
                        if reason not in ("at_capacity", "no_work"):
                            log.info("claim skipped: %s", reason)
                        break

                    # 5) 领到立即实算 SHA-256（耗时拍是演练模拟，实算本身很快）
                    digest, nbytes, ms = volume.hash_segment(
                        lease["volume_path"],
                        lease["offset_bytes"],
                        lease["size_bytes"],
                    )
                    expected = lease["expected_sha256"]
                    log.info(
                        "claimed job=%s seg=%s due_tick=%s real_hash=%s.. "
                        "match_manifest=%s bytes=%s hash_ms=%.2f",
                        lease["job_id"], lease["segment_number"], lease["due_tick"],
                        digest[:12], digest == expected, nbytes, ms,
                    )
                    task = LocalTask(
                        job_id=lease["job_id"],
                        segment_number=lease["segment_number"],
                        token=lease["lease_token"],
                        due_tick=lease["due_tick"],
                        claimed_tick=lease["claimed_tick"],
                        computed_sha256=digest,
                        byte_count=nbytes,
                        hash_wall_ms=ms,
                    )
                    # 登记实算摘要（失败不阻断，结论回报里仍带摘要）
                    try:
                        comp = client.post(
                            f"/machines/{machine_id}/computations",
                            json={
                                "generation": generation,
                                "lease_token": task.token,
                                "job_id": task.job_id,
                                "seg_number": task.segment_number,
                                "computed_sha256": digest,
                                "byte_count": nbytes,
                                "hash_wall_ms": ms,
                                "claimed_tick": task.claimed_tick,
                            },
                        )
                        if comp.status_code < 300:
                            task.computation_id = comp.json().get("computation_id")
                    except httpx.HTTPError as exc:
                        log.warning("computation upload failed: %r", exc)

                    tasks[task.token] = task
            elif status == "draining":
                log.info("draining: no new claims; pending_local_tasks=%d", len(tasks))

            # 6) draining 且本地全部了结 -> 真正退出（监管器随后 waitpid 确认）
            if status == "draining" and not tasks:
                log.info("all waited segments settled; exiting 0")
                return 0

            stop_event.wait(poll_seconds)

        log.info("stop event set; exit 0")
        return 0
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="备份段 SHA-256 校验机 Worker")
    parser.add_argument("--machine-id", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--poll-seconds", type=float,
                        default=float(__import__("os").environ.get(
                            "WORKER_POLL_SECONDS", "0.2")))
    args = parser.parse_args()
    raise SystemExit(run(args.machine_id, args.base_url,
                         args.generation, args.poll_seconds))


if __name__ == "__main__":
    main()
