"""Declarative mixins shared by every ORM model.

Four concerns recur across essentially every table in ApplicantOS: a UUID primary key,
audit timestamps, soft deletion, and ownership by a user. Each is implemented once here and
composed into models by inheritance::

    class Application(UUIDPrimaryKeyMixin, TimestampMixin, UserOwnedMixin, Base):
        __tablename__ = "applications"

Mixins are declared with SQLAlchemy 2.0's :func:`~sqlalchemy.orm.declarative_mixin`
decorator. Plain ``mapped_column`` objects on a mixin are copied into each subclass
automatically; :func:`~sqlalchemy.orm.declared_attr` is used only where the column must be
constructed per-subclass, which is the case for :class:`UserOwnedMixin` because a
``ForeignKey`` object cannot be shared between tables.

Design decisions worth knowing:

* **UUID primary keys, generated client-side.** ``uuid4`` is produced in Python, not by the
  database, so an object has its identity before it is flushed. That is what lets the
  pipeline build a whole object graph — application, resume version, cover letter, events —
  and write it in one transaction, and it keeps checkpoint state meaningful across a crash.
* **Timestamps carry both a Python default and a server default.** The Python default keeps
  the value available on the in-memory object immediately; the server default protects rows
  written by migrations or raw SQL. ``updated_at`` uses a Python-side ``onupdate`` because
  ``server_onupdate`` has no portable equivalent across PostgreSQL and SQLite.
* **Soft deletion never rewrites history.** :class:`SoftDeleteMixin` stamps ``deleted_at``
  rather than issuing a ``DELETE``, which matters for golden rule #6: a
  ``ResumeVersion``'s ``content_json`` is retained forever even after its rendered file is
  discarded.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ColumnElement, ForeignKey, func
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, declarative_mixin, declared_attr, mapped_column

from app.database.types import GUID, UTCDateTime, utcnow

__all__ = [
    "USER_FOREIGN_KEY_TARGET",
    "SoftDeleteMixin",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "UserOwnedMixin",
]

#: Fully-qualified target of the user ownership foreign key. Declared as a string so the
#: mixin never has to import the ``User`` model, which would create an import cycle.
USER_FOREIGN_KEY_TARGET: str = "users.id"

#: Referential action applied when a user is deleted: everything they own goes with them.
#: On SQLite this is only honoured because :mod:`app.database.session` issues
#: ``PRAGMA foreign_keys=ON`` on every connection.
USER_DELETE_RULE: str = "CASCADE"


@declarative_mixin
class UUIDPrimaryKeyMixin:
    """Adds a UUID version-4 primary key named ``id``.

    The value is generated in Python at object construction, so an unflushed instance
    already has a stable identity that can be referenced by other objects in the same unit
    of work. Stored as a native ``UUID`` on PostgreSQL and ``CHAR(36)`` on SQLite via
    :class:`~app.database.types.GUID`.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
        sort_order=-100,
    )


@declarative_mixin
class TimestampMixin:
    """Adds ``created_at`` and ``updated_at`` audit columns.

    Both are timezone-aware UTC (:class:`~app.database.types.UTCDateTime`). ``created_at``
    is indexed because nearly every list endpoint and analytics query orders or filters by
    it. ``updated_at`` is maintained by SQLAlchemy's ``onupdate`` hook, which fires on any
    flush that changes the row.
    """

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utcnow,
        server_default=func.now(),
        nullable=False,
        index=True,
        sort_order=100,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
        sort_order=101,
    )

    def touch(self) -> None:
        """Bump ``updated_at`` without changing anything else.

        Useful when a row's meaning changed through a side channel — a re-index that found
        no differences, a cache refresh — and the change should still be visible to
        staleness checks.
        """
        self.updated_at = utcnow()


@declarative_mixin
class SoftDeleteMixin:
    """Adds a nullable ``deleted_at`` column and an ``is_deleted`` predicate.

    Rows are retired by stamping a timestamp rather than being removed, so history stays
    auditable and foreign keys stay intact. Queries must filter explicitly — there is no
    global soft-delete filter, because an implicit one hides rows from maintenance and
    analytics code that legitimately needs them.

    ``is_deleted`` is a :class:`~sqlalchemy.ext.hybrid.hybrid_property`: it reads as a bool
    on an instance and compiles to ``deleted_at IS NOT NULL`` inside a query.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=None,
        nullable=True,
        index=True,
        sort_order=102,
    )

    @hybrid_property
    def is_deleted(self) -> bool:
        """Return whether this row has been soft-deleted."""
        return self.deleted_at is not None

    @is_deleted.inplace.expression
    @classmethod
    def _is_deleted_expression(cls) -> ColumnElement[bool]:
        """SQL form of :attr:`is_deleted`, usable in ``where()`` clauses."""
        return cls.deleted_at.is_not(None)

    def soft_delete(self) -> None:
        """Mark the row deleted as of now. Idempotent — an existing stamp is preserved."""
        if self.deleted_at is None:
            self.deleted_at = utcnow()

    def restore(self) -> None:
        """Clear the deletion stamp, returning the row to active use."""
        self.deleted_at = None


@declarative_mixin
class UserOwnedMixin:
    """Adds an indexed, cascading ``user_id`` foreign key to ``users.id``.

    Declared through :func:`~sqlalchemy.orm.declared_attr` because each mapped class needs
    its own :class:`~sqlalchemy.ForeignKey` instance — a single shared one cannot belong to
    two tables. Indexed because every user-scoped query filters on it, and cascading
    because deleting a user must not leave orphaned applications, facts, or documents
    behind.
    """

    @declared_attr
    def user_id(cls) -> Mapped[uuid.UUID]:
        """Owning user's primary key."""
        return mapped_column(
            GUID(),
            ForeignKey(USER_FOREIGN_KEY_TARGET, ondelete=USER_DELETE_RULE),
            nullable=False,
            index=True,
            sort_order=-50,
        )
