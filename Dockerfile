# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.7.8 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev

FROM python:3.12-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin pitcrew \
    && mkdir -p /data /app \
    && chown -R pitcrew:pitcrew /data /app

WORKDIR /app
COPY --from=builder --chown=pitcrew:pitcrew /app/.venv /app/.venv
COPY --chown=pitcrew:pitcrew pyproject.toml README.md ./
COPY --chown=pitcrew:pitcrew src ./src
COPY --chown=pitcrew:pitcrew brand ./brand
COPY --chown=pitcrew:pitcrew agent ./agent

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    PYTHONUNBUFFERED=1 \
    PIT_CREW_LEDGER_PATH="/data/ledger.db" \
    PIT_CREW_MEDIA_DIR="/data/media" \
    PIT_CREW_BRAND_DIR="/app/brand"

USER pitcrew
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["python", "-m", "pit_crew.runtime_cli", "serve", "--host", "0.0.0.0", "--port", "8000", "--ledger", "/data/ledger.db"]
