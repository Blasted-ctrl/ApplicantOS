#!/usr/bin/env python3
"""End-to-end smoke test for a running ApplicantOS backend. **Standard library only.**

Unit tests cannot catch a router that was never included, a Celery task registered on the
wrong queue, or a service whose two halves each work and disagree with each other. Every one
of those passes ``pytest`` and fails the moment a real person opens the app. This script is
the check for that class of failure, and it is deliberately built so that it can run against
a *deployed* backend with nothing installed beside it — no pytest, no httpx, no project
imports for the HTTP half.

Two halves:

**The endpoint table.** A declarative ``(area, method, path, body, expected_status)`` row for
every route in ``docs/CONTRACTS.md`` §14. It is a table rather than a function per endpoint
so that adding a route means adding a line, and so that "is this mounted?" is answered for
the whole surface at once.

**The flows.** Hand-written cross-service sequences that no single endpoint test covers:
discover → score → prepare → dry-run submit; review resolve; index → retrieve; tracking
signal → status. These are where the interesting bugs live, because each step is somebody
else's module and the seams between them are not exercised by any unit test.

**The fixtures.** A flow needs a row to act on, and for a long time this script did not
create one — so on an install with an empty feed it reported "80 passed, 5 skipped" and
every one of those five skips was the apply pipeline. It now seeds a synthetic account, one
posting and one status signal before the flows run and deletes them afterwards, so the
sequence the product exists for is exercised on every run. Two properties make that safe to
point at a database with real data in it: the flows act as the *synthetic* account, so they
never resolve a real review or dismiss a real signal; and everything created is deleted at
the end, with a non-zero exit if any of it survives. See "Synthetic fixtures" below.

Because there is no Celery worker in this configuration, ``POST /postings/{id}/apply`` only
enqueues. The apply flow therefore also runs :meth:`app.services.pipeline.Pipeline.run_one`
in this process, against the same database the backend is serving — which is the only way
the score → retrieve → tailor → render → guard-ladder sequence gets exercised at all.

Usage::

    python -m scripts.smoke_test                       # against http://127.0.0.1:8000
    python -m scripts.smoke_test --base-url http://localhost:9000
    python -m scripts.smoke_test --start               # start a backend, test it, stop it
    python -m scripts.smoke_test --skip-flows          # endpoints only
    python -m scripts.smoke_test --no-fixtures         # never write to the database

Exits ``0`` when everything passed and ``1`` on the first failure that matters, so it can be
the last line of a release script.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Final

# ======================================================================================
# Constants
# ======================================================================================

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
API_PREFIX = "/api/v1"
REQUEST_TIMEOUT_SECONDS = 30
STARTUP_TIMEOUT_SECONDS = 60
STARTUP_POLL_SECONDS = 0.5

#: Status codes that mean "the route exists and behaved sanely". A 404 on a collection
#: endpoint means it was never mounted, which is exactly what this script exists to catch;
#: a 404 on a ``{id}`` route with a random UUID is the correct answer.
OK_STATUSES = frozenset({200, 201, 202, 204})

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"


def _supports_colour() -> bool:
    """Whether to emit ANSI colour."""
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


COLOUR = _supports_colour()


def _paint(text: str, colour: str) -> str:
    """Colourise *text* when the terminal supports it."""
    return f"{colour}{text}{RESET}" if COLOUR else text


# ======================================================================================
# HTTP
# ======================================================================================


@dataclass(slots=True)
class Response:
    """One HTTP response, decoded as far as it can be."""

    status: int
    body: Any
    text: str
    error: str | None = None

    def json_path(self, *keys: Any) -> Any:
        """Walk *keys* into the decoded body, returning ``None`` if the path does not exist."""
        current = self.body
        for key in keys:
            indexable_dict = isinstance(current, dict) and key in current
            indexable_list = (
                isinstance(current, list) and isinstance(key, int) and len(current) > key
            )
            if not (indexable_dict or indexable_list):
                return None
            current = current[key]
        return current


class Client:
    """A minimal JSON HTTP client over :mod:`urllib`."""

    def __init__(self, base_url: str, *, user_id: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        params: dict[str, Any] | None = None,
    ) -> Response:
        """Perform one request and never raise for an HTTP status."""
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.user_id:
            headers["X-User-Id"] = self.user_id

        request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return Response(response.status, _decode(raw), raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            return Response(exc.code, _decode(raw), raw)
        except urllib.error.URLError as exc:
            return Response(0, None, "", error=f"{type(exc).__name__}: {exc.reason}")
        except Exception as exc:
            return Response(0, None, "", error=f"{type(exc).__name__}: {exc}")

    def get(self, path: str, **kwargs: Any) -> Response:
        """GET *path*."""
        return self.request("GET", path, **kwargs)

    def post(self, path: str, body: Any = None, **kwargs: Any) -> Response:
        """POST *path*."""
        return self.request("POST", path, body, **kwargs)


def _decode(raw: str) -> Any:
    """Decode a JSON body, returning the raw string when it is not JSON."""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


# ======================================================================================
# Results
# ======================================================================================


@dataclass(slots=True)
class Result:
    """One check and how it went."""

    area: str
    name: str
    passed: bool
    detail: str = ""
    skipped: bool = False


@dataclass(slots=True)
class Report:
    """Everything that was checked."""

    results: list[Result] = field(default_factory=list)

    def record(self, area: str, name: str, passed: bool, detail: str = "") -> Result:
        """Record one outcome."""
        outcome = Result(area, name, passed, detail)
        self.results.append(outcome)
        return outcome

    def skip(self, area: str, name: str, reason: str) -> None:
        """Record a check that could not run."""
        self.results.append(Result(area, name, True, reason, skipped=True))

    @property
    def failures(self) -> list[Result]:
        """Every failed check."""
        return [result for result in self.results if not result.passed]

    @property
    def passed(self) -> int:
        """How many checks passed outright."""
        return sum(1 for r in self.results if r.passed and not r.skipped)

    @property
    def skipped(self) -> int:
        """How many checks were skipped."""
        return sum(1 for r in self.results if r.skipped)

    def render(self) -> None:
        """Print the pass/fail table."""
        width = max((len(f"{r.area}/{r.name}") for r in self.results), default=20)
        current_area = None
        for result in self.results:
            if result.area != current_area:
                current_area = result.area
                print(f"\n{_paint(current_area.upper(), DIM)}")
            if result.skipped:
                mark = _paint("SKIP", YELLOW)
            elif result.passed:
                mark = _paint("PASS", GREEN)
            else:
                mark = _paint("FAIL", RED)
            label = f"{result.area}/{result.name}".ljust(width)
            detail = f"  {_paint(result.detail, DIM)}" if result.detail else ""
            print(f"  {mark}  {label}{detail}")

        print()
        total = len(self.results)
        summary = (
            f"{self.passed} passed, {len(self.failures)} failed, {self.skipped} skipped "
            f"({total} checks)"
        )
        print(_paint(summary, RED if self.failures else GREEN))


# ======================================================================================
# The endpoint table
# ======================================================================================

RANDOM_ID = str(uuid.uuid4())

#: ``(area, method, path, body, expected)``. ``expected`` is either an int, a set of ints, or
#: the sentinel ``"ok"`` meaning any of :data:`OK_STATUSES`.
ENDPOINTS: list[tuple[str, str, str, Any, Any]] = [
    # -- root ------------------------------------------------------------------------
    ("health", "GET", "/health", None, 200),
    ("health", "GET", "/ready", None, {200, 503}),
    ("health", "GET", "/metrics", None, 200),
    ("health", "GET", "/openapi.json", None, 200),
    # -- onboarding --------------------------------------------------------------------
    ("onboarding", "GET", f"{API_PREFIX}/onboarding/status", None, "ok"),
    ("onboarding", "GET", f"{API_PREFIX}/onboarding/steps", None, "ok"),
    # -- profile -----------------------------------------------------------------------
    ("profile", "GET", f"{API_PREFIX}/profile", None, "ok"),
    ("profile", "GET", f"{API_PREFIX}/profile/preferences", None, "ok"),
    # -- knowledge ---------------------------------------------------------------------
    ("knowledge", "GET", f"{API_PREFIX}/knowledge/sources", None, "ok"),
    ("knowledge", "GET", f"{API_PREFIX}/knowledge/facts", None, "ok"),
    ("knowledge", "GET", f"{API_PREFIX}/knowledge/entities", None, "ok"),
    ("knowledge", "GET", f"{API_PREFIX}/knowledge/graph", None, "ok"),
    ("knowledge", "GET", f"{API_PREFIX}/knowledge/stats", None, "ok"),
    ("knowledge", "DELETE", f"{API_PREFIX}/knowledge/sources/{RANDOM_ID}", None, 404),
    # -- postings ----------------------------------------------------------------------
    ("postings", "GET", f"{API_PREFIX}/postings", None, "ok"),
    ("postings", "GET", f"{API_PREFIX}/postings/{RANDOM_ID}", None, 404),
    # -- applications ------------------------------------------------------------------
    ("applications", "GET", f"{API_PREFIX}/applications", None, "ok"),
    ("applications", "GET", f"{API_PREFIX}/applications/{RANDOM_ID}", None, 404),
    ("applications", "GET", f"{API_PREFIX}/applications/{RANDOM_ID}/artifacts", None, 404),
    ("applications", "POST", f"{API_PREFIX}/applications/{RANDOM_ID}/retry", {}, 404),
    # -- reviews -----------------------------------------------------------------------
    ("reviews", "GET", f"{API_PREFIX}/reviews", None, "ok"),
    ("reviews", "POST", f"{API_PREFIX}/reviews/{RANDOM_ID}/resolve", {}, 404),
    ("reviews", "POST", f"{API_PREFIX}/reviews/{RANDOM_ID}/dismiss", {}, 404),
    # -- resumes -----------------------------------------------------------------------
    ("resumes", "GET", f"{API_PREFIX}/resumes", None, "ok"),
    ("resumes", "GET", f"{API_PREFIX}/resumes/versions/{RANDOM_ID}", None, 404),
    ("resumes", "GET", f"{API_PREFIX}/resumes/versions/{RANDOM_ID}/download", None, 404),
    # -- sessions ----------------------------------------------------------------------
    ("sessions", "GET", f"{API_PREFIX}/sessions", None, "ok"),
    ("sessions", "GET", f"{API_PREFIX}/sessions/{RANDOM_ID}", None, 404),
    # -- analytics ---------------------------------------------------------------------
    ("analytics", "GET", f"{API_PREFIX}/analytics/overview", None, "ok"),
    ("analytics", "GET", f"{API_PREFIX}/analytics/funnel", None, "ok"),
    ("analytics", "GET", f"{API_PREFIX}/analytics/timeseries", None, "ok"),
    ("analytics", "GET", f"{API_PREFIX}/analytics/insights", None, "ok"),
    # -- settings ----------------------------------------------------------------------
    ("settings", "GET", f"{API_PREFIX}/settings", None, "ok"),
    ("settings", "GET", f"{API_PREFIX}/settings/plugins", None, "ok"),
    ("settings", "GET", f"{API_PREFIX}/settings/scoring-rules", None, "ok"),
    # -- logs --------------------------------------------------------------------------
    ("logs", "GET", f"{API_PREFIX}/logs", None, "ok"),
    # -- tracking ----------------------------------------------------------------------
    ("tracking", "GET", f"{API_PREFIX}/tracking/accounts", None, "ok"),
    ("tracking", "GET", f"{API_PREFIX}/tracking/signals", None, "ok"),
    ("tracking", "POST", f"{API_PREFIX}/tracking/signals/{RANDOM_ID}/dismiss", {}, 404),
]


def _status_matches(status: int, expected: Any) -> bool:
    """Whether *status* satisfies *expected*."""
    if expected == "ok":
        return status in OK_STATUSES
    if isinstance(expected, (set, frozenset, tuple, list)):
        return status in expected
    return status == expected


def check_endpoints(client: Client, report: Report) -> None:
    """Walk :data:`ENDPOINTS` and record one result per row."""
    for area, method, path, body, expected in ENDPOINTS:
        response = client.request(method, path, body)
        name = f"{method} {path}"

        if response.error:
            report.record(area, name, False, response.error)
            continue

        ok = _status_matches(response.status, expected)
        detail = "" if ok else f"got {response.status}, expected {expected}"
        report.record(area, name, ok, detail)


def check_page_shape(client: Client, report: Report) -> None:
    """Every list endpoint must return ``{items, total, limit, offset}`` (§14)."""
    list_paths = [
        f"{API_PREFIX}/postings",
        f"{API_PREFIX}/applications",
        f"{API_PREFIX}/reviews",
        f"{API_PREFIX}/sessions",
        f"{API_PREFIX}/resumes",
        f"{API_PREFIX}/logs",
        f"{API_PREFIX}/knowledge/facts",
        f"{API_PREFIX}/tracking/signals",
    ]
    for path in list_paths:
        response = client.get(path)
        if response.status not in OK_STATUSES or not isinstance(response.body, dict):
            report.record("page", path, False, f"status {response.status}")
            continue
        missing = [key for key in ("items", "total", "limit", "offset") if key not in response.body]
        report.record("page", path, not missing, f"missing {missing}" if missing else "")


def check_settings_leaks_nothing(client: Client, report: Report) -> None:
    """``GET /settings`` must never return a credential — the one security check here."""
    response = client.get(f"{API_PREFIX}/settings")
    if response.status not in OK_STATUSES or not isinstance(response.body, dict):
        report.record("settings", "no credentials", False, f"status {response.status}")
        return

    banned_keys = [
        key
        for key in response.body
        if key.lower().endswith(("_key", "_token", "_secret", "_password", "_dsn"))
        and not key.endswith("_configured")
        and not isinstance(response.body[key], bool)
    ]
    report.record(
        "settings",
        "no credential fields",
        not banned_keys,
        f"exposed {banned_keys}" if banned_keys else "",
    )

    for forbidden in ("database_url", "redis_url", "secret_key"):
        report.record(
            "settings",
            f"{forbidden} absent",
            forbidden not in response.body,
            "" if forbidden not in response.body else "exposed",
        )

    allowed = response.body.get("is_submission_allowed")
    report.record(
        "settings",
        "is_submission_allowed present",
        isinstance(allowed, bool),
        "" if isinstance(allowed, bool) else "missing or not a bool",
    )


# ======================================================================================
# Synthetic fixtures
# ======================================================================================
#
# Every interesting flow below needs a row to act on: a posting to apply to, a parked
# application to resolve, a status signal to dismiss. Without them the script reported
# "80 passed, 5 skipped" and the five skips were the entire apply pipeline — the one path
# the product exists for went unexercised on every run.
#
# Two rules govern what this may do to a database it did not create:
#
# 1. **It never touches real data.** Rather than grabbing the first row the API lists —
#    which on a real install means dismissing somebody's actual signal and resolving their
#    actual review — it creates a *dedicated synthetic user* and drives every flow as that
#    user via ``X-User-Id``. The real account is only ever read from, by the endpoint table.
# 2. **It removes everything it created.** ``users.id`` cascades to every user-owned table,
#    so deleting the fixture user removes the application, its documents, its events, its
#    memories and its signals in one statement. Postings and companies are not user-owned
#    and are deleted explicitly by id, but only when *this run* inserted them.
#
# Idempotent for the same reason ``scripts/seed.py`` is: everything is looked up by its
# natural key first, so an interrupted run is repaired by the next one rather than
# duplicated. And nothing here can be mistaken for real data — the account is on the RFC
# 2606 ``.invalid`` TLD, the company is named "Smoke Test Fixtures", and both the posting and
# the signal carry ``smoke-test-`` identifiers and a ``smoke_test`` flag in their JSON.

#: Account every flow acts as. ``.invalid`` can never resolve, so this address cannot
#: collide with a real one and cannot receive mail.
FIXTURE_EMAIL: Final[str] = "smoke-test@applicantos.invalid"

#: Employer for the fixture posting. Named so that a human who sees it in the UI — because
#: a run was killed before cleanup — knows immediately what it is.
FIXTURE_COMPANY_NAME: Final[str] = "Smoke Test Fixtures, Inc."

#: ATS the fixture posting claims to come from. Greenhouse because it is one of the three
#: providers that *do* support automated submission, so the apply pipeline reaches the kill
#: switch rather than stopping earlier at the provider-posture guard.
FIXTURE_PROVIDER: Final[str] = "greenhouse"

#: Provider-native id of the fixture posting; half of its natural key.
FIXTURE_POSTING_EXTERNAL_ID: Final[str] = "smoke-test-fixture-posting"

#: Provider-native id of the fixture status signal.
FIXTURE_SIGNAL_EXTERNAL_REF: Final[str] = "smoke-test-fixture-signal"

#: Marker written into every fixture row's JSON column.
FIXTURE_MARKER_KEY: Final[str] = "smoke_test"

#: The answer posted to ``/reviews/{id}/resolve``. ``resolve`` rejects an empty answer set
#: (resolving nothing would re-queue a still-unanswerable application), so the flow has to
#: supply something, and it must be obviously not a real form answer.
FIXTURE_REVIEW_ANSWER: Final[dict[str, str]] = {"smoke_test_acknowledgement": "yes"}

#: Title and body of the fixture posting. Written to score *above* ``auto_apply_min_score``
#: against the persona ``scripts.seed`` installs, because a posting below the floor stops at
#: the score guard and the flow would never reach prepare, render or the kill switch.
FIXTURE_POSTING_TITLE: Final[str] = "Embedded Firmware Engineer, Robotics (smoke test)"
FIXTURE_POSTING_BODY: Final[str] = (
    "This posting exists only to exercise scripts/smoke_test.py end to end. It is not a "
    "real job and no application will ever be sent to it.\n\n"
    "Embedded firmware engineering for a mobile robotics platform: bare-metal C++17 on a "
    "microcontroller, a Zephyr RTOS application layer, and the real-time control loop that "
    "keeps the robot on its trajectory. Sensor fusion, CAN bus, and board bring-up "
    "alongside the electrical team."
)


@dataclass(slots=True)
class Fixtures:
    """Identifiers of the synthetic rows this run created, and whether it created them.

    Attributes:
        attempted: Whether this run tried to create fixtures at all. ``False`` under
            ``--no-fixtures``, and the only thing that stops cleanup from touching the
            database — everything else about cleanup is deliberately belt-and-braces.
        available: Whether the flows may use these. ``False`` means the fixture layer could
            not reach the database (a remote backend, or a missing driver), and the flows
            fall back to whatever the API lists.
        note: One line explaining :attr:`available`, printed above the results table.
        user_id: The synthetic account every flow acts as.
        posting_id: The synthetic posting the apply flow runs against.
        company_id: Its employer.
        signal_id: The synthetic status signal the tracking flow dismisses.
        application_id: The application the apply flow's pipeline run produced, set once
            that run has happened.
        created_company: Whether *this* run inserted the company, and may delete it.
        created_posting: Whether *this* run inserted the posting, and may delete it.
    """

    attempted: bool = False
    available: bool = False
    note: str = ""
    user_id: str | None = None
    posting_id: str | None = None
    company_id: str | None = None
    signal_id: str | None = None
    application_id: str | None = None
    created_company: bool = False
    created_posting: bool = False


async def _create_fixtures(fixtures: Fixtures) -> None:
    """Insert the synthetic account, posting and status signal into *fixtures*.

    The account is built by :func:`scripts.seed.seed` with ``postings=False``: it needs a
    real knowledge graph, because ``Pipeline.prepare`` refuses to generate a resume with
    nothing behind it (golden rule #7 — an empty resume is the honest output, and applying
    with one is worse than not applying), and ``postings=False`` because companies and
    postings are not user-owned and would therefore survive the cleanup that deletes the
    user.

    Populates *in place* rather than returning a new object, so that a failure halfway
    through still leaves the caller holding the ids of everything already written. A
    fixture layer that leaked rows when it broke would be worse than no fixture layer.

    Args:
        fixtures: The record to populate.
    """
    from datetime import timedelta

    from sqlalchemy import select

    from app.database.session import session_scope
    from app.database.types import utcnow
    from app.models.company import Company
    from app.models.enums import (
        ATSProviderName,
        EmploymentType,
        PostingStatus,
        SignalKind,
        SignalSource,
        WorkArrangement,
    )
    from app.models.posting import JobPosting
    from app.models.tracking import StatusSignal
    from app.models.user import User
    from scripts.seed import seed

    await seed(FIXTURE_EMAIL, postings=False)

    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.email == FIXTURE_EMAIL))
        if user is None:  # pragma: no cover - seed() has just written it
            raise LookupError(f"the fixture account {FIXTURE_EMAIL} was not created")
        fixtures.user_id = str(user.id)

        normalized = Company.normalize(FIXTURE_COMPANY_NAME)
        company = await session.scalar(
            select(Company).where(Company.normalized_name == normalized)
        )
        if company is None:
            company = Company(
                name=FIXTURE_COMPANY_NAME,
                domain="smoke-test.invalid",
                industry="Robotics",
                metadata_json={FIXTURE_MARKER_KEY: True},
            )
            session.add(company)
            await session.flush()
            fixtures.created_company = True
        fixtures.company_id = str(company.id)

        provider = ATSProviderName(FIXTURE_PROVIDER)
        posting = await session.scalar(
            select(JobPosting).where(
                JobPosting.provider == provider,
                JobPosting.external_id == FIXTURE_POSTING_EXTERNAL_ID,
            )
        )
        if posting is None:
            url = f"https://smoke-test.invalid/jobs/{FIXTURE_POSTING_EXTERNAL_ID}"
            posting = JobPosting(
                company_id=company.id,
                provider=provider,
                external_id=FIXTURE_POSTING_EXTERNAL_ID,
                url=url,
                apply_url=f"{url}/apply",
                title=FIXTURE_POSTING_TITLE,
                description=FIXTURE_POSTING_BODY,
                location="Remote — United States",
                work_arrangement=WorkArrangement.REMOTE,
                employment_type=EmploymentType.FULL_TIME,
                salary_min=130_000,
                salary_max=170_000,
                salary_currency="USD",
                posted_at=utcnow(),
                raw_json={FIXTURE_MARKER_KEY: True},
                status=PostingStatus.DISCOVERED,
            )
            session.add(posting)
            await session.flush()
            fixtures.created_posting = True
        fixtures.posting_id = str(posting.id)

        signal = await session.scalar(
            select(StatusSignal).where(
                StatusSignal.user_id == user.id,
                StatusSignal.external_ref == FIXTURE_SIGNAL_EXTERNAL_REF,
            )
        )
        if signal is None:
            signal = StatusSignal(
                user_id=user.id,
                source=SignalSource.MANUAL,
                kind=SignalKind.UNKNOWN,
                external_ref=FIXTURE_SIGNAL_EXTERNAL_REF,
                sender="no-reply@smoke-test.invalid",
                sender_domain="smoke-test.invalid",
                subject="Smoke test fixture — not a real message",
                snippet=(
                    "Synthetic status signal created by scripts/smoke_test.py so the "
                    "tracking flow has something to dismiss. Deleted at the end of the run."
                ),
                received_at=utcnow() - timedelta(hours=1),
                confidence=0.0,
                applied=False,
                needs_review=True,
                match_evidence={FIXTURE_MARKER_KEY: True},
            )
            session.add(signal)
            await session.flush()
        fixtures.signal_id = str(signal.id)

    fixtures.available = True
    fixtures.note = f"synthetic account {FIXTURE_EMAIL} + 1 posting + 1 status signal"


def ensure_fixtures(base_url: str) -> Fixtures:
    """Create the fixtures, and confirm the backend under test can see them.

    The confirmation matters: this process reaches the database directly, while every check
    below reaches it through the backend. If the two resolved different ``DATABASE_URL``\\ s
    — an already-running server started with a different environment, say — the fixtures
    would exist and every flow would still 404 on them. Verifying one round trip turns that
    into a clear message instead of four confusing failures.

    Args:
        base_url: The backend the flows will run against.

    Returns:
        Fixtures with :attr:`~Fixtures.available` set only when the round trip succeeded.
    """
    import asyncio

    fixtures = Fixtures(attempted=True)
    try:
        asyncio.run(_create_fixtures(fixtures))
    except Exception as exc:  # any failure here degrades the run, never aborts it
        fixtures.available = False
        fixtures.note = f"not seeded: {type(exc).__name__}: {exc}"
        return fixtures

    probe = Client(base_url, user_id=fixtures.user_id).get(
        f"{API_PREFIX}/postings/{fixtures.posting_id}"
    )
    if probe.status not in OK_STATUSES:
        fixtures.available = False
        fixtures.note = (
            f"seeded, but the backend cannot see them (GET /postings/{{id}} → "
            f"{probe.status}); is it running against a different database?"
        )
    return fixtures


async def _delete_fixtures(fixtures: Fixtures) -> None:
    """Delete every row the fixtures own, newest dependency first.

    Order is forced by the schema: ``applications.posting_id`` is ``ON DELETE RESTRICT``
    (deleting a posting that was applied to would erase the evidence behind golden rule #1),
    so the user — and therefore every application it owns — has to go first.

    Args:
        fixtures: What :func:`_create_fixtures` recorded.
    """
    import uuid as uuid_module

    from sqlalchemy import delete

    from app.database.session import session_scope
    from app.models.company import Company
    from app.models.posting import JobPosting
    from app.models.user import User

    async with session_scope() as session:
        # By id when it was recorded, by email otherwise: `_create_fixtures` can fail
        # between `seed()` writing the account and the id reaching the caller, and an
        # orphaned synthetic user is precisely what this must never leave behind.
        if fixtures.user_id:
            await session.execute(
                delete(User).where(User.id == uuid_module.UUID(fixtures.user_id))
            )
        else:
            await session.execute(delete(User).where(User.email == FIXTURE_EMAIL))
        if fixtures.created_posting and fixtures.posting_id:
            await session.execute(
                delete(JobPosting).where(JobPosting.id == uuid_module.UUID(fixtures.posting_id))
            )
        if fixtures.created_company and fixtures.company_id:
            await session.execute(
                delete(Company).where(Company.id == uuid_module.UUID(fixtures.company_id))
            )


def remove_fixtures(fixtures: Fixtures) -> str | None:
    """Delete the fixtures and the documents the pipeline rendered for them.

    Args:
        fixtures: What :func:`ensure_fixtures` returned.

    Returns:
        ``None`` when everything was removed, or a message describing what was left behind —
        which the caller turns into a non-zero exit, because synthetic rows surviving in
        somebody's database is a failure of this script even when every check passed.
    """
    import asyncio

    if not fixtures.attempted:
        return None

    async def _run() -> None:
        if fixtures.application_id:
            # Golden rule #6: the rendered PDF is disposable and must not be left on disk.
            # Best effort — a missing render directory is not a reason to skip the delete.
            from app.config.settings import get_settings
            from app.database.session import session_scope
            from app.services.pipeline import Pipeline

            try:
                async with session_scope() as session:
                    await Pipeline(session, get_settings()).cleanup_application(
                        fixtures.application_id
                    )
            except Exception as exc:  # reported, never fatal
                print(f"  fixture documents were not cleaned up: {type(exc).__name__}: {exc}")
        await _delete_fixtures(fixtures)

    try:
        asyncio.run(_run())
    except Exception as exc:  # reported through the return value
        return (
            f"{type(exc).__name__}: {exc} — synthetic rows may remain "
            f"(account {FIXTURE_EMAIL}, posting {FIXTURE_POSTING_EXTERNAL_ID})"
        )
    return None


async def _run_apply_pipeline(fixtures: Fixtures) -> dict[str, Any]:
    """Run ``Pipeline.run_one`` against the fixture posting, in this process.

    This is the part no HTTP call can cover. ``POST /postings/{id}/apply`` only *enqueues*
    ``apply.run_one``, and the smoke test runs with no Celery worker, so without this the
    apply flow would prove nothing beyond "the route is mounted". Running the pipeline here
    exercises the real sequence — score, retrieve, tailor, render, then the full guard
    ladder — against the real database the backend is serving.

    Args:
        fixtures: The synthetic account and posting to run against.

    Returns:
        :meth:`~app.services.pipeline.PipelineResult.as_dict` of the outcome.
    """
    from app.config.settings import get_settings
    from app.database.session import session_scope
    from app.services.pipeline import Pipeline

    async with session_scope() as session:
        pipeline = Pipeline(session, get_settings())
        result = await pipeline.run_one(fixtures.posting_id, fixtures.user_id)
    return result.as_dict()


def run_apply_pipeline(fixtures: Fixtures) -> dict[str, Any]:
    """Synchronous wrapper around :func:`_run_apply_pipeline`.

    Args:
        fixtures: The synthetic account and posting to run against.

    Returns:
        The outcome mapping, or ``{"error": ...}`` when the run raised — a raised exception
        out of the pipeline is itself the finding, so it is reported rather than propagated.
    """
    import asyncio

    try:
        return asyncio.run(_run_apply_pipeline(fixtures))
    except Exception as exc:  # a raised exception out of the pipeline *is* the result
        return {"error": f"{type(exc).__name__}: {exc}"}


# ======================================================================================
# Cross-service flows
# ======================================================================================


def flow_discover_score_prepare_submit(
    client: Client, report: Report, fixtures: Fixtures
) -> None:
    """discover → score → prepare → dry-run submit.

    The whole product in one sequence. Every step is a different module, and the seams
    between them are what no unit test covers. The submit step must **not** submit: both
    switches default closed, so the correct outcome is a refusal, and a smoke test that
    reported success here would be reporting a safety failure.

    The third check runs the pipeline in this process as well as through the API, because
    ``POST /postings/{id}/apply`` only enqueues and there is no worker consuming the queue.
    See :func:`run_apply_pipeline`.

    Args:
        client: HTTP client bound to the fixture account.
        report: Where results are recorded.
        fixtures: The synthetic rows this run may act on.
    """
    area = "flow:apply"

    discovered = client.post(f"{API_PREFIX}/postings/discover", {"providers": ["greenhouse"]})
    if discovered.status not in OK_STATUSES:
        report.record(area, "discover", False, f"status {discovered.status}")
        return
    report.record(area, "discover", True, "")

    posting_id = fixtures.posting_id
    if posting_id:
        # A round trip through the API, not a database read: this proves the backend under
        # test is serving the same database the fixture was written to.
        visible = client.get(f"{API_PREFIX}/postings/{posting_id}")
        report.record(
            area,
            "posting available",
            visible.status in OK_STATUSES,
            "" if visible.status in OK_STATUSES else f"status {visible.status}",
        )
    else:
        listing = client.get(f"{API_PREFIX}/postings", params={"limit": 1})
        items = listing.json_path("items") or []
        posting_id = items[0].get("id") if items else None
        if not posting_id:
            report.skip(area, "posting available", "no postings and no fixture")
            report.skip(area, "prepare + dry-run submit", "no postings available")
            report.skip(area, "did NOT submit with switches closed", "no postings available")
            return
        report.record(area, "posting available", True, "using an existing posting")

    applied = client.post(f"{API_PREFIX}/postings/{posting_id}/apply", {})
    # Any non-5xx answer is fine: with the kill switch closed, "refused" is the right answer.
    queued = applied.status < 500

    if not fixtures.available:
        report.record(area, "prepare + dry-run submit", queued, f"status {applied.status}")
        status = applied.json_path("status") or applied.json_path("application", "status")
        report.record(
            area,
            "did NOT submit with switches closed",
            status not in ("submitted", "confirmed"),
            f"status={status}",
        )
        return

    outcome = run_apply_pipeline(fixtures)
    fixtures.application_id = outcome.get("application_id")
    error = outcome.get("error")
    verdict = outcome.get("verdict")
    # `skipped` means the score guard or a terminal state stopped it before any work
    # happened, which on the fixture posting means the pipeline is not doing what it claims.
    ran = queued and not error and verdict in ("needs_review", "blocked", "submitted", "failed")
    report.record(
        area,
        "prepare + dry-run submit",
        ran,
        error or f"apply {applied.status}; pipeline {verdict}/{outcome.get('stage')}",
    )

    submitted = bool(outcome.get("submitted")) or outcome.get("status") in (
        "submitted",
        "confirmed",
    )
    report.record(
        area,
        "did NOT submit with switches closed",
        not submitted,
        f"status={outcome.get('status')}, reason={outcome.get('review_reason')}",
    )


def flow_review_resolve(client: Client, report: Report, fixtures: Fixtures) -> None:
    """The review queue answers, resolving works, and an unknown id is a clean 404.

    The item resolved is the one the apply flow's pipeline run just parked — the kill switch
    is closed, so ``Pipeline.submit`` stopped at guard 5 and asked for a human. Resolving
    *that* row rather than whatever the queue happens to list first is what keeps this
    script from acting on a real person's review.

    Args:
        client: HTTP client bound to the fixture account.
        report: Where results are recorded.
        fixtures: The synthetic rows this run may act on.
    """
    area = "flow:review"

    listing = client.get(f"{API_PREFIX}/reviews")
    report.record(area, "queue reachable", listing.status in OK_STATUSES, f"{listing.status}")

    application_id = fixtures.application_id
    if application_id is None:
        items = listing.json_path("items") or []
        item = items[0] if items else None
        application_id = ((item or {}).get("application") or {}).get("id") if item else None

    if application_id is None:
        report.skip(area, "resolve", "review queue is empty and no fixture was parked")
    else:
        resolved = client.post(
            f"{API_PREFIX}/reviews/{application_id}/resolve",
            {"answers": FIXTURE_REVIEW_ANSWER},
        )
        report.record(
            area,
            "resolve",
            resolved.status in OK_STATUSES,
            f"status {resolved.status}",
        )

    missing = client.post(f"{API_PREFIX}/reviews/{RANDOM_ID}/resolve", {})
    report.record(area, "unknown id is 404", missing.status == 404, f"status {missing.status}")


def flow_index_retrieve(client: Client, report: Report) -> None:
    """index → retrieve. A knowledge base that indexes but cannot be searched is inert."""
    area = "flow:knowledge"

    stats = client.get(f"{API_PREFIX}/knowledge/stats")
    report.record(area, "stats", stats.status in OK_STATUSES, f"{stats.status}")

    reindex = client.post(f"{API_PREFIX}/knowledge/reindex", {})
    report.record(area, "reindex accepted", reindex.status < 500, f"status {reindex.status}")

    search = client.get(f"{API_PREFIX}/knowledge/search", params={"q": "python"})
    report.record(area, "search", search.status < 500, f"status {search.status}")

    graph = client.get(f"{API_PREFIX}/knowledge/graph")
    report.record(area, "graph", graph.status in OK_STATUSES, f"{graph.status}")


def flow_tracking_signal_to_status(client: Client, report: Report, fixtures: Fixtures) -> None:
    """tracking signal → status. The loop that closes an application's outcome.

    Dismisses the synthetic signal rather than the newest real one: a dismissed signal is
    how the sync stops asking the same question, and dismissing somebody's genuine mail on
    their behalf would silently drop a status update.

    Args:
        client: HTTP client bound to the fixture account.
        report: Where results are recorded.
        fixtures: The synthetic rows this run may act on.
    """
    area = "flow:tracking"

    accounts = client.get(f"{API_PREFIX}/tracking/accounts")
    report.record(area, "accounts", accounts.status in OK_STATUSES, f"{accounts.status}")

    signals = client.get(f"{API_PREFIX}/tracking/signals")
    report.record(area, "signals", signals.status in OK_STATUSES, f"{signals.status}")

    sync = client.post(f"{API_PREFIX}/tracking/sync", {})
    # With no mailbox connected this must be a clean no-op, never a 500.
    report.record(area, "sync is a clean no-op", sync.status < 500, f"status {sync.status}")

    signal_id = fixtures.signal_id
    if signal_id is None:
        items = signals.json_path("items") or []
        signal_id = items[0].get("id") if items else None
    if signal_id is None:
        report.skip(area, "dismiss signal", "no signals present and no fixture")
        return

    dismissed = client.post(f"{API_PREFIX}/tracking/signals/{signal_id}/dismiss", {})
    report.record(
        area,
        "dismiss signal",
        dismissed.status in OK_STATUSES,
        f"status {dismissed.status}",
    )


def flow_worker_queues(report: Report) -> None:
    """Every Celery task is registered on the queue §15 assigns it.

    A task on the wrong queue is invisible to ``pytest`` and fatal in production: the worker
    consuming ``apply`` never sees a task routed to ``discovery``, so applications simply
    stop happening with no error anywhere. Imported rather than fetched, so this is skipped
    when the script runs against a remote backend.
    """
    area = "flow:workers"

    expected: dict[str, str] = {
        "jobs.poll_all": "discovery",
        "jobs.poll_provider": "discovery",
        "jobs.score_posting": "ai",
        "apply.prepare": "ai",
        "apply.submit": "apply",
        "apply.run_one": "apply",
        "knowledge.index_source": "knowledge",
        "knowledge.index_all": "knowledge",
        "knowledge.refresh_stale": "knowledge",
        "cleanup.temp_documents": "maintenance",
        "cleanup.expire_postings": "maintenance",
        "session.watchdog": "maintenance",
        "sync.poll_all": "maintenance",
    }

    try:
        import importlib

        from app.workers.celery_app import TASK_MODULES, celery_app
    except Exception as exc:
        report.skip(area, "queue routing", f"celery app unavailable: {type(exc).__name__}")
        return

    # Celery imports its ``include=`` modules when a *worker* boots, not when the app object
    # is constructed, so ``celery_app.tasks`` is empty in a plain client process. Importing
    # them here is exactly what the worker does — and is itself a check: a task module
    # missing from TASK_MODULES means a queue nobody consumes and a task name that resolves
    # to nothing, which stays invisible until a scheduled job silently never runs.
    for module in TASK_MODULES:
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as exc:
            # A database driver that is not installed in *this* process is an environment
            # fact, not a wiring bug — the worker that will really run these tasks has it.
            # Reporting it as a failure would train everyone to ignore this section.
            report.skip(area, f"import {module}", f"optional dependency missing: {exc.name}")
        except Exception as exc:
            report.record(area, f"import {module}", False, f"{type(exc).__name__}: {exc}")
        else:
            report.record(area, f"import {module}", True, "")

    if not set(expected) & set(celery_app.tasks):
        report.skip(area, "queue routing", "no task modules could be imported in this process")
        return

    routes = celery_app.conf.task_routes or {}
    registered = set(celery_app.tasks)

    for name, queue in expected.items():
        if name not in registered:
            report.record(area, name, False, "task is not registered")
            continue
        route = routes.get(name) if isinstance(routes, dict) else None
        actual = (route or {}).get("queue") if isinstance(route, dict) else None
        if actual is None:
            report.skip(area, name, "no explicit route (uses the default queue)")
            continue
        report.record(area, name, actual == queue, "" if actual == queue else f"on {actual!r}")


# ======================================================================================
# Backend lifecycle
# ======================================================================================


def wait_for_health(client: Client, timeout: float = STARTUP_TIMEOUT_SECONDS) -> bool:
    """Poll ``/health`` until it answers or *timeout* elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get("/health").status == 200:
            return True
        time.sleep(STARTUP_POLL_SECONDS)
    return False


#: The configuration ``--start`` runs under: no Postgres, no Redis, no API keys, and both
#: safety switches closed. Identical to the "zero-dependency mode" in ``CLAUDE.md``.
ZERO_DEPENDENCY_ENV: dict[str, str] = {
    "SQLITE_MODE": "true",
    "LLM_PROVIDER": "null",
    "EMBEDDING_PROVIDER": "hashing",
    "VECTOR_STORE": "memory",
    "CACHE_BACKEND": "memory",
    "AUTO_APPLY_ENABLED": "false",
    "DRY_RUN": "true",
    "LOG_LEVEL": "WARNING",
}


def start_backend(port: int) -> subprocess.Popen[bytes]:
    """Launch a zero-dependency backend for the duration of the run."""
    environment = dict(os.environ)
    environment.update(ZERO_DEPENDENCY_ENV)
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ======================================================================================
# Entry point
# ======================================================================================


def main(argv: list[str] | None = None) -> int:
    """Run the smoke test and return a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="backend to test")
    parser.add_argument("--user-id", default=None, help="value for the X-User-Id header")
    parser.add_argument("--start", action="store_true", help="start a backend for the run")
    parser.add_argument("--port", type=int, default=8123, help="port to use with --start")
    parser.add_argument("--skip-flows", action="store_true", help="endpoint table only")
    parser.add_argument("--skip-endpoints", action="store_true", help="flows only")
    parser.add_argument(
        "--no-fixtures",
        dest="fixtures",
        action="store_false",
        help=(
            "do not create the synthetic account, posting and signal; flows then act on "
            "whatever the API already lists, and skip when it lists nothing"
        ),
    )
    args = parser.parse_args(argv)

    process: subprocess.Popen[bytes] | None = None
    base_url = args.base_url
    if args.start:
        base_url = f"http://127.0.0.1:{args.port}"
        # Apply the zero-dependency configuration to *this* process too, before anything
        # imports `app`: the worker-queue check imports the task modules, which build a
        # database engine from `settings.database_url` at import time and would otherwise
        # demand a PostgreSQL driver this install may not have.
        os.environ.update(ZERO_DEPENDENCY_ENV)
        print(f"starting a backend on {base_url} ...")
        process = start_backend(args.port)

    client = Client(base_url, user_id=args.user_id)
    report = Report()
    fixtures = Fixtures(note="disabled with --no-fixtures")
    leftover: str | None = None

    try:
        if not wait_for_health(client, STARTUP_TIMEOUT_SECONDS if args.start else 5):
            print(_paint(f"backend at {base_url} is not answering /health", RED))
            print("start one with:  python -m scripts.smoke_test --start")
            return 1

        if not args.skip_endpoints:
            check_endpoints(client, report)
            check_page_shape(client, report)
            check_settings_leaks_nothing(client, report)

        if not args.skip_flows:
            if args.fixtures:
                fixtures = ensure_fixtures(base_url)
                print(f"fixtures: {fixtures.note}")
            # Flows act as the synthetic user so that nothing they do — resolving a review,
            # dismissing a signal, re-indexing a knowledge source — can touch a real
            # account. `--user-id` still wins when it was given explicitly.
            flow_client = Client(base_url, user_id=args.user_id or fixtures.user_id)
            flow_discover_score_prepare_submit(flow_client, report, fixtures)
            flow_review_resolve(flow_client, report, fixtures)
            flow_index_retrieve(flow_client, report)
            flow_tracking_signal_to_status(flow_client, report, fixtures)
            flow_worker_queues(report)
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                process.kill()
        # After the backend is down: it holds the SQLite file, and on Windows a live
        # connection turns a delete into a lock error rather than a clean statement.
        leftover = remove_fixtures(fixtures)

    report.render()

    if leftover is not None:
        print()
        print(_paint(f"fixture cleanup failed: {leftover}", RED))

    if report.failures:
        print()
        print(_paint(f"{len(report.failures)} check(s) failed:", RED))
        for failure in report.failures:
            print(f"  - {failure.area}/{failure.name}: {failure.detail}")
        return 1
    return 1 if leftover is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
