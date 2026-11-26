"""校验机进程、Worker 领段/实算/回报、延迟截获规则。"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import services
from ..schemas import (
    ClaimRequest,
    ComputationRequest,
    HoldRuleRequest,
    ResultRequest,
)
from ..supervisor import supervisor

router = APIRouter(prefix="/machines", tags=["校验机进程"])


@router.get("", summary="两台校验机：PID、状态、代次、在途段")
def list_machines() -> list[dict]:
    items = services.list_machines()
    for item in items:
        item["pid_alive_os"] = supervisor.is_pid_alive(item.get("pid"))
    return items


@router.get("/{machine_id}", summary="单台校验机详情（含租约 token，便于演练核对）")
def get_machine(machine_id: str) -> dict:
    view = services.get_machine(machine_id)
    view["pid_alive_os"] = supervisor.is_pid_alive(view.get("pid"))
    return view


@router.post("/{machine_id}/start",
             summary="检修后重新拉起进程（代次 +1，旧代次回报会被围栏拒绝）")
def start_machine(machine_id: str) -> dict:
    return supervisor.start_machine(machine_id, bump_generation=True)


@router.get("/{machine_id}/work",
            summary="Worker 轮询的工作快照：状态/时钟/有效租约/退机计划")
def work(machine_id: str, generation: int) -> dict:
    return services.work_snapshot(machine_id, generation)


@router.post("/{machine_id}/claim", summary="原子领取一段（每台最多 2 段在途）")
def claim(machine_id: str, req: ClaimRequest) -> JSONResponse:
    code, body = services.claim_segment(machine_id, req.generation, req.pid)
    return JSONResponse(status_code=code, content=body)


@router.post("/{machine_id}/computations", status_code=201,
             summary="登记一次真实 SHA-256 实算摘要（可有多条，不是正式结论）")
def computation(machine_id: str, req: ComputationRequest) -> JSONResponse:
    code, body = services.add_computation(machine_id, req.model_dump())
    return JSONResponse(status_code=code, content=body)


@router.post("/{machine_id}/results",
             summary="Worker 正式回报：过围栏后才能写成每段唯一结论")
def result(machine_id: str, req: ResultRequest) -> JSONResponse:
    code, body = services.submit_result(machine_id, req.model_dump())
    return JSONResponse(status_code=code, content=body)


# ---------------- 延迟回报截获规则 ----------------

@router.get("/{machine_id}/holds", tags=["延迟回报箱"],
            summary="查看该机的回报截获规则")
def list_holds(machine_id: str) -> list[dict]:
    return services.list_hold_rules(machine_id)


@router.post("/{machine_id}/holds", tags=["延迟回报箱"], status_code=201,
             summary="截获指定段的下一次回报到延迟箱（segment_number=-1 为全部）")
def add_hold(machine_id: str, req: HoldRuleRequest) -> dict:
    return services.create_hold_rule(machine_id, req.segment_number)


@router.delete("/{machine_id}/holds/{segment_number}", tags=["延迟回报箱"],
               summary="删除截获规则")
def delete_hold(machine_id: str, segment_number: int) -> dict:
    return services.delete_hold_rule(machine_id, segment_number)
