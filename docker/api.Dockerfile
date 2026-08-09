# syntax=docker/dockerfile:1.7
#
# ApplicantOS API image — FastAPI + the WebSocket event bus, and nothing else.
#
# Build from the repository root, because the build context is the whole tree:
#
#     docker build -f docker/api.Dockerfile -t applicantos-api .
#
# Two stages. The builder owns a compiler and a package index; the runtime owns neither, and
# receives only the finished virtualenv plus the source. That is what keeps `gcc`, the pip
# cache and the wheel build directories out of the shipped image and off the attack surface.
#
# **The project is installed editable on purpose.** `[tool.setuptools.package-data]` in
# `pyproject.toml` declares only `app/config/*.yaml`, so a wheel silently omits
# `app/ai/prompts/*.md` and `app/documents/templates/*.j2`. The image would build and start
# and then fail at the first tailoring pass with a missing prompt. Editable + the real source
# tree is the honest shape until that packaging metadata grows.
#
# This image deliberately does **not** carry Chromium or a LaTeX engine. The API dispatches
# Celery tasks; it never drives a browser and never typesets a PDF. Those live in
# `docker/worker.Dockerfile`.

ARG PYTHON_VERSION=3.12


# ======================================================================================
# Stage 1 — builder
# ======================================================================================

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Only the toolchain needed to build a wheel that has no prebuilt one for this platform.
# `--no-install-recommends` is what keeps this from dragging in a documentation tree.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# An explicit venv rather than the system interpreter: one directory to copy into the
# runtime stage, and no chance of a stray `pip install` colliding with Debian's Python.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    VIRTUAL_ENV=/opt/venv

WORKDIR /app

# The serving stack first, alone, so that editing a Python file does not re-resolve and
# re-download every dependency. This layer changes only when the requirements file does.
COPY docker/requirements-runtime.txt /tmp/requirements-runtime.txt
RUN pip install --upgrade pip setuptools wheel \
    && pip install --requirement /tmp/requirements-runtime.txt

# Then the project itself, with the extras that make up the server posture:
#   postgres — asyncpg for `database_url`, psycopg for the derived `sync_database_url`
#              that Alembic runs on. `app/database/session.py` builds the engine at import
#              time, so a missing driver is an ImportError, not a connection error.
#   redis    — cache backend, Celery broker and result backend
#   orjson   — cache value codec
#   pgvector — native `vector(n)` columns instead of JSON-encoded embeddings
COPY . /app
RUN pip install --editable ".[postgres,redis,orjson,pgvector]"


# ======================================================================================
# Stage 2 — runtime
# ======================================================================================

FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="ApplicantOS API" \
      org.opencontainers.image.description="FastAPI backend for ApplicantOS." \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/applicantos/applicantos"

# PYTHONUNBUFFERED is load-bearing rather than cosmetic: without it structlog's JSON lines
# sit in a 8KB pipe buffer and `docker logs` shows nothing during an incident.
ENV PATH="/opt/venv/bin:${PATH}" \
    VIRTUAL_ENV=/opt/venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ca-certificates is the only runtime system dependency: every ATS provider, the GitHub
# analyzer and both model clients speak TLS, and a slim image ships no trust store.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# A fixed, high, non-root uid. Fixed so a bind-mounted volume has predictable ownership;
# high so it cannot collide with a host account if the daemon is running rootless.
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --gid "${APP_GID}" applicantos \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin applicantos

WORKDIR /app

COPY --from=builder --chown=${APP_UID}:${APP_GID} /opt/venv /opt/venv
COPY --from=builder --chown=${APP_UID}:${APP_GID} /app /app

# `DATA_DIR` and everything derived from it (`storage_local_root`, `cache_dir`,
# `screenshot_dir`, `browser_user_data_dir`) are created on first access by the settings
# properties. Creating them here, owned by the app user, means that happens without the
# container ever needing write access to `/app` itself.
RUN mkdir -p /app/var/storage /app/var/cache /app/var/screenshots /app/var/browser \
    && chown -R "${APP_UID}:${APP_GID}" /app/var

USER applicantos

EXPOSE 8000

# `/health` is the liveness probe and touches no dependency by design (app/api/routes/
# health.py): it stays true while PostgreSQL is down, so a database outage never turns into
# a container restart loop. Readiness — which does probe the database and Redis — is
# `/ready`, and belongs to the orchestrator, not to Docker's binary healthcheck.
#
# urllib rather than curl: it is already in the image, and adding curl for a probe means
# shipping a general-purpose HTTP client to every production container.
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
    CMD ["python", "-c", "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]

# 0.0.0.0 rather than the `api_host` default of 127.0.0.1: a loopback bind inside a
# container is reachable by nothing. Compose sets API_HOST to match, so the value the
# desktop client reads back from `GET /settings` describes reality.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
