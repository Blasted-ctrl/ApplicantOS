"""Pydantic request/response contracts shared with the desktop client.

This package is the HTTP boundary of ApplicantOS. Everything the API accepts or returns is
declared here, and ``desktop/src/lib/api/types.ts`` mirrors it — so a change to a model in
this package is a change to a published contract, not an implementation detail.

Three rules hold throughout:

1. **Every model sets ``from_attributes=True``** (inherited from
   :class:`~app.schemas.common.Schema`), so an ORM row validates directly:
   ``PostingRead.model_validate(posting)``.
2. **Every list endpoint returns** :class:`~app.schemas.common.Page`, and every ``*Update``
   model is a partial applied with ``model_dump(exclude_unset=True)`` — so "field absent"
   and "field explicitly null" stay distinguishable.
3. **No secret is ever a response field.** :class:`~app.schemas.settings.SettingsRead`
   exposes ``anthropic_configured`` booleans, never keys; credentials are accepted for
   *write* only, on :class:`~app.schemas.settings.SettingsUpdate`.

One subtlety worth internalising before adding a field: ``from_attributes`` reads attributes
eagerly, so a field named after a **lazily loaded** SQLAlchemy relationship will trigger
implicit IO and raise ``MissingGreenlet`` inside an async session. Fields here either mirror
an eagerly loaded (``selectin``) relationship, or carry a default and are populated by the
service layer. The two exceptions that require an explicit eager load are documented on the
models themselves (:class:`~app.schemas.session.SessionDetail`).

Import order below follows the dependency graph: shared envelopes, then leaf domains, then
the composites that nest them.
"""

from __future__ import annotations

# -- shared envelopes ------------------------------------------------------------------
from app.schemas.common import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    MIN_PAGE_LIMIT,
    ErrorResponse,
    OkResponse,
    Page,
    PaginationParams,
    Schema,
    paginate,
)

# -- identity and policy ---------------------------------------------------------------
from app.schemas.user import (
    CoverLetterPolicy,
    PreferencesRead,
    PreferencesUpdate,
    ProfileRead,
    ProfileUpdate,
    UserCreate,
    UserRead,
    UserUpdate,
)

# -- knowledge engine -------------------------------------------------------------------
from app.schemas.knowledge import (
    ChunkRead,
    DocumentRead,
    EdgeRead,
    EntityRead,
    FactRead,
    FactUpdate,
    GraphEdge,
    GraphNode,
    GraphView,
    IndexReportRead,
    KnowledgeSearchKind,
    KnowledgeSearchResult,
    KnowledgeStats,
    SourceCreate,
    SourceRead,
)

# -- onboarding (composes knowledge + user) -----------------------------------------------
from app.schemas.onboarding import (
    ONBOARDING_PAYLOAD_MODELS,
    ONBOARDING_STEP_KEYS,
    ContactPayload,
    DemographicsPayload,
    IdentityPayload,
    LinksPayload,
    MasterResumePayload,
    OnboardingField,
    OnboardingStatus,
    OnboardingStep,
    OnboardingStepKey,
    PreferencesPayload,
    SourcesPayload,
    WorkAuthorizationPayload,
)

# -- scoring -------------------------------------------------------------------------------
from app.schemas.scoring import (
    MAX_NORMALIZED_SCORE,
    MIN_NORMALIZED_SCORE,
    ScoreComponentRead,
    ScoreRead,
    ScoreRuleSchema,
    ScoreVerdict,
    ScoringRulesUpdate,
)

# -- postings and companies ------------------------------------------------------------------
from app.schemas.posting import (
    DEFAULT_DISCOVER_LIMIT,
    CompanyRead,
    DiscoverRequest,
    PostingFilter,
    PostingRead,
    PostingSummary,
)

# -- resumes ------------------------------------------------------------------------------------
from app.schemas.resume import (
    DEFAULT_RENDER_FORMAT,
    DEFAULT_RESUME_TEMPLATE,
    RenderFormat,
    ResumeContactSchema,
    ResumeCreate,
    ResumeDocumentSchema,
    ResumeEntrySchema,
    ResumePreviewRequest,
    ResumeRead,
    ResumeSectionSchema,
    ResumeVersionRead,
)

# -- applications and the review queue ----------------------------------------------------------
from app.schemas.application import (
    ApplicationDetail,
    ApplicationEventRead,
    ApplicationFilter,
    ApplicationRead,
    ApplicationUpdate,
    ArtifactRead,
    CoverLetterRead,
    ReviewField,
    ReviewItem,
    ReviewResolve,
)

# -- run sessions --------------------------------------------------------------------------------
from app.schemas.session import (
    DEFAULT_SESSION_TRIGGER,
    SessionDetail,
    SessionRead,
    SessionStartRequest,
)

# -- dashboard and analytics -----------------------------------------------------------------------
from app.schemas.dashboard import (
    MIN_INSIGHT_SAMPLE_SIZE,
    AnalyticsOverview,
    DashboardStats,
    FunnelStage,
    InsightItem,
    InsightSentiment,
    TimeseriesPoint,
)

# -- settings and plugins ----------------------------------------------------------------------------
from app.schemas.settings import (
    DatabaseBackend,
    PluginInfo,
    SettingsRead,
    SettingsUpdate,
)

__all__ = [
    # -- common -------------------------------------------------------------------
    "DEFAULT_PAGE_LIMIT",
    "ErrorResponse",
    "MAX_PAGE_LIMIT",
    "MIN_PAGE_LIMIT",
    "OkResponse",
    "Page",
    "PaginationParams",
    "Schema",
    "paginate",
    # -- user ---------------------------------------------------------------------
    "CoverLetterPolicy",
    "PreferencesRead",
    "PreferencesUpdate",
    "ProfileRead",
    "ProfileUpdate",
    "UserCreate",
    "UserRead",
    "UserUpdate",
    # -- onboarding ----------------------------------------------------------------
    "ContactPayload",
    "DemographicsPayload",
    "IdentityPayload",
    "LinksPayload",
    "MasterResumePayload",
    "ONBOARDING_PAYLOAD_MODELS",
    "ONBOARDING_STEP_KEYS",
    "OnboardingField",
    "OnboardingStatus",
    "OnboardingStep",
    "OnboardingStepKey",
    "PreferencesPayload",
    "SourcesPayload",
    "WorkAuthorizationPayload",
    # -- knowledge -------------------------------------------------------------------
    "ChunkRead",
    "DocumentRead",
    "EdgeRead",
    "EntityRead",
    "FactRead",
    "FactUpdate",
    "GraphEdge",
    "GraphNode",
    "GraphView",
    "IndexReportRead",
    "KnowledgeSearchKind",
    "KnowledgeSearchResult",
    "KnowledgeStats",
    "SourceCreate",
    "SourceRead",
    # -- scoring ----------------------------------------------------------------------
    "MAX_NORMALIZED_SCORE",
    "MIN_NORMALIZED_SCORE",
    "ScoreComponentRead",
    "ScoreRead",
    "ScoreRuleSchema",
    "ScoreVerdict",
    "ScoringRulesUpdate",
    # -- postings ----------------------------------------------------------------------
    "CompanyRead",
    "DEFAULT_DISCOVER_LIMIT",
    "DiscoverRequest",
    "PostingFilter",
    "PostingRead",
    "PostingSummary",
    # -- resumes ------------------------------------------------------------------------
    "DEFAULT_RENDER_FORMAT",
    "DEFAULT_RESUME_TEMPLATE",
    "RenderFormat",
    "ResumeContactSchema",
    "ResumeCreate",
    "ResumeDocumentSchema",
    "ResumeEntrySchema",
    "ResumePreviewRequest",
    "ResumeRead",
    "ResumeSectionSchema",
    "ResumeVersionRead",
    # -- applications --------------------------------------------------------------------
    "ApplicationDetail",
    "ApplicationEventRead",
    "ApplicationFilter",
    "ApplicationRead",
    "ApplicationUpdate",
    "ArtifactRead",
    "CoverLetterRead",
    "ReviewField",
    "ReviewItem",
    "ReviewResolve",
    # -- sessions --------------------------------------------------------------------------
    "DEFAULT_SESSION_TRIGGER",
    "SessionDetail",
    "SessionRead",
    "SessionStartRequest",
    # -- dashboard --------------------------------------------------------------------------
    "AnalyticsOverview",
    "DashboardStats",
    "FunnelStage",
    "InsightItem",
    "InsightSentiment",
    "MIN_INSIGHT_SAMPLE_SIZE",
    "TimeseriesPoint",
    # -- settings ----------------------------------------------------------------------------
    "DatabaseBackend",
    "PluginInfo",
    "SettingsRead",
    "SettingsUpdate",
]
