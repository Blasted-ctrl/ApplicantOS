"""Checkpoints at runtime — the service that makes golden rule #8 true (§13).

``app/models/checkpoint.py`` describes a resumable step. Until this module existed, nothing
outside ``app/models/`` imported it: the schema was there, the runtime was not, and "a crash
resumes, never restarts" was an intention rather than a mechanism. :class:`CheckpointService`
is that mechanism.

**The key is the whole design.** ``checkpoints.key`` is unique, so two workers racing to
start the same step collide on the index rather than doing the work twice. :meth:`~Checkpoint
Service.save` is therefore an *upsert on the key*, and its insert runs inside a SAVEPOINT:
the loser of the race takes an :class:`~sqlalchemy.exc.IntegrityError` that rolls back alone,
re-reads the winner's row, and continues. Without the savepoint the loser's whole transaction
would be poisoned and a batch run would lose every operation after the first collision — the
same reasoning that governs
:meth:`app.services.application_service.ApplicationService.create_or_get`.

**Use the context manager.** :meth:`CheckpointService.step` is the ergonomic surface and the
one callers should reach for::

    async with checkpoints.step(f"apply:{app_id}:tailor", f"apply:{app_id}", "tailor", state):
        document = await engine.tailor(request)

It saves on entry, completes on a clean exit, and **fails on an exception and on
:class:`asyncio.CancelledError` alike**, re-raising either. Cancellation is not an
afterthought: a worker shut down mid-flight is exactly the crash golden rule #8 exists for,
and a step left in ``running`` forever is indistinguishable from one that never started.

**This service commits.** Like :class:`~app.services.application_service.ApplicationService`
and unlike the rest of the package, every mutating method here commits before returning. A
checkpoint that was never committed because the process died is not a checkpoint; it is a
comment. The whole point of the row is that it outlives the process that wrote it.

**Steps are ordered tuples, not a graph.** :data:`APPLY_STEPS` and :data:`INDEX_STEPS` name
the steps of the two long operations in the system, in order, so a UI can render "5 of 7"
without asking the database what the total is. An application is a fixed linear sequence; a
data-driven step registry here would be a workflow engine, and this product does not need
one.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, InvalidRequestError

from app.database.types import utcnow
from app.models.checkpoint import INITIAL_ATTEMPT, Checkpoint
from app.models.enums import CheckpointStatus

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "APPLY_STEPS",
    "CHECKPOINT_ERROR_MAX_CHARS",
    "DEFAULT_CHECKPOINT_TTL_SECONDS",
    "INDEX_STEPS",
    "OWNER_APPLY",
    "OWNER_INDEX",
    "OWNER_SEPARATOR",
    "RESUMABLE_CHECKPOINT_STATES",
    "STEPS_BY_OWNER",
    "CheckpointService",
    "owner_key",
    "step_key",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# The step vocabulary
# ======================================================================================

#: The steps of one application, in the order the pipeline drives them. Tuples rather than
#: a graph on purpose: an application is a fixed linear sequence, and the moment this became
#: a data-driven registry it would be a workflow engine nobody asked for.
APPLY_STEPS: Final[tuple[str, ...]] = (
    "score",
    "retrieve",
    "tailor",
    "render",
    "fill",
    "verify",
    "submit",
)

#: The steps of one knowledge-indexing pass, in order. Mirrors the pipeline documented on
#: :class:`app.knowledge.indexer.KnowledgeIndexer`.
INDEX_STEPS: Final[tuple[str, ...]] = (
    "fingerprint",
    "analyze",
    "chunk",
    "embed",
    "upsert",
)

#: Owner-label prefix for an application run.
OWNER_APPLY: Final[str] = "apply"

#: Owner-label prefix for a knowledge-indexing run.
OWNER_INDEX: Final[str] = "index"

#: Separator between the parts of an owner label and of a checkpoint key
#: (``"apply:<application-id>:submit"``).
OWNER_SEPARATOR: Final[str] = ":"

#: Owner prefix to the ordered steps of that kind of operation. This is what lets a UI say
#: "step 5 of 7" without querying for a total that is already known statically.
STEPS_BY_OWNER: Final[dict[str, tuple[str, ...]]] = {
    OWNER_APPLY: APPLY_STEPS,
    OWNER_INDEX: INDEX_STEPS,
}

#: Statuses a recovery pass acts on, in a deterministic order. Frozenset iteration order
#: varies between processes, which would defeat SQLAlchemy's compiled-statement cache for
#: the ``IN`` clause in :meth:`CheckpointService.resume_all`.
RESUMABLE_CHECKPOINT_STATES: Final[tuple[CheckpointStatus, ...]] = (
    CheckpointStatus.PENDING,
    CheckpointStatus.RUNNING,
    CheckpointStatus.FAILED,
)

#: Default lifetime of saved step state. A week is long enough that a laptop closed over a
#: weekend still resumes, and short enough that abandoned state does not accumulate forever.
#: Pass ``ttl_seconds=0`` to :meth:`CheckpointService.save` for state that never expires.
DEFAULT_CHECKPOINT_TTL_SECONDS: Final[int] = 7 * 24 * 60 * 60

#: Longest error message stored on ``checkpoints.last_error``. The column is ``Text`` and
#: could hold more, but the full detail is already in the structured log and a 40kB
#: traceback in a resume payload helps nobody.
CHECKPOINT_ERROR_MAX_CHARS: Final[int] = 2000

#: Message recorded when a step is interrupted by cancellation rather than by a failure.
CANCELLED_ERROR_TEXT: Final[str] = "CancelledError: step was cancelled before it finished"


def owner_key(kind: str, identifier: uuid.UUID | str) -> str:
    """Build the owner label grouping every step of one operation.

    Args:
        kind: The operation kind, normally :data:`OWNER_APPLY` or :data:`OWNER_INDEX`.
        identifier: What the operation is about — an application id, a source id.

    Returns:
        ``"<kind>:<identifier>"``, the value :meth:`CheckpointService.resume_all` groups on.
    """
    return f"{kind}{OWNER_SEPARATOR}{identifier}"


def step_key(owner: str, step: str) -> str:
    """Build the unique checkpoint key for one step of one operation.

    Args:
        owner: The owner label, normally from :func:`owner_key`.
        step: The step name within that operation.

    Returns:
        ``"<owner>:<step>"``. Uniqueness of this string is what serialises racing workers,
        so it must be derived from the operation's identity and never from a counter.
    """
    return f"{owner}{OWNER_SEPARATOR}{step}"


class CheckpointService:
    """Saves, resumes, completes and reaps the steps of long operations.

    Args:
        session: The unit of work. Every mutating method **commits** — see the module
            docstring for why a checkpoint that is not durable is not a checkpoint.

    Usage::

        checkpoints = CheckpointService(session)
        owner = owner_key(OWNER_APPLY, application.id)
        async with checkpoints.step(step_key(owner, "render"), owner, "render", state):
            await render_resume(document, path)
        done, total = await checkpoints.progress(owner, step_key(owner, "render"))
    """

    #: The ordered step names, exposed on the class so a caller rendering progress does not
    #: have to import the module-level constant separately.
    STEPS_BY_OWNER: Final[dict[str, tuple[str, ...]]] = STEPS_BY_OWNER

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to one session."""
        self._session = session

    # ----------------------------------------------------------------------------------
    # Writing
    # ----------------------------------------------------------------------------------

    async def save(
        self,
        key: str,
        owner: str,
        step: str,
        state: Mapping[str, Any] | None = None,
        *,
        resumable: bool = True,
        session_id: uuid.UUID | None = None,
        ttl_seconds: int | None = DEFAULT_CHECKPOINT_TTL_SECONDS,
    ) -> Checkpoint:
        """Claim a step for execution, creating its checkpoint or re-driving the existing one.

        An upsert on :attr:`~app.models.checkpoint.Checkpoint.key`. Re-saving the same step
        increments :attr:`~app.models.checkpoint.Checkpoint.attempt`, which is what bounds
        the retry policy; re-saving a *different* step under the same key resets the counter,
        because attempts belong to a step and not to a string.

        The insert runs inside a SAVEPOINT. A concurrent worker that wins the race makes this
        one's insert raise :class:`~sqlalchemy.exc.IntegrityError`, which rolls the savepoint
        back on its own and leaves the outer transaction usable; the winner's row is then
        re-read and re-driven. Without the savepoint a single collision would poison the
        whole unit of work.

        Args:
            key: The unique idempotency key for this step. Build it with :func:`step_key`.
            owner: Group label tying this step to its operation. Build it with
                :func:`owner_key`.
            step: Name of the step within that operation.
            state: Everything the step needs to continue from where it stopped. Copied
                defensively and assigned wholesale, because JSON columns are not change
                tracked. ``None`` leaves an existing state untouched.
            resumable: Whether a recovery pass may re-drive this step. ``False`` for terminal
                outcomes such as ``NEEDS_REVIEW`` and policy blocks, which golden rule #2
                says must never be retried automatically.
            session_id: The run this step belongs to, when there is one.
            ttl_seconds: Lifetime of the saved state. ``None`` uses
                :data:`DEFAULT_CHECKPOINT_TTL_SECONDS`; ``0`` or a negative value means the
                state never expires and :meth:`purge_expired` will never reclaim it.

        Returns:
            The checkpoint, in ``running``, committed.

        Raises:
            ValueError: If *key*, *owner* or *step* is blank. A blank key would collide with
                every other blank key and quietly serialise unrelated operations.
        """
        cleaned_key = _require_text(key, "checkpoint key")
        cleaned_owner = _require_text(owner, "checkpoint owner")
        cleaned_step = _require_text(step, "checkpoint step")
        expires_at = _expiry(ttl_seconds)

        existing = await self.load(cleaned_key)
        if existing is None:
            checkpoint = Checkpoint(
                key=cleaned_key,
                owner=cleaned_owner,
                step=cleaned_step,
                session_id=session_id,
                state=dict(state or {}),
                resumable=bool(resumable),
                expires_at=expires_at,
                attempt=INITIAL_ATTEMPT,
                status=CheckpointStatus.PENDING,
            )
            checkpoint.mark_running()
            try:
                async with self._session.begin_nested():
                    self._session.add(checkpoint)
                    await self._session.flush()
            except IntegrityError:
                self._detach(checkpoint)
                logger.info(
                    "checkpoint.save_raced",
                    checkpoint_key=cleaned_key,
                    owner=cleaned_owner,
                    step=cleaned_step,
                )
                existing = await self.load(cleaned_key)
                if existing is None:
                    raise
            else:
                await self._session.commit()
                logger.info(
                    "checkpoint.saved",
                    checkpoint_key=cleaned_key,
                    owner=cleaned_owner,
                    step=cleaned_step,
                    attempt=checkpoint.attempt,
                    created=True,
                )
                return checkpoint

        return await self._redrive(
            existing,
            owner=cleaned_owner,
            step=cleaned_step,
            state=state,
            resumable=resumable,
            session_id=session_id,
            expires_at=expires_at,
        )

    async def _redrive(
        self,
        checkpoint: Checkpoint,
        *,
        owner: str,
        step: str,
        state: Mapping[str, Any] | None,
        resumable: bool,
        session_id: uuid.UUID | None,
        expires_at: datetime | None,
    ) -> Checkpoint:
        """Re-claim an existing checkpoint for another attempt.

        Args:
            checkpoint: The row found under the key.
            owner: Owner label to (re)assert.
            step: Step name being driven.
            state: Replacement state, or ``None`` to keep what is stored.
            resumable: Whether recovery may re-drive this step.
            session_id: Run to attribute the step to, or ``None`` to keep the stored one.
            expires_at: New expiry, or ``None`` for no expiry.

        Returns:
            The checkpoint, in ``running``, committed.
        """
        if checkpoint.step != step:
            # Attempts belong to a step. A key reused for a different step starts over
            # rather than inheriting a retry budget it never spent.
            checkpoint.step = step
            checkpoint.attempt = INITIAL_ATTEMPT
        checkpoint.owner = owner
        checkpoint.resumable = bool(resumable)
        checkpoint.expires_at = expires_at
        if session_id is not None:
            checkpoint.session_id = session_id
        if state is not None:
            checkpoint.state = dict(state)
        checkpoint.mark_running()

        await self._session.flush()
        await self._session.commit()
        logger.info(
            "checkpoint.saved",
            checkpoint_key=checkpoint.key,
            owner=owner,
            step=step,
            attempt=checkpoint.attempt,
            created=False,
        )
        return checkpoint

    async def complete(
        self,
        key: str,
        state: Mapping[str, Any] | None = None,
    ) -> Checkpoint:
        """Record that a step succeeded.

        Clears :attr:`~app.models.checkpoint.Checkpoint.last_error`, so a step that failed
        and then succeeded does not leave a stale message behind for the review screen.

        Args:
            key: The step's idempotency key.
            state: Final state to persist. ``None`` keeps whatever is stored — which is what
                a caller who reassigned ``checkpoint.state`` in place of passing it here
                wants, since attribute *assignment* is tracked while in-place mutation of a
                JSON column is not.

        Returns:
            The checkpoint, committed.

        Raises:
            LookupError: If no checkpoint carries that key.
        """
        checkpoint = await self._require(key)
        checkpoint.mark_succeeded(dict(state) if state is not None else None)
        await self._session.flush()
        await self._session.commit()
        logger.info(
            "checkpoint.completed",
            checkpoint_key=checkpoint.key,
            owner=checkpoint.owner,
            step=checkpoint.step,
            attempt=checkpoint.attempt,
        )
        return checkpoint

    async def fail(self, key: str, error: BaseException | str) -> Checkpoint:
        """Record that a step failed, keeping its state for the next attempt.

        The status becomes ``failed``, which
        :meth:`~app.models.enums.CheckpointStatus.is_resumable` includes: a failure is the
        normal reason a recovery pass exists. Use :meth:`compensate` for a deliberate
        rollback, which is a different fact.

        Args:
            key: The step's idempotency key.
            error: The exception, or a pre-formatted message.

        Returns:
            The checkpoint, committed.

        Raises:
            LookupError: If no checkpoint carries that key.
        """
        checkpoint = await self._require(key)
        checkpoint.mark_failed(_error_text(error))
        await self._session.flush()
        await self._session.commit()
        return checkpoint

    async def compensate(self, key: str) -> Checkpoint:
        """Record that a successful step was deliberately rolled back.

        ``compensated`` is not a failure and is not resumable: the step ran, its effect was
        undone on purpose, and re-driving it would redo work somebody chose to unwind. Keeping
        it distinct from ``failed`` is what lets a recovery pass tell "this broke" from "this
        was withdrawn".

        Args:
            key: The step's idempotency key.

        Returns:
            The checkpoint, committed.

        Raises:
            LookupError: If no checkpoint carries that key.
        """
        checkpoint = await self._require(key)
        checkpoint.mark_compensated()
        await self._session.flush()
        await self._session.commit()
        logger.info(
            "checkpoint.compensated",
            checkpoint_key=checkpoint.key,
            owner=checkpoint.owner,
            step=checkpoint.step,
        )
        return checkpoint

    # ----------------------------------------------------------------------------------
    # The ergonomic wrapper
    # ----------------------------------------------------------------------------------

    @asynccontextmanager
    async def step(
        self,
        key: str,
        owner: str,
        step: str,
        state: Mapping[str, Any] | None = None,
        *,
        resumable: bool = True,
        session_id: uuid.UUID | None = None,
        ttl_seconds: int | None = DEFAULT_CHECKPOINT_TTL_SECONDS,
    ) -> AsyncIterator[Checkpoint]:
        """Run one step inside its checkpoint. **This is what callers should use.**

        Saves on entry, :meth:`complete`\\ s on a clean exit, and :meth:`fail`\\ s on an
        exception *and* on :class:`asyncio.CancelledError`, re-raising either untouched.

        Cancellation is handled explicitly because
        :class:`~asyncio.CancelledError` derives from :class:`BaseException` and would
        otherwise slip past ``except Exception`` — leaving a step stuck in ``running``
        forever, which a recovery pass cannot distinguish from a step still in flight. A
        worker cancelled mid-flight is precisely the crash golden rule #8 exists for.

        The yielded row is live: reassign ``checkpoint.state = {...}`` inside the block to
        persist progress (assignment is tracked; in-place mutation of a JSON column is not).

        Args:
            key: The step's unique idempotency key. Build it with :func:`step_key`.
            owner: Group label for the operation. Build it with :func:`owner_key`.
            step: Name of the step within that operation.
            state: Initial state for the step.
            resumable: Whether recovery may re-drive this step.
            session_id: The run this step belongs to, when there is one.
            ttl_seconds: Lifetime of the saved state; see :meth:`save`.

        Yields:
            The checkpoint, already claimed and in ``running``.

        Raises:
            ValueError: If *key*, *owner* or *step* is blank.
            BaseException: Whatever the body raised, re-raised after being recorded.
        """
        checkpoint = await self.save(
            key,
            owner,
            step,
            state,
            resumable=resumable,
            session_id=session_id,
            ttl_seconds=ttl_seconds,
        )
        try:
            yield checkpoint
        except asyncio.CancelledError:
            await self._record_failure(checkpoint.key, CANCELLED_ERROR_TEXT)
            raise
        except Exception as exc:
            await self._record_failure(checkpoint.key, exc)
            raise
        await self.complete(checkpoint.key)

    async def _record_failure(self, key: str, error: BaseException | str) -> None:
        """Record a failure without letting the record itself mask the original error.

        Args:
            key: The step's idempotency key.
            error: The exception, or a pre-formatted message.
        """
        try:
            await self.fail(key, error)
        except LookupError:
            logger.warning("checkpoint.fail_target_missing", checkpoint_key=key)

    # ----------------------------------------------------------------------------------
    # Reading
    # ----------------------------------------------------------------------------------

    async def load(self, key: str) -> Checkpoint | None:
        """Return the checkpoint stored under *key*, or ``None``.

        Args:
            key: The step's idempotency key.

        Returns:
            The row, or ``None`` when nothing has been saved under that key. Returning
            ``None`` rather than raising is deliberate: "have I done this before?" is the
            question this method exists to answer, and "no" is a normal answer.
        """
        cleaned = (key or "").strip()
        if not cleaned:
            return None
        return await self._session.scalar(
            select(Checkpoint).where(Checkpoint.key == cleaned).limit(1)
        )

    async def resume_all(self, owner: str) -> list[Checkpoint]:
        """Return every step of *owner* that a recovery pass should re-drive.

        Served by the composite ``(owner, status)`` index the model declares for exactly this
        query. All three conditions of
        :meth:`~app.models.checkpoint.Checkpoint.can_resume` are pushed into SQL — the
        ``resumable`` flag, a resumable status, and an expiry that has not passed — so a
        crash-recovery sweep reads only rows it will actually act on.

        Args:
            owner: The operation's group label, from :func:`owner_key`.

        Returns:
            The outstanding steps, oldest first, so recovery re-drives them in the order they
            were first claimed.
        """
        cleaned = (owner or "").strip()
        if not cleaned:
            return []
        now = utcnow()
        statement = (
            select(Checkpoint)
            .where(
                Checkpoint.owner == cleaned,
                Checkpoint.status.in_(RESUMABLE_CHECKPOINT_STATES),
                Checkpoint.resumable.is_(True),
                (Checkpoint.expires_at.is_(None)) | (Checkpoint.expires_at > now),
            )
            .order_by(Checkpoint.created_at.asc(), Checkpoint.id.asc())
        )
        rows = list((await self._session.execute(statement)).scalars().all())
        logger.debug("checkpoint.resume_all", owner=cleaned, outstanding=len(rows))
        return rows

    async def progress(self, owner: str, key: str) -> tuple[int, int]:
        """Return ``(completed, total)`` for the operation *owner* names.

        The total comes from :data:`STEPS_BY_OWNER` rather than from the database, which is
        the point of declaring the sequences statically: a UI can render "5 of 7" for a run
        whose last two steps have not been written yet. The completed count is one aggregate
        query over the steps that sequence actually contains, so a stray checkpoint saved
        under an unlisted step name cannot inflate the bar past its total.

        Args:
            owner: The operation's group label, from :func:`owner_key`.
            key: Any key belonging to the operation — normally one of its step keys. Its
                leading segment selects the sequence; *owner* is used as the fallback, so
                ``progress(owner, owner)`` works too.

        Returns:
            ``(completed, total)``. ``(0, 0)`` when neither argument names a known sequence,
            which is the honest answer for an ad-hoc operation with no declared steps.
        """
        sequence = _steps_for(key) or _steps_for(owner)
        total = len(sequence)
        cleaned_owner = (owner or "").strip()
        if total == 0 or not cleaned_owner:
            return (0, total)

        completed = await self._session.scalar(
            select(func.count(func.distinct(Checkpoint.step))).where(
                Checkpoint.owner == cleaned_owner,
                Checkpoint.status == CheckpointStatus.SUCCEEDED,
                Checkpoint.step.in_(sequence),
            )
        )
        return (min(int(completed or 0), total), total)

    # ----------------------------------------------------------------------------------
    # Maintenance
    # ----------------------------------------------------------------------------------

    async def purge_expired(self) -> int:
        """Delete every checkpoint whose saved state has aged out.

        Expiry means the state is no longer trustworthy, so the row goes regardless of its
        status: resuming from a week-old browser context would produce a worse outcome than
        starting the operation again. Rows with no ``expires_at`` are kept forever, which is
        what :meth:`save` writes when it is passed a non-positive ``ttl_seconds``.

        Returns:
            How many rows were removed.
        """
        result = await self._session.execute(
            delete(Checkpoint)
            .where(
                Checkpoint.expires_at.is_not(None),
                Checkpoint.expires_at <= utcnow(),
            )
            .execution_options(synchronize_session=False)
        )
        await self._session.commit()
        removed = max(0, int(getattr(result, "rowcount", 0) or 0))
        if removed:
            logger.info("checkpoint.purged", removed=removed)
        return removed

    # ----------------------------------------------------------------------------------
    # Internals
    # ----------------------------------------------------------------------------------

    async def _require(self, key: str) -> Checkpoint:
        """Load a checkpoint by key or explain that it is not there.

        Args:
            key: The step's idempotency key.

        Returns:
            The row.

        Raises:
            LookupError: If no checkpoint carries that key. ``app.api.errors`` maps this to
                ``404``, which is the right answer for a key nobody ever saved.
        """
        checkpoint = await self.load(key)
        if checkpoint is None:
            raise LookupError(f"checkpoint {key!r} not found")
        return checkpoint

    def _detach(self, instance: object) -> None:
        """Remove a failed insert from the session so a later flush does not retry it.

        Args:
            instance: The object whose insert lost a race.
        """
        try:
            self._session.expunge(instance)
        except InvalidRequestError:
            logger.debug("checkpoint.already_detached")


# ======================================================================================
# Helpers
# ======================================================================================


def _steps_for(value: str | None) -> tuple[str, ...]:
    """Return the declared step sequence a key or owner label belongs to.

    Args:
        value: A key (``"apply:<id>:submit"``), an owner label (``"apply:<id>"``), or a bare
            kind (``"apply"``).

    Returns:
        The sequence from :data:`STEPS_BY_OWNER`, or an empty tuple when the value names no
        declared operation.
    """
    text = (value or "").strip()
    if not text:
        return ()
    kind = text.split(OWNER_SEPARATOR, 1)[0].strip().lower()
    return STEPS_BY_OWNER.get(kind, ())


def _require_text(value: str, label: str) -> str:
    """Return *value* trimmed, rejecting blanks.

    Args:
        value: The candidate.
        label: What the value names, used in the error message.

    Returns:
        The trimmed string.

    Raises:
        ValueError: If the value is empty or only whitespace.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} must not be blank")
    return text


def _expiry(ttl_seconds: int | None) -> datetime | None:
    """Translate a lifetime into an absolute expiry instant.

    Args:
        ttl_seconds: Seconds of validity. ``None`` means
            :data:`DEFAULT_CHECKPOINT_TTL_SECONDS`; zero or negative means never expire.

    Returns:
        The expiry instant, or ``None`` for state that never expires.
    """
    ttl = DEFAULT_CHECKPOINT_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    if ttl <= 0:
        return None
    return utcnow() + timedelta(seconds=ttl)


def _error_text(error: BaseException | str) -> str:
    """Render an exception as the message stored on ``last_error``.

    Args:
        error: The exception, or an already-formatted message.

    Returns:
        ``"TypeName: message"`` for an exception, the string itself otherwise, truncated to
        :data:`CHECKPOINT_ERROR_MAX_CHARS`.
    """
    if isinstance(error, BaseException):
        detail = str(error).strip() or error.__class__.__name__
        text = f"{type(error).__name__}: {detail}"
    else:
        text = str(error).strip()
    return text[:CHECKPOINT_ERROR_MAX_CHARS]
