# Safety

This tool submits job applications under your name. That makes a whole class of bugs
*unrecoverable* — you cannot unsend an application, and a made-up answer on one is attached to you
at a company you wanted to work for.

Everything here follows from that.

---

## The two switches

```bash
AUTO_APPLY_ENABLED=false      # master kill switch
DRY_RUN=true                  # fills forms, never clicks submit
```

Both default to the safe position, and submission requires **both** to be flipped:

```python
@property
def is_submission_allowed(self) -> bool:
    return self.auto_apply_enabled and not self.dry_run
```

A fresh install will discover jobs, score them, generate a tailored resume, open the application,
fill every field it can answer — and stop at the button. That's deliberate. Watch it work on a few
real applications before you trust it with one.

`AutoFiller.submit` returns `False` **before locating or clicking anything** when either switch is
closed. Not "logs a warning and continues" — it never touches the DOM:

```python
if dry_run or not settings.auto_apply_enabled:
    logger.info("autofill.submit_blocked", ...)
    return False  # nothing below this line runs
```

`AutoFiller` exposes `submit_attempted` / `submit_clicked` hooks specifically so this can be
asserted on the **recorded clicks** rather than the return value — a function can return `False`
and still have clicked something.

This is covered by `tests/test_golden_kill_switch.py`, which drives `AutoFiller.submit` against a
page double that records every interaction, across all four combinations of the two switches plus
the caller's own `dry_run` argument. The assertions are `fake_page.clicks == []` and
`fake_page.lookups == []` — the second being the stronger claim, that the DOM was never even
*queried* for a submit control. One test in that file asserts the opposite: with both switches
open, a control **is** clicked, so the suite cannot be satisfied by a `submit` that never works.

---

## It escalates instead of guessing

The most important design decision in the product: when the system isn't sure, it stops and asks
you. A blank in the review queue costs you two minutes. A wrong answer costs you the job.

| What happened | `ReviewReason` |
|---|---|
| Answer confidence below `MIN_ANSWER_CONFIDENCE` (default 0.75) | `low_confidence` |
| A required field it can't answer honestly | `unknown_field` |
| More essay questions than `MAX_ESSAY_QUESTIONS_BEFORE_REVIEW` (default 3) | `too_many_essays` |
| Captcha detected | `captcha` |
| MFA / verification code prompt | `mfa` |
| Login wall on the application flow | `login_required` |
| Resume upload didn't take | `file_upload_failed` |
| No submit control found via the provider's selector pack | `submit_not_found` |
| Provider doesn't support automated submission | `unsupported_flow` |
| Submission didn't confirm and didn't error | `verification_failed` |

Essay count is checked **before** any field is filled, so a form that's going to review anyway
doesn't waste a resume render on the way there.

---

## It won't apply twice

Two independent mechanisms, because either one alone eventually fails:

1. **A database constraint** — `UNIQUE(user_id, posting_id)` on `applications`, present in both the
   model and the migration. This holds even if application code has a bug.
2. **A status guard** — `Pipeline.submit` refuses when the application is already `SUBMITTED` or
   `CONFIRMED`, checked *before* the provider is called. A guard that runs after the network call
   is not a guard.

Deduplication sits upstream of both: the same posting discovered from two different boards, or the
same posting twice from one board with different tracking parameters, collapses to a single row
before an application is ever created.

---

## Nothing on your resume is invented

Every bullet on a generated resume carries the ID of the `KnowledgeFact` it came from. The model
selects and rewrites — it never authors. Four validations run on every response:

| Guard | Behaviour | Log event |
|---|---|---|
| Unknown fact ID | Dropped entirely | `resume_engine.hallucinated_fact` |
| Rewrite drifts too far (token overlap < 0.35) | Reverted to the original text | `resume_engine.rewrite_rejected` |
| Employer / role / dates | Copied from the source fact, model output discarded | — |
| A number not present in the source | Bullet reverted | `resume_engine.unsupported_metric` |

This is a validation step the model cannot route around, not a prompt asking it nicely. Cover
letters get the same treatment: any figure in the letter must appear in the resume or the posting.

If the LLM is unavailable entirely, `fallback_tailor` ranks your facts and uses their text
verbatim. The pipeline never stops because an API was down, and it never fills the gap with
invention.

---

## Demographic questions are never inferred

Gender, race and ethnicity, disability status, and veteran status are answered from what **you**
entered during onboarding, or default to "decline to self-identify" when that option exists on the
form. They never reach the language model. There is no code path that guesses them from your name,
your school, or anything else.

Work authorization and sponsorship *are* answered from your profile, because they're factual and
you set them explicitly.

---

## Terms of service

Some platforms permit automated applications. Some don't. This tool respects the difference rather
than working around it.

| Platform | Discovery | Submission | Why |
|---|---|---|---|
| Greenhouse | ✅ | ✅ | Public board API, public application form |
| Lever | ✅ | ✅ | Same |
| Ashby | ✅ | ✅ | Same |
| Workday | ✅ | ❌ | Account-gated, multi-step, differs per tenant — routed to manual review |
| LinkedIn | ⚠️ | ❌ | Terms prohibit automated scraping and submission |

**LinkedIn specifically:** no login, no scraping, no submission. Discovery reads only a data export
you downloaded yourself or a public RSS feed. `apply()` raises `UnsupportedFlowError`. This is a
deliberate boundary — if it ever looks like a limitation worth "fixing", it isn't.

Providers that forbid automation set `supports_auto_apply = False` and say so in their module
docstring. Adding a provider means making that call honestly.

---

## Your data

- **Local by default.** Filesystem storage, local vector store, SQLite if you want it. S3 and
  Postgres are opt-in, not assumed.
- **Local models supported.** Point `LLM_PROVIDER=local` at Ollama or LM Studio and nothing leaves
  your machine.
- **Secrets never reach logs.** A structlog processor recursively scrubs anything matching
  password / token / api_key / secret / authorization / cookie / ssn / dob — through nested dicts
  and lists. Traceback rendering runs with `show_locals=False`, because frame locals otherwise dump
  your API key into the log while the same key is correctly redacted two fields over. That bug
  shipped once here and was fixed, and the fix now carries a committed regression test:
  `tests/test_golden_redaction.py` raises an exception from a function holding a credential in a
  frame local, logs it through the configured pipeline, and asserts the secret appears nowhere in
  the bytes that reach the stream.
- **Email access is read-only.** The status-sync feature requests read-only scopes and contains no
  send, delete, move, or flag call anywhere — grep-verifiable. It queries a bounded window scoped
  to companies you actually applied to, never a full mailbox sweep, and persists only a message id,
  sender, subject, a 500-character snippet, and the classification. Full message bodies are never
  written to the database. Credentials live in the OS keychain, never in the database.

---

## Rate and volume

`MAX_APPLICATIONS_PER_DAY` (default 50) and `MAX_APPLICATIONS_PER_SESSION` (default 200) are hard
caps checked before submission. Providers are polled at human speed with backoff; the website
crawler honors `robots.txt` and rate-limits itself to roughly one request per second.

This isn't only about being a good citizen. Blasting hundreds of applications an hour is how you
get flagged, and a flagged account is worse than no automation at all.

---

## Every submission is photographed

Before and after. Screenshots, the confirmation ID when the ATS provides one, the confirmation
text, the exact timestamp, and the duration — all stored against the application. If a company
later says they never received it, you have proof.

The `content_json` of every tailored resume is kept forever, so you can always see exactly what you
sent. The rendered PDF is deleted after confirmation — the structured version is the record, the
file is disposable.

---

## If something goes wrong

Long operations checkpoint as they go, so a crash resumes rather than restarts. An unexpected
exception transitions the application to `FAILED` with the error recorded, rather than leaving it
stuck mid-flight. Nothing is left in `PREPARING` or `SUBMITTING` when a process dies.

To stop everything immediately: set `AUTO_APPLY_ENABLED=false` and restart the workers. In-flight
submissions finish; nothing new starts.

---

## Reporting a safety issue

If you find a path that submits without both switches open, guesses instead of escalating, applies
twice, or puts something on a resume that isn't traceable to a fact — that's a blocker, not a bug.
Open an issue with the file, the line, and the scenario that triggers it.
