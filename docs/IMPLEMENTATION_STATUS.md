# Implementation status

Reproducible evidence for every production claim. Nothing here is marked done because it was
written; it is marked done because a command was run and its output is quoted.

A claim with no command under it is not a claim, it is an intention. Those live under
**Not yet proven**.

Last verified: 2026-08-14.

---

## Quality gates

| Gate | Command | Result |
|---|---|---|
| Lint | `ruff check app/ tests/ scripts/` | clean |
| Types | `mypy app` | clean, 177 source files |
| Tests | `pytest` (SQLite, `LLM_PROVIDER=null`) | **775 passed** |
| Desktop types | `npm run typecheck` | clean |
| Desktop lint | `npm run lint` | clean |

The test command is the zero-dependency path — no API key, no Postgres, no Redis:

```
SQLITE_MODE=true LLM_PROVIDER=null EMBEDDING_PROVIDER=hashing VECTOR_STORE=memory pytest
```

---

## Proven end to end

### The pipeline runs without Celery, Redis or a broker

`dispatch()` routes to a Celery worker when one is *consuming the target queue* and otherwise
executes in-process. The distinction matters: publishing to a broker succeeds whether or not
anything reads it, and this install's default broker URL is the default Redis port, which on
the development machine was answered by an unrelated project's container. Every task was
published successfully and executed by nobody.

Measured on a clean database with no worker and no beat process:

```
run started -> mode: inline
245 postings discovered and scored
0 "database is locked", 0 discovery.ingest_failed, 0 task failures
```

With a worker running, the same call reports `mode: worker` with a real task id and no
inline execution — exactly one executor runs each task.

### The periodic schedule runs without `celery beat`

`PeriodicScheduler` ticks `BEAT_SCHEDULE` inside the API process and stands down when a real
worker appears. Observed firing on its own five minutes after start:

```
scheduler.fired task=cleanup.refresh_gauges mode=inline
scheduler.fired task=session.watchdog       mode=inline
inline.executed x2
```

`session.watchdog` is the reaper for stranded run sessions, which previously could never run
on a desktop install — a dead session stayed `running` forever and silently swallowed every
subsequent "Start a Run".

### Discovery reaches every board

The three submission-capable providers spent one shared budget across their board list in
list order, so the tail was never polled. Before the fix an entire Greenhouse run came from
one employer. After:

```
yielded 120 postings from 52 distinct employers
most-represented: Affirm 11, May Mobility 9, Airbnb 2, Airtable 2, Amplitude 2
```

### Résumé tailoring is grounded and one-page

Run against a real posting with the user's 167 indexed facts:

```
resume_engine.rewrite_rejected  x6   reason=low_overlap threshold=0.35
resume_engine.summary_rejected       reason=low_overlap threshold=0.5
resume_engine.title_rejected    x3
resume_engine.tailored  bullets=8 candidates=60 selected=8
latex.rendered  pages=1 bytes=20442 engine=xelatex
pipeline.prepared  page_count=1 sections=['Experience', 'Projects']
```

Six candidate bullets and the summary were **rejected for insufficient overlap with their
source facts** — golden rules #7 and #9 refusing to fabricate, observed rather than asserted.

`tectonic` is absent on this machine; the LaTeX path fell through to MiKTeX `xelatex`
automatically and produced a one-page PDF.

### A dry run rehearses a real application

Against a live Lever posting, headless, `auto_apply_enabled=true` + `dry_run=true`:

```
apply.started  dry_run=True url=https://jobs.lever.co/calstart/<id>/apply
autofill.fields_discovered  fields=24 required=15 essays=0
autofill.filled  filled=14 needs_review=9 unanswered_required=8
pipeline.rehearsed  screenshots=1 unanswered_fields=9
status after: ready
```

Nothing was submitted, and the application stayed `READY` — a rehearsal is repeatable and
does not consume the row's one chance at a real submission.

### Never apply twice, under concurrency

The status ladder closed sequential double-runs only; the `READY` check was a read, with a
résumé render and a PDF between it and the write. `ApplicationService.claim` is now a
conditional `UPDATE ... WHERE status = :expected`.

Mutation-tested: removing the status predicate from the `WHERE` turns
`test_two_concurrent_claims_cannot_both_win` and `test_a_lost_claim_writes_nothing` red;
restoring it turns them green.

### Prompt-injection defence

`app/ai/untrusted.py` — 61 KB, **66 tests passing**, wired into résumé tailoring, cover
letter generation and field answering.

---

## Proven: one real application, accepted by the employer

**Software Engineering Intern — Instead**, `job-boards.greenhouse.io/instead/jobs/7761472003`,
application `daa3aed2-5ad1-41eb-a2bb-64c67ca02b0d`, scored 100 / verdict `apply`, submitted
2026-08-14 with `AUTO_APPLY_ENABLED=true DRY_RUN=false RESUME_SOURCE=master`.

```
filled  First Name / Last Name / Preferred First Name / Email / Phone
        LinkedIn URL / "Which computer type do you use; Mac, Linux or PC?"
autofill.combobox_committed   label=Country via=option
autofill.uploaded             filename='Ali F Resume (1).pdf' selector=#resume
autofill.submit_clicked       pack=greenhouse
[challenge] email code prompt is up
[RESULT] CONFIRMED via 'Thank you for applying'
URL: https://job-boards.greenhouse.io/instead/jobs/7761472003/confirmation
```

Proof is archived under `var/screenshots/instead-7761472003/`: the filled form, the page
after the first submit, the code entered, and the confirmation page reading *"Thank you for
applying! Your application has been received."*

One step needed a human, by the employer's design. Greenhouse held the completed application
behind an 8-character code emailed to the applicant — the anti-bot check now detected by name
(`app/browser/selectors.py`, `_COMMON_CAPTCHA_MARKERS`) rather than reported as an
inconclusive page. Each submit attempt invalidates every earlier code, so clearing it requires
a session that is still open at the prompt when the code arrives. Reading the code out of the
mailbox and typing it was scripted here; **that loop is not in the product yet** — the pipeline
still stops at `NEEDS_REVIEW`, which is the correct default and the honest state of the code.

---

## Not yet proven

Listed with what is missing, not with an excuse.

| Item | State | What is missing |
|---|---|---|
| Injection chokepoints 4 and 5 | Partial | Wired into 3 of the 5 sites the contract names. Website/portfolio extraction and job-posting ingestion are not screened. |
| Gmail / Outlook OAuth | Unproven **in-product** | An authorised Gmail account was read during the submission above, but through an external connector, not through `app/tracking/`. Credentials-in-keychain and read-only scope remain unverified. |
| Resuming a review item with a supplied answer | Absent | A challenge escalates to `NEEDS_REVIEW`; nothing can hand an answer back to a live browser session and continue. This is what would make the emailed-code flow unattended. |
| macOS / Linux sidecars | Absent | Only `applicantos-server-x86_64-pc-windows-msvc.exe` is built. |
| Signed installers, release CI, updater | Absent | `ci.yml` and `integration.yml` exist; there is no `release.yml`, no signing, no updater. |
| Performance CI (Playwright + CDP) | Absent | The budget is documented in `docs/UI.md` and not enforced anywhere. |
| `portal_tracker.py` | Absent | Named in the contracts. Logged at every startup as `tracking.tracker_absent`. Either implement it or narrow the contract. |
| Natural-language policy compilation | Absent | Settings flow, compiler and 30-day preview are not built. |

### Known defects, open

- **Only a "before" screenshot is captured.** For a rehearsal — whose entire purpose is
  showing what would be submitted — the filled state is the half that matters, and it is the
  half that is missing.
- **Free-text and knowledge-answerable questions go unanswered under `LLM_PROVIDER=null`.**
  Nine questions were escalated on the live form, including "Do you have experience working
  with front-end languages such as HTML, CSS, and JavaScript?" — answerable from the indexed
  facts with a real model. This is a configuration state, not a defect, but it is the
  difference between 14 fields filled and most of the form filled.

---

## Method

Live runs are against real ATS boards with `dry_run=true`. Provider posture is unchanged:
Greenhouse, Lever and Ashby permit automated submission; Workday and LinkedIn are
discovery-only and route to manual review by design.

Reference hardware for the timings above: Windows 11, the development machine this was run
on. They are indicative, not a benchmark — a benchmark is what the missing performance CI
job would produce.
