"""演练配置：全部可通过环境变量覆盖。"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _path_from_env(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if not raw:
        return default
    p = Path(raw)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


# SQLite 数据库文件（多进程通过 FastAPI 串行化写，WAL 允许并发读）
DB_PATH = _path_from_env("DRILL_DB_PATH", PROJECT_ROOT / "data" / "drill.db")

# 只读样例卷目录（volume.bin + manifest.json）
VOLUME_DIR = _path_from_env("SAMPLE_VOLUME_DIR", PROJECT_ROOT / "sample_volume")

# API 监听 / Worker 回调地址
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
BASE_URL = os.environ.get("DRILL_BASE_URL", f"http://127.0.0.1:{PORT}")

# Worker 轮询间隔（秒，真实墙钟时间；演练耗时以“拍”为单位手工推进）
WORKER_POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "0.2"))

# 每台校验机最多同时验几段 —— 需求硬约束
MAX_INFLIGHT_PER_MACHINE = 2

MACHINE_IDS = ("m1", "m2")
MACHINE_NAMES = {"m1": "校验机一号", "m2": "校验机二号"}
