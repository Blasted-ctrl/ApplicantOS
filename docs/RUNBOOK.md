# Runbook

Operating ApplicantOS: what to check, what "bad" looks like, and what to do about it.

Written for the person running this — which, for a desktop install, is the person using it. Every
command assumes the repository root and a configured `.env`.

---

## 0. Bringing the stack up

```bash
cp .env.example .env
docker compose -f docker-compose.yml up -d          # production shape
docker compose up -d                                # dev shape: adds the override
```

**Use `-f docker-compose.yml` explicitly when you want the production shape.** Compose applies
`docker-compose.override.yml` automatically whenever it exists, and that file deliberately runs
the API with `--reload` and **disables its healthcheck** — a reloading process flaps unhealthy on
every save and would drag anything with a `depends_on: service_healthy` down with it. Both files
say so inline. If `docker compose ps` shows the API with no health status, this is why, and it is
correct.

### Port conflicts

Every published port is parameterised, because 5432 and 6379 are the first ports any other local
stack takes. If `up` fails with *"Bind for 0.0.0.0:6379 failed: port is already allocated"*, shift
the host side in `.env` — the containers keep talking to each other on the standard ports inside
the compose network, so only the host mapping moves:

```bash
POSTGRES_PORT=5442
REDIS_PORT=6389
API_PORT=8010
PROMETHEUS_PORT=9091
GRAFANA_PORT=3001
```

Find the culprit with `docker ps --format '{{.Names}}\t{{.Ports}}'` before assuming it is another
copy of this stack.

### Confirming the database came up correctly

Two things are worth checking once, because they are silent when wrong — the extension and the
column type. `EmbeddingType` stores JSON on SQLite and a native `vector` on PostgreSQL, so a
missing extension does not fail loudly; it just degrades every similarity search:

```bash
docker compose exec postgres psql -U applicantos -d applicantos -tAc \
  "select extname||' '||extversion from pg_extension order by 1;"
# expect: plpgsql 1.0 / vector 0.8.x

docker compose exec postgres psql -U applicantos -d applicantos -tAc \
  "select table_name||'.'||column_name||' -> '||udt_name from information_schema.columns
   where column_name='embedding' order by 1;"
# expect four rows, each ending in "-> vector" (not "-> json")
```

---

## 1. Is it healthy?

Three endpoints, and they answer different questions on purpose.

### `GET /health` — liveness

```bash
curl -s http://127.0.0.1:8000/health | jq
```
```json
{"app": "ApplicantOS", "version": "0.1.0", "environment": "local", "status": "ok"}
```

**Touches no dependency.** This must keep answering while the database is down, or a supervisor
restarts the process during an outage that a restart cannot fix. If this fails, the process is
gone.

### `GET /ready` — readiness

```bash
curl -s -o /tmp/ready.json -w '%{http_code}\n' http://127.0.0.1:8000/ready && jq . /tmp/ready.json
```
```json
{
  "ready": true,
  "checks": {
    "database": {"ok": true, "required": true,  "skipped": false, "detail": null},
    "redis":    {"ok": true, "required": false, "skipped": true,
                 "detail": "Not in use: cache_backend is 'disk'."}
  }
}
```

Returns **503** when a required dependency is down — **and still returns the body**, because
"which one" is the useful part. Redis is only `required` when `cache_backend == "redis"`; in
SQLite mode it is reported as skipped, not as failing.

### `GET /metrics` — Prometheus

```bash
curl -s http://127.0.0.1:8000/metrics | head -40
```

Returns **404** when `metrics_enabled=false`. That is deliberate: a 404 says "not collecting",
which a scraper can distinguish from "collecting nothing".

### Workers

```bash
celery -A app.workers.celery_app inspect ping         # are workers alive?
celery -A app.workers.celery_app inspect active       # what is running right now
celery -A app.workers.celery_app inspect reserved     # prefetched but not started
celery -A app.workers.celery_app inspect scheduled    # countdown/ETA tasks
celery -A app.workers.celery_app inspect stats | jq '.[].pool'
```

Queue depth, straight from Redis:

```bash
for q in discovery ai apply knowledge maintenance; do
  echo "$q: $(redis-cli llen "$q")"
done
```

---

## 2. Metrics, and what bad looks like

Fifteen collectors. For each: what it means, and the shape that should worry you.

| Metric | Labels | Healthy | **Bad, and what it means** |
|---|---|---|---|
| `applicantos_postings_discovered_total` | `provider` | Rises each poll | **Flat for >2 polls** → provider changed its feed shape, or seed boards 404 |
| `applicantos_postings_deduped_total` | `provider` | 10–40% of discovered | **≈100%** → `dedupe_key` too loose, real jobs being merged. **≈0%** → too tight, duplicates leaking through |
| `applicantos_scores_total` | `verdict` | Mixed apply/review/skip | **All `skip`** → preference gates too aggressive or an empty rule pack. **All `apply`** → `min_score` too low |
| `applicantos_applications_total` | `status`, `provider` | `submitted` growing | **`needs_review` growing while `submitted` is flat** → normal on a fresh install (kill switch); otherwise a selector pack has rotted |
| `applicantos_apply_duration_seconds` | `provider` | p50 30–90s | **p99 > 600s** → pages hanging; check `playwright_timeout_ms`. **Bimodal** → one provider's flow changed |
| `applicantos_llm_tokens_total` | `model`, `kind` | Proportional to postings | **Spiking with no new postings** → a cache key is unstable; check `cache_llm_responses` and temperature |
| `applicantos_llm_requests_total` | `model`, `outcome` | `outcome="ok"` dominant | **`error` climbing** → key expired, rate limit, or daily budget exhausted |
| `applicantos_cache_events_total` | `namespace`, `event` | hit ≫ miss on `llm`/`embed` | **hit ≈ 0** → keys are unstable. Look for `hash()`, a timestamp, or a mutable dict in a key |
| `applicantos_documents_rendered_total` | `engine`, `outcome` | `outcome="ok"` | **`engine="html"` when `pdf_engine=latex`** → the LaTeX binary is missing and it silently fell back |
| `applicantos_knowledge_documents_total` | `kind` | Stable between indexes | **Growing on every re-index** → `fingerprint()` is unstable; you are duplicating documents |
| `applicantos_knowledge_index_duration_seconds` | `analyzer` | Seconds to a few minutes | **Growing run over run** → the same source is being fully re-analyzed each time |
| `applicantos_review_queue_size` | — | Small, drains | **Monotonically rising** → nobody is resolving reviews, or every application escalates |
| `applicantos_task_duration_seconds` | `task`, `outcome` | `ok` dominant | **`outcome="error"` on one task only** → that task's dependency is down |
| `applicantos_session_active` | — | 0 or 1 | **Stuck at 1 with no activity** → a session outlived its worker; `session.watchdog` should clear it within 5 min |
| `applicantos_http_request_duration_seconds` | `route`, `method`, `status` | p99 < 200ms | **p99 > 1s on a list route** → a missing index, or a lazy-load N+1 |

The single most informative pair is **`review_queue_size` against `applications_total{status="submitted"}`**.
Rising reviews with flat submissions is the system telling you it does not understand something —
and, on a fresh install, that it is doing exactly what it was told.

---

## 3. Draining workers for a deploy or a restart

**Never `kill -9` an apply worker.** It is holding a browser session on a partly-filled
application form.

```bash
# 1. Stop consuming new work, let in-flight tasks finish
celery -A app.workers.celery_app control cancel_consumer discovery
celery -A app.workers.celery_app control cancel_consumer ai
celery -A app.workers.celery_app control cancel_consumer apply
celery -A app.workers.celery_app control cancel_consumer knowledge

# 2. Watch until nothing is active (apply tasks can legitimately take 45 minutes)
watch -n5 "celery -A app.workers.celery_app inspect active | jq '[.[][]] | length'"

# 3. Warm shutdown — SIGTERM. Celery finishes what it has and exits.
kill -TERM $(pgrep -f 'celery.*worker')
```

Stop beat first, so nothing new is scheduled mid-drain:

```bash
kill -TERM $(pgrep -f 'celery.*beat')
```

**Why warm matters.** `task_acks_late=True` means a hard kill returns the message to the queue and
it runs again. That is safe by design — golden rule 1 is enforced in the database, and
`Pipeline.submit`'s first guard refuses an application that already reached `submitted` — but the
second run still re-opens a browser and re-does the work. Warm shutdown avoids the waste.

To drain a single queue while leaving the rest running, cancel just that consumer.

---

## 4. Replaying a failed application

A `failed` application carries `last_error` and its `attempt_count`. Nothing about it is lost —
`ResumeVersion.content_json` is retained forever, so the resume can be re-rendered without a model
call.

### Look at it first

```bash
export UID_HDR="X-User-Id: $USER_ID"
curl -s -H "$UID_HDR" "http://127.0.0.1:8000/api/v1/applications?status=failed" | jq '.items[] | {id, status, last_error, attempt_count, posting_id}'
curl -s -H "$UID_HDR" "http://127.0.0.1:8000/api/v1/applications/$APP_ID" | jq
```

### Look at what the browser saw

```bash
curl -s -H "$UID_HDR" "http://127.0.0.1:8000/api/v1/applications/$APP_ID/artifacts" | jq
```

Screenshots, page HTML and the artifact manifest. The **before** screenshot usually answers the
question in one glance: a login wall, a captcha, or a form that looks nothing like the selector
pack expects.

### Replay it

```bash
curl -X POST -H "$UID_HDR" "http://127.0.0.1:8000/api/v1/applications/$APP_ID/retry"
```

That re-enqueues `apply.submit`. The full guard ladder runs again from the top, so a replay cannot
double-submit: guard 1 refuses anything already `submitted` or `confirmed`.

If the rendered PDF was cleaned up, `submit` re-renders it from `content_json` automatically.

### From the shell, without the API

```bash
SQLITE_MODE=true python -c "
import asyncio, uuid
from app.database.session import session_scope
from app.services.pipeline import Pipeline
from app.config.settings import get_settings

async def main():
    async with session_scope() as s:
        result = await Pipeline(s, get_settings()).submit(uuid.UUID('$APP_ID'))
        print(result)
asyncio.run(main())"
```

### Before replaying in bulk

Ask *why* it failed. A batch of failures with the same `last_error` on the same provider means a
selector pack needs updating, and replaying them will simply fail again — more slowly, and with
more browser sessions.

```bash
curl -s -H "$UID_HDR" "http://127.0.0.1:8000/api/v1/applications?status=failed&limit=200" \
  | jq -r '.items[] | .last_error' | sort | uniq -c | sort -rn | head
```

---

## 5. Clearing a stuck review

A review is an application in `needs_review` with a `review_reason` and a `review_payload`
describing what could not be answered.

```bash
curl -s -H "$UID_HDR" "http://127.0.0.1:8000/api/v1/reviews" | jq '.items[] | {id, review_reason, posting_id}'
```

### Resolve — answer the questions and re-queue

```bash
curl -X POST -H "$UID_HDR" -H "Content-Type: application/json" \
  -d '{"answers": {"Why do you want to work here?": "…", "Years of C++ experience": "3"}}' \
  "http://127.0.0.1:8000/api/v1/reviews/$APP_ID/resolve"
```

Resolving returns the application to `ready` and enqueues `apply.submit`.

### Dismiss — decide against it

```bash
curl -X POST -H "$UID_HDR" "http://127.0.0.1:8000/api/v1/reviews/$APP_ID/dismiss"
```

Moves it to `abandoned`. Terminal; nothing retries it.

### When the reason is structural

| `review_reason` | Resolvable by answering? | Do this instead |
|---|---|---|
| `too_many_essays`, `unknown_field`, `low_confidence`, `ambiguous_answer` | ✅ | Answer and resolve |
| `captcha`, `mfa`, `login_required` | ❌ | Apply by hand. There is no honest automated path |
| `unsupported_flow` | ❌ | LinkedIn / Workday by design. Use the link, apply manually |
| `policy_block` | ❌ | The kill switch. Enable it in Settings, then resolve |
| `submit_not_found`, `file_upload_failed`, `verification_failed` | 🟡 | Check the artifacts. Usually a selector pack has rotted |
| `insufficient_knowledge` | ❌ | Index a knowledge source first, then retry |
| `rate_limited` | 🟡 | Wait, then resolve. It will retry with backoff |

### A review that will not clear

If `POST /resolve` returns 409, the application is not actually in `needs_review` — most often it
was already resolved in another window. Re-read it:

```bash
curl -s -H "$UID_HDR" "http://127.0.0.1:8000/api/v1/applications/$APP_ID" | jq '{status, review_reason}'
```

If the status is `submitting` and has been for more than an hour, the worker died mid-submit. See
§7, "an application is stuck in `submitting`".

---

## 6. Backup and restore

### What actually matters

| Priority | What | Why |
|---|---|---|
| **1** | The database | Every fact, every application, every proof of submission. Irreplaceable |
| **2** | `var/storage/` | Confirmation screenshots — the evidence you applied |
| **3** | `.env` | Reconstructible, but tedious |
| — | `var/cache/`, `var/browser/`, `var/screenshots/` temp files | Regenerable. Do not back these up |

Rendered resume PDFs are **deliberately** not on that list. `ResumeVersion.content_json` is the
source of truth and re-renders any of them.

### PostgreSQL

```bash
# Back up
pg_dump --format=custom --file="applicantos-$(date +%F).dump" \
  "postgresql://applicantos:applicantos@localhost:5432/applicantos"

# Restore into a fresh database
createdb applicantos_restored
pg_restore --dbname=applicantos_restored --clean --if-exists "applicantos-2026-08-08.dump"
```

### SQLite

Use the online backup API, not `cp` — copying a live SQLite file mid-write yields a corrupt copy.

```bash
sqlite3 var/applicantos.db ".backup 'var/applicantos-$(date +%F).db'"

# Restore
mv var/applicantos-2026-08-08.db var/applicantos.db
```

### Blobs

```bash
tar czf "storage-$(date +%F).tar.gz" var/storage/          # local backend
aws s3 sync "s3://$S3_BUCKET" ./storage-backup/            # s3 backend
```

### Verify a restore before you need it

```bash
SQLITE_MODE=true python -c "
import asyncio
from sqlalchemy import func, select
from app.database.session import session_scope
from app.models.application import Application
from app.models.knowledge import KnowledgeFact

async def main():
    async with session_scope() as s:
        print('applications:', await s.scalar(select(func.count(Application.id))))
        print('facts:', await s.scalar(select(func.count(KnowledgeFact.id))))
asyncio.run(main())"

alembic current          # schema version matches the code?
```

**Credentials are not in the backup.** Mailbox tokens live in the OS keychain
(`EmailAccount.credential_ref` is only a key). After restoring onto a new machine, reconnect the
mailbox — that is the privacy invariant working as designed, not a gap.

---

## 7. Symptom → cause → fix

### Nothing is happening

| Symptom | Likely cause | Fix |
|---|---|---|
| No postings discovered, no errors | Beat is not running | `celery -A app.workers.celery_app beat` |
| Postings discovered, nothing scored | No worker on the `ai` queue | Add `-Q …,ai` to the worker |
| Everything scores, nothing applies | **Kill switch — the expected default** | Set `AUTO_APPLY_ENABLED=true` and `DRY_RUN=false` |
| Applications reach `ready` and stop | No worker on the `apply` queue | Add `-Q …,apply` |
| API returns 202 but nothing runs | Broker unreachable; look for `degraded: true` in the body | Start Redis, or run in SQLite/inline mode |
| One provider finds nothing | Seed boards 404, or the feed shape changed | `pytest tests/test_providers.py -k <provider>` |
| `insufficient_knowledge` on every application | The knowledge graph is empty | Add a source, then `POST /knowledge/reindex` |

### Something is wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| An application is stuck in `submitting` | The worker died mid-submit | `session.watchdog` clears it within 5 min. To force: `PATCH /applications/{id}` to `failed`, then retry |
| Every application escalates with `unknown_field` | The provider redesigned its form | Update the `SelectorPack` in `app/browser/selectors.py`; check the artifact screenshots first |
| `submit_not_found` on one provider | The submit selector no longer matches | Same. **Never** relax to a heuristic button search |
| Resumes render as HTML when `pdf_engine=latex` | The LaTeX binary is missing | `pip install tectonic`, or set `LATEX_BINARY` to its path |
| Resumes overflow one page | `resume_max_pages` raised, or the shrink ladder exhausted | Check `documents.render_attempt` logs; reduce `max_bullets` |
| LLM calls fail with an auth error | Key expired or absent | Re-set it in Settings. Or run `LLM_PROVIDER=null` — the pipeline still works |
| Token usage climbing fast | Cache misses on LLM responses | Check `cache_events_total{namespace="llm"}`; requires `temperature=0` and `cache_llm_responses=true` |
| Duplicate postings from two providers | `dedupe_key` too tight | Check `normalize_company` / `normalize_title` against the two titles |
| The same job disappeared | `dedupe_key` too loose — merged into another | `pytest tests/test_dedupe.py`; compare the two `dedupe_key`s |
| Knowledge documents duplicate on re-index | `fingerprint()` is unstable for that analyzer | It must not include a timestamp or an unordered set |
| Desktop shows stale data | The WebSocket dropped | Check `/ws` connectivity; the app should reconnect and refetch |
| Desktop screen is empty, network shows 200 | **Enum drift** between `types.ts` and `enums.py` | Run the parity check in `.claude/agents/desktop-engineer.md` |
| Desktop starts, backend never comes up | The sidecar did not spawn, or an orphan holds the SQLite file | `pkill -f 'applicantos-server'`, then restart |
| `/ready` returns 503 on `database` | The database is down or the URL is wrong | Check `DATABASE_URL`; `alembic current` |
| `/ready` returns 503 on `redis` | Redis is down while `cache_backend=redis` | Start Redis, or set `CACHE_BACKEND=disk` |
| Worker logs `Event loop is closed` | Someone replaced `run_async` with `asyncio.run` | Restore `run_async` — the connection pool is loop-bound |
| A secret appeared in a log line | The redaction processor left the chain | Check `@setup_logging.connect` in `celery_app.py`. **Treat the key as compromised and rotate it** |

### Performance

| Symptom | Likely cause | Fix |
|---|---|---|
| Discovery is slow | Descriptions parsed before filtering | Cheap filters first — see `ADDING_A_PROVIDER.md` §4 |
| Apply p99 in the tens of minutes | Pages hanging on load | Lower `PLAYWRIGHT_TIMEOUT_MS`; check `detect_blockers` |
| Indexing takes longer each run | Fingerprints unstable → full re-analysis | Fix `fingerprint()` for the offending analyzer |
| Desktop idle CPU above 3% | The query persister is rewriting a large blob | The per-query IndexedDB persister must be in use — see `lib/query/persist.ts` |
| The database is growing fast | `log_entries` and old artifacts | Run `cleanup.prune_artifacts` and `cleanup.prune_cache` |

---

## 8. Emergency stop

**Stop it applying to anything, right now:**

```bash
# In .env — takes effect on the next task, no restart needed for new workers
AUTO_APPLY_ENABLED=false
DRY_RUN=true
```

Or from the desktop app: **Settings → Automation → Auto-apply off**. Or over the API:

```bash
curl -X PUT -H "$UID_HDR" -H "Content-Type: application/json" \
  -d '{"auto_apply_enabled": false, "dry_run": true}' \
  http://127.0.0.1:8000/api/v1/settings
```

Then stop the workers (§3). Applications already in flight finish or fail; nothing new is
submitted. Both switches default to the safe position, so if you are unsure what state you are in,
setting both back is always correct.

---

## 9. Useful one-liners

```bash
# What is the pipeline doing right now?
curl -s -H "$UID_HDR" http://127.0.0.1:8000/api/v1/analytics/overview | jq

# The funnel: discovered → scored → applied → interview
curl -s -H "$UID_HDR" http://127.0.0.1:8000/api/v1/analytics/funnel | jq

# Recent errors
curl -s -H "$UID_HDR" "http://127.0.0.1:8000/api/v1/logs?level=ERROR&limit=50" | jq -r '.items[] | "\(.at) \(.event) \(.payload)"'

# Every registered plugin
curl -s -H "$UID_HDR" http://127.0.0.1:8000/api/v1/settings/plugins | jq -r '.[] | "\(.kind)\t\(.name)\t\(.version)"'

# Force a discovery poll
curl -X POST -H "$UID_HDR" http://127.0.0.1:8000/api/v1/postings/discover

# Force a knowledge reindex
curl -X POST -H "$UID_HDR" http://127.0.0.1:8000/api/v1/knowledge/reindex

# Confirm the kill switch is where you think it is
curl -s -H "$UID_HDR" http://127.0.0.1:8000/api/v1/settings | jq '{auto_apply_enabled, dry_run, is_submission_allowed}'
```

---

## See also

- [`CONFIGURATION.md`](CONFIGURATION.md) — every setting and when to change it
- [`PIPELINE.md`](PIPELINE.md) — what each stage writes, so you know where to look
- [`SAFETY.md`](SAFETY.md) — the safety envelope
- [`PACKAGING.md`](PACKAGING.md) — building and shipping the desktop app
