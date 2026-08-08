"""Maintenance tasks — ``cleanup.*`` and ``session.watchdog`` (``docs/CONTRACTS.md`` §15).

Every task here deletes something, and deletion in this system touches a person's job
search: their tailored resumes, the evidence that they applied, the audit trail of what the
automation did on their behalf. So the retention policy is part of the contract, not a
detail of the implementation, and it has four rules.

**1. Dry run by default.** Every destructive task takes ``apply: bool = False`` and, unless
it is passed ``True``, changes nothing — it reports exactly what it *would* delete. Only the
beat schedule passes ``apply=True``. An operator who types the task name by hand gets a
report, not a deletion.

**2. A minimum-age floor.** Each task clamps its age argument to a floor
(:data:`MIN_TEMP_DOCUMENT_AGE_HOURS`, :data:`MIN_POSTING_AGE_DAYS`,
:data:`MIN_ARTIFACT_AGE_DAYS`). ``cleanup.prune_artifacts(older_than_days=0, apply=True)`` is
a plausible typo and would otherwise erase everything written today; clamped, the worst it
can do is act on data that was already a month past its retention window.

**3. Audit-bearing data is archived before it is deleted.** ``application_events`` are the
record of what the automation did, and a confirmation screenshot is the proof a submission
happened. Both are exported to a dated JSONL file under ``<data_dir>/archive/`` *before* the
rows go, and the export path is reported in the task's return value. A deletion nobody can
undo is not maintenance, it is data loss on a schedule.

**4. Proof of submission is kept forever.** Confirmation screenshots belonging to a
``CONFIRMED`` application are never pruned, at any age, by any argument. They are the user's
evidence that they applied to that job — the single most valuable artifact this system
produces — and no disk-space argument outweighs that. :data:`PROOF_RETAINED_STATUSES` is the
guard, and it is applied in SQL, not in a loop that could be skipped.

``cleanup.refresh_gauges`` and ``session.watchdog`` are the two non-destructive tasks here:
the first repopulates the Prometheus gauges §16 defines (a gauge is process-local, so a
restarted API server reports zero until something sets it), and the second closes run
sessions whose worker died — without it, the one-running-session rule in
:meth:`app.services.session_service.SessionService.start` becomes a permanent deadlock for
that user.
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import structlog
from sqlalchemy import delete, func, select

from app.api.events import EVENT_SESSION_FINISHED, bus
from app.api.tasks import (
    TASK_CLEANUP_EXPIRE_POSTINGS,
    TASK_CLEANUP_PRUNE_ARTIFACTS,
    TASK_CLEANUP_PRUNE_CACHE,
    TASK_CLEANUP_REFRESH_GAUGES,
    TASK_CLEANUP_TEMP_DOCUMENTS,
    TASK_SESSION_WATCHDOG,
)
from app.config.settings import get_settings
from app.database.session import session_scope
from app.models.enums import ApplicationStatus, DocumentKind
from app.workers import run_async, task_span
from app.workers.celery_app import celery_app
from app.workers.retry import retryable

__all__ = [
    "ARCHIVE_DIR_NAME",
    "DEFAULT_ARTIFACT_AGE_DAYS",
    "DEFAULT_POSTING_AGE_DAYS",
    "DEFAULT_TEMP_DOCUMENT_AGE_HOURS",
    "MIN_ARTIFACT_AGE_DAYS",
    "MIN_POSTING_AGE_DAYS",
    "MIN_TEMP_DOCUMENT_AGE_HOURS",
    "PROOF_RETAINED_STATUSES",
    "expire_postings",
    "prune_artifacts",
    "prune_cache",
    "refresh_gauges",
    "temp_documents",
    "watchdog",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Retention policy constants
# ======================================================================================

#: Hours a submitted application's rendered documents are kept before the hourly sweep
#: deletes them. The default is a day; the floor is an hour, so a mistyped ``0`` cannot
#: delete the resume of an application submitted ten seconds ago.
DEFAULT_TEMP_DOCUMENT_AGE_HOURS: Final[int] = 24
MIN_TEMP_DOCUMENT_AGE_HOURS: Final[int] = 1

#: Days after which an unclosed posting nobody applied to is marked ``expired``. Boards
#: routinely leave dead listings up, and a stale posting quietly wastes a scoring pass every
#: cycle.
DEFAULT_POSTING_AGE_DAYS: Final[int] = 45
MIN_POSTING_AGE_DAYS: Final[int] = 7

#: Days a retired artifact row or an application event is kept before pruning. A month is
#: the floor: recovering from "the automation did something odd last week" requires last
#: week's audit trail to still exist.
DEFAULT_ARTIFACT_AGE_DAYS: Final[int] = 90
MIN_ARTIFACT_AGE_DAYS: Final[int] = 30

#: Application statuses whose confirmation screenshot is retained **indefinitely**. This is
#: the user's proof they applied; it outlives every retention window, every age argument and
#: every ``apply=True``.
PROOF_RETAINED_STATUSES: Final[tuple[ApplicationStatus, ...]] = (ApplicationStatus.CONFIRMED,)

#: Directory under ``settings.data_path`` holding dated exports of deleted audit data.
ARCHIVE_DIR_NAME: Final[str] = "archive"

#: Directory under ``settings.data_path`` the pipeline renders documents into. Must match
#: :data:`app.services.pipeline.RENDER_DIR_NAME`.
RENDER_DIR_NAME: Final[str] = "renders"

#: Timestamp format for archive filenames — sortable, filesystem-safe on Windows.
ARCHIVE_STAMP_FORMAT: Final[str] = "%Y%m%dT%H%M%SZ"

#: Ceiling on rows touched by one destructive pass, so a first sweep over a neglected
#: install finishes inside its time limit and resumes on the next tick.
BATCH_LIMIT: Final[int] = 2_000


def _utcnow() -> datetime:
    """Return the current instant as timezone-aware UTC.

    Returns:
        The same clock every model default uses, so a cutoff computed here is comparable to
        a stored timestamp.
    """
    from app.database.types import utcnow

    return utcnow()


def _archive_path(stem: str) -> Path:
    """Return a dated export path under ``<data_dir>/archive/``, creating the directory.

    Args:
        stem: What is being archived, used as the filename prefix (``application_events``).

    Returns:
        An absolute path ending in ``.jsonl``. The timestamp makes successive runs distinct
        rather than overwriting each other.
    """
    directory = get_settings().data_path / ARCHIVE_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{stem}-{_utcnow().strftime(ARCHIVE_STAMP_FORMAT)}.jsonl"


def _write_archive(stem: str, rows: list[dict[str, Any]]) -> str | None:
    """Export *rows* to a dated JSONL file before their originals are deleted.

    JSONL rather than a single JSON array so the file is appendable, streamable, and
    readable with ``head`` — an archive nobody can open is not an archive.

    Args:
        stem: Filename prefix identifying what is being archived.
        rows: JSON-ready mappings, one per line.

    Returns:
        The path written, or ``None`` when there was nothing to archive.

    Raises:
        OSError: If the export cannot be written. Deliberately **not** caught: failing to
            archive must abort the deletion, otherwise the guarantee is worthless.
    """
    if not rows:
        return None
    path = _archive_path(stem)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str, ensure_ascii=False))
            handle.write("\n")
    logger.info("cleanup.archived", stem=stem, rows=len(rows), path=str(path))
    return str(path)


# ======================================================================================
# cleanup.temp_documents
# ======================================================================================


def _stale_render_dirs(cutoff: datetime, known: set[uuid.UUID]) -> list[Path]:
    """Return render directories left behind by a crash.

    :meth:`app.services.pipeline.Pipeline.cleanup_application` removes the directory for an
    application it knows about. A worker killed mid-render leaves one nobody will ever ask
    about again, because the directory is named after an application whose row may not even
    exist.

    Args:
        cutoff: Only directories last modified before this instant are considered.
        known: Application ids handled by the row-driven pass, which must not be listed
            twice.

    Returns:
        Directories safe to remove.
    """
    root = get_settings().data_path / RENDER_DIR_NAME
    if not root.is_dir():
        return []

    stale: list[Path] = []
    threshold = cutoff.timestamp()
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        try:
            identifier = uuid.UUID(entry.name)
        except ValueError:
            # Not one of ours. Never delete a directory we cannot account for.
            continue
        if identifier in known:
            continue
        try:
            if entry.stat().st_mtime >= threshold:
                continue
        except OSError as exc:
            logger.warning("cleanup.render_dir_stat_failed", path=str(entry), error=str(exc))
            continue
        stale.append(entry)
    return stale


@celery_app.task(name=TASK_CLEANUP_TEMP_DOCUMENTS, bind=False)
@retryable()
def temp_documents(
    older_than_hours: int = DEFAULT_TEMP_DOCUMENT_AGE_HOURS,
    apply: bool = False,
) -> dict[str, Any]:
    """Delete rendered documents for applications that have already been sent.

    Golden rule #6 on a schedule: a tailored resume is a person's full contact details and
    employment history sitting in a file, and keeping it after it has been delivered is a
    liability with no upside. The knowledge behind it is untouched —
    ``ResumeVersion.content_json`` is kept forever and the file can be re-rendered from it.

    Proof-of-submission screenshots are **not** touched here. They are evidence, not output.

    Args:
        older_than_hours: How long after submission to wait. Clamped to at least
            :data:`MIN_TEMP_DOCUMENT_AGE_HOURS`.
        apply: ``False`` (the default) reports what would be deleted and changes nothing.

    Returns:
        ``{"applied", "older_than_hours", "applications", "orphan_dirs", "application_ids"}``.
    """
    hours = max(MIN_TEMP_DOCUMENT_AGE_HOURS, int(older_than_hours))

    with task_span(TASK_CLEANUP_TEMP_DOCUMENTS, apply=apply) as log:

        async def _run() -> dict[str, Any]:
            """Find, and optionally clean, applications whose renders have outlived them."""
            from app.models.application import Application
            from app.models.cover_letter import CoverLetter
            from app.models.enums import APPLICATION_POST_SUBMIT_STATES
            from app.models.resume import ResumeVersion
            from app.services.pipeline import Pipeline

            post_submit = tuple(APPLICATION_POST_SUBMIT_STATES)

            settings = get_settings()
            cutoff = _utcnow() - timedelta(hours=hours)

            async with session_scope() as session:
                has_resume_file = (
                    select(ResumeVersion.id)
                    .where(
                        ResumeVersion.application_id == Application.id,
                        ResumeVersion.file_id.is_not(None),
                    )
                    .exists()
                )
                has_letter_file = (
                    select(CoverLetter.id)
                    .where(
                        CoverLetter.application_id == Application.id,
                        CoverLetter.file_id.is_not(None),
                    )
                    .exists()
                )
                candidates = list(
                    (
                        await session.execute(
                            select(Application.id)
                            .where(
                                Application.status.in_(post_submit),
                                Application.submitted_at.is_not(None),
                                Application.submitted_at <= cutoff,
                                has_resume_file | has_letter_file,
                            )
                            .order_by(Application.submitted_at.asc())
                            .limit(BATCH_LIMIT)
                        )
                    )
                    .scalars()
                    .all()
                )
                orphans = _stale_render_dirs(cutoff, set(candidates))

                if not apply:
                    return {
                        "applications": len(candidates),
                        "orphan_dirs": len(orphans),
                        "application_ids": [str(value) for value in candidates],
                    }

                pipeline = Pipeline(session, settings)
                cleaned = 0
                for identifier in candidates:
                    try:
                        await pipeline.cleanup_application(identifier)
                    except LookupError as exc:
                        logger.warning(
                            "cleanup.temp_documents_missing",
                            application_id=str(identifier),
                            error=str(exc),
                        )
                        continue
                    cleaned += 1

            removed_dirs = 0
            for directory in orphans:
                try:
                    shutil.rmtree(directory)
                except OSError as exc:
                    logger.warning(
                        "cleanup.render_dir_remove_failed",
                        path=str(directory),
                        error=str(exc),
                    )
                    continue
                removed_dirs += 1

            return {
                "applications": cleaned,
                "orphan_dirs": removed_dirs,
                "application_ids": [str(value) for value in candidates],
            }

        outcome = run_async(_run())
        outcome["applied"] = bool(apply)
        outcome["older_than_hours"] = hours
        log.info(
            "cleanup.temp_documents_finished",
            applied=apply,
            applications=outcome["applications"],
            orphan_dirs=outcome["orphan_dirs"],
        )
        return outcome


# ======================================================================================
# cleanup.expire_postings
# ======================================================================================


@celery_app.task(name=TASK_CLEANUP_EXPIRE_POSTINGS, bind=False)
@retryable()
def expire_postings(
    older_than_days: int = DEFAULT_POSTING_AGE_DAYS,
    apply: bool = False,
) -> dict[str, Any]:
    """Mark dead listings ``expired`` so they stop consuming scoring passes.

    Two independent reasons to expire a posting: the board published a closing date that has
    passed, or nothing has been heard about it for *older_than_days*. Postings that already
    have an application are never expired — the application's history refers to them, and
    the applications screen would show a job that claims to have closed before it was
    applied to.

    Nothing is deleted: the status moves to ``expired`` and the row stays, because it is
    still the target of scores, dedupe keys and analytics.

    Args:
        older_than_days: Silence tolerated before a posting is presumed dead. Clamped to at
            least :data:`MIN_POSTING_AGE_DAYS`.
        apply: ``False`` (the default) reports the count and changes nothing.

    Returns:
        ``{"applied", "older_than_days", "closed", "stale", "candidates", "expired"}``.
        ``closed`` and ``stale`` overlap — a posting can be both — so they explain the
        candidate set rather than partitioning it.
    """
    days = max(MIN_POSTING_AGE_DAYS, int(older_than_days))

    with task_span(TASK_CLEANUP_EXPIRE_POSTINGS, apply=apply) as log:

        async def _run() -> dict[str, Any]:
            """Count, and optionally apply, the expiry."""
            from sqlalchemy import or_, update

            from app.models.application import Application
            from app.models.enums import POSTING_TERMINAL_STATES, PostingStatus
            from app.models.posting import JobPosting

            now = _utcnow()
            cutoff = now - timedelta(days=days)

            closed = JobPosting.closes_at.is_not(None) & (JobPosting.closes_at <= now)
            stale = or_(
                JobPosting.posted_at.is_not(None) & (JobPosting.posted_at <= cutoff),
                JobPosting.posted_at.is_(None) & (JobPosting.created_at <= cutoff),
            )
            selectable = (
                JobPosting.status.not_in(tuple(POSTING_TERMINAL_STATES)),
                or_(closed, stale),
                ~select(Application.id).where(Application.posting_id == JobPosting.id).exists(),
            )

            async with session_scope() as session:
                counts = {
                    "closed": int(
                        await session.scalar(
                            select(func.count(JobPosting.id)).where(*selectable, closed)
                        )
                        or 0
                    ),
                    "stale": int(
                        await session.scalar(
                            select(func.count(JobPosting.id)).where(*selectable, stale)
                        )
                        or 0
                    ),
                }
                total = int(
                    await session.scalar(select(func.count(JobPosting.id)).where(*selectable)) or 0
                )
                if not apply:
                    return {**counts, "expired": 0, "candidates": total}

                result = await session.execute(
                    update(JobPosting)
                    .where(*selectable)
                    .values(status=PostingStatus.EXPIRED)
                    .execution_options(synchronize_session=False)
                )
                return {
                    **counts,
                    "expired": max(0, int(getattr(result, "rowcount", 0) or 0)),
                    "candidates": total,
                }

        outcome = run_async(_run())
        outcome["applied"] = bool(apply)
        outcome["older_than_days"] = days
        log.info(
            "cleanup.expire_postings_finished",
            applied=apply,
            candidates=outcome["candidates"],
            expired=outcome["expired"],
        )
        return outcome


# ======================================================================================
# cleanup.prune_artifacts
# ======================================================================================


@celery_app.task(name=TASK_CLEANUP_PRUNE_ARTIFACTS, bind=False)
@retryable()
def prune_artifacts(
    older_than_days: int = DEFAULT_ARTIFACT_AGE_DAYS,
    apply: bool = False,
) -> dict[str, Any]:
    """Prune retired file rows and aged application events, archiving the audit trail first.

    Three phases, in this order because the third depends on the first two having reported:

    1. **Retired file rows.** ``UploadedFile`` rows already soft-deleted (by
       ``Pipeline.cleanup_application``) whose bytes are long gone. Their metadata is
       archived, then the rows are removed.
    2. **Application events.** ``application_events`` older than the window, belonging to
       applications that have reached a terminal state. This is audit-bearing data, so it is
       exported to a dated JSONL file *before* a single row is deleted, and the export path
       is in the return value.
    3. **Nothing at all, for proof of submission.** Confirmation screenshots for
       :data:`PROOF_RETAINED_STATUSES` applications are excluded in SQL. Screenshots for
       applications that never reached ``confirmed`` are archived and then released.

    Args:
        older_than_days: Retention window. Clamped to at least :data:`MIN_ARTIFACT_AGE_DAYS`.
        apply: ``False`` (the default) reports what would go and changes nothing.

    Returns:
        Per-phase counts, plus ``archives`` mapping each phase to the export it wrote.
    """
    days = max(MIN_ARTIFACT_AGE_DAYS, int(older_than_days))

    with task_span(TASK_CLEANUP_PRUNE_ARTIFACTS, apply=apply) as log:

        async def _run() -> dict[str, Any]:
            """Archive then prune, or report what would be archived and pruned."""
            from app.models.application import Application, ApplicationEvent
            from app.models.enums import APPLICATION_TERMINAL_STATES
            from app.models.file import UploadedFile

            cutoff = _utcnow() - timedelta(days=days)
            archives: dict[str, str | None] = {}

            async with session_scope() as session:
                # -- phase 1: retired file rows ------------------------------------------
                retired = list(
                    (
                        await session.execute(
                            select(UploadedFile)
                            .where(
                                UploadedFile.deleted_at.is_not(None),
                                UploadedFile.deleted_at <= cutoff,
                            )
                            .order_by(UploadedFile.deleted_at.asc())
                            .limit(BATCH_LIMIT)
                        )
                    )
                    .scalars()
                    .all()
                )

                # -- phase 3: screenshots that are not proof of a confirmed application ---
                is_confirmed_proof = (
                    select(Application.id)
                    .where(
                        Application.confirmation_screenshot_id == UploadedFile.id,
                        Application.status.in_(PROOF_RETAINED_STATUSES),
                    )
                    .exists()
                )
                releasable = list(
                    (
                        await session.execute(
                            select(UploadedFile)
                            .where(
                                UploadedFile.deleted_at.is_(None),
                                UploadedFile.created_at <= cutoff,
                                UploadedFile.kind == DocumentKind.SCREENSHOT,
                                ~is_confirmed_proof,
                            )
                            .order_by(UploadedFile.created_at.asc())
                            .limit(BATCH_LIMIT)
                        )
                    )
                    .scalars()
                    .all()
                )

                # -- phase 2: application events on terminal applications ------------------
                event_rows = list(
                    (
                        await session.execute(
                            select(ApplicationEvent)
                            .join(
                                Application,
                                Application.id == ApplicationEvent.application_id,
                            )
                            .where(
                                ApplicationEvent.at <= cutoff,
                                Application.status.in_(tuple(APPLICATION_TERMINAL_STATES)),
                            )
                            .order_by(ApplicationEvent.at.asc())
                            .limit(BATCH_LIMIT)
                        )
                    )
                    .scalars()
                    .all()
                )

                report = {
                    "retired_files": len(retired),
                    "released_screenshots": len(releasable),
                    "application_events": len(event_rows),
                }
                if not apply:
                    return {**report, "archives": {}, "bytes_freed": 0}

                # Archive first. ``_write_archive`` raises on failure, and that failure must
                # abort the deletion — an unarchived audit row is not deletable.
                archives["uploaded_files"] = _write_archive(
                    "uploaded_files",
                    [
                        {
                            "id": str(row.id),
                            "user_id": str(row.user_id) if row.user_id else None,
                            "kind": str(row.kind),
                            "filename": row.filename,
                            "storage_key": row.storage_key,
                            "sha256": row.sha256,
                            "size_bytes": row.size_bytes,
                            "deleted_at": row.deleted_at,
                        }
                        for row in retired
                    ],
                )
                archives["application_events"] = _write_archive(
                    "application_events",
                    [
                        {
                            "id": str(row.id),
                            "application_id": str(row.application_id),
                            "kind": row.kind,
                            "message": row.message,
                            "payload": row.payload,
                            "at": row.at,
                        }
                        for row in event_rows
                    ],
                )
                archives["screenshots"] = _write_archive(
                    "screenshots",
                    [
                        {
                            "id": str(row.id),
                            "user_id": str(row.user_id) if row.user_id else None,
                            "filename": row.filename,
                            "storage_key": row.storage_key,
                            "sha256": row.sha256,
                            "size_bytes": row.size_bytes,
                            "created_at": row.created_at,
                        }
                        for row in releasable
                    ],
                )

                freed = 0
                for row in releasable:
                    freed += await _release_bytes(row)
                    row.soft_delete()

                if retired:
                    await session.execute(
                        delete(UploadedFile)
                        .where(UploadedFile.id.in_([row.id for row in retired]))
                        .execution_options(synchronize_session=False)
                    )
                if event_rows:
                    await session.execute(
                        delete(ApplicationEvent)
                        .where(ApplicationEvent.id.in_([row.id for row in event_rows]))
                        .execution_options(synchronize_session=False)
                    )
                return {**report, "archives": archives, "bytes_freed": freed}

        outcome = run_async(_run())
        outcome["applied"] = bool(apply)
        outcome["older_than_days"] = days
        log.info(
            "cleanup.prune_artifacts_finished",
            applied=apply,
            retired_files=outcome["retired_files"],
            released_screenshots=outcome["released_screenshots"],
            application_events=outcome["application_events"],
            bytes_freed=outcome["bytes_freed"],
        )
        return outcome


async def _release_bytes(record: Any) -> int:
    """Delete one catalogued file's bytes through the storage backend.

    Args:
        record: The :class:`~app.models.file.UploadedFile` row.

    Returns:
        The row's recorded size when the object was removed, ``0`` otherwise. Failures are
        logged rather than raised: a missing object is the desired end state, and an
        unreachable backend must not abort the whole sweep.
    """
    from app.storage import get_storage

    try:
        removed = await get_storage().delete(record.storage_key)
    except Exception as exc:
        logger.warning(
            "cleanup.artifact_delete_failed",
            file_id=str(record.id),
            storage_key=record.storage_key,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return 0
    return int(record.size_bytes or 0) if removed else 0


# ======================================================================================
# cleanup.prune_cache
# ======================================================================================


@celery_app.task(name=TASK_CLEANUP_PRUNE_CACHE, bind=False)
@retryable()
def prune_cache(apply: bool = False) -> dict[str, Any]:
    """Remove expired cache entries and expired checkpoints.

    Both stores expire lazily — a value is only found to be stale when somebody reads it —
    so a key written once and never read again occupies its row or its file forever. This
    sweep is what bounds them.

    No archive and no age floor: everything here is derived state with an explicit
    ``expires_at`` that has already passed. Nothing is audit-bearing, and re-deriving it
    costs a cache miss.

    Args:
        apply: ``False`` (the default) reports the counts and changes nothing.

    Returns:
        ``{"applied", "cache_entries", "disk_entries", "checkpoints"}``.
    """
    with task_span(TASK_CLEANUP_PRUNE_CACHE, apply=apply) as log:

        async def _run() -> dict[str, Any]:
            """Count, and optionally purge, everything that has already expired."""
            from app.models.cache_entry import CacheEntry
            from app.models.checkpoint import Checkpoint
            from app.services.checkpoint_service import CheckpointService

            now = _utcnow()
            expired = (CacheEntry.expires_at.is_not(None)) & (CacheEntry.expires_at <= now)
            stale_checkpoint = (Checkpoint.expires_at.is_not(None)) & (Checkpoint.expires_at <= now)

            async with session_scope() as session:
                rows = int(
                    await session.scalar(select(func.count(CacheEntry.id)).where(expired)) or 0
                )
                checkpoints = int(
                    await session.scalar(select(func.count(Checkpoint.id)).where(stale_checkpoint))
                    or 0
                )
                if not apply:
                    return {
                        "cache_entries": rows,
                        "checkpoints": checkpoints,
                        "disk_entries": 0,
                    }

                result = await session.execute(
                    delete(CacheEntry).where(expired).execution_options(synchronize_session=False)
                )
                removed_rows = max(0, int(getattr(result, "rowcount", 0) or 0))
                removed_checkpoints = await CheckpointService(session).purge_expired()

            disk_entries = 0
            from app.cache import get_cache

            cache = get_cache()
            purge = getattr(cache, "purge_expired", None)
            if callable(purge):
                disk_entries = int(await purge() or 0)

            return {
                "cache_entries": removed_rows,
                "checkpoints": removed_checkpoints,
                "disk_entries": disk_entries,
            }

        outcome = run_async(_run())
        outcome["applied"] = bool(apply)
        log.info(
            "cleanup.prune_cache_finished",
            applied=apply,
            cache_entries=outcome["cache_entries"],
            checkpoints=outcome["checkpoints"],
            disk_entries=outcome["disk_entries"],
        )
        return outcome


# ======================================================================================
# cleanup.refresh_gauges
# ======================================================================================


@celery_app.task(name=TASK_CLEANUP_REFRESH_GAUGES, bind=False)
@retryable()
def refresh_gauges() -> dict[str, Any]:
    """Repopulate the Prometheus gauges ``docs/CONTRACTS.md`` §16 defines.

    A gauge is process-local and starts at zero. ``applicantos_review_queue_size`` and
    ``applicantos_session_active`` describe database state, so after a restart they claim
    "no reviews pending, no runs active" until something sets them — which, without this
    task, is never. Non-destructive by nature, so it takes no ``apply`` argument.

    Returns:
        ``{"review_queue_size": int, "session_active": int}``.
    """
    with task_span(TASK_CLEANUP_REFRESH_GAUGES) as log:

        async def _run() -> dict[str, int]:
            """Count the two gauge quantities in one unit of work."""
            from app.models.application import Application
            from app.models.enums import ApplicationStatus, SessionStatus
            from app.models.session import RunSession

            async with session_scope() as session:
                reviews = int(
                    await session.scalar(
                        select(func.count(Application.id)).where(
                            Application.status == ApplicationStatus.NEEDS_REVIEW
                        )
                    )
                    or 0
                )
                active = int(
                    await session.scalar(
                        select(func.count(RunSession.id)).where(
                            RunSession.status == SessionStatus.RUNNING
                        )
                    )
                    or 0
                )
            return {"review_queue_size": reviews, "session_active": active}

        outcome = run_async(_run())

        from app.observability.metrics import set_review_queue_size, set_session_active

        set_review_queue_size(outcome["review_queue_size"])
        set_session_active(outcome["session_active"])

        log.info(
            "cleanup.gauges_refreshed",
            review_queue_size=outcome["review_queue_size"],
            session_active=outcome["session_active"],
        )
        return outcome


# ======================================================================================
# session.watchdog
# ======================================================================================


@celery_app.task(name=TASK_SESSION_WATCHDOG, bind=False)
@retryable()
def watchdog(stale_after_minutes: int | None = None) -> dict[str, Any]:
    """Close run sessions whose worker died, and publish the closures.

    A worker killed mid-run leaves its session ``running`` forever. That is not untidy, it is
    a deadlock: :meth:`~app.services.session_service.SessionService.start` folds a second run
    into the existing one, so every later run for that user is attached to a session nothing
    is driving. This sweep is what makes that rule safe, and beat runs it every five minutes.

    Staleness is measured on ``updated_at``, which every counter increment bumps, so a busy
    run is never reaped and a silent one always is.

    Args:
        stale_after_minutes: Silence tolerated before a run is declared dead. ``None`` uses
            :data:`app.services.session_service.DEFAULT_STALE_AFTER_MINUTES`; the service
            clamps whatever it is given.

    Returns:
        ``{"reaped": int, "session_ids": [...]}``.
    """
    with task_span(TASK_SESSION_WATCHDOG) as log:

        async def _run() -> dict[str, Any]:
            """Reap stale runs, then publish the ones this pass closed."""
            from app.models.enums import SessionStatus
            from app.models.session import RunSession
            from app.schemas.session import SessionRead
            from app.services.session_service import (
                DEFAULT_STALE_AFTER_MINUTES,
                SessionService,
            )

            window = (
                DEFAULT_STALE_AFTER_MINUTES
                if stale_after_minutes is None
                else int(stale_after_minutes)
            )
            started = _utcnow()

            async with session_scope() as session:
                reaped = await SessionService(session).watchdog(window)
                if not reaped:
                    return {"reaped": 0, "session_ids": []}

                # Only the runs this pass closed: ``finish`` preserves an existing
                # ``ended_at``, so a run reaped earlier keeps its older stamp and is not
                # re-published.
                rows = list(
                    (
                        await session.execute(
                            select(RunSession).where(
                                RunSession.status == SessionStatus.FAILED,
                                RunSession.ended_at.is_not(None),
                                RunSession.ended_at >= started,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                identifiers = [str(row.id) for row in rows]
                for row in rows:
                    bus.publish_model(EVENT_SESSION_FINISHED, SessionRead.model_validate(row))

            return {"reaped": reaped, "session_ids": identifiers}

        outcome = run_async(_run())
        if outcome["reaped"]:
            log.warning("session.watchdog_reaped", reaped=outcome["reaped"])
        return outcome
