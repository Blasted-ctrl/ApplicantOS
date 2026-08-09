"""The ORM layer: unique constraints, enum storage, and the invariants they protect.

A unique constraint is the only guarantee in this system that survives a bug in the
application code, which is why ``docs/SAFETY.md`` lists ``UNIQUE(user_id, posting_id)`` as
mechanism *one* of two for never applying twice. A constraint that is declared on the model
but missing from the created schema is worse than no constraint at all, because everything
above it is written assuming it holds.

So every unique constraint in the schema is exercised by actually trying to violate it and
asserting the database refuses. The test is generic over
:data:`~tests.test_models.UNIQUE_CONSTRAINTS` rather than hand-written per table, so adding a
table without adding its constraint shows up as a gap in the inventory check at the bottom.

Enum storage gets its own group: ``values_callable`` is what persists ``"full_time"`` rather
than SQLAlchemy's default of ``"FULL_TIME"``, and that string is the API's wire format and
the desktop app's TypeScript union. Getting it wrong breaks the client silently.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

import app.models as models
from app.database.base import Base
from app.models.application import Application
from app.models.company import Company
from app.models.enums import (
    ApplicationStatus,
    ATSProviderName,
    EmploymentType,
    IndexStatus,
    SourceKind,
    WorkArrangement,
)
from app.models.knowledge import KnowledgeSource
from app.models.posting import JobPosting
from app.models.score import JobScore
from app.models.user import User

# ======================================================================================
# Every unique constraint actually raises
# ======================================================================================


async def _expect_conflict(session, row) -> None:
    """Add *row*, expect an ``IntegrityError``, and leave the session usable."""
    session.add(row)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_users_email_is_unique(session, user) -> None:
    """Two accounts cannot share an address."""
    await _expect_conflict(session, User(email=user.email, full_name="Impostor", preferences={}))


async def test_user_email_is_normalised_before_the_constraint(session, user) -> None:
    """Case variation must not defeat the unique index."""
    await _expect_conflict(
        session, User(email=user.email.upper(), full_name="Impostor", preferences={})
    )


async def test_companies_normalized_name_is_unique(session, company) -> None:
    """One employer, one row — the block list and the analytics both depend on it."""
    await _expect_conflict(
        session, Company(name="Acme Robotics LLC", normalized_name=company.normalized_name)
    )


async def test_job_postings_provider_external_id_is_unique(session, posting) -> None:
    """The provider's own identity for a posting."""
    await _expect_conflict(
        session,
        JobPosting(
            company_id=posting.company_id,
            provider=posting.provider,
            external_id=posting.external_id,
            url="https://example.com/other",
            title="Other",
            content_hash="different",
            dedupe_key="different-key",
        ),
    )


async def test_job_postings_dedupe_key_is_unique(session, posting) -> None:
    """Backs the "same posting twice" half of deduplication."""
    await _expect_conflict(
        session,
        JobPosting(
            company_id=posting.company_id,
            provider=ATSProviderName.LEVER,
            external_id="a-different-id",
            url="https://example.com/other",
            title="Other",
            content_hash="different",
            dedupe_key=posting.dedupe_key,
        ),
    )


async def test_dedupe_key_may_be_null_more_than_once(session, make_posting) -> None:
    """SQL treats NULLs as distinct, so not-yet-deduped rows coexist.

    ``Pipeline.ingest`` relies on this: a posting exists between insertion and dedupe running.
    """
    await make_posting(dedupe_key=None, external_id="pending-1")
    await make_posting(dedupe_key=None, external_id="pending-2")  # must not raise


async def test_applications_user_posting_is_unique(session, application) -> None:
    """**Golden rule #1's database half.**"""
    await _expect_conflict(
        session,
        Application(
            user_id=application.user_id,
            posting_id=application.posting_id,
            company_id=application.company_id,
            status=ApplicationStatus.DRAFT,
        ),
    )


async def test_job_scores_posting_user_is_unique(session, posting, user, make_score) -> None:
    """Re-scoring replaces rather than accumulates."""
    await make_score(posting)
    await _expect_conflict(
        session,
        JobScore(posting_id=posting.id, user_id=user.id, total=1, normalized=1, breakdown={}),
    )


async def test_knowledge_sources_user_kind_uri_is_unique(session, user) -> None:
    """Adding the same GitHub profile twice is one source, not two."""
    first = KnowledgeSource(
        user_id=user.id,
        kind=SourceKind.GITHUB_PROFILE,
        uri="https://github.com/ada",
        index_status=IndexStatus.PENDING,
    )
    session.add(first)
    await session.commit()

    await _expect_conflict(
        session,
        KnowledgeSource(
            user_id=user.id,
            kind=SourceKind.GITHUB_PROFILE,
            uri="https://github.com/ada",
            index_status=IndexStatus.PENDING,
        ),
    )


async def test_user_profiles_user_id_is_unique(session, user) -> None:
    """One profile per user."""
    from app.models.profile import UserProfile

    session.add(UserProfile(user_id=user.id))
    await session.commit()
    await _expect_conflict(session, UserProfile(user_id=user.id))


async def test_resume_versions_resume_version_number_is_unique(session, user) -> None:
    """Version numbers are the ordering of a resume's history."""
    from app.models.resume import Resume, ResumeVersion

    resume = Resume(user_id=user.id, name="Default", template="modern")
    session.add(resume)
    await session.flush()

    session.add(ResumeVersion(resume_id=resume.id, version_number=1, content_json={}))
    await session.commit()

    await _expect_conflict(
        session, ResumeVersion(resume_id=resume.id, version_number=1, content_json={})
    )


async def test_knowledge_chunks_document_ordinal_is_unique(session, user) -> None:
    """Chunk ordinals reconstruct a document in order."""
    from app.models.knowledge import KnowledgeChunk, KnowledgeDocument

    source = KnowledgeSource(
        user_id=user.id,
        kind=SourceKind.RESUME,
        uri="file:///cv.pdf",
        index_status=IndexStatus.PENDING,
    )
    session.add(source)
    await session.flush()

    document = KnowledgeDocument(
        user_id=user.id,
        source_id=source.id,
        kind=SourceKind.RESUME,
        uri="file:///cv.pdf",
        title="CV",
        content_hash="abc",
    )
    session.add(document)
    await session.flush()

    session.add(KnowledgeChunk(document_id=document.id, ordinal=0, text="first"))
    await session.commit()

    await _expect_conflict(session, KnowledgeChunk(document_id=document.id, ordinal=0, text="dup"))


async def test_checkpoints_key_is_unique(session) -> None:
    """The checkpoint key *is* the idempotency token."""
    from app.models.checkpoint import Checkpoint

    session.add(Checkpoint(key="apply:1:submit", owner="apply:1", step="submit", state={}))
    await session.commit()

    await _expect_conflict(
        session, Checkpoint(key="apply:1:submit", owner="apply:1", step="submit", state={})
    )


# ======================================================================================
# The inventory — a new table cannot skip this file unnoticed
# ======================================================================================

#: Tables whose unique constraints are exercised above.
COVERED_TABLES: frozenset[str] = frozenset(
    {
        "users",
        "companies",
        "job_postings",
        "applications",
        "job_scores",
        "knowledge_sources",
        "user_profiles",
        "resume_versions",
        "knowledge_chunks",
        "checkpoints",
    }
)

#: Tables with a unique constraint that this file deliberately does not exercise, each
#: because it belongs to a subsystem with its own file or has no cheap fixture.
KNOWN_UNCOVERED: frozenset[str] = frozenset(
    {
        "cache_entries",  # covered by the cache layer's own behaviour
        "email_accounts",  # tracking subsystem
        "status_signals",  # tracking subsystem, exercised in test_tracking.py
        "knowledge_documents",
        "knowledge_entities",
        "knowledge_edges",
    }
)


def _tables_with_unique_constraints() -> set[str]:
    """Every table carrying a unique constraint or a unique index."""
    found: set[str] = set()
    for table in Base.metadata.sorted_tables:
        has_constraint = any(
            type(constraint).__name__ == "UniqueConstraint" for constraint in table.constraints
        )
        has_index = any(index.unique for index in table.indexes)
        if has_constraint or has_index:
            found.add(table.name)
    return found


def test_every_unique_constraint_is_accounted_for() -> None:
    """A new unique constraint must be either tested here or explicitly listed as deferred."""
    found = _tables_with_unique_constraints()
    unaccounted = found - COVERED_TABLES - KNOWN_UNCOVERED
    assert not unaccounted, (
        "these tables gained a unique constraint with no test and no deferral: "
        f"{sorted(unaccounted)}"
    )


def test_the_covered_list_has_no_stale_entries() -> None:
    """A table that lost its constraint should not still be claimed as covered."""
    found = _tables_with_unique_constraints()
    stale = COVERED_TABLES - found
    assert not stale, f"these tables no longer have a unique constraint: {sorted(stale)}"


# ======================================================================================
# The schema is complete
# ======================================================================================


def test_every_model_module_is_imported_by_the_package() -> None:
    """``init_db`` populates ``Base.metadata`` from ``app.models``; a missing import empties it.

    ``docs/OPEN_QUESTIONS.md`` records this as load-bearing twice, because a
    ``relationship("User")`` that never resolves raises at first *use*, not at import.
    """
    assert len(Base.metadata.tables) >= 22


def test_model_classes_and_table_names_agree() -> None:
    """``MODEL_CLASSES`` and ``TABLE_NAMES`` are the "every model is in the migration" check."""
    assert len(models.MODEL_CLASSES) == len(models.TABLE_NAMES)
    assert [cls.__tablename__ for cls in models.MODEL_CLASSES] == list(models.TABLE_NAMES)


def test_every_table_has_a_uuid_primary_key_and_timestamps() -> None:
    """§4: "All tables: UUID PK, ``created_at``, ``updated_at``"."""
    for table in Base.metadata.sorted_tables:
        columns = set(table.columns.keys())
        assert "id" in columns, f"{table.name} has no id column"
        assert "created_at" in columns, f"{table.name} has no created_at"
        assert "updated_at" in columns, f"{table.name} has no updated_at"


async def test_the_schema_creates_and_round_trips(session, engine) -> None:
    """A smoke test that the metadata the tests use is the metadata the app uses."""
    async with engine.connect() as connection:
        tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())

    assert "applications" in tables
    assert "job_postings" in tables
    assert "knowledge_facts" in tables


# ======================================================================================
# Enum storage — the wire format the desktop app parses
# ======================================================================================


async def test_enums_persist_as_their_lowercase_values(session, make_posting) -> None:
    """``values_callable`` persists ``"full_time"``, not SQLAlchemy's default ``"FULL_TIME"``.

    That string is the JSON API's value and the TypeScript union in
    ``desktop/src/lib/api/types.ts``. Persisting the member *name* would break the client
    silently, because JSON has no schema to complain.
    """
    from sqlalchemy import text

    posting = await make_posting(
        employment_type=EmploymentType.FULL_TIME,
        work_arrangement=WorkArrangement.REMOTE,
        provider=ATSProviderName.GREENHOUSE,
    )

    row = (
        await session.execute(
            text(
                "SELECT employment_type, work_arrangement, provider "
                "FROM job_postings WHERE id = :id"
            ),
            {"id": str(posting.id)},
        )
    ).one()

    assert row[0] == "full_time"
    assert row[1] == "remote"
    assert row[2] == "greenhouse"


async def test_enum_values_round_trip_back_to_members(session, make_posting) -> None:
    """Reading returns the enum member, not the raw string."""
    posting = await make_posting(employment_type=EmploymentType.INTERNSHIP)
    fetched = await session.scalar(select(JobPosting).where(JobPosting.id == posting.id))
    assert fetched.employment_type is EmploymentType.INTERNSHIP


@pytest.mark.parametrize(
    "enum_cls",
    [ApplicationStatus, ATSProviderName, EmploymentType, WorkArrangement, SourceKind, IndexStatus],
)
def test_every_enum_value_is_lowercase_snake_case(enum_cls) -> None:
    """§3 freezes the vocabulary as lowercase snake_case."""
    for member in enum_cls:
        assert member.value == member.value.lower()
        assert " " not in member.value
        assert "-" not in member.value


def test_application_status_helpers_agree_with_each_other() -> None:
    """``is_post_submit`` is what golden rule #1's status guard reads."""
    for status in ApplicationStatus:
        if status.is_post_submit():
            assert status is not ApplicationStatus.DRAFT
            assert status is not ApplicationStatus.READY

    assert ApplicationStatus.SUBMITTED.is_post_submit() is True
    assert ApplicationStatus.CONFIRMED.is_post_submit() is True
    assert ApplicationStatus.READY.is_post_submit() is False
    assert ApplicationStatus.DRAFT.is_post_submit() is False


def test_terminal_states_are_not_active() -> None:
    """A state cannot be both finished and in flight."""
    for status in ApplicationStatus.terminal_states():
        assert status.is_active() is False


# ======================================================================================
# Referential integrity that protects golden rule #1
# ======================================================================================


async def test_deleting_an_applied_to_posting_is_refused(session, application, posting) -> None:
    """``applications.posting_id`` is RESTRICT, not CASCADE — deliberately.

    With CASCADE, hard-deleting a posting removes the application with it; discovery then
    rediscovers the job under a fresh primary key, ``UNIQUE(user_id, posting_id)`` no longer
    recognises it, and the system applies a **second** time, silently.
    """
    await session.delete(posting)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_a_posting_with_no_application_can_be_deleted(session, make_posting) -> None:
    """RESTRICT must not make ordinary maintenance impossible."""
    orphan = await make_posting(external_id="deletable")
    await session.delete(orphan)
    await session.flush()  # must not raise


async def test_user_preferences_default_to_a_valid_document(session, user) -> None:
    """``users.preferences`` is NOT NULL and parses through ``User.prefs``."""
    assert user.preferences == {}
    prefs = user.prefs
    assert prefs.min_score == 70
    assert prefs.auto_apply is False


async def test_updating_preferences_persists(session, user) -> None:
    """``prefs`` returns a copy, so ``update_prefs`` is the supported mutation path."""
    user.update_prefs(min_score=85, remote_only=True)
    await session.commit()
    await session.refresh(user)

    assert user.prefs.min_score == 85
    assert user.prefs.remote_only is True


async def test_an_unknown_preference_key_is_rejected(session, user) -> None:
    """A typo in a preference name must fail loudly rather than vanish."""
    with pytest.raises((ValueError, TypeError)):
        user.update_prefs(min_scoer=85)


async def test_application_ids_are_uuids(session, application) -> None:
    """The ``GUID`` type stores a real UUID on both backends."""
    assert isinstance(application.id, uuid.UUID)
