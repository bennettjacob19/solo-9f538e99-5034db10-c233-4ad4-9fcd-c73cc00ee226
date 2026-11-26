# 备份校验退机交接演练镜像
# 单容器内运行：1 个 FastAPI 监管进程 + 2 个独立 Worker OS 子进程
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    DRILL_BASE_URL=http://127.0.0.1:8000 \
    DRILL_DB_PATH=/data/drill.db \
    SAMPLE_VOLUME_DIR=/srv/sample_volume \
    WORKER_POLL_SECONDS=0.25

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码
COPY app ./app
COPY scripts ./scripts

# 构建期确定性生成只读样例卷 + 清单，并移除写位（运行时 compose 还会 read_only 根文件系统）
RUN python -m app.volume --dir /srv/sample_volume --segments 8 \
    && chmod -R a-w /srv/sample_volume \
    && mkdir -p /data \
    && useradd --create-home --uid 10001 drill \
    && chown drill:drill /data

USER drill

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=5s --timeout=4s --start-period=6s --retries=12 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "app.main"]
