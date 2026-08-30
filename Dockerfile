# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.5.9 AS uv

FROM python:3.12-slim AS build
COPY --from=uv /uv /bin/uv
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# Reproducible install: resolved versions come only from uv.lock, no source builds
# of third-party deps (wheels only), project installed non-editable into /app/.venv.
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    STATE_PATH=/data/state.json \
    PATH=/app/.venv/bin:$PATH
RUN useradd --system --uid 10001 --home-dir /home/bridge --create-home bridge \
    && mkdir /data && chown bridge:bridge /data
COPY --from=build --chown=bridge:bridge /app/.venv /app/.venv
USER bridge
VOLUME ["/data"]
HEALTHCHECK --interval=1m --timeout=5s --start-period=30s --retries=3 \
    CMD test -f "${STATE_PATH}.healthy" \
        && test "$(( $(date +%s) - $(stat -c %Y "${STATE_PATH}.healthy") ))" -lt 5400
ENTRYPOINT ["python", "-m", "bridge"]
