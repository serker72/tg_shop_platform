# syntax=docker/dockerfile:1
# Based on the official uv Docker guide: https://docs.astral.sh/uv/guides/integration/docker/

# Stage `test` (make test-unit): dev dependencies in /opt/venv, the code is bind-mounted into /app.
# Only pyproject.toml + uv.lock are used, so the layer is cached until they change. Not part of
# the production image: the last stage below stays the default build target (docker-compose.yml).
FROM python:3.13.18-slim AS test

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

# Unit tests call make/git/openssl/bash; safe.directory: /app belongs to the host uid
RUN apt-get update \
    && apt-get install -y --no-install-recommends make git openssl bash \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory /app

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

# Production image (default target: the last stage)
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

# Build version (ops/scripts/version.sh --env via Makefile/compose build.args); read by
# app/core/version.py. Declared last: changing them does not invalidate the layers above.
# Empty values -> the app reports "<pyproject base>+unknown".
ARG APP_VERSION=""
ARG APP_COMMIT=""
ARG APP_BUILD_TIME=""
ENV APP_VERSION="${APP_VERSION}" \
    APP_COMMIT="${APP_COMMIT}" \
    APP_BUILD_TIME="${APP_BUILD_TIME}"

USER app

CMD ["python", "-m", "app.entrypoints.master_bot"]
