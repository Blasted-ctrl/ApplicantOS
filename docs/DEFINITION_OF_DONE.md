# Definition of Done

The honest, complete list of what remains before ApplicantOS is finished. Measured against the
original brief, not against a feeling. Every ✅ below was verified by running something; every ⬜
is a real gap with a named owner task.

Last measured: 2026-08-09.

---

## The twelve original criteria

From the brief: *"When this project is 'done', it should…"*

| # | Criterion | State | Evidence |
|---|---|---|---|
| 1 | Discover postings from supported ATS platforms | ✅ | Run against the live APIs. `tests/integration/test_providers_live.py` (12 tests, nightly CI) parses real postings from Greenhouse, Lever, Ashby and Workday; `scripts/validate_boards.py` checks every shipped board token. **G1** |
| 2 | Deduplicate so the same role is never processed twice | ✅ | `tests/test_dedupe.py`; cross-provider collapse proven |
| 3 | Score against configurable preferences | ✅ | Canonical example totals exactly 70; deterministic over 100 runs |
| 4 | Generate a tailored one-page résumé | ✅ | Verified live: same graph → different résumés for Microsoft Embedded vs NVIDIA |
| 5 | Generate a tailored cover letter | ✅ | `CoverLetterWriter`, policy-gated |
| 6 | Apply automatically to supported flows | ✅ | Driven against real Greenhouse, Lever and Ashby forms in a real Chromium. `tests/integration/test_browser_live.py` (25 tests, nightly) discovers 26/9/12 fields, locates the real submit control, uploads and verifies a placeholder résumé, and proves the kill switch holds with zero recorded clicks. It found five selector defects. **G2** |
| 7 | Detect manual-input cases and pause | ✅ | 10 `ReviewReason` paths, mutation-tested |
| 8 | Store metadata, documents, timestamps, screenshots | 🟡 | Stored; **`ApplicationVerifier` had no caller** → in flight, **G3** |
| 9 | Clean up temp résumés after submission | ✅ | `test_golden_cleanup.py` |
| 10 | Dashboard: search, filter, stats, logs | ✅ | Driven in a real Chromium against a real backend. All 13 routes settle on real content or a genuine empty state, zero console errors, and the §10.14 budget holds on the production build. It found four defects, including a CORS default that broke the entire desktop dev loop. Screenshots in `docs/screenshots/`. **G8**, **G11** |
| 11 | Run continuously in Docker with retries + health checks | 🟡 | Compose + Dockerfiles written and `config`-validated; **images never built or run.** → **G4** |
| 12 | Modular — new ATS as a plugin, no core changes | ✅ | `test_golden_plugin_isolation.py` enforces it statically |

---

## Open gaps

### In flight (workflow `wire-and-close`)

- **G3 — Three subsystems have no caller.** `ApplicationVerifier` (submission proof never
  verified), `as_prompt_context` (the AI memory learns nothing that reaches a prompt),
  `reinforce()` (memory weight never moves). Plus `sanitize_external_text` was specified in
  CONTRACTS §10b and never implemented, leaving job descriptions — attacker-controlled text —
  going straight into prompts.
- **G5 — The apply pipeline is unexercised end to end** — ✅ **closed.** `scripts/smoke_test.py`
  now reports `85 passed, 0 failed, 0 skipped`. It seeds a synthetic account, posting and status
  signal before the flows run, drives every flow as that account so nothing it does can touch real
  data, and deletes all of it afterwards (non-zero exit if anything survives). Because no Celery
  worker is running, `POST /postings/{id}/apply` only enqueues — so the apply flow *also* runs
  `Pipeline.run_one` in-process against the same database, which is what actually exercises
  score → retrieve → tailor → render → the guard ladder. It ends where it must with both switches
  closed: `needs_review` / `policy_block`, `submitted=False`.
  `scripts/seed.py` gained the six scored postings that make a fresh clone show a populated
  dashboard, feed and review queue; every score comes from the real `Scorer`, none is hand-written.
- **G6 — 31 ruff findings** — ✅ **closed.** `ruff check .` is clean. The `E501`s were rewrapped
  rather than suppressed; `SIM103`/`SIM105`/`SIM108`/`RUF005`/`RUF007`/`RUF046`/`F401`/`N811` were
  fixed in the code; `UP042` was closed by deriving `StrEnum` from `enum.StrEnum` instead of a
  hand-rolled `(str, Enum)` mixin. One documented per-file ignore was added — `N805` on
  `app/models/mixins.py`, where SQLAlchemy's `@declared_attr` requires the first parameter to be
  `cls` and pep8-naming cannot know that.
- **G7 — 50 mypy findings** — ✅ **closed.** `mypy app` reports
  `Success: no issues found in 173 source files`. None of the 50 was a defect, so none was
  closed by changing behaviour: every fix either states a type the code already had, or
  narrows one the checker could not follow. Where a rewrite touched control flow rather than
  annotations — the `pyproject.toml` dependency parser, `_is_present`, the timeseries bucket
  lookup — the new code was run against the old side by side over a matrix of inputs
  (20 / 11 / 7 cases) and matched on every one, exceptions included.

  How they closed, in the order of preference the gate is meant to encourage:

  1. **A truer annotation — 43.** `StrEnum.coerce` returns `Self`, not a bare `StrEnum`, so
     `FactKind.coerce(...)` is a `FactKind` again (3). `ATSProvider.paginate` is an
     `AsyncGenerator`, not merely an `AsyncIterator` — which is what lets `aclosing` release a
     suspended page (1). The three board readers keep whatever their `isinstance(job, Mapping)`
     guard admits, so they return `list[Mapping[str, Any]]`, matching the `_to_raw` /
     `_candidates` helpers they feed (3). `ITEM_FACT`/`ITEM_CHUNK`/`ITEM_ENTITY`/`ITEM_MEMORY`
     are `Final[Literal[…]]`, which is the point of them — they are handed straight to a
     schema field that accepts exactly those four strings (4). `FLOAT32_TYPECODE` is
     `Final[Literal["f"]]`, because `array` is overloaded on its typecode and a plain `str`
     resolves to the *integer* overload (1). The rest are locals annotated for what they hold
     (unvalidated YAML, a serialised request body, a payload dict), three shadowed names given
     their own, one `Final` tuple bound before unpacking, and one repeated
     `buckets.get(_local_day(x))` promoted to a `_bucket_for` helper that handles the null
     timestamp once instead of five times.
  2. **A `TypeGuard` — 1.** `_is_usable_vector` was a type guard in behaviour and a `bool` in
     signature; the caller iterates the value it has just proven is a list of numbers.
  3. **A precise local cast — 5.** `session.execute(delete(...))` is annotated `Result` but
     always produces the `CursorResult` that carries `rowcount` (2); a column declared with
     `EmbeddingType` is typed as the base `TypeEngine`, which has no `load_dialect_impl` (2);
     FastAPI's `tags` is `list[str | Enum]` and `list` is invariant (1).
  4. **A targeted `# type: ignore` — exactly 1**, with its reason in the code:
     `RobotFileParser.disallow_all` is set in `__init__` and read by `can_fetch` in CPython,
     and simply missing from typeshed.

  No ignore was added to `pyproject.toml`, and no rule was disabled: the mypy config is
  byte-for-byte what it was.

- **G9 — `FieldKind.FILE` is skipped by `AutoFiller.fill`** — ✅ **closed.** `fill()` still skips
  file inputs (the confidence machinery has nothing to say about one); the apply driver now
  reconciles them itself from `discover_fields()` via `app/browser/apply.py::plan_documents`,
  uploads each through `AutoFiller.upload`, and routes a missing document, a stale path or an
  `UploadFailedError` to `FILE_UPLOAD_FAILED`. An application can no longer be submitted with no
  résumé attached; `tests/test_apply_driver.py` proves it.
- **G10 — the `parser` plugin kind had zero implementations** — ✅ **closed by removing it**,
  and the sweep that came with it found two more things.

  Option B was considered first and rejected on evidence: the readers a `ParserPlugin` would
  abstract already exist exactly once in `app/knowledge/analyzers/document.py`, are already
  shared by both `DocumentAnalyzer` and `ResumeParser`, and are dispatched by **magic number**
  (`%PDF-`, the ZIP header) ahead of suffix and `Content-Type` — a decision a registry keyed by
  `(kind, name)` cannot express. There was no duplication to remove and there would have been
  one implementation behind the abstraction. `app/documents/` is the *write* path and shares
  nothing with it. Removed from all five places the vocabulary lives —
  `app/models/enums.py`, `desktop/src/lib/api/types.ts`, `docs/CONTRACTS.md` §6,
  `app/plugins/loader.py::ENTRY_POINT_GROUPS`, `docs/ARCHITECTURE.md` — plus `CLAUDE.md` and
  two module docstrings. No migration: `PluginKind` is not a column. `pyproject.toml` needed
  no edit — this distribution publishes no entry points; the groups are for third parties.
  Reasoning recorded in `docs/OPEN_QUESTIONS.md` §72.

  What it revealed:

  1. **§17 enum parity was never tested.** Removing a member from `app/models/enums.py` and
     forgetting `desktop/src/lib/api/types.ts` would have left every gate green and broken only
     the running client. `tests/test_models.py` now parses every `as const` union out of
     `types.ts` and asserts values *and order* per enum, plus that the mapping table covers
     every declared `StrEnum` — so an enum added without a mirror fails. 23 tests, all 22 enums
     pass.
  2. **Eight domain metrics have no producer** — filed as **G12**, and the reason the sweep was
     worth running: it is the `ApplicationVerifier` pattern one layer up.

  Also swept clean: no dead `Settings` keys. Four smaller findings — `StatusSource.PIPELINE`
  never written (a provenance defect: pipeline submissions are stamped `manual`), five
  `PostingStatus` members never written, six API schemas with no endpoint, and two deliberately
  unproduced `SignalSource` members — are inventoried with recommendations in
  `docs/OPEN_QUESTIONS.md` §74.

- **G1 — No provider had been run against its live API** — ✅ **closed**, and it found three
  real defects that every unit test passed straight through.

  `scripts/validate_boards.py` probes every token in `app/jobs/seeds.py` against the live
  provider API, sequentially and with a descriptive User-Agent, and exits non-zero when any
  token is dead. `tests/integration/test_providers_live.py` (12 tests, `-m integration`, so
  the default suite stays hermetic and offline) parses real postings from all four discovery
  providers and asserts on *shape* — `external_id`, absolute `url`, `title`, `company_name`,
  a non-empty `description`, a timezone-aware `posted_at` — never on a job that will be gone
  next week. A transient failure skips with its reason; a schema change fails.
  `.github/workflows/integration.yml` runs both nightly and on demand, and never on push, so
  a provider outage cannot redden a pull request.

  What it revealed:

  1. **The seed lists were mostly dead.** 46 of 107 tokens returned nothing — including
     **28 of 33 Lever tokens**, so Lever discovery had been silently returning zero on every
     install. Every replacement was verified individually; the lists are now
     greenhouse 49, lever 30, ashby 40, workday 37, all live on 2026-08-09.
  2. **Workday discovery was returning nothing for every tenant.** The tenant root now
     answers `406` to every request, browser User-Agents included, and shard/site resolution
     was built entirely on fetching that page. Resolution now reads `robots.txt` — which
     answers `200` only on the tenant's own shard, and names its career sites exactly — with
     the CXS jobs endpoint as confirmation (`404` = right shard, wrong site; `422` = wrong
     shard). A `Disallow`ed board is never polled. Live result: **0/37 tenants → 25/37**.
  3. **An empty board was invisible.** `LeverProvider.search` now emits
     `lever.board_empty` for a feed that carried no postings — distinct from a feed the query
     filtered to nothing — and `DiscoveryReport` carries `boards_by_provider` and
     `empty_providers`, so a run can say `lever: 30 boards, 0 postings` instead of nothing.
     `tests/test_board_health.py` covers all of it hermetically.

- **G2 — Browser automation had never driven a real browser** — ✅ **closed**, and like G1 it
  found real defects that every unit test passed straight through. Five of them.

  `tests/integration/test_browser_live.py` (25 tests, `-m integration`) resolves one live
  posting per provider from the board API, opens its real application form once in a real
  headless Chromium, and asserts on what discovery, blocker detection and the kill switch
  actually do. `DRY_RUN=true` / `AUTO_APPLY_ENABLED=false` are forced at import time and
  re-asserted against the live settings object; every page carries a capture-phase click
  recorder installed before navigation, and every test asserts **zero clicks of any kind** for
  the whole session — no submit, no cookie banner, nothing. One page load per provider, a
  polite gap between them, and a User-Agent that names the tool. The only bytes sent to an
  employer are one 700-byte generated placeholder PDF, to one board, to prove
  `AutoFiller.upload` can verify an attachment against a real uploader.

  What it revealed:

  1. **Every application would have escalated to `CAPTCHA`.** Greenhouse, Lever and Ashby all
     load a captcha vendor in *invisible*, score-based mode on every posting — a reCAPTCHA
     Enterprise badge parked off-screen, an hCaptcha enclave with zero height. The packs
     matched that bookkeeping, so `detect_blockers` reported `captcha` on all three forms and
     100% of applications would have gone to manual review, with the unit suite green.
     `_COMMON_CAPTCHA_MARKERS` now excludes the badge subtree, `[data-sitekey]` and
     `#g-recaptcha-response`; `BrowserSession._probe_captcha` now requires a marker to be
     **rendered**. A real challenge — including one an invisible widget escalates to — is
     rendered by definition and still caught.
  2. **Lever's form root was a page section, not the form.** `.application-form` is the class on
     each of five panels; the form is `form#application-form`. Discovery took the panel with the
     most controls, 7 of 23, and never saw the LinkedIn field, the employer's custom question
     card or the consent checkbox. 6 → 9 discovered fields.
  3. **Lever's submit selector pointed at a hidden button.**
     `[data-qa='submit-application-button']` matches nothing today; the real control is
     `#btn-submit[data-qa='btn-submit']` and carries `type="button"`. The generic
     `button[type='submit']` fallback therefore resolved to `#hcaptchaSubmitBtn.hidden` — a
     zero-size helper earlier in the document, and a Playwright locator's `.first` is document
     order, not selector order.
  4. **Greenhouse's field containers matched nothing at all.** The current board renders
     `.text-input-wrapper` and `.select__container`, not `.field` / `.application-question`.
     Survivable only because Greenhouse also emits `<label for>`.
  5. **A redirecting board discovered a search box as its application form.** Stripe,
     Databricks, Coinbase, Asana and Brex redirect `job-boards.greenhouse.io/<board>/jobs/<id>`
     to their own careers site. No pack selector matched, discovery fell back to
     `document.body`, and returned that site's "Search for a role" input as the form's only
     field. `discover_fields` now reports an unmatched `form_root` as no fields (CONTRACTS §12
     invariant 6) — a question for a human, per golden rule #2.

  Live result on 2026-08-09: greenhouse 26 fields / lever 9 / ashby 12, no blockers on any of
  the three, the real submit control located and visible on all three, résumé upload verified,
  `submit(dry_run=True)` false with zero clicks recorded.

- **G8 / G11 — the desktop app had never been run against real data** — ✅ **closed**, and it
  found four defects, two of them in the class "the screen states something false".

  A Playwright sweep drives all thirteen routes in a real Chromium at 1440×900 against a real
  backend (`SQLITE_MODE=true LLM_PROVIDER=null`) on a database seeded by `scripts/seed.py` and
  then put through one real pass of `Pipeline.run_one` — so the two applications on screen were
  produced by the pipeline stopping at the kill switch, not written by hand. Every route is
  asserted to settle on real content or a genuine empty state — never a skeleton, never an error
  boundary — with **zero `console.error`s and zero unhandled rejections** on all thirteen.
  `docs/screenshots/` holds one full-page PNG per screen and README §Screenshots embeds five of
  them; the placeholder is gone.

  What it revealed:

  1. **The whole desktop development loop was broken by CORS, in both of its modes.** The default
     `cors_origins` listed `http://localhost:5173` and `app://applicantos` — an Electron scheme
     this Tauri app never presents. But `desktop/vite.config.ts` binds the dev server to
     `127.0.0.1`, which is a *different origin* to a browser, so every request from the printed
     URL failed pre-flight. `tauri dev` failed the same way for a subtler reason: a debug shell
     **attaches** to the backend `scripts/dev-with-backend.mjs` already started, so the correct
     origin list in `sidecar.rs::backend_environment` never gets applied. The default now carries
     both spellings of `:5173` and all three webview origins, so the fallback is correct wherever
     the backend is started from. Proven by pre-flight against each origin, with an unlisted
     origin still rejected 400.
  2. **The persisted query cache never garbage-collected, and threw on every launch.** The
     `idb-keyval` adapter implemented `getItem`/`setItem`/`removeItem` but not `entries`, which is
     the only way `persisterGc` can walk the store — so expired and busted entries stayed in
     IndexedDB forever, and a development build *raises* rather than degrading. It was the one
     uncaught error the sweep saw on every route.
  3. **The score panel said "No rule contributed to this score" on every application and every
     posting.** `ScoreRead.components` was documented as "populated by the service layer" and no
     service layer populated it; `Score` is a table with a `breakdown` JSON column and no
     `components` attribute, so every `ScoreRead.model_validate(row)` produced an empty list over
     a breakdown holding the whole arithmetic. CONTRACTS §10 makes explainability a hard
     requirement, and it was failing in the product with the unit suite green. Derived in the
     schema now, so no route can forget it; a breakdown that will not parse yields no components
     rather than a 500.
  4. **Six screens printed an empty state over a query that had not answered yet**, which
     §10.10 forbids in as many words. Measured, not inferred: `/reviews` showed
     *"Nothing needs you — the agent handled everything on its own"* for **646ms** while two
     applications sat in the queue, `/knowledge` claimed "No sources yet" for 725ms over three
     indexed sources, `/resumes` for 204ms. On the review queue that sentence is the one the
     product must never guess at. All gated on `isPending`; re-measured at zero flashes.

  **The instant-feel budget (§10.14), measured on the production build against a live backend.**
  The dev server is not a fair test — unbundled ESM and StrictMode's double invoke put every
  number 3-10× over — so it was built and served with `vite preview`.

  | Metric | Budget | Measured | |
  |---|---|---|---|
  | Cold start → dashboard's first real figure, empty cache | — | **455ms** | first-ever launch |
  | Cold start → same, hot snapshot present | ≤ 800ms p50 | **262ms** | §10.5 layer 1 works; snapshot 138 KB of a 500 KB cap |
  | Cold start → interactive | ≤ 1500ms | **455ms** | ✅ |
  | Route change, unvisited route, warm cache | ≤ 100ms | **31.6ms p50** | ✅; one 294ms outlier, `/resumes` fetching its lazily-split leaf (§10.7, by design — 29.5ms on the next visit) |
  | Route change, visited route | ≤ 16ms p50 / 50ms p99 | **8.6ms p50** corrected | measured 20.3ms p50 / 56.1ms p99, minus 11.7ms of harness overhead calibrated by running the same instrument on a click that changes nothing |
  | Interaction → paint (filter chip) | ≤ 50ms p99 | **13.2ms p50 / 18.2ms p99** | ✅ |

  The one number that looked like a cache miss was chased down and was not one. A lap over all
  eleven destinations appeared to attribute ~3 API requests per revisit to `/knowledge`; a
  controlled revisit issues **zero** — first visit fetches `stats`, `sources`, `graph` and
  `facts`, every later visit fetches nothing, and that holds past `usePrefetch`'s 60-second
  throttle floor too (measured with a 65-second gap: zero requests, 30.7ms to paint). The
  per-route counts in the lap run were the *previous* destination's requests landing inside the
  next one's measurement window. The cache is genuinely reused; nothing refetches on revisit.

  Two defects were found and deliberately **not** fixed here, because each needs an API change
  rather than a render fix, and inventing one at the end of a verification pass is the wrong
  trade:

  - **The Résumés screen can never show a version.** It picks one with
    `selected?.latest_version?.id`, `GET /resumes` deliberately leaves `latest_version` null (a
    picker should not drag a full `content_json` per row), and no endpoint lists a variant's
    versions. So the pane reads "This variant has never been generated" beside a header reading
    "1 variant · 2 versions". Needs `GET /resumes/{id}` returning the latest version, or a
    versions list endpoint.
  - **Re-entering onboarding shows empty fields over a completed profile**, because
    `GET /onboarding/steps` describes fields but carries no current values — and
    `_write_identity` / `_write_work_authorization` assign unconditionally, so stepping through
    with blanks would erase a name, a location and a citizenship. Needs a `value` on
    `OnboardingField`.

### Not yet started

- **G4 — Docker images have never been built or run.** `docker compose config` validates and env
  parity was checked against `Settings`, but no image exists. Needs a real
  `docker compose build && up`, a health-check pass on every service, and one end-to-end run
  through the composed stack.
- **G12 — every domain metric has no producer.** Found by the G10 decoration sweep. The
  infrastructure metrics are wired (HTTP, cache, LLM, Celery, review-queue and session gauges),
  but `record_posting_discovered`, `record_posting_deduped`, `record_score`,
  `record_application`, `observe_apply`, `record_document_rendered`,
  `record_knowledge_document` and `observe_knowledge_index` have **zero call sites** — so the
  funnel this product exists to run is flat zero on `/metrics` forever. Same shape as G3's
  `ApplicationVerifier`, one layer up. Call sites are named in `docs/OPEN_QUESTIONS.md` §74.1.

---

## Standing quality gates

These must be green at all times, not just once:

```
ruff check .                 # target: clean            (clean ✅)
mypy app                     # target: clean            (clean ✅ — 173 files, 2026-08-09)
pytest                       # target: all pass         (643 passed, 37 deselected ✅)
python -m scripts.smoke_test # target: 85/0/0           (85/0/0 ✅)
cd desktop && npm run typecheck && npm run lint && npm run build   # ✅
```

Plus one gate that is deliberately **not** in the default run, because it needs a network and
talks to third parties:

```
pytest -m integration            # target: all pass     (37 passed ✅ 2026-08-09)
python -m scripts.validate_boards  # target: 0 dead     (156/156 live ✅ 2026-08-09)
```

Plus, from a genuinely fresh clone: `pip install -e ".[dev]"` → `alembic upgrade head` →
`python -m scripts.seed` → `pytest` must all succeed. ✅ verified 2026-08-09.

---

## Explicitly out of scope

Recorded so they are not mistaken for gaps:

- **LinkedIn and Workday automated submission.** Deliberate — their terms prohibit it. They are
  discovery-only by design and route to manual review.
- **Multi-user / hosted deployment.** Single-user desktop app; auth is a single-tenant shim.
- **The future AI features in `docs/ROADMAP.md`** — natural-language policy rules, recruiter-email
  answering, interview scheduling, rejection analysis. Designed, not built.
