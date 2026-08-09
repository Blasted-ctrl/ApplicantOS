# syntax=docker/dockerfile:1.7
#
# ApplicantOS worker image — Celery, headless Chromium, and a LaTeX engine.
#
# Build from the repository root:
#
#     docker build -f docker/worker.Dockerfile -t applicantos-worker .
#
# One image serves all five queues (`discovery`, `ai`, `apply`, `knowledge`, `maintenance`).
# Splitting it per queue would save nothing: the discovery and AI workers would still carry
# the base Python layer, and the apply queue — the only one that needs Chromium — is the one
# that must never be starved of capacity. `docker-compose.yml` runs several containers from
# this single image, each consuming a different `-Q`.
#
# It is the API image plus two things the API has no use for:
#
#   * **Chromium**, via `playwright install --with-deps chromium`, which also installs the
#     ~40 shared libraries headless Chromium needs on Debian.
#   * **Tectonic**, because `Settings.pdf_engine` defaults to `latex` and
#     `Settings.latex_binary` to `tectonic`. See `docker/install-tectonic.sh` for why it is
#     fetched from upstream rather than installed with apt.

ARG PYTHON_VERSION=3.12

# Pinned rather than "latest": a worker that silently changes typesetting engine between two
# builds produces two different PDFs from one `ResumeVersion.content_json`, which breaks the
# "the rendered PDF is disposable, the JSON is forever" guarantee (golden rule #6).
ARG TECTONIC_VERSION=0.17.0

# Optional SHA-256 of the Tectonic tarball for the build architecture. Empty means the
# download is not verified and the installer says so, loudly, in the build log.
ARG TECTONIC_SHA256=""


# ======================================================================================
# Stage 1 — builder
# ======================================================================================

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    VIRTUAL_ENV=/opt/venv

WORKDIR /app

# Both requirements files are copied because `requirements-worker.txt` starts with
# `-r requirements-runtime.txt`; pip resolves that relative to the file, not to the CWD.
COPY docker/requirements-runtime.txt docker/requirements-worker.txt /tmp/requirements/
RUN pip install --upgrade pip setuptools wheel \
    && pip install --requirement /tmp/requirements/requirements-worker.txt

COPY . /app
RUN pip install --editable ".[postgres,redis,orjson,pgvector]"


# ======================================================================================
# Stage 2 — runtime
# ======================================================================================

FROM python:${PYTHON_VERSION}-slim AS runtime

ARG TECTONIC_VERSION
ARG TECTONIC_SHA256

LABEL org.opencontainers.image.title="ApplicantOS worker" \
      org.opencontainers.image.description="Celery worker: discovery, AI, apply, knowledge, maintenance." \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/applicantos/applicantos"

# PLAYWRIGHT_BROWSERS_PATH moves the browser out of the installing user's home directory.
# The default (`~/.cache/ms-playwright`) would put a root-owned Chromium where the app user
# cannot read it, and the failure surfaces as "Executable doesn't exist" at the first apply.
ENV PATH="/opt/venv/bin:${PATH}" \
    VIRTUAL_ENV=/opt/venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --gid "${APP_GID}" applicantos \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin applicantos

COPY --from=builder /opt/venv /opt/venv

# Chromium and its system libraries. `--with-deps` runs apt itself, so its lists are cleaned
# afterwards in the same layer. The `chmod` is what lets the non-root app user execute the
# browser that root just installed.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright

# The LaTeX engine.
COPY docker/install-tectonic.sh /usr/local/share/install-tectonic.sh
RUN chmod +x /usr/local/share/install-tectonic.sh \
    && /usr/local/share/install-tectonic.sh "${TECTONIC_VERSION}" "${TECTONIC_SHA256}" \
    && rm /usr/local/share/install-tectonic.sh

WORKDIR /app
COPY --from=builder --chown=${APP_UID}:${APP_GID} /app /app

# Tectonic downloads its support files (the TeX bundle) on first use and caches them under
# XDG_CACHE_HOME. Pointing that at a directory the app user owns is what keeps the download
# from being retried on every single render because the cache write failed.
ENV XDG_CACHE_HOME=/home/applicantos/.cache
RUN mkdir -p /app/var/storage /app/var/cache /app/var/screenshots /app/var/browser \
        "${XDG_CACHE_HOME}" \
    && chown -R "${APP_UID}:${APP_GID}" /app/var "${XDG_CACHE_HOME}"

USER applicantos

# Prime the TeX bundle cache so the first real resume render does not pay for a ~300MB
# download inside a Celery task with a 45-minute time limit. A failure here is reported and
# tolerated: without the cache Tectonic simply fetches at first use, which is the documented
# behaviour, and a transient network fault at build time should not fail the image.
#
# Written as a heredoc rather than a `printf` chain because the document is LaTeX: every
# command in it starts with a backslash, which the Dockerfile parser and `printf` would each
# take a turn at reinterpreting (`\b` is a backspace to printf). A quoted heredoc is passed
# through verbatim.
RUN <<'WARMUP'
set -eu
cat > /tmp/warmup.tex <<'TEX'
\documentclass{article}
\begin{document}warmup\end{document}
TEX
cd /tmp
if tectonic --chatter minimal /tmp/warmup.tex; then
    echo "tectonic: bundle cache primed"
else
    echo "tectonic: WARNING - bundle cache not primed; first render will download it"
fi
rm -f /tmp/warmup.tex /tmp/warmup.pdf
WARMUP

# `app/workers/healthcheck.py` exits 0 when the broker is reachable. `--workers` additionally
# requires a worker to answer a control ping, which is the failure this probe exists for: a
# worker that cannot reach its broker consumes nothing, silently, forever.
#
# Caveat worth knowing: the ping is a broadcast, so with several worker containers on one
# broker another container's reply satisfies this one's probe. It catches a broker outage and
# a fully dead worker tier precisely; a single wedged container it catches only via the
# soft/hard task time limits in `celery_app.py`.
HEALTHCHECK --interval=30s --timeout=15s --start-period=60s --retries=3 \
    CMD ["python", "-m", "app.workers.healthcheck", "--workers", "--timeout", "10"]

# The default command consumes every queue, which is the right shape for a single-container
# install. `docker-compose.yml` overrides it with one `-Q` per container so a slow apply run
# cannot block discovery.
#
# `--concurrency=1` and prefork: `celery_app.py` disposes the inherited SQLAlchemy pool in
# `worker_process_init`, which is correct for prefork and only for prefork. An apply task
# drives a real browser, so concurrency is bounded by memory, not by CPU.
CMD ["celery", "--app", "app.workers.celery_app", "worker", \
     "--queues", "discovery,ai,apply,knowledge,maintenance", \
     "--concurrency", "1", \
     "--loglevel", "INFO"]
