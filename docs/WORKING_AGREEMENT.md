# Working Agreement

How work gets done in this repository. These are operating rules for any agent or contributor
driving the build — human or AI. They exist because each one was learned the expensive way.

---

## 1. Never idle. Never delay unnecessarily.

**Rule:** If a background job is running, immediately pick up other non-conflicting work in the
same turn. Do not end a turn, and do not schedule a wait, purely to "let something finish."

Background workflows notify on completion. A scheduled wakeup is a **hang-guard**, never a reason
to stop working. Waiting has zero upside and costs real wall-clock time on a multi-phase build.

**In practice:**

| While this is running | Do this in parallel |
|---|---|
| A phase build workflow | Write docs, specs, or agent briefs for files it does not own |
| A verification pass | Prepare the next phase's workflow script |
| A long analyzer/index run | Review the previous phase's output, write tests |
| Anything at all | Never nothing |

**Only block** when the very next step is hard-dependent on the running job's output — e.g. you
cannot audit code that has not been written yet. Even then, find adjacent work first.

If a fallback wakeup is genuinely wanted, arm it *after* doing real work, never as the turn's
only action.

---

## 2. Waterfall by phase, commit at each boundary

The build proceeds in ordered phases. Each phase is:

1. **Build** — parallel agents against the frozen contract
2. **Verify** — mechanical audit (does it import, run, and pass its acceptance test?) plus an
   adversarial contract/safety audit
3. **Repair** — findings bucketed by area, fixed in parallel
4. **Commit** — one coherent commit per phase, with a message explaining *why*, not just *what*

Do not start phase N+1 until phase N passes its acceptance test. Do not commit a phase that
has not been independently verified.

---

## 3. The contract is frozen; the code moves toward it

[`docs/CONTRACTS.md`](CONTRACTS.md) is the single source of truth for every cross-module boundary,
and [`docs/UI.md`](UI.md) is the same for the desktop app's visual and interaction design.

Agents build in parallel against these files. That only works if nobody renames, "improves," or
restructures unilaterally. If a contract looks wrong: **implement it as written**, then record the
concern in [`docs/OPEN_QUESTIONS.md`](OPEN_QUESTIONS.md). Repairs always move code toward the
contract, never away from it.

---

## 4. Verification means running it, not reading it

A phase is not done because the code looks right. It is done because it was executed.

- Every Python file gets `python -m py_compile`.
- Every phase has an **acceptance test that must actually run**, with its real output reported.
- Agents may not report success they have not observed.
- Audits must construct a concrete failing scenario. "This looks fragile" is not a finding;
  "given input X, line Y persists a fabricated fact" is.
- Default to **not** reporting when uncertain — a false finding wastes a repair cycle.

The highest-value checks are the ones that catch silent corruption: schema drift between the ORM
and the migration, hash-scheme drift between a writer and its reader, duplicate rows on re-index.
These fail quietly and are discovered months later.

---

## 5. No placeholders, ever

No `TODO`. No `FIXME`. No `pass  # implement later`. No `raise NotImplementedError` except where
the contract explicitly specifies it as a default (e.g. `ATSProvider.apply` for providers that do
not support automated submission).

Every function ships with a real, working body. A feature is either integrated or not started.

---

## 6. It must work offline, with zero API keys

The entire pipeline runs end to end with no credentials: `LLM_PROVIDER=null` gives a
deterministic, schema-aware model stub; `EMBEDDING_PROVIDER=hashing` gives a real hashing
embedder; `VECTOR_STORE=memory` gives a pure-python cosine store; `SQLITE_MODE=true` removes
Postgres.

This is a hard requirement, not a convenience. It is what makes the system testable in CI, on a
plane, and by a contributor who has not signed up for anything.

Corollary: **all third-party imports in shipped code are lazy** — inside the function that uses
them — so the app imports cleanly without optional dependencies installed.

---

## 7. Safety defaults are the defaults

`AUTO_APPLY_ENABLED=false` and `DRY_RUN=true`. Submission requires *both* to be deliberately
flipped. Nothing in a build, a test, or a repair may quietly relax this.

The full set of non-negotiables lives in [`docs/CONTRACTS.md` §18](CONTRACTS.md) — never apply
twice, never guess (escalate to manual review instead), never log a secret, never fabricate a
resume bullet.

---

## 8. Be honest about what is and is not done

Report outcomes faithfully. If a test fails, say so with the output. If a step was skipped, say
that. If research could not reach a source, mark the conclusion as inferred rather than
presenting it as observed.

Feature tables in documentation use honest status markers (✅ implemented / 🟡 partial /
⬜ planned) derived from what is actually in the tree.
