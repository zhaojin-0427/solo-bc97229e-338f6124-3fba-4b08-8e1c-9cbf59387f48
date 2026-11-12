FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_DATA_DIR=/data \
    APP_DB_PATH=/data/app.db

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 单 worker + 多线程：保证后台任务线程在同一进程内可访问内存锁，
# 任务状态与续跑依赖 SQLite 持久化，不依赖内存
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD ["python", "-I", "/app/app/healthcheck.py"]

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", \
     "--threads", "8", "--timeout", "120", "app.main:app"]
