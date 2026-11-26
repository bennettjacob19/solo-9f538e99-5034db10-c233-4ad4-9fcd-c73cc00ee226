"""FastAPI 入口：备份校验退机交接本地演练。

启动时：
- 初始化 SQLite schema；
- 确保只读样例卷 + 清单存在；
- 由 Supervisor 拉起 m1/m2 两个独立 Worker 进程并后台回收。

/docs 可完成：提交作业 -> 设置各段耗时 -> 推进时钟 -> 受理退机 ->
确认进程退出 -> 放出迟到回报 -> 查询各段实算摘要与唯一结论。
"""
from __future__ import annotations

import contextlib
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import config, db, volume
from .routers import clock as clock_router
from .routers import held as held_router
from .routers import jobs as jobs_router
from .routers import machines as machines_router
from .routers import shutdowns as shutdowns_router
from .routers import system as system_router
from .services import ApiError
from .supervisor import supervisor

DESCRIPTION = """
两台**独立 OS 进程**校验机（m1/m2）从同一份**只读样例卷**逐段实算 SHA-256，
与清单核对；每台最多同时验 **2** 段。

## 推荐演练顺序

1. `POST /jobs` 提交作业并设置各段 `segment_durations`（多少拍）；
2. `GET /machines` 观察两台进程领满 2 段、PID 与实算摘要；
3. `POST /shutdowns` 选择要停的机器，填 `deadline_tick`（最晚停机拍）
   与 `max_reverify`（最多可重验在途段数）；限额冲突时返回 **blocking_segments**，
   原作业继续；
4. `POST /clock/advance` 推进演练时钟；旧机 draining 不领新段，另一台照常接单；
5. `POST /shutdowns/{id}/confirm-exit`：只有 OS 进程**实际退出**才返回
   “可检修”报告（含 `stop_tick` 与 `reverify_segment_numbers`）；
6. 可用 `POST /machines/{id}/holds` 截获旧机回报，停机后
   `POST /held-reports/{id}/release` 放出：迟到结果被围栏拒绝、**不得入账**；
7. `GET /jobs/{id}/segments` 查看每段实算摘要与**唯一**正式结论。
"""


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    volume.ensure_sample_volume(config.VOLUME_DIR)
    # 每次 API 启动都 bump 代次，防止上一代孤儿 Worker 仍以旧身份写库
    supervisor.start_all(bump_generation=True)
    reaper_task = __import__("asyncio").create_task(supervisor.reaper_loop())
    try:
        yield
    finally:
        reaper_task.cancel()
        supervisor.stop_all()


app = FastAPI(
    title="备份校验退机交接演练",
    description=DESCRIPTION,
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.include_router(system_router.router)
app.include_router(clock_router.router)
app.include_router(jobs_router.router)
app.include_router(machines_router.router)
app.include_router(shutdowns_router.router)
app.include_router(held_router.router)


@app.exception_handler(ApiError)
async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=config.HOST,
        port=config.PORT,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
