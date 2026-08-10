# Open Questions

Append-only log of contract ambiguities, conflicts, and deliberate additions found while
implementing. Every item here was **implemented as written** in `docs/CONTRACTS.md`; this
file records the concern for a later decision rather than deviating from the spec.

---

## Phase 1 — Core primitives (config, logging, enums, mixins, database core)

### 1. `session_id` was wrongly added to the redaction vocabulary — **resolved, removed**

**This item previously misquoted §16.** It claimed a "conflict", stating that "the redaction
vocabulary includes `session_id`". It does not. §16 defines the scrub set as exactly
`password / token / api_key / secret / authorization / cookie / ssn / dob`, and separately
names `session_id` as one of the seven **bound context keys** that must appear on log lines
(`correlation_id`, `user_id`, `session_id`, `posting_id`, `application_id`, `provider`,
`event`). `session_id` in `SENSITIVE_KEY_PATTERNS` was an unrequested addition, not an
implementation of a contradictory spec.

**Resolved:** `session_id` has been deleted from `SENSITIVE_KEY_PATTERNS`. A web/auth session
credential is still caught by the contract-specified `token` and `cookie` patterns.

**What it was breaking:** every line of a pipeline run bound via
`bind_context(session_id=str(run_session.id))` rendered `session_id: "***redacted***"`, so a
`RunSession` could not be traced through its own logs — the whole point of §16. Worse,
`log_entries.session_id` (§4) is a real `GUID` column (`app/models/log.py`), so a log sink
populating it from that bound key would pass `"***redacted***"` into
`GUID.process_bind_param`, raising `ValueError: badly formed hexadecimal UUID string` on
every insert.

### 2. `work_authorization` is still redacted, because §16 mandates `authorization`

**Partly resolved.** The non-contractual bare `auth` pattern has been removed from
`SENSITIVE_KEY_PATTERNS` (§16 does not list it; `authorization` covers the header, `token`
covers `oauth_token`, and `x-auth` is now listed explicitly for `x-auth-header`). That stops
collateral matches such as `authored_by`.

**Still open, and it is not fixed by that removal.** `work_authorization` contains
`authorization` — which §16 *does* mandate — so under substring matching it is still scrubbed:
`redact_secrets(None, "info", {"work_authorization": "citizen"})` returns `"***redacted***"`.

**Why it matters:** `work_authorization` is a real `user_profiles` column (§4), a
`WorkAuthStatus` enum value (§3), and a key emitted by name from `UserProfile.to_dto()`
(`app/models/profile.py`). §10 makes sponsorship a *hard negative* the LLM may never flip into
"apply", and §12.2 routes an unanswerable work-authorization field to `NEEDS_REVIEW`. When
either fires wrongly, the one field that determined the outcome is unreadable in the logs and
an operator cannot tell `citizen` from `needs_sponsorship`.

**Needs a decision.** None of the available fixes is purely local, so none was taken:

- **Whole-token matching** (split the key on `_`/`-`/`.`, compare tokens) does *not* help:
  `work_authorization` still yields the token `authorization`. It would additionally break
  `api_key`, which is not a single token of anything.
- **An explicit non-sensitive allow-list** consulted before the patterns (e.g.
  `{"work_authorization"}`) is the smallest fix, but it introduces a deliberate hole in a
  security-critical denylist and a mechanism §16 does not describe.
- **Exact-key matching** (`key.lower() in PATTERNS`) fixes it but loses `x-api-key`,
  `auth_token`, `session_cookie` and every other real-world compound.

Recommend the allow-list, scoped to exactly the §4 column names that collide, and stated in
§16 so it cannot grow silently. Note the same substring hazard exists for `dob` (any key
containing that trigram) and `ssn`.

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

---

## Phase 1 — Identity & knowledge ORM models (`user.py`, `profile.py`, `knowledge.py`)

Everything below was **implemented exactly as `docs/CONTRACTS.md` §4/§5 specify**. These are
either ambiguities the spec leaves open, cross-module assumptions other agents must match, or
additive helpers.

### 9. Reverse relationship names on models owned by other modules

§4 names the columns but not the ORM relationship attributes, so the two sides of every
cross-module relationship had to agree without a contract to agree on. `User` declares the six
collections §4 implies, split into two groups by who owns the other end:

**Bidirectional (both sides in this module set)** — plain `back_populates` on both sides:

| `User` attribute | reverse attribute |
|---|---|
| `profile` | `UserProfile.user` |
| `sources` | `KnowledgeSource.user` |
| `facts`   | `KnowledgeFact.user` |

**One-directional (other end owned by another module)** — `app/models/application.py`,
`app/models/resume.py` and `app/models/session.py` each declare
`user: Mapped[User] = relationship("User")` with **no** `back_populates`, documented in those
files as "this module owns the reverse collection's name". `User.applications`, `User.resumes`
and `User.sessions` therefore declare `overlaps="user"` instead of `back_populates="user"`:
both relationships knowingly write `<table>.user_id`, and `overlaps` is the construct that says
so. One side declaring it suppresses the conflict check in both directions.

**Consequence:** assigning `application.user = u` does not also append to `u.applications`
before a flush. Persistence is unaffected — appending to the collection still writes
`user_id` — but in-memory back-population only flows collection → foreign key.

**Suggested resolution:** if bidirectional syncing is wanted later, add
`back_populates="applications"` / `"resumes"` / `"sessions"` to the three sibling modules and
swap `overlaps=` for `back_populates="user"` here. That is a one-line change on each of six
relationships and needs the two modules changed together.

`KnowledgeDocument`, `KnowledgeChunk`, `KnowledgeEntity`, `KnowledgeEdge` and `MemoryEntry`
deliberately declare **no** `user` relationship, because §4 lists only the six collections
above on `User` and adding more would have required inventing reverse names on `User`.
`CoverLetter`, `JobScore` and `UploadedFile` each hold a one-directional `user` many-to-one with
no matching collection on `User`; that is consistent with §4 and creates no conflict.

### 10. `UserProfileDTO` has no field list, so `to_dto()` defined one

§9 requires a DB-free `UserProfileDTO` but does not enumerate its fields.
`UserProfile.to_dto()` returns every profile column plus `user_id`, `full_name` and `email`.
The latter two are read from the owning `User` **only when that relationship is already
loaded** — `to_dto()` emits no SQL, because a lazy load inside an async session raises
`MissingGreenlet` and a `to_dto` that can raise is unusable in worker paths. `User.profile`
is `selectin`-loaded, so arriving via `user.profile` always satisfies this; a query that
selects `UserProfile` directly must `selectinload(UserProfile.user)` if it needs identity
fields. Enum columns are rendered as their string values.

### 11. Nullability and defaults the contract does not state

- `users.full_name` is **nullable** — a user row can exist before onboarding collects a name.
- `users.preferences` is `NOT NULL DEFAULT {}` and read through `User.prefs`.
- `knowledge_documents.title` and `.content_hash`, and `knowledge_facts.content_hash`, are
  `NOT NULL` with no default: a document or fact without a change-detection / dedupe key is
  unusable, so the failure should be loud at insert rather than silent at query time.
- `knowledge_facts.normalized_text` is `NOT NULL DEFAULT ''`; populate it with
  `KnowledgeFact.refresh_derived_fields()`.
- All JSON columns are `NOT NULL` with a Python-side `{}` / `[]` default.
- Enum columns default to the neutral member (`WorkAuthStatus.UNKNOWN`,
  `WorkArrangement.UNKNOWN`, `IndexStatus.PENDING`) — never to a guess.
- `user_profiles.start_date_availability` is **free text** (`VARCHAR(64)`), not a date, because
  application forms accept "Immediately" and "after May graduation" as often as an ISO date.
- `knowledge_facts.date_start` / `date_end` are likewise stored as the source wrote them.
  Parsing them into dates would fabricate precision the source never had.

### 12. Foreign-key delete policy

`ON DELETE CASCADE` for containment (`source -> documents -> chunks`, `entity -> edges`) and
`ON DELETE SET NULL` for provenance (`knowledge_facts.source_document_id`,
`knowledge_facts.entity_id`). Re-indexing churns documents; a user-verified fact must outlive
the document it was first seen in, losing only its pointer. All child relationships use
`passive_deletes=True` so the database performs the action.

`knowledge_chunks` has no `user_id` — §4 does not list one. Ownership is reached through
`document_id`; every user-scoped chunk query must join `knowledge_documents`.

### 13. Enum storage: `VARCHAR`, not a native PostgreSQL type

Both modules build enum columns with
`Enum(cls, native_enum=False, length=64, values_callable=..., validate_strings=True)`.
`native_enum=False` avoids a PostgreSQL `TYPE` that would need a migration on every new enum
member; `values_callable` persists the member **value** (`"github_repo"`) rather than
SQLAlchemy's default of the member **name** (`"GITHUB_REPO"`), which is what keeps the
database, the JSON API and `desktop/src/lib/api/types.ts` byte-identical (§17). No `CHECK`
constraint is emitted (`create_constraint` has defaulted to `False` since SQLAlchemy 1.4), so
the `ck_` naming convention is not involved.

**Known duplication:** the `_enum_column_type()` factory is byte-identical in
`app/models/profile.py` and `app/models/knowledge.py`. Phase 1 scope did not permit creating a
shared module. It belongs in `app/database/types.py` (as e.g. `enum_column(cls, name=...)`) and
should be hoisted there when that file is next touched; both copies then become imports.

### 14. `KnowledgeEntity.normalize()` preserves `+` and `#`

The contract says "normalized_name"; it does not define the normalisation. Implemented in
`normalize_for_matching()` as: NFKC, casefold, delete intra-word punctuation (`.`, apostrophes),
replace all other punctuation and `_` with a space, collapse whitespace, trim — **except** `+`
and `#`, which survive. Stripping them would merge `C`, `C++` and `C#` into one entity, which is
a damaging false merge in this domain. Consequences: `Node.js` and `NodeJS` both normalise to
`nodejs` (intended), `Objective-C` becomes `objective c`, and a name made only of punctuation
normalises to `""` — which `KnowledgeEntity.normalize()` logs as
`knowledge.entity_name_normalized_empty` and returns, for the caller to reject.

`KnowledgeFact.normalize_text()` uses the same function so an entity name and the fact text
mentioning it reduce identically.

### 15. `KnowledgeFact.compute_hash()` field order is frozen by implementation

`sha256(normalized_text | norm(organization) | norm(role) | date_start | date_end)`, UTF-8,
joined by ASCII UNIT SEPARATOR (U+001F) so no combination of field values can collide across a
field boundary. Verified stable across casing and whitespace differences. **Changing the field
order, the separator, or the normalisation invalidates every stored `content_hash` and requires
a re-index**, so it is effectively frozen. `build_content_hash()` is exposed as a static method
so `KnowledgeIndexer` can hash an `ExtractedFact` *before* deciding whether to insert, using the
same arithmetic the stored row will use.

### 16. Additive helpers (no contract name changed)

- `User`: `update_prefs(**changes)` (validated partial update — the `prefs` getter returns a
  copy, so mutating it silently does nothing; this closes that footgun), `is_onboarded`,
  `mark_onboarded()`, and an `@validates("email")` hook that trims and lowercases, so a
  case-varying duplicate cannot defeat the unique constraint.
- `UserProfile`: `canonical_links()`, `eeo_answers()`, `decline_eeo_self_identification()`
  (fills only *unanswered* EEO fields with `decline_to_self_identify`, never overwriting a
  stated answer), plus the module constants `DECLINE_TO_SELF_IDENTIFY`, `EEO_FIELD_NAMES`,
  `PROFILE_LINK_KEYS`.
- `KnowledgeFact`: `normalize_text()`, `build_content_hash()`, `refresh_derived_fields()`.
- `MemoryEntry`: `is_expired(at=None)`.
- Module-level `normalize_for_matching()` in `knowledge.py`.
- `UserPreferences` list fields use `Field(default_factory=...)` instead of the literal `= []`
  shown in §5. Identical defaults and identical JSON schema; `default_factory` merely removes
  any dependence on pydantic's default deep-copy behaviour. `model_config` adds
  `extra="ignore"` (pydantic's own default, made explicit — a preferences document written by a
  newer build must still load on an older one) and `validate_assignment=True`.

### 17. `__repr__` renders from `self.__dict__`

Every model's `__repr__` reads `self.__dict__`, which holds only already-loaded attribute
values. Reading mapped attributes directly would emit SQL for an expired instance and raise
`DetachedInstanceError` for a detached one, and a `__repr__` that can raise or query is a
debugging hazard. An unloaded column therefore renders as `None`.

### 18. `app/models/__init__.py` must import all three modules

Confirming item 8 from the previous phase: `init_db()` and mapper configuration both depend on
`app.models` importing `user`, `profile` and `knowledge` alongside every other model module.
`User` declares relationships to `Application`, `Resume` and `RunSession`; if those modules are
not imported, the first mapper configuration fails with "expression 'Application' failed to
locate a name".

---

## Phase 2 â€” Application-domain and runtime ORM models

Files: `app/models/company.py`, `posting.py`, `score.py`, `resume.py`, `cover_letter.py`,
`application.py`, `session.py`, `checkpoint.py`, `file.py`, `log.py`, `cache_entry.py`.
Every table name, column name, unique constraint, and enum value in Â§4 was implemented
exactly as written. The items below are ambiguities, cross-module coupling, and three
deliberate referential-integrity choices.

### 9. Relationships to `User` are declared one-directionally

`JobScore`, `Resume`, `CoverLetter`, `Application`, `RunSession` and `UploadedFile` each
declare `user: Mapped[User] = relationship("User")` **without** `back_populates`.

**Why.** Â§4 freezes columns, not relationship attribute names, and `app/models/user.py` is
built in parallel. A `back_populates="applications"` here would hard-fail mapper
configuration (`InvalidRequestError: Mapper 'User' has no property 'applications'`) if that
module happens to name the collection `job_applications`. The one-directional form has no
hard-failure path: if `User` declares `applications = relationship("Application",
back_populates="user")`, SQLAlchemy links **both** sides from there and the pair becomes
fully bidirectional at no cost.

**What `app/models/user.py` needs to know.** The many-to-one attribute is named `user` on
all six models. Declaring reverse collections with `back_populates="user"` is supported and
recommended. Declaring them *without* `back_populates` is also functional but emits a
`SAWarning` about conflicting sync targets on every mapper configuration.

**Needs a decision:** freeze the six reverse collection names on `User` (suggested:
`job_scores`, `resumes`, `cover_letters`, `applications`, `sessions`, `files`) so the models
package can be made symmetric.

### 10. `applications.posting_id` is `ON DELETE RESTRICT`, not `CASCADE`

The only deliberate departure from the "CASCADE for owned children" rule, and it exists to
protect golden rule #1.

If a posting that has already been applied to is hard-deleted, `CASCADE` removes the
`Application` row with it. Discovery then rediscovers the same job under a **fresh primary
key**, `uq_applications_user_posting` no longer recognises it, and the system applies a
second time â€” silently. `RESTRICT` turns that path into an `IntegrityError`.

`cleanup.expire_postings` sets `status = expired` and is unaffected. Any maintenance code
that genuinely intends to remove an applied-to posting must delete the applications first,
explicitly. `JobPosting.applications` therefore carries `passive_deletes=True` and **no**
`delete-orphan` cascade. `job_scores` and `cover_letters` still cascade from a posting â€”
both are regenerable derivations, not history.

### 11. `job_postings.company_id` and `applications.company_id` are nullable and `SET NULL`

Â§4 lists `company_id` as an FK without stating nullability. Both are implemented nullable
with `ON DELETE SET NULL`, and `Company` has no delete cascade in either direction.

Rationale: a company row is reference data that gets merged and re-enriched, and `ingest`
must be able to persist a posting before company resolution completes. Cascading from
`companies` would make a company merge destroy postings and, through them, application
history. `Company.postings` / `Company.applications` are plain `passive_deletes=True`
collections.

**Consequence:** `posting.company` may be `None`, and the desktop client must render an
unresolved employer rather than assuming the relationship is populated.

### 12. `applications` and `resume_versions` / `cover_letters` form a mutual foreign key

Â§4 specifies both `applications.resume_version_id` and `resume_versions.application_id`
(likewise for cover letters), which is a genuine cycle between two tables.

Implemented as written, resolved the standard way: the two pointer columns on `applications`
carry `use_alter=True` with explicit constraint names
(`fk_applications_resume_version_id_resume_versions`,
`fk_applications_cover_letter_id_cover_letters`), and `Application.resume_version` /
`Application.cover_letter` carry `post_update=True` so a flush can insert both rows and then
wire them together.

**Consequence:** SQLite reports `supports_alter = False`, so SQLAlchemy silently omits
those two constraints there. On the lightweight install, deleting a `ResumeVersion` or
`CoverLetter` leaves a dangling `applications.resume_version_id` /
`applications.cover_letter_id` instead of nulling it. The ownership foreign keys
(`resume_versions.application_id`, `cover_letters.application_id`) are ordinary
`ON DELETE SET NULL` and *are* enforced on both backends. Both document tables are
soft-deleted in normal operation, so this is latent rather than live â€” but it should be
closed by a service-layer guard or by dropping one of the two pointer columns.

### 13. `Company.normalize()` and `app.jobs.dedupe.normalize_company()` must agree

`COMPANY_LEGAL_SUFFIXES` is frozen at exactly the thirteen tokens named in the brief:
`ag co corp gmbh inc incorporated limited llc ltd plc pte pvt sa`. Nothing was added, even
though `company`, `corporation`, `llp`, `bv`, `nv`, `oy`, `ab` and `srl` are obvious
omissions.

**Why it matters:** `normalized_name` is `UNIQUE`. If `dedupe.normalize_company` strips a
token this does not (or vice versa), the same employer produces two rows, which fragments
the block list, the analytics, and the applications-per-company view. Any change to the
suffix set is a data migration, not a code change.

The full transformation, which `normalize_company` must mirror: NFKD decomposition, ASCII
fold, lowercase, `&` expanded to `" and "`, every non-alphanumeric run collapsed to one
space, then trailing suffix tokens removed repeatedly â€” never removing the last remaining
token, so a company literally named "Limited" does not normalise to the empty string.

### 14. `JobScore.total` and `.normalized` were typed as `int`

Â§4 names both columns but not their types. Both are `Integer`: every threshold they are
compared against is an int (`settings.auto_apply_min_score`, `UserPreferences.min_score`,
both `70`). `total` is the unbounded raw rule sum; `normalized` is the clamped 0-100 figure
and is the one every threshold reads. If `app/ai/scoring.py` produces floats, this needs a
migration to `Float` before any data exists.

`verdict` remains a free `String(32)` â€” see item 3 above, which is still open.

### 15. `log_entries.session_id` / `application_id` / `posting_id` are correlation ids, not foreign keys

Implemented as plain `GUID` columns with no `ForeignKey`. Three reasons: a log line must be
writable for an entity whose transaction rolled back (exactly the case worth logging); a
cascade would destroy the audit trail of whatever was deleted; and this is the
highest-write table in the schema, where three referential checks per insert buy nothing.

Note this interacts with item 1 above: `LogEntry.session_id` is populated from the structlog
bound key `session_id`. That key is no longer scrubbed — `session_id` has been removed from
`SENSITIVE_KEY_PATTERNS` — so the value reaching `GUID.process_bind_param` is a real UUID.

### 16. Enum storage: `native_enum=False` plus `values_callable`, and no CHECK constraint

Every enum column is declared as
`Enum(TheEnum, name="...", native_enum=False, values_callable=operator.methodcaller("values"))`.

`values_callable` is **mandatory**: SQLAlchemy's default persists `Enum.name`, which would
write `"FULL_TIME"` where Â§3 and `desktop/src/lib/api/types.ts` require `"full_time"`.
`native_enum=False` keeps the column a `VARCHAR` so adding a member never needs a PostgreSQL
type migration.

`create_constraint` is left at its default (`False`), so no `CHECK` constraint is emitted.
Trade: adding an enum member stays a code-only change, at the cost of the database not
rejecting an out-of-vocabulary string written by raw SQL. Flagging in case a `CHECK` is
wanted; it would have to be added to every enum column at once.

### 17. `job_postings.dedupe_key` is nullable

`UNIQUE(dedupe_key)` is implemented as a unique index (`unique=True, index=True` on one
column produces exactly one unique index, satisfying both the constraint and the index the
brief asks for). The column is nullable so a posting can exist between ingestion and
`app.jobs.dedupe` running; SQL treats NULLs as distinct, so any number of not-yet-deduped
rows coexist under the constraint. `Pipeline.ingest` must therefore never rely on
`dedupe_key` being present on a freshly inserted row.

`content_hash` is deliberately a different column answering a different question ("has the
text changed since I scored it?") and is not unique.

### 18. Additions beyond the letter of the brief (all additive, none rename anything)

- **`Application.can_submit` is a `hybrid_property`, not a plain `property`.** It reads as a
  bool on an instance exactly as specified, and additionally compiles to
  `status NOT IN (...)` inside a `where()` clause, so `Pipeline` can filter submittable
  applications in SQL instead of in Python.
- **`RunSession.duration_seconds` needs a dialect-compiled SQL form.** Timestamp subtraction
  has no portable spelling, so `_ElapsedSeconds` compiles to `EXTRACT(EPOCH FROM ...)` by
  default and to `julianday` arithmetic on SQLite. Any third backend (MySQL) would emit the
  PostgreSQL form and fail; add a `@compiles` handler if one is ever supported.
- **`RunSession.observe_application_duration(seconds)`** maintains
  `avg_application_seconds` as a running mean using `applications_completed` as the sample
  count. It must therefore be called *after* `record(applications_completed=1)` for the same
  application. `record(**deltas)` itself only touches the six counters in
  `SESSION_COUNTER_FIELDS`, raises `ValueError` on an unknown keyword rather than silently
  creating an instance attribute, and clamps at zero with a
  `run_session.counter_underflow` warning.
- Small, non-contractual helpers were added where a service will obviously need them:
  `JobPosting.is_open` / `.salary_range`, `JobScore.meets()` / `.is_clamped`,
  `ResumeVersion.bullet_count` / `.has_artifact`, `CoverLetter.word_count` / `.has_artifact`,
  `Application.needs_review`, `Checkpoint.is_expired()` / `.can_resume()` / `.mark_*()`,
  `UploadedFile.is_expired()` / `.is_reclaimable()`, `CacheEntry.is_expired()` /
  `.is_fresh()` / `.register_hit()`, `RunSession.finish()` / `.add_token_usage()`.

### 19. `app/models/__init__.py` is still not created

Phase 2 owns the eleven model modules listed above and did not create the package
`__init__`. Restating phase-1 item 8 because it is now load-bearing: `init_db()` imports
`app.models` to populate `Base.metadata`, and **mapper configuration will fail** unless that
`__init__` imports every model module â€” including `user.py`, `profile.py` and
`knowledge.py`. A `relationship("User")` that never resolves raises at first use, not at
import, so a missing import surfaces as a confusing runtime error rather than an
`ImportError`.


### 20. Addendum to items 9 (both of them): the `User` pairing is now bidirectional

Two independently-written notes above are numbered 9 â€” the one from `app/models/user.py`
and the one from this module set. Both described the same coordination problem and both are
now partly superseded. This is the resolved state, verified against the files on disk.

`app/models/user.py` declares `applications`, `resumes` and `sessions` with `overlaps="user"`
and no `back_populates`, expecting the sibling modules to stay one-directional. Once that
file existed, its collection names were no longer a guess, so `application.py`, `resume.py`
and `session.py` now declare the reverse explicitly:

| child attribute | declares | pairs with |
|---|---|---|
| `Application.user` | `back_populates="applications"` | `User.applications` |
| `Resume.user`      | `back_populates="resumes"`      | `User.resumes` |
| `RunSession.user`  | `back_populates="sessions"`     | `User.sessions` |

Declaring `back_populates` on **one** side is sufficient: SQLAlchemy's
`_add_reverse_property` registers the pair in both directions, so mapper configuration stays
silent and the caveat recorded in the other item 9 no longer applies â€” assigning
`application.user = u` now *does* append to `u.applications` before a flush. The
`overlaps="user"` arguments on the `User` side became redundant when this landed, but they
are harmless and were left alone rather than editing another module's file.

This also works unchanged if `user.py` reverts to `back_populates="user"`: naming the
reverse from both sides is the ordinary symmetric form. The only edit that would break it is
renaming or removing one of those three collections on `User`.

`JobScore.user`, `CoverLetter.user` and `UploadedFile.user` remain one-directional, because
`User` declares no reverse collection for scores, cover letters or files. Nothing else writes
those three foreign keys, so there is no conflicting sync target and no warning. Adding
`User.job_scores` / `.cover_letters` / `.files` later needs `back_populates="user"` there and
nothing at all in these three modules.


---

## Phase 1 — Caching layer (`app/cache/`) and plugin system (`app/plugins/`)

Everything in §7 and §6 was implemented as written. The notes below record placement
decisions the contract left open, one cross-backend semantic that callers must know about,
and two additive validations.

### 9. Cache values round-trip as **JSON** on disk/Redis but as **live objects** in memory

`MemoryCache` stores values by reference and hands the original object back.
`DiskCache` and `RedisCache` serialise to JSON, so a pydantic model goes in and a `dict`
comes out; a `set` comes back as a `list`; `bytes` come back as a base64 `str`; a
`datetime` comes back as an ISO string.

This means the **same code observes different types depending on `cache_backend`**, which
is a real trap for any module caching a model instance. The default (`redis`) and the
`disk` backend are tiered behind memory, so a value can even come back as the live object
on the first read (front-tier hit) and as a `dict` after a restart (backing-tier hit).

**Implemented as specified** — §7 defines `RedisCache` as "orjson-serialized" and
`DiskCache` as file-backed, and gives no serialisation contract for values.

**Guidance for callers until this is decided:** cache plain JSON-native data, or re-validate
on read (`Model.model_validate(cached)`). Do not rely on getting your object back.

**Suggested resolution:** either (a) state in §7 that cached values are JSON documents and
callers must re-validate, or (b) have `MemoryCache` round-trip through the same codec so
every backend behaves identically (costs a serialise/deserialise per memory hit), or
(c) add an optional `loader`/`dumper` pair to `@cached`. Recommend (a) — cheapest, and it
matches what the durable backends can actually promise.

### 10. `TieredCache`, the value codec, and the metric bridge live in `app/cache/base.py`

§7 declares `class TieredCache(Cache)` but §0's file list for `app/cache/` (`base.py`,
`memory.py`, `disk.py`, `redis_cache.py`, `decorators.py`, `keys.py`) names no file for it.
Since the file list is authoritative, no new module was created; `TieredCache` sits in
`base.py` next to the `Cache` protocol and `BaseCache`, together with `serialize`/
`deserialize` (shared by the disk and Redis backends) and `emit_cache_event`.

All of these are re-exported from `app/cache/__init__.py`. **Import from `app.cache`, not
from `app.cache.base`,** so a later move costs nothing.

### 11. `NAMESPACES` is a constant-holder class, not an enum

The brief says "`NAMESPACES` constants: `LLM, EMBEDDING, ...`" without fixing the container.
Implemented as a class with upper-case `Final[str]` attributes, so `NAMESPACES.LLM` reads
naturally and `NAMESPACES.all()` is an accurate inventory for `cleanup.prune_cache`. It is
deliberately **not** a `StrEnum` in `app/models/enums.py`, because cache namespaces are not
part of the API surface mirrored by `desktop/src/lib/api/types.ts` (§17), and putting them
there would imply they are.

Unknown namespaces are accepted (normalised, not rejected), so a later module can add one
without editing this file.

### 12. Cache keys are hashed with the **standard library** `json`, never `orjson`

`orjson` is used for cache *values* but never for key derivation. A process with the
accelerator installed and one without must derive byte-identical keys, otherwise a shared
Redis instance silently fragments into two disjoint caches. `app/cache/keys.py` therefore
imports nothing outside the standard library.

### 13. `@cached` returns a descriptor object, not a plain function

`functools.wraps` is applied (so `__name__`, `__qualname__`, `__doc__`, `__wrapped__` and
`inspect.signature` are all preserved), but the wrapper is a class instance implementing
`__get__`.

**Why:** the brief requires `.invalidate()`/`.cache_key()` on the wrapped function *and*
that methods skip `self` in key derivation. With a plain function wrapper those two
requirements silently conflict — `service.compute.invalidate(21)` resolves through
`MethodType` to the underlying function with the receiver already lost, so it derives a
*different* key from the one `await service.compute(21)` stored and invalidates nothing.
Since every service in §13 is a class, that would have been a silent, repo-wide
cache-invalidation bug. The descriptor re-supplies the receiver, so `obj.m(x)`,
`obj.m.cache_key(x)` and `obj.m.invalidate(x)` all agree on one key. (Verified by test.)

**Known consequence:** `inspect.iscoroutinefunction(decorated)` is `False` — the object is
not a coroutine function, its `__call__` is. Do not stack `@cached` beneath a decorator that
tests for a coroutine function, notably a FastAPI route decorator. `@cached` is intended for
service and AI-layer functions, not route handlers.

### 14. `PluginRegistry.register` requires each class to declare its **own** `meta`

An inherited `meta` is rejected with `PluginLoadError`. §6 says register "validates that the
class declares `meta`"; this is the strict reading. Without it, a subclass that forgot its
own `meta` would try to register under its parent's `(kind, name)` and either shadow the
parent or fail confusingly. Abstract intermediates (`ATSProvider`, `Analyzer`,
`ModelPlugin`, `TemplatePlugin`) are unaffected — they are never registered themselves.

Also additive, none of which change a specified signature:

- `PluginMeta.__post_init__` coerces `kind` from a string to `PluginKind` and any iterable
  of capabilities to a `frozenset`, and rejects a blank `name`. Representation only; no
  value is altered.
- Registry lookups are case-insensitive and whitespace-trimmed; `describe()` and `names()`
  report each name exactly as declared.
- Added beyond §6: `unregister`, `clear` (needed by `reload_all`), `is_registered`,
  `is_enabled`, `counts`, `configure`, and an async `healthcheck` aggregator for
  `GET /ready`. `describe()` keeps its exact no-argument signature.
- `registry.all(kind)` lets a plugin's constructor failure propagate as `PluginLoadError`
  rather than skipping it, so a broken provider cannot silently narrow a discovery run.

### 15. `RedisCache` degrades on a **retry window**, not permanently

The brief says an unreachable Redis should "log once and behave as a permanent miss rather
than raising". Implemented as: log once on entering degradation, answer every read as a miss
and every write as a no-op, then re-probe after `DEGRADED_RETRY_SECONDS` (30s), logging
recovery. Observable behaviour to callers is identical — never raises, always a miss while
down — but a restarted Redis is picked up without restarting the application. A missing
`redis` **package** is treated as permanent, since no retry can fix it.

### 16. `app.observability.metrics` integration is duck-typed

`applicantos_cache_events_total{namespace,event}` is emitted through a lazily-resolved
bridge that accepts either a module-level `record_cache_event(namespace, event)` callable, or
a counter object named `CACHE_EVENTS` / `cache_events` / `applicantos_cache_events_total`
exposing `.labels(namespace=..., event=...).inc()`. If the module is absent, or exposes
neither, emission disables itself silently — the cache package stands alone.

**Whoever writes `app/observability/metrics.py` should expose one of those names**, or cache
metrics will be silently absent. Events emitted: `hit`, `miss`, `set`, `delete`, `clear`,
`eviction`, `error`. A `clear(None)` is labelled `namespace="all"`. Tiered backends emit once
per logical operation, not once per tier.

### 17. Do not pass `event=` as a keyword to a structlog call

`structlog.stdlib.BoundLogger.debug/info/warning(...)` names its first parameter `event`, so
`logger.debug("cache.x", event=y)` raises `TypeError: got multiple values for argument
'event'`. This bit the cache metric bridge during implementation. §16 lists `event` among the
bound context keys, which reads as though it can be passed explicitly — it cannot. Any module
wanting to log an event *name* as data must use a different key; this module uses
`cache_event`.

---

## Phase 1 — Assembly (model exports, pydantic schemas, initial migration)

Every item below was **implemented as written** in `docs/CONTRACTS.md`. Where the contract is
silent rather than wrong, the shape chosen is recorded so a later module can match it instead
of inventing a second one.

### 18. `Page[T]` carries a derived `has_more` beyond the four frozen keys

§14 freezes `Page[T] = {items, total, limit, offset}`. `has_more` is added as a pydantic
`computed_field`, so it appears in every serialised page. It is derived from
`offset + len(items) < total` — deliberately not `offset + limit`, so a short final page and
an over-large `offset` both report correctly.

**Consequence:** `desktop/src/lib/api/types.ts` must include `has_more: boolean` on `Page<T>`.
The four contract keys are unchanged and carry identical meaning.

### 19. `UserPreferences` is *mirrored* by `PreferencesRead`, not re-exported

§5 freezes `UserPreferences` as a pydantic model living at `app.models.user`. The API needs a
`from_attributes=True` model with the same field list, and inheriting from a
`validate_assignment=True` config model would change its assignment semantics, so
`app/schemas/user.py` declares `PreferencesRead` with the identical seventeen fields and
defaults (importing the default *constants* from `app.models.user`, so the values cannot
drift). `PreferencesUpdate` is the all-optional partial, and the onboarding `preferences` step
payload **inherits** `PreferencesUpdate` rather than restating it.

**Risk:** the field *list* is stated twice. A field added to `UserPreferences` must be added
to `PreferencesRead`/`PreferencesUpdate` too. A test asserting
`set(UserPreferences.model_fields) == set(PreferencesRead.model_fields)` would close this and
is worth adding when the test suite lands.

### 20. `ResumeDocumentSchema` guesses the `Contact` field list

§11 names `Contact, ResumeEntry(title, organization, location, date_range, bullets,
fact_ids), ResumeSection(heading, entries), ResumeDocument(contact, summary, sections,
skills_line, meta)` — every field except `Contact`'s. The brief also requires the schema
mirror to be **self-contained** (no import of `app.documents`), so `app/schemas/resume.py`
had to pick a shape:

```
ResumeContactSchema: name, email, phone, location, website, github, linkedin, portfolio,
                     links: dict[str, str]
```

The four named URL slots mirror `PROFILE_LINK_KEYS` in `app/models/profile.py`; `links` is the
overflow bag. **Whoever writes `app/documents/models.py` should use this field list**, or
`ResumeVersion.content_json` will round-trip lossily. Every field of the mirror is optional
with a default, so a document written by a different (or older) shape still deserialises
rather than raising — losing only the unknown keys.

`estimated_lines()` and `total_bullets()` are implemented on the mirror as well, since §11
declares them on `ResumeDocument` and the renderer's shrink loop needs the estimate before
paying to typeset a document.

### 21. `ScoreComponent` and `ScoreRule` field lists are not specified

§10 names `ScoreRule, ScoreComponent, ScoreResult, Scorer, DEFAULT_RULES, load_rules(),
explain()` without fields. `app/schemas/scoring.py` assumes:

- `ScoreComponentRead`: `key, label, points, weight, matched, hard_negative, detail`
- `ScoreRuleSchema`: `key, label, description, points, weight, category, enabled,
  hard_negative, criteria`

`hard_negative` is the load-bearing one: §10 forbids the LLM adjustment step from flipping a
sponsorship / blocked-company / blocked-industry negative into "apply", and that prohibition
needs a flag the API can show and the engine can check. **`app/ai/scoring.py` and
`app/config/scoring_rules.yaml` should adopt these names.**

`ScoreVerdict` is pinned as `Literal["apply", "review", "skip"]` at the schema boundary,
because §3 declares no verdict enum while §10 requires a routing decision (already raised
against `app/models/score.py`). If a `Verdict` enum is ever added to §3, the literal should be
replaced by it.

### 22. Onboarding step keys and payload model names are unfrozen

§13 specifies `OnboardingService.steps() / submit_step(user_id, step, payload) /
status(user_id) / complete(user_id)` and §14 specifies `POST /onboarding/steps/{step}`, but
nothing fixes the step vocabulary. The module brief named eight steps, and those are now
frozen in code as `ONBOARDING_STEP_KEYS`, with `ONBOARDING_PAYLOAD_MODELS` mapping each key to
its payload class so the route dispatches by lookup rather than by a conditional chain:

`identity, contact, work_authorization, demographics, preferences, links, sources,
master_resume`

Only `identity` is `required=True`; everything else is skippable. `demographics` additionally
accepts `decline_all`, which sets every unanswered EEO field to `DECLINE_TO_SELF_IDENTIFY` —
without it, skipping the step leaves those form fields unanswerable and every application
routes to manual review.

### 23. `KnowledgeSearchResult` is one hit, not a whole `RetrievalResult`

§8.3 defines `RetrievalResult` as four parallel lists (facts, chunks, entities, memories),
while §14 requires every list endpoint to return `Page[T]`. Those cannot both describe
`GET /knowledge/search`, so `KnowledgeSearchResult` models a **single fused hit**
(`id, kind, score, title, text, source_uri, metadata`) and the endpoint returns
`Page[KnowledgeSearchResult]`. `RetrievalResult` remains the internal shape the retriever
returns to the resume engine; the two are not interchangeable.

### 24. Embeddings are declared-but-excluded on every read schema

`ChunkRead`, `EntityRead` and `FactRead` declare `embedding` with `exclude=True` and expose a
computed `has_embedding: bool`. Declaring the field (rather than omitting it) is what lets
`model_validate(orm_row)` populate the flag; `exclude=True` is what keeps 1536 floats per row
out of the response. No endpoint returns a raw vector.

### 25. `SettingsRead` omits `database_url` and `redis_url` entirely

The brief says "never expose API keys". Both connection URLs embed credentials, so they are
treated the same way: absent, replaced by `database_backend: "postgresql" | "sqlite" |
"other"`. `secret_key`, `sentry_dsn`, `s3_endpoint_url`, and all five API keys are likewise
absent, represented where useful by `*_configured` booleans.

`SettingsRead.from_settings()` names every field explicitly rather than copying
`settings.model_dump()`. That is the point: a secret added to `Settings` later cannot leak
through by default, because a field not listed does not exist in the response. Credentials
*are* accepted for write on `SettingsUpdate` (there is no other way to set them) and carry
`repr=False` so they cannot surface in a traceback.

### 26. Email is validated structurally, not with `EmailStr`

`UserCreate.email` / `UserUpdate.email` use `str` with a validator that trims, lowercases, and
requires a `local@domain` shape — mirroring `User._normalize_email`. `pydantic.EmailStr` was
avoided because it raises at *import* time when `email-validator` is not installed, which
would make `app.schemas` unimportable on a machine that has pydantic but not that extra. If
`email-validator` joins the dependency set, `EmailStr` can replace the validator.

### 27. The applications-to-documents foreign-key cycle is resolved per dialect

`applications.resume_version_id` and `applications.cover_letter_id` are declared
`use_alter=True` in `app/models/application.py`, so no single `CREATE TABLE` ordering
satisfies them. `0001_initial_schema.py` resolves this the same way SQLAlchemy's `create_all`
does, which is *differently per backend*: on PostgreSQL the constraints are added by
`ALTER TABLE` after all three tables exist; on SQLite (no `ALTER TABLE ADD CONSTRAINT`) they
are declared inline in `CREATE TABLE applications`, referencing tables that do not exist yet,
which SQLite permits because it resolves foreign key targets lazily.

Verified: `alembic upgrade head` followed by `alembic revision --autogenerate` produces an
**empty** migration — the migration reproduces `Base.metadata` exactly: 22 tables, 308 columns,
102 indexes, 10 unique constraints and 35 foreign keys, with no drift.

`downgrade()` mirrors this: with `ALTER` the two constraints are dropped, without it the two
*columns* are nulled instead, because SQLite refuses to drop a table that is still referenced.

### 28. The initial migration imports `app.database.types`, and `EmbeddingType()` reads settings

Migrations are normally frozen snapshots that import nothing from the application. This one
imports `GUID`, `JSONType`, `UTCDateTime` and `EmbeddingType`, because substituting literal
types would create `CHAR(36)` primary keys on PostgreSQL where the models expect native
`uuid`, and `JSON` where they expect `JSONB`.

The consequence worth knowing: `EmbeddingType()` takes its width from
`settings.embedding_dim` at *migration* time, exactly as the models do. Changing
`embedding_dim` after rows exist therefore requires a new migration **and** a re-index, and
re-running `0001` against a differently-configured environment produces a differently-sized
vector column. That is inherited from `app/database/types.py`, not introduced here.

The `vector` extension is created only when the dialect is PostgreSQL **and** the `pgvector`
Python package is importable — the latter being precisely the condition under which
`EmbeddingType` renders a `vector(n)` column at all. Guarding on the dialect alone would fail
the migration on a PostgreSQL server without the extension, for a schema that would not have
used it.

### 29. Enum columns are `VARCHAR(longest-value)`, which is tighter than it looks

The models declare enums as `sa.Enum(..., native_enum=False)`. With no explicit `length`,
SQLAlchemy sizes the column to the longest current member *value* — `VARCHAR(12)` for
`ApplicationStatus`, `VARCHAR(7)` for `WorkArrangement`, `VARCHAR(10)` for `ATSProviderName`,
`VARCHAR(19)` for `ReviewReason`. The migration mirrors those widths exactly, as named
constants.

**Consequence:** adding an enum member longer than the current maximum requires a migration
widening the column, on both backends. The two helper-built groups
(`app/models/profile.py`, `app/models/knowledge.py`) pass `length=64` explicitly and have
plenty of headroom; the five modules using a bare `sa.Enum` (`posting`, `application`,
`session`, `checkpoint`, `file`) do not. Standardising every enum column on `VARCHAR(64)`
would remove this whole class of migration, and is worth deciding before the first enum gains
a member.

### 30. `sqlalchemy.*` keys in `alembic.ini` are forwarded to `create_engine`

Recorded because it cost a debugging cycle. `alembic.ini`'s `[alembic]` section is read by
`(async_)engine_from_config` with `prefix="sqlalchemy."`, so any `sqlalchemy.<x>` key becomes
a `create_engine()` keyword argument. A `sqlalchemy.warn_20` line — valid in SQLAlchemy 1.4 —
raises `TypeError: Invalid argument(s) 'warn_20'` on 2.0 at *connect* time, not at parse time,
which makes it look like a driver fault. The file now carries no `sqlalchemy.*` key at all:
`env.py` injects the URL and nothing else.

### 31. `app/models/__init__.py` exports `MODEL_CLASSES` / `TABLE_NAMES` beyond §4

Two additions, both to make the "every model is in the migration" invariant checkable rather
than aspirational: `MODEL_CLASSES` is every mapped class in dependency order, and
`TABLE_NAMES` is their table names in that same order — which is exactly the creation order
of `0001_initial_schema.py`, and reversed, its drop order. A model added without being wired
into this package fails a single obvious assertion instead of silently vanishing from the
schema.

---

## Phase 2 — Web analyzers (`analyzers/github.py`, `analyzers/website.py`, `analyzers/_text.py`)

### 1. `used_in` edge direction: technology → project, not project → technology

The build brief for `GitHubAnalyzer` specified `(PROJECT) -[USED_IN]-> (TECHNOLOGY)` and asked
for the direction choice to be justified. It was implemented the other way round —
`(TECHNOLOGY) -[USED_IN]-> (PROJECT)` — in both `github.py` and `website.py`, for one reason
that outweighs the intuitive reading:

* §8.1 `ExtractedEdge` documents `relation` as "read subject-first" and gives *"PyTorch
  used_in PoseNet"* as its own example — subject technology, object project.
* `app/knowledge/extractors.py::extract_entities_rule_based` (already built and verified)
  emits exactly that direction for the same relationship recovered from README/page text.
* `KnowledgeEdge` is `UNIQUE(source_entity_id, target_entity_id, relation)`. Emitting the
  reverse here would not overwrite the rule-based edge, it would create a **second, parallel**
  edge for every project/technology pair — doubling the graph and breaking any
  `KnowledgeGraph.neighbors()` traversal that assumes one direction.

**Decision needed:** confirm the single direction for the whole engine, or introduce a second
relation (`RelationKind.BUILT`?) for the project→technology reading and populate both
deliberately. Until then all analyzers emit technology → `used_in` → project.

### 2. `same_origin()` ignores a leading `www.`

A browser treats `example.com` and `www.example.com` as different origins. `_text.py` treats
them as the same site, because a crawler that did not would abandon most personal sites at
the first internal link (canonical host and linked host routinely disagree on that one
label). Scheme and port remain strict. The relaxation is confined to `same_origin()`;
`normalize_url()` never rewrites a host.

### 3. `normalize_url()` strips trailing slashes, which costs one redirect on directory URLs

`/projects/` normalises to `/projects`, so the two spellings dedupe to one visited entry.
Most servers then answer `/projects` with a 301 to `/projects/`, which the client follows —
correct, but one extra round trip per directory URL. The alternative (preserving the slash)
would let a site's own inconsistent linking fetch the same page twice, which is worse for a
crawl that is rate-limited to ~1 req/s. Recorded in case the trade-off should flip.

### 4. `WebsiteAnalyzer.fingerprint()` probes the root only, per the brief

A new project page added without touching the root's bytes is not detected until the periodic
full re-index (`knowledge_reindex_interval_minutes`). Detecting it properly would mean
crawling the site to decide whether the site needs crawling. A cheap middle ground would be to
also probe `sitemap.xml`'s `lastmod` when one exists; not implemented because §8.1 defines no
such behaviour.

### 5. `HEAD` and `GET` disagreeing on validators causes one wasted re-index

`fingerprint()` composes its digest from the root's `ETag`/`Last-Modified` (`HEAD`), and
`analyze()` from the same headers on its `GET`. A server that omits `ETag` from a `HEAD` but
sends it on `GET` produces two different digests, so the source re-indexes once per run. That
is the safe direction to fail in (wasted work, never a frozen knowledge base) and no
additional request is spent to avoid it.

### 6. `SourceKind.PORTFOLIO_PAGE` defaults to crawl depth 0

`§1` defines one `website_crawl_max_depth` for both website source kinds. `PORTFOLIO_PAGE`
defaults to depth 0 (index that page, discover nothing through it) while `PERSONAL_WEBSITE`
uses the setting, on the reading that the two enum values exist precisely to distinguish "a
site" from "a page". Both are overridable per source via `KnowledgeSource.config["max_depth"]`.

### 7. Per-source analyzer options are not part of any contract

Both analyzers read options off `KnowledgeSource.config` (§4 defines the column, not its
contents): `max_repos`, `include_forks`, `include_archived`, `fetch_readme`, `fetch_manifest`
for GitHub; `max_pages`, `max_depth`, `request_interval` for websites. They exist because at
60 requests/hour unauthenticated the GitHub budget is the binding constraint on a large
account. If the desktop app is to expose them, the key names need to be contract-defined.

### 8. No HTTP timeout setting exists for the crawler

Noted already by `analyzers/base.py` (`HTTP_TIMEOUT_SETTING_NAMES`): §1 defines no
`http_timeout_seconds`, so the shared client falls back to 30s. Both analyzers inherit that.
A slow-but-alive site and a hung one are currently indistinguishable at 30s.

### 9. `selectolax` is an optional accelerator, not a dependency

`_text.py` uses `selectolax` when importable and a complete `html.parser` implementation
otherwise. Both back ends build the same tree and share one renderer, and both were verified
to produce byte-identical text on malformed markup (unclosed `<li>`, `<td>`, `<p>`). If
`selectolax` is added to the project's dependencies it should be an extra, never a hard
requirement — the fallback is not a degraded mode.

---

## Phase 2 — AI layer (`app/ai/embeddings.py`, `app/ai/prompts/`, package exports)

### 32. `app/ai/prompts/` has no contract API, and two callers want two substitution dialects

§0 lists `app/ai/prompts/` in the layout; §10 defines no API for it. The loader implements
`load_prompt(name, **vars)` over `<name>.md` with `string.Template.safe_substitute`, because
prompts are mostly JSON — schemas, worked examples, expected replies — and `str.format` would
require every one of those braces to be doubled by hand.

`app/knowledge/extractors.py` was built against the same undefined API and chose differently:
its `_load_prompt(name, default)` probes this package for a `get_prompt(name)` callable or a
module constant, then renders the result with **`str.format`**. Handing it a `$`-placeholder
file would leave `$text` unsubstituted; handing it a file full of literal JSON braces would
raise inside its `try`, silently disabling LLM extraction in favour of the regex fallback.

Resolved without changing either module's public behaviour: no `get_prompt` is defined (so
the probe falls through to the constant lookup), and a module-level `__getattr__` answers the
four names it looks for — `EXTRACT_FACTS_SYSTEM`, `EXTRACT_FACTS`, `EXTRACT_ENTITIES_SYSTEM`,
`EXTRACT_ENTITIES` — from the `.md` files. System prompts are returned verbatim (their caller
never formats them); the two user prompts go through `as_format_template`, which rewrites
`$name` to `{name}` and doubles every literal brace. Verified end to end: both user prompts
render through `.format(**keys)` with the exact keyword set `extractors.py` passes.

**The decision to make:** one substitution dialect for the whole codebase. The bridge works
and is tested, but a prompt file whose placeholder set drifts from the caller's keyword set
degrades silently to the rule-based extractor rather than failing loudly.

### 33. A purely lexical hashing embedder cannot satisfy its own acceptance test

§10 requires `HashingEmbedder` as the offline fallback, and the product requires it to be
genuinely useful — the whole knowledge engine retrieves through it when there is no API key.
The three canonical examples ("embedded firmware in C++ on STM32", "RTOS driver development
for ARM Cortex-M", "React dashboard with Tailwind") share **no token and no character n-gram**
between any pair, so unigram/bigram/subword hashing alone scores all three pairs at ~0 and
their relative order is collision noise.

The implementation therefore adds a fourth hashed feature family: a curated
`DOMAIN_LEXICON` (28 domains, ~700 terms) mapping technical vocabulary onto the semantic
neighbourhood it belongs to. Measured cosines: 0.275 for the two embedded strings, −0.025 and
0.005 against the frontend string.

**The concern:** the lexicon is hand-maintained and overlaps in *coverage* (not in purpose)
with `app.knowledge.extractors.SKILL_VOCABULARY`, which canonicalises skill names rather than
grouping them. A term added to one will not appear in the other. If a domain taxonomy is ever
added to `SKILL_VOCABULARY`, this lexicon should be derived from it instead.

### 34. `HASHING_ALGORITHM_VERSION` is part of the embedder's model identity

`HashingEmbedder.model` is `hashing-<version>-<dim>`, and that string is part of every cache
key and of the vectors' meaning in the store. **Any change to the tokenizer, the family
weights, the n-gram range or `DOMAIN_LEXICON` must bump `HASHING_ALGORITHM_VERSION`**, or
vectors from two different geometries will be mixed in one collection and every similarity
score computed against them becomes meaningless. This is a convention, not an enforced
invariant — a checksum of the algorithm's inputs would make it one.

### 35. `get_llm` degrades on a missing key; `get_embedder` also degrades on a missing SDK

`app.ai.llm.resolve_provider_name` checks only `PROVIDER_API_KEY_FIELDS`, so a machine that
has `ANTHROPIC_API_KEY` set but no `anthropic` package resolves to `AnthropicModel` and fails
at the first `complete()` with an `LLMError` — observed while testing.
`resolve_embedding_provider` additionally probes `importlib.util.find_spec`, so the same
machine falls back to `HashingEmbedder` with one warning and keeps running.

The asymmetry is deliberate here (`llm.py` is frozen and out of scope for this phase) but the
embedding behaviour is the correct one: "must work offline with zero API keys" should read
"and with none of the optional SDKs installed". Worth lifting the `find_spec` check into
`resolve_provider_name`.

### 36. `cosine` exists twice, in two layers

§10 mandates `app.ai.embeddings.cosine`; §8.2's vector layer already ships
`app.knowledge.vector.base.cosine_similarity` alongside `normalize` and `dot`. The two are
independent implementations of the same eight lines. They are *not* shared because
`app.knowledge` depends on `app.ai` (the indexer takes an embedder) and importing back the
other way would invert the layering. A neutral `app/common/vectors.py` would own both, at the
cost of a module the contract does not list.

### 37. Retry/backoff policy is implemented twice for the same reason

`app.ai.embeddings._backoff_delay` mirrors `GuardedModelPlugin._backoff_delay` and imports
its four constants (`RETRY_BASE_DELAY_SECONDS`, `RETRY_MAX_DELAY_SECONDS`,
`RETRY_JITTER_RATIO`, `MAX_RETRY_AFTER_SECONDS`) so the two cannot drift numerically. The
loop that uses it is duplicated too, because `GuardedModelPlugin` is a `ModelPlugin` and an
embedder is not a plugin at all. A free function `retry_async(fn, *, attempts, classify)` in
`llm.py` would collapse both.

### 38. `settings.embedding_dim` is one global width, but a local model's width is fixed

`EMBEDDING_DIM` defaults to 1536, which is right for `text-embedding-3-small` and wrong for
every model Ollama serves (`nomic-embed-text` is 768, `mxbai-embed-large` is 1024). Only the
`text-embedding-3` family accepts a `dimensions` request parameter, so `OpenAIEmbedder` sends
it and genuinely honours the setting, while `LocalEmbedder` cannot and a mismatch surfaces as
`EmbeddingDimensionMismatch` on the first batch — with the correct value to set in the
message. Switching providers is therefore a config change *plus* a re-index, and nothing in
the settings layer currently says so. A per-provider default width, or validation that
refuses a known-bad pair at startup, would catch it before the first index run.

### 39. The `Embedder` protocol carries no identity, but the cache key needs one

§10 defines `Embedder` as exactly one method. Cache keys must include the provider, the model
and the width, or a vector embedded by one backend would be served to another.
`embedder_identity()` reads `provider` / `model` / `dimension` as optional duck-typed
attributes and falls back to the class name with width `0`; width `0` disables caching for
that embedder rather than writing an unidentifiable key. Every built-in embedder supplies all
three via `BaseEmbedder`. Adding the three as read-only properties on the protocol would make
this checkable instead of best-effort.


## Phase 2 — Local analyzers (`project_folder`, `resume_parser`, `linkedin_export`, `document`)

### 40. `FactKind` has no `certification` member, so certifications become `AWARD` facts

§3 defines `FactKind` as `accomplishment responsibility metric skill_usage award
education_item leadership_item publication_item`, while `EntityKind` *does* carry
`certification`. A resume's CERTIFICATIONS section and a LinkedIn `Certifications.csv` row
therefore emit a first-class `certification` **entity** and an `award` **fact**.

**The concern:** "IPC-A-610 Certified Specialist" and "Eagle Scout" are not the same kind of
claim, and a resume template that wants a separate *Certifications* block cannot currently
tell them apart from the fact alone — it has to join back to the entity. Either a
`certification` `FactKind` or a documented convention (e.g. an `award` fact whose
`organization` is the issuing authority, which is what is implemented) should be decided
before the first template renders one.

### 41. `DocumentAnalyzer` claims source kinds it does not declare, as a last resort

§8.1 describes `DocumentAnalyzer` as handling "`readme` / `documentation` / `blog_post` /
`interview_note` / generic". The first four are declared in `source_kinds`; "generic" is
implemented by overriding `supports()` to additionally accept **any** source whose uri is an
`http(s)` URL or a file with a readable suffix — so a `video` or `user_correction` source with
a real file behind it is indexed rather than raising `PluginNotFound`.

**The concern:** this is safe only because `analyzer_for` ranks candidates by
`len(source_kinds)` ascending. `DocumentAnalyzer` declares five kinds — more than any other
analyzer in the package — so it always loses to `ResumeParser` (2), `GitHubAnalyzer` (2),
`WebsiteAnalyzer` (2), `ProjectFolderAnalyzer` (1) and `LinkedInExportAnalyzer` (1). **A future
analyzer that declares six or more source kinds would silently outrank the fallback and
change resolution for every kind they share.** The ordering rule is load-bearing and is not
expressed anywhere as an invariant.

### 42. The `.gitignore` subset stops where repository state begins

`ProjectFolderAnalyzer` parses `.gitignore` itself rather than shelling out to git, so a
folder scans identically on a machine with no git installed. Glob semantics, `!` negation,
trailing-`/` directory-only rules, leading/middle-`/` anchoring, `**` in all three positions,
character classes, backslash escapes and nested per-directory files are all implemented and
unit-checked.

**Not implemented, deliberately:** `.git/info/exclude`, the global `core.excludesFile`, and
`.gitattributes`-driven exclusions. Each requires resolving configuration outside the folder
the user pointed at — a global excludes file lives in `$XDG_CONFIG_HOME` and can name paths
that have nothing to do with this project — and consulting them would make a scan's result
depend on machine state the user cannot see from the ApplicantOS UI. The consequence is that
a file ignored *only* by `.git/info/exclude` is indexed.

### 43. `ExtractedDocument` has no `summary`, but `KnowledgeDocument` does

§4 gives `knowledge_documents` a `summary` column and §8.1 gives `ExtractedDocument` the
fields `uri / title / text / kind / metadata / content_hash` — with no summary. Analyzers that
derive one (the project README's opening paragraph) therefore put it on the `PROJECT` entity's
`summary` and in the document's `metadata`, and `KnowledgeDocument.summary` is left for the
indexer to fill.

**The concern:** the indexer will have to re-derive or re-generate a summary that the analyzer
already computed, from text the analyzer already had in hand. Adding `summary: str | None` to
`ExtractedDocument` would remove a duplicated LLM call per document.

### 44. The shared file-to-text readers live in `analyzers/document.py`

`ResumeParser`, `LinkedInExportAnalyzer` and `ProjectFolderAnalyzer` all need to turn bytes
into text, and the brief requires the PDF/DOCX paths to be factored into shared helpers rather
than duplicated. §0 lists no module for that, so `read_pdf_text`, `read_docx_text`,
`read_html_text`, `decode_bytes`, `extract_text_from_path`, `extract_text_from_bytes`,
`fetch_text_from_url`, `file_probe_fingerprint`, `local_path_for` and `knowledge_extractor`
are public in `app/knowledge/analyzers/document.py`, and the sibling analyzers import them
from there.

**The concern:** this reads oddly — `project_folder` importing from `document` — and the two
responsibilities (generic analyzer, shared reader) are only together because §0 defines no
third file. An `analyzers/_files.py`, alongside the existing `analyzers/_text.py`, would be
the natural home. Golden rule #5 is not violated: nothing *outside* `app/knowledge/analyzers/`
imports either module.

### 45. A manifest dependency can canonicalise to a surprising vocabulary name

`ProjectFolderAnalyzer` runs every declared dependency through
`extractors.canonical_skill`, so `torch` in a `requirements.txt` and `PyTorch` in a README
merge into one graph node — which is the point. But `SKILL_VOCABULARY` lists `pgvector` as an
alias of **PostgreSQL**, so scanning this repository emits a `PostgreSQL` technology entity
whose `version` attribute is `>=0.2` — the pgvector package's version, attached to a node
named after the database.

**The concern:** aliasing is right for prose ("we used pgvector") and wrong for a manifest,
where the string is a package identity rather than a mention. Either dependency names should
skip canonicalisation, or the alias should carry the original name (it does, in `aliases`) and
the `version` attribute should not be copied onto a canonicalised node. Implemented as
written — the vocabulary is frozen substrate.

### 46. Oversized files are fingerprinted and counted but never read

`settings.project_scan_max_file_bytes` (256 KB) is described only as a cap. A file above it is
still recorded in the scan — so it contributes its path, size and mtime to the folder
fingerprint, and its extension to the language histogram's `files` count — but it is not
opened, so it contributes no lines of code and no text, and the analysis result carries an
error line naming how many were skipped.

**The concern:** the alternative reading is that an oversized file should be excluded
entirely. The implemented reading is the safer one for change detection (editing a 1 MB
generated header still marks the project dirty), but it means `languages[lang]["files"]` and
`languages[lang]["lines"]` are counted over different populations. Both numbers are reported;
neither is wrong; the asymmetry is worth knowing about before a UI charts them together.

### 47. `KnowledgeStore` lives in `graph.py` because §0 defines no shared module

`KnowledgeGraph`, `FactStore` and `MemoryStore` all take `(session, *, embedder=None,
vector_store=None)` and all need the same two things: lazy resolution of the embedder and the
vector store, and a documented degradation to SQL-only behaviour when either is missing. That
substrate is implemented once as `KnowledgeStore` in `app/knowledge/graph.py`, and `facts.py`
and `memory.py` import it from there — along with `chunked`, the `IN (...)` batching helper
that keeps every query in the package inside SQLite's bound-parameter limit.

**The concern:** `memory.py` importing from `graph.py` reads oddly; the dependency is real
only in the "shared base class" sense, not the "memories are part of the graph" sense. §0
lists no module that would be the natural home (an `app/knowledge/_store.py`, alongside the
existing `analyzers/_text.py`, would be). The alternative — duplicating the resolution and
degradation logic three times — was worse, and creating `app/knowledge/__init__.py` was not an
option, because that file is outside this agent's scope and `indexer.py` / `retrieval.py` are
being built in parallel.

### 48. `KnowledgeGraph` accepts an embedder and a vector store it never uses

`KnowledgeEntity` has an `embedding` column, described in §4 as being for "similarity
expansion", but §8.3's `KnowledgeGraph` API has no method that would produce one — and
`upsert_entity` embedding each entity as it arrives would mean one embedding call per entity,
precisely the per-item pattern the rest of the engine is built to avoid. Entities are
therefore never embedded by this module; the column stays `NULL`.

**The concern:** either something (most plausibly `KnowledgeIndexer`, which already batches)
should embed entity names after an index pass, or `KnowledgeEntity.embedding` is dead weight
and graph expansion in `retrieval.py` will have to work purely structurally. The constructor
accepts the collaborators today so that adding a batched `embed_entities()` later needs no
signature change.

### 49. The 0.93 cosine threshold and the offline embedder disagree about "near duplicate"

§8.3 fixes near-duplicate merging at cosine ≥ 0.93, and `FactStore` implements it. Measured
against the offline `HashingEmbedder`, though, that threshold only catches near-*verbatim*
variants. Real pairs, measured on this machine at `EMBEDDING_DIM=256`:

| Pair | Cosine |
|---|---|
| "…quadruped controller, using FreeRTOS." / "…quadruped controller with FreeRTOS." | 0.951 |
| "Reduced p95 API latency 38%…" / "Reduced the p95 API latency by 38%…" | 1.000 |
| "Cut control-loop jitter by 43% … using FreeRTOS." / "Reduced control loop jitter 43% … with FreeRTOS." | 0.759 |

The third pair is the same accomplishment as written in a README and in a resume, and it will
produce two rows on a zero-API-key install. With `text-embedding-3-small` it would very likely
clear 0.93.

**The concern:** deduplication quality is therefore a function of which embedder is
configured, and the offline install — the one the product promises works — is the weaker one.
Either the threshold should be provider-dependent, or `HashingEmbedder` needs a synonym layer
(its `DOMAIN_LEXICON` already does this for topics, not for verbs). Implemented as written;
0.93 is a frozen contract value.

### 50. Merging a fact rewrites its `content_hash`, so the next re-index misses the fast path

A near-duplicate merge widens the date range (earliest start, latest end), and `date_start` /
`date_end` are inputs to `KnowledgeFact.build_content_hash`. `FactStore` therefore calls
`refresh_derived_fields()` after a merge, because a stale hash would silently stop
deduplicating that claim. The consequence is that the *original* fact's hash no longer matches
any row: re-indexing the same unchanged source falls through the exact-hash pass into the
embedding-and-cosine pass, which then correctly merges it (verified — re-ingesting an
identical batch creates zero rows). It costs one cached embedding and one vector probe per
fact.

**The concern:** the cheap path advertised by §8.3 — "dedupe by content_hash" — quietly stops
being the path taken for any fact that has ever been merged. Recording the set of contributing
hashes on the row would restore it, but §4 defines no column for that.

### 51. `EmbeddingType` stores a missing embedding as JSON `null`, not as SQL `NULL`

`EmbeddingType` resolves to pgvector's `vector` on PostgreSQL and to `JSON` everywhere else —
including on PostgreSQL without the extension package. SQLAlchemy's `JSON` persists a Python
`None` as the JSON literal `null` (the four-character string) unless the column is declared
`none_as_null=True`, which `app/database/types.py` does not do. Verified on the SQLite
install: an unembedded fact's column holds `'null'` with `typeof() = 'text'`, and
`WHERE embedding IS NULL` matches **nothing**.

Written the obvious way, `reembed_missing` would report "no backlog" forever while never
embedding a row, and `stats()` would count every unembedded fact as embedded. `FactStore`
therefore asks the bound dialect which implementation the column actually resolved to
(`FactStore._embedding_is_json`) and builds the matching predicate.

**The concern:** every other query in the codebase that asks "does this row have an
embedding?" — `KnowledgeChunk` in the indexer, the `knowledge.embed_backlog` worker, the
`KnowledgeStats.chunks_embedded` figure — has the same trap, and the natural spelling is the
broken one. Adding `none_as_null=True` to `EmbeddingType` would fix it in one place for
everybody, at the cost of being unable to store a literal JSON null, which nothing in this
schema wants.

### 52. `upsert_edge` deliberately does not count its endpoints as mentions

`KnowledgeEntity.mention_count` is the prominence signal the resume engine ranks bullets by.
The indexer upserts every `ExtractedEntity` and *then* every `ExtractedEdge`, and analyzers
routinely emit both for the same technology, so counting an edge's reference as well would
double every count. `KnowledgeGraph._resolve_endpoint` therefore find-or-creates without
incrementing, and an entity that exists *only* because an edge named it is created at
`EDGE_ENDPOINT_CONFIDENCE` (0.4) with `mention_count = 1`.

**The concern:** this couples the graph's arithmetic to the indexer's call order. An analyzer
that emits edges without the corresponding entities produces nodes that look weak, and one
that emits an entity twice plus an edge produces a count of 2 rather than 3. §8.3 does not say
what `mention_count` counts.

### 53. Methods this build required that §8.3 does not list

§8.3 defines `KnowledgeGraph` through `stats`, `FactStore` through `verify`, and `MemoryStore`
through `prune_expired`. The build brief additionally required `KnowledgeGraph.prune`,
`FactStore.by_ids` / `update_text` / `stats` / `reembed_missing`, and
`MemoryStore.record_preference` / `reinforce` / `forget` / `as_prompt_context`. All are
implemented as additions; nothing §8.3 states was changed, and every signature it does state
stays call-compatible (the extra `*, embedder=None, vector_store=None` on
`KnowledgeGraph.__init__` are keyword-only with defaults, so `KnowledgeGraph(session)` works
exactly as written in the contract).

**The concern:** §8.3 should list them, or a caller reading only CONTRACTS.md will not know
they exist. Two return types were unspecified and had to be chosen. `FactStore.upsert_many ->
int` returns the number of extracted facts that *reached* the store — inserted **plus** merged
— rather than the number of new rows, because "rows created" is zero on every re-index of
unchanged content and would make `IndexReport.facts` read as a failure. `deactivate` /
`verify` / `update_text` return the updated row (or `None`) rather than a bool, since the
caller almost always needs it.

### 54. Memories deduplicate on exact text, and `record_outcome` reads three application tables

`MemoryStore._record` treats `(user_id, kind, text)` equality as "the same lesson again" and
reinforces the existing row by `REPEAT_REINFORCEMENT` instead of inserting. A user who makes
the same edit three times gets one memory weighted 3.0 rather than three memories — emphasis
rather than volume, which is what stops one repeated correction from dominating every
retrieval.

Separately, `record_outcome` resolves the company name and job title through
`applications → companies / job_postings` at write time and writes them into the memory's
text, so the lesson ("robotics firmware roles at hardware companies convert") outlives the
posting being expired and deleted.

**The concern:** the second makes `app/knowledge/memory.py` import from
`app.models.application`, `app.models.company` and `app.models.posting` — the only place the
knowledge engine reaches into the application-tracking half of the schema.
`AnalyticsService.what_gets_interviews` will want exactly this data and may prefer to own the
join, in which case `record_outcome` should take the description rather than derive it.

---

## Findings from the evolveagent-ai research pass (2026-08-07)

### A. Prompt injection via job descriptions — REAL EXPOSURE, spec'd in CONTRACTS §10b
Job descriptions are attacker-controlled text piped into `ResumeEngine.tailor`,
`CoverLetterWriter.write` and `FieldAnswerer.answer`. The fact-id validator defeats the
fabrication half of the attack on resumes (an invented degree has no `KnowledgeFact` behind
it), but `FieldAnswerer` emits free text with no equivalent backstop. Needs
`app/ai/untrusted.py` and four call sites. **Owner: Phase 5 follow-up.**

### B. `CheckpointService` is spec'd but not implemented — golden rule 8 has no runtime
`app/models/checkpoint.py` exists with a good design (unique `key` as the idempotency
mechanism, `attempt`, `resumable`, and a `COMPENSATED` status distinguishing deliberate
rollback from failure). But there is no `app/services/checkpoint_service.py`, and nothing
outside `app/models/` imports `Checkpoint`. **Owner: Phase 6.**

Add alongside it a module-level ordered step declaration per owner kind, e.g.
`APPLY_STEPS = ("score", "retrieve", "tailor", "render", "fill", "verify", "submit")`, so the
desktop app can render "5 of 7" rather than "the rows that happen to exist". A tuple, not a
graph — the moment it becomes a data-driven registry we have rebuilt a workflow engine we do
not need for a fixed linear sequence.

### C. `ReviewService.dismiss` loses the negative signal
`resolve()` feeds `MemoryStore.record_correction`, so "here is the right answer" is captured.
`dismiss()` writes a note to `Application.notes` and nothing else, so "I don't want jobs like
this" is discarded. `MemoryEntry.kind` already has a `feedback` value. Cheapest real
self-improvement win available. **Owner: Phase 6.**

### D. No USD cost surface
We count real tokens (`usage.input_tokens` from the provider) and enforce a real daily budget,
which is more than most systems do — but there is no `cost_usd` anywhere. A per-model
input/output rate table turns the existing `token_usage` JSON on `RunSession` and
`ResumeVersion` into a number the user understands. **Owner: Phase 8.**

### E. Second-opinion retry before escalating to a human
When `FieldAnswerer` falls below `min_answer_confidence`, try a *different* model plugin before
routing to `NEEDS_REVIEW`. Human interrupts are the real cost in this product. Gate behind a
setting, default off, and fire it only where the alternative is interrupting the user — never
as a general quality pass, since it doubles tokens on exactly the expensive requests.
**Owner: Phase 6, optional.**

### F. What NOT to take from evolveagent-ai
Task DAG (their `depends_on` has 2 writers, 0 readers; our pipeline is a fixed linear
sequence), the agent registry (4 hardcoded classes that all run on every task; `find_capable`
is substring matching with an alphabetical tiebreak), the 6-call multi-agent fan-out (no
measured quality gain, 6x token cost), the LLM judge (never calls an LLM — scores by output
length and whether the text contains "##"), `DurableWorkflowService` (simulated execution,
checkpoints with zero readers, `resume_run` that cannot resume a crash), and their usage ledger
(flat $0.002/call, `response.usage` discarded).

### G. Three zero-caller subsystems (verified 2026-08-07)
- `MemoryStore.as_prompt_context` — fully implemented, 600-token budget, graceful truncation,
  **zero call sites**. Memory is recorded, embedded, ranked, attached to `RetrievalResult`, and
  dropped. `MemoryStore.reinforce()` likewise has zero callers.
- `ApplicationVerifier` — no caller outside its own module. Better designed than anything in
  the researched repo; the gap is wiring, not design.
  **Resolved 2026-08-09 — `app/browser/apply.py`.** `run_apply` is the apply driver
  `app.jobs._apply.run_browser_apply` resolves by name, and it calls
  `ApplicationVerifier.verify` after every real submit click. The mapping is asymmetric:
  `confirmed` → `CONFIRMED` with the confirmation id, text and evidence screenshot; an error
  marker → `FAILED` quoting the page; `inconclusive` → `NEEDS_REVIEW` /
  `VERIFICATION_FAILED`. Keyed off `AutoFiller.submit_clicked` rather than the return value,
  so a dry run verifies nothing and takes no evidence capture. Covered by
  `tests/test_apply_driver.py`.
- `CheckpointService` — spec'd in CONTRACTS §13, never written.

### H. Wiring memory into prompts is unsafe without a PII screen first
`ReviewService._remember` stores the human's literal answer to a form field. A reviewer typing
an SSN, DOB or salary creates a `MemoryEntry` whose text is that value. `redact_secrets` now
scrubs values as well as key names, but it is still log-scoped and its value patterns target
addresses, credentials and opaque blobs — it would not catch a bare SSN or salary in a memory
body, and it does not run on prompts at all. Screen before injecting. Inject into the **system** prompt as style/preference
constraints only, never into `$facts`, so golden rule 7 and the resume validators stay
authoritative. `field_answer.py` is the safer first target.

### I. Documentation honesty fix applied
`docs/SAFETY.md` claimed the kill switch "is verified by a test" and that the redaction fix "is
now regression-tested". Neither was true — there is no `tests/` directory; both were verified
with throwaway scripts. Both claims now state the real status and carry a note to delete when
the test phase lands. Full analysis in `docs/RESEARCH_EVOLVEAGENT.md`.

### J. `AutoFiller.fill` skips FILE fields entirely (note for the apply driver)
`fill()` `continue`s on `FieldKind.FILE`, so a required resume upload appears in **neither**
the `filled` nor the `needs_review` list. Not a defect today — no caller exists — but the
driver that wires the browser layer to `Pipeline.submit` must reconcile file fields from
`discover_fields()` output itself and route an `UploadFailedError` to
`ReviewReason.FILE_UPLOAD_FAILED`. Iterating only `needs_review` would submit an application
with no resume attached.

**Handled 2026-08-09 — `app/browser/apply.py::plan_documents`.** The driver reads the file
fields off `discover_fields()` itself and assigns a document to each: cover-letter slots
first, then resume/CV slots, then a single unlabelled slot (which on a job application is the
resume — but only once, so a second unlabelled slot is not given the resume again). A
*required* slot with no document, a document whose path no longer exists, and an
`UploadFailedError` all become `FILE_UPLOAD_FAILED` with the offending field in
`unanswered_fields`; an *optional* one is left empty. `fill()` itself is unchanged, because
the confidence machinery has nothing to say about a file input.

---

## Phase 7 — Untrusted text (`app/ai/untrusted.py`) and the memory PII screen

`docs/CONTRACTS.md` §10b implemented, plus the screen `RESEARCH_EVOLVEAGENT.md` gap #2 makes a
prerequisite for wiring memory into prompts. Measured on the corpus in `tests/test_untrusted.py`
(25 genuine job descriptions, 12 real-shaped injections): **block precision 1.00, block recall
1.00, genuine-posting flag rate 0.00.**

### 55. §10b's signature can be ignored by a call site, so a raising form was added

§10b specifies `sanitize_external_text(...) -> tuple[str, InjectionVerdict]` returning `""` at
`HIGH`. Implemented exactly as written — but an empty string is *silently ignorable*, and the same
section says the caller "routes to `NEEDS_REVIEW` with `ReviewReason.POLICY_BLOCK`, never
sanitize-and-hope". A contract whose safety property depends on every call site remembering to
check a second return value will eventually be violated by a call site that forgets.

So `sanitize_or_raise()` and `UntrustedContentError` sit beside it, and the exception carries
`review_reason = ReviewReason.POLICY_BLOCK` as a class attribute. All prompt-building call sites
use the raising form; `sanitize_external_text` remains public and is what `FieldAnswerer` uses,
because that one genuinely cannot raise (below). **Additive: no contract name changed.**

### 56. `InjectionRisk` is a `StrEnum`, not `(str, Enum)`

§10b writes `class InjectionRisk(str, Enum)`. Every enum in `app/models/enums.py` is a `StrEnum`,
which is the same thing with a better `__str__`, and `ruff` flags the two-base form (`UP042`).
Followed the codebase, not the letter.

### 57. `FieldAnswerer.answer` refuses with a plan, not an exception

The other three call sites abandon one document when they refuse. A form has twenty fields, and
raising out of `answer()` would abandon the other nineteen mid-fill — the browser would leave a
half-populated form behind, which is worse than either finishing or not starting.

So a `HIGH` field returns `AnswerPlan(value="", confidence=0.0, source=SOURCE_BLOCKED)`, and
`AutoFiller.fill` promotes that to the new `BLOCKER_UNSAFE_CONTENT`, which
`review_reason_for` maps to `ReviewReason.POLICY_BLOCK` **ahead of captcha** — a page carrying an
injection is not a page this system finishes on its own. The contract's outcome is unchanged; only
the mechanism differs, and it reaches the same enum value.

### 58. Options are screened for *risk* but never rewritten for *submission*

`FieldAnswerer.coerce_to_options` compares against `field.options` byte for byte and the browser
must click the real option. So the screen reads the options to score them, and `_prompt` renders
the screened text, but `field.options` itself is never mutated. A `MEDIUM` option therefore reaches
the model with its offending span removed and coerces back to the original by fuzzy match — or
fails to, and the field goes to a human, which is the safe direction.

### 59. `ResumeEngine.prefilter` screens *before* its own early returns

§10b names `prefilter` and `tailor` as call sites. The screen is placed ahead of the `user_id is
None` and empty-posting guards rather than after them: with the original ordering, a request whose
profile had no `user_id` returned `[]` and `tailor` degraded to `fallback_tailor` **without ever
screening the posting**. Nothing reached a model on that path, so it was not exploitable — but the
safety property was resting on an ordering that looked like an optimisation, and the next edit to
those guards would have silently broken it.

### 60. `KnowledgeExtractor.extract` needed a `source_kind` it was never given

§10b screens the extractor "when the source kind is `personal_website` or `portfolio_page`", but
`extract()` receives only `organization`, `role` and `source_uri`. Added `source_kind` as a
recognised `context` key (ignored when absent, like every other key) and passed it from
`WebsiteAnalyzer`. Local sources — a résumé, a LinkedIn export, a project folder — are deliberately
**not** screened: that is material the user handed over, and an adversary who can write to the
user's own disk has already won.

### 61. The PII screen is not in `CONTRACTS.md`, and reports rather than redacts

`contains_pii(text, *, allow=…) -> PiiVerdict` lives in `app/ai/untrusted.py` because it shares
the normalisation pipeline. It is not in any contract section; record it here.

It never rewrites. Redacting a memory leaves a lesson that no longer parses — "Preferred wording:
`***`" teaches nothing — so the two honest outcomes are "use it" and "leave it out", and only the
reader knows which. `MemoryStore._record` therefore *stamps* `context["pii"]` and logs
`memory.pii_detected`; the agent that wires `as_prompt_context` into a prompt is expected to skip
any entry carrying that stamp.

Two consequences worth stating:

* `MemoryStore` gained an `__init__` (it previously inherited `KnowledgeStore`'s) to hold a
  per-user contact allow-list cache. One small query per user per store instance resolves
  `User.email` and `UserProfile.phone`; a read failure yields an empty allow-list, which only makes
  the screen *more* cautious.
* Allow-listing applies to `EMAIL` and `PHONE` only. There is no configuration under which a
  Social Security number belongs in a prompt, so `_ALLOWLISTABLE` excludes every other category.

### 62. A bare calendar date is treated as a possible date of birth

A `MemoryEntry` body is `"Rejected wording: … / Preferred wording: …"`; the field label the human
was answering lives in `context`, not in the text that would be pasted into a prompt. So a
reviewer's typed date of birth arrives as a bare `1987-04-12` with nothing to identify it.

`DATE_OF_BIRTH` therefore fires on a *complete* day-month-year date whose year implies an age
between 16 and 120, as well as on any labelled form. "Graduated May 2018" has no day and never
matches; "shipped 2024-11-03" is too recent. This is a deliberate over-reach in the safe
direction — the cost of a false positive is one excluded memory, and the cost of a false negative
is a date of birth in a prompt.

### 63. Signals that could plausibly fire on a real posting are worth less than `MEDIUM`

The calibration rule behind `INJECTION_SIGNALS`, stated because it is what keeps the
false-positive rate at zero and it is invisible from the weights alone:

* a signal that *can* fire on a genuine posting scores below `MEDIUM_RISK_SCORE` alone, so it can
  never block anything by itself (`instruction_density`, `hidden_markup`, `encoded_blob`,
  `task_directive`, `output_directive`);
* a signal that *cannot* — a chat-template delimiter, a bidi control character, an explicit
  instruction override, a request to email "this prompt" somewhere — reaches `HIGH_RISK_SCORE`
  alone, because waiting for corroboration means shipping a known injection.

The corpus exists to keep this honest, and it deliberately contains the cases that break naive
detectors: an AI-engineering role that talks about designing system prompts and evaluating model
outputs, an SRE role whose bullets say "respond to incidents", "the ideal candidate has", "please
do not include a cover letter", a security role that mentions base64 and hex, and a posting written
in fullwidth punctuation. **Any new signal must be added with a corresponding genuine posting that
it must not flag.**

---

## Phase 8 — Memory in the prompt (`app/ai/memory_prompt.py` and its four call sites)

Closes the first bullet of finding **G** and gap **#2** of `docs/RESEARCH_EVOLVEAGENT.md`:
`MemoryStore.as_prompt_context` and `MemoryStore.reinforce` had zero callers, so everything the
system learned from the user was recorded, embedded, ranked, attached to `RetrievalResult` — and
dropped. Both now have callers, behind the PII screen gap #2 makes a prerequisite.

Wiring, in the order the research doc asks for it:

* `FieldAnswerer._llm_answer` injects into the **system** prompt (`field_answer.system.md`,
  `$memories`). The safer first target, because a memory here is "last time you told me my notice
  period is four weeks" and that is exactly what should decide a free-text answer.
* `ResumeEngine._complete` injects into the **system** prompt (`resume_tailor.system.md`,
  `$memories`) as style constraints only, never into `$facts`.
* `Pipeline.prepare` reinforces on the `ready` branch; `FieldAnswerer.answer` reinforces a field
  whose plan cleared `settings.min_answer_confidence`.
* `ReviewService.dismiss` records a `feedback` memory (finding **C**).

### 64. A memory counts as supporting material for `numbers_in` — in `field_answer` only

`FieldAnswerer._llm_answer` rejects any number the supporting material does not contain, and the
memory block is now part of that material. Without this the feature does not work at all: the
user's correction says "four weeks", the model answers "4 weeks", the guard reads `4` as invented,
and the system re-asks — forever — the one question the user has already answered by hand.

The justification is narrow and does not generalise. Every memory in the block is *something the
user themselves typed*, recorded by `ReviewService._remember` when a human resolved this exact
kind of question. Quoting it back is quotation, not invention. **The résumé path does not do
this**: there, `numbers_in` is checked against the source *fact*, a memory is not in the fact
list, and golden rule #7 is unchanged.

### 65. Reinforcement is asymmetric on purpose: reward on clean, nothing on escalation

`reinforce_used` is only ever called with a positive delta. A field escalates for a hundred
reasons — a model outage, an option that matched nothing, a question nobody could answer — and
docking a memory for all of them would drive good corrections to `MIN_MEMORY_WEIGHT` and silence
the loop this product runs on. "Earns nothing" is the whole penalty.

`REINFORCE_CLEAN_DELTA` is 0.25, half of `REPEAT_REINFORCEMENT`: a user repeating an edit by hand
is much stronger evidence than one application not needing them.

### 66. The reward is placed where the outcome is known, which is two different places

The résumé path reports its injected ids out of `ResumeEngine.tailor` on `TailorResult.memory_ids`
and `Pipeline.prepare` spends them **after** the `ready` transition — every earlier return from
that method is an escalation or a failure. The field-answering path settles inside
`FieldAnswerer.answer`, after `coerce_to_options`, because that is the first moment the plan is
final; the browser layer holds no session and could not reach a `MemoryStore` if it wanted to.

**Consequence worth stating:** `app/browser/apply.py` builds its resolver with
`FieldResolver.for_user(ctx.user, ctx.answers, llm=llm)` and passes no `knowledge=`, because
`ApplyContext` deliberately carries DTOs and no session (§12). Until that argument is supplied,
the *field-answering* memory path — and the fact-grounding path beside it, which has the same
gap and predates this work — is exercised only by callers that construct a `FieldAnswerer`
themselves. Giving the browser layer a session is a §12 decision, not a wiring fix, so it was not
taken here.

### 67. Which memories were injected is derived by counting rendered lines

`as_prompt_context` truncates at the first entry that would breach its 600-token budget, and
returns only a string. `MemoryBlock.memory_ids` therefore takes the surviving prefix by counting
lines (`header + one per memory`) rather than by re-deriving `MEMORY_PROMPT_BULLET`. A duplicated
format that drifted would credit the wrong memories; a line count that drifted credits none, which
is the safe direction.

Crediting an entry the budget dropped would be worse than not crediting it at all: the model never
saw it, so the outcome says nothing about it.

### 68. The screen honours the recorded stamp *and* re-screens the body

Item 61 says the reader is expected to skip any entry carrying `context["pii"]`. `_screen` does
that **and** calls `contains_pii` again with the caller's allow-list, because the stamp did not
exist for memories written before Phase 7 and because a repeat write merges contexts. Either test
excludes.

The cost is a memory that was stamped under an empty allow-list (a failed profile read at record
time) stays excluded even once the allow-list resolves. One quiet memory versus a value that
should never have been in a prompt; taken deliberately in the cautious direction.

### 69. `ResumeEngine._cache_key` gained a component and an argument

The rendered memory block is now in the key (golden rule #9): a correction the user made this
morning must not be answered from a résumé generated before they made it, and that staleness would
look exactly like the system ignoring them. The block is keyed by its **text**, not by its ids — an
id set that survives a reinforcement is the same prompt, and an entry the budget dropped never
reached the model.

`_cache_key` is private with one production call site, so the third parameter is required rather
than defaulted; `tests/test_golden_cache.py` passes `MemoryBlock()` and now parametrises the new
component, so the golden test proves it participates.

### 70. `Pipeline._generate_documents` reports the ids in its summary, not as a second return value

`MEMORY_IDS_SUMMARY_KEY` rides in the summary dictionary, so the `ready` event records which
lessons shaped the document — provenance a user asking *why does it word things this way* needs —
and so the method's signature stays the one `tests/test_golden_no_fabrication.py` monkeypatches.

### 71. `ReviewService.dismiss` writes its memory before the transition, like `resolve` does

`MemoryStore` flushes and never commits, and `ApplicationService.transition` is what commits. So
the memory has to be recorded *before* the transition or it is never persisted. That is the same
ordering `resolve` uses and the same exposure the `Application.notes` mutation directly above it
already has: a transition that raises leaves an uncommitted memory in a session the caller is
about to roll back.

---

## Phase 9 — G10: the `parser` plugin kind, and a sweep for other decoration

### 72. `PluginKind.PARSER` is removed — document reading is not an extension point

`parser` was one of the declared plugin kinds and had **zero implementations**. Nothing ever
called `registry.get(PluginKind.PARSER, ...)` or `registry.all(PluginKind.PARSER)`; the only
references anywhere were the enum member itself and its `ENTRY_POINT_GROUPS` row. That is the
decorative registry `docs/RESEARCH_EVOLVEAGENT.md` criticises in the researched repo, so it was
removed rather than shipped.

**Implementing one was considered first and rejected on evidence.** The thing a `ParserPlugin`
would abstract already exists exactly once and is already shared:
`app/knowledge/analyzers/document.py` holds `read_pdf_text`, `read_docx_text`, `read_html_text`
and the single dispatcher `extract_text_from_bytes`, and **both** consumers —
`DocumentAnalyzer` and `ResumeParser` — import them by name from that one module. There is no
duplication to remove, so Option B would have created an abstraction over code that is already
factored.

Worse, it would have created one that cannot express the dispatch it replaces.
`extract_text_from_bytes` picks a reader by **inspecting the bytes** — the `%PDF-` and ZIP
magic numbers beat both the filename suffix and the server's `Content-Type`, deliberately,
because a portfolio serving `resume.pdf` as `application/octet-stream` is entirely normal. A
registry keyed by `(kind, name)` has no way to say that. Routing through it would mean either
re-implementing the sniffing at the call site to pick a plugin *name*, or replacing one
`if/elif` chain with an "ask every registered parser whether it claims these bytes" loop:
strictly more machinery, identical behaviour, and — with `document.py` the only producer — one
implementation behind it.

`app/documents/{docx,html,markdown,latex}.py` were checked and are the *write* path
(`TemplatePlugin` renderers). They share nothing with the readers. `docs/ROADMAP.md` names no
parser-shaped extension point either; the resume reader it lists is one of the six
**analyzers**.

**The rule that survives:** a new *format* is a branch in `extract_text_from_bytes`; a new
source of *knowledge* is an `analyzer`. Golden rule #5 is untouched — `document.py` is inside
`app/knowledge/analyzers/`, and its readers are imported within that package only.

Changed together, because §17 freezes the enum vocabulary across three files:
`app/models/enums.py`, `desktop/src/lib/api/types.ts`, `docs/CONTRACTS.md` §6 — plus
`app/plugins/loader.py` (`ENTRY_POINT_GROUPS`), the `app/plugins/base.py` and
`app/plugins/registry.py` module docstrings, `docs/ARCHITECTURE.md` and `CLAUDE.md`.
`pyproject.toml` needed no edit: this distribution declares **no** entry points of its own —
the groups exist for third-party distributions only.

**No migration is required.** `PluginKind` is not a database column. It appears only in
`PluginMeta.kind` and in `PluginRead.kind` on `GET /settings/plugins`, both derived at
runtime from registered classes. No stored row could ever have held `"parser"`.

**Corrected in passing:** §6 said "Five kinds: `provider`, `model`, `template`, `parser`,
`analyzer`" while the enum has carried `tracker` since §17 landed — so the contract listed a
kind with no implementations and omitted one with a real one (`EmailTracker`). §6 and the
documented form of `ENTRY_POINT_GROUPS` now both read `provider`, `model`, `template`,
`analyzer`, `tracker`.

### 73. Enum parity between Python and TypeScript was never actually tested

§17 requires every enum vocabulary to be byte-identical in `app/models/enums.py` and
`desktop/src/lib/api/types.ts`, and **nothing enforced it**. Removing `PARSER` from one file
and forgetting the other would have left `ruff`, `mypy`, `pytest`, `npm run typecheck` and the
smoke test all green, and broken only at runtime in a client that has no schema to complain
with.

`tests/test_models.py` now parses every `export const NAME = [...] as const;` block out of
`types.ts` and asserts, per enum, that the values **and their order** match. A second test
asserts the `MIRRORED_ENUMS` table covers every `StrEnum` declared in `app/models/enums.py`,
so adding an enum without a mirror fails rather than passing silently. All 22 enums pass,
order included.

### 74. What else the decoration sweep found

Method: parse every `__all__` in `app/`, every `Settings` field and every enum member; index
identifiers across `app/`, `tests/`, `scripts/`, `alembic/` and `desktop/src/`; report anything
whose only occurrence is its own definition. Constants exported purely for readability were
filtered out — the pattern worth hunting is the `as_prompt_context` / `ApplicationVerifier`
shape, which is *behaviour* with no producer or consumer.

**Clean.** Every `Settings` field has a reader — no dead config keys. Every plugin class,
storage backend and template that looks unreferenced is resolved through the registry by name
string, which is golden rule #5 working as designed, not decoration.

**Fixed here.** `PluginKind.PARSER` (item 72) and the missing parity test (item 73).

**Left, with a recommendation each:**

1. **Eight Prometheus collectors have no producer** — ✅ **closed; see §75-77 below for what
   the wiring found.** `record_posting_discovered`, `record_posting_deduped`, `record_score`,
   `record_application`, `observe_apply`,
   `record_document_rendered`, `record_knowledge_document`, `observe_knowledge_index`. The
   *infrastructure* metrics are wired (HTTP via `ObservabilityMiddleware`, cache via
   `app/cache/base.py`, LLM via `app/ai/llm.py`, Celery via `app/workers/__init__.py`, review
   queue and session gauges via `app/workers/cleanup.py`), but **every domain metric is flat
   zero forever** — which is to say the whole funnel this product exists to run (discovered →
   deduped → scored → applied) is unobservable, and a dashboard built on `/metrics` would show
   nothing. This is the same shape as `ApplicationVerifier` and larger; it is a gap, not a
   sweep leftover, and is filed as **G12** in `docs/DEFINITION_OF_DONE.md`. Recommended call
   sites, one line each: `DiscoveryService` for the two posting counters, after `Scorer.score`
   for `record_score`, `ApplicationService.transition` for `record_application`, the apply
   path in `Pipeline` for `observe_apply`, `render_resume` in `app/documents/renderer.py` for
   `record_document_rendered`, and `KnowledgeIndexer` for the last two. Not taken in this pass
   because half those modules are being edited concurrently and a partial wiring is worse than
   an honest inventory.

2. **`StatusSource.PIPELINE` is never written.** `applications.status_source` defaults to
   `MANUAL` and is only ever set by `app/tracking/service.py` (`EMAIL`, `PORTAL`, `INFERRED`,
   `MANUAL`). So an application the pipeline drove to `submitted` is recorded as though a
   human did it, and the enum member that exists precisely to say otherwise is unused. That is
   a provenance defect, not merely dead vocabulary. Recommended: give
   `ApplicationService.transition` a `source: StatusSource = StatusSource.MANUAL` keyword and
   have `Pipeline` pass `StatusSource.PIPELINE`. Not taken here because it changes a signature
   on the golden-rule-#1 transition path, which wants its own test pass.

3. **Five `PostingStatus` members are never written** — `QUEUED`, `PROCESSING`, `APPLIED`,
   `NEEDS_REVIEW`, `FAILED`. A posting's status stops at `DISCOVERED` → `DEDUPED` → `SCORED` /
   `SKIPPED`, plus `EXPIRED` from `cleanup`; everything after scoring is tracked on the
   `Application` row instead, which is the right design. Recommended: **delete the five**, the
   same three-file change as item 72, once someone confirms no desktop screen renders them.
   Deliberately not bundled into this pass — two independent enum removals in one commit make
   the parity test's first real exercise ambiguous if it fails.

4. **Six API schemas have no endpoint** — `DocumentRead`, `ChunkRead`, `EdgeRead` (the
   knowledge API exposes sources, facts, entities, graph, search and stats, but never
   documents, chunks or edges), `SyncReportRead` (`POST /tracking/sync` returns `OkResponse`;
   the report reaches the client only as a WebSocket `tracking.sync_progress` payload built
   from `SyncReport.as_dict()`), and `UserCreate` / `UserUpdate` (there is no users route —
   auth is a single-tenant shim). None has a `types.ts` mirror except that event payload, so
   nothing client-side depends on them. Recommended: keep `DocumentRead`/`ChunkRead`/`EdgeRead`
   only if the "browse indexed documents" screen in `docs/UI.md` is still wanted, and delete
   `UserCreate`/`UserUpdate` outright — multi-user is explicitly out of scope.

5. **`SignalSource.ATS_PORTAL` and `SignalSource.WEBHOOK` are unproduced**, and that is fine.
   `tracker` is a real extension point with a real implementation (`EmailTracker`), and
   `MailProvider.signal_source()` produces the three email sources dynamically. These two name
   the channels a third-party tracker would report against — vocabulary for an extension point
   that exists, which is the opposite of item 72's problem. No action.

---

## Phase 10 — G12: wiring the domain metrics

### 75. Where each domain recorder belongs, and the three judgement calls

§74.1 named the eight recorders with no producer. Wiring them raised four questions the
inventory did not answer.

**`discovered` is a superset of `deduped`, not a sibling.** The obvious reading — increment
`discovered` on `DedupeService.upsert`'s *created* branch and `deduped` on the other — makes
the two disjoint, and contradicts the collector's own help text ("Job postings returned by a
provider, **before** deduplication"). It would also make the Grafana panel unreadable: it plots
both series together, and two partitions of the same total do not tell you the dedupe rate.
So `record_posting_discovered` fires once per call at the top of `upsert`, before the tiers
run, and `record_posting_deduped` fires on both collapse branches (the ordinary match and the
lost-insert-race one). `discovered = created + deduped` holds by construction.

**`observe_apply` is in a `finally`, not on the success path.** `Pipeline.submit` already timed
the provider call in a `finally` to compute `elapsed`; the recorder goes in the same block.
An apply that spends ninety seconds in a browser and then escalates to a human is precisely
what the histogram exists to surface, and a success-only observation would hide every one of
them. Cancellation unwinds through it too. A guard that refuses *before* the provider is
reached records nothing, which is correct — no attempt was made, and timing one would be a lie.

**`record_document_rendered` is a decorator (`_measured`), not a call.** `render_resume` leaves
by five doors — three returns, the page-limit raise, and whatever the template raised — and the
failure half is the half a success-only counter silently hides. The decorator covers every door
by construction, reads the engine off the `RenderResult` on success and off
`DocumentRenderError.engine` on failure, and counts one sample per *document* rather than one
per shrink-ladder rung.

**Scoring and knowledge counters are folded before recording.** `score_new` already builds a
verdict tally and `index_source` already has the document set, so each calls its recorder once
per label value with an `amount` rather than once per row. Same series, fewer calls on the hot
path.

### 76. The provider label was `unknown` for the entire pre-submit band

`applicantos_applications_total` is labelled by provider, and the provider lives on
`Application.posting` — a `lazy="selectin"` relationship, so "just read it" looks safe. It is
not: a row `ApplicationService.create_or_get` has just inserted was never loaded, and reading
an unloaded relationship inside a coroutine raises `MissingGreenlet`. Inspecting
`state.unloaded` first and degrading to `None` fixes the crash but produces `provider="unknown"`
for every `draft` → `preparing` → `ready` transition, which is worse than it sounds: the
dashboard panel filters `provider=~"$provider"`, so those samples vanish from the chart rather
than showing up as an anomaly.

Found by scraping a live container, not by a test. `_resolve_provider` now tries the loaded
relationship, then the session's **identity map** — where `create_or_get` and
`Pipeline.prepare` have both already put the `JobPosting` a few lines earlier — and only then
gives up. Both lookups are pure memory and cannot emit SQL.

### 77. A metric recorded in a Celery worker is scraped by nobody

Discovered while proving G12 end to end, and **not fixed**, because it is a different piece of
work from wiring producers.

`docker/prometheus/prometheus.yml` scrapes exactly one target, the API, and its header explains
why the workers are not scraped: *"the worker-side recorders (`track_task`, `observe_apply`,
`record_application`) write to the same Redis-backed application state the API reads."* That
sentence is not true. `app.observability.metrics.registry` is an ordinary in-process object —
`prometheus_client.CollectorRegistry` or the built-in fallback — with no shared backend of any
kind. A counter incremented in `worker-apply` lives and dies in that process.

The consequence is topological rather than a code defect: `POST /postings/{id}/apply` and
`POST /postings/discover` **enqueue**, so in the Docker deployment the funnel is produced almost
entirely inside processes Prometheus never contacts. The API's own `/metrics` shows the domain
series only for the work the API does in-process — `PATCH /applications/{id}` (a real
transition) and `POST /resumes/preview` (a real render), both confirmed non-zero on a live
scrape over TCP.

Three honest options, in ascending cost:

1. **Correct the comment** and accept that the Docker dashboard shows the API's slice. Cheapest,
   and stops the file claiming a mechanism that does not exist.
2. **`prometheus_client`'s multiprocess mode** — a shared `PROMETHEUS_MULTIPROC_DIR` volume and
   a `MultiProcessCollector` on the API's `/metrics`. Works, but only with the real client
   library installed (the fallback has no equivalent), needs the gauge aggregation modes chosen
   per series, and leaks files for workers that exit uncleanly.
3. **Push gateway or a per-worker exporter sidecar.** Most faithful, most moving parts.

Recommended: (1) now, (2) if and when the Grafana dashboard becomes something an operator
actually watches. The desktop install — one process, API and work in the same interpreter — is
unaffected either way, and that is the shipping topology.
