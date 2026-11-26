#!/usr/bin/env python3
"""退机交接端到端试跑（只依赖 httpx + 标准库）。

覆盖需求：
1. 两台独立进程各领满 2 段、真实 SHA-256 与清单核对；
2. 限额无法兼顾 -> 409 + blocking_segments，原作业继续并全部验完；
3. 可受理的退机：旧机 draining 停领、等待段验完、在途段交回重验；
4. confirm-exit 以 OS 进程实际退出为准，返回 stop_tick / reverify 段号；
5. 旧机回报截获到停机后放出 -> machine_exited 拒绝；
6. 另一台重验入账后，再放旧回报 -> conclusion_exists，每段仍只有一个结论；
7. 旧进程代次重放 -> generation_fenced。

退出码 0 表示全部断言通过。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Callable

import httpx

PASS_COUNT = 0
FAIL_COUNT = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS_COUNT, FAIL_COUNT
    if condition:
        PASS_COUNT += 1
        print(f"  \033[32m[PASS]\033[0m {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL_COUNT += 1
        print(f"  \033[31m[FAIL]\033[0m {name}" + (f" — {detail}" if detail else ""))
        raise AssertionError(name)


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def wait_for(fn: Callable[[], Any], timeout: float = 30.0,
             interval: float = 0.15, label: str = "") -> Any:
    deadline = time.time() + timeout
    last: Any = None
    while time.time() < deadline:
        try:
            result = fn()
            if result:
                return result
            last = "condition not met yet"
        except Exception as exc:  # noqa: BLE001
            last = repr(exc)
        time.sleep(interval)
    raise AssertionError(f"等待超时: {label} (last={last!r})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url",
                        default=os.environ.get("DRILL_BASE_URL", "http://127.0.0.1:8000"))
    args = parser.parse_args()

    c = httpx.Client(base_url=args.base_url, timeout=20.0, trust_env=False)

    # ------------------------------------------------------------ 0. 复位
    section("0. 复位演练环境并等待两台校验机上线")
    h = c.get("/health").json()
    print("health before reset:", h["status"], "tick", h["clock_tick"])
    r = c.post("/admin/reset")
    r.raise_for_status()
    print("reset:", r.json()["machines_started"])

    def both_active() -> list[dict] | None:
        ms = c.get("/machines").json()
        if all(m["status"] == "active" and m["pid_alive_os"] for m in ms):
            return ms
        return None

    ms = wait_for(both_active, label="两台 active 且 PID 存活")
    for m in ms:
        print(f"  {m['machine_id']} pid={m['pid']} gen={m['generation']}")
    check("两台独立进程均存活", len(ms) == 2)

    # ---------------------------------------- 1. 限额冲突：列出阻塞段，作业继续
    section("1. 限额无法兼顾：deadline=1, max_reverify=0（两段都来不及）")
    job_a = c.post("/jobs", json={
        "note": "A-限额冲突应被拒绝且作业继续",
        "segment_count": 4,
        "segment_durations": {"1": 2, "2": 3, "3": 4, "4": 5},
    }).json()
    ja = job_a["job_id"]
    print("job A:", ja)

    def saturated_a() -> dict | None:
        machines = c.get("/machines").json()
        if all(m["active_lease_count"] == 2 for m in machines):
            return {m["machine_id"]: m for m in machines}
        return None

    pool = wait_for(saturated_a, label="两台各领满 2 段")
    m1a = pool["m1"]
    check("每台最多同时验 2 段",
          all(m["active_lease_count"] == 2 for m in pool.values()),
          f"m1={m1a['active_lease_count']}, m2={pool['m2']['active_lease_count']}")

    r = c.post("/shutdowns", json={
        "machine_id": "m1", "deadline_tick": 1, "max_reverify": 0,
    })
    check("冲突请求返回 409", r.status_code == 409, f"http={r.status_code}")
    blocked = r.json()
    check("状态为 blocked", blocked.get("status") == "blocked")
    check("列出 2 个阻塞段",
          len(blocked.get("blocking_segments", [])) == 2,
          f"required_max_reverify={blocked.get('required_max_reverify')}")
    for b in blocked["blocking_segments"]:
        print(f"    blocker: {b['job_id']}#seg{b['segment_number']} "
              f"due={b['due_tick']} reason={b['reason']}")
    check("系统给出所需重验额度", blocked.get("required_max_reverify") == 2)

    m1 = c.get("/machines/m1").json()
    check("未受理后 m1 仍是 active", m1["status"] == "active", m1["status"])
    check("租约未被改动（仍 2 段在途）", len(m1["active_leases"]) == 2)

    print("  -> 推进时钟到第 5 拍，原作业应不受影响并全部验完")
    c.post("/clock/advance", json={"to_tick": 5})

    def job_completed(job_id: str) -> dict | None:
        j = c.get(f"/jobs/{job_id}").json()
        return j if j["status"] == "completed" else None

    ja_view = wait_for(lambda: job_completed(ja), timeout=20, label="job A completed")
    segs_a = c.get(f"/jobs/{ja}/segments").json()["segments"]
    check("作业 A 4 段全部完成", len(segs_a) == 4 and
          all(s["official_conclusion"] for s in segs_a))
    check("作业 A 每段恰好一个结论且 verified",
          all(s["official_conclusion"]["verdict"] == "verified" for s in segs_a))
    print(f"  job A progress={ja_view['progress']} "
          f"verified={ja_view['verified_count']} mismatch={ja_view['mismatch_count']}")

    # ------------------------- 2. 可受理退机：1 段等待 + 1 段交回，延迟回报
    section("2. 退机交接：按实际 due_tick 选 deadline，构造 1 等 1 交回")
    job_b = c.post("/jobs", json={
        "note": "B-退机交接+迟到回报拒绝",
        "segment_count": 4,
        "segment_durations": {"1": 2, "2": 3, "3": 5, "4": 7},
    }).json()
    jb = job_b["job_id"]
    print("job B:", jb, " clock:", c.get("/clock").json()["tick"])

    def saturated_b() -> dict | None:
        machines = c.get("/machines").json()
        ok = (len(machines) == 2 and
              all(m["active_lease_count"] == 2 and
                  all(l["job_id"] == jb for l in m["active_leases"])
                  for m in machines))
        return {m["machine_id"]: m for m in machines} if ok else None

    poolb = wait_for(saturated_b, label="两台各领到 job B 的 2 段")
    m1 = c.get("/machines/m1").json()
    gen = m1["generation"]
    pid = m1["pid"]
    leases = sorted(m1["active_leases"], key=lambda l: l["due_tick"])
    w_lease, r_lease = leases[0], leases[-1]
    w_seg, r_seg = w_lease["segment_number"], r_lease["segment_number"]
    deadline = w_lease["due_tick"]
    old_token = r_lease["lease_token"]
    print(f"  m1 pid={pid} gen={gen} leases="
          f"{[(l['segment_number'], l['due_tick']) for l in leases]}")
    print(f"  => 等待段 seg{w_seg}(due={w_lease['due_tick']})，"
          f"交回段 seg{r_seg}(due={r_lease['due_tick']})，deadline={deadline}")
    check("两段 due 不同，可构造一等一交回", w_seg != r_seg and
          r_lease["due_tick"] > deadline)

    # 等 m1 对交回段的真实实算摘要落库
    def m1_digest_for_r() -> str | None:
        view = c.get(f"/jobs/{jb}/segments").json()["segments"]
        seg = next(s for s in view if s["segment_number"] == r_seg)
        for comp in seg["computations"]:
            if comp["machine_id"] == "m1":
                return comp["computed_sha256"]
        return None

    old_digest = wait_for(m1_digest_for_r, label="m1 对交回段的实算摘要")
    seg_r_view = next(s for s in c.get(f"/jobs/{jb}/segments").json()["segments"]
                      if s["segment_number"] == r_seg)
    old_size = seg_r_view["size_bytes"]
    check("旧机已真实算出 SHA-256", len(old_digest) == 64, old_digest[:12] + "...")

    # 截获旧机对 r_seg 的回报（退机交回时 worker 会 best-effort 投一次）
    rr = c.post("/machines/m1/holds", json={"segment_number": r_seg})
    check("已设置延迟截获规则", rr.status_code in (200, 201))

    r = c.post("/shutdowns", json={
        "machine_id": "m1", "deadline_tick": deadline, "max_reverify": 1,
    })
    check("可行退机返回 202", r.status_code == 202, f"http={r.status_code} {r.text[:200]}")
    plan = r.json()
    plan_id = plan["shutdown_id"]
    check("等待段集合正确", plan["wait_segment_numbers"] == [w_seg],
          str(plan["wait_segment_numbers"]))
    check("重验段号正确", plan["reverify_segment_numbers"] == [r_seg],
          str(plan["reverify_segment_numbers"]))
    check("计划停机拍 <= 最晚停机拍", plan["planned_stop_tick"] == deadline
          and plan["on_time"])

    # 旧机立即停领新段（直接替它调 claim 验证服务端规则）
    cr = c.post("/machines/m1/claim", json={"generation": gen}).json()
    check("旧机 draining 后服务端拒领新段", cr.get("leased") is None
          and cr.get("reason") == "draining", str(cr.get("reason")))

    # 延迟箱收到旧机回报
    def held_captured() -> int | None:
        rows = c.get("/held-reports",
                     params={"machine_id": "m1", "status": "held"}).json()
        for h in rows:
            if h["segment_number"] == r_seg and h["job_id"] == jb:
                return h["held_id"]
        return None

    held_id = wait_for(held_captured, timeout=8, label="延迟箱截获旧机回报")
    check("旧机回报进入延迟箱", isinstance(held_id, int), f"held_id={held_id}")

    # 推进到 deadline，等待段验完 -> worker 自行退出 -> confirm-exit 成功
    c.post("/clock/advance", json={"to_tick": deadline})

    report = None
    for _ in range(80):
        rr = c.post(f"/shutdowns/{plan_id}/confirm-exit")
        if rr.status_code == 200:
            report = rr.json()
            break
        time.sleep(0.15)
    check("confirm-exit 最终返回 200", report is not None,
          "进程在限定时间内实际退出")
    if report is None:
        print("  confirm-exit last:", rr.status_code, rr.text[:300])
        raise AssertionError("confirm-exit failed")

    check("报告 maintenance_ready=true", report["maintenance_ready"] is True)
    check("OS 确认进程退出", report["process_exit_confirmed"] is True
          and report["exit_code"] == 0, f"exit_code={report['exit_code']}")
    check("返回停机拍 stop_tick==deadline", report["stop_tick"] == deadline,
          f"stop_tick={report['stop_tick']} deadline={deadline}")
    check("返回重验段号", report["reverify_segment_numbers"] == [r_seg])
    print("  检修报告 headline:", report["headline"])

    # 脚本与进程在同一 PID 命名空间时（本机直接跑 / docker compose exec），
    # 直接做 OS 级旁证；独立 drill-test 容器有自己的 PID ns，只能依赖 API 确认。
    from urllib.parse import urlparse
    host = urlparse(args.base_url).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1"):
        try:
            os.kill(pid, 0)
            os_alive = True
        except ProcessLookupError:
            os_alive = False
        except PermissionError:
            os_alive = True
        check("os.kill(pid,0) 旁证进程不存在", os_alive is False, f"pid={pid}")
    else:
        print("  [SKIP] 跨容器运行，OS PID 旁证不适用，以 confirm-exit 为准")

    # ------------------------- 3. 停机后放出延迟回报：machine_exited 被拒
    section("3. 停机后放出旧机延迟回报 -> 不得入账")
    rr = c.post(f"/held-reports/{held_id}/release")
    check("延迟回报被裁决为 409", rr.status_code == 409, f"http={rr.status_code}")
    released = rr.json()
    reason = released.get("delivery_status", {}).get("reason")
    check("拒绝原因是 machine_exited", reason == "machine_exited", f"reason={reason}")

    seg_r = next(s for s in c.get(f"/jobs/{jb}/segments").json()["segments"]
                 if s["segment_number"] == r_seg)
    check("被拒后该段仍无正式结论", seg_r["official_conclusion"] is None,
          f"status={seg_r['status']}")

    c.delete(f"/machines/m1/holds/{r_seg}")

    # ------------------------- 4. 另一台照常接单并完成重验
    section("4. m2 接单重验交回段（自适应推进时钟）")

    def r_state() -> dict:
        view = c.get(f"/jobs/{jb}/segments").json()
        return next(s for s in view["segments"] if s["segment_number"] == r_seg)

    deadline_wall = time.time() + 45
    while time.time() < deadline_wall:
        s = r_state()
        if s["official_conclusion"]:
            break
        m2 = c.get("/machines/m2").json()
        tick = c.get("/clock").json()["tick"]
        lease2 = next((l for l in m2["active_leases"]
                       if l["job_id"] == jb and l["segment_number"] == r_seg), None)
        if lease2:
            if tick < lease2["due_tick"]:
                c.post("/clock/advance", json={"to_tick": lease2["due_tick"]})
            else:
                time.sleep(0.15)
        else:
            # 段在队列里：推进到 m2 最早的在途 due，让它腾槽接单
            dues = [l["due_tick"] for l in m2["active_leases"]
                    if l["job_id"] == jb]
            target = min(dues) if dues else tick + 1
            if target > tick:
                c.post("/clock/advance", json={"to_tick": target})
            else:
                time.sleep(0.15)
    else:
        raise AssertionError("m2 重验超时")

    seg_r = r_state()
    concl_r = seg_r["official_conclusion"]
    check("交回段已由 m2 重验入账", concl_r is not None
          and concl_r["machine_id"] == "m2",
          f"conclusion={concl_r and concl_r['machine_id']}")
    check("重验结论 verified", concl_r and concl_r["verdict"] == "verified")

    # ------------------------- 5. 旧回报重放：conclusion_exists，仍唯一
    section("5. 再放旧机回报（直打 result 接口）-> conclusion_exists")
    stale_payload = {
        "generation": gen,
        "lease_token": old_token,
        "job_id": jb,
        "seg_number": r_seg,
        "computed_sha256": old_digest,
        "byte_count": old_size,
        "hash_wall_ms": 0.42,
    }
    rr = c.post("/machines/m1/results", json=stale_payload)
    check("陈旧回报返回 409", rr.status_code == 409)
    check("拒绝原因 conclusion_exists",
          rr.json().get("reason") == "conclusion_exists", rr.json().get("reason"))

    seg_view_b = c.get(f"/jobs/{jb}/segments").json()
    conclusions = [s["official_conclusion"] for s in seg_view_b["segments"]]
    check("job B 每段恰有 1 个正式结论（共 4 个）",
          sum(x is not None for x in conclusions) == 4)
    r_conclusions_owner = [s["official_conclusion"]["machine_id"]
                           for s in seg_view_b["segments"]
                           if s["segment_number"] == r_seg]
    check("交回段结论仍只属于 m2", r_conclusions_owner == ["m2"])
    jb_view = c.get(f"/jobs/{jb}").json()
    check("job B 已 completed", jb_view["status"] == "completed", jb_view["status"])

    rej = c.get("/rejected-reports", params={"job_id": jb}).json()
    reasons = {x["reason"] for x in rej}
    check("留痕包含 machine_exited", "machine_exited" in reasons, str(sorted(reasons)))
    check("留痕包含 conclusion_exists", "conclusion_exists" in reasons)

    # ------------------------- 6. 检修后重启 + 代次围栏
    section("6. m1 检修后重新上线，旧代次消息被 generation_fenced 拒绝")
    started = c.post("/machines/m1/start")
    check("重启接口 200", started.status_code == 200, started.text[:200])
    new_info = started.json()
    check("代次 +1 且 PID 更换",
          new_info["generation"] == gen + 1 and new_info["pid"] != pid,
          f"new_pid={new_info['pid']} gen={new_info['generation']}")
    wait_for(lambda: (m := c.get("/machines/m1").json()) and m["status"] == "active"
             and m["pid_alive_os"], timeout=10, label="m1 重新 active")

    rr = c.post("/machines/m1/results", json=stale_payload)
    check("旧代次回报被 generation_fenced 拒绝",
          rr.status_code == 409 and rr.json().get("reason") == "generation_fenced",
          f"{rr.status_code} {rr.json().get('reason')}")

    # 余量压测：6 段 + 长耗时，确认在途上限 2、队列等待
    job_c = c.post("/jobs", json={
        "note": "C-在途上限与队列",
        "segment_count": 6,
        "default_duration_ticks": 100,
    }).json()
    jc = job_c["job_id"]

    def cap_check() -> bool:
        ms = c.get("/machines").json()
        return (all(m["active_lease_count"] <= 2 for m in ms)
                and sum(m["active_lease_count"] for m in ms) == 4)

    wait_for(cap_check, timeout=10, label="4 段在途、其余排队")
    ms = c.get("/machines").json()
    check("6 段作业：总共只放 4 段在途，每台 <=2",
          sorted(m["active_lease_count"] for m in ms) == [2, 2])

    # ------------------------- 汇总打印 job B 各段
    section("附：job B 各段实算摘要与正式结论")
    for s in seg_view_b["segments"]:
        comps = ", ".join(f"{x['machine_id']}:{x['computed_sha256_short']}"
                          for x in s["computations"]) or "-"
        oc = s["official_conclusion"]
        oc_txt = (f"{oc['verdict']} by {oc['machine_id']}@tick{oc['recorded_tick']} "
                  f"({oc['computed_sha256_short']})" if oc else "无")
        print(f"  seg{s['segment_number']}: status={s['status']:<9} "
              f"实算[{comps}] 结论=[{oc_txt}]")

    print("\n" + "#" * 78)
    print(f"# 全部关键路径试跑通过：PASS={PASS_COUNT} FAIL={FAIL_COUNT}")
    print("#" * 78)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"\n\033[31mDRILL FAILED: {exc}\033[0m", file=sys.stderr)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        print(f"\n\033[31mHTTP ERROR: {exc!r}\033[0m", file=sys.stderr)
        raise SystemExit(2)
