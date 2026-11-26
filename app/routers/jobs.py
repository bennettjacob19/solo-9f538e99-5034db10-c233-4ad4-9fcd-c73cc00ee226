"""校验作业与各段实算摘要/结论查询。"""
from __future__ import annotations

from fastapi import APIRouter

from .. import services
from ..schemas import DurationUpdateRequest, JobCreateRequest

router = APIRouter(prefix="/jobs", tags=["校验作业"])


@router.post("", status_code=201, summary="提交校验作业（可同时设置各段耗时）")
def create_job(req: JobCreateRequest) -> dict:
    return services.create_job(req)


@router.get("", summary="作业列表与进度")
def list_jobs() -> list[dict]:
    return services.list_jobs()


@router.get("/{job_id}", summary="单个作业详情")
def get_job(job_id: str) -> dict:
    return services.get_job(job_id)


@router.get("/{job_id}/segments",
            summary="按作业查各段：实算 SHA-256 摘要 + 唯一正式结论")
def get_segments(job_id: str) -> dict:
    return services.segment_views(job_id)


@router.patch("/{job_id}/segments/{segment_number}",
              summary="设置/修改某一段的演练耗时（在途租约按领段快照执行）")
def update_segment_duration(job_id: str, segment_number: int,
                            req: DurationUpdateRequest) -> dict:
    return services.update_duration(job_id, segment_number, req.duration_ticks)
