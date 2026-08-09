# The ApplicantOS test suite

This tool submits job applications under someone's name. That makes a whole class of bugs
*unrecoverable* — you cannot unsend an application — so the suite is organised around the ten
golden rules first and around code coverage second. A file named `test_golden_*.py` is not a
unit test of a module; it is an executable statement of a promise the product makes to its user.

```bash
pytest                                  # everything
pytest tests/test_golden_kill_switch.py # the single most important file
pytest -m "not integration"             # skip anything needing a real browser/DB/broker
pytest --cov=app --cov-report=term      # with coverage
python -m scripts.smoke_test --start    # end-to-end against a real backend
```

No services are required. The suite runs on SQLite in memory, the null model, the hashing
embedder and an in-memory vector store — the same zero-dependency configuration `CLAUDE.md`
documents — and `tests/conftest.py` pins that configuration into `os.environ` before anything
imports `app`.

---

## The golden-rule tests

Nine files, one per rule. Each asserts the *observable behaviour* the rule promises, not the
shape of the code that implements it.

| File | Rule | The load-bearing assertion |
|---|---|---|
| `test_golden_never_apply_twice.py` | 1 | A spy provider's `apply()` is never reached for a post-submit application. The `UNIQUE(user_id, posting_id)` constraint raises. |
| `test_golden_never_guess.py` | 2 | On every escalation path, `fake_page.fills == []`. Nothing is typed on the way to the review queue. |
| `test_golden_kill_switch.py` | 3 | `fake_page.clicks == []` **and** `fake_page.lookups == []` across all four switch combinations. |
| `test_golden_redaction.py` | 4 | A secret in a **traceback frame local** does not reach the log — asserted against the bytes the configured pipeline writes. |
| `test_golden_plugin_isolation.py` | 5 | Static `ast` scan: no concrete provider/analyzer/model/template imported outside its package. |
| `test_golden_cleanup.py` | 6 | The render is deleted; `content_json` is byte-for-byte unchanged. |
| `test_golden_no_fabrication.py` | 7 | Four crafted adversarial model replies, four guards firing. |
| `test_golden_resumable.py` | 8 | A `CancelledError` marks the checkpoint failed and parks the application in review — never leaves it `submitting`. |
| `test_golden_tos.py` | 9, 10 | LinkedIn and Workday refuse to apply; no credentialed request is constructed in `app/jobs/linkedin.py`. |

### Why the assertions look the way they do

**Assert on what happened, not on what was returned.** A function can `return False` and still
have clicked something on the way. `docs/SAFETY.md` promises that `AutoFiller.submit` returns
without *touching the DOM*, so the test asserts against a recorded action log. This is why
`tests/fakes.py` exists at all.

**Every "it refuses" test has an "it works" twin.** `test_submit_never_clicks_unless_both_switches_are_open`
is worthless on its own — a `submit` hard-coded to `return False` would pass it. So
`test_submit_clicks_only_when_both_switches_are_open` asserts the permitted path really clicks.
The same pattern appears in the never-guess file (a confident answer *is* filled) and the
no-fabrication file (a faithful rewrite *is* kept).

**Structural rules are tested structurally.** Plugin isolation and the LinkedIn credential ban
are properties of the source tree, so they are checked by parsing the source tree with `ast`
rather than by convention or code review. Parsing rather than grepping matters: a module path
inside a docstring, a commented-out import, and the plugin loader's own
`importlib.import_module(name)` are all correctly ignored.

---

## Subsystem tests

| File | What it covers |
|---|---|
| `test_dedupe.py` | Tracking-parameter stripping, the provider-scoped identity key, cross-provider collapse as a *judgement*, near-duplicate thresholds |
| `test_scoring.py` | The canonical 70 from `scoring_rules.yaml`'s own header, determinism over 100 runs, word boundaries, the hard-negative lock |
| `test_resume_tailor.py` | Prefiltering, the cache key, the bullet budget, and the degradation contract (every model failure yields a document) |
| `test_documents.py` | `escape_latex` round-trips, the one-page shrink ladder, `build_key` traversal safety |
| `test_knowledge.py` | index → skip → force, with **no doubling** on any re-index |
| `test_tracking.py` | The classifier corpus, relay-domain matching, idempotent re-sync, the read-only privacy invariants |
| `test_api.py` | Health/ready/metrics, pagination, filters, 404s, the review resolve flow, and that `GET /settings` returns no credential |
| `test_models.py` | Every unique constraint raises; enum wire format; the RESTRICT that protects rule #1 |
| `test_apply_driver.py` | One whole application attempt on a recording session: the verification verdict mapped onto `CONFIRMED` / `FAILED` / `VERIFICATION_FAILED`, file-input reconciliation, and that a dry run neither clicks nor verifies |

---

## The fake strategy

Everything lives in `tests/fakes.py`. Three principles:

**Doubles record, they do not merely answer.** `FakePage` keeps an ordered `actions` log with
views over it — `clicks`, `fills`, `checks`, `uploads`, `lookups`, `writes`. Safety assertions
are made against that log.

**And the page moves.** `PageTransition` is what the page becomes on the first click, which is
what lets one test tell a confirmation from an error from silence: `AutoFiller.submit` reads the
page to see whether a marker appeared and `ApplicationVerifier.verify` reads it again to decide
whether the employer really has the application, so a frozen page could not distinguish them.
`form_control` / `discovery_payload` build the raw descriptors `DISCOVERY_SCRIPT` emits, which is
faithful because the script does DOM access only — every policy decision is made in Python.

**No network, no browser, no external process.** `FakePage` and `FakeSession` are duck-typed
against the surface `docs/CONTRACTS.md` §12 defines, so `app/browser/autofill.py` — which never
imports Playwright — is fully testable on a machine with no browser. `MockRouter` wraps
`httpx.MockTransport` and **raises on an unmocked request**, so a test cannot silently reach the
real internet and then fail in CI for the wrong reason.

**Fakes are shared; stubs are local.** Anything reusable is a fixture in `conftest.py`. Anything
that exists to defeat one specific guard — an over-eager model returning `adjustment: 100`, an
analyzer whose fingerprint the test controls — is defined in the file that uses it, next to the
assertion it serves.

### Fixtures

| Fixture | What it gives you |
|---|---|
| `settings` | The real settings singleton, pointed at `tmp_path`, both switches closed |
| `submission_allowed` | The same, with **both** switches open — the only configuration that may submit |
| `engine` / `session` | A private in-memory SQLite database with the full schema, dropped after the test |
| `user` / `company` / `posting` / `application` | Persisted rows |
| `make_posting` / `make_application` / `make_score` | Factories, each call producing distinct identifiers |
| `master_facts` | Four realistic `KnowledgeFact` rows — the only material a resume may be built from |
| `null_llm` | A `RecordingLLM` that replays canned replies and records every prompt |
| `fake_page` / `fake_browser` | The recording browser doubles |
| `fake_storage` | An in-memory `StorageBackend` that records deletions |
| `mock_http` | An `httpx.MockTransport` router |
| `api_client` | `httpx.AsyncClient` over `create_app()` with the session dependency overridden |

`make_score` deserves a note: `Pipeline.submit`'s guard ladder refuses an unscored posting
outright ("refusing to apply blind"), so any test aiming at a *later* guard has to get past that
one first.

---

## What is **not** covered

Stated plainly, because a coverage number that quietly excludes the risky parts is worse than no
number at all.

- **No real browser — in the default run.** Here `app/browser/playwright_runner.py` is exercised
  only through its duck-typed double, and `AutoFiller`'s policy logic — which label wins, what
  counts as an essay, what may be clicked — is tested directly, because it was deliberately
  written to be reachable without a browser. Field discovery, blocker detection, résumé upload
  and the kill switch *against real Greenhouse, Lever and Ashby markup* live in
  `tests/integration/test_browser_live.py` (`-m integration`, nightly). The submit-verification
  polling loop after a real click remains untested and cannot honestly be tested: confirming it
  would mean submitting an application to a real employer.
- **No real ATS — in the default run.** Provider `search`/`fetch_posting` parsing runs against
  recorded payloads here and against the live feeds in
  `tests/integration/test_providers_live.py`. Their *auto-apply posture* is checked hermetically
  (`test_golden_tos.py`).
- **No PostgreSQL.** Everything runs on SQLite. Two consequences are real: the `use_alter`
  foreign keys between `applications` and the document tables are silently omitted on SQLite
  (`docs/OPEN_QUESTIONS.md` item 12), and `pgvector` behaviour is never exercised.
- **No Celery broker.** Task *registration and queue routing* are checked by
  `scripts/smoke_test.py`; actual execution, retries and beat scheduling are not.
- **No real mailbox.** The tracking classifier and matcher are tested on synthetic messages;
  the IMAP/Gmail/Graph adapters are checked only for the read-only invariants, statically.
- **No LaTeX toolchain.** `escape_latex` and the shrink ladder are unit-tested; no PDF is
  actually typeset, so page counting is not exercised end to end.
- **No desktop app.** `desktop/` has no tests here.

The `integration` marker is declared in `pyproject.toml` for exactly this purpose, and
`pyproject.toml` deselects it by default: the suite was built so that every check runs with no
database server, no broker, no browser and no network. That is deliberate — a suite that only
runs on a fully provisioned machine is a suite that stops being run. Any test that genuinely
needs one of those services carries `@pytest.mark.integration`, so plain `pytest` stays the
command that works everywhere.

Two modules use it today, both under `tests/integration/` and both run nightly by
`.github/workflows/integration.yml`:

| Module | Needs | What it proves the hermetic suite cannot |
|---|---|---|
| `test_providers_live.py` | network | The four discovery feeds still have the shape the parsers expect |
| `test_browser_live.py` | network + Chromium | The selector packs still match real Greenhouse, Lever and Ashby application forms — and the kill switch holds on one |

Neither may change anything at anybody's end. `test_browser_live.py` in particular forces both
safety switches at import time, re-asserts them against the live settings object, installs a
capture-phase click recorder in every page before it navigates, and asserts that each session
recorded **zero clicks of any kind**.

Two behaviours are asserted **as they are** rather than as they ideally should be, so that a
future change to them is a deliberate decision rather than a silent one. Both are labelled in
place: `test_a_dotted_legal_suffix_does_not_collapse` (`L.L.C.` and `LLC` produce two company
rows — the legal-suffix set is frozen and changing it is a data migration), and the fingerprint
probe memo in `test_knowledge.py`.

---

## `scripts/smoke_test.py`

Standard-library only, so it can run against a *deployed* backend with nothing installed beside
it. It catches the class of failure no unit test can: a router that was never included, a Celery
task registered on the wrong queue, two services that each work and disagree with each other.

```bash
python -m scripts.smoke_test --start          # start a backend, test it, stop it
python -m scripts.smoke_test --base-url http://localhost:8000
python -m scripts.smoke_test --skip-flows     # the endpoint table only
```

A declarative `(area, method, path, body, expected_status)` table covers every route in §14,
plus hand-written flows: discover → score → prepare → dry-run submit, review resolve, index →
retrieve, and tracking signal → status. It prints a pass/fail table and exits 1 on failure.

The apply flow asserts that the pipeline **did not submit** — with both switches at their
defaults, a refusal is the correct outcome, and a smoke test reporting success there would be
reporting a safety failure.
