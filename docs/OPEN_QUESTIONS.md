# Open Questions

Append-only log of contract ambiguities, conflicts, and deliberate additions found while
implementing. Every item here was **implemented as written** in `docs/CONTRACTS.md`; this
file records the concern for a later decision rather than deviating from the spec.

---

## Phase 1 — Core primitives (config, logging, enums, mixins, database core)

### 1. `session_id` is both a redaction target and a required bound log key

**Conflict.** The redaction vocabulary includes `session_id`, while §16 lists `session_id`
among the bound context keys (`correlation_id`, `user_id`, `session_id`, `posting_id`,
`application_id`, `provider`, `event`).

**Implemented as written:** `SENSITIVE_KEY_PATTERNS` contains `session_id`, so every log
line bound via `bind_context(session_id=...)` renders `session_id: "***redacted***"`.

**Consequence:** run sessions cannot be correlated through logs by `session_id`. Log-based
debugging of a `RunSession` will have to go through `correlation_id` instead.

**Suggested resolution:** the pattern almost certainly means a *web/auth* session token, not
our `RunSession` UUID. Either drop `session_id` from the pattern set (relying on `token` and
`cookie` to catch auth sessions), or rename the bound key to `run_session_id`. Needs a
decision before the observability module ships, since `LogEntry.session_id` (§4) is a real
column that will be populated from this key.

### 2. `auth` as a substring pattern also redacts `work_authorization`

`UserProfile.work_authorization` (§4) contains the substring `auth`, so it is scrubbed
whenever it appears in a log payload. This is harmless — arguably correct, since it is
sensitive personal data — but it is over-redaction relative to intent and will make
debugging the sponsorship scoring path slightly harder. Same applies to any key containing
`dob` (e.g. a hypothetical `dobule`-style typo) and `auth` inside `authored_by`.

Implemented as specified. Flagging in case the intended semantics were whole-token rather
than substring matching.

### 3. `JobScore.verdict` has no enum in §3

§4 defines a `verdict` column on `job_scores` and §10 references a verdict of "apply", but
§3 enumerates no `ScoreVerdict`. No enum was added here to avoid colliding with a definition
the scoring module may introduce in `app/ai/scoring.py`.

**Needs a decision:** who owns the verdict vocabulary, and what are the values? Suggested:
`apply | review | skip`. Until then `verdict` is a free string and the desktop client cannot
type it.

### 4. `MemoryEntry.kind` values were enumerated as `MemoryKind`

§4 specifies the values (`correction`/`outcome`/`feedback`/`preference`/`note`) but §3 does
not declare an enum for them. `MemoryKind` was **added** to `app/models/enums.py` with
exactly those values, because leaving it a free string would break the enum-parity rule
between `app/models/enums.py` and `desktop/src/lib/api/types.ts` (§17).

This is an addition, not a change — no existing name or value was touched. If the models
module prefers a plain string column, `MemoryKind` is simply unused.

### 5. Additive deviations in the database core (all intentional, all backward-compatible)

- **`Base` also mixes in `AsyncAttrs`.** §2 writes `class Base(DeclarativeBase)`. The
  implementation is `class Base(AsyncAttrs, DeclarativeBase)`. `AsyncAttrs` adds only the
  `awaitable_attrs` accessor, which is how an async session loads a relationship without
  raising `MissingGreenlet`. No columns, no behavioural change otherwise.
- **In-memory SQLite uses `StaticPool`, not `NullPool`.** The spec calls for `NullPool` on
  SQLite. That is correct for file-backed databases and applied there, but an in-memory
  database lives *inside* its connection, so `NullPool` would discard the schema between
  statements. `sqlite+aiosqlite:///:memory:` therefore gets `StaticPool`. File-backed SQLite
  is unaffected.
- **`get_settings()` returns the same object as the module-level `settings`.** §1 lists both
  `settings = Settings()` and `@lru_cache get_settings()`. Implemented as
  `settings = get_settings()` so the process has exactly one instance; two independent
  instances would silently diverge whenever a test mutates one of them.
- **`SettingsConfigDict` gained `env_file_encoding="utf-8"`.** Without it, `.env` is decoded
  with the system ANSI codepage on Windows, which corrupts non-ASCII values. No field,
  name, or default changed.
- **`Base.type_annotation_map` maps more than the three required annotations.** It adds
  `list[str]`, `list[Any]`, `list[dict[str, Any]]` → `JSONType` and `list[float]` →
  `EmbeddingType`, so the many JSON-list columns in §4 (`links`, `skills`, `aliases`,
  `fact_ids`, …) and the embedding columns map without an explicit type at each call site.

### 6. `scoring_rules.yaml` — field vocabulary and match semantics need to match the engine

The per-rule schema (`key, label, points, field, any_of, all_of, none_of, regex`) is frozen,
but §10 does not define what `field` may contain or how the four match keys combine. The
packaged pack assumes, and documents in its header:

- **Fields:** `text` (title + description + company + location, lowercased), `title`,
  `description`, `company`, `location`, `work_arrangement`, `employment_type`.
- **Combination:** a plain conjunction — a rule fires when `any_of` is empty or one entry
  matches, **and** `all_of` is empty or all match, **and** `none_of` is empty or none match,
  **and** `regex` is null or matches. All substring matching is case-insensitive.

`app/ai/scoring.py` must implement these exact semantics or the canonical worked example
will not total 70. Verified: with the semantics above, the canonical posting fires exactly
the eight documented rules and totals exactly 70.

Note also that the frozen rule schema has no numeric comparator, so the salary-floor
requirement is expressed as regexes over the description (`salary_six_figure` at +12,
`salary_below_floor` at -15) rather than as a comparison against
`JobPosting.salary_min`/`salary_max`. If numeric thresholds are wanted, the rule schema
needs a comparator field.

### 7. Minimum SQLAlchemy version

`app/models/mixins.py` uses `mapped_column(..., sort_order=...)` (SQLAlchemy 2.0.4+) and
`hybrid_property.inplace` (2.0.4+). The dependency pin must be **`sqlalchemy>=2.0.4`**, and
`>=2.0.30` is recommended. `pyproject.toml` should not pin a bare `>=2.0`.

### 8. `app/models/__init__.py` was intentionally not created

Phase 1 owns `app/models/enums.py` and `app/models/mixins.py` only; the package `__init__`
belongs to the models module. Note that `app.database.session.init_db()` imports
`app.models` to populate `Base.metadata` before `create_all` — that package `__init__` must
import every model module, or `init_db()` will create an empty schema. The import failure is
logged (`database.models_import_failed`) rather than raised.
