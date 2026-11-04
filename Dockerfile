# syntax=docker/dockerfile:1
# Based on the official uv Docker guide: https://docs.astral.sh/uv/guides/integration/docker/
FROM python:3.13.18-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (cached layer)
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

# Copy the application code
COPY . /app

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# Unprivileged user; uid/gid match the host owner of bind-mounted log dirs (APP_UID/APP_GID).
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --system --non-unique --gid "${APP_GID}" app \
    && useradd --system --non-unique --uid "${APP_UID}" --gid "${APP_GID}" \
       --home-dir /app --no-create-home app \
    && mkdir -p /app/logs \
    && chown -R "${APP_UID}:${APP_GID}" /app/logs

ENV PATH="/app/.venv/bin:$PATH"

USER app

CMD ["python", "-m", "app.entrypoints.master_bot"]
