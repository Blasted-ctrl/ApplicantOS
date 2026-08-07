"""Application settings — the single, typed source of runtime configuration.

Every tunable in ApplicantOS lives here. The class is defined by ``docs/CONTRACTS.md`` §1
and is frozen: field names, types, and defaults must not drift, because the environment
variable name for each field is the UPPER_SNAKE form of its attribute name and the desktop
settings screen mirrors this shape one-for-one.

Three derivations happen automatically after the environment is read:

1. **SQLite mode.** When ``sqlite_mode`` is enabled the whole stack collapses to a
   zero-infrastructure install: the database URL is rewritten to a local aiosqlite file
   under ``data_dir``, the vector store switches to ``sqlite_vec`` and the cache to disk.
2. **Sync database URL.** Alembic and any blocking tooling need a synchronous driver, so
   ``sync_database_url`` is derived from ``database_url`` by swapping the async driver for
   its synchronous counterpart (``asyncpg`` → ``psycopg``, ``aiosqlite`` → ``pysqlite``, …).
3. **Celery URLs.** ``celery_broker_url`` and ``celery_result_backend`` default to
   ``redis_url`` so a single Redis instance powers cache, broker, and result backend.

Directory-valued settings are exposed as ``Path`` properties (``data_path``, ``cache_path``,
``storage_root``, ``screenshot_path``, ``browser_profile_path``) that resolve to absolute
paths and create the directory on first access. Creation is lazy on purpose: importing this
module must never touch the filesystem, otherwise test collection and ``--help`` invocations
would litter the working directory.

This module deliberately has **no** dependency on ``app.config.logging`` — logging is
configured *from* settings, so the reverse dependency would be circular.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "ASYNC_TO_SYNC_DRIVERS",
    "DEFAULT_SCORING_RULES_PATH",
    "SQLITE_ASYNC_DRIVER",
    "SQLITE_DATABASE_FILENAME",
    "Settings",
    "derive_sync_database_url",
    "get_settings",
    "settings",
]

# --------------------------------------------------------------------------------------
# Module constants (no magic values anywhere below this block)
# --------------------------------------------------------------------------------------

#: Filename of the local database used when ``sqlite_mode`` is enabled.
SQLITE_DATABASE_FILENAME: Final[str] = "applicantos.db"

#: SQLAlchemy dialect+driver used for the lightweight SQLite install.
SQLITE_ASYNC_DRIVER: Final[str] = "sqlite+aiosqlite"

#: Vector store forced by ``sqlite_mode``.
SQLITE_MODE_VECTOR_STORE: Final[str] = "sqlite_vec"

#: Cache backend forced by ``sqlite_mode`` (Redis is not part of a lightweight install).
SQLITE_MODE_CACHE_BACKEND: Final[str] = "disk"

#: Separator between the URL scheme and the rest of a SQLAlchemy connection URL.
_URL_SCHEME_SEPARATOR: Final[str] = "://"

#: Separator between dialect and driver inside a SQLAlchemy URL scheme.
_DRIVER_SEPARATOR: Final[str] = "+"

#: Async SQLAlchemy driver → synchronous equivalent, used to derive ``sync_database_url``.
#: Alembic migrations and any blocking maintenance script run on the synchronous URL.
ASYNC_TO_SYNC_DRIVERS: Final[dict[str, str]] = {
    "postgresql+asyncpg": "postgresql+psycopg",
    "postgres+asyncpg": "postgresql+psycopg",
    "postgresql+psycopg_async": "postgresql+psycopg",
    "cockroachdb+asyncpg": "cockroachdb+psycopg2",
    "sqlite+aiosqlite": "sqlite+pysqlite",
    "mysql+aiomysql": "mysql+pymysql",
    "mysql+asyncmy": "mysql+pymysql",
    "mariadb+aiomysql": "mariadb+pymysql",
    "mariadb+asyncmy": "mariadb+pymysql",
    "mssql+aioodbc": "mssql+pyodbc",
    "oracle+oracledb_async": "oracle+oracledb",
}

#: Location of the packaged default scoring rule pack (``app/config/scoring_rules.yaml``).
DEFAULT_SCORING_RULES_PATH: Final[Path] = Path(__file__).resolve().parent / "scoring_rules.yaml"

#: Default CORS origins: the Vite dev server and the packaged Electron custom scheme.
_DEFAULT_CORS_ORIGINS: Final[list[str]] = ["http://localhost:5173", "app://applicantos"]


# --------------------------------------------------------------------------------------
# Path helpers
# --------------------------------------------------------------------------------------


def _resolve_dir(raw: str) -> Path:
    """Expand ``~`` and resolve *raw* to an absolute path without touching the filesystem.

    Args:
        raw: A user-supplied path, possibly relative and possibly containing ``~``.

    Returns:
        The absolute :class:`~pathlib.Path` equivalent of *raw*.
    """
    return Path(raw).expanduser().resolve()


def _ensure_dir(raw: str) -> Path:
    """Resolve *raw* to an absolute path and create the directory if it does not exist.

    Args:
        raw: A user-supplied directory path.

    Returns:
        The absolute :class:`~pathlib.Path`, guaranteed to exist as a directory.
    """
    path = _resolve_dir(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def derive_sync_database_url(async_url: str) -> str:
    """Translate an async SQLAlchemy URL into its synchronous counterpart.

    Alembic, ad-hoc scripts, and any blocking tooling cannot use an async driver. The
    mapping is table-driven (:data:`ASYNC_TO_SYNC_DRIVERS`); an unknown async driver falls
    back to the bare dialect (``dialect+unknown://`` → ``dialect://``) so SQLAlchemy picks
    its own default driver rather than exploding on an async one.

    Args:
        async_url: A SQLAlchemy connection URL, typically with an async driver.

    Returns:
        A connection URL suitable for the synchronous SQLAlchemy engine. URLs that already
        name a synchronous driver (or name no driver at all) are returned unchanged.
    """
    scheme, separator, remainder = async_url.partition(_URL_SCHEME_SEPARATOR)
    if not separator:
        return async_url
    if _DRIVER_SEPARATOR not in scheme:
        # No explicit driver — SQLAlchemy's default for the dialect is synchronous.
        return async_url
    sync_scheme = ASYNC_TO_SYNC_DRIVERS.get(scheme.lower())
    if sync_scheme is None:
        sync_scheme = scheme.split(_DRIVER_SEPARATOR, 1)[0]
    return f"{sync_scheme}{separator}{remainder}"


# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------


class Settings(BaseSettings):
    """Typed runtime configuration for the whole backend.

    Values are read from (in decreasing precedence) explicit constructor keyword arguments,
    process environment variables, the ``.env`` file, and finally the defaults declared
    here. Nested settings use ``__`` as a delimiter.

    Attributes are grouped by concern; see ``docs/CONTRACTS.md`` §1 for the binding list.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_nested_delimiter="__",
    )

    # -- application ---------------------------------------------------------------
    app_name: str = "ApplicantOS"
    environment: Literal["local", "dev", "prod"] = "local"
    debug: bool = False
    secret_key: str = "change-me"
    data_dir: str = "./var"

    # -- persistence ---------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://applicantos:applicantos@localhost:5432/applicantos"
    sync_database_url: str | None = None
    sqlite_mode: bool = False
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None

    # -- cache ---------------------------------------------------------------------
    cache_backend: Literal["memory", "disk", "redis"] = "redis"
    cache_dir: str = "./var/cache"
    cache_default_ttl: int = 86400
    cache_llm_responses: bool = True
    cache_embeddings: bool = True

    # -- AI ------------------------------------------------------------------------
    llm_provider: Literal["anthropic", "openai", "local", "null"] = "anthropic"
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    llm_model_reasoning: str = "claude-sonnet-4-5"
    llm_model_fast: str = "claude-haiku-4-5-20251001"
    llm_model_local: str = "llama3.1:8b"
    local_llm_base_url: str = "http://localhost:11434/v1"
    llm_max_retries: int = 3
    llm_timeout_seconds: int = 90
    llm_daily_token_budget: int = 2_000_000

    # -- embeddings / vector store --------------------------------------------------
    embedding_provider: Literal["openai", "local", "hashing"] = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    vector_store: Literal["pgvector", "sqlite_vec", "memory"] = "pgvector"
    knowledge_chunk_tokens: int = 512
    knowledge_chunk_overlap: int = 64

    # -- knowledge engine ------------------------------------------------------------
    knowledge_autoindex: bool = True
    knowledge_reindex_interval_minutes: int = 60
    github_token: str | None = None
    github_include_forks: bool = False
    github_max_repos: int = 200
    website_crawl_max_pages: int = 40
    website_crawl_max_depth: int = 3
    project_scan_max_files: int = 2000
    project_scan_max_file_bytes: int = 256_000

    # -- storage ---------------------------------------------------------------------
    storage_backend: Literal["local", "s3"] = "local"
    storage_local_root: str = "./var/storage"
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None

    # -- browser ---------------------------------------------------------------------
    playwright_headless: bool = True
    playwright_slow_mo_ms: int = 0
    playwright_timeout_ms: int = 30000
    browser_user_data_dir: str = "./var/browser"
    screenshot_dir: str = "./var/screenshots"

    # -- policy / safety ---------------------------------------------------------------
    auto_apply_enabled: bool = False
    dry_run: bool = True
    auto_apply_min_score: int = 70
    max_applications_per_day: int = 50
    max_applications_per_session: int = 200
    max_essay_questions_before_review: int = 3
    min_answer_confidence: float = 0.75
    delete_temp_resume_after_submit: bool = True

    # -- documents -----------------------------------------------------------------------
    pdf_engine: Literal["latex", "docx", "html"] = "latex"
    latex_binary: str = "tectonic"
    resume_max_pages: int = 1
    resume_template: str = "modern"

    # -- api / desktop -------------------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: list(_DEFAULT_CORS_ORIGINS))

    # -- observability -------------------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True
    metrics_enabled: bool = True
    sentry_dsn: str | None = None

    # ---------------------------------------------------------------------------------
    # Derived configuration
    # ---------------------------------------------------------------------------------

    @model_validator(mode="after")
    def _apply_derived_defaults(self) -> Settings:
        """Apply SQLite mode, then derive the sync database and Celery URLs.

        Ordering is significant: SQLite mode rewrites ``database_url``, so it must run
        before ``sync_database_url`` is derived from it.

        Returns:
            This same instance, mutated in place (pydantic's documented ``mode="after"``
            contract). ``validate_assignment`` is off, so the assignments below do not
            re-enter validation.
        """
        if self.sqlite_mode:
            self._apply_sqlite_mode()
        if self.sync_database_url is None:
            self.sync_database_url = derive_sync_database_url(self.database_url)
        if self.celery_broker_url is None:
            self.celery_broker_url = self.redis_url
        if self.celery_result_backend is None:
            self.celery_result_backend = self.redis_url
        return self

    def _apply_sqlite_mode(self) -> None:
        """Collapse the stack to a zero-infrastructure local install.

        Points the database at a file under ``data_dir``, switches the vector store to
        ``sqlite_vec`` and the cache to disk, because neither PostgreSQL/pgvector nor
        Redis is available in this deployment shape.
        """
        database_file = _resolve_dir(self.data_dir) / SQLITE_DATABASE_FILENAME
        # ``as_posix`` keeps the URL valid on Windows ("C:/…") and yields the required
        # four-slash form for absolute POSIX paths ("////home/…").
        self.database_url = f"{SQLITE_ASYNC_DRIVER}:///{database_file.as_posix()}"
        self.vector_store = SQLITE_MODE_VECTOR_STORE  # type: ignore[assignment]
        self.cache_backend = SQLITE_MODE_CACHE_BACKEND  # type: ignore[assignment]

    # ---------------------------------------------------------------------------------
    # Computed paths (absolute, created on first access)
    # ---------------------------------------------------------------------------------

    @property
    def data_path(self) -> Path:
        """Root directory for every piece of local state. Created on first access."""
        return _ensure_dir(self.data_dir)

    @property
    def cache_path(self) -> Path:
        """Directory backing :class:`~app.cache.disk.DiskCache`. Created on first access."""
        return _ensure_dir(self.cache_dir)

    @property
    def storage_root(self) -> Path:
        """Root of the local blob store (resumes, cover letters, uploads, artifacts)."""
        return _ensure_dir(self.storage_local_root)

    @property
    def screenshot_path(self) -> Path:
        """Directory where browser proof-of-submission screenshots are written."""
        return _ensure_dir(self.screenshot_dir)

    @property
    def browser_profile_path(self) -> Path:
        """Persistent Chromium user-data directory (keeps logins between sessions)."""
        return _ensure_dir(self.browser_user_data_dir)

    # ---------------------------------------------------------------------------------
    # Policy predicates
    # ---------------------------------------------------------------------------------

    @property
    def is_submission_allowed(self) -> bool:
        """Whether the process is permitted to actually submit an application.

        Both switches default to the safe position, and both must be flipped: the kill
        switch (``auto_apply_enabled``) must be on *and* ``dry_run`` must be off. Every
        submission path in the codebase gates on this property.
        """
        return self.auto_apply_enabled and not self.dry_run

    @property
    def is_sqlite(self) -> bool:
        """Whether the configured database is SQLite (regardless of how it got there)."""
        return self.database_url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        """Whether the configured database is PostgreSQL."""
        return self.database_url.startswith(("postgresql", "postgres", "cockroachdb"))

    @property
    def is_production(self) -> bool:
        """Whether this process is running under the production environment profile."""
        return self.environment == "prod"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` singleton.

    Cached so that repeated FastAPI dependency resolution and worker task setup do not
    re-parse the environment. Tests may call ``get_settings.cache_clear()`` to force a
    reload after mutating the environment.

    Returns:
        The shared :class:`Settings` instance — the same object as the module-level
        :data:`settings`.
    """
    return Settings()


#: Process-wide settings singleton. Import this (or call :func:`get_settings`) rather than
#: constructing :class:`Settings` yourself, so the whole process agrees on configuration.
settings: Settings = get_settings()
