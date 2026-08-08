# ApplicantOS — Agent Brief

An AI desktop application that automates the repetitive parts of job hunting. It continuously
discovers postings from supported ATS platforms, scores them against the user's preferences,
generates a tailored resume and cover letter, applies automatically where it safely can, escalates
to manual review where it cannot, and tracks every application with proof of submission.

The architectural centre is a **Personal Knowledge Engine**. Instead of storing one resume, the
system stores *knowledge* — facts, entities, edges and embeddings — continuously indexed from
GitHub, portfolio sites, project folders, resumes and LinkedIn exports. A resume is a **generated
view** over that graph, which is why every bullet traces back to a `KnowledgeFact.id` and nothing
is ever fabricated.

> **Binding specs — read before changing anything:**
> [`docs/CONTRACTS.md`](docs/CONTRACTS.md) (all cross-module boundaries),
> [`docs/UI.md`](docs/UI.md) (all desktop visual/interaction design),
> [`docs/WORKING_AGREEMENT.md`](docs/WORKING_AGREEMENT.md) (how work gets done here).

---

## The ten golden rules

These are invariants, not guidelines. Every one is enforced in code and checked by the
`safety-reviewer` agent.

1. **Never apply twice** — `UNIQUE(user_id, posting_id)` on `applications` *and* a status guard in `Pipeline.submit`.
2. **Never guess** — low answer confidence, essay overflow, captcha, MFA, or an unknown required field ⇒ `NEEDS_REVIEW`.
3. **Kill switch** — submission requires `auto_apply_enabled=True` **and** `dry_run=False`. Both default to the safe position.
4. **No secrets in logs** — the `redact_secrets` processor is always in the structlog chain, and traceback frame locals stay off.
5. **Plugin isolation** — never import a concrete provider / analyzer / model / template module outside its own package. Go through `app.plugins.registry`.
6. **Knowledge is the source of truth** — resumes are generated views. `ResumeVersion.content_json` is kept forever; the rendered PDF is disposable.
7. **Nothing is fabricated** — every resume bullet traces to a `KnowledgeFact.id`.
8. **Everything is resumable** — long operations checkpoint; a crash resumes, never restarts.
9. **Cache aggressively, invalidate precisely** — content-addressed keys, never cache a mutation.
10. **ToS honesty** — providers forbidding automation set `supports_auto_apply=False` and route to manual review, documented in the module docstring.

---

## Architecture map

| Path | Role |
|---|---|
| `app/config/` | Settings (pydantic-settings), structlog setup + secret redaction, default scoring rules |
| `app/database/` | Async engine, session scope, portable column types (`GUID`, `JSONType`, `EmbeddingType`) |
| `app/models/` | 22 SQLAlchemy 2.0 tables + every shared enum |
| `app/schemas/` | Pydantic v2 request/response models — the API's public shape |
| `app/cache/` | `Cache` protocol; memory / disk / redis backends + tiered read-through, `@cached` |
| `app/plugins/` | One registry for all five plugin kinds: provider, model, template, parser, analyzer |
| `app/knowledge/` | **The knowledge engine** — analyzers, extractors, vector store, graph, facts, memory, indexer, retrieval |
| `app/jobs/` | ATS provider plugins + dedupe |
| `app/ai/` | Model plugins, embeddings, prompts, scoring, resume engine, cover letters, field answering |
| `app/documents/` | Render model + LaTeX / DOCX / HTML template plugins, one-page enforcement |
| `app/browser/` | Playwright session, field discovery, autofill, submission verification, artifacts |
| `app/storage/` | `StorageBackend` protocol — local filesystem or S3 |
| `app/services/` | Orchestration: pipeline, sessions, checkpoints, reviews, onboarding, analytics |
| `app/api/` | FastAPI routes + the WebSocket event bus |
| `app/workers/` | Celery tasks across the `discovery` / `ai` / `apply` / `knowledge` / `maintenance` queues |
| `app/observability/` | Prometheus collectors + request middleware |
| `desktop/` | Tauri shell (Rust) + React/Vite renderer |

---

## The pipeline

```
Scheduler → Discover (provider plugins) → Deduplicate → Score
   → Retrieve knowledge → Tailor resume → Write cover letter → Render PDF
   → Browser: open, fill, upload, answer → Verify → Submit → Screenshot
   → Store metadata → Delete temp resume
```

Any step may divert to `NEEDS_REVIEW` instead of guessing. Every step checkpoints, so a crash
resumes rather than restarts. See [`docs/PIPELINE.md`](docs/PIPELINE.md).

---

## Running it

```bash
cp .env.example .env            # AUTO_APPLY_ENABLED=false, DRY_RUN=true are the safe defaults
docker compose up -d postgres redis
alembic upgrade head
python -m scripts.seed

uvicorn app.main:app --reload                       # API
celery -A app.workers.celery_app worker -Q discovery,ai,apply,knowledge,maintenance
celery -A app.workers.celery_app beat                # scheduler
cd desktop && npm install && npm run dev             # desktop app
```

**Zero-dependency mode** — the whole pipeline runs with no API keys and no Postgres:

```bash
SQLITE_MODE=true LLM_PROVIDER=null EMBEDDING_PROVIDER=hashing VECTOR_STORE=memory
```

Quality gates: `ruff check .` · `mypy app` · `pytest`

---

## Conventions

- Async-first. SQLAlchemy 2.0 (`Mapped[]` / `mapped_column`). Pydantic v2.
- `from __future__ import annotations` at the top of every module; full type annotations.
- `logger = structlog.get_logger(__name__)`. Never a bare `except:`.
- `pathlib` always — this project runs on Windows as a first-class target.
- **All third-party imports are lazy** (inside the function that uses them) so the app imports
  without optional dependencies.
- Never `hash()` for anything persisted or cached — it is salted per process. Use `hashlib`.
- Named constants, not magic numbers. Small modules. No duplicated logic.

---

## Where to make each kind of change

| You want to… | Go here | Also do this |
|---|---|---|
| Add an ATS provider | New module in `app/jobs/`, `@plugin`-registered | Register the entry point in `pyproject.toml`; add a `SelectorPack`; state its ToS posture in the docstring |
| Change job scoring | `app/config/scoring_rules.yaml` | `score_rules()` must stay pure and deterministic |
| Add a knowledge source | New analyzer in `app/knowledge/analyzers/` | Implement `fingerprint()` cheaply — it is what makes re-indexing free |
| Change resume layout | `app/documents/templates/` + a `TemplatePlugin` | One-page enforcement lives in `render_resume` |
| Change what goes on a resume | `app/ai/resume_engine.py` | Fact-id validation must stay intact |
| Add an API endpoint | `app/api/routes/` + a schema in `app/schemas/` | Mirror the type in `desktop/src/lib/api/types.ts` |
| Add a background task | `app/workers/` | Route it to a queue; make it idempotent; never retry `NEEDS_REVIEW` |
| Change the database | `app/models/` then `alembic revision --autogenerate` | Verify the migration reproduces `Base.metadata` exactly |
| Change the UI | `desktop/src/` | [`docs/UI.md`](docs/UI.md) is binding — including the instant-feel contract |

---

## The safety envelope

Two independent switches gate every real submission, and both default closed. Beyond them:

- **Manual review is the failure mode, not a wrong answer.** Every `ReviewReason` exists because
  guessing there would be worse than asking.
- **LinkedIn and Workday are discovery-only.** LinkedIn's ToS prohibits automated scraping and
  submission, so discovery is limited to a user-supplied export or a public feed, and `apply()`
  raises `UnsupportedFlowError`. Workday's account-gated multi-step flow routes to review by
  design. Greenhouse, Lever and Ashby support real automated submission.
- **EEO/demographic questions** are never inferred and always answerable as
  "decline to self-identify".
- **The user's data stays local** by default — local filesystem storage, local vector store,
  optional local LLM.

---

## Before you finish

- [ ] `python -m compileall app` is clean and the affected tests pass
- [ ] `ruff check .` and `mypy app` pass
- [ ] No `TODO`, `FIXME`, or stub bodies introduced
- [ ] New third-party imports are lazy
- [ ] The zero-API-key path still works (`LLM_PROVIDER=null EMBEDDING_PROVIDER=hashing`)
- [ ] Schema changes have a migration that round-trips (`upgrade` then `downgrade`)
- [ ] Enum changes are mirrored in `desktop/src/lib/api/types.ts`
- [ ] None of the ten golden rules got weaker
