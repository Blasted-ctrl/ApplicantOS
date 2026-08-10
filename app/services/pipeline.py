"""The pipeline — discovery to submission, with the safety envelope wrapped around it (§13).

```
discover → ingest → score → prepare (retrieve · tailor · render)
    → submit (guard · apply · verify) → cleanup
```

Every module below this one does one job well and knows nothing about the others. This file
is where they meet, which makes it the only place the product's *policy* lives — and policy,
here, is mostly about refusing to act.

**The guard ladder in :meth:`Pipeline.submit` is the most important code in the repository.**
It runs in a fixed order, and every rung returns without touching a browser:

1. The application is already ``submitted`` or ``confirmed`` → refuse. This is the in-process
   half of golden rule #1; ``UNIQUE(user_id, posting_id)`` is the other half. A guard that
   ran *after* the network call would not be a guard.
2. The user is at ``settings.max_applications_per_day`` → refuse, and leave the application
   ``ready`` so tomorrow's run picks it up. A rate limit is not a review item; there is
   nothing for a human to decide.
3. The posting scored below ``settings.auto_apply_min_score`` → refuse. An unscored posting
   is refused too: the gate cannot be satisfied by a number that does not exist.
4. The provider declares ``supports_auto_apply = False`` → ``needs_review`` with
   :attr:`~app.models.enums.ReviewReason.UNSUPPORTED_FLOW`. LinkedIn and Workday live here
   permanently (golden rule #10), and the user gets a link to apply by hand.
5. ``settings.is_submission_allowed`` is ``False`` → ``needs_review`` with
   :attr:`~app.models.enums.ReviewReason.POLICY_BLOCK`. Both switches default closed, so
   **this is the rung a fresh install stops on**, having done all the useful work first.

Only past all five does a provider see an :class:`~app.jobs.base.ApplyContext`.

:meth:`Pipeline.prepare` has a sixth refusal of its own, one rung earlier: a posting body that
:func:`app.ai.untrusted.sanitize_external_text` scores as a prompt injection
(``docs/CONTRACTS.md`` §10b) never reaches a model at all, and the application goes to
``needs_review`` with :attr:`~app.models.enums.ReviewReason.POLICY_BLOCK` rather than to
``failed`` — a failure would be retried, and retrying an injection only replays it.

**Documents are disposable; knowledge is not.** ``ResumeVersion.content_json`` is written
once and kept forever, and :meth:`Pipeline.cleanup_application` deletes the rendered PDF from
disk and from storage while leaving that column untouched (golden rule #6). Anything rendered
can be re-rendered from it, which is exactly what :meth:`Pipeline.submit` does when a retry
finds the temp file gone.

**No stage propagates an unexpected exception.** Each one is wrapped so a crash becomes a
``failed`` application carrying ``last_error``, because an exception escaping into a Celery
worker leaves the row in ``preparing`` forever and golden rule #8 says a crash resumes rather
than restarts. Lookup failures *before* an application exists are the exception to the
exception: there is no row to record them on, so they propagate as :class:`LookupError`.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import func, select

from app.ai.scoring import VERDICT_SKIP
from app.ai.untrusted import UntrustedContentError
from app.jobs.base import (
    ApplyContext,
    ApplyResult,
    JobPostingDTO,
    ProviderError,
    UnsupportedFlowError,
    UserProfileDTO,
)
from app.models.application import Application
from app.models.cover_letter import CoverLetter
from app.models.enums import ApplicationStatus, DocumentKind, ReviewReason
from app.models.file import UploadedFile
from app.models.posting import JobPosting
from app.models.resume import Resume, ResumeVersion
from app.models.score import JobScore
from app.models.user import User
from app.observability.metrics import observe_apply
from app.services.application_service import ApplicationService, InvalidTransition
from app.services.dedupe_service import DedupeService
from app.services.discovery_service import DiscoveryService

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config.settings import Settings
    from app.documents.models import ResumeDocument
    from app.documents.renderer import RenderResult
    from app.jobs.base import ATSProvider, RawPosting, SearchQuery
    from app.models.user import UserPreferences

__all__ = [
    "FALLBACK_RENDER_FORMAT",
    "FALLBACK_TEMPLATE",
    "MEMORY_IDS_SUMMARY_KEY",
    "PREPARABLE_STATES",
    "RENDER_DIR_NAME",
    "VERDICT_ALREADY_APPLIED",
    "VERDICT_BLOCKED",
    "VERDICT_FAILED",
    "VERDICT_NEEDS_REVIEW",
    "VERDICT_PREPARED",
    "VERDICT_SKIPPED",
    "VERDICT_SUBMITTED",
    "Pipeline",
    "PipelineResult",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Vocabulary
# ======================================================================================

#: A real submission happened and the employer confirmed it.
VERDICT_SUBMITTED: Final[str] = "submitted"

#: The run stopped and asked for a human. Golden rule #2 — a normal outcome, never retried.
VERDICT_NEEDS_REVIEW: Final[str] = "needs_review"

#: Policy said no: the kill switch, dry run, or the daily cap.
VERDICT_BLOCKED: Final[str] = "blocked"

#: Nothing was wrong and nothing was done — the score was too low, or the application was
#: not in a state a submission could start from.
VERDICT_SKIPPED: Final[str] = "skipped"

#: Something broke. ``last_error`` on the application carries the detail.
VERDICT_FAILED: Final[str] = "failed"

#: The employer already has this application. The never-apply-twice refusal.
VERDICT_ALREADY_APPLIED: Final[str] = "already_applied"

#: Documents were generated; no submission was attempted in this call.
VERDICT_PREPARED: Final[str] = "prepared"

#: Stage names carried on :class:`PipelineResult`, so a caller can tell "refused before we
#: started" from "broke halfway through the browser flow".
_STAGE_GUARD: Final[str] = "guard"
_STAGE_SCORE: Final[str] = "score"
_STAGE_PREPARE: Final[str] = "prepare"
_STAGE_SUBMIT: Final[str] = "submit"

#: Directory under ``settings.data_path`` holding the rendered files an apply flow uploads
#: from. Temporary by construction: everything in it can be rebuilt from
#: ``ResumeVersion.content_json``, and :meth:`Pipeline.cleanup_application` empties it.
RENDER_DIR_NAME: Final[str] = "renders"

#: Template used when the configured one cannot render on this machine. ``markdown`` has no
#: dependencies at all, which is what keeps the zero-install path working: a box without
#: ``tectonic`` still produces a real, uploadable document instead of a failed application.
FALLBACK_TEMPLATE: Final[str] = "markdown"

#: Format the fallback template emits.
FALLBACK_RENDER_FORMAT: Final[str] = "md"

#: Statuses :meth:`Pipeline.prepare` will actually do work from. Everything else — most
#: importantly ``ready`` itself and every post-submit state — is a **no-op**, which is what
#: makes ``prepare`` idempotent (``docs/CONTRACTS.md`` §13) and what stops a re-run from
#: burning tokens regenerating a resume that already exists.
PREPARABLE_STATES: Final[frozenset[ApplicationStatus]] = frozenset(
    {
        ApplicationStatus.DRAFT,
        ApplicationStatus.PREPARING,
        ApplicationStatus.NEEDS_REVIEW,
        ApplicationStatus.FAILED,
    }
)

#: Key under which :meth:`Pipeline._generate_documents` reports the
#: :class:`~app.models.knowledge.MemoryEntry` ids the résumé engine injected. It travels in the
#: summary rather than as a second return value so the ``ready`` event carries the provenance
#: too — "these are the lessons that shaped this document" is exactly what a user asking *why
#: does it word things this way* needs to see.
MEMORY_IDS_SUMMARY_KEY: Final[str] = "memory_ids"

#: Statuses an :class:`~app.jobs.base.ApplyResult` may legitimately ask for. Anything else
#: coming back from a provider is a provider bug, and the safe reading of a provider bug is
#: "a human should look at this" rather than "assume it worked".
_APPLY_RESULT_TARGETS: Final[frozenset[ApplicationStatus]] = frozenset(
    {
        ApplicationStatus.CONFIRMED,
        ApplicationStatus.SUBMITTED,
        ApplicationStatus.NEEDS_REVIEW,
        ApplicationStatus.FAILED,
    }
)

#: MIME types for the formats a render can produce, so a download response is correct.
_CONTENT_TYPES: Final[dict[str, str]] = {
    "pdf": "application/pdf",
    "md": "text/markdown",
    "html": "text/html",
    "tex": "application/x-tex",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}

#: Read size when hashing a stored artifact. Large enough to be one syscall for a resume,
#: small enough that a stray multi-megabyte screenshot does not sit in memory.
_HASH_CHUNK_BYTES: Final[int] = 1 << 20

#: Name of the default resume variant created for a user who has none.
_DEFAULT_RESUME_NAME: Final[str] = "Tailored"


@dataclass(slots=True)
class PipelineResult:
    """The outcome of one pipeline call (``docs/CONTRACTS.md`` §13).

    Deliberately not a success/failure boolean. "Refused because the kill switch is off",
    "refused because we already applied", "stopped to ask a human" and "the browser crashed"
    are four completely different things, and a caller that cannot tell them apart will
    retry the ones it must not retry. :attr:`verdict` is the discriminator.

    Attributes:
        verdict: One of :data:`VERDICT_SUBMITTED`, :data:`VERDICT_NEEDS_REVIEW`,
            :data:`VERDICT_BLOCKED`, :data:`VERDICT_SKIPPED`, :data:`VERDICT_FAILED`,
            :data:`VERDICT_ALREADY_APPLIED`, :data:`VERDICT_PREPARED`.
        stage: Which stage produced it — ``guard``, ``score``, ``prepare`` or ``submit``.
        posting_id: The posting involved, when known.
        application_id: The application involved, when one exists.
        status: The application's status after the call.
        review_reason: Why a human is needed, when one is.
        score: The posting's normalised 0–100 score, when it was consulted.
        submitted: Whether an application genuinely reached an employer. **Only ever
            ``True`` on the** :data:`VERDICT_SUBMITTED` **path.**
        duration_seconds: Wall-clock time this call took.
        message: One line safe to show a user.
        error: Failure detail, when there was one.
        payload: Structured context — provider name, confirmation id, blocked switches.
    """

    verdict: str
    stage: str
    posting_id: uuid.UUID | None = None
    application_id: uuid.UUID | None = None
    status: ApplicationStatus | None = None
    review_reason: ReviewReason | None = None
    score: int | None = None
    submitted: bool = False
    duration_seconds: float = 0.0
    message: str = ""
    error: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Whether an application genuinely reached an employer."""
        return self.verdict == VERDICT_SUBMITTED and self.submitted

    @property
    def needs_human(self) -> bool:
        """Whether this outcome is waiting on a person."""
        return self.verdict == VERDICT_NEEDS_REVIEW

    @property
    def retryable(self) -> bool:
        """Whether re-running this call could plausibly produce a different outcome.

        ``needs_review`` is **not** retryable: it is a request for a decision, and a worker
        that retried it would ask the same unanswerable question forever
        (``docs/CONTRACTS.md`` §15).
        """
        return self.verdict in {VERDICT_FAILED, VERDICT_BLOCKED}

    def as_dict(self) -> dict[str, Any]:
        """Return the result as a JSON-ready mapping.

        Returns:
            Every field, with enums and UUIDs flattened to strings, in the shape the API and
            the event bus publish.
        """
        return {
            "verdict": self.verdict,
            "stage": self.stage,
            "posting_id": str(self.posting_id) if self.posting_id else None,
            "application_id": str(self.application_id) if self.application_id else None,
            "status": self.status.value if self.status else None,
            "review_reason": self.review_reason.value if self.review_reason else None,
            "score": self.score,
            "submitted": self.submitted,
            "duration_seconds": round(self.duration_seconds, 3),
            "message": self.message,
            "error": self.error,
            "payload": dict(self.payload),
        }


class Pipeline:
    """Orchestrates one user's journey from a job board to a submitted application.

    Args:
        session: The unit of work. The pipeline commits at stage boundaries so a crash
            resumes rather than restarts.
        settings: Application settings. The two safety switches are read from here on
            **every** submission attempt rather than cached, so flipping the kill switch
            takes effect on the next application and not on the next process restart.
        applications: Explicit application state machine; built over *session* when omitted.
        dedupe: Explicit dedupe service; built over *session* when omitted.
        discovery: Explicit discovery service; built over *session* and *settings* when
            omitted.

    Usage::

        pipeline = Pipeline(session, get_settings())
        result = await pipeline.run_one(posting.id, user.id)
        if result.needs_human:
            ...
    """

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        *,
        applications: ApplicationService | None = None,
        dedupe: DedupeService | None = None,
        discovery: DiscoveryService | None = None,
    ) -> None:
        """Bind the pipeline to a session, its settings and its collaborators."""
        self._session = session
        self._settings = settings
        self._applications = (
            applications if applications is not None else ApplicationService(session)
        )
        self._dedupe = dedupe if dedupe is not None else DedupeService(session)
        self._discovery = (
            discovery
            if discovery is not None
            else DiscoveryService(session, settings, dedupe=self._dedupe)
        )
        self._cache: Any | None = None
        self._llm: Any | None = None

    # ----------------------------------------------------------------------------------
    # Front half: discover, ingest, score
    # ----------------------------------------------------------------------------------

    async def discover(
        self,
        user_id: uuid.UUID | str,
        providers: Sequence[str] | None = None,
        query: SearchQuery | None = None,
    ) -> int:
        """Poll every selected board and ingest what it returns.

        Delegates wholesale to :class:`~app.services.discovery_service.DiscoveryService`,
        which isolates each provider so one rate-limited board cannot empty the feed.

        Args:
            user_id: Whose preferences drive the run.
            providers: Provider names to poll, or ``None`` for the user's enabled set.
            query: An explicit query, or ``None`` to build one from preferences.

        Returns:
            How many genuinely new postings were created. Postings that deduplicated onto an
            existing row are not counted — they are not new work.

        Raises:
            LookupError: If no user with *user_id* exists.
        """
        started = time.monotonic()
        report = await self._discovery.discover(user_id, providers, query)
        await self._session.commit()

        logger.info(
            "pipeline.discovered",
            user_id=str(user_id),
            found=report.found,
            created=report.created,
            deduped=report.deduped,
            skipped=report.skipped,
            errors=len(report.errors),
            duration_seconds=round(time.monotonic() - started, 3),
        )
        return report.created

    async def ingest(self, raw: RawPosting) -> tuple[JobPosting, bool]:
        """Persist one discovered posting, deduplicating it against everything known.

        Args:
            raw: The posting as a provider produced it.

        Returns:
            ``(posting, created)`` — the persisted row and whether this call inserted it.
        """
        posting, created = await self._dedupe.upsert(raw)
        await self._session.commit()
        logger.debug(
            "pipeline.ingested",
            posting_id=str(posting.id),
            provider=str(posting.provider),
            created=created,
        )
        return posting, created

    async def score_posting(
        self,
        posting_id: uuid.UUID | str,
        user_id: uuid.UUID | str,
    ) -> JobScore:
        """Score one posting against one user and persist the verdict.

        Args:
            posting_id: The posting to judge.
            user_id: Whose preferences to judge it against.

        Returns:
            The persisted :class:`~app.models.score.JobScore`, upserted on
            ``UNIQUE(posting_id, user_id)`` so re-scoring replaces rather than accumulates.

        Raises:
            LookupError: If either identifier is malformed, if the user does not exist, or
                if the posting does not exist.
        """
        posting_uuid = _as_uuid(posting_id, "posting id")
        user_uuid = _as_uuid(user_id, "user id")
        await self._load_posting(posting_uuid)

        await self._discovery.score_new(user_uuid, [posting_uuid])
        await self._session.commit()

        score = await self._score_for(posting_uuid, user_uuid)
        if score is None:  # pragma: no cover - score_new writes exactly this row
            raise LookupError(
                f"scoring produced no row for posting {posting_uuid} and user {user_uuid}"
            )

        logger.info(
            "pipeline.scored",
            posting_id=str(posting_uuid),
            user_id=str(user_uuid),
            normalized=score.normalized,
            verdict=score.verdict,
        )
        return score

    # ----------------------------------------------------------------------------------
    # Prepare
    # ----------------------------------------------------------------------------------

    async def prepare(
        self,
        posting_id: uuid.UUID | str,
        user_id: uuid.UUID | str,
    ) -> Application:
        """Generate this application's documents and leave it ``ready`` to submit.

        The expensive stage: knowledge retrieval, tailoring, rendering, and (when policy
        asks for one) a cover letter. It is **idempotent**. An application that has already
        reached ``ready`` — or that has been submitted, confirmed, or abandoned — is returned
        untouched, with no model call, no render and no new
        :class:`~app.models.resume.ResumeVersion` row. Only the states in
        :data:`PREPARABLE_STATES` do work, which is what lets a scheduler call this on every
        pass without cost and what lets a crashed run resume from ``preparing``.

        Nothing on the resume is invented: :class:`~app.ai.resume_engine.ResumeEngine` selects
        and rewrites facts retrieved from the user's knowledge graph and returns their ids,
        which are stored on ``ResumeVersion.fact_ids`` (golden rule #7). The document itself
        is stored as ``content_json`` and kept forever; the rendered file is disposable.

        Args:
            posting_id: The posting to apply to.
            user_id: The applicant.

        Returns:
            The application. ``ready`` on success, ``failed`` when generation broke — the
            exception is recorded rather than raised, so a batch run continues.

        Raises:
            LookupError: If the posting or the user does not exist. There is no application
                row to record that on, so it propagates.
        """
        posting = await self._load_posting(posting_id)
        user = await self._load_user(user_id)

        application, created = await self._applications.create_or_get(user.id, posting.id)
        log = logger.bind(
            application_id=str(application.id),
            user_id=str(user.id),
            posting_id=str(posting.id),
        )

        if application.status not in PREPARABLE_STATES:
            log.info(
                "pipeline.prepare_noop",
                status=application.status.value,
                resume_version_id=str(application.resume_version_id or "") or None,
            )
            return application

        started = time.monotonic()
        try:
            await self._applications.transition(
                application,
                ApplicationStatus.PREPARING,
                message="Generating tailored documents.",
                payload={"created": created},
            )
            summary = await self._generate_documents(application, user, posting)

            # Golden rule #7 has a corollary nobody wrote down: if the knowledge graph holds
            # nothing relevant, the honest output is an *empty* resume — and we may not fill
            # the gap with invented content. Without this check `prepare` cheerfully marks
            # that ready to send. The usual cause is a user who finished onboarding before
            # their sources finished indexing, and the result is a PDF containing only a
            # contact header. Not applying is strictly better than applying with that.
            if summary.get("bullets", 0) <= 0 or summary.get("facts", 0) <= 0:
                log.warning(
                    "pipeline.prepare_empty_resume",
                    bullets=summary.get("bullets", 0),
                    facts=summary.get("facts", 0),
                )
                await self._applications.mark_needs_review(
                    application,
                    ReviewReason.INSUFFICIENT_KNOWLEDGE,
                    payload={
                        **summary,
                        "hint": (
                            "No relevant experience was found for this role, so the resume "
                            "would have been empty. Add a knowledge source (GitHub, a project "
                            "folder, or an existing resume) and index it, then retry."
                        ),
                    },
                )
                return application

            await self._applications.transition(
                application,
                ApplicationStatus.READY,
                message="Documents ready.",
                payload=summary,
            )
            # The outcome is known only here. Every earlier return from this method is an
            # escalation to a human or a failure, and a memory that preceded one of those has
            # earned nothing. Reaching `ready` is the clean branch, so the memories that shaped
            # the résumé get their weight — the supervised half of the loop.
            await self._reinforce_memories(summary.get(MEMORY_IDS_SUMMARY_KEY) or [])
        except asyncio.CancelledError:
            raise
        except UntrustedContentError as exc:
            # `docs/CONTRACTS.md` §10b. The posting body is attacker-controlled text and it
            # scored HIGH, so the résumé engine and the letter writer both refused to read it.
            # This is a policy decision, not a failure: `failed` would put the application in
            # the retry population, and retrying an injection just replays it.
            log.warning(
                "pipeline.prepare_blocked",
                score=exc.verdict.score,
                signals=exc.verdict.signals,
                duration_seconds=round(time.monotonic() - started, 3),
            )
            await self._applications.mark_needs_review(
                application,
                exc.review_reason,
                payload={
                    "untrusted": exc.verdict.as_dict(),
                    "source": exc.source,
                    "hint": (
                        "This job description contains text that tries to give instructions "
                        "to the AI writing your application. Nothing was generated from it. "
                        "Read the posting yourself and decide whether to apply by hand."
                    ),
                },
            )
            return application
        except Exception as exc:
            await self._fail(application, exc, stage=_STAGE_PREPARE)
            log.warning(
                "pipeline.prepare_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                duration_seconds=round(time.monotonic() - started, 3),
            )
            return application

        log.info(
            "pipeline.prepared",
            duration_seconds=round(time.monotonic() - started, 3),
            **summary,
        )
        return application

    async def _generate_documents(
        self,
        application: Application,
        user: User,
        posting: JobPosting,
    ) -> dict[str, Any]:
        """Tailor, render and persist this application's resume and optional cover letter.

        Args:
            application: The application being prepared, already ``preparing``.
            user: The applicant, with their profile loaded.
            posting: The posting being applied to.

        Returns:
            A JSON-ready summary of what was produced, recorded on the ``ready`` event:
            bullet and fact counts, the sections generated, whether the LLM path degraded,
            whether a cover letter was written, and — under
            :data:`MEMORY_IDS_SUMMARY_KEY` — which of the user's own recorded lessons shaped
            the résumé. That last one is both the audit trail behind "why does it word things
            this way" and the input :meth:`_reinforce_memories` reads once the outcome is
            known.
        """
        from app.ai.cover_letter import CoverLetterRequest, CoverLetterWriter
        from app.ai.resume_engine import ResumeEngine, TailorRequest
        from app.knowledge.retrieval import KnowledgeRetriever

        prefs: UserPreferences = user.prefs
        posting_dto = JobPostingDTO.from_model(posting)
        user_dto = UserProfileDTO.from_model(user)
        template = (prefs.resume_template or self._settings.resume_template).strip()

        engine = ResumeEngine(
            self._session,
            self._llm_client(),
            KnowledgeRetriever(self._session),
            self._cache_client(),
        )
        tailored = await engine.tailor(
            TailorRequest(
                user=user_dto,
                posting=posting_dto,
                prefs=prefs,
                template=template,
                variant_label=prefs.resume_variant,
            )
        )

        directory = self._render_dir(application.id)
        render = await self._render_resume_file(tailored.document, directory, template)
        stored = await self._store_artifact(
            render.path,
            user_id=user.id,
            application_id=application.id,
            kind=DocumentKind.TAILORED_RESUME,
        )

        version = await self._persist_resume_version(
            application, user, tailored, render, stored, template
        )
        application.resume_version_id = version.id
        application.ai_reasoning = tailored.reasoning or None

        summary: dict[str, Any] = {
            "resume_version_id": str(version.id),
            "bullets": tailored.document.total_bullets(),
            "facts": len(tailored.selected_fact_ids),
            "sections": [section.heading for section in tailored.document.sections],
            "render_format": version.render_format,
            "template": render.template,
            "engine": render.engine,
            "page_count": render.page_count,
            "degraded": tailored.degraded,
            "cached": tailored.cached,
            "cover_letter": False,
            MEMORY_IDS_SUMMARY_KEY: list(tailored.memory_ids),
        }

        score = await self._score_for(posting.id, user.id)
        normalized = int(score.normalized) if score is not None else None
        writer = CoverLetterWriter(self._llm_client(), self._cache_client())
        if writer.should_write(posting_dto, prefs, score=normalized):
            letter = await writer.write(
                CoverLetterRequest(
                    user=user_dto,
                    posting=posting_dto,
                    resume=tailored.document,
                    prefs=prefs,
                    score=normalized,
                )
            )
            record = await self._persist_cover_letter(
                application, user, posting, letter, directory, template
            )
            application.cover_letter_id = record.id
            summary["cover_letter"] = True
            summary["cover_letter_words"] = letter.word_count()

        await self._session.flush()
        return summary

    async def _reinforce_memories(self, memory_ids: Sequence[str]) -> int:
        """Credit the memories that shaped a résumé which did not need a human.

        Best-effort in the strongest sense: the application is already ``ready`` and committed
        when this runs, so a memory store that is unavailable costs a slightly worse ranking
        next week and nothing else. It must never turn a successful preparation into a failure.

        Args:
            memory_ids: The ids :meth:`_generate_documents` returned.

        Returns:
            How many memories were reinforced.
        """
        if not memory_ids:
            return 0

        from app.ai.memory_prompt import reinforce_used
        from app.ai.resume_engine import MEMORY_PURPOSE
        from app.knowledge.memory import MemoryStore

        try:
            return await reinforce_used(
                MemoryStore(self._session), memory_ids, purpose=MEMORY_PURPOSE
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "pipeline.memory_reinforcement_failed",
                memories=len(memory_ids),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return 0

    async def _persist_resume_version(
        self,
        application: Application,
        user: User,
        tailored: Any,
        render: RenderResult,
        stored: UploadedFile,
        template: str,
    ) -> ResumeVersion:
        """Write the generated resume to the database.

        ``content_json`` is the permanent artefact and ``file_id`` the disposable one; the
        two are written together here so that a version row can never exist without the
        content it claims to describe.

        Args:
            application: The application this version was tailored for.
            user: The owning user.
            tailored: The :class:`~app.ai.resume_engine.TailorResult`.
            render: What the renderer produced.
            stored: The catalogued file holding the rendered bytes.
            template: The template requested (which may differ from the one that rendered).

        Returns:
            The persisted, flushed :class:`~app.models.resume.ResumeVersion`.
        """
        container = await self._resume_container(user, template)
        version = ResumeVersion(
            resume_id=container.id,
            application_id=application.id,
            version_number=await self._next_version_number(container.id),
            content_json=tailored.document.model_dump(mode="json"),
            render_format=_format_of(render.path),
            file_id=stored.id,
            fact_ids=[str(fact_id) for fact_id in tailored.selected_fact_ids],
            token_usage=dict(tailored.token_usage or {}),
            reasoning=tailored.reasoning or None,
        )
        self._session.add(version)
        await self._session.flush()
        return version

    async def _persist_cover_letter(
        self,
        application: Application,
        user: User,
        posting: JobPosting,
        letter: Any,
        directory: Path,
        template: str,
    ) -> CoverLetter:
        """Render and persist the cover letter.

        Args:
            application: The application the letter belongs to.
            user: The owning user.
            posting: The posting being applied to.
            letter: The :class:`~app.ai.cover_letter.CoverLetterResult`.
            directory: The application's render directory.
            template: The template to match the resume with.

        Returns:
            The persisted, flushed :class:`~app.models.cover_letter.CoverLetter`.
        """
        render = await self._render_cover_letter_file(letter.document, directory, template)
        stored = await self._store_artifact(
            render.path,
            user_id=user.id,
            application_id=application.id,
            kind=DocumentKind.COVER_LETTER,
        )
        record = CoverLetter(
            user_id=user.id,
            posting_id=posting.id,
            application_id=application.id,
            body=letter.body,
            tone=letter.tone,
            file_id=stored.id,
            token_usage=dict(letter.token_usage or {}),
        )
        self._session.add(record)
        await self._session.flush()
        return record

    # ----------------------------------------------------------------------------------
    # Submit — the guarded path
    # ----------------------------------------------------------------------------------

    async def submit(self, application_id: uuid.UUID | str) -> PipelineResult:
        """Attempt one real submission, behind the full guard ladder.

        The ladder is documented at the top of this module and implemented here in exactly
        that order. Read the order as load-bearing: the never-apply-twice check runs before
        anything is loaded, resolved or opened, so no amount of failure further down can skip
        it.

        Produces ``applicantos_apply_duration_seconds{provider}`` (``docs/CONTRACTS.md``
        §16) around the provider call itself — on every exit from it, not only the
        successful one. A guard that refuses before a provider is ever reached records
        nothing, which is correct: no attempt was made.

        Args:
            application_id: The application to submit.

        Returns:
            A :class:`PipelineResult` whose :attr:`~PipelineResult.verdict` says what
            happened. ``submitted=True`` appears on exactly one path.

        Raises:
            LookupError: If no such application exists.
        """
        application = await self._applications.get(application_id)
        started = time.monotonic()
        log = logger.bind(
            application_id=str(application.id),
            user_id=str(application.user_id),
            posting_id=str(application.posting_id),
        )

        # -- 1. never apply twice (golden rule #1) --------------------------------------
        if application.status.is_post_submit():
            log.warning("pipeline.submit_refused_already_applied", status=application.status.value)
            return self._result(
                VERDICT_ALREADY_APPLIED,
                _STAGE_GUARD,
                application,
                started,
                message=(f"Already applied — this application is {application.status.value}."),
            )
        if not application.can_submit:
            log.info("pipeline.submit_refused_terminal", status=application.status.value)
            return self._result(
                VERDICT_SKIPPED,
                _STAGE_GUARD,
                application,
                started,
                message=f"Application is {application.status.value}; nothing to submit.",
            )
        if application.status is not ApplicationStatus.READY:
            log.info("pipeline.submit_not_ready", status=application.status.value)
            return self._result(
                VERDICT_SKIPPED,
                _STAGE_GUARD,
                application,
                started,
                message=(
                    f"Application is {application.status.value}, not ready; prepare it first."
                ),
            )

        # -- 2. daily cap ----------------------------------------------------------------
        used = await self._applications.daily_count(application.user_id)
        cap = int(self._settings.max_applications_per_day)
        if used >= cap:
            log.info("pipeline.submit_rate_limited", used=used, cap=cap)
            return self._result(
                VERDICT_BLOCKED,
                _STAGE_GUARD,
                application,
                started,
                review_reason=ReviewReason.RATE_LIMITED,
                message=f"Daily limit reached ({used}/{cap}); will resume tomorrow.",
                payload={"submitted_today": used, "max_applications_per_day": cap},
            )

        # -- 3. score floor ---------------------------------------------------------------
        score = await self._score_for(application.posting_id, application.user_id)
        value = int(score.normalized) if score is not None else None
        floor = int(self._settings.auto_apply_min_score)
        if value is None or value < floor:
            log.info("pipeline.submit_below_score", score=value, minimum=floor)
            return self._result(
                VERDICT_SKIPPED,
                _STAGE_GUARD,
                application,
                started,
                score=value,
                message=(
                    f"Score {value} is below the auto-apply floor of {floor}."
                    if value is not None
                    else "Posting has not been scored; refusing to apply blind."
                ),
                payload={"auto_apply_min_score": floor},
            )

        posting = await self._load_posting(application.posting_id)
        provider_name = str(posting.provider)

        # -- 4. provider posture (golden rule #10) -----------------------------------------
        if not self._supports_auto_apply(provider_name):
            # Annotated because the kill-switch branch below reuses the name for a payload
            # carrying the two switch booleans; both go to ``mark_needs_review``, which takes
            # ``dict[str, Any]``.
            payload: dict[str, Any] = {
                "provider": provider_name,
                "apply_url": posting.apply_url or posting.url,
                "reason": "provider does not support automated submission",
            }
            await self._applications.mark_needs_review(
                application, ReviewReason.UNSUPPORTED_FLOW, payload
            )
            log.info("pipeline.submit_unsupported_flow", provider=provider_name)
            return self._result(
                VERDICT_NEEDS_REVIEW,
                _STAGE_GUARD,
                application,
                started,
                score=value,
                review_reason=ReviewReason.UNSUPPORTED_FLOW,
                message=f"{provider_name} requires a manual application.",
                payload=payload,
            )

        # -- 5. the kill switch (golden rule #3) --------------------------------------------
        if not self._settings.is_submission_allowed:
            payload = {
                "auto_apply_enabled": bool(self._settings.auto_apply_enabled),
                "dry_run": bool(self._settings.dry_run),
                "provider": provider_name,
                "apply_url": posting.apply_url or posting.url,
            }
            await self._applications.mark_needs_review(
                application, ReviewReason.POLICY_BLOCK, payload
            )
            log.info("pipeline.submit_policy_blocked", **payload)
            return self._result(
                VERDICT_BLOCKED,
                _STAGE_GUARD,
                application,
                started,
                score=value,
                review_reason=ReviewReason.POLICY_BLOCK,
                message=(
                    "Submission is disabled: auto_apply_enabled must be true and "
                    "dry_run must be false."
                ),
                payload=payload,
            )

        # -- 6. attempt ---------------------------------------------------------------------
        # Set once `transition(SUBMITTING)` has committed. From that moment a cancellation
        # would otherwise strand the row mid-flight, so the handler below has to clean up.
        submitting_committed = False
        try:
            user = await self._load_user(application.user_id)
            provider = self._provider(provider_name)
            resume_path, cover_path = await self._materialize_documents(application, posting)

            context = ApplyContext(
                application_id=application.id,
                posting=JobPostingDTO.from_model(posting),
                user=UserProfileDTO.from_model(user),
                resume_path=resume_path,
                cover_letter_path=cover_path,
                answers=dict(application.answers or {}),
                # Belt and braces: guard 5 already proved this is False. If that guard is
                # ever reordered away, the provider still receives the safe value.
                dry_run=not self._settings.is_submission_allowed,
                # Without this the field answerer has no retriever, so every free-text
                # form answer is written from the profile alone -- ignoring both the
                # knowledge graph and any correction the user has already made.
                knowledge=self._retriever(),
            )

            await self._applications.transition(
                application,
                ApplicationStatus.SUBMITTING,
                message=f"Submitting via {provider_name}.",
                payload={"provider": provider_name, "score": value},
            )

            submitting_committed = True

            attempt_started = time.monotonic()
            try:
                result = await provider.apply(context)
            finally:
                elapsed = time.monotonic() - attempt_started
                # ``applicantos_apply_duration_seconds{provider}`` (§16) measures the real
                # attempt, so it is observed in the ``finally`` and not on the success
                # path: an apply that spends ninety seconds in a browser and then escalates
                # to a human is exactly the shape this histogram exists to show. The
                # cancellation, provider-error and unexpected-exception handlers below all
                # unwind through here.
                observe_apply(provider_name, elapsed)
        except asyncio.CancelledError:
            # Golden rule #8. `transition(SUBMITTING)` has already committed, so without this
            # the row is durably `submitting` with no path out — and the caller cannot tell
            # the two cases apart: cancelled *before* the provider ran (nothing was sent) or
            # *after* it returned (the employer really has the application). Guard 3 refuses
            # to re-submit anything that is not READY, so this cannot double-apply — but it
            # would sit in the UI as permanently in-flight and never reach the review queue.
            #
            # Escalate rather than guess. VERIFICATION_FAILED is exactly right: we do not
            # know whether it was sent, and a human checking their inbox settles it in
            # seconds. Shielded so the cleanup is not itself cancelled mid-write.
            if submitting_committed:
                await self._abandon_in_flight(
                    application, started, score=value, provider=provider_name
                )
            raise
        except UnsupportedFlowError as exc:
            return await self._escalate(
                application,
                ReviewReason.UNSUPPORTED_FLOW,
                exc,
                started,
                score=value,
                payload={"provider": provider_name, "apply_url": posting.apply_url},
            )
        except ProviderError as exc:
            reason = exc.review_reason
            if reason is not None:
                return await self._escalate(
                    application,
                    reason,
                    exc,
                    started,
                    score=value,
                    payload={"provider": provider_name},
                )
            return await self._failed_result(application, exc, started, score=value)
        except Exception as exc:
            return await self._failed_result(application, exc, started, score=value)

        return await self._finalize(
            application, result, elapsed, started, score=value, provider=provider_name
        )

    async def _finalize(
        self,
        application: Application,
        result: ApplyResult,
        elapsed: float,
        started: float,
        *,
        score: int | None,
        provider: str,
    ) -> PipelineResult:
        """Record what the provider reported and move the application to match.

        Evidence is persisted **before** the transition, so a process killed between the two
        leaves an application in ``submitting`` with its screenshots already on disk — which
        a human can resolve — rather than a ``confirmed`` application with no proof.

        Args:
            application: The application that was attempted.
            result: What the provider returned.
            elapsed: Wall-clock seconds the provider call took, used when the provider did
                not time itself.
            started: Monotonic start of the whole :meth:`submit` call.
            score: The posting's normalised score, for the returned result.
            provider: The board that was applied through, passed in rather than re-read off
                the application so this never risks a lazy load inside async code.

        Returns:
            The pipeline's verdict.
        """
        screenshots = await self._store_screenshots(application, result.screenshot_paths)

        application.duration_seconds = float(result.duration_seconds or elapsed)
        application.confirmation_id = result.confirmation_id
        application.confirmation_text = result.confirmation_text
        application.external_application_id = result.external_application_id
        # Reassigned wholesale: JSON columns are not change tracked.
        application.browser_log = [dict(entry) for entry in result.browser_log]
        if screenshots:
            application.confirmation_screenshot_id = screenshots[-1].id
        await self._session.flush()

        target = ApplicationStatus.CONFIRMED if result.ok else result.status
        if target not in _APPLY_RESULT_TARGETS:
            logger.warning(
                "pipeline.unexpected_apply_status",
                application_id=str(application.id),
                reported=str(target),
            )
            target = ApplicationStatus.NEEDS_REVIEW

        payload: dict[str, Any] = {
            "provider": provider,
            "confirmation_id": result.confirmation_id,
            "screenshots": [str(file.id) for file in screenshots],
            "duration_seconds": round(application.duration_seconds or 0.0, 3),
        }
        if result.unanswered_fields:
            payload["unanswered_fields"] = [
                _field_payload(field_) for field_ in result.unanswered_fields
            ]
        if result.error:
            payload["error"] = result.error

        if target is ApplicationStatus.FAILED:
            await self._applications.record_error(
                application, result.error or "the apply flow failed without a message"
            )

        await self._applications.transition(
            application,
            target,
            reason=result.review_reason if target is ApplicationStatus.NEEDS_REVIEW else None,
            message=result.confirmation_text or result.error or f"Apply finished: {target.value}.",
            payload=payload,
        )

        if target is ApplicationStatus.CONFIRMED:
            await self.cleanup_application(application.id)

        verdict = {
            ApplicationStatus.CONFIRMED: VERDICT_SUBMITTED,
            ApplicationStatus.SUBMITTED: VERDICT_SUBMITTED,
            ApplicationStatus.NEEDS_REVIEW: VERDICT_NEEDS_REVIEW,
            ApplicationStatus.FAILED: VERDICT_FAILED,
        }[target]

        logger.info(
            "pipeline.submit_finished",
            application_id=str(application.id),
            verdict=verdict,
            status=target.value,
            review_reason=result.review_reason.value if result.review_reason else None,
            screenshots=len(screenshots),
            apply_seconds=round(elapsed, 3),
            duration_seconds=round(time.monotonic() - started, 3),
        )
        return self._result(
            verdict,
            _STAGE_SUBMIT,
            application,
            started,
            score=score,
            review_reason=result.review_reason,
            submitted=target.is_post_submit(),
            message=result.confirmation_text or result.error or "",
            error=result.error,
            payload=payload,
        )

    # ----------------------------------------------------------------------------------
    # Run one
    # ----------------------------------------------------------------------------------

    async def run_one(
        self,
        posting_id: uuid.UUID | str,
        user_id: uuid.UUID | str,
    ) -> PipelineResult:
        """Score, prepare and submit one posting, stopping at the first refusal.

        The unit of work a scheduler queues. Short-circuiting matters for cost as much as
        for safety: a posting the scorer rejects must never reach the resume engine, and an
        application that failed to prepare must never reach a browser.

        Args:
            posting_id: The posting to apply to.
            user_id: The applicant.

        Returns:
            The verdict of whichever stage stopped, with
            :attr:`~PipelineResult.duration_seconds` spanning the whole call.

        Raises:
            LookupError: If the posting or the user does not exist.
        """
        started = time.monotonic()
        posting_uuid = _as_uuid(posting_id, "posting id")

        score = await self.score_posting(posting_uuid, user_id)
        value = int(score.normalized or 0)
        floor = int(self._settings.auto_apply_min_score)
        if score.verdict == VERDICT_SKIP or value < floor:
            logger.info(
                "pipeline.run_one_skipped",
                posting_id=str(posting_uuid),
                score=value,
                minimum=floor,
                verdict=score.verdict,
            )
            return PipelineResult(
                verdict=VERDICT_SKIPPED,
                stage=_STAGE_SCORE,
                posting_id=posting_uuid,
                score=value,
                duration_seconds=time.monotonic() - started,
                message=f"Scored {value}; the floor is {floor}.",
                payload={"score_verdict": score.verdict},
            )

        application = await self.prepare(posting_uuid, user_id)
        if application.status is not ApplicationStatus.READY:
            verdict = _VERDICT_FOR_STATUS.get(application.status, VERDICT_SKIPPED)
            logger.info(
                "pipeline.run_one_stopped_after_prepare",
                application_id=str(application.id),
                status=application.status.value,
                verdict=verdict,
            )
            return self._result(
                verdict,
                _STAGE_PREPARE,
                application,
                started,
                score=value,
                review_reason=application.review_reason,
                message=f"Preparation ended in {application.status.value}.",
                error=application.last_error,
            )

        result = await self.submit(application.id)
        result.duration_seconds = time.monotonic() - started
        return result

    # ----------------------------------------------------------------------------------
    # Cleanup — golden rule #6
    # ----------------------------------------------------------------------------------

    async def cleanup_application(self, application_id: uuid.UUID | str) -> None:
        """Delete this application's rendered documents, keeping the knowledge behind them.

        Golden rule #6 in one method. Three things happen and one deliberately does not:

        * The rendered bytes are deleted from local storage **and** the temp render
          directory the browser uploaded from. A tailored resume is a person's full contact
          details and employment history sitting in a file; keeping it after it has been
          sent is a liability with no upside.
        * The catalogue rows (:class:`~app.models.file.UploadedFile`) are soft-deleted and
          unlinked, so nothing tries to serve bytes that are gone.
        * ``ResumeVersion.deleted_at`` is stamped, retiring the version from the documents UI.
        * **``ResumeVersion.content_json`` is not touched, ever.** It is the resume — the
          rendered file was only a view of it, and this method can be undone by rendering it
          again.

        Proof-of-submission screenshots are also left alone. They are evidence, not output.

        Args:
            application_id: The application to clean up.

        Raises:
            LookupError: If no such application exists.
        """
        application = await self._applications.get(application_id)
        removed_files = 0
        removed_bytes = 0

        versions = (
            (
                await self._session.execute(
                    select(ResumeVersion).where(ResumeVersion.application_id == application.id)
                )
            )
            .scalars()
            .unique()
            .all()
        )
        for version in versions:
            freed = await self._discard_artifact(version.file_id)
            if freed is not None:
                removed_files += 1
                removed_bytes += freed
            version.file_id = None
            if version.deleted_at is None:
                version.soft_delete()

        letters = (
            (
                await self._session.execute(
                    select(CoverLetter).where(CoverLetter.application_id == application.id)
                )
            )
            .scalars()
            .unique()
            .all()
        )
        for letter in letters:
            freed = await self._discard_artifact(letter.file_id)
            if freed is not None:
                removed_files += 1
                removed_bytes += freed
            letter.file_id = None

        removed_temp = self._remove_render_dir(application.id)

        application.add_event(
            "cleaned_up",
            message="Rendered documents deleted; knowledge retained.",
            payload={
                "files_deleted": removed_files,
                "bytes_freed": removed_bytes,
                "temp_files_deleted": removed_temp,
                "resume_versions_retired": len(versions),
            },
        )
        await self._session.flush()
        await self._session.commit()

        logger.info(
            "pipeline.cleaned_up",
            application_id=str(application.id),
            files_deleted=removed_files,
            bytes_freed=removed_bytes,
            temp_files_deleted=removed_temp,
            resume_versions_retired=len(versions),
        )

    async def _discard_artifact(self, file_id: uuid.UUID | None) -> int | None:
        """Delete one catalogued file's bytes and soft-delete its row.

        Args:
            file_id: The file to discard, or ``None``.

        Returns:
            How many bytes were freed, or ``None`` when there was nothing to discard.
        """
        if file_id is None:
            return None
        record = await self._session.scalar(
            select(UploadedFile).where(UploadedFile.id == file_id).limit(1)
        )
        if record is None:
            return None

        path = self._storage_path(record.storage_key)
        freed = 0
        try:
            if path.is_file():
                freed = path.stat().st_size
                path.unlink()
        except OSError as exc:
            logger.warning(
                "pipeline.artifact_unlink_failed",
                file_id=str(record.id),
                path=str(path),
                error=str(exc),
            )
        if record.deleted_at is None:
            record.soft_delete()
        return freed

    def _remove_render_dir(self, application_id: uuid.UUID) -> int:
        """Delete the temp directory an application's renders were written to.

        Args:
            application_id: The application whose renders should go.

        Returns:
            How many files were removed. ``0`` when the directory was already gone, which is
            the normal case for a second cleanup pass.
        """
        directory = self._settings.data_path / RENDER_DIR_NAME / str(application_id)
        if not directory.is_dir():
            return 0
        count = sum(1 for entry in directory.rglob("*") if entry.is_file())
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            logger.warning(
                "pipeline.render_dir_cleanup_failed",
                application_id=str(application_id),
                path=str(directory),
                error=str(exc),
            )
            return 0
        return count

    # ----------------------------------------------------------------------------------
    # Failure handling
    # ----------------------------------------------------------------------------------

    async def _fail(
        self,
        application: Application,
        exc: BaseException,
        *,
        stage: str,
    ) -> None:
        """Record an unexpected exception on the application and mark it ``failed``.

        Never raises. A failure handler that can itself fail turns one broken application
        into a broken batch, and the state machine legitimately refuses some moves — a
        post-submit application, for instance, must not be walked back to ``failed`` just
        because a cleanup step threw.

        Args:
            application: The row that failed.
            exc: What went wrong.
            stage: Which stage it happened in, recorded on the event.
        """
        try:
            await self._applications.record_error(application, exc)
            if application.status.is_post_submit():
                logger.warning(
                    "pipeline.failure_after_submit",
                    application_id=str(application.id),
                    status=application.status.value,
                    stage=stage,
                )
                return
            await self._applications.transition(
                application,
                ApplicationStatus.FAILED,
                message=f"{stage} failed: {exc}",
                payload={"stage": stage, "error_type": type(exc).__name__},
            )
        except InvalidTransition:
            logger.warning(
                "pipeline.failure_transition_refused",
                application_id=str(application.id),
                status=application.status.value,
                stage=stage,
            )
        except Exception as inner:
            logger.error(
                "pipeline.failure_handler_failed",
                application_id=str(application.id),
                stage=stage,
                error=str(inner),
                error_type=type(inner).__name__,
            )

    async def _failed_result(
        self,
        application: Application,
        exc: BaseException,
        started: float,
        *,
        score: int | None,
    ) -> PipelineResult:
        """Turn an exception raised during :meth:`submit` into a ``failed`` verdict.

        Args:
            application: The application being submitted.
            exc: What went wrong.
            started: Monotonic start of the submit call.
            score: The posting's normalised score.

        Returns:
            The failure verdict.
        """
        await self._fail(application, exc, stage=_STAGE_SUBMIT)
        logger.warning(
            "pipeline.submit_failed",
            application_id=str(application.id),
            error=str(exc),
            error_type=type(exc).__name__,
            duration_seconds=round(time.monotonic() - started, 3),
        )
        return self._result(
            VERDICT_FAILED,
            _STAGE_SUBMIT,
            application,
            started,
            score=score,
            message=f"Submission failed: {exc}",
            error=str(exc),
        )

    async def _abandon_in_flight(
        self,
        application: Application,
        started: float,
        *,
        score: int | None,
        provider: str,
    ) -> None:
        """Move a cancelled, mid-submission application out of ``SUBMITTING``.

        Called only from the :class:`asyncio.CancelledError` handler in :meth:`submit`, after
        the transition to ``SUBMITTING`` has committed. A Celery warm shutdown, a Ctrl-C or a
        task revocation can land anywhere between that commit and :meth:`_finalize`, and the
        two ends of that window are indistinguishable afterwards: the provider may never have
        run, or it may have submitted successfully and been interrupted while recording the
        result.

        Guard 3 in :meth:`submit` refuses anything that is not ``READY``, so a stranded row can
        never be silently re-submitted. But it would sit in the interface as permanently
        in-flight and never reach the review queue, which is the failure golden rule #8 exists
        to prevent — and in the worst case the user would not know they had applied at all.

        So the row is parked in review with
        :attr:`~app.models.enums.ReviewReason.VERIFICATION_FAILED`, which is precisely the
        situation: the outcome is unknown. A human confirms it from their inbox in seconds.

        Every step is best-effort and never raises. The caller is already unwinding a
        cancellation; failing to tidy up must not replace that with a different exception.

        Args:
            application: The row currently sitting in ``SUBMITTING``.
            started: Monotonic timestamp from the start of :meth:`submit`, for the duration.
            score: The posting's score, when it was computed.
            provider: Name of the ATS provider the attempt was routed to.
        """
        log = logger.bind(application_id=str(application.id), provider=provider)
        try:
            # Shielded: this is cleanup running *during* a cancellation, so without the shield
            # the first await would be cancelled too and the row would stay stranded.
            await asyncio.shield(
                self._applications.mark_needs_review(
                    application,
                    ReviewReason.VERIFICATION_FAILED,
                    payload={
                        "provider": provider,
                        "score": score,
                        "cause": "cancelled_mid_submission",
                        "duration_seconds": round(time.monotonic() - started, 3),
                        "note": (
                            "Submission was interrupted after it began. It is not known "
                            "whether the employer received this application — check your "
                            "email for a confirmation before resubmitting."
                        ),
                    },
                )
            )
            log.warning("pipeline.submit_cancelled_parked_for_review")
        except Exception:
            log.exception("pipeline.submit_cancelled_cleanup_failed")

    async def _escalate(
        self,
        application: Application,
        reason: ReviewReason,
        exc: BaseException,
        started: float,
        *,
        score: int | None,
        payload: dict[str, Any] | None = None,
    ) -> PipelineResult:
        """Park an application in review because a provider said a human is needed.

        Args:
            application: The application being submitted.
            reason: Why a human is needed.
            exc: The provider error that carried the reason.
            started: Monotonic start of the submit call.
            score: The posting's normalised score.
            payload: Extra context for the review screen.

        Returns:
            The ``needs_review`` verdict.
        """
        context = dict(payload or {})
        context["error"] = str(exc)
        context["error_type"] = type(exc).__name__
        await self._applications.mark_needs_review(application, reason, context)
        logger.info(
            "pipeline.submit_escalated",
            application_id=str(application.id),
            review_reason=reason.value,
            error=str(exc),
            duration_seconds=round(time.monotonic() - started, 3),
        )
        return self._result(
            VERDICT_NEEDS_REVIEW,
            _STAGE_SUBMIT,
            application,
            started,
            score=score,
            review_reason=reason,
            message=str(exc),
            error=str(exc),
            payload=context,
        )

    # ----------------------------------------------------------------------------------
    # Rendering
    # ----------------------------------------------------------------------------------

    async def _render_resume_file(
        self,
        document: ResumeDocument,
        directory: Path,
        template: str,
    ) -> RenderResult:
        """Render a resume, degrading to a dependency-free template if the engine is absent.

        The configured template is tried first, at the configured page budget. If it cannot
        run on this machine — no ``tectonic``, no LaTeX distribution, a broken template — the
        :data:`FALLBACK_TEMPLATE` is tried, which needs nothing but the standard library.
        Degrading is the right call: an application blocked because a PDF engine is missing is
        a worse outcome for the user than a plainer document, and the failure is logged loudly
        enough to be fixed.

        Args:
            document: The resume to render.
            directory: The application's render directory.
            template: The template the user asked for.

        Returns:
            The successful :class:`~app.documents.renderer.RenderResult`.

        Raises:
            DocumentRenderError: If no rung of the ladder could produce a file.
        """
        from app.documents.renderer import DocumentRenderError, render_resume

        last: DocumentRenderError | None = None
        for candidate, fmt in self._render_ladder(template):
            out = directory / f"resume.{fmt}"
            try:
                return await render_resume(
                    document,
                    out,
                    template=candidate,
                    fmt=fmt,
                    max_pages=int(self._settings.resume_max_pages),
                )
            except DocumentRenderError as exc:
                last = exc
                logger.warning(
                    "pipeline.render_degraded",
                    kind="resume",
                    template=candidate,
                    fmt=fmt,
                    error=str(exc),
                )
        raise _exhausted(last, "resume")

    async def _render_cover_letter_file(
        self,
        document: Any,
        directory: Path,
        template: str,
    ) -> RenderResult:
        """Render a cover letter, using the same degradation ladder as the resume.

        Args:
            document: The :class:`~app.documents.models.CoverLetterDocument` to render.
            directory: The application's render directory.
            template: The template to match the resume with.

        Returns:
            The successful :class:`~app.documents.renderer.RenderResult`.

        Raises:
            DocumentRenderError: If no rung of the ladder could produce a file.
        """
        from app.documents.renderer import DocumentRenderError, render_cover_letter

        last: DocumentRenderError | None = None
        for candidate, fmt in self._render_ladder(template):
            out = directory / f"cover-letter.{fmt}"
            try:
                return await render_cover_letter(document, None, out, template=candidate, fmt=fmt)
            except DocumentRenderError as exc:
                last = exc
                logger.warning(
                    "pipeline.render_degraded",
                    kind="cover_letter",
                    template=candidate,
                    fmt=fmt,
                    error=str(exc),
                )
        raise _exhausted(last, "cover letter")

    def _render_ladder(self, template: str) -> tuple[tuple[str, str], ...]:
        """Return the ``(template, format)`` pairs to try, best first.

        Args:
            template: The template the user asked for.

        Returns:
            The requested template at the configured PDF engine's format, then the
            dependency-free fallback. The fallback is omitted when it *is* the request.
        """
        requested = (template or self._settings.resume_template or FALLBACK_TEMPLATE).strip()
        if requested.lower() == FALLBACK_TEMPLATE:
            return ((FALLBACK_TEMPLATE, FALLBACK_RENDER_FORMAT),)
        return (
            (requested, "pdf"),
            (FALLBACK_TEMPLATE, FALLBACK_RENDER_FORMAT),
        )

    async def _materialize_documents(
        self,
        application: Application,
        posting: JobPosting,
    ) -> tuple[Path | None, Path | None]:
        """Return on-disk paths for the documents this submission will upload.

        Golden rule #6 made operational. The rendered file is disposable, so a retry — or a
        submission that follows a cleanup, a restart, or a move between machines — will often
        find it gone. Rather than failing, the document is re-rendered from
        ``ResumeVersion.content_json``, which is the thing that was never allowed to be
        deleted. The bytes may differ; the content cannot.

        Args:
            application: The application being submitted.
            posting: Its posting, used only for logging context.

        Returns:
            ``(resume_path, cover_letter_path)``. Either may be ``None`` when no such
            document was generated.
        """
        from app.documents.models import CoverLetterDocument, ResumeDocument

        directory = self._render_dir(application.id)
        resume_path: Path | None = None
        cover_path: Path | None = None

        version: ResumeVersion | None = None
        if application.resume_version_id is not None:
            version = await self._session.scalar(
                select(ResumeVersion)
                .where(ResumeVersion.id == application.resume_version_id)
                .limit(1)
            )

        if version is not None:
            existing = directory / f"resume.{version.render_format}"
            if existing.is_file():
                resume_path = existing
            else:
                document = ResumeDocument.model_validate(version.content_json or {})
                render = await self._render_resume_file(
                    document, directory, version.resume.template if version.resume else ""
                )
                resume_path = render.path
                logger.info(
                    "pipeline.resume_rerendered",
                    application_id=str(application.id),
                    posting_id=str(posting.id),
                    resume_version_id=str(version.id),
                    path=str(render.path),
                )

        letter: CoverLetter | None = None
        if application.cover_letter_id is not None:
            letter = await self._session.scalar(
                select(CoverLetter).where(CoverLetter.id == application.cover_letter_id).limit(1)
            )

        if letter is not None:
            candidates = list(directory.glob("cover-letter.*"))
            if candidates:
                cover_path = candidates[0]
            else:
                letter_document = CoverLetterDocument(body=letter.body)
                render = await self._render_cover_letter_file(
                    letter_document, directory, self._settings.resume_template
                )
                cover_path = render.path

        return resume_path, cover_path

    # ----------------------------------------------------------------------------------
    # Storage
    # ----------------------------------------------------------------------------------

    def _render_dir(self, application_id: uuid.UUID) -> Path:
        """Return (and create) the temp directory this application renders into.

        Args:
            application_id: The application.

        Returns:
            The directory, guaranteed to exist.
        """
        directory = self._settings.data_path / RENDER_DIR_NAME / str(application_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @staticmethod
    def _storage_key(
        user_id: uuid.UUID,
        application_id: uuid.UUID,
        filename: str,
        *,
        prefix: str = "",
    ) -> str:
        """Build the backend-relative key an artifact is stored under.

        Every artifact lives beneath the owning user, which is what keeps one user's
        documents and screenshots — full of their address, phone number and history — out of
        another user's scope on a shared install.

        Args:
            user_id: The owning user.
            application_id: The application the artifact belongs to.
            filename: The file's name.
            prefix: Optional sub-directory, e.g. ``"screenshots"``.

        Returns:
            A POSIX-style key, which is what both the local backend and S3 expect.
        """
        parts = ["users", str(user_id), "applications", str(application_id)]
        if prefix:
            parts.append(prefix)
        parts.append(filename)
        return "/".join(parts)

    def _storage_path(self, storage_key: str) -> Path:
        """Resolve a storage key to a path under the local blob root.

        ``app/storage/`` does not exist in this tree, so the local filesystem *is* the
        backend: keys are resolved beneath ``settings.storage_root``. When the
        :class:`StorageBackend` protocol lands, only this method and
        :meth:`_store_artifact` change.

        Args:
            storage_key: The backend-relative key.

        Returns:
            The absolute path. Built with :mod:`pathlib` from the key's segments so a
            Windows install gets real backslashes and a key can never escape the root.
        """
        segments = [part for part in storage_key.split("/") if part not in ("", ".", "..")]
        return self._settings.storage_root.joinpath(*segments)

    async def _store_artifact(
        self,
        source: Path,
        *,
        user_id: uuid.UUID,
        application_id: uuid.UUID,
        kind: DocumentKind,
        prefix: str = "",
    ) -> UploadedFile:
        """Copy a rendered file into storage and catalogue it.

        Args:
            source: The file on disk, typically in the temp render directory.
            user_id: The owning user.
            application_id: The application it belongs to.
            kind: What the file is, which drives retention.
            prefix: Optional sub-directory within the application's scope.

        Returns:
            The persisted, flushed :class:`~app.models.file.UploadedFile`.

        Raises:
            OSError: If the copy fails. A document that could not be stored is a real
                failure — the caller's stage wrapper turns it into a ``failed`` application
                rather than a silently document-less submission.
        """
        key = self._storage_key(user_id, application_id, source.name, prefix=prefix)
        destination = self._storage_path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

        record = UploadedFile(
            user_id=user_id,
            kind=kind,
            filename=source.name,
            content_type=_content_type(source),
            size_bytes=destination.stat().st_size,
            storage_key=key,
            sha256=_sha256(destination),
            backend="local",
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def _store_screenshots(
        self,
        application: Application,
        paths: Sequence[Path],
    ) -> list[UploadedFile]:
        """Catalogue the proof-of-submission captures a provider returned.

        Screenshots are evidence and are never removed by
        :meth:`cleanup_application` — "did this really get sent?" has to stay answerable. A
        capture that cannot be copied is logged and skipped rather than failing the
        submission: losing one screenshot is bad, losing a confirmed application is worse.

        Args:
            application: The application the captures belong to.
            paths: Files the provider wrote.

        Returns:
            The catalogued files, in the order they were captured.
        """
        stored: list[UploadedFile] = []
        for path in paths:
            candidate = Path(path)
            if not candidate.is_file():
                logger.warning(
                    "pipeline.screenshot_missing",
                    application_id=str(application.id),
                    path=str(candidate),
                )
                continue
            try:
                stored.append(
                    await self._store_artifact(
                        candidate,
                        user_id=application.user_id,
                        application_id=application.id,
                        kind=DocumentKind.SCREENSHOT,
                        prefix="screenshots",
                    )
                )
            except OSError as exc:
                logger.warning(
                    "pipeline.screenshot_store_failed",
                    application_id=str(application.id),
                    path=str(candidate),
                    error=str(exc),
                )
        return stored

    # ----------------------------------------------------------------------------------
    # Plumbing
    # ----------------------------------------------------------------------------------

    def _cache_client(self) -> Any:
        """Return the process-wide cache, resolved once per pipeline instance."""
        if self._cache is None:
            from app.cache import get_cache

            self._cache = get_cache()
        return self._cache

    def _retriever(self) -> Any:
        """Return a knowledge retriever bound to this pipeline's session.

        Used both for résumé tailoring and — via :attr:`ApplyContext.knowledge` — for
        answering free-text questions on the form itself. The second consumer is easy to
        forget, and forgetting it is silent: the answerer simply falls back to the profile
        block, so every free-text answer loses its knowledge grounding *and* any correction
        the user has already made, and the same question escalates to review a second time.

        Returns:
            A :class:`app.knowledge.retrieval.KnowledgeRetriever` over this session.
        """
        from app.knowledge.retrieval import KnowledgeRetriever

        return KnowledgeRetriever(self._session)

    def _llm_client(self) -> Any:
        """Return the reasoning-tier model client, resolved once per pipeline instance.

        With ``LLM_PROVIDER=null`` this is the deterministic offline model, which is what
        makes the whole pipeline runnable with zero API keys.
        """
        if self._llm is None:
            from app.ai.llm import get_llm

            self._llm = get_llm("reasoning")
        return self._llm

    @staticmethod
    def _supports_auto_apply(provider_name: str) -> bool:
        """Return whether a provider declares real automated submission.

        Read from the *class* rather than an instance, so nothing is constructed in order to
        discover that it must not be used. A provider that cannot be resolved at all is
        treated as unsupported: the honest response to "I do not know how to apply here" is
        to ask a human, never to improvise.

        Args:
            provider_name: The provider's registered name.

        Returns:
            Whether ``apply()`` really submits.
        """
        from app.jobs.registry import get_provider_class
        from app.plugins.base import PluginError

        try:
            return bool(get_provider_class(provider_name).supports_auto_apply)
        except (PluginError, LookupError, ValueError) as exc:
            logger.warning(
                "pipeline.provider_unresolvable",
                provider=provider_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return False

    @staticmethod
    def _provider(provider_name: str) -> ATSProvider:
        """Return the shared instance of one provider.

        Args:
            provider_name: The provider's registered name.

        Returns:
            The provider, resolved through :mod:`app.jobs.registry` — never imported
            directly (golden rule #5).

        Raises:
            PluginError: If the provider is missing, disabled, or fails to construct.
        """
        from app.jobs.registry import get_provider

        return get_provider(provider_name)

    async def _load_posting(self, posting_id: uuid.UUID | str) -> JobPosting:
        """Load one posting by id.

        Args:
            posting_id: The posting's identifier.

        Returns:
            The row, with its company eagerly loaded.

        Raises:
            LookupError: If the identifier is malformed or names no posting.
        """
        identifier = _as_uuid(posting_id, "posting id")
        posting = await self._session.scalar(
            select(JobPosting).where(JobPosting.id == identifier).limit(1)
        )
        if posting is None:
            raise LookupError(f"posting {identifier} not found")
        return posting

    async def _load_user(self, user_id: uuid.UUID | str) -> User:
        """Load one user by id.

        Args:
            user_id: The user's identifier.

        Returns:
            The row, with its profile eagerly loaded.

        Raises:
            LookupError: If the identifier is malformed or names no user.
        """
        identifier = _as_uuid(user_id, "user id")
        user = await self._session.scalar(select(User).where(User.id == identifier).limit(1))
        if user is None:
            raise LookupError(f"user {identifier} not found")
        return user

    async def _score_for(
        self,
        posting_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> JobScore | None:
        """Return the persisted score for one ``(posting, user)`` pair.

        Args:
            posting_id: The posting.
            user_id: The scoring user.

        Returns:
            The score, or ``None`` when the posting has never been scored for this user.
        """
        return await self._session.scalar(
            select(JobScore)
            .where(JobScore.posting_id == posting_id, JobScore.user_id == user_id)
            .limit(1)
        )

    async def _resume_container(self, user: User, template: str) -> Resume:
        """Return the :class:`~app.models.resume.Resume` variant new versions belong to.

        A ``Resume`` holds no content — it is the configuration a family of versions was
        generated under. Users who have never opened the resume screen have none, so one is
        created on first use rather than making every caller check.

        Args:
            user: The owning user.
            template: The template this generation used.

        Returns:
            The user's default variant, created if there was none.
        """
        container = await self._session.scalar(
            select(Resume)
            .where(Resume.user_id == user.id, Resume.is_default.is_(True))
            .order_by(Resume.created_at.asc())
            .limit(1)
        )
        if container is not None:
            return container

        container = await self._session.scalar(
            select(Resume)
            .where(Resume.user_id == user.id)
            .order_by(Resume.created_at.asc())
            .limit(1)
        )
        if container is not None:
            return container

        container = Resume(
            user_id=user.id,
            name=_DEFAULT_RESUME_NAME,
            template=template or self._settings.resume_template,
            is_default=True,
            config={},
        )
        self._session.add(container)
        await self._session.flush()
        logger.info(
            "pipeline.resume_variant_created",
            user_id=str(user.id),
            resume_id=str(container.id),
            template=container.template,
        )
        return container

    async def _next_version_number(self, resume_id: uuid.UUID) -> int:
        """Return the next version number for one resume variant.

        Args:
            resume_id: The variant.

        Returns:
            ``max(version_number) + 1``, or ``1`` for the first version. Soft-deleted
            versions still count: ``UNIQUE(resume_id, version_number)`` does not care that a
            row was retired, and reusing a number would collide.
        """
        highest = await self._session.scalar(
            select(func.max(ResumeVersion.version_number)).where(
                ResumeVersion.resume_id == resume_id
            )
        )
        return int(highest or 0) + 1

    def _result(
        self,
        verdict: str,
        stage: str,
        application: Application,
        started: float,
        *,
        score: int | None = None,
        review_reason: ReviewReason | None = None,
        submitted: bool = False,
        message: str = "",
        error: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> PipelineResult:
        """Build a :class:`PipelineResult` describing an application's current state.

        Args:
            verdict: What happened.
            stage: Which stage produced it.
            application: The application involved.
            started: Monotonic start of the call, used for the duration.
            score: The posting's normalised score, when consulted.
            review_reason: Why a human is needed, when one is.
            submitted: Whether an application genuinely reached an employer.
            message: One line safe to show a user.
            error: Failure detail.
            payload: Structured context.

        Returns:
            The populated result.
        """
        return PipelineResult(
            verdict=verdict,
            stage=stage,
            posting_id=application.posting_id,
            application_id=application.id,
            status=application.status,
            review_reason=review_reason,
            score=score,
            submitted=submitted,
            duration_seconds=time.monotonic() - started,
            message=message,
            error=error,
            payload=dict(payload or {}),
        )


# ======================================================================================
# Helpers
# ======================================================================================

#: How a non-``ready`` outcome of :meth:`Pipeline.prepare` is reported by
#: :meth:`Pipeline.run_one`.
_VERDICT_FOR_STATUS: Final[dict[ApplicationStatus, str]] = {
    ApplicationStatus.NEEDS_REVIEW: VERDICT_NEEDS_REVIEW,
    ApplicationStatus.FAILED: VERDICT_FAILED,
    ApplicationStatus.SUBMITTED: VERDICT_ALREADY_APPLIED,
    ApplicationStatus.CONFIRMED: VERDICT_ALREADY_APPLIED,
    ApplicationStatus.REJECTED: VERDICT_ALREADY_APPLIED,
    ApplicationStatus.INTERVIEW: VERDICT_ALREADY_APPLIED,
    ApplicationStatus.OFFER: VERDICT_ALREADY_APPLIED,
    ApplicationStatus.GHOSTED: VERDICT_ALREADY_APPLIED,
    ApplicationStatus.ABANDONED: VERDICT_SKIPPED,
}


def _as_uuid(value: uuid.UUID | str, label: str) -> uuid.UUID:
    """Coerce an identifier to a :class:`~uuid.UUID`.

    Args:
        value: The identifier, already a UUID or its string form.
        label: What it names, for the error message.

    Returns:
        The parsed UUID.

    Raises:
        LookupError: If the value is not a well-formed UUID. Malformed and missing are the
            same outcome for a caller, so they raise the same class.
    """
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise LookupError(f"{value!r} is not a valid {label}") from exc


def _exhausted(last: Exception | None, kind: str) -> Exception:
    """Return the error to raise when every rung of the render ladder failed.

    Args:
        last: The final :class:`~app.documents.renderer.DocumentRenderError`, when the ladder
            actually ran.
        kind: What was being rendered, for the message.

    Returns:
        The exception to raise — the last real failure when there was one, so the engine's
        own diagnostics survive; otherwise a :class:`RuntimeError`, which is only reachable
        if :meth:`Pipeline._render_ladder` ever returns nothing.
    """
    if last is not None:
        return last
    return RuntimeError(f"no render candidates were tried for the {kind}")


def _format_of(path: Path) -> str:
    """Return the render format a produced file represents.

    Args:
        path: The rendered file.

    Returns:
        The extension without its dot, lowercased; ``"pdf"`` when there is none.
    """
    suffix = path.suffix.lstrip(".").lower()
    return suffix or "pdf"


def _content_type(path: Path) -> str:
    """Return the MIME type for a rendered file.

    Args:
        path: The file.

    Returns:
        A known MIME type, or ``application/octet-stream``. Resolved from a fixed table
        rather than :mod:`mimetypes`, whose answers depend on the machine's registry.
    """
    return _CONTENT_TYPES.get(_format_of(path), "application/octet-stream")


def _sha256(path: Path) -> str:
    """Return the SHA-256 of a file, read in chunks.

    ``hashlib``, never :func:`hash` — the built-in is salted per process and would produce a
    different content address on every run (golden rule #9).

    Args:
        path: The file to hash.

    Returns:
        The lowercase hexadecimal digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _field_payload(field_: Any) -> dict[str, Any]:
    """Render one unanswered :class:`~app.jobs.base.FormField` for the review screen.

    Mirrors :class:`app.schemas.application.ReviewField` so the desktop app can render the
    payload without a translation step.

    Args:
        field_: The form field the automation refused to answer.

    Returns:
        A JSON-ready mapping. Reads attributes defensively, because a review item that
        cannot be serialised would strand the application it belongs to.
    """
    kind = getattr(field_, "kind", None)
    return {
        "selector": str(getattr(field_, "selector", "") or ""),
        "label": str(getattr(field_, "label", "") or ""),
        "kind": str(kind) if kind is not None else "unknown",
        "required": bool(getattr(field_, "required", False)),
        "options": [str(option) for option in (getattr(field_, "options", None) or [])],
        "max_length": getattr(field_, "max_length", None),
        "hint": getattr(field_, "hint", None),
    }
