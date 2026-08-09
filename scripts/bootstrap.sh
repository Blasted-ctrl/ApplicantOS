#!/usr/bin/env bash
#
# One command from a fresh clone of ApplicantOS to a running, seeded backend.
#
#     ./scripts/bootstrap.sh                 # zero infrastructure: SQLite, no keys, no Docker
#     ./scripts/bootstrap.sh --mode postgres # start PostgreSQL + Redis with Docker instead
#     ./scripts/bootstrap.sh --skip-browser  # skip the ~150MB Chromium download
#     ./scripts/bootstrap.sh --skip-seed     # install and migrate, leave the database empty
#
# The POSIX twin of `scripts/bootstrap.ps1`, which is the primary one because this project
# targets Windows as a first-class platform. Both do the same eight steps, and both skip any
# step that has already been done, so re-running is cheap and safe.
#
# Nothing here flips a safety switch. AUTO_APPLY_ENABLED and DRY_RUN keep the values
# .env.example ships, which are the safe ones (golden rule #3).

# `pipefail` matters as much as `-e` here: without it, `pip install ... | tail` reports the
# exit status of `tail`, and a failed install looks like a success.
set -euo pipefail

# ── Constants ───────────────────────────────────────────────────────────────────────────

# The floor from pyproject.toml's `requires-python`.
readonly MINIMUM_PYTHON_MAJOR=3
readonly MINIMUM_PYTHON_MINOR=12

readonly VENV_DIR_NAME=".venv"
readonly HEALTH_TIMEOUT_SECONDS=120
readonly HEALTH_POLL_SECONDS=2

# ── Output ──────────────────────────────────────────────────────────────────────────────

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    readonly CYAN=$'\033[36m'
    readonly GREEN=$'\033[32m'
    readonly YELLOW=$'\033[33m'
    readonly DIM=$'\033[2m'
    readonly RESET=$'\033[0m'
else
    readonly CYAN='' GREEN='' YELLOW='' DIM='' RESET=''
fi

step() { printf '\n%s==> %s%s\n' "$CYAN" "$1" "$RESET"; }
note() { printf '%s    %s%s\n' "$DIM" "$1" "$RESET"; }
ok()   { printf '%s    %s%s\n' "$GREEN" "$1" "$RESET"; }
warn() { printf '%s    %s%s\n' "$YELLOW" "$1" "$RESET"; }
die()  { printf '\nbootstrap: %s\n' "$1" >&2; exit 1; }

# ── Arguments ───────────────────────────────────────────────────────────────────────────

MODE="sqlite"
SKIP_BROWSER=0
SKIP_SEED=0
BOOTSTRAP_PYTHON=""

usage() {
    sed -n '3,15p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --mode)          MODE="${2:-}"; shift 2 ;;
        --mode=*)        MODE="${1#*=}"; shift ;;
        --python)        BOOTSTRAP_PYTHON="${2:-}"; shift 2 ;;
        --python=*)      BOOTSTRAP_PYTHON="${1#*=}"; shift ;;
        --skip-browser)  SKIP_BROWSER=1; shift ;;
        --skip-seed)     SKIP_SEED=1; shift ;;
        -h|--help)       usage; exit 0 ;;
        *)               die "unknown option: $1 (try --help)" ;;
    esac
done

case "$MODE" in
    sqlite|postgres) ;;
    *) die "--mode must be 'sqlite' or 'postgres', got '$MODE'" ;;
esac

# ── Locate the repository ───────────────────────────────────────────────────────────────

# From the script's own path, not the working directory, so it can be run from anywhere.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname -- "$SCRIPT_DIR")"

[ -f "$REPO_ROOT/pyproject.toml" ] \
    || die "cannot find pyproject.toml above $SCRIPT_DIR - is this a full clone?"

cd "$REPO_ROOT"

printf '\nApplicantOS bootstrap\n'
note "repository : $REPO_ROOT"
note "mode       : $MODE"

# ── 1. Interpreter ──────────────────────────────────────────────────────────────────────

step "Checking Python"

# Each candidate is **executed**, not merely resolved with `command -v`. Two reasons:
#
#   * a bare `python3` on an older distribution is routinely 3.9, and the failure that
#     produces — a syntax error deep inside a model module — does not name the version as
#     the cause;
#   * on Windows, `%LOCALAPPDATA%\Microsoft\WindowsApps\python3` is a Microsoft Store
#     execution alias that exists and resolves, then prints "Python was not found" and exits
#     non-zero. It passes a `command -v` test and fails everything after it.
#
# Versioned names come first so an explicitly installed 3.12/3.13 wins over whatever `python`
# happens to be.
version_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= ('"$MINIMUM_PYTHON_MAJOR"','"$MINIMUM_PYTHON_MINOR"') else 1)' \
        >/dev/null 2>&1
}

resolve_python() {
    if [ -n "$BOOTSTRAP_PYTHON" ]; then
        printf '%s' "$BOOTSTRAP_PYTHON"
        return
    fi
    for candidate in python3.13 python3.12 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && version_ok "$candidate"; then
            printf '%s' "$candidate"
            return
        fi
    done
    die "no Python ${MINIMUM_PYTHON_MAJOR}.${MINIMUM_PYTHON_MINOR}+ found on PATH. Install one, or pass --python /path/to/python3.12."
}

PYTHON="$(resolve_python)"
note "interpreter: $PYTHON"

# Re-checked rather than assumed, because --python bypasses resolve_python's own check.
PY_VERSION="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" \
    || die "'$PYTHON' is not a working Python interpreter."

if ! version_ok "$PYTHON"; then
    die "Python ${MINIMUM_PYTHON_MAJOR}.${MINIMUM_PYTHON_MINOR}+ is required; '$PYTHON' is $PY_VERSION."
fi
ok "Python $PY_VERSION"

# ── 2. Virtualenv ───────────────────────────────────────────────────────────────────────

VENV_PATH="$REPO_ROOT/$VENV_DIR_NAME"

# The interpreter lives in `bin/` on POSIX and `Scripts/` on Windows, and **both have to be
# probed even from a POSIX shell**: Git Bash and MSYS2 are supported ways to work on this
# repository, and a virtualenv created there by the native Windows interpreter has the
# `Scripts/` layout. Assuming `bin/` would report "no virtualenv", run `venv` on top of a
# perfectly good one, and leave a hybrid with two half-populated layouts and a `pyvenv.cfg`
# pointing at an interpreter that owns neither. (That is not hypothetical; it is why this
# function exists.)
resolve_venv_python() {
    if [ -x "$VENV_PATH/bin/python" ]; then
        printf '%s' "$VENV_PATH/bin/python"
    elif [ -x "$VENV_PATH/Scripts/python.exe" ]; then
        printf '%s' "$VENV_PATH/Scripts/python.exe"
    fi
}

VENV_PYTHON="$(resolve_venv_python)"

if [ -n "$VENV_PYTHON" ]; then
    step "Virtualenv already exists ($VENV_DIR_NAME)"
else
    step "Creating virtualenv ($VENV_DIR_NAME)"
    "$PYTHON" -m venv "$VENV_PATH" \
        || die "python -m venv failed. On Debian/Ubuntu: apt install python3-venv"
    VENV_PYTHON="$(resolve_venv_python)"
    [ -n "$VENV_PYTHON" ] || die "python -m venv reported success but produced no interpreter in $VENV_PATH"
    ok "created"
fi

# Every command below names the interpreter explicitly rather than activating. Activation
# mutates the caller's shell, which is wrong for a script that may be sourced or run from a
# task runner.

# ── 3. Dependencies ─────────────────────────────────────────────────────────────────────

step "Installing dependencies"

"$VENV_PYTHON" -m pip install --upgrade --quiet pip setuptools wheel

note "serving stack (docker/requirements-runtime.txt)"
"$VENV_PYTHON" -m pip install --quiet --requirement "$REPO_ROOT/docker/requirements-runtime.txt"

# The database driver is chosen at install time, not run time: app/database/session.py builds
# the engine while it is being imported, so the driver named by DATABASE_URL must already be
# installed before anything under app.database can be imported at all.
if [ "$MODE" = "postgres" ]; then
    EXTRAS=".[postgres,redis,orjson,pgvector]"
else
    EXTRAS=".[sqlite,orjson]"
fi
note "project, editable: $EXTRAS"
"$VENV_PYTHON" -m pip install --quiet --editable "$EXTRAS"

note "dev toolchain (ruff, mypy, pytest)"
"$VENV_PYTHON" -m pip install --quiet ruff mypy pytest pytest-asyncio pytest-cov

note "browser automation and document rendering"
"$VENV_PYTHON" -m pip install --quiet playwright pypdf python-docx reportlab selectolax

ok "dependencies installed"

# ── 4. Browser ──────────────────────────────────────────────────────────────────────────

if [ "$SKIP_BROWSER" -eq 1 ]; then
    step "Skipping the Playwright browser (--skip-browser)"
    warn "app/browser/ will not run until you install it: $VENV_DIR_NAME/bin/playwright install chromium"
else
    step "Installing the Playwright Chromium browser (~150MB, cached after the first run)"
    # `--with-deps` needs root and is only meaningful on Linux; it is offered rather than
    # forced, because a bootstrap script should not sudo behind the user's back.
    if ! "$VENV_PYTHON" -m playwright install chromium; then
        warn "chromium install failed."
        warn "On Linux the shared libraries may be missing. Install them with:"
        warn "  sudo $VENV_PYTHON -m playwright install-deps chromium"
        die "playwright install chromium failed"
    fi
    ok "chromium ready"
fi

# ── 5. Environment file ─────────────────────────────────────────────────────────────────

if [ -f "$REPO_ROOT/.env" ]; then
    step ".env already exists - leaving it alone"
else
    step "Creating .env from .env.example"
    cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"
    ok "created (AUTO_APPLY_ENABLED=false, DRY_RUN=true - the safe posture)"
fi

# ── 6. Infrastructure ───────────────────────────────────────────────────────────────────

# Applied to every backend command below. In sqlite mode this is the whole zero-dependency
# posture; in postgres mode it is empty and .env decides.
runtime_env() {
    if [ "$MODE" = "sqlite" ]; then
        env SQLITE_MODE=true LLM_PROVIDER=null EMBEDDING_PROVIDER=hashing VECTOR_STORE=memory "$@"
    else
        "$@"
    fi
}

if [ "$MODE" = "postgres" ]; then
    step "Starting PostgreSQL and Redis"

    command -v docker >/dev/null 2>&1 \
        || die "docker was not found on PATH. Install Docker, or re-run without --mode postgres to use the zero-infrastructure SQLite install."

    docker compose up -d postgres redis

    note "waiting for both to report healthy"
    deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECONDS ))
    while :; do
        healthy=$(docker compose ps --format json 2>/dev/null | grep -c '"Health":"healthy"' || true)
        [ "$healthy" -ge 2 ] && break
        if [ "$(date +%s)" -gt "$deadline" ]; then
            die "postgres and redis did not become healthy within ${HEALTH_TIMEOUT_SECONDS}s. Check \`docker compose logs postgres redis\`."
        fi
        sleep "$HEALTH_POLL_SECONDS"
    done
    ok "postgres and redis are healthy"
else
    step "Zero-infrastructure mode - no PostgreSQL, no Redis, no API keys"
    note "SQLITE_MODE=true  LLM_PROVIDER=null  EMBEDDING_PROVIDER=hashing  VECTOR_STORE=memory"
fi

# ── 7. Migrations ───────────────────────────────────────────────────────────────────────

step "Applying migrations"
runtime_env "$VENV_PYTHON" -m alembic upgrade head
ok "schema is at head"

# ── 8. Seed ─────────────────────────────────────────────────────────────────────────────

if [ "$SKIP_SEED" -eq 1 ]; then
    step "Skipping the seed (--skip-seed)"
else
    step "Seeding a user, a profile and a knowledge graph"
    runtime_env "$VENV_PYTHON" -m scripts.seed
fi

# ── Done ────────────────────────────────────────────────────────────────────────────────

if [ "$MODE" = "sqlite" ]; then
    PREFIX="SQLITE_MODE=true LLM_PROVIDER=null EMBEDDING_PROVIDER=hashing VECTOR_STORE=memory "
else
    PREFIX=""
fi

# The resolved interpreter, relative to the repository root, so the printed commands can be
# pasted as-is on either layout.
RELATIVE_PYTHON="${VENV_PYTHON#"$REPO_ROOT/"}"

printf '\n%sReady.%s\n\n' "$GREEN" "$RESET"
printf '  Serve the API:\n'
printf '    %s%s -m uvicorn app.main:app --reload\n\n' "$PREFIX" "$RELATIVE_PYTHON"
printf '  Prove the whole surface answers:\n'
printf '    %s%s -m scripts.smoke_test --start\n\n' "$PREFIX" "$RELATIVE_PYTHON"
printf '  Run the desktop app:\n'
printf '    cd desktop && npm install && npm run dev\n\n'
printf '%s  Safety posture: AUTO_APPLY_ENABLED=false, DRY_RUN=true.%s\n' "$DIM" "$RESET"
printf '%s  Nothing is submitted anywhere until you flip both, deliberately, in .env.%s\n\n' "$DIM" "$RESET"
