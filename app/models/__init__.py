"""ORM model package — importing this module populates ``Base.metadata`` completely.

Everything that creates, inspects, or migrates the schema depends on one guarantee: that
``import app.models`` leaves :attr:`app.database.base.Base.metadata` holding **every** table
in the system. SQLAlchemy only registers a table when the module declaring its mapped class
is executed, so a model that nothing imports simply does not exist as far as
``create_all``, ``alembic revision --autogenerate``, and ``alembic upgrade`` are concerned —
and the failure is silent, which is the worst kind. This module is the single place that
imports all of them.

Consumers therefore have exactly two things to remember:

* ``import app.models`` (or ``from app.models import X``) before touching metadata —
  ``alembic/env.py`` and ``app.database.session.init_db`` both do.
* Import model classes **from here**, not from the leaf modules, so that a partial import
  can never leave the mapper registry half-configured. ``from app.models.user import
  UserPreferences`` remains valid and is the spelling frozen by ``docs/CONTRACTS.md`` §5.

Import order below runs bottom-up through the dependency graph — vocabulary (:mod:`enums`),
then column building blocks (:mod:`mixins`), then the identity root (:mod:`user`), then
everything hanging off it. Runtime cycles do not actually exist (every model-to-model import
in this package is guarded by ``TYPE_CHECKING``, and relationships are declared with string
targets resolved lazily at mapper-configuration time), but keeping the order honest means a
future direct import cannot silently introduce one.

The re-exported surface is: the declarative :class:`~app.database.base.Base`, all four
mixins, every enum plus the membership frozensets and lookup mappings defined beside them,
all twenty-four mapped classes, the :class:`~app.models.user.UserPreferences` pydantic
document, and the handful of module constants other packages legitimately need
(normalisation helpers, the EEO decline sentinel).
"""

from __future__ import annotations

from app.database.base import Base
from app.models.application import Application, ApplicationEvent
from app.models.cache_entry import CacheEntry
from app.models.checkpoint import Checkpoint

# Reference data, then postings and their per-user scores.
from app.models.company import COMPANY_LEGAL_SUFFIXES, Company
from app.models.cover_letter import CoverLetter

# Vocabulary first: every model below annotates columns with these.
from app.models.enums import (
    APPLICATION_ACTIVE_STATES,
    APPLICATION_POST_SUBMIT_STATES,
    APPLICATION_PRE_SUBMIT_STATES,
    APPLICATION_TERMINAL_STATES,
    EMAIL_SIGNAL_SOURCES,
    INDEX_TERMINAL_STATES,
    MAIL_PROVIDER_TO_SIGNAL_SOURCE,
    POSTING_TERMINAL_STATES,
    SESSION_FINISHED_STATES,
    SIGNAL_KIND_TO_STATUS,
    ApplicationStatus,
    ATSProviderName,
    CheckpointStatus,
    DocumentKind,
    EmploymentType,
    EntityKind,
    FactKind,
    FieldKind,
    IndexStatus,
    MailProvider,
    MemoryKind,
    PluginKind,
    PostingStatus,
    RelationKind,
    ReviewReason,
    SessionStatus,
    SignalKind,
    SignalSource,
    SourceKind,
    StatusSource,
    StrEnum,
    WorkArrangement,
    WorkAuthStatus,
)
from app.models.file import UploadedFile

# Knowledge engine: source -> document -> chunk, plus graph, facts and memory.
from app.models.knowledge import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeEdge,
    KnowledgeEntity,
    KnowledgeFact,
    KnowledgeSource,
    MemoryEntry,
    normalize_for_matching,
)
from app.models.log import LogEntry

# Column building blocks next: every mapped class inherits from these.
from app.models.mixins import (
    USER_FOREIGN_KEY_TARGET,
    SoftDeleteMixin,
    TimestampMixin,
    UserOwnedMixin,
    UUIDPrimaryKeyMixin,
)
from app.models.posting import JobPosting
from app.models.profile import (
    DECLINE_TO_SELF_IDENTIFY,
    EEO_FIELD_NAMES,
    PROFILE_LINK_KEYS,
    UserProfile,
)

# Generated documents and the applications that carry them.
from app.models.resume import Resume, ResumeVersion
from app.models.score import JobScore

# Runtime bookkeeping.
from app.models.session import RunSession

# Status sync: the connected mailbox and the signals read out of it.
from app.models.tracking import EmailAccount, StatusSignal

# Identity root — every user-owned table's foreign key points here.
from app.models.user import User, UserPreferences

__all__ = [
    # -- declarative base ------------------------------------------------------------
    "Base",
    # -- enums -----------------------------------------------------------------------
    "ATSProviderName",
    "ApplicationStatus",
    "CheckpointStatus",
    "DocumentKind",
    "EmploymentType",
    "EntityKind",
    "FactKind",
    "FieldKind",
    "IndexStatus",
    "MailProvider",
    "MemoryKind",
    "PluginKind",
    "PostingStatus",
    "RelationKind",
    "ReviewReason",
    "SessionStatus",
    "SignalKind",
    "SignalSource",
    "SourceKind",
    "StatusSource",
    "StrEnum",
    "WorkArrangement",
    "WorkAuthStatus",
    # -- enum membership sets and mappings -----------------------------------------------
    "APPLICATION_ACTIVE_STATES",
    "APPLICATION_POST_SUBMIT_STATES",
    "APPLICATION_PRE_SUBMIT_STATES",
    "APPLICATION_TERMINAL_STATES",
    "EMAIL_SIGNAL_SOURCES",
    "INDEX_TERMINAL_STATES",
    "MAIL_PROVIDER_TO_SIGNAL_SOURCE",
    "POSTING_TERMINAL_STATES",
    "SESSION_FINISHED_STATES",
    "SIGNAL_KIND_TO_STATUS",
    # -- mixins ------------------------------------------------------------------------
    "SoftDeleteMixin",
    "TimestampMixin",
    "USER_FOREIGN_KEY_TARGET",
    "UUIDPrimaryKeyMixin",
    "UserOwnedMixin",
    # -- identity and profile -----------------------------------------------------------
    "DECLINE_TO_SELF_IDENTIFY",
    "EEO_FIELD_NAMES",
    "PROFILE_LINK_KEYS",
    "User",
    "UserPreferences",
    "UserProfile",
    # -- knowledge engine ---------------------------------------------------------------
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeEdge",
    "KnowledgeEntity",
    "KnowledgeFact",
    "KnowledgeSource",
    "MemoryEntry",
    "normalize_for_matching",
    # -- postings and scoring -------------------------------------------------------------
    "COMPANY_LEGAL_SUFFIXES",
    "Company",
    "JobPosting",
    "JobScore",
    # -- documents and applications ---------------------------------------------------------
    "Application",
    "ApplicationEvent",
    "CoverLetter",
    "Resume",
    "ResumeVersion",
    # -- runtime ------------------------------------------------------------------------------
    "CacheEntry",
    "Checkpoint",
    "LogEntry",
    "RunSession",
    "UploadedFile",
    # -- status sync ----------------------------------------------------------------------------
    "EmailAccount",
    "StatusSignal",
]

#: Every mapped class in the system, in dependency order. Exists so that tooling and tests
#: can assert "the migration covers exactly these tables" without re-deriving the list by
#: hand, and so that a newly added model that is *not* wired into this package fails a
#: single, obvious assertion instead of silently vanishing from the schema.
MODEL_CLASSES: tuple[type[Base], ...] = (
    User,
    Company,
    CacheEntry,
    LogEntry,
    UserProfile,
    UploadedFile,
    RunSession,
    Checkpoint,
    KnowledgeSource,
    KnowledgeDocument,
    KnowledgeChunk,
    KnowledgeEntity,
    KnowledgeEdge,
    KnowledgeFact,
    MemoryEntry,
    JobPosting,
    JobScore,
    Resume,
    Application,
    ResumeVersion,
    CoverLetter,
    ApplicationEvent,
    EmailAccount,
    StatusSignal,
)

#: Table names of :data:`MODEL_CLASSES`, in the same dependency order. This is the exact
#: creation order used by ``alembic/versions/0001_initial_schema.py``; reversed, it is the
#: drop order.
TABLE_NAMES: tuple[str, ...] = tuple(model.__tablename__ for model in MODEL_CLASSES)

__all__ += ["MODEL_CLASSES", "TABLE_NAMES"]
