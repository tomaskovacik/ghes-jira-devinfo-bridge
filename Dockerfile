# syntax=docker/dockerfile:1

FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip build \
    && python -m build --wheel --outdir /dist

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    STATE_PATH=/data/state.json
RUN useradd --system --uid 10001 --home-dir /home/bridge --create-home bridge \
    && mkdir /data && chown bridge:bridge /data
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
USER bridge
VOLUME ["/data"]
HEALTHCHECK --interval=1m --timeout=5s --start-period=30s --retries=3 \
    CMD test -f "${STATE_PATH}.healthy" \
        && test "$(( $(date +%s) - $(stat -c %Y "${STATE_PATH}.healthy") ))" -lt 5400
ENTRYPOINT ["python", "-m", "bridge"]
