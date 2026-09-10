# Multi-stage build using uv (see Makefile's `image`/`build` targets and the
# uv workspace at the repo root). Only the `timekpr-hub` package and its own
# dependencies are installed -- not the dev group, and not the sibling
# `timekpr-hub-agent` package's httpx/dbus footprint.
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY core ./core
COPY hub ./hub
COPY agent/pyproject.toml ./agent/pyproject.toml

RUN uv sync --locked --no-dev --package timekpr-hub --no-editable

FROM python:3.12-slim

RUN groupadd --system hub && useradd --system --gid hub --no-create-home hub

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY hub/migrations /app/migrations
COPY hub/alembic.ini /app/alembic.ini

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1

USER hub
EXPOSE 8000

# `exec` on the final command so uvicorn (PID that matters, not `sh`) gets
# SIGTERM directly on `docker/podman stop` -- the original single-command
# form left `sh` as PID 1 and uvicorn as an unreachable child.
CMD ["sh", "-c", "alembic -c alembic.ini upgrade head && exec uvicorn timekpr_hub.app:app --host 0.0.0.0 --port 8000"]
