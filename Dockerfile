# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.12.7 AS uv

FROM python:3.12-slim AS build
COPY --from=uv /uv /bin/uv
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# 1. Third-party deps: exactly what uv.lock pins, wheels only (--no-build =>
#    no dependency setup script ever runs). 2. Build and install this repo's
#    own package from its just-copied source (first-party, no external code).
RUN uv sync --frozen --no-dev --no-install-project --no-build \
    && uv build --wheel --no-sources --out-dir /tmp/dist \
    && uv pip install --no-deps --no-build /tmp/dist/*.whl

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
