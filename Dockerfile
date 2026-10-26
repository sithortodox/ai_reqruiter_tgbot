FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/bot

# Системные зависимости: компиляция wheels не нужен — все пакеты бинарные
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY prompts ./prompts
COPY scripts ./scripts
COPY app ./app

RUN chmod +x scripts/entrypoint.sh

# Хранилище резюме — том (переживает пересоздание контейнера)
VOLUME ["/srv/bot/storage"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8080/healthz || exit 1

ENTRYPOINT ["scripts/entrypoint.sh"]