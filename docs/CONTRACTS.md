# ApplicantOS — Binding Interface Contracts (v2)

> **Single source of truth for every cross-module boundary.**
> Modules are built in parallel against this file. You MAY add private helpers; you MUST NOT
> change any name, path, signature, enum value, table name, task name, or route defined here.
> If a contract looks wrong, implement it as written and append to `docs/OPEN_QUESTIONS.md`.

**Runtime:** Python 3.12+ (async-first, SQLAlchemy 2.0 `Mapped[]`, Pydantic v2).
**Desktop:** React 19 + TypeScript 5 + Vite + Tailwind v4 + shadcn/ui + TanStack Query v5 +
TanStack Router + Zustand + Framer Motion, shipped in Electron via `electron-vite`.
**Design system:** `docs/UI.md` is binding for all frontend work.

---

## 0. Repository layout (authoritative)

```
ApplicantOS/
  app/
    main.py                     # FastAPI factory + ASGI app
    config/        settings.py  logging.py  scoring_rules.yaml
    database/      base.py  session.py  types.py
    cache/         base.py  memory.py  disk.py  redis_cache.py  decorators.py  keys.py
    plugins/       base.py  registry.py  loader.py
    models/        enums.py  mixins.py  user.py  profile.py  knowledge.py  resume.py
                   application.py  company.py  posting.py  score.py  cover_letter.py
                   session.py  checkpoint.py  log.py  file.py  cache_entry.py
    schemas/       common.py  user.py  onboarding.py  knowledge.py  posting.py
                   application.py  resume.py  scoring.py  session.py  dashboard.py  settings.py
    knowledge/     graph.py  facts.py  indexer.py  retrieval.py  memory.py  extractors.py
                   vector/    base.py  pgvector.py  sqlite_vec.py  memory_store.py
                   analyzers/ base.py  github.py  website.py  project_folder.py
                              resume_parser.py  linkedin_export.py  document.py
    jobs/          base.py  registry.py  dedupe.py  seeds.py  _parsing.py
                   linkedin.py  greenhouse.py  lever.py  ashby.py  workday.py
    ai/            llm.py  models/ (anthropic.py openai.py local.py null.py)
                   embeddings.py  scoring.py  resume_engine.py  cover_letter.py
                   field_answer.py  prompts/
    documents/     models.py  renderer.py  latex.py  docx.py  html.py  markdown.py  templates/
    browser/       playwright_runner.py  autofill.py  selectors.py  recorder.py  verification.py
    storage/       base.py  local.py  s3.py
    services/      pipeline.py  discovery_service.py  application_service.py
                   review_service.py  dedupe_service.py  onboarding_service.py
                   session_service.py  checkpoint_service.py  knowledge_service.py
                   analytics_service.py
    api/           deps.py  errors.py  events.py  routes/
    workers/       celery_app.py  poll_jobs.py  apply_jobs.py  index_knowledge.py
                   cleanup.py  retry.py  healthcheck.py
    observability/ metrics.py  middleware.py
  desktop/         electron/  src/  electron.vite.config.ts  package.json
  alembic/  docker/  scripts/  tests/  docs/  .claude/agents/
```

---

## 1. Settings — `app/config/settings.py`

Env-var name = UPPER_SNAKE of the field name. `.env.example` must list every key.

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_nested_delimiter="__")

    app_name: str = "ApplicantOS"
    environment: Literal["local","dev","prod"] = "local"
    debug: bool = False
    secret_key: str = "change-me"
    data_dir: str = "./var"                       # root for all local state

    # persistence
    database_url: str = "postgresql+asyncpg://applicantos:applicantos@localhost:5432/applicantos"
    sync_database_url: str | None = None          # derived when unset
    sqlite_mode: bool = False                     # lightweight install
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None

    # cache
    cache_backend: Literal["memory","disk","redis"] = "redis"
    cache_dir: str = "./var/cache"
    cache_default_ttl: int = 86400
    cache_llm_responses: bool = True
    cache_embeddings: bool = True

    # AI
    llm_provider: Literal["anthropic","openai","local","null"] = "anthropic"
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    llm_model_reasoning: str = "claude-sonnet-4-5"
    llm_model_fast: str = "claude-haiku-4-5-20251001"
    llm_model_local: str = "llama3.1:8b"
    local_llm_base_url: str = "http://localhost:11434/v1"
    llm_max_retries: int = 3
    llm_timeout_seconds: int = 90
    llm_daily_token_budget: int = 2_000_000

    # embeddings / vector store
    embedding_provider: Literal["openai","local","hashing"] = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    vector_store: Literal["pgvector","sqlite_vec","memory"] = "pgvector"
    knowledge_chunk_tokens: int = 512
    knowledge_chunk_overlap: int = 64

    # knowledge engine
    knowledge_autoindex: bool = True
    knowledge_reindex_interval_minutes: int = 60
    github_token: str | None = None
    github_include_forks: bool = False
    github_max_repos: int = 200
    website_crawl_max_pages: int = 40
    website_crawl_max_depth: int = 3
    project_scan_max_files: int = 2000
    project_scan_max_file_bytes: int = 256_000

    # storage
    storage_backend: Literal["local","s3"] = "local"
    storage_local_root: str = "./var/storage"
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None

    # browser
    playwright_headless: bool = True
    playwright_slow_mo_ms: int = 0
    playwright_timeout_ms: int = 30000
    browser_user_data_dir: str = "./var/browser"
    screenshot_dir: str = "./var/screenshots"

    # policy / safety
    auto_apply_enabled: bool = False              # KILL SWITCH — default OFF
    dry_run: bool = True                          # never submits when True
    auto_apply_min_score: int = 70
    max_applications_per_day: int = 50
    max_applications_per_session: int = 200
    max_essay_questions_before_review: int = 3
    min_answer_confidence: float = 0.75
    delete_temp_resume_after_submit: bool = True

    # documents
    pdf_engine: Literal["latex","docx","html"] = "latex"
    latex_binary: str = "tectonic"
    resume_max_pages: int = 1
    resume_template: str = "modern"

    # api / desktop
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    cors_origins: list[str] = ["http://localhost:5173", "app://applicantos"]

    # observability
    log_level: str = "INFO"
    log_json: bool = True
    metrics_enabled: bool = True
    sentry_dsn: str | None = None

    # computed properties (all mkdir lazily, return Path)
    data_path / cache_path / storage_root / screenshot_path / browser_profile_path
    @property def is_submission_allowed(self) -> bool: return self.auto_apply_enabled and not self.dry_run

settings = Settings()
@lru_cache def get_settings() -> Settings
```

---

## 2. Database — `app/database/`

```python
# base.py
class Base(DeclarativeBase): metadata = MetaData(naming_convention=NAMING_CONVENTION)
# session.py
engine, async_session_factory
async def get_session() -> AsyncIterator[AsyncSession]        # FastAPI dep
@asynccontextmanager async def session_scope() -> AsyncIterator[AsyncSession]
async def init_db() / dispose_engine() / check_database() -> bool
# types.py
GUID          # native UUID on pg, CHAR(36) elsewhere
JSONType      # JSONB on pg, JSON elsewhere
EmbeddingType # pgvector Vector(dim) on pg, JSON list elsewhere
UTCDateTime   # tz-aware
```

`app/models/mixins.py`: `UUIDPrimaryKeyMixin`, `TimestampMixin`, `SoftDeleteMixin`, `UserOwnedMixin`.

---

## 3. Enums — `app/models/enums.py` (all `str, Enum`, lowercase snake_case values)

```python
ATSProviderName:  linkedin greenhouse lever ashby workday manual
PostingStatus:    discovered deduped scored queued processing applied skipped
                  needs_review failed expired
ApplicationStatus: draft preparing ready submitting submitted confirmed needs_review
                   failed abandoned rejected interview offer ghosted
ReviewReason:     too_many_essays unknown_field login_required captcha mfa
                  file_upload_failed ambiguous_answer low_confidence policy_block
                  submit_not_found unsupported_flow verification_failed rate_limited
DocumentKind:     master_resume tailored_resume cover_letter screenshot transcript
                  portfolio source_document other
EmploymentType:   full_time part_time internship contract new_grad unknown
WorkArrangement:  remote hybrid onsite unknown
FieldKind:        text textarea select multiselect radio checkbox file date
                  number email phone url unknown
PluginKind:       provider model template parser analyzer
EntityKind:       person skill technology project organization role education award
                  certification course leadership publication interest goal language
RelationKind:     used_in worked_at built studied_at earned led contributed_to
                  related_to published mentored achieved requires
SourceKind:       github_repo github_profile portfolio_page personal_website project_folder
                  resume cover_letter readme documentation linkedin_export interview_note
                  user_correction generated_resume blog_post video manual_entry
FactKind:         accomplishment responsibility metric skill_usage award education_item
                  leadership_item publication_item
IndexStatus:      pending indexing indexed failed skipped stale
SessionStatus:    running completed failed cancelled
CheckpointStatus: pending running succeeded failed compensated
WorkAuthStatus:   citizen permanent_resident visa_holder needs_sponsorship unknown
```

`ApplicationStatus.terminal_states()`, `.is_post_submit()`, `.is_active()` helpers required.

---

## 4. Models — table names & required columns

All tables: UUID PK, `created_at`, `updated_at`.

### Identity & profile
| Model | Table | Key columns |
|---|---|---|
| `User` | `users` | `email` uniq, `full_name`, `is_active`, `onboarded_at` |
| `UserProfile` | `user_profiles` | `user_id` uniq FK, `phone`, `pronouns`, `location`, `address` JSON, `links` JSON (github/linkedin/portfolio/website/other), `citizenship`, `work_authorization` (enum), `requires_sponsorship` bool, `veteran_status`, `gender`, `race_ethnicity`, `disability_status`, `clearance`, `salary_min`, `salary_max`, `salary_currency`, `remote_preference` (enum), `willing_to_relocate` bool, `relocation_targets` JSON, `desired_roles` JSON, `desired_industries` JSON, `excluded_companies` JSON, `excluded_industries` JSON, `education` JSON, `start_date_availability`, `notice_period_weeks`, `extra` JSON |
| `UserPreferences` | *pydantic only* | see §5 |

EEO fields are nullable, never inferred, and always answerable as "decline to self-identify".

### Knowledge engine
| Model | Table | Key columns |
|---|---|---|
| `KnowledgeSource` | `knowledge_sources` | `user_id`, `kind` (SourceKind), `uri`, `label`, `config` JSON, `enabled`, `index_status`, `last_indexed_at`, `last_error`, `etag`, `content_hash`, `auto_refresh`, **UNIQUE(user_id, kind, uri)** |
| `KnowledgeDocument` | `knowledge_documents` | `user_id`, `source_id` FK, `kind` (SourceKind), `uri`, `title`, `raw_text`, `summary`, `content_hash` idx, `metadata_json`, `token_count`, `indexed_at`, **UNIQUE(source_id, uri)** |
| `KnowledgeChunk` | `knowledge_chunks` | `document_id` FK, `ordinal`, `text`, `token_count`, `embedding` (EmbeddingType), `metadata_json`, **UNIQUE(document_id, ordinal)** |
| `KnowledgeEntity` | `knowledge_entities` | `user_id`, `kind` (EntityKind), `name`, `normalized_name` idx, `summary`, `attributes` JSON, `aliases` JSON, `confidence` float, `mention_count`, `first_seen_at`, `last_seen_at`, `embedding`, **UNIQUE(user_id, kind, normalized_name)** |
| `KnowledgeEdge` | `knowledge_edges` | `user_id`, `source_entity_id` FK, `target_entity_id` FK, `relation` (RelationKind), `weight` float, `evidence` JSON, **UNIQUE(source_entity_id, target_entity_id, relation)** |
| `KnowledgeFact` | `knowledge_facts` | `user_id`, `kind` (FactKind), `text`, `normalized_text`, `organization`, `role`, `location`, `date_start`, `date_end`, `skills` JSON, `technologies` JSON, `metrics` JSON, `impact_score` int, `confidence` float, `embedding`, `source_document_id` FK nullable, `entity_id` FK nullable, `user_verified` bool, `is_active` bool, `content_hash` idx |
| `MemoryEntry` | `memory_entries` | `user_id`, `kind` (correction/outcome/feedback/preference/note), `text`, `context` JSON, `embedding`, `weight` float, `expires_at` nullable |

**`KnowledgeFact` replaces the old "master resume bullet".** A resume is a *generated view* of facts.

### Applications
| Model | Table | Key columns |
|---|---|---|
| `Company` | `companies` | `name`, `normalized_name` uniq, `domain`, `industry`, `size_bucket`, `employee_count`, `is_defense`, `is_startup`, `metadata_json`, `enriched_at` |
| `JobPosting` | `job_postings` | `company_id` FK, `provider`, `external_id`, `url`, `apply_url`, `title`, `description`, `location`, `work_arrangement`, `employment_type`, `salary_min/max/currency`, `posted_at`, `closes_at`, `raw_json`, `content_hash` idx, `dedupe_key` idx, `status`, **UNIQUE(provider, external_id)**, **UNIQUE(dedupe_key)** |
| `JobScore` | `job_scores` | `posting_id` FK, `user_id` FK, `total`, `normalized`, `breakdown` JSON, `verdict`, `model_used`, `rationale`, **UNIQUE(posting_id, user_id)** |
| `Resume` | `resumes` | `user_id`, `name`, `variant_label`, `template`, `is_default`, `config` JSON |
| `ResumeVersion` | `resume_versions` | `resume_id` FK, `application_id` FK nullable, `version_number`, `content_json` (ResumeDocument), `render_format`, `file_id` FK nullable, `fact_ids` JSON, `token_usage` JSON, `reasoning`, `deleted_at`, **UNIQUE(resume_id, version_number)** |
| `CoverLetter` | `cover_letters` | `user_id`, `posting_id` FK, `application_id` FK nullable, `body`, `tone`, `file_id` FK nullable, `token_usage` JSON |
| `Application` | `applications` | `user_id`, `posting_id`, `company_id`, `session_id` FK nullable, `status`, `resume_version_id`, `cover_letter_id`, `submitted_at`, `duration_seconds`, `confirmation_screenshot_id`, `confirmation_id`, `confirmation_text`, `review_reason`, `review_payload` JSON, `attempt_count`, `last_error`, `external_application_id`, `answers` JSON, `ai_reasoning`, `browser_log` JSON, `notes`, **UNIQUE(user_id, posting_id)** |
| `ApplicationEvent` | `application_events` | `application_id` FK, `kind`, `message`, `payload` JSON, `at` |

### Runtime
| Model | Table | Key columns |
|---|---|---|
| `RunSession` | `run_sessions` | `user_id`, `status`, `started_at`, `ended_at`, `jobs_found`, `jobs_qualified`, `resumes_generated`, `applications_completed`, `manual_review`, `failures`, `avg_application_seconds`, `token_usage` JSON, `config_snapshot` JSON, `trigger` |
| `Checkpoint` | `checkpoints` | `session_id` FK nullable, `key` uniq idx, `owner`, `step`, `status`, `state` JSON, `attempt`, `last_error`, `resumable`, `expires_at` |
| `UploadedFile` | `uploaded_files` | `user_id` nullable, `kind`, `filename`, `content_type`, `size_bytes`, `storage_key`, `sha256`, `backend`, `expires_at`, `deleted_at` |
| `LogEntry` | `log_entries` | `level`, `event`, `logger`, `correlation_id`, `session_id`, `application_id`, `posting_id`, `payload` JSON, `at` |
| `CacheEntry` | `cache_entries` | `key` uniq, `namespace`, `value` JSON, `content_hash`, `hits`, `expires_at` |

---

## 5. `UserPreferences` — pydantic, stored on `users.preferences`

```python
class UserPreferences(BaseModel):
    min_score: int = 70
    auto_apply: bool = False
    max_applications_per_day: int = 50
    max_essay_questions: int = 3
    min_salary: int | None = None
    preferred_locations: list[str] = []
    preferred_keywords: list[str] = []
    blocked_companies: list[str] = []
    blocked_industries: list[str] = []
    exclude_defense: bool = False
    skip_startups_under: int | None = None
    remote_only: bool = False
    require_no_sponsorship: bool = True
    resume_variant: str | None = None
    resume_template: str = "modern"
    cover_letter_policy: Literal["always","when_required","when_high_score","never"] = "when_required"
    providers_enabled: list[str] = ["greenhouse","lever","ashby"]
```
`User.prefs` property parses/defaults it. Imported everywhere as
`from app.models.user import UserPreferences`.

---

## 6. Plugin system — `app/plugins/`

**Everything pluggable goes through this.** Five kinds: `provider`, `model`, `template`,
`parser`, `analyzer`.

```python
# base.py
@dataclass(frozen=True) class PluginMeta:
    kind: PluginKind; name: str; version: str = "1.0.0"
    display_name: str = ""; description: str = ""; author: str = ""
    capabilities: frozenset[str] = frozenset(); enabled_by_default: bool = True

class Plugin(Protocol):
    meta: ClassVar[PluginMeta]
    def __init__(self, settings: Settings, **kw: Any) -> None: ...
    async def healthcheck(self) -> bool: ...

class PluginError(Exception); class PluginNotFound(PluginError)
class PluginLoadError(PluginError); class PluginDisabled(PluginError)

# registry.py
class PluginRegistry:
    def register(self, cls: type[Plugin]) -> type[Plugin]
    def get(self, kind: PluginKind, name: str) -> Plugin        # cached instance
    def get_class(self, kind: PluginKind, name: str) -> type[Plugin]
    def all(self, kind: PluginKind) -> list[Plugin]
    def names(self, kind: PluginKind) -> list[str]
    def describe(self) -> list[PluginMeta]
    def disable(self, kind, name) / enable(self, kind, name)
registry: PluginRegistry                      # module singleton
def plugin(cls) -> type[Plugin]               # decorator == registry.register

# loader.py
def load_all() -> None      # idempotent; imports built-ins then entry points
ENTRY_POINT_GROUPS = {PluginKind.PROVIDER: "applicantos.providers",
                      PluginKind.MODEL:    "applicantos.models",
                      PluginKind.TEMPLATE: "applicantos.templates",
                      PluginKind.PARSER:   "applicantos.parsers",
                      PluginKind.ANALYZER: "applicantos.analyzers"}
```

Nothing outside `app/jobs/` may import a concrete provider module; nothing outside
`app/knowledge/analyzers/` may import a concrete analyzer; nothing outside `app/ai/models/` may
import a concrete model client. Always go through `registry`.

---

## 7. Cache — `app/cache/`

```python
class Cache(Protocol):
    async def get(self, key: str) -> Any | None
    async def set(self, key: str, value: Any, *, ttl: int | None = None) -> None
    async def delete(self, key: str) -> None
    async def exists(self, key: str) -> bool
    async def clear(self, namespace: str | None = None) -> int
    async def get_or_set(self, key: str, factory: Callable[[], Awaitable[Any]], *, ttl=None) -> Any

class MemoryCache(Cache)   # LRU + TTL
class DiskCache(Cache)     # content-addressed under settings.cache_path
class RedisCache(Cache)    # orjson-serialized
class TieredCache(Cache)   # memory -> disk/redis read-through

def get_cache() -> Cache                                   # from settings, cached
def make_key(namespace: str, *parts: Any) -> str           # stable sha256 of normalized parts
def hash_payload(obj: Any) -> str

@cached(namespace="llm", ttl=..., key=lambda *a, **k: ...)  # decorator for async fns
def invalidate(namespace: str, *parts) -> Awaitable[None]
```

**Cache these by contract:** embeddings, LLM responses (when `cache_llm_responses` and
temperature==0), parsed resumes, project summaries, GitHub analysis, company metadata,
job descriptions, rendered resumes/cover letters (keyed by content hash), scoring results.

---

## 8. Knowledge engine — `app/knowledge/`

### 8.1 Analyzers — `analyzers/base.py` (PluginKind.ANALYZER)

```python
@dataclass(slots=True) class SourceRef:
    kind: SourceKind; uri: str; label: str | None = None; config: dict[str, Any] = ...

@dataclass(slots=True) class ExtractedDocument:
    uri: str; title: str; text: str; kind: SourceKind
    metadata: dict[str, Any] = ...; content_hash: str = ""

@dataclass(slots=True) class ExtractedFact:
    kind: FactKind; text: str; skills: list[str] = ...; technologies: list[str] = ...
    metrics: list[str] = ...; organization: str | None = None; role: str | None = None
    date_start: str | None = None; date_end: str | None = None
    impact_score: int = 0; confidence: float = 0.5; source_uri: str | None = None

@dataclass(slots=True) class ExtractedEntity:
    kind: EntityKind; name: str; summary: str | None = None
    attributes: dict[str, Any] = ...; aliases: list[str] = ...; confidence: float = 0.5

@dataclass(slots=True) class ExtractedEdge:
    source: tuple[EntityKind, str]; target: tuple[EntityKind, str]
    relation: RelationKind; weight: float = 1.0; evidence: dict[str, Any] = ...

@dataclass(slots=True) class AnalysisResult:
    documents: list[ExtractedDocument] = ...; facts: list[ExtractedFact] = ...
    entities: list[ExtractedEntity] = ...; edges: list[ExtractedEdge] = ...
    fingerprint: str = ""                  # for change detection
    errors: list[str] = ...

class Analyzer(Plugin, abc.ABC):
    meta: ClassVar[PluginMeta]
    source_kinds: ClassVar[frozenset[SourceKind]]
    @abc.abstractmethod async def analyze(self, source: SourceRef) -> AnalysisResult: ...
    async def fingerprint(self, source: SourceRef) -> str: ...      # cheap change probe
    def supports(self, source: SourceRef) -> bool: ...
```

Concrete analyzers (each a registered plugin):
- `GitHubAnalyzer` — `github_profile` / `github_repo`. Uses the REST API with
  `settings.github_token` (works unauthenticated at lower rate limits). Pulls repos, languages,
  topics, stars, README, recent commit activity, and per-repo dependency manifests. Skips forks
  unless `github_include_forks`. Emits one document per repo + entities for each
  language/technology + facts for notable repos (stars, scale, described impact).
- `WebsiteAnalyzer` — `personal_website` / `portfolio_page`. Polite crawler: robots.txt honored,
  same-origin only, `website_crawl_max_pages`/`max_depth`, 1 req/sec, extracts main content to
  text, discovers project pages and links.
- `ProjectFolderAnalyzer` — `project_folder`. Walks a local directory: README, docs/, manifests
  (`package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, `pom.xml`, `CMakeLists.txt`,
  `platformio.ini`), language histogram by extension, LOC, git history if present. Respects
  `.gitignore`, skips binaries/`node_modules`/`venv`, caps at `project_scan_max_files`.
- `ResumeParser` — `resume`. PDF (pypdf) / DOCX (python-docx) / MD / TXT → text, then LLM
  structuring into facts + education + skills, with a deterministic regex fallback.
- `LinkedInExportAnalyzer` — `linkedin_export`. Parses the official export ZIP/CSVs
  (`Positions.csv`, `Education.csv`, `Skills.csv`, `Projects.csv`, `Certifications.csv`,
  `Honors.csv`). **User-supplied export only — no scraping.**
- `DocumentAnalyzer` — `readme` / `documentation` / `blog_post` / `interview_note` / generic.

### 8.2 Vector store — `knowledge/vector/base.py`

```python
@dataclass(slots=True) class VectorRecord:
    id: str; embedding: list[float]; text: str; metadata: dict[str, Any]
@dataclass(slots=True) class VectorHit:
    id: str; score: float; text: str; metadata: dict[str, Any]

class VectorStore(Protocol):
    async def upsert(self, collection: str, records: Sequence[VectorRecord]) -> int
    async def query(self, collection: str, embedding: Sequence[float], *, k: int = 10,
                    filters: dict[str, Any] | None = None) -> list[VectorHit]
    async def delete(self, collection: str, ids: Sequence[str]) -> int
    async def count(self, collection: str) -> int
def get_vector_store() -> VectorStore
```
`PgVectorStore` (pgvector, cosine, ivfflat index), `SqliteVecStore`, `InMemoryVectorStore`
(numpy-free pure-python cosine — always available).

### 8.3 Indexer / graph / retrieval / memory

```python
# indexer.py
@dataclass class IndexReport:
    source_id: uuid.UUID; documents: int; chunks: int; facts: int; entities: int
    edges: int; skipped: bool; duration_seconds: float; errors: list[str]

class KnowledgeIndexer:
    def __init__(self, session, settings, *, embedder=None, cache=None)
    async def index_source(self, source_id, *, force: bool = False) -> IndexReport
    async def index_all(self, user_id, *, force: bool = False) -> list[IndexReport]
    async def add_source(self, user_id, ref: SourceRef) -> KnowledgeSource
    async def remove_source(self, source_id) -> None
    async def refresh_stale(self, user_id) -> list[IndexReport]

# Pipeline: fingerprint -> skip if unchanged & not force -> analyze -> upsert documents
# -> chunk -> embed (cached) -> vector upsert -> merge facts (dedupe by content_hash +
# 0.93 cosine) -> upsert entities (by normalized_name) -> upsert edges -> mark indexed.

# graph.py
class KnowledgeGraph:
    def __init__(self, session)
    async def upsert_entity(self, user_id, e: ExtractedEntity) -> KnowledgeEntity
    async def upsert_edge(self, user_id, edge: ExtractedEdge) -> KnowledgeEdge
    async def neighbors(self, entity_id, *, relations=None, depth: int = 1) -> list[KnowledgeEntity]
    async def subgraph(self, user_id, *, kinds=None, limit=500) -> GraphView   # nodes+edges for UI
    async def merge_entities(self, keep_id, drop_id) -> KnowledgeEntity
    async def stats(self, user_id) -> dict[str, int]

# facts.py
class FactStore:
    async def upsert_many(self, user_id, facts: list[ExtractedFact], *, source_document_id=None) -> int
    async def search(self, user_id, query: str, *, k=40, kinds=None) -> list[KnowledgeFact]
    async def active(self, user_id) -> list[KnowledgeFact]
    async def deactivate(self, fact_id) / verify(self, fact_id, verified: bool)

# retrieval.py
@dataclass class RetrievalResult: facts: list[KnowledgeFact]; chunks: list[VectorHit]
                                  entities: list[KnowledgeEntity]; memories: list[MemoryEntry]
class KnowledgeRetriever:
    async def retrieve(self, user_id, query: str, *, k_facts=40, k_chunks=12,
                       kinds=None, expand_graph: bool = True) -> RetrievalResult
    # hybrid: vector similarity + keyword/BM25-ish + graph expansion, reciprocal-rank fused

# memory.py
class MemoryStore:
    async def record_correction(self, user_id, *, before: str, after: str, context: dict) -> MemoryEntry
    async def record_outcome(self, user_id, application_id, outcome: ApplicationStatus, notes=None)
    async def record_feedback(self, user_id, text: str, context: dict) -> MemoryEntry
    async def relevant(self, user_id, query: str, *, k=8) -> list[MemoryEntry]
    async def prune_expired(self) -> int
```

---

## 9. ATS providers — `app/jobs/base.py` (PluginKind.PROVIDER)

Unchanged from v1 except `ATSProvider` is now a `Plugin` and carries `meta`.

```python
@dataclass(slots=True) class RawPosting:      # provider, external_id, url, title, company_name,
    ...                                        # description, location, work_arrangement,
                                               # employment_type, salary_*, posted_at, closes_at,
                                               # apply_url, raw
@dataclass(slots=True) class SearchQuery:     # keywords, locations, remote_only,
                                               # posted_within_days, limit, extra
@dataclass(slots=True) class FormField:       # selector, label, kind, required, options,
                                               # max_length, hint
@dataclass(slots=True) class ApplyContext:    # application_id, posting: JobPostingDTO,
                                               # user: UserProfileDTO, resume_path,
                                               # cover_letter_path, answers, dry_run, recorder
@dataclass(slots=True) class ApplyResult:     # ok, status, review_reason, confirmation_text,
                                               # confirmation_id, screenshot_paths,
                                               # external_application_id, unanswered_fields,
                                               # duration_seconds, browser_log, error

class ATSProvider(Plugin, abc.ABC):
    meta: ClassVar[PluginMeta]                       # kind=PluginKind.PROVIDER
    name: ClassVar[ATSProviderName]
    supports_auto_apply: ClassVar[bool] = False
    requires_login: ClassVar[bool] = False
    URL_PATTERNS: ClassVar[list[re.Pattern[str]]] = []
    @abc.abstractmethod async def search(self, q: SearchQuery) -> AsyncIterator[RawPosting]
    @abc.abstractmethod async def fetch_posting(self, id_or_url: str) -> RawPosting | None
    async def detect(self, url: str) -> bool
    async def apply(self, ctx: ApplyContext) -> ApplyResult          # default raises UnsupportedFlowError
    async def healthcheck(self) -> bool

Errors: ProviderError, ProviderAuthError, ProviderRateLimitError,
        UnsupportedFlowError, PostingUnavailableError
DTOs (DB-free): JobPostingDTO, UserProfileDTO — each with `.from_model()`
```

`registry.py` helpers: `get_provider(name)`, `all_providers()`, `provider_for_url(url)`
(delegate to `app.plugins.registry`).

`dedupe.py`: `canonical_url`, `content_hash`, `dedupe_key`, `normalize_company`,
`normalize_title`, `similarity(a,b)`, `is_duplicate(a,b,threshold=0.92)`.

**Auto-apply posture (binding):** greenhouse ✅, lever ✅, ashby ✅ — real submission.
workday ❌ (account-gated multi-step) and linkedin ❌ (ToS prohibits automated submission;
discovery limited to a user-supplied export or public feed) — both raise `UnsupportedFlowError`
and route to manual review. Each provider module docstring states its posture.

---

## 10. AI — `app/ai/`

```python
# llm.py
@dataclass class LLMResponse: text, input_tokens, output_tokens, model, cached: bool, raw
class ModelPlugin(Plugin, abc.ABC):                      # PluginKind.MODEL
    async def complete(self, *, system, prompt, max_tokens=4096, temperature=0.2,
                       json_schema: dict | None = None) -> LLMResponse
    async def complete_json(self, *, system, prompt, schema: dict, **kw) -> dict
    def count_tokens(self, text: str) -> int
# models/: AnthropicModel, OpenAIModel, LocalModel (OpenAI-compatible), NullModel
def get_llm(tier: Literal["reasoning","fast"] = "reasoning") -> ModelPlugin
```
`NullModel` is a deterministic offline stub returning schema-valid JSON — **the entire pipeline
must run end-to-end with zero API keys.** SDK imports are lazy. Responses are cached via
`app/cache` when `temperature == 0` and `cache_llm_responses`. Token usage feeds
`applicantos_llm_tokens_total` and the daily budget guard.

```python
# embeddings.py
class Embedder(Protocol): async def embed(self, texts: list[str]) -> list[list[float]]
OpenAIEmbedder | LocalEmbedder | HashingEmbedder(deterministic offline fallback)
def get_embedder() -> Embedder; def cosine(a,b) -> float; def top_k(...)

# scoring.py — deterministic rule engine + optional LLM nudge (identical to v1 semantics)
ScoreRule, ScoreComponent, ScoreResult, Scorer, DEFAULT_RULES, load_rules(), explain()
Scorer.score_rules() is pure/sync/deterministic. LLM may adjust ±10 and write a rationale but
MUST NOT flip a hard negative (sponsorship / blocked company / blocked industry) into "apply".

# resume_engine.py  — generates a resume as a VIEW OF THE KNOWLEDGE GRAPH
@dataclass class TailorRequest: user, posting: JobPostingDTO, prefs: UserPreferences,
                                template: str = "modern", max_bullets: int = 18,
                                variant_label: str | None = None
@dataclass class TailorResult: document: ResumeDocument, selected_fact_ids: list[str],
                               reasoning: str, token_usage: dict[str,int], cached: bool
class ResumeEngine:
    def __init__(self, session, llm, retriever: KnowledgeRetriever, cache)
    async def tailor(self, req) -> TailorResult
    async def prefilter(self, req, top_k: int = 60) -> list[KnowledgeFact]
    def fallback_tailor(self, req, facts) -> TailorResult      # no-LLM deterministic ranking
```
**Anti-hallucination (binding):** the LLM only *selects and rewrites* facts retrieved from the
knowledge graph and must return `fact_id`s. Any id not in the retrieved set is dropped and logged
(`resume_engine.hallucinated_fact`). A rewrite whose token overlap with the source fact is < 0.35
is reverted to the original text (`resume_engine.rewrite_rejected`). Organizations, roles, and
dates are copied from the source fact, never from the model. No invented employers, degrees,
metrics, or dates — ever.

```python
# cover_letter.py — CoverLetterWriter.should_write(posting, prefs) / .write(req) -> CoverLetterResult
# field_answer.py — FieldAnswerer.answer(field, user, knowledge) -> AnswerPlan(value, confidence, source)
```

---

## 11. Documents — `app/documents/`

```python
# models.py  (also the shape of ResumeVersion.content_json)
Contact, ResumeEntry(title, organization, location, date_range, bullets, fact_ids),
ResumeSection(heading, entries), ResumeDocument(contact, summary, sections, skills_line, meta)
ResumeDocument.estimated_lines() / .total_bullets()

# renderer.py  (PluginKind.TEMPLATE)
@dataclass class RenderResult: path, page_count, engine, template, bytes_written
class TemplatePlugin(Plugin, abc.ABC):
    formats: ClassVar[frozenset[str]]      # {"pdf","docx","md","html"}
    async def render(self, doc: ResumeDocument, out: Path, *, fmt: str = "pdf",
                     options: dict | None = None) -> RenderResult
def get_template(name: str) -> TemplatePlugin
async def render_resume(doc, out, *, template=None, fmt="pdf", max_pages=1) -> RenderResult
async def render_cover_letter(body, contact, out, *, template=None, fmt="pdf") -> RenderResult
```
Built-in templates: `modern` (LaTeX), `classic` (LaTeX), `ats_plain` (DOCX), `web` (HTML→PDF),
`markdown` (MD). `render_resume` MUST enforce `max_pages` via a shrink loop (font 10.5→10→9.5pt,
margins 0.5→0.45→0.4in, then drop lowest-impact bullets; max 5 attempts, each logged).
`escape_latex` is mandatory on every model-produced string.

---

## 12. Browser — `app/browser/`

```python
class BrowserSession:            # async CM; chromium; artifacts dir; tracing when debug
    page: Page; artifacts: BrowserArtifacts
    async def goto(url, *, wait="domcontentloaded"); async def screenshot(name, full_page=True)
    async def save_storage_state(path); async def detect_blockers() -> set[str]
                                        # {"captcha","mfa","login_wall","cloudflare"}
class FieldResolver:  async def resolve(self, field: FormField) -> AnswerPlan
class AutoFiller:
    async def discover_fields() -> list[FormField]
    async def fill(fields) -> tuple[list[AnswerPlan], list[FormField]]   # (filled, needs_review)
    async def upload(field, path) -> None
    async def submit(*, dry_run: bool) -> bool
class ApplicationVerifier:       # verification.py
    async def verify(self, session, provider) -> VerificationResult
    # confirms success markers / confirmation id / URL change; captures proof screenshot
class ArtifactRecorder:          # recorder.py — screenshots, html, json, manifest
```

**Safety invariants (non-negotiable):**
1. `AutoFiller.submit` returns `False` **without clicking** when `dry_run` or
   `not settings.auto_apply_enabled`.
2. Confidence `< settings.min_answer_confidence`, any unanswerable required field, captcha, MFA,
   login wall, or essay count `> settings.max_essay_questions_before_review` ⇒ `NEEDS_REVIEW`.
   **Never guess.**
3. Every apply attempt captures a screenshot before and after submit.
4. Only a submit control located via the provider's `SelectorPack` or an exact accessible-name
   match may ever be clicked.

---

## 13. Services — `app/services/`

```python
class Pipeline:
    async def discover(user_id, providers, query) -> int
    async def ingest(raw: RawPosting) -> tuple[JobPosting, bool]
    async def score_posting(posting_id, user_id) -> JobScore
    async def prepare(posting_id, user_id) -> Application
    async def submit(application_id) -> PipelineResult
    async def run_one(posting_id, user_id) -> PipelineResult
    async def cleanup_application(application_id) -> None
class SessionService:      start(user_id, trigger) / record(session_id, **deltas)
                           finish(session_id, status) / current(user_id) / stats(session_id)
class CheckpointService:   save(key, owner, step, state) / load(key) / resume_all(owner)
                           complete(key) / fail(key, error) -> resumable state machine
class OnboardingService:   steps() / submit_step(user_id, step, payload) / status(user_id)
                           complete(user_id) -> triggers first knowledge index
class KnowledgeService:    add_source / list_sources / reindex / search / graph / facts / stats
class AnalyticsService:    overview(user_id) / funnel(user_id) / timeseries(user_id, days)
                           by_company / by_provider / outcomes / what_gets_interviews(user_id)
```
Idempotency: `ingest` upserts on `dedupe_key`; `prepare` is a no-op past `READY`;
`submit` refuses when already `SUBMITTED`/`CONFIRMED`. **Never apply twice.**
Every long operation writes a `Checkpoint` so a crash resumes rather than restarts.

---

## 14. API — `app/main.py` + `app/api/routes/`

`create_app() -> FastAPI`; `app = create_app()`. All under `/api/v1` except `/health`,
`/ready`, `/metrics`, `/ws`.

| Group | Endpoints |
|---|---|
| health | `GET /health` `GET /ready` `GET /metrics` |
| onboarding | `GET /onboarding/status` `GET /onboarding/steps` `POST /onboarding/steps/{step}` `POST /onboarding/complete` |
| profile | `GET|PUT /profile` `GET|PUT /profile/preferences` |
| knowledge | `GET|POST /knowledge/sources` `DELETE /knowledge/sources/{id}` `POST /knowledge/sources/{id}/reindex` `POST /knowledge/reindex` `GET /knowledge/facts` `PATCH /knowledge/facts/{id}` `GET /knowledge/entities` `GET /knowledge/graph` `GET /knowledge/search` `GET /knowledge/stats` |
| postings | `GET /postings` `GET /postings/{id}` `POST /postings/discover` `POST /postings/{id}/apply` |
| applications | `GET /applications` `GET /applications/{id}` `POST /applications/{id}/retry` `PATCH /applications/{id}` `GET /applications/{id}/artifacts` |
| reviews | `GET /reviews` `POST /reviews/{id}/resolve` `POST /reviews/{id}/dismiss` |
| resumes | `GET|POST /resumes` `GET /resumes/versions/{id}` `GET /resumes/versions/{id}/download` `POST /resumes/preview` |
| sessions | `GET /sessions` `GET /sessions/{id}` `POST /sessions/start` `POST /sessions/{id}/stop` |
| analytics | `GET /analytics/overview` `GET /analytics/funnel` `GET /analytics/timeseries` `GET /analytics/insights` |
| settings | `GET|PUT /settings` `GET /settings/plugins` `GET|PUT /settings/scoring-rules` |
| logs | `GET /logs` |
| events | `GET /ws` (WebSocket) — live event stream |

Every list endpoint returns `Page[T] = {items, total, limit, offset}`.

`app/api/events.py` — `EventBus` publishing typed events to WebSocket subscribers:
`session.started|updated|finished`, `posting.discovered|scored`,
`application.created|status_changed|submitted|needs_review`,
`knowledge.index_started|index_progress|index_finished`, `log.entry`.
Payloads are the same pydantic schemas the REST endpoints return, so the desktop app can
`setQueryData` directly without refetching.

---

## 15. Workers — `app/workers/`

Celery app `applicantos`. Queues: `discovery`, `ai`, `apply`, `knowledge`, `maintenance`.

```
jobs.poll_all / jobs.poll_provider / jobs.score_posting        -> discovery, ai
apply.prepare / apply.submit / apply.run_one                   -> ai, apply
knowledge.index_source / knowledge.index_all /
knowledge.refresh_stale / knowledge.embed_backlog              -> knowledge
cleanup.temp_documents / cleanup.expire_postings /
cleanup.prune_artifacts / cleanup.prune_cache /
cleanup.refresh_gauges                                          -> maintenance
session.watchdog                                                -> maintenance
```
Beat: `jobs.poll_all` 30m · `knowledge.refresh_stale` 60m · `cleanup.temp_documents` 1h ·
`cleanup.expire_postings` daily · `session.watchdog` 5m.
Tasks are thin sync wrappers over async services via `run_async()`. `NEEDS_REVIEW` and policy
blocks are terminal and never retried.

---

## 16. Observability

structlog with `redact_secrets` (recursive over dicts/lists; scrubs password/token/api_key/
secret/authorization/cookie/ssn/dob). Bound keys: `correlation_id`, `user_id`, `session_id`,
`posting_id`, `application_id`, `provider`, `event`.

```
applicantos_postings_discovered_total{provider}     applicantos_postings_deduped_total{provider}
applicantos_scores_total{verdict}                   applicantos_applications_total{status,provider}
applicantos_apply_duration_seconds{provider}        applicantos_llm_tokens_total{model,kind}
applicantos_llm_requests_total{model,outcome}       applicantos_cache_events_total{namespace,event}
applicantos_documents_rendered_total{engine,outcome}
applicantos_knowledge_documents_total{kind}         applicantos_knowledge_index_duration_seconds{analyzer}
applicantos_review_queue_size                       applicantos_task_duration_seconds{task,outcome}
applicantos_session_active                          applicantos_http_request_duration_seconds{route,method,status}
```

---

## 17. Desktop app — `desktop/`

```
desktop/
  package.json  electron.vite.config.ts  tsconfig*.json  tailwind config via CSS
  electron/  main.ts  preload.ts  ipc.ts  window.ts  menu.ts  backend.ts  store.ts
  src/
    main.tsx  app.tsx  router.tsx
    routes/      onboarding/  index  applications/  postings/  reviews/
                 knowledge/  resumes/  sessions/  analytics/  settings/  logs/
    components/  ui/ (shadcn primitives)  + app components
    lib/         api/client.ts  api/endpoints.ts  api/types.ts
                 query/client.ts  query/keys.ts  query/persist.ts
                 ws.ts  utils.ts  shortcuts.ts
    stores/      ui.ts  session.ts  filters.ts
    hooks/       use-*.ts
    styles/      globals.css  tokens.css
```

**Binding frontend rules:**
- `src/lib/api/types.ts` mirrors `app/schemas/` and `app/models/enums.py` — **enum string values
  must match exactly.**
- All server state goes through TanStack Query. No `useEffect` fetching.
- Query cache is persisted to disk and hydrated **before first paint** — a cold start renders
  real data, not skeletons.
- The WebSocket feeds `queryClient.setQueryData`; live updates never produce a loading state.
- Routes preload on hover/focus intent. Navigation between visited routes is instant.
- Mutations are optimistic with rollback.
- Never render a spinner for data already in cache. Skeletons only past 200ms of genuinely
  uncached load.
- Long lists (>100 rows) are virtualized.
- Electron window uses `show: false` + `ready-to-show` (no white flash); renderer↔main IPC only
  via `contextBridge` (`contextIsolation: true`, `nodeIntegration: false`).
- **`docs/UI.md` governs all visual and motion decisions.** Performance budget is enforceable:
  route change < 100ms, interaction-to-paint < 50ms, list scroll 60fps, cold start to
  interactive < 1.5s.

---

## 18. Cross-cutting rules (the golden rules)

1. **Never apply twice** — `UNIQUE(user_id, posting_id)` *and* a status guard in `Pipeline.submit`.
2. **Never guess** — low confidence, essay overflow, captcha, MFA, or unknown required field ⇒
   `NEEDS_REVIEW`.
3. **Kill switch** — submission requires `auto_apply_enabled=True` AND `dry_run=False`. Both
   default to the safe position.
4. **No secrets in logs** — the redaction processor is always in the structlog pipeline.
5. **Plugin isolation** — never import a concrete provider / analyzer / model / template module
   outside its own package. Go through `app.plugins.registry`.
6. **Knowledge is the source of truth** — resumes are generated views. Never edit a static PDF.
   `ResumeVersion.content_json` is retained forever; the rendered file is disposable.
7. **Nothing is fabricated** — every resume bullet traces to a `KnowledgeFact.id`.
8. **Everything is resumable** — long operations checkpoint; a crash resumes, never restarts.
9. **Cache aggressively, invalidate precisely** — content-addressed keys; never cache a mutation.
10. **ToS honesty** — providers that forbid automation set `supports_auto_apply=False` and route
    to manual review, documented in the module docstring.
