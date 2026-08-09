# Configuration

All 87 settings, what each one does, and when you would change it.

Source of truth: `app/config/settings.py`. **The environment variable name is the UPPER_SNAKE form
of the field name** — `auto_apply_enabled` is `AUTO_APPLY_ENABLED`, always, with no exceptions and
no prefix.

Precedence, highest first:

1. Explicit constructor keyword arguments (tests)
2. Process environment variables
3. The `.env` file in the working directory
4. The defaults below

Nested settings use `__` as the delimiter. Unknown keys are ignored, so an old `.env` never blocks
a start.

---

## Start here

Three commands cover most of what people need.

**The safe default — a fresh install submits nothing:**
```bash
cp .env.example .env
```

**Zero dependencies — no API keys, no Postgres, no Redis, no Docker:**
```bash
SQLITE_MODE=true LLM_PROVIDER=null EMBEDDING_PROVIDER=hashing VECTOR_STORE=memory
```

**Actually apply to things** (both are required; each defaults to the safe position):
```bash
AUTO_APPLY_ENABLED=true
DRY_RUN=false
```

---

## Application

| Env var | Type | Default | What it does | When to change |
|---|---|---|---|---|
| `APP_NAME` | str | `ApplicantOS` | Name in logs, `/health`, and the window title | Forking or white-labelling |
| `ENVIRONMENT` | `local` \| `dev` \| `prod` | `local` | Environment profile; `prod` enables stricter behaviour | Deploying beyond a desktop |
| `DEBUG` | bool | `false` | Verbose errors and Playwright tracing | Debugging a browser run. **Never in prod** — it widens error bodies |
| `SECRET_KEY` | str | `change-me` | Signing key for anything the app signs | Any non-local deployment. Generate with `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `DATA_DIR` | str | `./var` | Root of every piece of local state | Putting state on another volume, or running two installs side by side |

`DATA_DIR` is the parent of the SQLite database, the cache, storage, screenshots and the browser
profile. Directories are created lazily on first access, never at import — so `--help` and test
collection do not litter your working directory.

---

## Persistence

| Env var | Type | Default | What it does | When to change |
|---|---|---|---|---|
| `DATABASE_URL` | str | `postgresql+asyncpg://applicantos:applicantos@localhost:5432/applicantos` | The async connection URL | Pointing at a real database. Must use an **async** driver |
| `SYNC_DATABASE_URL` | str \| null | *derived* | Synchronous URL for Alembic and blocking tooling | Almost never — derived by swapping the async driver (`asyncpg`→`psycopg`, `aiosqlite`→`pysqlite`) |
| `SQLITE_MODE` | bool | `false` | Collapses the stack to zero infrastructure | Local use, CI, or any machine without Postgres |
| `REDIS_URL` | str | `redis://localhost:6379/0` | Cache and Celery broker/backend | A non-default Redis, or a managed one |
| `CELERY_BROKER_URL` | str \| null | *= `REDIS_URL`* | Queue transport | Separating the broker from the cache |
| `CELERY_RESULT_BACKEND` | str \| null | *= `REDIS_URL`* | Task result store | Same |

**What `SQLITE_MODE=true` actually does**, before anything else is derived:

- `DATABASE_URL` → `sqlite+aiosqlite:///<DATA_DIR>/applicantos.db`
- `VECTOR_STORE` → `sqlite_vec`
- `CACHE_BACKEND` → `disk`

It overrides those three whatever you set them to, because neither pgvector nor Redis is part of a
lightweight install. Ordering matters and is handled for you: SQLite mode rewrites `DATABASE_URL`
*before* `SYNC_DATABASE_URL` is derived from it.

---

## Cache

| Env var | Type | Default | What it does | When to change |
|---|---|---|---|---|
| `CACHE_BACKEND` | `memory` \| `disk` \| `redis` | `redis` | Which cache implementation | `disk` for single-machine, `memory` for tests |
| `CACHE_DIR` | str | `./var/cache` | Where the disk cache lives | Cache on faster storage |
| `CACHE_DEFAULT_TTL` | int (s) | `86400` | Default entry lifetime (24h) | Shorter if postings churn fast; longer to cut token spend |
| `CACHE_LLM_RESPONSES` | bool | `true` | Cache model completions at `temperature=0` | **Leave on.** Turning it off multiplies token cost with no behaviour change |
| `CACHE_EMBEDDINGS` | bool | `true` | Cache embedding vectors | Leave on — re-embedding an unchanged chunk is pure waste |

Cache keys are content-addressed SHA-256 of normalised parts. Never `hash()`, which is salted per
process and would invalidate every key on restart.

If `applicantos_cache_events_total{event="hit"}` is near zero, a key is unstable — look for a
timestamp, a `hash()`, or an unordered set in the key parts.

---

## AI models

| Env var | Type | Default | What it does | When to change |
|---|---|---|---|---|
| `LLM_PROVIDER` | `anthropic` \| `openai` \| `local` \| `null` | `anthropic` | Which model plugin resolves | `null` for offline; `local` for Ollama/LM Studio |
| `ANTHROPIC_API_KEY` | str \| null | `null` | Anthropic credential | Using Claude. **Never logged, never returned by `GET /settings`** |
| `OPENAI_API_KEY` | str \| null | `null` | OpenAI credential | Using GPT or OpenAI embeddings |
| `LLM_MODEL_REASONING` | str | `claude-sonnet-4-5` | The model for resume tailoring and cover letters | Trading quality for cost |
| `LLM_MODEL_FAST` | str | `claude-haiku-4-5-20251001` | The model for scoring adjustments and field answers | Same. Short, structured, high volume |
| `LLM_MODEL_LOCAL` | str | `llama3.1:8b` | Model name passed to the local endpoint | Running a different local model |
| `LOCAL_LLM_BASE_URL` | str | `http://localhost:11434/v1` | OpenAI-compatible endpoint | Ollama on another host or port |
| `LLM_MAX_RETRIES` | int | `3` | Retries per model call | Lower on a flaky metered API |
| `LLM_TIMEOUT_SECONDS` | int | `90` | Per-call timeout | Raise for a slow local model on CPU |
| `LLM_DAILY_TOKEN_BUDGET` | int | `2000000` | Hard daily ceiling | Lower to cap spend. On exhaustion the pipeline **degrades**, it does not stop |

**`LLM_PROVIDER=null` is a first-class mode, not a stub for tests.** `NullModel` returns
deterministic, schema-valid JSON, so the entire pipeline runs end to end with no credentials. The
resume it produces is the `fallback_tailor` output: the user's own facts, in impact order, in
their own words. That is a real resume.

Budget exhaustion degrades gracefully everywhere — scoring returns the rule total, tailoring falls
back to the deterministic ranking, field answering escalates to review. No stage fails because a
model was unavailable.

---

## Embeddings and vector store

| Env var | Type | Default | What it does | When to change |
|---|---|---|---|---|
| `EMBEDDING_PROVIDER` | `openai` \| `local` \| `hashing` | `openai` | Which embedder | `hashing` for offline; `local` for a self-hosted model |
| `EMBEDDING_MODEL` | str | `text-embedding-3-small` | Model name | A different model — **but see the warning below** |
| `EMBEDDING_DIM` | int | `1536` | Vector width | Must match the model exactly |
| `VECTOR_STORE` | `pgvector` \| `sqlite_vec` \| `memory` | `pgvector` | Where vectors live | Forced to `sqlite_vec` by SQLite mode; `memory` for tests |
| `KNOWLEDGE_CHUNK_TOKENS` | int | `512` | Target chunk size | Larger for long-form prose; smaller for dense technical docs |
| `KNOWLEDGE_CHUNK_OVERLAP` | int | `64` | Token overlap between chunks | Raise if answers straddle chunk boundaries |

> ⚠️ **Changing `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL` or `EMBEDDING_DIM` invalidates every stored
> vector.** Vectors from two different models are not comparable, and a mismatched `EMBEDDING_DIM`
> either errors on insert or silently produces meaningless similarity. After changing any of the
> three, force a full reindex: `POST /api/v1/knowledge/reindex` with `force=true`.

`hashing` is a real deterministic embedder, not a placeholder. Retrieval quality is lower than a
learned model — it is lexical, so it will not connect "real-time embedded control" to "1 kHz PID
loop" — but the pipeline is fully functional and every test passes on it.

---

## Knowledge engine

| Env var | Type | Default | What it does | When to change |
|---|---|---|---|---|
| `KNOWLEDGE_AUTOINDEX` | bool | `true` | Index new sources automatically | `false` to control indexing manually |
| `KNOWLEDGE_REINDEX_INTERVAL_MINUTES` | int | `60` | How often stale sources refresh | Longer if sources rarely change; shorter while iterating |
| `GITHUB_TOKEN` | str \| null | `null` | GitHub API credential | **Strongly recommended.** Unauthenticated is 60 req/h; a token is 5,000 |
| `GITHUB_INCLUDE_FORKS` | bool | `false` | Analyze forked repositories | You do substantial work in forks |
| `GITHUB_MAX_REPOS` | int | `200` | Repository ceiling per profile | Lower to speed up a first index |
| `WEBSITE_CRAWL_MAX_PAGES` | int | `40` | Page ceiling per site | Raise for a large portfolio |
| `WEBSITE_CRAWL_MAX_DEPTH` | int | `3` | Link depth from the entry point | Raise for a deeply nested site |
| `PROJECT_SCAN_MAX_FILES` | int | `2000` | File ceiling per project folder | Raise for a monorepo |
| `PROJECT_SCAN_MAX_FILE_BYTES` | int | `256000` | Per-file size ceiling | Raise if real source files are being skipped |

Without `GITHUB_TOKEN`, a first index of a profile with more than about 20 repositories will
exhaust the anonymous rate limit and produce a partial graph. It is the single highest-value
optional credential.

The website crawler honours `robots.txt`, stays same-origin, and rate-limits itself to one request
per second. Those are not configurable, deliberately.

---

## Storage

| Env var | Type | Default | What it does | When to change |
|---|---|---|---|---|
| `STORAGE_BACKEND` | `local` \| `s3` | `local` | Where blobs go | `s3` for a server deployment |
| `STORAGE_LOCAL_ROOT` | str | `./var/storage` | Local blob root | Storage on another volume |
| `S3_BUCKET` | str \| null | `null` | Bucket name | Using S3 |
| `S3_ENDPOINT_URL` | str \| null | `null` | Custom endpoint | MinIO, R2, B2, any S3-compatible service |
| `S3_REGION` | str | `us-east-1` | Bucket region | Your actual region |
| `AWS_ACCESS_KEY_ID` | str \| null | `null` | S3 credential | Using S3. **Never logged** |
| `AWS_SECRET_ACCESS_KEY` | str \| null | `null` | S3 credential | Same |

Blobs are resumes, cover letters, uploaded source documents and **confirmation screenshots**. That
last category is your proof you applied — back it up (see [`RUNBOOK.md`](RUNBOOK.md) §6).

---

## Browser

| Env var | Type | Default | What it does | When to change |
|---|---|---|---|---|
| `PLAYWRIGHT_HEADLESS` | bool | `true` | Run Chromium headless | `false` to **watch it fill a form** — the best way to debug an apply |
| `PLAYWRIGHT_SLOW_MO_MS` | int | `0` | Delay between actions | `250`–`500` when watching headed, to follow what it does |
| `PLAYWRIGHT_TIMEOUT_MS` | int | `30000` | Per-operation timeout | Raise on a slow connection; lower if pages hang |
| `BROWSER_USER_DATA_DIR` | str | `./var/browser` | Persistent Chromium profile | Isolating profiles per user |
| `SCREENSHOT_DIR` | str | `./var/screenshots` | Where proof screenshots are written | Screenshots on a backed-up volume |

The debugging combination worth memorising:

```bash
PLAYWRIGHT_HEADLESS=false PLAYWRIGHT_SLOW_MO_MS=400 DRY_RUN=true AUTO_APPLY_ENABLED=false
```

You watch every field fill, and it stops at the button because both switches are closed.

---

## Policy and safety

**This is the section that matters.**

| Env var | Type | Default | What it does | When to change |
|---|---|---|---|---|
| `AUTO_APPLY_ENABLED` | bool | **`false`** | **The master kill switch** | Only when you want it to submit |
| `DRY_RUN` | bool | **`true`** | Fills forms, never clicks submit | Only when you want it to submit |
| `AUTO_APPLY_MIN_SCORE` | int | `70` | Score floor for automatic submission | Raise to be pickier. See the note below |
| `MAX_APPLICATIONS_PER_DAY` | int | `50` | Daily submission cap | Lower — 50 is generous. A cap hit leaves the application `ready` for tomorrow |
| `MAX_APPLICATIONS_PER_SESSION` | int | `200` | Per-run cap | Lower for a bounded test run |
| `MAX_ESSAY_QUESTIONS_BEFORE_REVIEW` | int | `3` | Essays past which it escalates | Lower if you want to write your own essays; raise only if you trust the model with your voice |
| `MIN_ANSWER_CONFIDENCE` | float | `0.75` | Confidence floor per answer | Raise toward `0.9` to escalate more. **Lowering means more guessing** |
| `DELETE_TEMP_RESUME_AFTER_SUBMIT` | bool | `true` | Delete the rendered PDF after submitting | `false` to keep every PDF. `content_json` is retained either way |

### The two switches

```python
is_submission_allowed = auto_apply_enabled and not dry_run
```

Both default to the safe position, and **both** must be deliberately flipped. Every submission
path in the codebase gates on that one property, and `AutoFiller.submit` returns `False` *without
touching the button* when it is `False`.

That is why a fresh install does all the useful work — discovers, scores, tailors, renders, fills —
and stops at rung 5 of the guard ladder with `NEEDS_REVIEW` / `POLICY_BLOCK`. You get to watch it
work before trusting it.

### `AUTO_APPLY_MIN_SCORE` versus `prefs.min_score`

Two different numbers that are easy to confuse:

- **`prefs.min_score`** (per user, default 70) decides the **verdict** — `apply` / `review` /
  `skip` — and therefore what the dashboard shows.
- **`AUTO_APPLY_MIN_SCORE`** (global, default 70) is guard 3 in `Pipeline.submit`, checked again
  at submission time.

They are equal by default. Setting the global one *higher* gives you a second, stricter gate: a
posting can read `apply` on screen and still be refused at submission. An **unscored** posting is
always refused — the gate cannot be satisfied by a number that does not exist.

---

## Documents

| Env var | Type | Default | What it does | When to change |
|---|---|---|---|---|
| `PDF_ENGINE` | `latex` \| `docx` \| `html` | `latex` | Preferred render engine | `html` if you cannot install LaTeX; `docx` if a target ATS parses DOCX better |
| `LATEX_BINARY` | str | `tectonic` | LaTeX executable | `xelatex`, `pdflatex`, or an absolute path |
| `RESUME_MAX_PAGES` | int | `1` | Page budget the shrink loop enforces | `2` for an academic CV. **`1` is the right answer for most jobs** |
| `RESUME_TEMPLATE` | str | `modern` | Default template | `classic`, `ats_plain`, `web`, `markdown` |

If `tectonic` is not installed, rendering falls back down the engine chain to HTML and logs
`documents.render_attempt`. The output is still a valid PDF and still one page — it just looks
different. Check `applicantos_documents_rendered_total{engine="html"}` if you expected LaTeX.

`ats_plain` (DOCX) is the safe choice against an ATS that mangles PDF text extraction.

---

## API and desktop

| Env var | Type | Default | What it does | When to change |
|---|---|---|---|---|
| `API_HOST` | str | `127.0.0.1` | Bind address | **Leave it.** `0.0.0.0` exposes an unauthenticated API to your network |
| `API_PORT` | int | `8000` | Bind port | A port conflict. The Tauri shell picks a free port itself and ignores this |
| `CORS_ORIGINS` | list[str] | both spellings of `:5173` plus the three `tauri://` / `tauri.localhost` webview origins | Allowed origins | A different dev-server port. Note `localhost` and `127.0.0.1` are **different origins** to a browser — list both |

There is no authentication. The API identifies the user by an `X-User-Id` header because there is
exactly one user and it listens on loopback. **Binding it to a routable address would expose every
endpoint, including `PUT /settings`, to anyone on the network.**

---

## Observability

| Env var | Type | Default | What it does | When to change |
|---|---|---|---|---|
| `LOG_LEVEL` | str | `INFO` | Minimum level | `DEBUG` while diagnosing; it is loud |
| `LOG_JSON` | bool | `true` | Structured JSON output | `false` for human-readable console logs in development |
| `METRICS_ENABLED` | bool | `true` | Serve `/metrics` | `false` disables it — the endpoint then returns 404, which a scraper can distinguish from "collecting nothing" |
| `SENTRY_DSN` | str \| null | `null` | Error reporting endpoint | Sending errors off-machine. **Consider what leaves the device first** |

The `redact_secrets` processor is permanently in the structlog chain and cannot be disabled by
configuration. Traceback frame locals stay off, because a frame local is how an API key reaches a
log line despite every other precaution.

---

## Application status sync

Outcomes reach the database by reading the user's mailbox, never by scraping a job board.
**Nothing here takes effect until a mailbox is explicitly connected** — with no `EmailAccount` row,
every setting below is inert.

| Env var | Type | Default | What it does | When to change |
|---|---|---|---|---|
| `STATUS_SYNC_ENABLED` | bool | `true` | Feature switch for the whole subsystem | `false` to disable it entirely |
| `STATUS_SYNC_ON_LAUNCH` | bool | `true` | Sync when the desktop app starts | `false` for a faster cold start |
| `STATUS_SYNC_INTERVAL_MINUTES` | int | `30` | Mailbox poll interval | Longer to be gentler on the provider. **Floored at 5** in the beat schedule |
| `STATUS_SYNC_LOOKBACK_DAYS` | int | `120` | How far back to search | Longer for a long job search; it widens each query |
| `STATUS_SYNC_MIN_CONFIDENCE` | float | `0.80` | Floor for applying a status automatically | Raise toward `0.95` to confirm more by hand |
| `STATUS_SYNC_AUTO_APPLY` | bool | `true` | Apply high-confidence signals without asking | `false` to review every status change |
| `STATUS_SYNC_MAX_MESSAGES_PER_RUN` | int | `500` | Per-run message ceiling | Lower on a very busy mailbox |
| `GHOSTED_AFTER_DAYS` | int | `45` | Silence after which an application is inferred ghosted | Longer if you work with slow-moving employers |

Below `STATUS_SYNC_MIN_CONFIDENCE`, a signal is stored with `needs_review=true` and surfaced for
one-click confirmation. Ambiguous outcomes are never guessed — the same principle as the
application pipeline.

---

## Mailbox providers

**OAuth application identity only.** User tokens and IMAP passwords never appear here and never
reach the database: they live in the OS keychain, addressed by `EmailAccount.credential_ref`.

| Env var | Type | Default | What it does | When to change |
|---|---|---|---|---|
| `GMAIL_CLIENT_ID` | str \| null | `null` | Gmail OAuth app id | Connecting Gmail |
| `GMAIL_CLIENT_SECRET` | str \| null | `null` | Gmail OAuth app secret | Same |
| `OUTLOOK_CLIENT_ID` | str \| null | `null` | Microsoft OAuth app id | Connecting Outlook |
| `OUTLOOK_CLIENT_SECRET` | str \| null | `null` | Microsoft OAuth app secret | Same |
| `IMAP_HOST` | str \| null | `null` | Generic IMAP server | Any other mail provider |
| `IMAP_PORT` | int | `993` | IMAP port | A non-standard server |
| `IMAP_USE_SSL` | bool | `true` | Require SSL | **Never `false`** against a real mailbox |

Scopes requested are read-only (`gmail.readonly`, `Mail.Read`), and IMAP mailboxes open with
`readonly=True`. The code contains no send, delete, move or flag-modifying call — verifiable by
grep, and the reason the recruiter-reply feature on the roadmap drafts rather than sends.

---

## Computed properties

Not settings — derived, and available on the settings object.

| Property | Returns | Notes |
|---|---|---|
| `data_path` | `Path` | `DATA_DIR`, absolute, created on first access |
| `cache_path` | `Path` | `CACHE_DIR`, same |
| `storage_root` | `Path` | `STORAGE_LOCAL_ROOT`, same |
| `screenshot_path` | `Path` | `SCREENSHOT_DIR`, same |
| `browser_profile_path` | `Path` | `BROWSER_USER_DATA_DIR`, same |
| `is_submission_allowed` | `bool` | `auto_apply_enabled and not dry_run` — **the gate every submission path checks** |
| `is_sqlite` | `bool` | Whether the configured database is SQLite |
| `is_postgres` | `bool` | Whether it is PostgreSQL/CockroachDB |
| `is_production` | `bool` | `environment == "prod"` |

Directory creation is lazy on purpose: importing `app.config.settings` must never touch the
filesystem.

---

## Recipes

**Fully offline development**
```bash
SQLITE_MODE=true
LLM_PROVIDER=null
EMBEDDING_PROVIDER=hashing
VECTOR_STORE=memory
LOG_JSON=false
```

**Local models, no cloud calls at all**
```bash
SQLITE_MODE=true
LLM_PROVIDER=local
LOCAL_LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL_LOCAL=llama3.1:8b
EMBEDDING_PROVIDER=local
EMBEDDING_DIM=768          # must match your local embedding model
VECTOR_STORE=sqlite_vec
```

**Watch it fill a real form without submitting**
```bash
PLAYWRIGHT_HEADLESS=false
PLAYWRIGHT_SLOW_MO_MS=400
DRY_RUN=true
AUTO_APPLY_ENABLED=false
LOG_LEVEL=DEBUG
LOG_JSON=false
```

**Cautious live use**
```bash
AUTO_APPLY_ENABLED=true
DRY_RUN=false
AUTO_APPLY_MIN_SCORE=80
MAX_APPLICATIONS_PER_DAY=5
MAX_ESSAY_QUESTIONS_BEFORE_REVIEW=0     # every essay goes to you
MIN_ANSWER_CONFIDENCE=0.9
```

**Minimum token spend**
```bash
CACHE_LLM_RESPONSES=true
CACHE_EMBEDDINGS=true
CACHE_DEFAULT_TTL=604800                 # a week
LLM_DAILY_TOKEN_BUDGET=200000
LLM_MODEL_REASONING=claude-haiku-4-5-20251001
```

---

## Checking what is actually in force

```bash
# The safe projection — no secrets, ever
curl -s -H "X-User-Id: $USER_ID" http://127.0.0.1:8000/api/v1/settings | jq

# Just the switches
curl -s -H "X-User-Id: $USER_ID" http://127.0.0.1:8000/api/v1/settings \
  | jq '{auto_apply_enabled, dry_run, is_submission_allowed, auto_apply_min_score}'

# From the shell, including what is derived
SQLITE_MODE=true python -c "
from app.config.settings import get_settings
s = get_settings()
print('database :', s.database_url)
print('sync     :', s.sync_database_url)
print('vectors  :', s.vector_store)
print('cache    :', s.cache_backend)
print('submit?  :', s.is_submission_allowed)"
```

`GET /api/v1/settings` **never returns a credential.** Not redacted, not masked — absent. Where
the client needs to know whether one is configured, it gets a boolean: `anthropic_configured`,
`openai_configured`, `github_configured`, `aws_configured`, `sentry_configured`. Connection URLs
are reduced to `database_backend`. A masked key still leaks length, prefix and enough tail to
confirm a guess; a boolean cannot leak anything.

---

## See also

- [`CONTRACTS.md`](CONTRACTS.md) §1 — the binding settings specification
- [`RUNBOOK.md`](RUNBOOK.md) — what to change when something is wrong
- [`SAFETY.md`](SAFETY.md) — the safety envelope these settings implement
- `.env.example` — every key with its default
