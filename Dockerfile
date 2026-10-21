FROM python:3.11-slim AS wheels

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt ./requirements.txt
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.11-slim AS runtime

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt /app/backend/requirements.txt
COPY --from=wheels /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels \
        -r /app/backend/requirements.txt \
    && rm -rf /wheels
COPY backend/app /app/backend/app
RUN mkdir -p /app/data/logs /app/logs

ARG APP_REVISION=unknown
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/app/data/bstocks.db \
    APP_REVISION=${APP_REVISION}
LABEL io.bajiao.project="bajiao" \
    org.opencontainers.image.source="https://github.com/david-yang-pixel/bajiao" \
    org.opencontainers.image.revision="${APP_REVISION}"

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl --fail --silent --show-error http://127.0.0.1:8000/api/health || exit 1
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
