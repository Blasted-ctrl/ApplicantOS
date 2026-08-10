#!/usr/bin/env python3
"""Wipe the local database so you can start on your own data.

    python -m scripts.reset --yes
    python -m scripts.reset --yes --keep-profile

A fresh clone seeds a fictional embedded engineer so the screens are not empty on first launch.
That is useful for looking around and actively harmful once you start applying: a résumé
generated from someone else's knowledge graph is someone else's résumé. This removes it.

Deliberately destructive and deliberately awkward. It refuses to run without ``--yes``, refuses
to touch anything but SQLite, and prints what it is about to delete first. There is no
``--force`` that skips the summary.

Only the local SQLite database is in scope. A PostgreSQL install is a deployment someone chose
on purpose, and dropping it from a convenience script is not a favour — for those, run
``alembic downgrade base`` and ``alembic upgrade head`` yourself, deliberately.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path
from typing import Final

__all__ = ["main"]

#: Tables emptied by ``--keep-profile``: everything the system learned or did, but not who
#: you are. Ordered so a child is always deleted before its parent.
_DERIVED_TABLES: Final[tuple[str, ...]] = (
    "application_events",
    "applications",
    "cover_letters",
    "resume_versions",
    "job_scores",
    "job_postings",
    "companies",
    "status_signals",
    "run_sessions",
    "checkpoints",
    "knowledge_chunks",
    "knowledge_documents",
    "knowledge_edges",
    "knowledge_entities",
    "knowledge_facts",
    "knowledge_sources",
    "memory_entries",
    "uploaded_files",
    "log_entries",
    "cache_entries",
)

#: Directories of generated artefacts. Rendered documents are disposable by design (golden
#: rule #6) and screenshots belong to applications that are about to stop existing.
_ARTEFACT_DIRS: Final[tuple[str, ...]] = ("storage", "screenshots", "browser", "cache")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments, or ``None`` to read :data:`sys.argv`.

    Returns:
        The parsed namespace.
    """
    parser = argparse.ArgumentParser(
        prog="python -m scripts.reset",
        description="Delete the local database so you can start on your own data.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required. Without it this prints what it would delete and stops.",
    )
    parser.add_argument(
        "--keep-profile",
        action="store_true",
        help="Empty the derived tables but keep your account, profile and preferences.",
    )
    return parser.parse_args(argv)


async def _truncate_derived() -> dict[str, int]:
    """Empty every derived table, leaving identity intact.

    Returns:
        Rows removed per table, for the summary.
    """
    from sqlalchemy import delete, func, select

    from app.database.session import session_scope
    from app.models import Base

    removed: dict[str, int] = {}
    async with session_scope() as session:
        for name in _DERIVED_TABLES:
            table = Base.metadata.tables.get(name)
            if table is None:  # a table this build does not have
                continue
            count = int(await session.scalar(select(func.count()).select_from(table)) or 0)
            if count:
                await session.execute(delete(table))
                removed[name] = count
    return removed


def _database_file() -> Path | None:
    """Return the SQLite file backing this install, or ``None`` if it is not SQLite.

    Returns:
        Path to the database file, or ``None`` when the URL names another dialect.
    """
    from app.config.settings import get_settings

    settings = get_settings()
    url = settings.database_url
    if not url.startswith("sqlite"):
        return None
    _, _, remainder = url.partition("///")
    return Path(remainder) if remainder else None


def main(argv: list[str] | None = None) -> int:
    """Delete local state.

    Args:
        argv: Command-line arguments, or ``None`` to read :data:`sys.argv`.

    Returns:
        ``0`` on success, ``1`` when the install is not SQLite, ``2`` without ``--yes``.
    """
    args = _parse_args(argv)

    database = _database_file()
    if database is None:
        print(
            "This install does not use SQLite, so this script will not touch it.\n"
            "For a Postgres deployment run the migrations yourself:\n"
            "    alembic downgrade base && alembic upgrade head",
            file=sys.stderr,
        )
        return 1

    from app.config.settings import get_settings

    data_path = get_settings().data_path

    print("About to delete:")
    if args.keep_profile:
        print("  every posting, application, document, session and knowledge fact")
        print("  keeping: your account, profile and preferences")
    else:
        print(f"  {database}  (the whole database, including your profile)")
    for name in _ARTEFACT_DIRS:
        target = data_path / name
        if target.exists():
            print(f"  {target}")

    if not args.yes:
        print("\nNothing was deleted. Re-run with --yes if that is what you want.")
        return 2

    if args.keep_profile:
        removed = asyncio.run(_truncate_derived())
        total = sum(removed.values())
        print(f"\nEmptied {len(removed)} table(s), {total} row(s):")
        for name, count in sorted(removed.items(), key=lambda item: -item[1]):
            print(f"  {name:24s} {count}")
    elif database.exists():
        database.unlink()
        print(f"\nDeleted {database}")
    else:
        print(f"\n{database} was already absent.")

    for name in _ARTEFACT_DIRS:
        target = data_path / name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            print(f"Removed {target}")

    print("\nNow recreate the schema:")
    print("    alembic upgrade head")
    print("\nThen open the app and onboard with your own details.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
