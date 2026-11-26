"""请求/响应模型。示例会直接展示在 /docs 页面。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ---------- 时钟 ----------

class AdvanceClockRequest(BaseModel):
    ticks: int | None = Field(default=None, ge=1, description="向前推进多少拍")
    to_tick: int | None = Field(default=None, ge=0, description="直接推进到第几拍（不能回拨）")

    model_config = {
        "json_schema_extra": {"examples": [{"to_tick": 4}, {"ticks": 2}]}
    }


# ---------- 作业 ----------

class JobCreateRequest(BaseModel):
    note: str | None = Field(default="值班演练作业", description="备注")
    segment_count: int = Field(
        default=4, ge=1, le=32, description="取清单前多少段（与 segment_numbers 二选一）"
    )
    segment_numbers: list[int] | None = Field(
        default=None, description="显式指定清单段号（从 1 开始）"
    )
    default_duration_ticks: int = Field(
        default=3, ge=1, le=10_000, description="每段默认耗时（演练拍）"
    )
    segment_durations: dict[int, int] | None = Field(
        default=None,
        description="按段号覆盖耗时，例如 {\"1\": 2, \"3\": 5}",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "note": "夜班备份校验-退机演练",
                "segment_count": 4,
                "default_duration_ticks": 3,
                "segment_durations": {"1": 2, "2": 3, "3": 5, "4": 7},
            }
        }
    }


class DurationUpdateRequest(BaseModel):
    duration_ticks: int = Field(ge=1, le=10_000, description="新的耗时拍数")

    model_config = {"json_schema_extra": {"example": {"duration_ticks": 6}}}


# ---------- Worker 回报 ----------

class ClaimRequest(BaseModel):
    generation: int = Field(..., description="机器当前代次（启动时分配，围栏用）")
    pid: int | None = None


class ComputationRequest(BaseModel):
    generation: int
    lease_token: str
    job_id: str
    seg_number: int
    computed_sha256: str = Field(..., min_length=64, max_length=64)
    byte_count: int = Field(ge=0)
    hash_wall_ms: float = Field(ge=0)
    claimed_tick: int | None = None


class ResultRequest(BaseModel):
    generation: int
    lease_token: str
    job_id: str
    seg_number: int
    computed_sha256: str = Field(..., min_length=64, max_length=64)
    byte_count: int = Field(ge=0)
    hash_wall_ms: float = Field(ge=0)
    source_computation_id: int | None = None
    client_match: bool | None = Field(
        default=None, description="Worker 自行与清单比对的结果，仅作参考"
    )


# ---------- 停机 ----------

class ShutdownRequest(BaseModel):
    machine_id: str = Field(..., examples=["m1"], description="要停掉的校验机")
    deadline_tick: int = Field(
        ..., ge=0, description="最晚停机拍（含）；到该拍仍算不完的在途段必须交回"
    )
    max_reverify: int = Field(
        ..., ge=0, le=2, description="最多允许交回/重验的在途段数（每台在途上限 2）"
    )
    decisions: dict[int, Literal["wait", "return"]] | None = Field(
        default=None,
        description="可选：逐段决定 wait（等验完）或 return（交回队列）；缺省自动判断",
    )
    note: str | None = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "machine_id": "m1",
                "deadline_tick": 8,
                "max_reverify": 1,
            }
        }
    }


# ---------- 延迟回报箱 / 截获规则 ----------

class HoldRuleRequest(BaseModel):
    segment_number: int = Field(
        ..., description="要截获回报的段号；传 -1 表示截获该机所有段"
    )

    model_config = {"json_schema_extra": {"example": {"segment_number": 3}}}
