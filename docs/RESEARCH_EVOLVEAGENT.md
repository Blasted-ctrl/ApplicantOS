# Research: evolveagent-ai — findings and adoption plan

Four parallel research agents read the full clone of
[`manit0700/evolveagent-ai`](https://github.com/manit0700/evolveagent-ai) — 431 Python files,
~70k LOC backend, 809 route handlers — against our own codebase. This is the distilled result:
what we take, what we refuse, and the gaps the comparison exposed **in our own code**.

Source repo tagline: *"Local-first, governance-first AI operating system for turning goals into
planned, approved, verified work."* Shape is strikingly convergent with ours — FastAPI + React/Vite
+ pgvector + Tauri, organised around `goal → plan → approval → execution → verification → memory`.

---

## Executive summary

**The repo is breadth-first demo software with a small, real engineering core buried inside it.**

The tell is in the commit history: in the 13-day window visible in the clone, non-merge commits
break down as **37 `feat:` · 2 `fix:` · 1 `test:` · 1 `docs:`** — zero refactors, zero deletions.
That is a pure feature-accretion loop, and it explains `os_routes.py` sitting beside
`os2_routes.py`, 18,011 lines of dead frontend never deleted, and six mutually contradictory
"current version" claims across its own docs.

Concretely, the things named after intelligence are not intelligent:

| Component | What it actually is |
|---|---|
| The planner | A keyword classifier over five hardcoded templates. No LLM. No prompt exists. |
| The task DAG | `depends_on` has **2 writers and 0 readers**. Never topologically sorted. |
| The LLM judge | Never calls an LLM. `score = 75`, `+5` per structural checkbox, capped at 95. Cannot fail anything. |
| The eval harness | Scores the stored `expected_keywords` against the stored `reference_answer`. Never invokes the system under test. `regressed` is structurally always `False`. |
| The durable workflow engine | `step["output"] = f"[simulated] {step['name']} completed"` |
| Cost tracking | Flat `$0.002` per call. `response.usage` is discarded. |
| Prompt versioning | Complete propose/activate/rollback system. **Zero runtime readers.** |

To its credit, the code comments are frequently honest about this (*"Mock simulation — no real LLM
call"*). The overstatement lives in the naming and the docs.

**On prompting specifically, we are not behind — we are far ahead.** Their longest system prompt is
~45 words. Zero of their prompts contain an output schema; zero contain a worked example. Ours
average 100+ lines with schema, worked examples, negative examples, refusal design, and a
confidence calibration ladder. **There is nothing to take from their prompt text.**

---

## What we adopt

Ranked by value per unit cost. Nothing here is a prompt.

### 1. Untrusted-text chokepoint — **already spec'd, see [CONTRACTS §10b](CONTRACTS.md)**

Their one genuinely good safety idea: a single function every externally-sourced string passes
through before it can reach a prompt, which on high risk **returns empty and escalates** rather
than sanitising and continuing.

This closes a real hole in our design. Job descriptions are attacker-controlled text piped into
`ResumeEngine.tailor`, `CoverLetterWriter.write` and `FieldAnswerer.answer`. Our fact-id validator
already defeats the fabrication half of that attack on resumes — an invented degree has no
`KnowledgeFact` behind it — but **`FieldAnswerer` emits free text with no equivalent backstop**.

Do not copy their detector: a 13-entry substring table that catches nothing an adversary would
write. Detection must be structural. **Cost: ~1 day.**

### 2. Cross-provider runtime fallback — with their bug fixed

`LLMRouter.generate_with_route()` attempts a deduplicated provider chain on failure. We have no
equivalent: `get_llm()` resolves one provider at config time, so a mid-run Anthropic outage raises
`LLMError` out of the pipeline and the run dies.

**Their fatal flaw, which we must not copy:** on total failure it returns `success=True` carrying
invented prose from a mock provider. In a product that submits text under the user's name, that is
a correctness bug with real-world consequences. Our terminal fallback must stay `NullModel`
(schema-valid, prompt-grounded, deterministic) and carry a `degraded=True` flag the UI surfaces.

Chain only on non-transient provider errors — `classify_error()` already distinguishes these.
Never chain on `LLMParseError` (a schema problem follows you to the next provider) or
`TokenBudgetExceeded`. **Cost: ~120 LOC.**

### 3. Retrieval rejection trace — highest value per line

Their `SmartContextService.plan()` emits a reason for every *excluded* candidate:
`"over context budget"`, `"no keyword overlap"`, `"duplicate removed"`.

Today, when a user asks *"why isn't my Robotics Team bullet on this resume?"*, we cannot answer.
`ResumeEngine.prefilter()` ranks by `0.5·similarity + 0.3·keyword + 0.2·impact` and silently
discards the rest — **we already compute every number needed for the explanation and throw it
away.** This is also the single best tool for improving prompts, because it distinguishes a prompt
failure from a retrieval failure. **Cost: ~50 LOC, mostly plumbing.**

### 4. Fact-id self-consistency on the resume path

They fan out N completions and pick the most representative by pairwise Jaccard over *prose* — a
weak signal. **Our version would be strictly better: compare `selected_fact_ids` sets.** That is
discrete, exact, and semantically meaningful. Three samples agreeing on the fact set means the
selection is stable; divergence means the retrieval set is ambiguous and the user should look.

A real uncertainty signal for our highest-stakes artifact, with no LLM judge required. Gate behind
an explicit user action — it is 3× tokens. **Cost: ~80 LOC.**

### 5. Per-section prompt budget

Recommended *because of what their absence causes*, not because they solved it — they have no
tokenizer anywhere and no truncation policy, so oversized prompts fail at the provider.

`DEFAULT_PREFILTER_TOP_K = 60` caps facts by **count, not tokens**. A user with 60 verbose facts
and a 4,000-word posting has no guard. `TokenBudget` is a daily spend cap, not a per-prompt sizer.
Drop order: lowest-composite-score facts first (already ranked), then truncate the posting body,
never the schema or hard rules. **Do this before #4** — self-consistency on an oversized prompt
just fails three times in parallel. **Cost: ~100 LOC.**

### 6. PII screen before context insertion

They screen memories for emails, card-like numbers, `sk-`/`ghp_`/`AKIA` tokens and
`password:` assignments *before* injection. **This is the one place their safety thinking exceeds
ours** — and it matters more here than there. See gap #2 below. **Cost: ~40 LOC.**

### 7. Prompt hashing, not a versioning subsystem

Their `PromptVersionService` is a complete propose/activate/rollback system with a REST surface and
a settings panel, and **nothing reads it** — activating a version changes nothing at runtime.

The lesson: prompt versioning is worthless unless the *load path* reads it. For us that is ~60 LOC:
`load_prompt()` records a `blake2b` hash of the text actually used, stamped onto the
`ResumeVersion`. That hash answers *"did output quality change because I edited the prompt?"*
retroactively, which is what versioning is for. Git already versions the markdown.
**Do not build** the workflow, the REST surface, or the UI.

### 8. Operational tooling worth copying outright

- **`scripts/smoke_test.py`** — a stdlib-only declarative
  `(area, method, path, body, expected_status)` table plus hand-written cross-service *flows*.
  Unit tests will not catch a router that was never `include_router`'d or a Celery task registered
  on the wrong queue. Theirs caught exactly that. **Run it in CI** — their biggest tooling miss was
  not doing so.
- **CI with a real pgvector service container + dual-backend parity tests.** We mandate `GUID` /
  `JSONType` / `EmbeddingType` behaving differently on Postgres vs SQLite, and three `VectorStore`
  implementations. Two backends and three stores without parity tests is guaranteed divergence.
- **`dev-with-backend.mjs`** — health-check first so it reuses an already-running backend, 60×500ms
  readiness poll, backend-exit kills the frontend, clean SIGINT/SIGTERM. The dev-mode half of the
  orphaned-sidecar problem [CONTRACTS §18](CONTRACTS.md) already flags. Needs Windows paths.
- **Archive-then-delete retention, dry-run by default**, with a minimum-age floor. `cleanup.*` in
  CONTRACTS §15 are currently bare task names with no policy behind them.
- **Recorded declines, never silent no-ops** — see gap #1.
- **The approve/reject consequence pair** — before any decision, render what happens if you approve
  and what happens if you reject. `/reviews` is our highest-stakes screen and currently states the
  *reason* but never the *consequence* of either branch. **Cost: ~3 hours.**
- **Scoped work orders with 🚫 do-not-touch file lists.** The missing layer between our binding
  contract and a parallel agent's diff — and exactly how you stop agents colliding.

---

## Gaps this exposed in *our* code

The research agents audited us as hard as them. Three real findings:

### Gap 1 — Blocked submissions leave no durable record
`Pipeline.submit` guards 2 (daily cap) and 3 (score floor) return via `_result(...)`, which is a
**pure constructor** — no persistence. Neither writes an `ApplicationEvent`. `docs/SAFETY.md`
promises *"if a company says they never received it, you have proof"*, but the mirror question —
*"why did you never apply to this one?"* — leaves only a log line that rotates.
`Application.add_event()` already exists. **~2 hours.**

### Gap 2 — Memory will carry PII into prompts the moment it is wired
`MemoryStore.as_prompt_context` is fully implemented — 600-token budget, graceful truncation — and
has **zero call sites**. Memory is recorded, embedded, ranked, attached to `RetrievalResult`, and
dropped on the floor. `reinforce()` likewise has zero callers.

But wiring it naively is unsafe: our only memory writer is `ReviewService._remember`, which stores
**the human's literal answer to a form field**. A reviewer typing an SSN, a DOB or a salary creates
a `MemoryEntry` whose text is that value. Our `redact_secrets` processor scrubs values as well as
key names, but it is *log-scoped* and its value patterns target addresses, credentials and opaque
blobs — it would not catch a bare SSN or salary in a memory body, and it does not run on prompts at
all. **Screen before injecting, not after.**

Also: inject memories into the **system** prompt as style/preference constraints only, never into
`$facts` — golden rule 7 requires every bullet to trace to a `KnowledgeFact.id`, and the resume
validators must stay authoritative over anything a memory suggests. `field_answer.py` is the safer
first target.

### Gap 3 — `ApplicationVerifier` has no caller
It is better designed than anything in their repo — four independent signals, error markers checked
first, confirmation-ID stopword validation, explicitly asymmetric verdict. It just has no entry
point yet. The gap is wiring, not design. **Do not build a second verifier.**

---

## What we explicitly refuse

| Thing | Why |
|---|---|
| **Their prompts** | 1–5 sentences of role assertion. Zero output schemas, zero examples. Copying any of it is a downgrade. |
| **The task DAG** | Doesn't exist there (2 writers, 0 readers), and we don't need one — an application is a fixed linear sequence. A generic DAG buys a scheduler, a topological sort, cycle detection, and a new failure surface to express five `await`s. |
| **The agent registry** | Four hardcoded classes that *all run on every task*; `find_capable` is substring matching with an alphabetical tiebreak over a tool vocabulary that doesn't intersect the real one. `PluginMeta.capabilities` is already better. |
| **The 4-specialist fan-out** | 6 LLM calls per request sharing a blackboard that is literally a growing string, quadratic in output length, with no measured quality gain. |
| **The LLM judge** | Scores by output length and whether the text contains `##`. Our `<0.35` token-overlap revert is a real deterministic gate that does strictly more. |
| **Their eval harness** | Grades the answer key against itself. Name the anti-pattern so we don't reinvent it: **the harness must call the model.** |
| **Risk tiers** | Three competing tier systems with four incompatible numeric scales, none of which decides anything a simpler check couldn't. `ReviewReason` already encodes *why*; the guard ladder encodes *how severely*. A tier column is a redundant third axis. |
| **Mock data seeded into live UI state** | Their frontend initialises from `INITIAL_APPROVALS` and fabricates audit records **in the browser**. In a job-application tool this would be catastrophic. `docs/UI.md` P6 already forbids it. |
| **Hand-maintained router registration** | 89 imports + 89 `include_router` calls in `main.py`. `create_app()` should walk `app/api/routes/`. |
| **Denylist risk classification** | `RISKY_ACTIONS = ["send","email","pay",...]` substring-matched against free text is fail-open, and flags "postpone" while missing "submit". Our taxonomy escalates on *unknown* — correct polarity, keep it. |
| **Building an atomic primitive and not migrating call sites** | They wrote `update_list` to fix lost updates, then adopted it in 12 of 156 services; the racy `write_list` is still called 124 times, including in both safety-critical paths. If we add a concurrency-safe helper, the same PR migrates every caller. |

---

## Where our design is already clearly ahead

| Dimension | Them | Us |
|---|---|---|
| Data model | 222 schemaless JSON collections → one JSONB blob table | 22 tables, FKs, unique constraints, verified migration |
| Realtime | **None** — manual refresh buttons | `GET /ws`, typed events → `setQueryData` |
| Prompts | ~45 words max, 0 schemas, 0 examples | 100+ lines, schema + worked + negative examples, calibration ladder |
| Structured output | Impossible — the interface is `str → str` | Forced tool call / JSON mode + repair retry |
| Token accounting | `"units": 1` per call; `response.usage` discarded | Real `input_tokens`/`output_tokens` + enforced daily budget |
| Verification | Judge that cannot score below 75 | 4 independent signals, asymmetric verdict |
| Secret redaction | 7 content regexes on user input | Recursive key **and** value processor + `show_locals=False` |
| Memory | No expiry, no delete path, hash-bucket embeddings | Half-life weighting, dedup-reinforcement, real embedder |
| Mock mode | 51 lines of canned prose, returns `success=True` on outage | Schema-walking, prompt-grounded `NullModel` |
| Type safety | **No `tsconfig.json` exists** | TS5 strict, enums mirrored from `enums.py` |
| Desktop | Tauri shell, no sidecar, macOS-only, dev-only | Tauri v2 + PyInstaller sidecar, dynamic port, health-gated |

---

## Adoption order

1. **Now (Phase 5 follow-up):** untrusted-text chokepoint · blocked-submission events
2. **Phase 6:** `CheckpointService` (golden rule 8 currently has a schema and no runtime) ·
   wire `ApplicationVerifier` · wire memory with the PII screen · smoke test · retention policy
3. **Phase 7:** approve/reject consequence pair · latency-phase escalation for 30–90s jobs
4. **Phase 8:** eval harness (build ours, copy nothing) · CI parity tests · prompt hashing ·
   cross-provider fallback · rejection trace · prompt budget

**Total genuinely worth building: roughly 5–6 days.** Everything else on their 809-endpoint surface
is either already better here or shouldn't exist in a single-user desktop app.
