"""The seam that puts what the user taught the system back in front of the model.

:meth:`app.knowledge.memory.MemoryStore.as_prompt_context` renders memories as a compact,
budgeted block, and :meth:`~app.knowledge.memory.MemoryStore.reinforce` adjusts a memory's
weight. Neither is safe to call directly from a prompt builder, for two separate reasons, and
this module is the one place both are handled:

**A memory body can carry personal data.** The only writer of a ``correction`` memory is
:meth:`app.services.review_service.ReviewService._remember`, and what it stores is *the
human's literal answer to a form field*. A reviewer who types a Social Security number into an
unknown field creates a :class:`~app.models.knowledge.MemoryEntry` whose body **is** that
number. :func:`app.ai.untrusted.contains_pii` reports that; this module acts on it by
**excluding the whole memory**, never by redacting it — a redacted lesson ("Preferred wording:
``***``") teaches nothing, and a mangled memory in a prompt is worse than an absent one. The
user's own contact details are allow-listed, so "my email is …" is not treated as a leak.

**A reward has to follow an outcome, not an injection.** :func:`reinforce_used` is deliberately
separate from :func:`build_memory_block`: the caller injects first, learns whether the work
escalated to a human second, and only then decides whether the memories that shaped it earned
weight. A memory that keeps preceding clean outcomes gets heavier; one that precedes an
escalation simply does not — there is no penalty, because an escalation has a hundred causes
and blaming the memory for all of them would silence the feedback loop this product runs on.

Both call sites inject into the **system** prompt and never into the material a validator
reads. On the résumé path that is load-bearing: golden rule #7 requires every bullet to trace
to a :class:`~app.models.knowledge.KnowledgeFact` id, so a memory may influence phrasing and
emphasis and may never introduce content.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import structlog

from app.ai.untrusted import contains_pii

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.knowledge.memory import MemoryStore
    from app.models.knowledge import MemoryEntry

__all__ = [
    "EMPTY_MEMORY_BLOCK",
    "MEMORY_PROMPT_K",
    "REINFORCE_CLEAN_DELTA",
    "MemoryBlock",
    "build_memory_block",
    "reinforce_used",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: How many memories are retrieved for one prompt. Matches
#: :data:`app.knowledge.memory.DEFAULT_MEMORY_K`, stated here because the value is part of
#: this module's own contract: the block is bounded by a token budget as well, so asking for
#: more would only mean more entries falling off the end after being screened for nothing.
MEMORY_PROMPT_K: Final[int] = 8

#: What the ``$memories`` placeholder renders as when there is nothing to say. A prompt with a
#: dangling placeholder or a bare empty heading reads to a model as an instruction that was
#: cut off; an explicit "nothing yet" reads as a fact about this user.
EMPTY_MEMORY_BLOCK: Final[str] = (
    "(Nothing yet — this user has not corrected you on anything relevant to this request.)"
)

#: Weight added to every memory that was injected into a piece of work that did **not** come
#: back to a human. Small on purpose: one clean outcome is weak evidence, and
#: :data:`app.knowledge.memory.MAX_MEMORY_WEIGHT` is reached only by a memory that keeps
#: preceding clean outcomes over many applications. Compare
#: :data:`app.knowledge.memory.REPEAT_REINFORCEMENT`, which is twice this, because a user
#: repeating a correction by hand is far stronger evidence than an absence of escalation.
REINFORCE_CLEAN_DELTA: Final[float] = 0.25

#: Reason recorded on the exclusion log line when the memory carried
#: :data:`app.knowledge.memory.CONTEXT_KEY_PII` from record time.
_EXCLUDED_BY_STAMP: Final[str] = "recorded_stamp"

#: Reason recorded when this module's own screen found personal data in the body.
_EXCLUDED_BY_SCREEN: Final[str] = "injection_screen"


# ======================================================================================
# The block
# ======================================================================================


@dataclass(frozen=True, slots=True)
class MemoryBlock:
    """One rendered memory block, plus the audit trail behind it.

    Attributes:
        text: The block as it goes into a **system** prompt, or ``""`` when nothing survived
            the screen and the token budget. Never ends in a newline.
        memory_ids: The memories that are actually *in* :attr:`text`, in the order they
            appear. Entries dropped by the token budget are not listed: rewarding a memory
            the model never saw would poison the signal :func:`reinforce_used` exists to
            collect.
        considered: How many memories retrieval offered, before screening.
        excluded: How many were withheld because they carry personal data.
    """

    text: str = ""
    memory_ids: tuple[uuid.UUID, ...] = ()
    considered: int = 0
    excluded: int = 0

    @property
    def empty(self) -> bool:
        """Whether this block carries nothing worth injecting."""
        return not self.text

    def for_prompt(self) -> str:
        """Return the value to substitute into a prompt's ``$memories`` placeholder.

        Returns:
            :attr:`text`, or :data:`EMPTY_MEMORY_BLOCK` when there is nothing to say.
        """
        return self.text or EMPTY_MEMORY_BLOCK


@dataclass(slots=True)
class _Screened:
    """Working state of one screening pass.

    Attributes:
        kept: The memories cleared for injection, ranking order preserved.
        excluded: How many were withheld.
    """

    kept: list[MemoryEntry] = field(default_factory=list)
    excluded: int = 0


async def build_memory_block(
    store: MemoryStore,
    user_id: uuid.UUID,
    query: str,
    *,
    allow: Iterable[str] = (),
    purpose: str,
    k: int = MEMORY_PROMPT_K,
) -> MemoryBlock:
    """Retrieve, screen and render the memories relevant to *query*.

    Three steps, in this order, and the order matters: retrieval ranks by relevance, the PII
    screen removes entries that must not reach a model at all, and only what is left is given
    to :meth:`app.knowledge.memory.MemoryStore.as_prompt_context` to fit into its 600-token
    budget. Screening *before* rendering is what stops an excluded memory from consuming
    budget that a usable one could have had.

    **Nothing here can fail a generation.** A retrieval error, an embedding backend that is
    down, a store that is not really a store — every one of them yields an empty block and a
    warning. The user gets an answer written without their corrections, which is exactly what
    they got before this module existed.

    Args:
        store: The memory store to read through.
        user_id: The applicant whose memories these are. Enforced in SQL by the store, so a
            stale vector record belonging to someone else cannot leak in.
        query: What the model is about to write — a field label, a job posting.
        allow: Allow-list keys from :func:`app.ai.untrusted.contact_allowlist`, normally the
            profile's own email address and phone number. Without these, "my email is …"
            reads as a leak and nearly every useful memory is excluded.
        purpose: Where this block is going (``"field_answer"``, ``"resume_tailor"``), for the
            log lines. Not sent to the model.
        k: How many memories to retrieve before screening.

    Returns:
        The block. Empty when the user has no memories, when every candidate carried personal
        data, or when anything went wrong.
    """
    if not (query or "").strip():
        return MemoryBlock()

    try:
        memories = await store.relevant(user_id, query, k=max(1, int(k)))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "memory.retrieval_failed",
            purpose=purpose,
            user_id=str(user_id),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return MemoryBlock()

    if not memories:
        return MemoryBlock()

    screened = _screen(memories, allow=allow, purpose=purpose, user_id=user_id)
    if not screened.kept:
        return MemoryBlock(considered=len(memories), excluded=screened.excluded)

    text = store.as_prompt_context(screened.kept)
    rendered = _rendered_count(text)
    block = MemoryBlock(
        text=text,
        memory_ids=tuple(entry.id for entry in screened.kept[:rendered]),
        considered=len(memories),
        excluded=screened.excluded,
    )
    logger.debug(
        "memory.injected",
        purpose=purpose,
        user_id=str(user_id),
        considered=block.considered,
        excluded=block.excluded,
        injected=len(block.memory_ids),
    )
    return block


def _screen(
    memories: Sequence[MemoryEntry],
    *,
    allow: Iterable[str],
    purpose: str,
    user_id: uuid.UUID,
) -> _Screened:
    """Drop every memory whose body carries personal data.

    Two independent tests, because they answer two different questions and neither subsumes
    the other:

    * the :data:`~app.knowledge.memory.CONTEXT_KEY_PII` stamp
      :meth:`app.knowledge.memory.MemoryStore._record` writes at record time — the stamp
      exists precisely so a reader can skip the entry without re-running the screen;
    * a live call to :func:`app.ai.untrusted.contains_pii`, which catches memories written
      before the stamp existed and any entry whose ``context`` was rebuilt on a repeat.

    Either one is enough to exclude. That is deliberately the cautious direction: the cost of
    a false positive is one memory the model does not see, and the cost of a false negative is
    a Social Security number in a prompt.

    Args:
        memories: The retrieved memories, most relevant first.
        allow: Allow-list keys for the user's own contact details.
        purpose: Where the block is going, for the log line.
        user_id: The owning user, for the log line.

    Returns:
        The memories cleared for injection and the count withheld. **The excluded values are
        never logged** — only the categories that recognised them.
    """
    from app.knowledge.memory import CONTEXT_KEY_PII

    allowed = tuple(allow)
    result = _Screened()
    for entry in memories:
        context = entry.context if isinstance(entry.context, dict) else {}
        stamp = context.get(CONTEXT_KEY_PII)
        stamped = _stamp_categories(stamp)
        if stamped:
            result.excluded += 1
            _log_exclusion(entry, stamped, _EXCLUDED_BY_STAMP, purpose, user_id)
            continue

        verdict = contains_pii(entry.text or "", allow=allowed)
        if verdict.found:
            result.excluded += 1
            _log_exclusion(
                entry,
                [category.value for category in verdict.categories],
                _EXCLUDED_BY_SCREEN,
                purpose,
                user_id,
            )
            continue

        result.kept.append(entry)
    return result


def _stamp_categories(stamp: object) -> list[str]:
    """Return the PII categories a recorded stamp names.

    Args:
        stamp: Whatever sits under :data:`app.knowledge.memory.CONTEXT_KEY_PII`. A JSON column
            round-trips through arbitrary shapes, so nothing about it is assumed.

    Returns:
        The category names, or ``[]`` when the stamp is absent, empty or unrecognisable. An
        unrecognisable stamp is treated as "no stamp" rather than as an exclusion, because the
        live screen in :func:`_screen` still runs and is the authority.
    """
    if not isinstance(stamp, dict):
        return []
    categories = stamp.get("categories")
    if not isinstance(categories, (list, tuple)):
        return []
    return [str(item) for item in categories if str(item).strip()]


def _log_exclusion(
    entry: MemoryEntry,
    categories: Sequence[str],
    reason: str,
    purpose: str,
    user_id: uuid.UUID,
) -> None:
    """Record that a memory was withheld, naming the category and never the value.

    Args:
        entry: The excluded memory.
        categories: The :class:`~app.ai.untrusted.PiiCategory` values that fired.
        reason: :data:`_EXCLUDED_BY_STAMP` or :data:`_EXCLUDED_BY_SCREEN`.
        purpose: Where the block was going.
        user_id: The owning user.
    """
    logger.warning(
        "memory.excluded_pii",
        purpose=purpose,
        user_id=str(user_id),
        memory_id=str(entry.id),
        kind=str(entry.kind),
        categories=list(categories),
        reason=reason,
    )


def _rendered_count(block: str) -> int:
    """Return how many memories a rendered block actually contains.

    :meth:`app.knowledge.memory.MemoryStore.as_prompt_context` emits one header line followed
    by one line per memory, in the order it was given, and stops at the first entry that would
    breach its token budget. Counting lines therefore recovers the surviving prefix exactly,
    without this module having to duplicate the bullet format — which it must not, because a
    duplicate that drifted would credit the wrong memories.

    Args:
        block: The rendered block, possibly ``""``.

    Returns:
        The number of memory lines, never negative.
    """
    if not block:
        return 0
    return max(0, len(block.splitlines()) - 1)


# ======================================================================================
# The reinforcement loop
# ======================================================================================


async def reinforce_used(
    store: MemoryStore,
    memory_ids: Iterable[uuid.UUID | str],
    *,
    purpose: str,
    delta: float = REINFORCE_CLEAN_DELTA,
) -> int:
    """Credit the memories that shaped a piece of work which did not need a human.

    Call this **only** once the outcome is known, and only on the clean branch. That is what
    makes it a supervised signal rather than a popularity counter: the alternative — crediting
    at injection time — would raise the weight of every memory that ever got retrieved,
    including the ones whose advice sent the application to the review queue.

    Best-effort. A weight that failed to move is a slightly worse ranking next week; it is
    never a reason to fail an application that has already succeeded.

    Args:
        store: The memory store to write through. It flushes, it does not commit, so this
            joins whatever unit of work the caller is already in.
        memory_ids: The ids from :attr:`MemoryBlock.memory_ids`. Strings are accepted so a
            caller that round-tripped them through JSON does not have to parse them back.
        purpose: Where the memories were injected, for the log line.
        delta: Weight to add to each, :data:`REINFORCE_CLEAN_DELTA` by default.

    Returns:
        How many memories were actually adjusted.
    """
    identifiers = [parsed for parsed in (_as_uuid(value) for value in memory_ids) if parsed]
    if not identifiers:
        return 0

    adjusted = 0
    for identifier in identifiers:
        try:
            entry = await store.reinforce(identifier, delta)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "memory.reinforce_failed",
                purpose=purpose,
                memory_id=str(identifier),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            # One failure means the store is unavailable, not that this one memory is
            # unusual; the remaining ids would only repeat it.
            break
        if entry is not None:
            adjusted += 1

    if adjusted:
        logger.info(
            "memory.reinforced_clean_outcome",
            purpose=purpose,
            memories=adjusted,
            delta=float(delta),
        )
    return adjusted


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID | None:
    """Parse a memory id, tolerating anything that is not one.

    Args:
        value: A :class:`~uuid.UUID` or its string form.

    Returns:
        The identifier, or ``None`` — a malformed id must not stop the rest of the batch from
        being credited.
    """
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None
