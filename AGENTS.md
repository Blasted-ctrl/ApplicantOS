# AGENTS.md

Portable brief for any coding agent working in this repository (Cursor, Codex, Aider, Copilot
Workspace, or a human doing the same job).

**Read [`CLAUDE.md`](CLAUDE.md) first — it is the full brief.** This file exists so tools that
look for `AGENTS.md` by convention find the entry point, and to hold the specialist-agent routing
table. Everything substantive lives in `CLAUDE.md` rather than being duplicated here, so the two
cannot drift apart.

---

## The three binding specs

| File | Governs | Rule |
|---|---|---|
| [`docs/CONTRACTS.md`](docs/CONTRACTS.md) | Every cross-module boundary: paths, signatures, enum values, table names, routes, task names | **Frozen.** Implement as written; record concerns in `docs/OPEN_QUESTIONS.md` |
| [`docs/UI.md`](docs/UI.md) | All desktop visual and interaction design, plus the instant-feel performance contract | **Binding** for anything under `desktop/` |
| [`docs/WORKING_AGREEMENT.md`](docs/WORKING_AGREEMENT.md) | How work gets done: phasing, verification, no-placeholder rule | Applies to every contributor |

Parallel agents build against these files simultaneously. That only works if nobody renames,
"improves," or restructures unilaterally.

---

## The ten golden rules (short form)

1. Never apply twice. 2. Never guess — escalate to manual review. 3. Submission needs
`auto_apply_enabled` **and** `not dry_run`; both default safe. 4. No secrets in logs.
5. No importing a concrete plugin outside its package. 6. Knowledge is the source of truth;
resumes are generated views. 7. Nothing is fabricated — every bullet traces to a `KnowledgeFact.id`.
8. Everything is resumable. 9. Cache aggressively, invalidate precisely. 10. Honor each ATS's
terms of service.

Full text and enforcement details: [`CLAUDE.md`](CLAUDE.md#the-ten-golden-rules).

---

## Specialist agent routing

Definitions live in [`.claude/agents/`](.claude/agents/). Each carries its own mission, owned
files, required reading, invariants, and verification steps.

| Agent | Owns | Use it when |
|---|---|---|
| `ats-provider-engineer` | `app/jobs/` | Adding or fixing an ATS provider, dedupe keys, selector packs |
| `knowledge-engineer` | `app/knowledge/` | Analyzers, extraction, the graph, indexing, retrieval |
| `resume-pipeline-engineer` | `app/ai/resume_engine.py`, `app/documents/` | Tailoring logic, anti-hallucination guards, one-page enforcement |
| `scoring-engineer` | `app/ai/scoring.py`, `app/config/scoring_rules.yaml` | Rule authoring, determinism, hard-negative guarantees |
| `browser-automation-engineer` | `app/browser/` | Field discovery, answer confidence, the never-guess rule, submit kill switch |
| `backend-api-engineer` | `app/api/`, `app/services/`, `app/schemas/` | Endpoints, orchestration, the event bus |
| `data-model-engineer` | `app/models/`, `app/database/`, `alembic/` | Schema changes and migrations |
| `worker-engineer` | `app/workers/` | Task idempotency, retries, queue routing, beat schedule |
| `desktop-engineer` | `desktop/` | UI, the typed API client, keeping `types.ts` in sync with `enums.py` |
| `safety-reviewer` | *read-only* | Auditing any diff against the ten golden rules |

---

## Quick start for an agent

```bash
# Zero-dependency mode — no API keys, no Postgres, no browser needed
export SQLITE_MODE=true LLM_PROVIDER=null EMBEDDING_PROVIDER=hashing VECTOR_STORE=memory

python -m compileall app     # must be clean
ruff check . && mypy app
pytest
```

Then work the checklist at the end of [`CLAUDE.md`](CLAUDE.md#before-you-finish) before calling
anything done.
