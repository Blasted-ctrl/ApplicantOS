"""The human review queue — where "never guess" becomes a screen someone can act on (§13).

Golden rule #2 says the system stops rather than fabricates. That rule only pays for itself
if stopping is *cheap to recover from*: an application parked in ``needs_review`` with no
way back into the pipeline is not a safety feature, it is a lost application. This service
is the way back.

Three operations, and the shape of each is deliberate:

* :meth:`ReviewService.list_pending` returns ORM rows rather than schemas. The API layer
  serialises them into :class:`~app.schemas.application.ReviewItem`; the ``apply.retry``
  worker needs the row itself. A service that returned only schemas would force the worker
  to re-select everything it was just handed.
* :meth:`ReviewService.resolve` merges the human's answers, clears the reason and moves the
  application back to ``ready`` — and then **returns it so the caller can re-enqueue**. It
  deliberately does not submit. Resolving a review and submitting an application are
  separate decisions, and fusing them would mean a reviewer answering one dropdown at 2am
  triggers a browser session they never asked for.
* :meth:`ReviewService.dismiss` moves to ``abandoned``, which is the honest record of "I
  looked at this and decided not to apply" — as opposed to ``failed``, which means the
  machine broke.

Answers supplied by a human are also recorded as :class:`~app.models.knowledge.MemoryEntry`
corrections, so the same question answers itself next time. That write is best-effort: the
feedback loop is valuable, but it is never worth failing a resolution over.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.models.application import Application
from app.models.company import Company
from app.models.enums import ApplicationStatus, ReviewReason
from app.models.posting import JobPosting
from app.services.application_service import ApplicationService

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "DEFAULT_REVIEW_LIMIT",
    "MAX_REVIEW_LIMIT",
    "REVIEW_FIELDS_PAYLOAD_KEY",
    "ReviewService",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Vocabulary
# ======================================================================================

#: Page size when a caller supplies no ``limit``.
DEFAULT_REVIEW_LIMIT: Final[int] = 50

#: Ceiling on one page of review items.
MAX_REVIEW_LIMIT: Final[int] = 200

#: Key under which ``Application.review_payload`` carries the unanswered fields, as written
#: by :class:`app.services.pipeline.Pipeline`. Read here so a resolved answer can be paired
#: with the suggestion it replaced when the correction is recorded.
REVIEW_FIELDS_PAYLOAD_KEY: Final[str] = "unanswered_fields"

#: Filter keys :meth:`ReviewService.list_pending` understands. Anything else is ignored
#: rather than rejected: the desktop app sends its whole filter state, and a key this
#: version does not know about must not blank the queue.
_FILTER_KEYS: Final[frozenset[str]] = frozenset(
    {
        "q",
        "review_reason",
        "reason",
        "provider",
        "company",
        "session_id",
        "since",
        "until",
        "limit",
        "offset",
    }
)


class ReviewService:
    """Lists, resolves and dismisses applications parked for a human.

    Args:
        session: The unit of work. Status changes go through
            :class:`~app.services.application_service.ApplicationService`, which commits.
        applications: An explicit application service; one is built over *session* when
            omitted.

    Usage::

        service = ReviewService(session)
        pending = await service.list_pending(user.id, {"review_reason": "unknown_field"})
        application = await service.resolve(pending[0].id, {"#veteran": "decline"})
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        applications: ApplicationService | None = None,
    ) -> None:
        """Bind the service to a session and its application state machine."""
        self._session = session
        self._applications = (
            applications if applications is not None else ApplicationService(session)
        )

    # ----------------------------------------------------------------------------------
    # Reading
    # ----------------------------------------------------------------------------------

    async def list_pending(
        self,
        user_id: uuid.UUID | str,
        filters: Mapping[str, Any] | Any | None = None,
    ) -> list[Application]:
        """Return this user's open review items, oldest first.

        Oldest first because the queue is work: a review that has been waiting three days
        is more urgent than one raised a minute ago, and a posting can close while its
        application sits in a queue sorted newest-first.

        Args:
            user_id: The applicant.
            filters: Recognised keys — ``q`` (substring of the posting title or company
                name), ``review_reason`` (or ``reason``), ``provider``, ``company``,
                ``session_id``, ``since``, ``until``, ``limit``, ``offset``. Accepts a plain
                mapping or any object exposing those names, so
                :class:`~app.schemas.application.ApplicationFilter` can be passed straight
                through. Unknown keys are ignored.

        Returns:
            The matching applications, with ``posting`` and ``company`` eagerly loaded so a
            caller can serialise them without emitting SQL per row.

        Raises:
            LookupError: If *user_id* is malformed.
            ValueError: If a filter value cannot be interpreted — an unknown
                ``review_reason``, for example.
        """
        identifier = _as_uuid(user_id, "user id")
        options = _FilterSet.from_input(filters)

        statement = (
            select(Application)
            .where(
                Application.user_id == identifier,
                Application.status == ApplicationStatus.NEEDS_REVIEW,
            )
            .options(
                selectinload(Application.posting).selectinload(JobPosting.company),
                selectinload(Application.company),
            )
            .order_by(Application.updated_at.asc(), Application.created_at.asc())
        )
        statement = options.apply(statement)
        statement = statement.limit(options.limit).offset(options.offset)

        result = await self._session.execute(statement)
        items = list(result.scalars().unique().all())

        logger.debug(
            "review.listed",
            user_id=str(identifier),
            count=len(items),
            limit=options.limit,
            offset=options.offset,
        )
        return items

    async def count_pending(
        self,
        user_id: uuid.UUID | str,
        filters: Mapping[str, Any] | Any | None = None,
    ) -> int:
        """Return how many review items match, ignoring pagination.

        The ``total`` of the :class:`~app.schemas.common.Page` wrapping
        :meth:`list_pending`.

        Args:
            user_id: The applicant.
            filters: Same keys as :meth:`list_pending`; ``limit`` and ``offset`` are ignored.

        Returns:
            The count.

        Raises:
            LookupError: If *user_id* is malformed.
            ValueError: If a filter value cannot be interpreted.
        """
        identifier = _as_uuid(user_id, "user id")
        options = _FilterSet.from_input(filters)

        statement = (
            select(func.count())
            .select_from(Application)
            .where(
                Application.user_id == identifier,
                Application.status == ApplicationStatus.NEEDS_REVIEW,
            )
        )
        total = await self._session.scalar(options.apply(statement))
        return int(total or 0)

    # ----------------------------------------------------------------------------------
    # Writing
    # ----------------------------------------------------------------------------------

    async def resolve(
        self,
        application_id: uuid.UUID | str,
        answers: Mapping[str, Any],
    ) -> Application:
        """Apply a human's answers and send the application back to ``ready``.

        Answers are **merged** into ``Application.answers``, not substituted: a reviewer
        answering the one question that blocked the flow must not erase the forty the
        automation already resolved. The dictionary is reassigned wholesale afterwards
        because JSON columns are not change tracked.

        The application is left at ``ready``, not submitted. The caller re-enqueues —
        typically ``apply.submit`` — which keeps every real submission behind
        :meth:`app.services.pipeline.Pipeline.submit` and therefore behind the kill switch.

        Args:
            application_id: The application being resolved.
            answers: Field identifier (the ``selector`` of a
                :class:`~app.schemas.application.ReviewField`) to the value the reviewer
                supplied.

        Returns:
            The application, now ``ready``, committed, and safe to re-enqueue.

        Raises:
            LookupError: If no such application exists.
            ValueError: If the application is not awaiting review, or *answers* is empty —
                resolving nothing would clear the reason and re-queue an application that is
                still unanswerable, which would loop.
        """
        application = await self._applications.get(application_id)

        if application.status is not ApplicationStatus.NEEDS_REVIEW:
            raise ValueError(
                f"application {application.id} is {application.status.value!r}, "
                "not awaiting review"
            )

        supplied = {str(key): value for key, value in dict(answers or {}).items()}
        if not supplied:
            raise ValueError("resolving a review needs at least one answer")

        previous_reason = application.review_reason
        suggestions = _suggested_answers(application.review_payload)

        merged = dict(application.answers or {})
        merged.update(supplied)
        # Reassigned wholesale: JSON columns are not change tracked.
        application.answers = merged

        await self._remember(application, supplied, suggestions)

        resolved = await self._applications.transition(
            application,
            ApplicationStatus.READY,
            message=f"Review resolved by a human ({len(supplied)} answers).",
            payload={
                "resolved_fields": sorted(supplied),
                "previous_reason": previous_reason.value if previous_reason else None,
            },
        )
        logger.info(
            "review.resolved",
            application_id=str(resolved.id),
            user_id=str(resolved.user_id),
            answers=len(supplied),
            previous_reason=previous_reason.value if previous_reason else None,
        )
        return resolved

    async def dismiss(
        self,
        application_id: uuid.UUID | str,
        note: str | None = None,
    ) -> Application:
        """Abandon an application the user has decided not to pursue.

        ``abandoned`` rather than ``failed``: nothing broke, a person made a decision, and
        conflating the two would corrupt every reliability number the dashboard shows.

        Args:
            application_id: The application being dismissed.
            note: Why, in the user's own words. Appended to ``Application.notes`` and
                recorded on the event.

        Returns:
            The application, now ``abandoned``, committed.

        Raises:
            LookupError: If no such application exists.
            InvalidTransition: If the application has already been sent. An employer who
                holds an application cannot be made not to.
        """
        application = await self._applications.get(application_id)
        text = (note or "").strip()

        if text:
            existing = (application.notes or "").strip()
            application.notes = f"{existing}\n{text}".strip() if existing else text

        dismissed = await self._applications.transition(
            application,
            ApplicationStatus.ABANDONED,
            message=text or "Dismissed from the review queue.",
            payload={"note": text} if text else None,
        )
        logger.info(
            "review.dismissed",
            application_id=str(dismissed.id),
            user_id=str(dismissed.user_id),
            has_note=bool(text),
        )
        return dismissed

    # ----------------------------------------------------------------------------------
    # Feedback loop
    # ----------------------------------------------------------------------------------

    async def _remember(
        self,
        application: Application,
        answers: Mapping[str, Any],
        suggestions: Mapping[str, Any],
    ) -> None:
        """Record each human answer as a knowledge-graph correction.

        Best-effort by design. The correction loop is what stops the same question being
        asked forever, but an embedding backend that is down must not cost the user their
        resolution — they answered the question, and the answer is already merged.

        Args:
            application: The application being resolved.
            answers: What the human supplied.
            suggestions: What the automation had proposed, keyed the same way, so ``before``
                and ``after`` are both real rather than one being invented.
        """
        from app.knowledge.memory import MemoryStore

        store = MemoryStore(self._session)
        recorded = 0
        for field, value in answers.items():
            after = _as_text(value)
            if not after:
                continue
            before = _as_text(suggestions.get(field))
            try:
                await store.record_correction(
                    application.user_id,
                    before=before,
                    after=after,
                    context={
                        "application_id": str(application.id),
                        "field": field,
                        "source": "review_resolve",
                    },
                )
            except Exception as exc:  # noqa: BLE001 - the feedback loop never blocks a fix
                logger.warning(
                    "review.correction_failed",
                    application_id=str(application.id),
                    field=field,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                # One failure means the store is unavailable, not that this one field is
                # unusual; retrying the remaining fields would only repeat it.
                break
            recorded += 1

        if recorded:
            logger.debug(
                "review.corrections_recorded",
                application_id=str(application.id),
                count=recorded,
            )


# ======================================================================================
# Filters
# ======================================================================================


class _FilterSet:
    """Normalised, validated view of the filter mapping ``list_pending`` accepts.

    Parsing happens once, in one place, so the list query and the count query can never
    drift — a "12 pending" badge above a list showing eight rows is a bug users report as
    data loss.
    """

    __slots__ = (
        "company",
        "limit",
        "offset",
        "provider",
        "reason",
        "search",
        "session_id",
        "since",
        "until",
    )

    def __init__(self) -> None:
        """Start with the unfiltered defaults."""
        self.search: str | None = None
        self.reason: ReviewReason | None = None
        self.provider: str | None = None
        self.company: str | None = None
        self.session_id: uuid.UUID | None = None
        self.since: datetime | None = None
        self.until: datetime | None = None
        self.limit: int = DEFAULT_REVIEW_LIMIT
        self.offset: int = 0

    @classmethod
    def from_input(cls, filters: Mapping[str, Any] | Any | None) -> _FilterSet:
        """Build a filter set from a mapping or from any object exposing the same names.

        Args:
            filters: The caller's filters, or ``None``.

        Returns:
            The parsed set.

        Raises:
            ValueError: If a value is present but uninterpretable.
        """
        parsed = cls()
        if filters is None:
            return parsed

        raw = _as_mapping(filters)

        parsed.search = _as_optional_text(raw.get("q"))
        parsed.provider = _as_optional_text(raw.get("provider"))
        parsed.company = _as_optional_text(raw.get("company"))

        reason = raw.get("review_reason", raw.get("reason"))
        if reason is not None:
            try:
                parsed.reason = ReviewReason(reason)
            except ValueError as exc:
                raise ValueError(f"unknown review reason {reason!r}") from exc

        session_id = raw.get("session_id")
        if session_id is not None:
            parsed.session_id = _as_uuid(session_id, "session id")

        parsed.since = _as_optional_datetime(raw.get("since"), "since")
        parsed.until = _as_optional_datetime(raw.get("until"), "until")

        limit = raw.get("limit")
        if limit is not None:
            parsed.limit = max(1, min(_as_int(limit, "limit"), MAX_REVIEW_LIMIT))
        offset = raw.get("offset")
        if offset is not None:
            parsed.offset = max(0, _as_int(offset, "offset"))

        return parsed

    def apply(self, statement: Any) -> Any:
        """Add every active filter to *statement*.

        Args:
            statement: A ``select`` already scoped to one user and to ``needs_review``.

        Returns:
            The statement with the filters applied. Joins are added only when a filter needs
            them, so the common unfiltered queue stays a single-table query.
        """
        if self.reason is not None:
            statement = statement.where(Application.review_reason == self.reason)
        if self.session_id is not None:
            statement = statement.where(Application.session_id == self.session_id)
        if self.since is not None:
            statement = statement.where(Application.created_at >= self.since)
        if self.until is not None:
            statement = statement.where(Application.created_at <= self.until)

        if self.provider is not None or self.search is not None:
            statement = statement.join(
                JobPosting, JobPosting.id == Application.posting_id
            )
        if self.provider is not None:
            statement = statement.where(JobPosting.provider == self.provider)

        if self.company is not None or self.search is not None:
            statement = statement.join(Company, Company.id == Application.company_id)
        if self.company is not None:
            statement = statement.where(
                Company.normalized_name == Company.normalize(self.company)
            )
        if self.search is not None:
            pattern = f"%{self.search.lower()}%"
            statement = statement.where(
                func.lower(JobPosting.title).like(pattern)
                | func.lower(Company.name).like(pattern)
            )
        return statement


# ======================================================================================
# Helpers
# ======================================================================================


def _as_mapping(filters: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Return *filters* as a plain dictionary of the keys this service understands.

    Args:
        filters: A mapping, a pydantic model, or any object with the filter attributes.

    Returns:
        Only the recognised keys, with ``None`` values omitted so "absent" and "explicitly
        null" behave identically.
    """
    if isinstance(filters, Mapping):
        source: dict[str, Any] = {str(key): value for key, value in filters.items()}
    else:
        dumper = getattr(filters, "model_dump", None)
        if callable(dumper):
            dumped = dumper()
            source = dict(dumped) if isinstance(dumped, Mapping) else {}
        else:
            source = {
                key: getattr(filters, key)
                for key in _FILTER_KEYS
                if hasattr(filters, key)
            }
    return {
        key: value
        for key, value in source.items()
        if key in _FILTER_KEYS and value is not None
    }


def _suggested_answers(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Extract the automation's rejected suggestions from a review payload.

    Args:
        payload: ``Application.review_payload`` as written by the pipeline.

    Returns:
        Field selector to suggested answer, empty when the payload carried none. Tolerant of
        every shape a payload could legitimately have, because a malformed payload must not
        stop a human from resolving the review it belongs to.
    """
    if not isinstance(payload, Mapping):
        return {}
    fields = payload.get(REVIEW_FIELDS_PAYLOAD_KEY)
    if not isinstance(fields, Sequence) or isinstance(fields, (str, bytes)):
        return {}

    suggestions: dict[str, Any] = {}
    for entry in fields:
        if not isinstance(entry, Mapping):
            continue
        selector = entry.get("selector") or entry.get("label")
        if not selector:
            continue
        suggestions[str(selector)] = entry.get("suggested_answer")
    return suggestions


def _as_text(value: Any) -> str:
    """Render an answer value as the text a memory entry stores.

    Args:
        value: Whatever the reviewer supplied — a string, a bool, a number, a list.

    Returns:
        A trimmed string; ``""`` for ``None``.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple, set)):
        return ", ".join(sorted(str(item).strip() for item in value if str(item).strip()))
    return str(value).strip()


def _as_optional_text(value: Any) -> str | None:
    """Return a trimmed string, or ``None`` for blank input.

    Args:
        value: The raw filter value.

    Returns:
        The trimmed text, or ``None`` when it is empty — a cleared search box matches all.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any, label: str) -> int:
    """Coerce a pagination value to an integer.

    Args:
        value: The raw value.
        label: Which parameter it is, for the error message.

    Returns:
        The integer.

    Raises:
        ValueError: If the value is not numeric.
    """
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer, got {value!r}") from exc


def _as_optional_datetime(value: Any, label: str) -> datetime | None:
    """Coerce a filter bound to an aware datetime.

    Args:
        value: A ``datetime`` or an ISO-8601 string.
        label: Which parameter it is, for the error message.

    Returns:
        The datetime, or ``None``.

    Raises:
        ValueError: If the value cannot be parsed.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 datetime, got {value!r}") from exc


def _as_uuid(value: uuid.UUID | str, label: str) -> uuid.UUID:
    """Coerce an identifier to a :class:`~uuid.UUID`.

    Args:
        value: The identifier.
        label: What it names, for the error message.

    Returns:
        The parsed UUID.

    Raises:
        LookupError: If the value is not a well-formed UUID.
    """
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise LookupError(f"{value!r} is not a valid {label}") from exc
