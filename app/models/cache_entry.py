"""Durable cache entries — the database-backed tier of :mod:`app.cache`.

Most caching in ApplicantOS is in-memory or on disk, and both die with the process or the
container. Some cached work must not: an embedding costs an API call, a GitHub analysis
costs a rate-limit budget, a tailored resume costs reasoning tokens. ``cache_entries`` is
where those survive a restart, a machine move, and a backup/restore cycle.

The shape follows golden rule #9 — *cache aggressively, invalidate precisely*:

``key`` (unique)
    A content-addressed key produced by :func:`app.cache.keys.make_key`, so an entry is
    keyed by what went into it rather than by when it was written. Uniqueness makes writes
    an upsert and makes a concurrent double-compute converge on one row.

``namespace`` + ``expires_at`` (composite index)
    The two maintenance query shapes, and only those: ``clear(namespace)`` for precise
    invalidation, and ``cleanup.prune_cache`` sweeping everything already expired.

``content_hash``
    Lets an invalidation check ask *did the input actually change?* before discarding a
    still-valid entry.

``hits``
    Cheap hit accounting, so a namespace that never pays for itself is visible rather than
    inferred.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from sqlalchemy import Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import JSONType, utcnow
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

__all__ = ["CacheEntry"]


# --------------------------------------------------------------------------------------
# Column sizes
# --------------------------------------------------------------------------------------

#: Width of a cache key. ``make_key`` produces ``"<namespace>:<sha256>"``, but callers may
#: use readable composite keys, so this is sized for both.
CACHE_KEY_MAX_LENGTH: Final[int] = 255

#: Width of a namespace label (``llm``, ``embeddings``, ``company``, ``render``, ...).
CACHE_NAMESPACE_MAX_LENGTH: Final[int] = 64

#: Width of a hex-encoded SHA-256 digest.
SHA256_HEX_LENGTH: Final[int] = 64


class CacheEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One durable cached value, addressed by :attr:`key`.

    Note:
        :attr:`value` is typed as ``Any`` because a cached payload is whatever the producer
        returned — an object, a list of floats, a string. It is a plain JSON column and is
        not change tracked; a cache entry is immutable by intent, so reads never mutate it
        and writes replace it wholesale.
    """

    __tablename__ = "cache_entries"
    __table_args__ = (
        # The two maintenance shapes: namespace invalidation and expiry sweeps.
        Index("ix_cache_entries_namespace_expires_at", "namespace", "expires_at"),
    )

    key: Mapped[str] = mapped_column(
        String(CACHE_KEY_MAX_LENGTH),
        nullable=False,
        unique=True,
        index=True,
        doc="Content-addressed cache key. Uniqueness makes a write an upsert.",
    )
    namespace: Mapped[str] = mapped_column(
        String(CACHE_NAMESPACE_MAX_LENGTH),
        nullable=False,
        doc="Logical group the entry belongs to; the unit of precise invalidation.",
    )
    value: Mapped[Any] = mapped_column(
        JSONType(),
        nullable=True,
        doc="The cached payload, serialised as JSON. NULL is a legitimate cached value.",
    )
    content_hash: Mapped[str | None] = mapped_column(
        String(SHA256_HEX_LENGTH),
        nullable=True,
        doc="Digest of the inputs, so invalidation can check whether they really changed.",
    )
    hits: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
        doc="How many times this entry has been served; feeds cache-effectiveness metrics.",
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        doc="When the entry stops being served. NULL means it never expires on its own.",
    )

    # -- behaviour --------------------------------------------------------------------

    def is_expired(self, now: datetime | None = None) -> bool:
        """Whether this entry has passed its time-to-live.

        Args:
            now: Instant to compare against. Defaults to the current UTC time; passing it
                explicitly makes sweep runs deterministic and testable.

        Returns:
            ``False`` when :attr:`expires_at` is unset — no expiry means never expires.
        """
        if self.expires_at is None:
            return False
        return self.expires_at <= (now if now is not None else utcnow())

    def is_fresh(self, now: datetime | None = None) -> bool:
        """Whether this entry may still be served.

        Args:
            now: Instant to compare against, forwarded to :meth:`is_expired`.

        Returns:
            The negation of :meth:`is_expired`, named positively because that is how call
            sites read: ``if entry.is_fresh(): return entry.value``.
        """
        return not self.is_expired(now)

    def register_hit(self) -> int:
        """Count one cache hit against this entry.

        Returns:
            The updated hit count. Unset counters — an unflushed row whose column default
            has not been applied — are treated as zero.
        """
        self.hits = (self.hits or 0) + 1
        return self.hits

    def __repr__(self) -> str:
        """Return a debugger-friendly summary that never triggers a lazy load."""
        return (
            f"CacheEntry(id={self.id!r}, namespace={self.namespace!r}, "
            f"key={self.key!r}, hits={self.hits!r}, expires_at={self.expires_at!r})"
        )
