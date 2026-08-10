# Getting started

How to actually use ApplicantOS on your own job hunt. About 20 minutes end to end, most of it
waiting for your GitHub to index.

The order matters: the system can only write a résumé from what it knows about you, so knowledge
comes before jobs.

---

## 0. Start it

```bash
cd C:\Users\kings\JobView

# one terminal — the backend
.venv\Scripts\activate
set SQLITE_MODE=true
uvicorn app.main:app --port 8000

# another — the interface
cd desktop
npm run dev
```

`npm run dev` starts the backend for you if one isn't already running, so in practice the second
command is usually enough. Open **http://localhost:5173**.

For the native window instead of a browser tab: `npm run app` (first run compiles the Rust shell,
several minutes; instant after that).

### Clear the demo data first

A fresh clone seeds a fictional embedded engineer so the screens aren't empty. That is not you, and
a résumé generated from it would be someone else's. Wipe it before you start:

```bash
python -m scripts.reset --yes      # drops and recreates the local database
alembic upgrade head
```

If that script isn't present, delete `var/applicantos.db` and re-run `alembic upgrade head`.

---

## 1. Add an API key (optional, but do it)

Without a key everything runs — discovery, scoring, rendering — but résumé *wording* comes from a
deterministic stub. With one, the tailoring is real.

In `.env`:

```bash
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

Or `LLM_PROVIDER=local` pointed at Ollama if you'd rather nothing leaves your machine.
Restart the backend after editing.

---

## 2. Onboarding

The app walks you through it on first launch. Eight steps:

| Step | What it wants | Why |
|---|---|---|
| Identity | Name, email, location, pronouns | Printed on every document |
| Contact | Phone, address | Application forms ask |
| Work authorization | Citizenship, sponsorship needs | Answered factually on forms, never guessed |
| Demographics | Gender, race, disability, veteran | **All optional.** Default is "decline to self-identify" and it is never inferred |
| Preferences | Minimum score, salary floor, locations, blocked companies | Drives what gets applied to |
| Links | GitHub, LinkedIn, portfolio | Both printed and indexed |
| Sources | What to learn from — see below | The important one |
| Master résumé | Upload your current CV | Seeds the knowledge graph |

Everything stays editable in **Settings** afterwards. Nothing is locked in.

---

## 3. Give it something to learn from

This is the step that determines whether the résumés are any good. Under **Knowledge → Sources**:

| Source | What to give it | What it extracts |
|---|---|---|
| **GitHub profile** | your username | Every repo: languages, dependencies, READMEs, stars, commit dates |
| **Résumé** | a PDF or DOCX | Roles, dates, employers, bullets, education |
| **Project folder** | a local path | READMEs, manifests, language mix, git history |
| **Personal website** | a URL | Project pages and prose (crawls politely, honours robots.txt) |
| **LinkedIn export** | the ZIP LinkedIn emails you | Positions, education, certifications, honours |

For LinkedIn: *Settings → Data privacy → Get a copy of your data*. It arrives by email in a few
minutes. **It is never scraped** — the export is the only way in, deliberately.

Then hit **Reindex**. GitHub takes a couple of minutes; a project folder is seconds. Watch the fact
count climb on the Knowledge screen — those facts are what your résumés get built from.

**Sanity check before going further.** Search the Knowledge screen for something you know you've
done. If it isn't there, no résumé will mention it. Add another source.

---

## 4. Tune the scoring

**Settings → Scoring rules.** The defaults are keyword rules with point values:

```
+40  embedded          -20  requires 8+ years
+30  robotics          -40  sponsorship unavailable
+25  firmware
+15  c++
```

Edit them to your field. Also set **minimum score** (default 70) — that's the bar a job must clear
before it's worth applying to.

Rules are deterministic. The same posting always scores the same, so when a number looks wrong you
can find the rule that caused it.

---

## 5. Find jobs

**Postings → Discover.** Pick providers, add keywords and locations, run it.

| Provider | Finds jobs | Applies for you |
|---|---|---|
| Greenhouse, Lever, Ashby | ✅ | ✅ |
| Workday | ✅ | ❌ manual review |
| LinkedIn | your export or a public feed only | ❌ manual review |

156 company boards are pre-configured, so leaving the board list empty still returns thousands of
postings. Results are scored as they arrive.

---

## 6. The first few, by hand

**Leave auto-apply off at first.** Out of the box:

```bash
AUTO_APPLY_ENABLED=false      # master switch
DRY_RUN=true                  # fills the form, never clicks submit
```

Open a high-scoring posting and hit **Apply**. With those defaults the system tailors a résumé,
writes a cover letter, opens the real application, fills every field it can answer — and stops at
the button. Read what it produced.

Check three things:

1. **Is the résumé true?** Every bullet traces to a fact from your sources; it cannot invent
   employers or dates. But it can *emphasise* oddly. If it does, that is a signal your sources are
   thin, not that it lied.
2. **Are the form answers right?** Anything it wasn't confident about is in the **Review queue**
   rather than guessed.
3. **Does the cover letter sound like you?** Adjust tone in Settings.

Correct anything wrong in the review queue. **Those corrections are remembered** — the same question
won't come back next time.

---

## 7. Turn it on

Once a few dry runs look right:

```bash
AUTO_APPLY_ENABLED=true
DRY_RUN=false
```

Restart the backend. Both switches are required — this is deliberately two decisions, not one.

Then set the pace in Settings: `MAX_APPLICATIONS_PER_DAY` defaults to 50. Start lower.

For it to run on its own, you also need the workers:

```bash
celery -A app.workers.celery_app worker -Q discovery,ai,apply,knowledge,maintenance
celery -A app.workers.celery_app beat
```

Or `docker compose -f docker-compose.yml up -d` for the whole thing.

---

## 8. Let it track outcomes

**Settings → Status sync.** Connect your mailbox and it reads rejections, interview invitations and
offers straight out of your inbox, so you don't have to type them in. Access is read-only, queries
are scoped to companies you actually applied to, and message bodies are never stored — only a
500-character snippet and the classification.

It also marks applications **ghosted** after 45 days of silence, which is the outcome you'd
otherwise never record and the one that tells you which résumés aren't landing.

---

## What a normal day looks like

Open the app. The dashboard says what happened overnight: applications submitted, what needs review,
what came back. Clear the review queue — usually a few essay questions. Check Analytics occasionally
to see which roles convert.

---

## When something looks wrong

| Symptom | Cause |
|---|---|
| Résumés feel generic | Not enough indexed. Add sources, check the fact count |
| Everything scores low | Rules don't match your field. Edit `Settings → Scoring rules` |
| Nothing gets applied to | Both switches still closed — that's the default |
| A posting went to review | Open it; the reason is stated. Usually essay questions or a field it wouldn't guess |
| Discovery returns nothing | `python -m scripts.validate_boards` checks every board against its live API |

Deeper operational detail is in [`RUNBOOK.md`](RUNBOOK.md). What it will and won't do on your
behalf is in [`SAFETY.md`](SAFETY.md) — worth reading once before you flip the switches.
