"""延迟回报箱与被拒回报留痕。"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import services

router = APIRouter(tags=["延迟回报箱"])


@router.get("/held-reports", summary="延迟箱中的回报（可按机器/状态过滤）")
def list_held(machine_id: str | None = None, status: str | None = None) -> list[dict]:
    return services.list_held_reports(machine_id, status)


@router.post("/held-reports/{held_id}/release",
             summary="停机后放出旧回报：过围栏裁决，迟到结果会被拒绝且不入账")
def release_held(held_id: int) -> JSONResponse:
    code, body = services.release_held_report(held_id)
    return JSONResponse(status_code=code, content=body)


@router.get("/rejected-reports", summary="所有被拒绝的迟到/陈旧/重复回报留痕")
def list_rejected(job_id: str | None = None, limit: int = 100) -> list[dict]:
    return services.list_rejected(job_id, limit)
