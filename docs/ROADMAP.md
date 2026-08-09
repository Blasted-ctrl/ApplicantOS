# Roadmap

Where ApplicantOS actually is, and where it goes next.

Status markers follow [`WORKING_AGREEMENT.md`](WORKING_AGREEMENT.md) §8 and are derived from what
is in the tree, not from what was planned:

**✅ implemented** · **🟡 partial** · **⬜ not built**

Everything below was checked against the repository on 2026-08-08.

---

## Part 1 — The twelve items that make it complete

### 1. Foundation ✅

Config (87 settings), structlog with permanent secret redaction, async SQLAlchemy 2.0, portable
column types, 24 tables, the migration, the cache protocol with four backends, and one plugin
registry for six plugin kinds.

Both database backends work. `alembic upgrade head` round-trips on SQLite and PostgreSQL, and the
CI matrix runs the whole suite against both because the zero-infrastructure path is a hard
requirement, not a fallback.

### 2. Knowledge engine ✅

Six analyzers (GitHub, website, project folder, resume parser, LinkedIn export, generic document),
the indexer with cheap `fingerprint()` change detection, three vector-store backends, the entity
graph, the fact store, four-signal hybrid retrieval fused by reciprocal rank, and the AI memory.

The pure-Python in-memory vector store means this works with no pgvector, no sqlite-vec and no
API key.

### 3. Job discovery ✅

Five ATS providers — Greenhouse, Lever, Ashby, Workday, LinkedIn — plus deduplication by canonical
URL, content hash and `dedupe_key`.

Postures are honest and enforced: Greenhouse, Lever and Ashby support real submission; Workday and
LinkedIn raise `UnsupportedFlowError` and route to manual review. `tests/test_golden_tos.py`
checks that the flags match the docstrings.

### 4. Scoring ✅

37-rule deterministic pack plus seven preference gates, an optional bounded `±10` model
adjustment, and the explicit hard-negative lock. The canonical worked example reproduces exactly
70 → `apply`, verified by `tests/test_scoring.py` and by the command in
[`SCORING.md`](SCORING.md) §6.

### 5. Document generation ✅

LaTeX (`modern`, `classic`), DOCX (`ats_plain`), HTML (`web`) and Markdown templates behind one
`TemplatePlugin` interface, with the four-rung one-page shrink loop and an import-time check that
the readability floor was not edited away.

🟡 **One honest caveat:** the LaTeX path needs `tectonic` installed. Without it the renderer
silently falls back down the engine chain to HTML. That fallback is correct behaviour, but it
means "PDF rendering works" is true on a machine with LaTeX and produces a different-looking
document on one without.

### 6. Resume tailoring and cover letters ✅

The resume engine as a view over the knowledge graph, with all six validation guards — id
membership, rewrite fidelity, metric support, header authority, skills grounding, summary
grounding — and a deterministic LLM-free fallback on any model failure. Cover-letter writer with
a four-value policy. Field answering with a calibrated confidence and an EEO path that never
consults a model.

### 7. Browser automation and submission ✅ built · 🟡 unproven against live ATS forms

`BrowserSession`, `FieldResolver`, `AutoFiller`, `ApplicationVerifier`, `ArtifactRecorder`, and
per-provider `SelectorPack`s. The kill switch, the confidence floor, the essay ceiling, blocker
detection, before/after screenshots and the submit-control whitelist are all implemented and
covered by tests using a duck-typed fake `Page`.

**What is not proven:** no automated end-to-end submission against a live Greenhouse, Lever or
Ashby form is recorded in this repository. The selector packs were written from the published
markup of each ATS, and ATS markup rots. This is the single largest gap between "the code is
correct" and "it works on your machine today" — expect the first real run to need selector
adjustments, and read the artifact screenshots when it escalates.

### 8. REST API, WebSocket events and workers ✅

14 endpoint modules under `/api/v1`, 18 typed WebSocket events whose payloads are the REST schemas,
five Celery queues, the beat schedule, the three-way retry classification, and the async bridge.

A missing broker degrades to `202 {degraded: true}` rather than a 500, so the API is usable with
no Redis at all.

### 9. Application status sync ✅ built · 🟡 mailbox connection untested end-to-end

`StatusClassifier` (deterministic rules first, LLM only below 0.7 confidence), `SignalMatcher`,
`StatusSyncService`, the four `sync.*` tasks, the six tracking endpoints, and three mailbox
backends: Gmail, Outlook and generic IMAP.

**Gaps:**
- ⬜ `app/tracking/trackers/portal_tracker.py` is specified in `CONTRACTS.md` §17 and **does not
  exist**. Only `email_tracker.py` is implemented. Email is the channel that covers every ATS, so
  this is a nice-to-have, but the contract lists it.
- 🟡 The OAuth flows for Gmail and Outlook are implemented but have not been exercised against a
  real account in this tree. The privacy invariants — read-only scopes, no full-mailbox sweep,
  500-character snippets, credentials in the OS keychain — are implemented and greppable.

### 10. Desktop app ✅

Tauri v2 shell (7 Rust modules, ~1,900 lines) hosting a React 19 renderer (113 TypeScript files,
~24,700 lines) across 14 routes, with the sidecar lifecycle, the two-layer cache persistence, the
WebSocket-to-`setQueryData` bridge, optimistic mutations, virtualized lists, a command palette and
the full component inventory from [`UI.md`](UI.md).

`npm run typecheck` and `npm run lint` both pass clean.

🟡 **The performance budget in `UI.md` §10.14 is specified and CI-gated in intent, but the
Playwright + CDP trace job that enforces it is not in `.github/workflows/ci.yml`.** The budgets are
therefore design targets that have not been measured on the reference machine.

### 11. Prompt-injection defence ⬜ **not built**

`CONTRACTS.md` §10b specifies `app/ai/untrusted.py` — `sanitize_external_text()`, an
`InjectionRisk` enum, an `InjectionVerdict`, and a chokepoint every externally-sourced string
passes through before it can reach a prompt.

**The module does not exist.**

This is the most significant known gap in the tree, and it is a security gap rather than a feature
gap. Job descriptions and crawled portfolio pages are attacker-controlled text that flows directly
into `ResumeEngine.tailor`, `CoverLetterWriter.write` and `FieldAnswerer.answer`.

| Surface | Currently protected? | By what |
|---|---|---|
| Resume bullets | 🟡 incidentally | The fact-id validator — an invented degree has no `KnowledgeFact` |
| Resume header fields | ✅ | Copied from facts, never from the model |
| Cover letter body | ⬜ | Nothing |
| Form free-text answers | ⬜ | Nothing — and this one writes into a real form |
| Crawled website content at extraction | ⬜ | Nothing |

**How it slots in.** `app/tracking/classifier.py` already implements the right pattern for email
bodies: an explicit `<untrusted_email>` fence the model is told about by name, plus a system prompt
stating the boundary. The work is (a) lift that into `app/ai/untrusted.py` as a reusable
chokepoint, (b) add structural detection — instruction-shaped verbs addressed at a model, role-marker
injection, base64/hex blobs, invisible and bidi characters, abnormal instruction density, and any
claim about *the candidate* appearing in a *job posting* — each a named constant with a weight,
(c) call it at the five mandatory sites, and (d) route `HIGH` to `NEEDS_REVIEW` with
`ReviewReason.POLICY_BLOCK`.

The metric that decides whether it ships is **the false-positive rate on genuine postings**. A
defence that flags normal postings gets switched off by the user and protects nothing.

### 12. Packaging and release 🟡

[`PACKAGING.md`](PACKAGING.md) documents the full path: PyInstaller freezes the backend into a
sidecar, Tauri bundles it, and the three platforms sign differently (Authenticode, Developer ID
plus notarization, detached signatures).

**Built:** the PyInstaller spec (`desktop/src-tauri/sidecar/build/applicantos-server.spec`) and a
Windows sidecar binary (`applicantos-server-x86_64-pc-windows-msvc.exe`).

**Not built:** macOS and Linux sidecar binaries, any signed installer, the updater endpoint, and
the release CI job. The `desktop` CI job typechecks, lints and builds the renderer; it does not
produce a bundle.

### Quality gates — honest state

| Gate | Status |
|---|---|
| `pytest` | ✅ **475 passed** in 64s on the zero-dependency stack |
| `python -m compileall app` | ✅ clean |
| `npm run typecheck` (desktop) | ✅ clean |
| `npm run lint` (desktop) | ✅ clean, zero warnings |
| `ruff check .` | 🟡 **31 findings** — line length (14), `RUF005` iterable-unpacking style (4), `RUF046` redundant `int()` (3), `SIM10x` simplifications (4), plus `F401`, `N805`, `N811`, `UP042`, `RUF007`. All cosmetic; none is a defect |
| `mypy app` | 🟡 **66 errors across 33 files** of 170 checked. Mostly variance and `Any`-narrowing complaints at ORM and plugin boundaries |

Neither of the two amber gates affects runtime behaviour, and both are mechanical to clear. They
are listed here rather than quietly omitted because a repository that claims a clean gate it does
not have is worse than one that says which gate is dirty.

---

## Part 2 — What comes next

Four features, in the order they earn their place. Each is described with enough of its shape to
show it fits the existing architecture rather than requiring a rewrite.

### Natural-language policy rules

**The problem.** The scoring pack is powerful and nobody wants to write YAML. "Don't apply to
anything requiring more than 3 years" and "skip crypto companies" are one sentence each and
currently take a rule authoring session.

**The shape.** A new `app/ai/policy.py` that compiles a natural-language sentence into a
`ScoreRule` or a preference gate, and shows the user the compiled rule before saving it.

```
"skip anything wanting more than 3 years"
   → ScoreRule(key="policy_max_experience", label="Requires more than 3 years",
               points=-1000, field="text",
               regex=r'\b([4-9]|[1-9][0-9])\s*\+?\s*(?:years|yrs)\b')
```

**Why it fits.** The rule pack is already data, already user-editable through
`PUT /settings/scoring-rules`, and already validated at load with an error naming the offending
rule. Compilation is a pure function from a sentence to a `ScoreRule` — the engine does not change
at all, and `score_rules()` stays deterministic because what the model produced was a *rule*, not
a *score*.

**The design constraint.** The compiled rule is shown, not applied silently. A user who cannot see
what their sentence became cannot trust it, and a mis-compiled policy silently discarding good
jobs is invisible by construction.

**New surface:** `POST /api/v1/settings/scoring-rules/compile` → the candidate rule plus the
postings in the last 30 days it would have changed the verdict for. Preview before commit.

### Recruiter-email answering

**The problem.** The status sync already reads the mailbox and classifies it. When a recruiter
asks "are you still interested, and what's your availability?", the system knows the application,
the company, the posting and the user's calendar constraints — and does nothing.

**The shape.** Extend `SignalKind` with the cases that carry a question, and add
`app/tracking/responder.py` that drafts a reply grounded in the same knowledge graph that writes
the cover letters.

**Why it fits.** `StatusSignal` already binds an email to an `Application`, so the draft has full
context for free. `FieldAnswerer`'s resolution ladder — explicit answers, then known profile
fields, then the model with a calibrated confidence, then give up — is exactly the right shape for
a reply.

**The design constraint, and it is absolute.** **Drafts only, and the mailbox stays read-only.**
`CONTRACTS.md` §17.8.1 states that the code contains no send, delete, move or flag-modifying call
and that this is verifiable by grep. Sending would break that invariant and turn a read-only
integration into one that can act as the user. A draft the user reviews and sends themselves keeps
the guarantee intact and delivers most of the value.

**New surface:** `GET /api/v1/tracking/signals/{id}/draft`, plus a reply pane in the desktop app.

### Interview scheduling

**The problem.** `interview_invite` is already a classified `SignalKind`. Turning it into a
calendar entry is manual.

**The shape.** Parse proposed times out of the invitation, check them against the user's
availability, and produce a draft reply proposing a slot — plus an `.ics` file. Availability comes
from a new `UserProfile` field, not from reading the user's calendar.

**Why it fits.** Classification exists; matching to an application exists; the drafting mechanism
is shared with recruiter-email answering. This is mostly date parsing across the many ways humans
write times.

**The design constraint.** No calendar write access, and no automatic acceptance. Same reasoning
as above: the system proposes, the user commits. A double-booked interview is a worse failure than
a manual copy-paste.

### Rejection analysis

**The problem.** The system accumulates outcomes — rejections, interviews, ghostings — against
postings whose full text and scores it retained, and against the exact resume that was submitted
(`ResumeVersion.content_json` plus `fact_ids`). It can already answer "what gets interviews?" and
does not yet answer "what should change?"

**The shape.** Extend `AnalyticsService` past the descriptive `what_gets_interviews()` into
comparative analysis: which facts appear on interviewing resumes and not on rejected ones, which
score components correlate with an outcome, which companies never respond, whether cover letters
change anything measurably.

**Why it fits.** Every input is already stored and already joined. `fact_ids` on
`ResumeVersion` is the key that makes fact-level analysis possible at all — it was retained for
provenance and turns out to be the analytics primitive too.

**The honest constraint, stated up front.** A job search produces tens to low hundreds of
applications. That is not enough data for statistical claims, and presenting a correlation from
n=40 as advice would be the same category of error as fabricating a resume bullet. So:

- Show counts, not p-values
- Never claim causation
- Suppress any comparison below a stated minimum n, and say so on screen
- Frame everything as "what happened", never "what to do"

The feature is a mirror, not an oracle. Building it as an oracle would be the most tempting way to
make this product dishonest.

---

## Part 3 — Smaller things worth doing

| | Item | Why |
|---|---|---|
| 🟡 | Clear the 31 `ruff` findings and the 66 `mypy` errors | A clean gate is the cheapest possible signal of care |
| ⬜ | Add the Playwright + CDP performance job to CI | `UI.md` §10.14 calls the budget CI-gated; it is not |
| ⬜ | Record a live-ATS submission test (dry run, headed) per supported provider | The only way to know a selector pack still matches |
| ⬜ | macOS and Linux sidecar builds | Two of three platforms cannot install today |
| ⬜ | Screenshots in the README | The product is visual and the README currently is not |
| ⬜ | `portal_tracker.py` | Specified in `CONTRACTS.md` §17, absent from the tree |
| 🟡 | A tectonic-free PDF path that looks as good | The LaTeX fallback silently changes the output |

---

## What is deliberately not on this roadmap

**LinkedIn scraping or automated LinkedIn applications.** Its terms prohibit both. Discovery stays
limited to a user-supplied export or a public feed, and `apply()` raises `UnsupportedFlowError`
permanently. If this ever looks like a bug worth fixing, it is not — see golden rule 10.

**Captcha solving.** A captcha is a site saying it wants a human. The correct response is to route
to manual review, which is what happens today.

**Bulk application volume.** The system applies at human speed and defaults to doing nothing. More
applications per hour is not the product.

**A hosted multi-tenant version.** The privacy posture — local storage, local vector store,
optional local LLM, credentials in the OS keychain — is a design commitment, not a limitation of
the current implementation.

---

## See also

- [`CONTRACTS.md`](CONTRACTS.md) — the binding specification, including §10b which item 11 owes
- [`OPEN_QUESTIONS.md`](OPEN_QUESTIONS.md) — every deviation from the contract, with reasoning
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — where each of these would live
- [`PACKAGING.md`](PACKAGING.md) — what item 12 still needs
