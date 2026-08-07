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
