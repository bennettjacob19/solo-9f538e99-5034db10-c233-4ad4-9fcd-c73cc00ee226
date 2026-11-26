"""演练时钟：手工推进，所有段耗时以“拍”计。"""
from __future__ import annotations

from fastapi import APIRouter

from .. import services
from ..schemas import AdvanceClockRequest

router = APIRouter(prefix="/clock", tags=["演练时钟"])


@router.get("", summary="读取当前演练拍")
def get_clock() -> dict:
    return services.clock_view()


@router.post("/advance", summary="推进时钟：传 ticks（推进几拍）或 to_tick（到第几拍）")
def advance_clock(req: AdvanceClockRequest) -> dict:
    if req.ticks is None and req.to_tick is None:
        # 缺省推进 1 拍，方便 /docs 连点
        return services.advance_clock()
    return services.advance_clock(ticks=req.ticks, to_tick=req.to_tick)


@router.post("/tick", summary="推进 1 拍")
def tick_once() -> dict:
    return services.advance_clock(ticks=1)
