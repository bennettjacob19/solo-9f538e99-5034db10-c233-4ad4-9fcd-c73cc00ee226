"""退机停机：受理可行性计划、确认进程实际退出、返回检修报告。"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import services
from ..schemas import ShutdownRequest
from ..supervisor import supervisor

router = APIRouter(prefix="/shutdowns", tags=["退机停机"])


@router.post("", status_code=202,
             summary="选定要停的进程，填最晚停机拍和最多可重验在途段数")
def request_shutdown(req: ShutdownRequest) -> JSONResponse:
    code, body = services.create_shutdown_plan(req)
    return JSONResponse(status_code=code, content=body)


@router.get("", summary="停机单列表（含 blocked 记录）")
def list_shutdowns() -> list[dict]:
    return services.list_shutdown_plans()


@router.get("/{plan_id}", summary="停机单详情 / 检修报告")
def get_shutdown(plan_id: str) -> dict:
    return services.get_shutdown_plan(plan_id)


@router.post("/{plan_id}/confirm-exit",
             summary="确认 OS 进程实际退出；未退出返回 409，退出后返回停机拍与重验段号")
def confirm_exit(plan_id: str) -> JSONResponse:
    code, body = services.confirm_process_exit(plan_id, supervisor)
    return JSONResponse(status_code=code, content=body)
