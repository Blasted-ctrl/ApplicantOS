"""Background work runs *somewhere*, exactly once, and says where — the inline executor.

This module guards the fix for a silent, total outage. ``app.api.tasks.dispatch`` used to
report success whenever a broker **accepted** a publish, and ``redis_url`` defaults to
``redis://localhost:6379/0`` — the default port for every Redis on the machine. On an install
pointed at an unrelated project's container every task was published, acknowledged, and never
executed: sessions sat at ``running`` forever, knowledge sources sat at ``pending``, and the
API reported ``degraded: false`` throughout. "The broker took it" and "something will run it"
are different questions, and only the second one is worth answering.

Three properties are load-bearing, and every test here exists to hold one of them:

**Exactly one executor.** A task goes to a Celery worker *or* to this process, never both.
Two executions of ``apply.submit`` is an application sent twice — golden rule #1 — so the
routing decision is made once, strictly *before* any publish, and never revisited afterwards.
The assertions are therefore against what the *other* executor was asked to do, not merely
against the returned ``mode``: a ``Dispatch(mode="worker")`` that also queued the job inline
is the exact bug, and a test that only reads the return value passes against it.

**Never silently inline.** ``task_execution="worker"`` is the deployed posture and it must
degrade loudly rather than quietly adopting the work, because a deployment that runs its
apply queue inside the web process is not the deployment that was configured.

**Nothing vanishes.** A saturated inline queue is ``mode="none"`` with a reason a user can
read, not a dropped job — the drop is precisely what this whole subsystem was written to end.

No test here needs a broker. The Celery client is a recording double injected over
``app.api.tasks._celery_client``, so ``worker_serves`` runs for real against a fake control
plane rather than being stubbed out — the liveness probe is the thing that was missing, so
stubbing it would leave the interesting half untested.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, Final

import pytest

from app.api.tasks import (
    DEGRADED_KEY,
    QUEUE_APPLY,
    QUEUE_DISCOVERY,
    QUEUE_KNOWLEDGE,
    TASK_APPLY_SUBMIT,
    TASK_JOBS_POLL_ALL,
    TASK_KNOWLEDGE_INDEX_ALL,
    Dispatch,
    dispatch,
    reset_dispatcher,
    reset_worker_probe,
    worker_serves,
)
from app.config.settings import Settings

#: How long a test waits for a daemon drain thread to pick up a job. Generous enough not to
#: flake on a loaded CI box, short enough that a genuinely dead pool fails the run quickly.
_THREAD_WAIT_SECONDS: Final[float] = 5.0


# ======================================================================================
# Doubles
# ======================================================================================


class FakeCeleryClient:
    """A Celery application stand-in that records publishes and answers liveness probes.

    Records rather than merely answers, for the same reason :class:`tests.fakes.FakePage`
    does: ``dispatch`` returning ``mode="inline"`` while *also* having published to a broker
    would satisfy any assertion made against the return value alone, and would be two
    executions of one task.

    Args:
        served: Queue names a live worker reports consuming. Empty means nobody is listening.
        send_error: Raised by :meth:`send_task` instead of publishing, to model an
            unreachable broker.
        send_delay: Seconds :meth:`send_task` blocks, to model a broker that hangs.
        inspect_error: Raised by the control plane, to model a probe that cannot complete.
    """

    def __init__(
        self,
        served: set[str] | None = None,
        *,
        send_error: Exception | None = None,
        send_delay: float = 0.0,
        inspect_error: Exception | None = None,
    ) -> None:
        """Build the double."""
        self.served: set[str] = served or set()
        self.send_error = send_error
        self.send_delay = send_delay
        self.inspect_error = inspect_error
        #: Every publish, as ``(task, args, kwargs, queue)``.
        self.sent: list[tuple[str, list[Any], dict[str, Any], str | None]] = []
        #: How many times the workers were asked what they consume.
        self.probes = 0

    # -- liveness ----------------------------------------------------------------------

    @property
    def control(self) -> Any:
        """The ``control`` namespace ``_inspect_queues`` reaches through."""
        return SimpleNamespace(inspect=self._inspect)

    def _inspect(self, timeout: float | None = None) -> Any:
        """Return an inspector reporting :attr:`served` for one fictional worker."""
        self.probes += 1
        if self.inspect_error is not None:
            raise self.inspect_error
        replies = (
            {"celery@fake": [{"name": name} for name in sorted(self.served)]}
            if self.served
            else {}
        )
        return SimpleNamespace(active_queues=lambda: replies)

    # -- publishing --------------------------------------------------------------------

    def send_task(
        self,
        name: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        queue: str | None = None,
    ) -> Any:
        """Record one publish and hand back an object carrying an id."""
        if self.send_delay:
            time.sleep(self.send_delay)
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((name, args or [], kwargs or {}, queue))
        return SimpleNamespace(id=f"celery-{len(self.sent)}")


class RecordingInline:
    """Stands in for :func:`app.workers.inline.submit_inline` and remembers every call.

    ``accept=False`` models a saturated pool, which is the one refusal the caller must
    surface rather than swallow.
    """

    def __init__(self, *, accept: bool = True) -> None:
        """Build the recorder."""
        self.accept = accept
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, task: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        """Record the submission and report whether it was queued."""
        self.calls.append((task, args, kwargs))
        return self.accept


# ======================================================================================
# Fixtures
# ======================================================================================


@pytest.fixture(autouse=True)
def _clean_dispatch_state() -> Iterator[None]:
    """Reset every process-wide cache this subsystem keeps, on both sides of each test.

    Three of them are global by design and all three leak across tests otherwise: the
    memoised Celery client, the liveness cache (which holds an answer for
    ``WORKER_PROBE_TTL_SECONDS`` and would let one test's "no worker" decide the next test's
    routing), and the inline executor's daemon threads. Cleaning up *before* as well as after
    means a failure in one test cannot cascade into a false failure in the next.
    """
    from app.workers.inline import reset_executor_state, shutdown_executor

    def _reset() -> None:
        reset_dispatcher()
        reset_worker_probe()
        shutdown_executor()
        reset_executor_state()

    _reset()
    yield
    _reset()


@pytest.fixture
def inline_calls(monkeypatch: pytest.MonkeyPatch) -> RecordingInline:
    """Replace the in-process executor's entry point with a recorder.

    ``_run_here`` imports ``submit_inline`` from the module inside the call, so patching the
    module attribute is what an inline dispatch will actually reach — and an empty
    ``calls`` list is the proof that a worker-routed task was *not* also run here.
    """
    recorder = RecordingInline()
    monkeypatch.setattr("app.workers.inline.submit_inline", recorder)
    return recorder


@pytest.fixture
def registered_tasks(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """Register two real Celery tasks — one that succeeds, one that raises.

    Real ``@celery_app.task`` registrations rather than duck types, because
    :meth:`InlineExecutor._execute` runs work through Celery's own ``apply()``, and the thing
    under test is that the executor survives whatever ``apply()`` reports back.

    The names carry a per-test suffix and are unregistered afterwards. Celery's registry is
    keyed by name and ``@celery_app.task`` hands back the *existing* task when one is already
    registered, so a fixed name would silently give the second test the first test's closure —
    its recorder would stay empty while the task ran perfectly, which looks like a dead pool.

    ``_registry_ready`` is pre-set so ``_resolve`` skips ``import_default_modules()``: these
    tasks are already registered, and importing the whole pipeline would cost seconds for no
    added coverage.
    """
    import uuid

    from app.workers.celery_app import celery_app

    marker = uuid.uuid4().hex[:8]
    ok_name = f"tests.inline.ok-{marker}"
    boom_name = f"tests.inline.boom-{marker}"
    ran: list[str] = []
    finished = threading.Event()

    @celery_app.task(name=boom_name)
    def _boom() -> None:
        ran.append("boom")
        raise RuntimeError("this task fails on purpose")

    @celery_app.task(name=ok_name)
    def _ok(label: str = "ok") -> str:
        ran.append(label)
        finished.set()
        return label

    ready = threading.Event()
    ready.set()
    monkeypatch.setattr("app.workers.inline._registry_ready", ready)

    yield SimpleNamespace(ok=ok_name, boom=boom_name, ran=ran, finished=finished)

    for name in (ok_name, boom_name):
        celery_app.tasks.pop(name, None)


# ======================================================================================
# 1 — no worker on the queue means the work runs here, and that is not degraded
# ======================================================================================


async def test_dispatch_runs_inline_when_no_worker_consumes_the_queue(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    inline_calls: RecordingInline,
) -> None:
    """A reachable broker with nobody listening must route inline, not publish into the void.

    This is the outage, in one test. The broker here happily accepts publishes — it is a
    perfectly healthy Redis — but no worker consumes ``discovery``. The old code published,
    got an id, and reported success while the task was never executed by anybody. The
    assertion that matters is ``client.sent == []``: the message must not be on the wire at
    all, because a task sitting in a queue nobody reads is indistinguishable from lost work.
    """
    monkeypatch.setattr(settings, "task_execution", "auto")
    client = FakeCeleryClient(served=set())
    monkeypatch.setattr("app.api.tasks._celery_client", lambda: client)

    result = await dispatch(TASK_JOBS_POLL_ALL)

    assert result.mode == "inline"
    assert result.degraded is False, "work that is running must never be reported as degraded"
    assert result.dispatched is True
    assert result.reason is None
    assert inline_calls.calls == [(TASK_JOBS_POLL_ALL, (), {})]
    assert client.sent == [], "published to a broker nobody is consuming"


async def test_dispatch_runs_inline_when_celery_is_not_installed(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    inline_calls: RecordingInline,
) -> None:
    """No Celery at all is still a working desktop app under ``auto``.

    ``CLAUDE.md`` promises the whole pipeline runs with no broker. A missing optional
    dependency is a routing input, not an error.
    """
    monkeypatch.setattr(settings, "task_execution", "auto")
    monkeypatch.setattr("app.api.tasks._celery_client", lambda: None)

    result = await dispatch(TASK_KNOWLEDGE_INDEX_ALL, "source-1", full=True)

    assert result.mode == "inline"
    assert result.degraded is False
    assert result.queue == QUEUE_KNOWLEDGE
    assert inline_calls.calls == [(TASK_KNOWLEDGE_INDEX_ALL, ("source-1",), {"full": True})]


# ======================================================================================
# 2 — a live worker gets the task, and the inline pool is never asked
# ======================================================================================


async def test_dispatch_routes_to_worker_and_never_also_runs_inline(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    inline_calls: RecordingInline,
) -> None:
    """The both-executors bug: golden rule #1 dies if one task runs in two places.

    ``apply.submit`` executed by a Celery worker *and* by the API process is an application
    submitted twice, which the ``UNIQUE(user_id, posting_id)`` constraint would turn into a
    crashed run rather than a clean refusal. So the test is not "did it say worker" — it is
    ``inline_calls.calls == []``, the assertion that the other executor was never even asked.

    The liveness probe runs for real here against the fake control plane, so this also pins
    the shape ``_inspect_queues`` parses out of ``active_queues()``.
    """
    monkeypatch.setattr(settings, "task_execution", "auto")
    client = FakeCeleryClient(served={QUEUE_APPLY})
    monkeypatch.setattr("app.api.tasks._celery_client", lambda: client)

    result = await dispatch(TASK_APPLY_SUBMIT, "application-id")

    assert result.mode == "worker"
    assert result.degraded is False
    assert result.task_id == "celery-1"
    assert client.sent == [(TASK_APPLY_SUBMIT, ["application-id"], {}, QUEUE_APPLY)]
    assert inline_calls.calls == [], "the task was handed to a worker *and* run in-process"


async def test_a_worker_on_another_queue_does_not_claim_this_one(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    inline_calls: RecordingInline,
) -> None:
    """Liveness is per queue, because deployments run one worker per queue.

    A running ``discovery`` worker says nothing about whether anything consumes ``apply``.
    Treating "some worker answered" as "this queue is served" would resurrect the original
    bug for every queue the user did not start a worker for.
    """
    monkeypatch.setattr(settings, "task_execution", "auto")
    client = FakeCeleryClient(served={QUEUE_DISCOVERY})
    monkeypatch.setattr("app.api.tasks._celery_client", lambda: client)

    to_worker = await dispatch(TASK_JOBS_POLL_ALL)
    to_inline = await dispatch(TASK_APPLY_SUBMIT, "application-id")

    assert to_worker.mode == "worker"
    assert to_inline.mode == "inline"
    assert inline_calls.calls == [(TASK_APPLY_SUBMIT, ("application-id",), {})]
    assert [entry[0] for entry in client.sent] == [TASK_JOBS_POLL_ALL]


async def test_worker_liveness_answer_is_cached_across_dispatches(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One probe answers for every queue, or every button press pays a broker round trip.

    The probe sits on the way *in* to a request the user is watching. Workers do not appear
    and vanish between two clicks, so the answer is trusted for ``WORKER_PROBE_TTL_SECONDS``.
    """
    monkeypatch.setattr(settings, "task_execution", "auto")
    client = FakeCeleryClient(served={QUEUE_DISCOVERY})
    monkeypatch.setattr("app.api.tasks._celery_client", lambda: client)

    assert await worker_serves(QUEUE_DISCOVERY) is True
    assert await worker_serves(QUEUE_DISCOVERY) is True
    assert await worker_serves(QUEUE_APPLY) is False
    assert client.probes == 1, "re-probed the broker inside the TTL"

    reset_worker_probe()
    assert await worker_serves(QUEUE_DISCOVERY) is True
    assert client.probes == 2, "reset_worker_probe() did not force a fresh probe"


async def test_worker_probe_failure_reads_as_no_worker(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    inline_calls: RecordingInline,
) -> None:
    """A probe that cannot complete must mean "no worker", never "assume one".

    Every failure mode of the probe — no broker, a hung transport, a raising control plane —
    resolves the same way, because the safe answer is the one that still runs the work.
    """
    monkeypatch.setattr(settings, "task_execution", "auto")
    client = FakeCeleryClient(served={QUEUE_APPLY}, inspect_error=OSError("broker is gone"))
    monkeypatch.setattr("app.api.tasks._celery_client", lambda: client)

    assert await worker_serves(QUEUE_APPLY) is False

    result = await dispatch(TASK_APPLY_SUBMIT, "application-id")

    assert result.mode == "inline"
    assert client.sent == []
    assert inline_calls.calls != []


# ======================================================================================
# 3 — task_execution="worker" never silently adopts the work
# ======================================================================================


async def test_worker_mode_with_no_worker_is_degraded_and_never_inline(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    inline_calls: RecordingInline,
) -> None:
    """``worker`` is the deployed posture: fail loudly rather than run the pipeline here.

    A production API process that quietly started executing the apply queue in-band would be
    running Playwright inside the web tier — different resource envelope, different failure
    blast radius, and nothing in the logs to say the deployment was not the one configured.
    """
    monkeypatch.setattr(settings, "task_execution", "worker")
    monkeypatch.setattr("app.api.tasks._celery_client", lambda: None)

    result = await dispatch(TASK_APPLY_SUBMIT, "application-id")

    assert result.mode == "none"
    assert result.degraded is True
    assert result.dispatched is False
    assert result.reason, "a degraded dispatch must tell the user why"
    assert inline_calls.calls == [], "worker mode silently ran the task in-process"


async def test_worker_mode_with_unreachable_broker_is_degraded_and_never_inline(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    inline_calls: RecordingInline,
) -> None:
    """Celery installed but the broker refusing is the same answer: degraded, not inline."""
    monkeypatch.setattr(settings, "task_execution", "worker")
    client = FakeCeleryClient(served={QUEUE_APPLY}, send_error=ConnectionError("refused"))
    monkeypatch.setattr("app.api.tasks._celery_client", lambda: client)

    result = await dispatch(TASK_APPLY_SUBMIT, "application-id")

    assert result.mode == "none"
    assert result.degraded is True
    assert result.reason
    assert inline_calls.calls == []


async def test_publish_that_may_have_landed_is_never_re_run_inline(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    inline_calls: RecordingInline,
) -> None:
    """A publish timeout is *ambiguous*, and ambiguity must not become a second execution.

    A timeout means the answer did not arrive, not that the message was rejected — the bytes
    may already be in the broker waiting for a worker. Running the task here as well would be
    two executions of one task, and for ``apply.submit`` that is an application sent twice.
    Golden rule #1 defeated by the very mechanism meant to make the app more reliable, so the
    inline decision is made strictly before any publish and never after one.
    """
    monkeypatch.setattr(settings, "task_execution", "auto")
    monkeypatch.setattr("app.api.tasks.BROKER_TIMEOUT_SECONDS", 0.05)
    client = FakeCeleryClient(served={QUEUE_APPLY}, send_delay=0.5)
    monkeypatch.setattr("app.api.tasks._celery_client", lambda: client)

    result = await dispatch(TASK_APPLY_SUBMIT, "application-id")

    assert result.mode == "none"
    assert result.degraded is True
    assert result.reason and "may be queued" in result.reason
    assert inline_calls.calls == [], "re-ran a task that may already be queued on the broker"


# ======================================================================================
# 4 — task_execution="inline" never touches the broker
# ======================================================================================


async def test_inline_mode_never_touches_the_broker(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    inline_calls: RecordingInline,
) -> None:
    """``inline`` means *no broker at all* — not "try the broker first".

    Building the Celery client opens a connection pool and the probe costs a round trip. An
    install that has declared it has no broker must pay neither, so the client factory is
    booby-trapped: any call at all fails this test.
    """
    monkeypatch.setattr(settings, "task_execution", "inline")

    def _forbidden() -> Any:
        raise AssertionError("inline mode reached for a Celery client")

    monkeypatch.setattr("app.api.tasks._celery_client", _forbidden)

    result = await dispatch(TASK_JOBS_POLL_ALL, "greenhouse", limit=5)

    assert result.mode == "inline"
    assert result.degraded is False
    assert result.task_id is None
    assert inline_calls.calls == [(TASK_JOBS_POLL_ALL, ("greenhouse",), {"limit": 5})]


# ======================================================================================
# 5 — the executor actually runs work, and one failure does not end the pool
# ======================================================================================


def test_inline_executor_runs_a_task_and_survives_one_that_raises(
    registered_tasks: SimpleNamespace,
) -> None:
    """A crashing task must not take the drain thread — and the pool — with it.

    This is the failure mode that would recreate the original outage from the inside: one
    poisoned job kills the only worker thread, every later job sits in the queue forever, and
    the user sees exactly what they saw before — a session stuck at ``running`` with nothing
    consuming its work. One drain thread is used deliberately, so ``ok`` can only have run on
    the same thread that ``boom`` just blew up on.
    """
    from app.workers.inline import InlineExecutor

    executor = InlineExecutor(workers=1, queue_size=8)
    try:
        assert executor.submit(registered_tasks.boom, (), {}) is True
        assert executor.submit(registered_tasks.ok, ("second",), {}) is True

        assert registered_tasks.finished.wait(timeout=_THREAD_WAIT_SECONDS), (
            "the drain thread died with the failing task"
        )
        assert registered_tasks.ran == ["boom", "second"]
        assert executor.running is True, "the pool stopped after a task raised"
    finally:
        executor.shutdown(timeout=_THREAD_WAIT_SECONDS)

    assert executor.running is False, "shutdown left drain threads alive"


def test_inline_executor_ignores_an_unregistered_task_without_dying(
    registered_tasks: SimpleNamespace,
) -> None:
    """An unknown task name is a logged mistake, not a dead pool.

    ``_resolve`` returning ``None`` is how a typo in a task name presents itself. It must cost
    that one job and nothing else.
    """
    from app.workers.inline import InlineExecutor

    executor = InlineExecutor(workers=1, queue_size=8)
    try:
        assert executor.submit("tests.inline.no-such-task", (), {}) is True
        assert executor.submit(registered_tasks.ok, ("after-unknown",), {}) is True

        assert registered_tasks.finished.wait(timeout=_THREAD_WAIT_SECONDS)
        assert registered_tasks.ran == ["after-unknown"]
        assert executor.running is True
    finally:
        executor.shutdown(timeout=_THREAD_WAIT_SECONDS)


# ======================================================================================
# 6 — concurrency is clamped by what the database can actually do
# ======================================================================================


def test_concurrency_is_clamped_to_one_writer_on_sqlite(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite has exactly one writer, so a second drain thread does not go faster — it loses.

    A discovery pass upserts a few hundred postings inside one transaction. Two threads doing
    that do not interleave: one holds the write lock for the whole pass and the other burns
    its ``busy_timeout`` and fails, which measured as 97 postings lost to
    ``discovery.ingest_failed``. Raising the timeout only trades lost work for a stalled
    thread; not creating the second writer is the honest fix.
    """
    from app.workers.inline import _concurrency_for

    monkeypatch.setattr(settings, "sqlite_mode", True)
    monkeypatch.setattr(settings, "inline_task_workers", 8)

    assert _concurrency_for(settings) == 1


def test_concurrency_is_clamped_when_only_the_url_says_sqlite(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``sqlite://`` URL with ``sqlite_mode=False`` is still SQLite.

    The flag and the URL can disagree — the flag is a posture, the URL is the truth about
    which engine will take the write lock. The clamp follows the truth.
    """
    from app.workers.inline import _concurrency_for

    monkeypatch.setattr(settings, "sqlite_mode", False)
    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///./var/app.db")
    monkeypatch.setattr(settings, "inline_task_workers", 4)

    assert _concurrency_for(settings) == 1


def test_concurrency_honours_the_setting_on_postgres(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgreSQL has real row-level concurrency, so the configured width is used.

    Without this the clamp above would pass against a function hard-coded to ``return 1``,
    which is safe, useless, and would make ``inline_task_workers`` a setting that does
    nothing.
    """
    from app.workers.inline import _concurrency_for

    monkeypatch.setattr(settings, "sqlite_mode", False)
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.setattr(settings, "inline_task_workers", 5)

    assert _concurrency_for(settings) == 5


def test_concurrency_never_returns_zero_threads(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfigured ``inline_task_workers=0`` must not produce a pool that drains nothing.

    Zero threads is the outage again, this time spelled as a configuration value.
    """
    from app.workers.inline import _concurrency_for

    monkeypatch.setattr(settings, "sqlite_mode", False)
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.setattr(settings, "inline_task_workers", 0)

    assert _concurrency_for(settings) == 1


# ======================================================================================
# 7 — a saturated pool refuses out loud
# ======================================================================================


def test_full_inline_queue_refuses_rather_than_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound is real: submission past it returns ``False`` instead of blocking.

    The caller is inside an HTTP request. Blocking on a full queue turns a fan-out of 244
    children into a hung button, and an unbounded queue turns it into memory pressure. Drain
    threads are suppressed here so the queue genuinely fills rather than being emptied
    underneath the test.
    """
    from app.workers.inline import InlineExecutor

    executor = InlineExecutor(workers=1, queue_size=2)
    monkeypatch.setattr(executor, "start", lambda: None)

    assert executor.submit("tests.inline.ok", (), {}) is True
    assert executor.submit("tests.inline.ok", (), {}) is True
    assert executor.submit("tests.inline.ok", (), {}) is False, "queue accepted past its bound"
    assert executor.depth == 2


async def test_saturated_pool_reports_mode_none_with_a_reason(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Work that did not start must say so — a silent drop is the original bug, restated.

    ``mode="inline"`` here would be a lie the desktop app renders as a running session that
    never finishes, which is exactly the state this subsystem was written to eliminate.
    """
    monkeypatch.setattr(settings, "task_execution", "inline")
    monkeypatch.setattr("app.workers.inline.submit_inline", RecordingInline(accept=False))

    result = await dispatch(TASK_JOBS_POLL_ALL)

    assert result.mode == "none"
    assert result.degraded is True
    assert result.dispatched is False
    assert result.reason and "not started" in result.reason
    assert result.queue == QUEUE_DISCOVERY


def test_submit_inline_refuses_while_the_application_is_stopping() -> None:
    """A request in flight during shutdown must not resurrect the pool it is closing.

    ``shutdown_executor`` joins for up to ten seconds, and that join is a window in which a
    late ``submit_inline`` would find ``_executor is None`` and build a fresh pool — daemon
    threads started moments after the app said it had stopped, running against an engine the
    lifespan is about to dispose. The latch turns that into an honest ``False``.
    """
    from app.workers.inline import reset_executor_state, shutdown_executor, submit_inline

    shutdown_executor()
    try:
        assert submit_inline("tests.inline.ok", (), {}) is False
    finally:
        reset_executor_state()


# ======================================================================================
# 8 — the wire format always carries both fields
# ======================================================================================


@pytest.mark.parametrize(
    ("mode", "expected_degraded"),
    [("worker", False), ("inline", False), ("none", True)],
)
def test_as_dict_always_carries_mode_and_degraded(
    mode: str,
    expected_degraded: bool,
) -> None:
    """The desktop app branches on ``degraded`` and *explains* using ``mode``.

    ``desktop/src/lib/api/dispatch.ts`` reads both from every dispatch body. A response that
    omitted either would leave the renderer unable to distinguish "running here" from "handed
    to a worker" from "did not start", and the fallback for an unknown shape is silence — the
    exact silence that let the outage go unnoticed.
    """
    result = Dispatch(task=TASK_JOBS_POLL_ALL, queue=QUEUE_DISCOVERY, mode=mode)  # type: ignore[arg-type]

    payload = result.as_dict()

    assert payload["mode"] == mode
    assert payload[DEGRADED_KEY] is expected_degraded
    assert payload["dispatched"] is not expected_degraded
    assert payload["task"] == TASK_JOBS_POLL_ALL
    assert payload["queue"] == QUEUE_DISCOVERY


def test_as_dict_omits_absent_optionals_but_keeps_the_required_pair() -> None:
    """``task_id`` and ``reason`` are conditional; ``mode`` and ``degraded`` never are."""
    started = Dispatch(
        task=TASK_APPLY_SUBMIT, queue=QUEUE_APPLY, mode="worker", task_id="abc"
    ).as_dict()
    refused = Dispatch(
        task=TASK_APPLY_SUBMIT, queue=QUEUE_APPLY, mode="none", reason="broker down"
    ).as_dict()

    assert started["task_id"] == "abc"
    assert "reason" not in started
    assert refused["reason"] == "broker down"
    assert "task_id" not in refused
    for payload in (started, refused):
        assert "mode" in payload
        assert DEGRADED_KEY in payload


# ======================================================================================
# Fan-out from inside a task keeps the same routing decision
# ======================================================================================


def test_enqueue_returns_an_id_when_it_routes_a_child_inline(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inline fan-out must report the children it queued, or every counter reads zero.

    ``enqueue`` returning ``None`` on its *success* path is the shape of a bug that already
    happened: ``apply_jobs`` sets ``queued_for_submit`` from this return, ``poll_jobs``
    counts what it fanned out. A working inline pipeline would have reported that it queued
    nothing, and the session UI would show a run that did everything and claims it did
    nothing.
    """
    from app.workers import celery_app as worker_module

    monkeypatch.setattr(settings, "task_execution", "inline")
    recorder = RecordingInline()
    monkeypatch.setattr("app.workers.inline.submit_inline", recorder)

    identifier = worker_module.enqueue(TASK_APPLY_SUBMIT, "application-id")

    assert identifier is not None, "an accepted inline child reported itself as not queued"
    assert identifier.startswith("inline-"), "an inline id must be distinguishable from a broker id"
    assert recorder.calls == [(TASK_APPLY_SUBMIT, ("application-id",), {})]


def test_enqueue_reports_none_when_the_inline_pool_is_full(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused child is ``None`` — the counter must not count work that did not start."""
    from app.workers import celery_app as worker_module

    monkeypatch.setattr(settings, "task_execution", "inline")
    monkeypatch.setattr("app.workers.inline.submit_inline", RecordingInline(accept=False))

    assert worker_module.enqueue(TASK_APPLY_SUBMIT, "application-id") is None


def test_a_real_celery_worker_still_fans_out_through_the_broker(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside ``celery worker`` the children go back to the broker, not into this process.

    Per-queue isolation is the whole point of running five queues: ``apply.submit`` must
    execute on the apply worker, not inside ``worker-discovery`` because that is where its
    parent happened to run. ``auto`` therefore means "inline *unless this process is a
    worker*", never "inline always".
    """
    from app.workers import celery_app as worker_module

    monkeypatch.setattr(settings, "task_execution", "auto")
    in_worker = threading.Event()
    in_worker.set()
    monkeypatch.setattr(worker_module, "_IN_CELERY_WORKER", in_worker)

    assert worker_module._executing_inline() is False

    in_worker.clear()
    assert worker_module._executing_inline() is True
