"""Settings schemas — the *safe* projection of :class:`app.config.settings.Settings`.

``GET /settings`` is the single most dangerous read in the API: the settings object holds
five API keys, a signing secret, and two connection URLs with embedded credentials. So this
module is built around one rule, and :meth:`SettingsRead.from_settings` is the only
sanctioned way to satisfy that read:

**No secret is ever a field of :class:`SettingsRead`.** Not redacted, not masked, not
truncated — absent. Where the client legitimately needs to know whether a credential is
configured (to show "connect your Anthropic key" versus "connected"), the answer is a
boolean: ``anthropic_configured``, ``openai_configured``, ``github_configured``,
``aws_configured``, ``sentry_configured``. Connection URLs are reduced to
``database_backend``.

Masking is deliberately rejected as an approach. A masked key ("sk-ant-…4f2a") still leaks
length, prefix, and enough tail to confirm a guess, and every masking scheme eventually
grows an "unmask" endpoint. A boolean cannot leak anything.

Writing is the mirror image: :class:`SettingsUpdate` **does** accept credentials, because
they have to be settable somehow, and marks them ``repr=False`` so they cannot surface in a
traceback or a debug dump. A field accepted for write is never echoed back on read.

:class:`PluginInfo` mirrors :class:`app.plugins.base.PluginMeta` for
``GET /settings/plugins``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import Field

from app.models.enums import PluginKind
from app.schemas.common import Schema

if TYPE_CHECKING:  # pragma: no cover - import kept out of runtime, schemas sit below config
    from app.config.settings import Settings

__all__ = [
    "DatabaseBackend",
    "PluginInfo",
    "SettingsRead",
    "SettingsUpdate",
]

#: Coarse description of the configured database, exposed instead of the connection URL —
#: which carries a username and password.
DatabaseBackend = Literal["postgresql", "sqlite", "other"]

#: Bounds for the 0-100 normalised score threshold.
_SCORE_FLOOR: int = 0
_SCORE_CEILING: int = 100

#: Bounds for the answer-confidence floor, a unit-interval probability.
_CONFIDENCE_FLOOR: float = 0.0
_CONFIDENCE_CEILING: float = 1.0


def _classify_database(url: str) -> str:
    """Reduce a connection URL to a backend name, discarding credentials.

    Args:
        url: A SQLAlchemy connection URL, which may embed a username and password.

    Returns:
        ``"postgresql"``, ``"sqlite"``, or ``"other"``.
    """
    if url.startswith(("postgresql", "postgres", "cockroachdb")):
        return "postgresql"
    if url.startswith("sqlite"):
        return "sqlite"
    return "other"


class SettingsRead(Schema):
    """The safe, readable projection of runtime configuration.

    Contains no credential of any kind. Build it with :meth:`from_settings`; constructing it
    field-by-field elsewhere is how a secret eventually leaks in.
    """

    # -- application ---------------------------------------------------------------
    app_name: str = "ApplicantOS"
    environment: str = "local"
    debug: bool = False
    data_dir: str = Field(description="Root directory for all local state.")

    # -- persistence (credentials reduced to a backend name) ------------------------
    database_backend: DatabaseBackend = Field(
        default="postgresql",  # type: ignore[assignment]
        description="Backend name only; the connection URL carries credentials.",
    )
    sqlite_mode: bool = Field(
        default=False,
        description="Whether the zero-infrastructure lightweight install is active.",
    )

    # -- cache -----------------------------------------------------------------------
    cache_backend: str = "redis"
    cache_default_ttl: int = 86_400
    cache_llm_responses: bool = True
    cache_embeddings: bool = True

    # -- AI --------------------------------------------------------------------------
    llm_provider: str = "anthropic"
    llm_model_reasoning: str = "claude-sonnet-4-5"
    llm_model_fast: str = "claude-haiku-4-5-20251001"
    llm_model_local: str = "llama3.1:8b"
    local_llm_base_url: str = "http://localhost:11434/v1"
    llm_max_retries: int = 3
    llm_timeout_seconds: int = 90
    llm_daily_token_budget: int = 2_000_000
    anthropic_configured: bool = Field(
        default=False,
        description="Whether an Anthropic API key is present. The key itself is never sent.",
    )
    openai_configured: bool = Field(
        default=False,
        description="Whether an OpenAI API key is present. The key itself is never sent.",
    )

    # -- embeddings / vector store -----------------------------------------------------
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    vector_store: str = "pgvector"
    knowledge_chunk_tokens: int = 512
    knowledge_chunk_overlap: int = 64

    # -- knowledge engine ---------------------------------------------------------------
    knowledge_autoindex: bool = True
    knowledge_reindex_interval_minutes: int = 60
    github_configured: bool = Field(
        default=False,
        description="Whether a GitHub token is present; without one, rate limits are lower.",
    )
    github_include_forks: bool = False
    github_max_repos: int = 200
    website_crawl_max_pages: int = 40
    website_crawl_max_depth: int = 3
    project_scan_max_files: int = 2_000
    project_scan_max_file_bytes: int = 256_000

    # -- storage --------------------------------------------------------------------------
    storage_backend: str = "local"
    s3_bucket: str | None = None
    s3_region: str = "us-east-1"
    aws_configured: bool = Field(
        default=False,
        description="Whether AWS credentials are present. The keys are never sent.",
    )

    # -- browser ----------------------------------------------------------------------------
    playwright_headless: bool = True
    playwright_slow_mo_ms: int = 0
    playwright_timeout_ms: int = 30_000

    # -- policy / safety ----------------------------------------------------------------------
    auto_apply_enabled: bool = Field(
        default=False,
        description="The kill switch. Submission also requires dry_run to be False.",
    )
    dry_run: bool = Field(
        default=True,
        description="When True nothing is ever submitted, whatever else is configured.",
    )
    is_submission_allowed: bool = Field(
        default=False,
        description="auto_apply_enabled AND NOT dry_run — the effective permission.",
    )
    auto_apply_min_score: int = Field(default=70, ge=_SCORE_FLOOR, le=_SCORE_CEILING)
    max_applications_per_day: int = 50
    max_applications_per_session: int = 200
    max_essay_questions_before_review: int = 3
    min_answer_confidence: float = Field(
        default=0.75,
        ge=_CONFIDENCE_FLOOR,
        le=_CONFIDENCE_CEILING,
    )
    delete_temp_resume_after_submit: bool = True

    # -- documents -----------------------------------------------------------------------------
    pdf_engine: str = "latex"
    latex_binary: str = "tectonic"
    resume_max_pages: int = 1
    resume_template: str = "modern"

    # -- api / observability ----------------------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    log_level: str = "INFO"
    log_json: bool = True
    metrics_enabled: bool = True
    sentry_configured: bool = Field(
        default=False,
        description="Whether a Sentry DSN is present. The DSN itself is never sent.",
    )

    @classmethod
    def from_settings(cls, settings: Settings) -> SettingsRead:
        """Project a :class:`~app.config.settings.Settings` into its safe read model.

        Every field is named explicitly rather than copied wholesale from
        ``settings.model_dump()``. That is the point: a new secret added to ``Settings``
        cannot leak through this method by default, because a field that is not listed here
        does not exist in the response. An allow-list is the only construction that fails
        closed.

        Args:
            settings: The process-wide settings singleton.

        Returns:
            The safe projection, containing no credential of any kind.
        """
        return cls(
            app_name=settings.app_name,
            environment=settings.environment,
            debug=settings.debug,
            data_dir=settings.data_dir,
            database_backend=_classify_database(settings.database_url),  # type: ignore[arg-type]
            sqlite_mode=settings.sqlite_mode,
            cache_backend=settings.cache_backend,
            cache_default_ttl=settings.cache_default_ttl,
            cache_llm_responses=settings.cache_llm_responses,
            cache_embeddings=settings.cache_embeddings,
            llm_provider=settings.llm_provider,
            llm_model_reasoning=settings.llm_model_reasoning,
            llm_model_fast=settings.llm_model_fast,
            llm_model_local=settings.llm_model_local,
            local_llm_base_url=settings.local_llm_base_url,
            llm_max_retries=settings.llm_max_retries,
            llm_timeout_seconds=settings.llm_timeout_seconds,
            llm_daily_token_budget=settings.llm_daily_token_budget,
            anthropic_configured=bool(settings.anthropic_api_key),
            openai_configured=bool(settings.openai_api_key),
            embedding_provider=settings.embedding_provider,
            embedding_model=settings.embedding_model,
            embedding_dim=settings.embedding_dim,
            vector_store=settings.vector_store,
            knowledge_chunk_tokens=settings.knowledge_chunk_tokens,
            knowledge_chunk_overlap=settings.knowledge_chunk_overlap,
            knowledge_autoindex=settings.knowledge_autoindex,
            knowledge_reindex_interval_minutes=settings.knowledge_reindex_interval_minutes,
            github_configured=bool(settings.github_token),
            github_include_forks=settings.github_include_forks,
            github_max_repos=settings.github_max_repos,
            website_crawl_max_pages=settings.website_crawl_max_pages,
            website_crawl_max_depth=settings.website_crawl_max_depth,
            project_scan_max_files=settings.project_scan_max_files,
            project_scan_max_file_bytes=settings.project_scan_max_file_bytes,
            storage_backend=settings.storage_backend,
            s3_bucket=settings.s3_bucket,
            s3_region=settings.s3_region,
            aws_configured=bool(settings.aws_access_key_id and settings.aws_secret_access_key),
            playwright_headless=settings.playwright_headless,
            playwright_slow_mo_ms=settings.playwright_slow_mo_ms,
            playwright_timeout_ms=settings.playwright_timeout_ms,
            auto_apply_enabled=settings.auto_apply_enabled,
            dry_run=settings.dry_run,
            is_submission_allowed=settings.is_submission_allowed,
            auto_apply_min_score=settings.auto_apply_min_score,
            max_applications_per_day=settings.max_applications_per_day,
            max_applications_per_session=settings.max_applications_per_session,
            max_essay_questions_before_review=settings.max_essay_questions_before_review,
            min_answer_confidence=settings.min_answer_confidence,
            delete_temp_resume_after_submit=settings.delete_temp_resume_after_submit,
            pdf_engine=settings.pdf_engine,
            latex_binary=settings.latex_binary,
            resume_max_pages=settings.resume_max_pages,
            resume_template=settings.resume_template,
            api_host=settings.api_host,
            api_port=settings.api_port,
            log_level=settings.log_level,
            log_json=settings.log_json,
            metrics_enabled=settings.metrics_enabled,
            sentry_configured=bool(settings.sentry_dsn),
        )


class SettingsUpdate(Schema):
    """Partial update to runtime configuration — the body of ``PUT /settings``.

    Only the settings a user can meaningfully change at run time appear here. Deliberately
    absent: ``database_url``, ``redis_url``, ``secret_key``, ``data_dir``, ``environment``,
    ``api_host``/``api_port``. Those either require a restart or would let an HTTP request
    repoint the process at a different database.

    Credentials **are** writable — they have to be settable somehow — and carry
    ``repr=False`` so they never appear in a traceback, a validation error, or a debug dump.
    They are never returned by :class:`SettingsRead`.

    Apply with ``model_dump(exclude_unset=True)``: a field the client did not send is left
    untouched, and a credential sent as ``null`` clears it.
    """

    # -- credentials (write-only; never echoed) --------------------------------------
    anthropic_api_key: str | None = Field(default=None, repr=False)
    openai_api_key: str | None = Field(default=None, repr=False)
    github_token: str | None = Field(default=None, repr=False)
    aws_access_key_id: str | None = Field(default=None, repr=False)
    aws_secret_access_key: str | None = Field(default=None, repr=False)
    sentry_dsn: str | None = Field(default=None, repr=False)

    # -- AI ----------------------------------------------------------------------------
    llm_provider: Literal["anthropic", "openai", "local", "null"] | None = None
    llm_model_reasoning: str | None = None
    llm_model_fast: str | None = None
    llm_model_local: str | None = None
    local_llm_base_url: str | None = None
    llm_max_retries: int | None = Field(default=None, ge=0)
    llm_timeout_seconds: int | None = Field(default=None, ge=1)
    llm_daily_token_budget: int | None = Field(default=None, ge=0)

    # -- cache --------------------------------------------------------------------------
    cache_backend: Literal["memory", "disk", "redis"] | None = None
    cache_default_ttl: int | None = Field(default=None, ge=0)
    cache_llm_responses: bool | None = None
    cache_embeddings: bool | None = None

    # -- embeddings / vector store ---------------------------------------------------------
    embedding_provider: Literal["openai", "local", "hashing"] | None = None
    embedding_model: str | None = None
    vector_store: Literal["pgvector", "sqlite_vec", "memory"] | None = None
    knowledge_chunk_tokens: int | None = Field(default=None, ge=1)
    knowledge_chunk_overlap: int | None = Field(default=None, ge=0)

    # -- knowledge engine ---------------------------------------------------------------------
    knowledge_autoindex: bool | None = None
    knowledge_reindex_interval_minutes: int | None = Field(default=None, ge=1)
    github_include_forks: bool | None = None
    github_max_repos: int | None = Field(default=None, ge=1)
    website_crawl_max_pages: int | None = Field(default=None, ge=1)
    website_crawl_max_depth: int | None = Field(default=None, ge=1)
    project_scan_max_files: int | None = Field(default=None, ge=1)
    project_scan_max_file_bytes: int | None = Field(default=None, ge=1)

    # -- storage -------------------------------------------------------------------------------
    storage_backend: Literal["local", "s3"] | None = None
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str | None = None

    # -- browser --------------------------------------------------------------------------------
    playwright_headless: bool | None = None
    playwright_slow_mo_ms: int | None = Field(default=None, ge=0)
    playwright_timeout_ms: int | None = Field(default=None, ge=0)

    # -- policy / safety -------------------------------------------------------------------------
    auto_apply_enabled: bool | None = Field(
        default=None,
        description="The kill switch. Submission also requires dry_run to be False.",
    )
    dry_run: bool | None = None
    auto_apply_min_score: int | None = Field(
        default=None,
        ge=_SCORE_FLOOR,
        le=_SCORE_CEILING,
    )
    max_applications_per_day: int | None = Field(default=None, ge=0)
    max_applications_per_session: int | None = Field(default=None, ge=0)
    max_essay_questions_before_review: int | None = Field(default=None, ge=0)
    min_answer_confidence: float | None = Field(
        default=None,
        ge=_CONFIDENCE_FLOOR,
        le=_CONFIDENCE_CEILING,
    )
    delete_temp_resume_after_submit: bool | None = None

    # -- documents ---------------------------------------------------------------------------------
    pdf_engine: Literal["latex", "docx", "html"] | None = None
    latex_binary: str | None = None
    resume_max_pages: int | None = Field(default=None, ge=1)
    resume_template: str | None = None

    # -- observability -------------------------------------------------------------------------------
    log_level: str | None = None
    log_json: bool | None = None
    metrics_enabled: bool | None = None


class PluginInfo(Schema):
    """One registered plugin, as reported by ``GET /settings/plugins``.

    Mirrors :class:`app.plugins.base.PluginMeta` plus the two pieces of state the registry
    holds rather than the metadata: whether the plugin is currently enabled, and the result
    of its last health check.
    """

    kind: PluginKind
    name: str = Field(description="Registry key, unique within its kind.")
    version: str = "1.0.0"
    display_name: str = Field(default="", description="Human-facing name.")
    description: str = ""
    author: str = ""
    capabilities: list[str] = Field(
        default_factory=list,
        description="Declared capability tokens, e.g. 'auto_apply', 'pdf'.",
    )
    enabled: bool = Field(default=True, description="Whether the registry will resolve it.")
    enabled_by_default: bool = True
    healthy: bool | None = Field(
        default=None,
        description="Last healthcheck result; None when it has not been probed.",
    )
