# ApplicantOS — the commands you actually run.
#
#     make            # the target list
#     make dev        # a fresh clone to a seeded, runnable backend, with no infrastructure
#     make api        # serve it
#
# ── Shell ─────────────────────────────────────────────────────────────────────────────────
# Recipes are POSIX sh. On Windows that means running make from Git Bash, MSYS2 or WSL —
# GNU make finds `sh` on PATH there and uses it. If you would rather not have a POSIX shell
# at all, `scripts/bootstrap.ps1` is the pure-PowerShell equivalent of `make dev` and
# `docs/PACKAGING.md` covers the desktop build; nothing here is load-bearing for either.
#
# ── Configuring a run ─────────────────────────────────────────────────────────────────────
# Put settings in `.env`, not in front of `make`. `pydantic-settings` reads `.env` directly,
# so it works identically on every platform — whereas `LOG_LEVEL=DEBUG make api` silently
# does nothing under MSYS2 on Windows, where GNU make does not forward its inherited
# environment to recipes (verified: a variable exported into `make` reads as unset inside the
# recipe shell). The `MODE`, `VENV`, `PORT` and `HOST` knobs below are make variables and are
# passed the make way: `make api PORT=9000`.
SHELL := /bin/sh
.SHELLFLAGS := -eu -c

.DEFAULT_GOAL := help

# ── Interpreter ───────────────────────────────────────────────────────────────────────────
# Every Python target runs the project virtualenv's interpreter when one exists, and falls
# back to whatever is on PATH when it does not — so `make install` works before there is a
# venv, and every target after it works without anyone having to activate one. Activation is
# a shell mutation; naming the interpreter is not, which is what makes these targets safe to
# run from an editor, a CI step or a fresh terminal.
VENV ?= .venv

# The layout is **probed by the shell inside each recipe**, not decided by make at parse
# time. Two reasons, both of which broke this file before they were understood:
#
#   * `$(OS)` is not reliable. Windows exports OS=Windows_NT, but GNU make under MSYS2 does
#     not surface it, so an `ifeq ($(OS),Windows_NT)` branch silently selected the POSIX
#     `bin/` layout on Windows and every target died with "No such file or directory".
#   * `make dev` creates the virtualenv in its first prerequisite and has to use it in the
#     second. A `:=` assignment is expanded before any prerequisite has run, so it would
#     resolve to the system interpreter and install into it.
#
# Order: POSIX venv, Windows venv, then the same validated PATH probe `install` bootstraps
# with — declared once, below, so the two cannot disagree about what "python" means.
PYTHON = $$(if [ -x "$(VENV)/bin/python" ]; then echo "$(VENV)/bin/python"; \
	elif [ -x "$(VENV)/Scripts/python.exe" ]; then echo "$(VENV)/Scripts/python.exe"; \
	else printf '%s' "$(BOOTSTRAP_PYTHON)"; fi)

# The interpreter that *creates* the venv. Candidates are tried in order and each one is
# **executed**, not merely resolved on PATH. `command -v python3` is not good enough on
# Windows: `%LOCALAPPDATA%\Microsoft\WindowsApps\python3` is a Microsoft Store execution
# alias that exists, resolves, and then prints "Python was not found" and exits non-zero. It
# passes a `command -v` test and fails everything after it.
#
# Running the candidate also enforces the `requires-python = ">=3.12"` floor from
# pyproject.toml here, where the message can name the cause, rather than three steps later as
# a syntax error inside a model module. Override for a specific version:
# `make install BOOTSTRAP_PYTHON=python3.12`.
BOOTSTRAP_PYTHON ?= $$(for c in python3 python py python3.13 python3.12; do \
	if "$$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,12) else 1)' \
		>/dev/null 2>&1; then echo "$$c"; exit 0; fi; \
	done; echo "no-python-3.12-or-newer-found")

# ── Run mode ──────────────────────────────────────────────────────────────────────────────
# MODE=sqlite (the default) is the zero-infrastructure posture from docs/WORKING_AGREEMENT.md
# §6: no PostgreSQL, no Redis, no API keys, and the whole pipeline still runs end to end.
# MODE=postgres sets nothing and lets `.env` and `app/config/settings.py` decide, which is
# what you want when `make up` is running the compose stack.
MODE ?= sqlite

ifeq ($(MODE),sqlite)
  RUNTIME_ENV := SQLITE_MODE=true LLM_PROVIDER=null EMBEDDING_PROVIDER=hashing VECTOR_STORE=memory
else
  RUNTIME_ENV :=
endif

# ── Compose ───────────────────────────────────────────────────────────────────────────────
COMPOSE ?= docker compose

# ── Ports and hosts ───────────────────────────────────────────────────────────────────────
HOST ?= 127.0.0.1
PORT ?= 8000

.PHONY: help install dev api worker beat desktop up down logs migrate revision seed \
        test smoke lint fmt typecheck clean


# ==========================================================================================
# Help
# ==========================================================================================

help: ## Show this list
	@printf '\nApplicantOS — make targets\n\n'
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@printf '\nVariables:\n'
	@printf '  \033[36mMODE\033[0m         sqlite (default, zero infrastructure) | postgres\n'
	@printf '  \033[36mVENV\033[0m         virtualenv directory (default: .venv)\n'
	@printf '  \033[36mPORT\033[0m         API port (default: 8000)\n'
	@printf '\nCurrent: MODE=$(MODE)  PYTHON=%s\n\n' "$(PYTHON)"


# ==========================================================================================
# Setup
# ==========================================================================================

install: ## Create the virtualenv and install the backend plus the dev toolchain
	@echo "==> creating $(VENV)"
	@test -x "$(VENV)/bin/python" -o -x "$(VENV)/Scripts/python.exe" \
		|| "$(BOOTSTRAP_PYTHON)" -m venv "$(VENV)"
	@echo "==> upgrading installer"
	@"$(PYTHON)" -m pip install --upgrade --quiet pip setuptools wheel
	@echo "==> serving stack (docker/requirements-runtime.txt - one declaration, two consumers)"
	@"$(PYTHON)" -m pip install --quiet --requirement docker/requirements-runtime.txt
	@echo "==> project, editable, with the SQLite and accelerator extras"
	@"$(PYTHON)" -m pip install --quiet --editable ".[sqlite,orjson]"
	@echo "==> dev toolchain"
	@"$(PYTHON)" -m pip install --quiet ruff mypy pytest pytest-asyncio pytest-cov
	@echo "==> done. next: make dev"

dev: install migrate seed ## Fresh clone -> installed, migrated, seeded (MODE=sqlite)
	@printf '\n\033[32mReady.\033[0m The knowledge graph is seeded and a resume can be generated now.\n\n'
	@printf '  make api        serve on http://$(HOST):$(PORT)\n'
	@printf '  make smoke      prove the whole surface answers\n'
	@printf '  make desktop    run the Tauri shell against it\n\n'


# ==========================================================================================
# Processes
# ==========================================================================================

api: ## Serve the API with autoreload
	@echo "==> uvicorn on http://$(HOST):$(PORT)  (MODE=$(MODE))"
	@$(RUNTIME_ENV) "$(PYTHON)" -m uvicorn app.main:app --host $(HOST) --port $(PORT) \
		--reload --reload-dir app

worker: ## Run a Celery worker across all five queues
	@echo "==> celery worker: discovery,ai,apply,knowledge,maintenance"
	@$(RUNTIME_ENV) "$(PYTHON)" -m celery --app app.workers.celery_app worker \
		--queues discovery,ai,apply,knowledge,maintenance --concurrency 1 --loglevel INFO

beat: ## Run the Celery beat scheduler (exactly one, ever)
	@echo "==> celery beat"
	@$(RUNTIME_ENV) "$(PYTHON)" -m celery --app app.workers.celery_app beat \
		--schedule var/celerybeat-schedule --loglevel INFO

desktop: ## Install renderer dependencies and start the desktop dev loop
	@echo "==> npm install && npm run dev  (starts the backend sidecar too)"
	@cd desktop && npm install && npm run dev


# ==========================================================================================
# Compose
# ==========================================================================================

up: ## Build and start the full stack (postgres, redis, api, workers, beat, prometheus, grafana)
	@$(COMPOSE) up -d --build
	@printf '\n  api         http://localhost:8000/health\n'
	@printf '  metrics     http://localhost:8000/metrics\n'
	@printf '  prometheus  http://localhost:9090\n'
	@printf '  grafana     http://localhost:3000  (admin/admin)\n\n'

down: ## Stop the stack, keeping volumes
	@$(COMPOSE) down

logs: ## Follow logs from every service
	@$(COMPOSE) logs --follow --tail=100


# ==========================================================================================
# Database
# ==========================================================================================

migrate: ## Apply migrations up to head
	@echo "==> alembic upgrade head  (MODE=$(MODE))"
	@$(RUNTIME_ENV) "$(PYTHON)" -m alembic upgrade head

revision: ## Autogenerate a migration: make revision m="what changed"
	@test -n "$(m)" || { \
		echo "revision needs a message: make revision m=\"add tracking signals\"" >&2; \
		exit 2; \
	}
	@$(RUNTIME_ENV) "$(PYTHON)" -m alembic revision --autogenerate -m "$(m)"
	@echo "==> verify the migration round-trips before committing it:"
	@echo "    make migrate && $(PYTHON) -m alembic downgrade -1 && make migrate"

seed: ## Seed a user, preferences, a profile and a realistic knowledge graph (idempotent)
	@$(RUNTIME_ENV) "$(PYTHON)" -m scripts.seed


# ==========================================================================================
# Quality gates
# ==========================================================================================

test: ## Run the test suite
	@"$(PYTHON)" -m pytest

smoke: ## Start a backend, exercise every route and flow, stop it
	@$(RUNTIME_ENV) "$(PYTHON)" -m scripts.smoke_test --start --port $(PORT)

lint: ## ruff check .
	@"$(PYTHON)" -m ruff check .

fmt: ## Format and apply safe autofixes
	@"$(PYTHON)" -m ruff format .
	@"$(PYTHON)" -m ruff check --fix .

typecheck: ## mypy app
	@"$(PYTHON)" -m mypy app


# ==========================================================================================
# Housekeeping
# ==========================================================================================

clean: ## Remove caches and build artefacts (never var/ — that is your data)
	@echo "==> removing caches and build artefacts"
	@rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage coverage.xml htmlcov \
		build dist *.egg-info
	@find . -type d -name __pycache__ -not -path "./$(VENV)/*" -not -path "./desktop/node_modules/*" \
		-prune -exec rm -rf {} + 2>/dev/null || true
	@echo "==> kept: $(VENV)/ and var/ (the database, generated documents and screenshots)"
