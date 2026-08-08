# ApplicantOS

Applying to jobs is mostly copying the same information into slightly different forms. This does
that part for you.

It watches job boards for roles you'd actually want, scores them against what you care about,
writes a resume tailored to each one, fills out the application, and keeps a record of every
submission with a screenshot proving it went through. When it hits something it isn't sure about —
an essay question, a captcha, a field it can't answer honestly — it stops and asks you instead of
guessing.

The part I think is actually interesting is underneath: it doesn't store your resume. It stores
what you've *done*.

---

## The idea

Most resume tools keep a master document and edit it. That falls apart the moment you have more
than one kind of job you're applying for, because a resume for an embedded firmware role and a
resume for a graphics role aren't edits of each other — they're different documents built from the
same life.

So ApplicantOS keeps a **knowledge graph** instead. It reads your GitHub, your portfolio site,
your project folders, your old resumes, your LinkedIn export, and breaks all of it into individual
facts:

```
"Wrote a FreeRTOS scheduler for an STM32F4, cut task latency 40%"
   skills:       [C, FreeRTOS, STM32, embedded]
   metrics:      ["40%"]
   organization: "Robotics Team"
   dates:        2024-09 → 2025-05
   source:       github.com/you/flight-controller/README.md
   impact:       82
```

A resume is then a **query** against that graph. Microsoft Embedded pulls firmware, RTOS and C++ to
the top. NVIDIA pulls CUDA, computer vision and OpenCV. Roblox pulls Luau, networking and
performance work. Same facts, different views.

This has a useful side effect: because every bullet on a generated resume carries the ID of the
fact it came from, the model **can't invent things**. If it returns a bullet that doesn't trace
back to something you actually did, that bullet gets dropped before it's ever rendered. It's not a
prompt asking it nicely not to lie — it's a validation step it can't route around.

And when you push a commit or add a class project, the next resume already knows about it.

---

## Status

Being built in the open, phase by phase. Honest state:

| | |
|---|---|
| Foundation — config, database, 22-table schema, migrations, cache, plugin system | ✅ |
| Knowledge engine — 6 analyzers, indexer, hybrid retrieval, entity graph, AI memory | ✅ |
| Job discovery — 5 ATS providers, deduplication | ✅ |
| Scoring — deterministic rule engine + optional LLM adjustment | ✅ |
| Document generation — LaTeX / DOCX / HTML / Markdown, one-page enforcement | ✅ |
| Resume tailoring engine + cover letters | 🟡 in progress |
| Browser automation + submission | ⬜ next |
| REST API + WebSocket events + background workers | ⬜ |
| Automatic status sync (reads your email for rejections/interviews) | ⬜ spec'd |
| Desktop app (Tauri) | ⬜ |

~69,000 lines across 104 modules so far. **Not usable end-to-end yet** — there's no UI and no
submission path until those phases land.

---

## Try the parts that work

Everything runs with **no API keys and no Postgres**. There's a deterministic stub model, a real
hashing embedder, a pure-Python vector store, and SQLite mode — so you can exercise the whole
knowledge pipeline offline.

```bash
git clone https://github.com/Blasted-ctrl/ApplicantOS.git
cd ApplicantOS
python -m venv .venv && .venv/Scripts/activate     # or source .venv/bin/activate
pip install -e ".[dev]"

export SQLITE_MODE=true LLM_PROVIDER=null EMBEDDING_PROVIDER=hashing VECTOR_STORE=memory
alembic upgrade head
```

Point it at a folder of your own code and see what it learns:

```python
from app.knowledge import KnowledgeIndexer, KnowledgeRetriever
from app.knowledge.analyzers import SourceRef
from app.models.enums import SourceKind

src = await indexer.add_source(
    user.id, SourceRef(kind=SourceKind.PROJECT_FOLDER, uri="~/code/my-project")
)
print(await indexer.index_source(src.id))
print(await retriever.retrieve(user.id, "embedded systems firmware C++"))
```

With real keys, add them to `.env` and it uses Claude or GPT instead of the stub. Nothing else
changes.

---

## Which job boards work

| | Finds jobs | Applies for you | |
|---|---|---|---|
| **Greenhouse** | ✅ | ✅ | Public board API, public application form |
| **Lever** | ✅ | ✅ | Same |
| **Ashby** | ✅ | ✅ | Same |
| **Workday** | ✅ | ❌ | Account-gated multi-step flow, different per tenant. Sent to manual review. |
| **LinkedIn** | ⚠️ limited | ❌ | See below |

**About LinkedIn.** Its terms prohibit automated scraping and automated applications, so this
doesn't do either. It won't log in, won't scrape, and won't submit. What it *will* do is read a
LinkedIn data export you download yourself, or a public RSS feed, and surface those roles for you
to apply to manually. That's a deliberate limitation, not a missing feature — and if it ever looks
like a bug worth "fixing", it isn't.

Adding a new ATS is a single file plus an entry point. Nothing in the core changes.

---

## It won't apply to anything until you tell it to

Two independent switches, both off by default:

```bash
AUTO_APPLY_ENABLED=false      # master kill switch
DRY_RUN=true                  # fills forms, never clicks submit
```

Submission requires `AUTO_APPLY_ENABLED=true` **and** `DRY_RUN=false`. A fresh install fills out
applications and stops at the button, so you can watch it work before trusting it.

Beyond that:

- **It escalates instead of guessing.** Low confidence on an answer, more essay questions than
  your limit, a captcha, an MFA prompt, a required field it can't answer — all of it goes to a
  review queue rather than getting a made-up answer with your name on it.
- **It won't apply twice.** Enforced by a database constraint *and* a status check, because one of
  those alone will eventually fail you.
- **Demographic questions are never inferred.** Gender, race, disability and veteran status come
  from what you entered or default to "decline to self-identify". The model never sees them.
- **Every submission is photographed.** Before and after. If a company later says they never got
  it, you have the screenshot and the timestamp.

---

## Layout

```
app/
  knowledge/     the knowledge graph — analyzers, extraction, vector search, retrieval
  jobs/          ATS providers + deduplication
  ai/            model plugins, embeddings, scoring, resume engine
  documents/     resume rendering — LaTeX, DOCX, HTML, Markdown
  browser/       Playwright automation
  services/      orchestration
  api/           FastAPI + WebSocket events
  workers/       Celery tasks
desktop/         Tauri shell + React app
docs/            architecture, contracts, design system
```

Providers, AI models, resume templates, parsers and knowledge analyzers are all plugins behind one
registry. Nothing imports a concrete implementation directly, which is what keeps adding a job
board from touching the pipeline.

---

## Docs

| | |
|---|---|
| [`docs/CONTRACTS.md`](docs/CONTRACTS.md) | Every module boundary — the spec the whole thing is built against |
| [`docs/UI.md`](docs/UI.md) | Design system for the desktop app, including the performance budget |
| [`docs/WORKING_AGREEMENT.md`](docs/WORKING_AGREEMENT.md) | How the build is run |
| [`CLAUDE.md`](CLAUDE.md) | Orientation for anyone (or anything) writing code here |

---

## A caveat worth reading

Automating job applications sits in a grey area. Some companies are fine with it; some consider it
a terms violation; a few will reject you for it if they notice. This tool tries to stay on the
right side of that line — it won't touch platforms that prohibit automation, it applies at human
speed rather than blasting hundreds of applications an hour, and it defaults to doing nothing.

But you're the one applying. Read the terms of the places you're applying to, keep the review queue
honest, and skim what it generates before it goes out with your name attached.

---

## License

MIT. See [LICENSE](LICENSE).
