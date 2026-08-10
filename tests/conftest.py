"""Shared fixtures for the ApplicantOS test suite.

Everything here exists to make the golden-rule tests cheap to write and impossible to fake.
Three principles shape it:

**The environment is pinned before ``app`` is imported.** ``app.config.settings`` builds its
singleton at import time and ``app.database.session`` builds an engine from it, so the
zero-dependency configuration has to be in ``os.environ`` before the first ``import app``.
That happens at the top of this module, which pytest imports before collecting any test.

**Every test gets its own schema.** :func:`engine` creates an in-memory SQLite database with
``StaticPool`` — an in-memory database lives inside its connection, so a pool that discards
connections would discard the schema — runs ``create_all``, and drops it afterwards. Tests
never see each other's rows.

**The doubles record, they do not merely answer.** :class:`FakePage` keeps an ordered log of
every click, fill, check, upload and lookup. That is the whole point of
``test_golden_kill_switch``: a function can return ``False`` and still have clicked, so the
assertion has to be against what the page was actually asked to do.
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

# --------------------------------------------------------------------------------------
# Environment — must precede every `app` import in the process.
# --------------------------------------------------------------------------------------

_TEST_ENV: dict[str, str] = {
    "SQLITE_MODE": "true",
    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
    "LLM_PROVIDER": "null",
    "EMBEDDING_PROVIDER": "hashing",
    "VECTOR_STORE": "memory",
    "CACHE_BACKEND": "memory",
    # Both switches in the safe position. Individual tests open them deliberately.
    "AUTO_APPLY_ENABLED": "false",
    "DRY_RUN": "true",
    "LOG_JSON": "true",
    "LOG_LEVEL": "DEBUG",
    "METRICS_ENABLED": "true",
    "ENVIRONMENT": "local",
    "ANTHROPIC_API_KEY": "",
    "OPENAI_API_KEY": "",
    "GITHUB_TOKEN": "",
}
for _key, _value in _TEST_ENV.items():
    os.environ[_key] = _value

# The repository root, so `import app` works when pytest is invoked from anywhere.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.models  # noqa: E402,F401  (populates Base.metadata)
from app.config.settings import Settings, get_settings  # noqa: E402
from app.database.base import Base  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.models.enums import (  # noqa: E402
    ApplicationStatus,
    ATSProviderName,
    EmploymentType,
    FactKind,
    PostingStatus,
    WorkArrangement,
)
from app.models.knowledge import KnowledgeFact  # noqa: E402
from app.models.posting import JobPosting  # noqa: E402
from app.models.user import User  # noqa: E402
from tests.fakes import (  # noqa: E402
    FakePage,
    FakeSession,
    FakeStorage,
    RecordingLLM,
    build_mock_transport,
)

pytest_plugins: list[str] = []


# ======================================================================================
# Settings
# ======================================================================================


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """The process-wide settings singleton, pointed at this test's temp directory.

    ``get_settings()`` is ``lru_cache``d and returns the same object as the module-level
    ``settings``, so every module that calls it mid-test — ``AutoFiller.submit`` does, on
    purpose, so the kill switch reflects a flip without a restart — observes these values.
    ``monkeypatch.setattr`` restores them when the test ends.

    ``get_storage()`` is memoised the same way but is *not* re-read per call: it resolves the
    root once and keeps it. So the memo is dropped on both sides of the test. Without that,
    the first test to touch storage pins every later test's uploads to the first test's temp
    directory, and the failure surfaces as "the bytes are not where the row says they are" —
    which reads like a bug in the code under test rather than in this fixture.
    """
    resolved = get_settings()
    for field, value in (
        ("data_dir", str(tmp_path / "data")),
        ("cache_dir", str(tmp_path / "cache")),
        ("storage_local_root", str(tmp_path / "storage")),
        ("screenshot_dir", str(tmp_path / "screenshots")),
        ("browser_user_data_dir", str(tmp_path / "browser")),
        ("sqlite_mode", True),
        ("llm_provider", "null"),
        ("embedding_provider", "hashing"),
        ("vector_store", "memory"),
        ("cache_backend", "memory"),
        ("auto_apply_enabled", False),
        ("dry_run", True),
    ):
        monkeypatch.setattr(resolved, field, value, raising=False)

    from app.storage import reset_storage

    reset_storage()
    yield resolved
    reset_storage()


@pytest.fixture
def submission_allowed(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings with **both** switches open — the only configuration that may submit."""
    monkeypatch.setattr(settings, "auto_apply_enabled", True)
    monkeypatch.setattr(settings, "dry_run", False)
    assert settings.is_submission_allowed is True
    return settings


# ======================================================================================
# Database
# ======================================================================================


@pytest.fixture
async def engine():
    """A private in-memory SQLite engine with the full schema created.

    ``StaticPool`` is mandatory: an in-memory SQLite database lives inside its connection,
    so ``NullPool`` would drop the schema between statements.
    """
    test_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
    )

    from sqlalchemy import event

    @event.listens_for(test_engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        # SQLite defaults foreign keys OFF, which silently voids every ON DELETE rule in
        # the schema — including the RESTRICT that protects golden rule #1.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield test_engine
    finally:
        async with test_engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await test_engine.dispose()


@pytest.fixture
async def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    """A session factory bound to this test's engine."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def session(session_factory: async_sessionmaker[AsyncSession]):
    """One :class:`AsyncSession` per test, rolled back and closed afterwards."""
    async with session_factory() as db:
        try:
            yield db
        finally:
            await db.rollback()
            await db.close()


# ======================================================================================
# Domain factories
# ======================================================================================


@pytest.fixture
async def user(session: AsyncSession) -> User:
    """A persisted, onboarded applicant with default preferences."""
    record = User(
        email="ada@example.com",
        full_name="Ada Lovelace",
        is_active=True,
        preferences={},
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record


@pytest.fixture
async def company(session: AsyncSession) -> Company:
    """A persisted employer."""
    record = Company(name="Acme Robotics, Inc.", normalized_name="acme robotics")
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record


@pytest.fixture
async def make_posting(session: AsyncSession, company: Company):
    """Factory building persisted :class:`JobPosting` rows.

    Each call gets a distinct ``external_id`` and ``dedupe_key`` unless one is supplied, so a
    test that wants two postings does not have to invent identifiers to dodge the unique
    constraints.
    """

    async def _make(**overrides) -> JobPosting:
        marker = uuid.uuid4().hex[:12]
        values: dict = {
            "company_id": company.id,
            "provider": ATSProviderName.GREENHOUSE,
            "external_id": f"gh-{marker}",
            "url": f"https://boards.greenhouse.io/acme/jobs/{marker}",
            "apply_url": f"https://boards.greenhouse.io/acme/jobs/{marker}#app",
            "title": "Senior Backend Engineer",
            "description": "Python, FastAPI and PostgreSQL. Remote within the US.",
            "location": "Remote — US",
            "work_arrangement": WorkArrangement.REMOTE,
            "employment_type": EmploymentType.FULL_TIME,
            "content_hash": f"hash-{marker}",
            "dedupe_key": f"dedupe-{marker}",
            "status": PostingStatus.SCORED,
        }
        values.update(overrides)
        record = JobPosting(**values)
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record

    return _make


@pytest.fixture
async def posting(make_posting) -> JobPosting:
    """One persisted posting."""
    return await make_posting()


@pytest.fixture
async def make_score(session: AsyncSession, user: User):
    """Factory persisting a :class:`JobScore` above the auto-apply floor.

    ``Pipeline.submit``'s guard ladder refuses an unscored posting outright — "refusing to
    apply blind" — so any test that wants to reach a *later* guard has to get past this one
    first.
    """
    from app.models.score import JobScore

    async def _make(target: JobPosting, *, normalized: int = 88, **overrides) -> JobScore:
        values: dict = {
            "posting_id": target.id,
            "user_id": user.id,
            "total": normalized,
            "normalized": normalized,
            "breakdown": {},
            "verdict": "apply",
        }
        values.update(overrides)
        record = JobScore(**values)
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record

    return _make


@pytest.fixture
async def make_application(session: AsyncSession, user: User):
    """Factory building persisted :class:`Application` rows for a posting."""
    from app.models.application import Application

    async def _make(target: JobPosting, **overrides) -> Application:
        values: dict = {
            "user_id": user.id,
            "posting_id": target.id,
            "company_id": target.company_id,
            "status": ApplicationStatus.READY,
        }
        values.update(overrides)
        record = Application(**values)
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record

    return _make


@pytest.fixture
async def application(make_application, posting: JobPosting):
    """One persisted application in ``READY``."""
    return await make_application(posting)


@pytest.fixture
async def master_facts(session: AsyncSession, user: User) -> list[KnowledgeFact]:
    """A small, realistic knowledge base — the only thing a resume may be built from.

    Deliberately concrete: every fact carries an organization, a role, dates and metrics, so
    the anti-fabrication tests can assert that those fields are copied from the fact rather
    than taken from the model.
    """
    specs = [
        {
            "kind": FactKind.ACCOMPLISHMENT,
            "text": (
                "Cut p99 checkout latency from 840ms to 120ms by adding a read-through Redis cache."
            ),
            "organization": "Acme Robotics",
            "role": "Backend Engineer",
            "date_start": "2022-01",
            "date_end": "2024-06",
            "skills": ["Python", "Redis", "Performance"],
            "technologies": ["Python", "Redis", "PostgreSQL"],
            "metrics": ["840ms", "120ms"],
            "impact_score": 9,
        },
        {
            "kind": FactKind.RESPONSIBILITY,
            "text": "Owned the payments service handling 12000 requests per second.",
            "organization": "Acme Robotics",
            "role": "Backend Engineer",
            "date_start": "2022-01",
            "date_end": "2024-06",
            "skills": ["Python", "Distributed Systems"],
            "technologies": ["Python", "FastAPI"],
            "metrics": ["12000"],
            "impact_score": 8,
        },
        {
            "kind": FactKind.SKILL_USAGE,
            "text": "Built internal tooling in Python and FastAPI used by 40 engineers.",
            "organization": "Initech",
            "role": "Software Engineer",
            "date_start": "2020-03",
            "date_end": "2021-12",
            "skills": ["Python", "FastAPI"],
            "technologies": ["Python", "FastAPI", "Docker"],
            "metrics": ["40"],
            "impact_score": 6,
        },
        {
            "kind": FactKind.EDUCATION_ITEM,
            "text": "BSc Computer Science, first class honours.",
            "organization": "University of Manchester",
            "role": "BSc Computer Science",
            "date_start": "2016-09",
            "date_end": "2020-06",
            "skills": ["Algorithms"],
            "technologies": [],
            "metrics": [],
            "impact_score": 4,
        },
    ]

    facts: list[KnowledgeFact] = []
    for spec in specs:
        fact = KnowledgeFact(user_id=user.id, **spec)
        fact.refresh_derived_fields()
        session.add(fact)
        facts.append(fact)
    await session.commit()
    for fact in facts:
        await session.refresh(fact)
    return facts


# ======================================================================================
# Doubles
# ======================================================================================


@pytest.fixture
def null_llm() -> RecordingLLM:
    """A deterministic offline model client that records every prompt it was given."""
    return RecordingLLM()


@pytest.fixture
def fake_storage(tmp_path: Path) -> FakeStorage:
    """An in-memory :class:`~app.storage.base.StorageBackend`."""
    return FakeStorage(root=tmp_path / "fake-storage")


@pytest.fixture
def fake_page() -> FakePage:
    """A duck-typed Playwright ``Page`` that records every action asked of it."""
    return FakePage()


@pytest.fixture
def fake_browser(fake_page: FakePage) -> FakeSession:
    """A duck-typed ``BrowserSession`` wrapping :func:`fake_page`."""
    return FakeSession(page=fake_page)


@pytest.fixture
def mock_http():
    """An ``httpx.MockTransport`` router. Call ``.route(url_fragment, json=..., status=...)``."""
    return build_mock_transport()


# ======================================================================================
# API client
# ======================================================================================


@pytest.fixture
async def api_client(session: AsyncSession, settings: Settings, user: User):
    """An ``httpx.AsyncClient`` speaking ASGI directly to ``create_app()``.

    The app's ``get_session`` dependency is overridden with this test's session, so a request
    and the test see the same rows. The lifespan is *not* run — it would load plugins and
    touch the real engine — and plugins are loaded explicitly instead.
    """
    import httpx

    from app.api.deps import get_current_user
    from app.database.session import get_session
    from app.main import create_app
    from app.plugins.loader import load_all
    from app.plugins.registry import registry

    registry.configure(settings)
    load_all()

    application = create_app(settings)

    async def _session_override():
        yield session

    async def _user_override() -> User:
        return user

    application.dependency_overrides[get_session] = _session_override
    application.dependency_overrides[get_current_user] = _user_override

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        client.app = application  # type: ignore[attr-defined]
        yield client

    application.dependency_overrides.clear()
