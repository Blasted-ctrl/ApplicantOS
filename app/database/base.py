"""SQLAlchemy declarative base and metadata conventions.

Every ORM model in :mod:`app.models` inherits from :class:`Base`. Two things are configured
here, and both matter more than they look:

**Naming conventions.** Without them, SQLAlchemy lets the database invent names for
indexes, unique constraints, check constraints, and foreign keys — and those invented names
differ between PostgreSQL and SQLite. Alembic then cannot drop or alter a constraint it did
not name, so migrations that work on one backend fail on the other.
:data:`NAMING_CONVENTION` makes every constraint name deterministic and derivable from the
model definition, which is what makes ``alembic revision --autogenerate`` trustworthy.

**Type annotation map.** Models are written in SQLAlchemy 2.0 style
(``Mapped[uuid.UUID]``, ``Mapped[dict[str, Any]]``) with no explicit column type. The map
below tells SQLAlchemy which portable type from :mod:`app.database.types` backs each Python
annotation, so a model author never has to remember that a UUID means ``GUID`` on SQLite
and native ``UUID`` on PostgreSQL.

:class:`Base` also mixes in :class:`~sqlalchemy.ext.asyncio.AsyncAttrs`, which gives every
model an ``awaitable_attrs`` accessor. In an async session, touching an unloaded
relationship raises ``MissingGreenlet``; ``await obj.awaitable_attrs.related`` loads it
correctly instead. It adds no columns and changes no behaviour otherwise.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import MetaData, inspect
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase

from app.database.types import GUID, EmbeddingType, JSONType, UTCDateTime

__all__ = ["NAMING_CONVENTION", "Base"]


#: Deterministic constraint naming for every backend, so Alembic can always address a
#: constraint by name. ``column_0_N_name`` expands to every column in a composite
#: constraint, which keeps multi-column unique constraints unambiguous.
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base for every ApplicantOS ORM model.

    Subclasses inherit the shared :class:`~sqlalchemy.MetaData` (and therefore the naming
    convention), the annotation-to-column-type map, and ``awaitable_attrs``.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    #: Maps Python annotations used in ``Mapped[...]`` to the portable column types
    #: defined in :mod:`app.database.types`. Any annotation not listed here must be given
    #: an explicit type in ``mapped_column(...)``.
    type_annotation_map = {
        uuid.UUID: GUID(),
        datetime: UTCDateTime(),
        dict[str, Any]: JSONType(),
        list[str]: JSONType(),
        list[dict[str, Any]]: JSONType(),
        list[Any]: JSONType(),
        list[float]: EmbeddingType(),
    }

    def __repr__(self) -> str:
        """Return ``ClassName(pk=...)`` without triggering any lazy load.

        Uses the identity key from the instance's :class:`~sqlalchemy.orm.InstanceState`,
        which is already in memory. Reading mapped attributes directly would emit SQL for a
        detached or expired object, and a ``__repr__`` that can raise or issue queries is a
        debugging hazard.
        """
        state = inspect(self)
        identity = state.identity
        if identity is None:
            return f"{type(self).__name__}(transient)"
        rendered = ", ".join(str(part) for part in identity)
        return f"{type(self).__name__}(pk={rendered})"
