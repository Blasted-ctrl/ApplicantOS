# Scoring

The number that decides whether ApplicantOS applies to a posting.

Code: `app/ai/scoring.py`. Default pack: `app/config/scoring_rules.yaml`. Editable from the
desktop app at **Settings → Scoring**, or over the API at `GET|PUT /api/v1/settings/scoring-rules`.

---

## 1. Two halves, kept apart

**The rule engine is the score.** `Scorer.score_rules()` is pure, synchronous and perfectly
deterministic. It reads a posting, matches a data-driven rule pack plus the user's hard preference
gates, and returns a `ScoreResult` whose arithmetic is fully itemised. No clock, no network, no
randomness, no database — the same posting scored today, tomorrow, and on a colleague's machine
produces byte-identical output.

That is not a nicety. The dashboard shows these numbers next to each other, and a score that
drifts makes every comparison it displays a lie.

**The model only comments.** When a language model is available, it is shown the posting and the
rule breakdown and asked for a bounded `±10` adjustment and one paragraph of prose. The adjustment
is clamped and cached by content hash. **A matched hard negative pins the verdict** to its
rule-based value — see §5.

Any model failure at all — missing key, rate limit, malformed JSON, exhausted budget — logs
`scoring.llm_unavailable` and returns the rule-based result unchanged. `score()` never raises
because of the model.

---

## 2. Rule format

A rule pack is YAML with a `rules` list. Every key is always present; unused lists are `[]` and
unused scalars are `null`.

```yaml
- key: "embedded"                    # stable identifier, unique in the pack, snake_case
  label: "Embedded systems role"     # shown in the score breakdown
  points: 40                         # signed contribution when the rule fires
  field: "text"                      # which haystack to match against
  any_of:                            # fires when AT LEAST ONE is present
    - "embedded systems"
    - "microcontroller"
    - "bare metal"
  all_of: []                         # requires EVERY entry
  none_of: []                        # suppresses the rule when ANY entry is present
  regex: null                        # additional case-insensitive pattern that must also match
```

A rule fires when **all** of these hold — a plain conjunction:

```
(any_of is empty OR at least one entry matches)
AND (all_of is empty OR every entry matches)
AND (none_of is empty OR no entry matches)
AND (regex is null OR the pattern matches)
```

### Fields

| `field` | Haystack |
|---|---|
| `text` (or `all`) | title + description + company + location, joined and lowercased — **the default choice** |
| `title` | Posting title only |
| `description` | Posting body only |
| `company` | Company display name |
| `location` | Location string |
| `work_arrangement` | `remote` / `hybrid` / `onsite` / `unknown` |
| `employment_type` | `full_time` / `part_time` / `internship` / `contract` / `new_grad` / `unknown` |

Use the narrowest field that works. A title-shaped signal scored against `text` fires on body text
and produces false positives nobody can explain — that is why `senior_title_mismatch` matches
`title` and not `text`.

The `text` haystack joins its parts with `" | "`, a non-whitespace token, so a multi-word phrase
can never be formed accidentally by the end of one field abutting the start of the next. A title
ending in "embedded" beside a company named "Systems Inc" must not conjure "embedded systems".

### Matching is word-boundary aware, and that is harder than it looks

Job descriptions are full of technology names that break naive substring search. `term_pattern`
builds a matcher that treats `+` and `#` as part of a technology name and escapes everything else,
which is what makes all six of these come out right at once:

| Must **not** match | Must match |
|---|---|
| `go` inside `Google` | `c++` inside `C++17` |
| `C` inside `CI/CD` or `C++` | `.net` after a letter |
| `R` inside `React` | `node.js` literally |

Multi-word terms join with `\s+`, so a phrase survives a line break. Everything is
case-insensitive.

### Validation

`ScoreRule.__post_init__` raises `ScoringConfigError` naming the offending rule if the key is
blank, the points are not a whole number, the field is unknown, a term list holds a non-string or
a blank string, the regex does not compile, or **the rule states no condition at all** (which
would fire on every posting ever discovered).

`any_of: "c++"` — a bare string instead of a list — is rejected too, because it would iterate as
characters and quietly match every `c` and every `+` in the posting.

Validation happens at load, not at match time, because the pack is user-editable and "invalid
YAML" is not a fixable error report.

---

## 3. The default pack

37 rules, tuned for a new-graduate embedded / robotics / firmware engineer with a strong
systems-programming background.

### The canonical eight

These eight are the worked example in §6. Their points are frozen.

| Key | Label | Points |
|---|---|---|
| `embedded` | Embedded systems role | **+40** |
| `robotics` | Robotics domain | **+30** |
| `firmware` | Firmware engineering | **+25** |
| `cpp` | C++ required | **+15** |
| `new_grad` | New graduate / entry level | **+10** |
| `remote` | Fully remote | **+10** |
| `requires_senior_experience` | Requires 8+ years of experience | **−20** |
| `sponsorship_unavailable` | Visa sponsorship explicitly unavailable | **−40** |

### Technology fit

| Key | Label | Points |
|---|---|---|
| `rtos` | Real-time operating systems | +22 |
| `computer_vision` | Computer vision | +20 |
| `cuda_gpu` | CUDA / GPU compute | +18 |
| `control_systems` | Control systems | +18 |
| `luau_roblox` | Roblox / Luau platform | +18 |
| `pcb_hardware` | PCB / hardware design | +16 |
| `ros_middleware` | ROS / robotics middleware | +16 |
| `linux_kernel_drivers` | Linux kernel / embedded Linux | +15 |
| `rust` | Rust systems programming | +14 |
| `dsp_signal_processing` | Digital signal processing | +14 |
| `machine_learning` | Machine learning | +12 |
| `simulation_modeling` | Simulation and modelling | +10 |
| `python` | Python | +8 |

### Role shape, arrangement, compensation

| Key | Label | Points |
|---|---|---|
| `sponsorship_friendly` | Employer sponsors work visas | +15 |
| `location_michigan` | Preferred location — Michigan | +12 |
| `new_grad_program_bonus` | Structured new-grad / rotational program | +12 |
| `salary_six_figure` | Posted compensation ≥ $100,000 | +12 |
| `internship_opportunity` | Internship or co-op | +8 |
| `full_time_role` | Full-time employment | +5 |
| `hybrid_arrangement` | Hybrid arrangement | −8 |
| `startup_too_small` | Very early-stage startup | −10 |
| `onsite_relocation_required` | Mandatory relocation or full-time on-site | −12 |
| `salary_below_floor` | Posted compensation below $60,000 | −15 |
| `staffing_agency` | Staffing agency or third-party recruiter | −18 |
| `senior_title_mismatch` | Seniority above new-graduate level | −25 |

### Hard blockers

| Key | Label | Points |
|---|---|---|
| `defense_contractor` | Defense / weapons contractor | −20 |
| `phd_required` | Doctorate required | −30 |
| `security_clearance_required` | Active security clearance required | −35 |
| `unpaid_or_commission` | Unpaid, equity-only, or commission-only | −50 |

`sponsorship_friendly` is the rule to copy when writing a negated signal: "visa sponsorship" fires
it, and its `none_of` list ensures "no visa sponsorship" does not.

**Retuning for yourself.** The pack is opinionated about one career. Editing it is expected — the
weights encode *your* preferences, not a universal truth. Change the terms, change the points,
delete what does not apply. The only rules to be careful with are the canonical eight, because
they are the documented worked example.

---

## 4. Preference gates

Separate from the pack, and much stronger. A gate encodes a decision the user has **already made**,
so each carries `HARD_GATE_PENALTY = -1000` — far more than any combination of positive rules can
offset.

| Gate key | Fires when | Driven by |
|---|---|---|
| `pref_blocked_company` | The employer is on the block list | `blocked_companies` |
| `pref_blocked_industry` | The employer's industry is blocked | `blocked_industries` |
| `pref_sponsorship_conflict` | The posting's visa posture contradicts the user's | `require_no_sponsorship` |
| `pref_salary_below_minimum` | Advertised compensation is below the floor | `min_salary` |
| `pref_defense_employer` | The employer is flagged as a defence contractor | `exclude_defense` |
| `pref_startup_too_small` | Headcount is below the floor | `skip_startups_under` |
| `pref_not_remote` | The posting is not fully remote | `remote_only` |

**Gates fire only on evidence.** A posting with no advertised salary never trips the salary floor.
A company with no known headcount never trips the size floor. A `work_arrangement` of `unknown`
never trips remote-only. Golden rule 2 is "never guess", and an absence is not a violation —
inferring one would silently discard good postings.

Two matching details worth knowing:

- **Company names are normalised on both sides.** `"Acme, Inc."`, `"ACME Incorporated"` and
  `"acme"` all reduce to `acme`. A block-list entry also matches as a whole word inside a longer
  name, so blocking `Meta` catches `Meta Platforms` without catching `Metabase`.
- **Industry is matched against the enriched `companies.industry` label only**, never against the
  description. A posting that merely *mentions* an industry is not in it.

### The sponsorship gate is symmetric

Which case applies depends entirely on `require_no_sponsorship`:

- **`true` (the default)** — the user wants a role needing no sponsorship. A posting that
  *demands* sponsorship ("visa sponsorship required") is the mismatch. The negation list is
  checked **first**, because "no sponsorship required" contains "sponsorship required" and means
  precisely the opposite.
- **`false`** — the user needs sponsorship. A posting that refuses to sponsor is the mismatch.

Note the asymmetry: a posting merely *stating* it will not sponsor is **not** a veto for a
candidate who needs no sponsorship. The packaged `sponsorship_unavailable` rule applies its
ordinary −40 there — which is exactly what the worked example relies on.

---

## 5. Verdicts, and the hard-negative lock

```python
threshold = prefs.min_score  # default 70
if total >= threshold:
    verdict = "apply"
elif total >= threshold - 15:
    verdict = "review"  # REVIEW_BAND
else:
    verdict = "skip"
```

`REVIEW_BAND = 15` is a product decision, not a magic number: 15 points is roughly one strong
positive rule, so "missed by one signal" reaches a human while "missed by a mile" does not.

`total` is **unclamped** — a gate drives it to −1000 or below on purpose. `normalized` is that
total clipped to 0–100, and is what `job_scores.normalized` stores and the desktop app renders.

### The lock

`Scorer.score()` lets the model adjust the total, but:

```python
locked = base.has_hard_negative
verdict: Verdict = base.verdict if locked else model_verdict
```

When `pref_sponsorship_conflict`, `pref_blocked_company` or `pref_blocked_industry` matched
(`HARD_GATE_KEYS`), the verdict is pinned to the rule-based decision and
`scoring.llm_verdict_locked` is logged.

**This is written out explicitly rather than left to emerge from the arithmetic.** `-1000` is
already unreachable from `±10`, so the check is technically redundant today — and that is exactly
why it is there. An emergent safeguard is one pack retune away from silently disappearing, and the
failure mode is the pipeline applying to a company the user explicitly blocked.

The model's adjustment lands in the components as `llm_adjustment`, so
`sum(component.contribution()) == total` still holds after the model has spoken.

### What the model is told

The system prompt states its position plainly: the rule total is authoritative, the adjustment is
an integer in `[-10, +10]`, most postings deserve `0`, it may judge only what the posting says,
and **components marked HARD NEGATIVE are policy decisions it may not argue against**. Temperature
is `0.0` — a sampled adjustment would make the visible score jitter between runs, and zero is the
only temperature at which the completion is cached.

The cache key is `(posting content hash, preferences hash, rule pack hash, model name)`. Re-scoring
an unchanged posting for an unchanged user against an unchanged pack costs nothing; editing any of
the three invalidates precisely and only the affected entries.

---

## 6. The worked example

A remote, new-graduate embedded-robotics firmware role written in C++ that also asks for 8+ years
of experience and states it cannot sponsor:

```
+40  Embedded systems role                     "embedded"
+30  Robotics domain                           "robotics"
+25  Firmware engineering                      "firmware"
+15  C++ required                              "c++"
+10  New graduate / entry level                "new grad"
+10  Fully remote                              work_arrangement = remote
-20  Requires 8+ years of experience           regex matched "8+ years"
-40  Visa sponsorship explicitly unavailable   "will not sponsor"
─────
 70  →  apply     (min_score = 70)
```

Exactly at the threshold, and therefore `apply`. That is the pack's calibration point: this
posting is the marginal case the default weights are tuned around. A posting with everything this
one has *minus* the sponsorship problem scores 110 and is an obvious apply; this one scrapes in.

Reproduce it:

```bash
SQLITE_MODE=true LLM_PROVIDER=null python -c "
from app.ai.scoring import score_posting, explain
r = score_posting({
  'title': 'New Grad Embedded Robotics Firmware Engineer',
  'description': 'Modern C++17 firmware for autonomous robots. 8+ years experience preferred. '
                 'We will not sponsor visas.',
  'company_name': 'Acme Robotics', 'location': 'Remote',
  'work_arrangement': 'remote', 'employment_type': 'new_grad'})
print(explain(r))
assert r.total == 70 and r.verdict == 'apply'"
```

Output:

```
+40 Embedded systems role  +30 Robotics domain  +25 Firmware engineering  +15 C++ required
+10 New graduate / entry level  +10 Fully remote  -20 Requires 8+ years of experience
-40 Visa sponsorship explicitly unavailable  = 70 -> apply
```

**Those eight rules' points are frozen.** Changing any of them means updating
`app/config/scoring_rules.yaml`'s header, the module docstring in `app/ai/scoring.py`, this
document, and `tests/test_scoring.py` in the same commit.

---

## 7. What gets stored

`job_scores` — one row per `(posting_id, user_id)`:

| Column | Contents |
|---|---|
| `total` | The unclamped sum |
| `normalized` | Clipped to 0–100 |
| `breakdown` | Every component, matched or not, plus the review band in force and the list of matched hard negatives |
| `verdict` | `apply` / `review` / `skip` |
| `rationale` | The model's paragraph, capped at 900 characters. Never the source of the number |
| `model_used` | Which model wrote it; `null` for a pure rule score |

**Non-matching components are kept**, with `matched=False`, so the desktop score panel can show
what was *examined* rather than only what fired — "we looked for CUDA and did not find it" is more
useful than silence.

The stored breakdown carries `review_band` so the panel can re-render a months-old decision
without re-running the engine against today's pack. **A rule key is therefore forever**: renaming
one orphans every historical score that references it.

---

## 8. Tuning it

**From the desktop app:** Settings → Scoring. The editor validates on save and reports the
offending rule by key.

**From the API:**

```bash
curl -H "X-User-Id: $USER_ID" http://127.0.0.1:8000/api/v1/settings/scoring-rules
curl -X PUT -H "X-User-Id: $USER_ID" -H "Content-Type: application/json" \
     -d @my-rules.json http://127.0.0.1:8000/api/v1/settings/scoring-rules
```

**By editing the file:** change `app/config/scoring_rules.yaml` and call `reset_rule_cache()` (or
restart). The pack is memoised per process.

### Before you commit a change

```bash
# The pack loads and every key is unique
python -c "
from app.ai.scoring import default_rules
r = default_rules(); print(len(r), 'rules')
assert len({x.key for x in r}) == len(r)"

# Scoring is still deterministic
python -c "
from app.ai.scoring import Scorer
p = {'title': 'Embedded Engineer', 'description': 'firmware, c++, robotics'}
assert Scorer().score_rules(p).to_breakdown() == Scorer().score_rules(p).to_breakdown()
print('deterministic')"

# The worked example still totals 70
pytest tests/test_scoring.py -v
```

### Common mistakes

| Mistake | What happens |
|---|---|
| `field: "text"` on a title signal | "senior" in the body fires a title penalty |
| A bare string in `any_of` | Rejected at load — it would iterate as characters |
| A rule with no conditions | Rejected at load — it would fire on every posting |
| Renaming a key | Historical breakdowns orphan; the score panel shows an unknown component |
| Fractional `points` | Rejected — silently rounding 12.5 to 12 makes a pack score differently than it reads |
| A greedy regex | Catastrophic backtracking on a long description. Prefer `any_of` term lists |

---

## See also

- [`CONTRACTS.md`](CONTRACTS.md) §10 — the binding scoring interface
- [`PIPELINE.md`](PIPELINE.md) — stage 3, and where the verdict is consumed
- [`CONFIGURATION.md`](CONFIGURATION.md) — `auto_apply_min_score` vs `prefs.min_score`
- `.claude/agents/scoring-engineer.md` — the agent brief for this work
