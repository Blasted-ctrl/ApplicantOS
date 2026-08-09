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
| 1 | Discover postings from supported ATS platforms | 🟡 | 5 providers registered and unit-tested against recorded payloads. **Never run against the live Greenhouse/Lever/Ashby APIs.** → **G1** |
| 2 | Deduplicate so the same role is never processed twice | ✅ | `tests/test_dedupe.py`; cross-provider collapse proven |
| 3 | Score against configurable preferences | ✅ | Canonical example totals exactly 70; deterministic over 100 runs |
| 4 | Generate a tailored one-page résumé | ✅ | Verified live: same graph → different résumés for Microsoft Embedded vs NVIDIA |
| 5 | Generate a tailored cover letter | ✅ | `CoverLetterWriter`, policy-gated |
| 6 | Apply automatically to supported flows | 🟡 | Full path coded; **never driven against a real browser or a real form.** → **G2** |
| 7 | Detect manual-input cases and pause | ✅ | 10 `ReviewReason` paths, mutation-tested |
| 8 | Store metadata, documents, timestamps, screenshots | 🟡 | Stored; **`ApplicationVerifier` had no caller** → in flight, **G3** |
| 9 | Clean up temp résumés after submission | ✅ | `test_golden_cleanup.py` |
| 10 | Dashboard: search, filter, stats, logs | ✅ | 12 screens, builds clean, runs |
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
- **G9 — `FieldKind.FILE` is skipped by `AutoFiller.fill`** — ✅ **closed.** `fill()` still skips
  file inputs (the confidence machinery has nothing to say about one); the apply driver now
  reconciles them itself from `discover_fields()` via `app/browser/apply.py::plan_documents`,
  uploads each through `AutoFiller.upload`, and routes a missing document, a stale path or an
  `UploadFailedError` to `FILE_UPLOAD_FAILED`. An application can no longer be submitted with no
  résumé attached; `tests/test_apply_driver.py` proves it.

### Not yet started

- **G1 — No provider has been run against its live API.** Every provider test uses a recorded
  fixture. A schema change at Greenhouse would not be caught. Needs a `@pytest.mark.integration`
  suite that hits the real public board APIs, plus a nightly CI job — kept out of the default run
  so the suite stays hermetic and offline.
- **G2 — Browser automation has never driven a real browser.** Playwright 1.62 is installed and
  every test uses a recording fake. The fake proves the *logic*; it cannot prove the selectors
  match real Greenhouse/Lever/Ashby DOM. Needs an integration test against each provider's real
  public application form with `DRY_RUN=true`, asserting fields are discovered and filled and that
  submit is never clicked.
- **G4 — Docker images have never been built or run.** `docker compose config` validates and env
  parity was checked against `Settings`, but no image exists. Needs a real
  `docker compose build && up`, a health-check pass on every service, and one end-to-end run
  through the composed stack.
- **G7 — 50 mypy findings.** All triaged as SQLAlchemy/stdlib stub gaps and narrowing limitations,
  not defects. Worth closing with targeted annotations so the gate is green and starts carrying
  signal.
- **G8 — No screenshots.** README §Screenshots is an explicit placeholder. The app runs; nobody
  has captured it.
- **G10 — `parser` plugin kind has zero implementations.** CONTRACTS §6 declares five plugin kinds;
  `parser` is declared and unused (résumé parsing registered as an *analyzer* instead). Either
  implement one or remove the kind — a declared extension point with no implementation is the
  same decorative registry the research pass criticised elsewhere.
- **G11 — Desktop app never visually verified.** It typechecks, lints, builds, and serves, but no
  one has confirmed the 12 screens render correctly against real data, or that the instant-feel
  budget (route change < 100ms) actually holds.

---

## Standing quality gates

These must be green at all times, not just once:

```
ruff check .                 # target: clean            (clean ✅)
mypy app                     # target: clean            (currently 50)
pytest                       # target: all pass         (601 passed ✅)
python -m scripts.smoke_test # target: 85/0/0           (85/0/0 ✅)
cd desktop && npm run typecheck && npm run lint && npm run build   # ✅
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
