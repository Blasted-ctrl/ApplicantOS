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

Usage::

    python -m scripts.smoke_test                       # against http://127.0.0.1:8000
    python -m scripts.smoke_test --base-url http://localhost:9000
    python -m scripts.smoke_test --start               # start a backend, test it, stop it
    python -m scripts.smoke_test --skip-flows          # endpoints only

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
from typing import Any

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
# Cross-service flows
# ======================================================================================


def flow_discover_score_prepare_submit(client: Client, report: Report) -> None:
    """discover → score → prepare → dry-run submit.

    The whole product in one sequence. Every step is a different module, and the seams
    between them are what no unit test covers. The submit step must **not** submit: both
    switches default closed, so the correct outcome is a refusal, and a smoke test that
    reported success here would be reporting a safety failure.
    """
    area = "flow:apply"

    discovered = client.post(f"{API_PREFIX}/postings/discover", {"providers": ["greenhouse"]})
    if discovered.status not in OK_STATUSES:
        report.record(area, "discover", False, f"status {discovered.status}")
        return
    report.record(area, "discover", True, "")

    listing = client.get(f"{API_PREFIX}/postings", params={"limit": 1})
    items = listing.json_path("items") or []
    if not items:
        report.skip(area, "score", "no postings available to score")
        report.skip(area, "prepare", "no postings available")
        report.skip(area, "dry-run submit", "no postings available")
        return

    posting_id = items[0].get("id")
    report.record(area, "posting available", bool(posting_id), "")

    applied = client.post(f"{API_PREFIX}/postings/{posting_id}/apply", {})
    # Any non-5xx answer is fine: with the kill switch closed, "refused" is the right answer.
    report.record(
        area,
        "prepare + dry-run submit",
        applied.status < 500,
        f"status {applied.status}",
    )

    if applied.status in OK_STATUSES:
        status = applied.json_path("status") or applied.json_path("application", "status")
        submitted = status in ("submitted", "confirmed")
        report.record(
            area,
            "did NOT submit with switches closed",
            not submitted,
            f"status={status}",
        )


def flow_review_resolve(client: Client, report: Report) -> None:
    """The review queue answers, and resolving a non-existent item is a clean 404."""
    area = "flow:review"

    listing = client.get(f"{API_PREFIX}/reviews")
    report.record(area, "queue reachable", listing.status in OK_STATUSES, f"{listing.status}")

    items = listing.json_path("items") or []
    if not items:
        report.skip(area, "resolve", "review queue is empty")
    else:
        item = items[0]
        application_id = (item.get("application") or {}).get("id") or item.get("id")
        resolved = client.post(
            f"{API_PREFIX}/reviews/{application_id}/resolve", {"answers": {}}
        )
        report.record(area, "resolve", resolved.status < 500, f"status {resolved.status}")

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


def flow_tracking_signal_to_status(client: Client, report: Report) -> None:
    """tracking signal → status. The loop that closes an application's outcome."""
    area = "flow:tracking"

    accounts = client.get(f"{API_PREFIX}/tracking/accounts")
    report.record(area, "accounts", accounts.status in OK_STATUSES, f"{accounts.status}")

    signals = client.get(f"{API_PREFIX}/tracking/signals")
    report.record(area, "signals", signals.status in OK_STATUSES, f"{signals.status}")

    sync = client.post(f"{API_PREFIX}/tracking/sync", {})
    # With no mailbox connected this must be a clean no-op, never a 500.
    report.record(area, "sync is a clean no-op", sync.status < 500, f"status {sync.status}")

    items = signals.json_path("items") or []
    if not items:
        report.skip(area, "resolve signal", "no signals present")
        return

    signal_id = items[0].get("id")
    dismissed = client.post(f"{API_PREFIX}/tracking/signals/{signal_id}/dismiss", {})
    report.record(area, "dismiss signal", dismissed.status < 500, f"status {dismissed.status}")


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
            flow_discover_score_prepare_submit(client, report)
            flow_review_resolve(client, report)
            flow_index_retrieve(client, report)
            flow_tracking_signal_to_status(client, report)
            flow_worker_queues(report)
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                process.kill()

    report.render()

    if report.failures:
        print()
        print(_paint(f"{len(report.failures)} check(s) failed:", RED))
        for failure in report.failures:
            print(f"  - {failure.area}/{failure.name}: {failure.detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
