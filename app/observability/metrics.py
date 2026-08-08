"""Prometheus collectors for ApplicantOS (``docs/CONTRACTS.md`` §16).

Every series named in §16 is declared here, once, on a module-private :data:`registry`.
Nothing outside this module constructs a collector, and nothing outside this module knows
whether ``prometheus_client`` is installed — callers use the recorder functions
(:func:`record_application`, :func:`observe_apply`, :func:`track_task`, …) and the exposition
helper :func:`render_metrics`.

**The client library is optional.** ``prometheus_client`` is not a declared dependency of
this project, and the desktop install ships without it. So this module carries a small,
self-contained fallback implementation of the four things it actually needs — counter, gauge,
histogram and text exposition — and picks it up when the real library is absent. The two
paths expose the same surface (``labels(**kw).inc()`` / ``.set()`` / ``.observe()``) and both
render valid Prometheus text format, so ``/metrics`` works on a bare install and an operator
scraping it cannot tell the difference. :data:`USING_PROMETHEUS_CLIENT` says which one is
live.

**Registration is idempotent.** ``prometheus_client`` raises ``ValueError: Duplicated
timeseries`` when a name is registered twice, which turns an innocuous double import — a
module reached both as ``app.observability.metrics`` and through a reloader — into a crash at
import time. :func:`_counter` and its siblings therefore resolve an already-registered
collector instead of raising.

**Telemetry is never load-bearing.** Every recorder is wrapped so that a metrics failure
degrades to a debug log. A broken counter must not fail an application submission.

Two recorder names here exist to satisfy probes written by earlier modules against a
metrics module that did not exist yet: :func:`record_llm_tokens` and
:func:`record_llm_request` are looked up by :mod:`app.ai.llm`, and :func:`record_cache_event`
by :mod:`app.cache.base`. Their signatures are fixed by those call sites — see each
docstring — and must not drift.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from types import ModuleType
from typing import Any, Final

import structlog

__all__ = [
    "APPLICATIONS",
    "APPLY_DURATION",
    "CACHE_EVENTS",
    "CONTENT_TYPE",
    "DOCUMENTS_RENDERED",
    "HTTP_REQUEST_DURATION",
    "KNOWLEDGE_DOCUMENTS",
    "KNOWLEDGE_INDEX_DURATION",
    "LLM_REQUESTS",
    "LLM_TOKENS",
    "OUTCOME_FAILURE",
    "OUTCOME_SUCCESS",
    "POSTINGS_DEDUPED",
    "POSTINGS_DISCOVERED",
    "REVIEW_QUEUE_SIZE",
    "SCORES",
    "SESSION_ACTIVE",
    "TASK_DURATION",
    "TOKEN_KIND_INPUT",
    "TOKEN_KIND_OUTPUT",
    "USING_PROMETHEUS_CLIENT",
    "observe_apply",
    "observe_http_request",
    "observe_knowledge_index",
    "record_application",
    "record_cache_event",
    "record_document_rendered",
    "record_knowledge_document",
    "record_llm_request",
    "record_llm_tokens",
    "record_llm_usage",
    "record_posting_deduped",
    "record_posting_discovered",
    "record_score",
    "registry",
    "render_metrics",
    "set_review_queue_size",
    "set_session_active",
    "track_task",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Vocabulary
# ======================================================================================

#: Prometheus text exposition content type. Mirrors ``prometheus_client``'s own constant;
#: restated because the fallback renderer must advertise it without importing the library.
CONTENT_TYPE: Final[str] = "text/plain; version=0.0.4; charset=utf-8"

#: ``kind`` label value for prompt tokens. Mirrors :mod:`app.ai.llm`.
TOKEN_KIND_INPUT: Final[str] = "input"

#: ``kind`` label value for completion tokens.
TOKEN_KIND_OUTPUT: Final[str] = "output"

#: ``outcome`` label value for work that completed.
OUTCOME_SUCCESS: Final[str] = "success"

#: ``outcome`` label value for work that raised.
OUTCOME_FAILURE: Final[str] = "failure"

#: Replacement used whenever a label value is missing. An empty label reads as "the series
#: does not have this dimension", which is a different and misleading claim.
_UNKNOWN_LABEL: Final[str] = "unknown"

#: Bucket ladder for a browser application flow. A real submission takes tens of seconds:
#: page load, field discovery, uploads, verification. Buckets below one second exist only
#: to make a *dry run* (which never opens a browser) distinguishable from a real attempt.
APPLY_DURATION_BUCKETS: Final[tuple[float, ...]] = (
    0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0, 180.0, 300.0,
)

#: Bucket ladder for one analyzer pass over one knowledge source. A fingerprint-skip is
#: milliseconds; a cold GitHub crawl of 200 repositories is minutes.
INDEX_DURATION_BUCKETS: Final[tuple[float, ...]] = (
    0.01, 0.05, 0.25, 1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0,
)

#: Bucket ladder for a Celery task. Spans the same range as an apply flow because
#: ``apply.run_one`` *is* one.
TASK_DURATION_BUCKETS: Final[tuple[float, ...]] = (
    0.01, 0.05, 0.25, 1.0, 5.0, 15.0, 60.0, 180.0, 600.0,
)

#: Bucket ladder for an HTTP request. The desktop app's interaction budget is 50ms and its
#: route-change budget is 100ms (``docs/CONTRACTS.md`` §18), so the ladder is dense there
#: and coarse afterwards — a request over a second has already missed every budget.
HTTP_DURATION_BUCKETS: Final[tuple[float, ...]] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)


# ======================================================================================
# The optional client library
# ======================================================================================


def _load_prometheus() -> ModuleType | None:
    """Import ``prometheus_client`` if it is installed.

    Returns:
        The module, or ``None`` when the optional accelerator is absent. Import happens
        exactly once, at module import, because the choice of backend has to be stable for
        the lifetime of the process — a registry cannot switch implementations halfway.
    """
    try:
        import prometheus_client
    except ImportError:  # pragma: no cover - exercised by the bare install
        return None
    return prometheus_client


_prometheus: Final[ModuleType | None] = _load_prometheus()

#: Whether the real client library is backing the collectors. Exposed for diagnostics and
#: for the readiness payload; behaviour is identical either way.
USING_PROMETHEUS_CLIENT: Final[bool] = _prometheus is not None


# ======================================================================================
# Fallback implementation
# ======================================================================================
#
# Deliberately minimal: counter, gauge, histogram, a registry, and the text renderer. It
# implements the subset of the ``prometheus_client`` surface this module uses and nothing
# else, so there is no illusion that it is a general-purpose replacement.


class _CounterChild:
    """One labelled counter series.

    Attributes:
        value: The monotonically increasing total.
    """

    __slots__ = ("_lock", "_value")

    def __init__(self) -> None:
        """Start at zero."""
        self._lock = threading.Lock()
        self._value = 0.0

    def inc(self, amount: float = 1.0) -> None:
        """Add *amount* to this series.

        Args:
            amount: The increment. Negative values are ignored rather than rejected: a
                counter that went backwards is a bug in the caller, and raising here would
                turn a telemetry bug into a request failure.
        """
        if amount < 0:
            return
        with self._lock:
            self._value += float(amount)

    @property
    def value(self) -> float:
        """The current total."""
        return self._value


class _GaugeChild:
    """One labelled gauge series, which may move in either direction."""

    __slots__ = ("_lock", "_value")

    def __init__(self) -> None:
        """Start at zero."""
        self._lock = threading.Lock()
        self._value = 0.0

    def set(self, value: float) -> None:
        """Replace the current value.

        Args:
            value: The new value.
        """
        with self._lock:
            self._value = float(value)

    def inc(self, amount: float = 1.0) -> None:
        """Add *amount* to the current value.

        Args:
            amount: The increment; may be negative.
        """
        with self._lock:
            self._value += float(amount)

    def dec(self, amount: float = 1.0) -> None:
        """Subtract *amount* from the current value.

        Args:
            amount: The decrement.
        """
        self.inc(-amount)

    @property
    def value(self) -> float:
        """The current value."""
        return self._value


class _HistogramChild:
    """One labelled histogram series: cumulative buckets, a sum, and a count."""

    __slots__ = ("_bounds", "_counts", "_lock", "_sum", "_total")

    def __init__(self, bounds: Sequence[float]) -> None:
        """Create the bucket vector.

        Args:
            bounds: Ascending upper bounds, excluding ``+Inf``.
        """
        self._bounds = tuple(bounds)
        self._counts = [0 for _ in self._bounds]
        self._sum = 0.0
        self._total = 0
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        """Record one observation.

        Args:
            value: The measured value, in the metric's unit. ``NaN`` is dropped — it would
                poison the sum for every subsequent scrape.
        """
        numeric = float(value)
        if math.isnan(numeric):
            return
        with self._lock:
            self._total += 1
            self._sum += numeric
            for index, bound in enumerate(self._bounds):
                if numeric <= bound:
                    self._counts[index] += 1

    def snapshot(self) -> tuple[tuple[tuple[float, int], ...], float, int]:
        """Return ``(cumulative_buckets, sum, count)`` as a consistent point-in-time view.

        Returns:
            The cumulative bucket pairs (upper bound, count), the sum of observations, and
            the observation count.
        """
        with self._lock:
            running = 0
            buckets: list[tuple[float, int]] = []
            for bound, count in zip(self._bounds, self._counts, strict=True):
                running += count
                buckets.append((bound, running))
            return tuple(buckets), self._sum, self._total


class _FallbackMetric:
    """A labelled metric family backed by one of the ``*Child`` classes above.

    Attributes:
        name: The series name exactly as declared.
        documentation: The ``# HELP`` text.
    """

    #: The ``# TYPE`` keyword this family renders as.
    metric_type: str = "untyped"

    __slots__ = ("_buckets", "_children", "_labelnames", "_lock", "documentation", "name")

    def __init__(
        self,
        name: str,
        documentation: str,
        labelnames: Sequence[str] = (),
        *,
        buckets: Sequence[float] = (),
    ) -> None:
        """Declare a metric family.

        Args:
            name: The series name.
            documentation: Help text.
            labelnames: Label dimensions, in declaration order.
            buckets: Histogram bounds; ignored by the other kinds.
        """
        self.name = name
        self.documentation = documentation
        self._labelnames = tuple(labelnames)
        self._buckets = tuple(buckets)
        self._children: dict[tuple[str, ...], Any] = {}
        self._lock = threading.Lock()
        if not self._labelnames:
            self._children[()] = self._new_child()

    def _new_child(self) -> Any:
        """Build the per-series accumulator for this family."""
        raise NotImplementedError  # pragma: no cover - every concrete family overrides

    def labels(self, *args: Any, **kwargs: Any) -> Any:
        """Return the series for one label combination, creating it on first use.

        Args:
            *args: Label values positionally, in declaration order.
            **kwargs: Label values by name.

        Returns:
            The accumulator for that combination.

        Raises:
            ValueError: If the supplied labels do not match the declared dimensions.
        """
        if args and kwargs:
            raise ValueError("supply label values either positionally or by name, not both")
        if args:
            if len(args) != len(self._labelnames):
                raise ValueError(
                    f"{self.name} expects {len(self._labelnames)} label(s), got {len(args)}"
                )
            key = tuple(str(value) for value in args)
        else:
            missing = sorted(set(self._labelnames) - set(kwargs))
            if missing:
                raise ValueError(f"{self.name} is missing label(s): {', '.join(missing)}")
            key = tuple(str(kwargs[name]) for name in self._labelnames)

        with self._lock:
            child = self._children.get(key)
            if child is None:
                child = self._children[key] = self._new_child()
        return child

    def _unlabelled(self) -> Any:
        """Return the single series of a family that declares no labels.

        Raises:
            ValueError: If the family is labelled, where an unlabelled operation is
                meaningless.
        """
        if self._labelnames:
            raise ValueError(f"{self.name} is labelled; call .labels(...) first")
        return self._children[()]

    def samples(self) -> list[tuple[tuple[str, ...], Any]]:
        """Return a stable snapshot of ``(label values, accumulator)`` pairs."""
        with self._lock:
            return sorted(self._children.items())

    @property
    def labelnames(self) -> tuple[str, ...]:
        """The declared label dimensions."""
        return self._labelnames


class _FallbackCounter(_FallbackMetric):
    """A monotonically increasing counter family."""

    metric_type = "counter"

    __slots__ = ()

    def _new_child(self) -> _CounterChild:
        """Build a counter accumulator."""
        return _CounterChild()

    def inc(self, amount: float = 1.0) -> None:
        """Increment the unlabelled series.

        Args:
            amount: The increment.
        """
        self._unlabelled().inc(amount)


class _FallbackGauge(_FallbackMetric):
    """A gauge family whose value may move in either direction."""

    metric_type = "gauge"

    __slots__ = ()

    def _new_child(self) -> _GaugeChild:
        """Build a gauge accumulator."""
        return _GaugeChild()

    def set(self, value: float) -> None:
        """Set the unlabelled series.

        Args:
            value: The new value.
        """
        self._unlabelled().set(value)

    def inc(self, amount: float = 1.0) -> None:
        """Add to the unlabelled series.

        Args:
            amount: The increment.
        """
        self._unlabelled().inc(amount)

    def dec(self, amount: float = 1.0) -> None:
        """Subtract from the unlabelled series.

        Args:
            amount: The decrement.
        """
        self._unlabelled().dec(amount)


class _FallbackHistogram(_FallbackMetric):
    """A histogram family with a fixed bucket ladder."""

    metric_type = "histogram"

    __slots__ = ()

    def _new_child(self) -> _HistogramChild:
        """Build a histogram accumulator over the declared bounds."""
        return _HistogramChild(self._buckets)

    def observe(self, value: float) -> None:
        """Record one observation on the unlabelled series.

        Args:
            value: The measured value.
        """
        self._unlabelled().observe(value)


class _FallbackRegistry:
    """A name-indexed set of :class:`_FallbackMetric` families."""

    __slots__ = ("_lock", "_metrics")

    def __init__(self) -> None:
        """Create an empty registry."""
        self._metrics: dict[str, _FallbackMetric] = {}
        self._lock = threading.Lock()

    def register(self, metric: _FallbackMetric) -> None:
        """Add a family.

        Args:
            metric: The family to register.

        Raises:
            ValueError: If the name is already taken, mirroring ``prometheus_client`` so
                that :func:`_register` handles both backends with one ``except``.
        """
        with self._lock:
            if metric.name in self._metrics:
                raise ValueError(f"duplicated timeseries in registry: {metric.name}")
            self._metrics[metric.name] = metric

    def get(self, name: str) -> _FallbackMetric | None:
        """Return the family registered under *name*, or ``None``.

        Args:
            name: The series name.

        Returns:
            The family, if one is registered.
        """
        with self._lock:
            return self._metrics.get(name)

    def families(self) -> list[_FallbackMetric]:
        """Return every registered family, ordered by name."""
        with self._lock:
            return [self._metrics[name] for name in sorted(self._metrics)]


def _escape_label_value(value: str) -> str:
    """Escape a label value for the Prometheus text format.

    Args:
        value: The raw label value.

    Returns:
        The value with backslashes, double quotes and newlines escaped.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(names: Sequence[str], values: Sequence[str], extra: str = "") -> str:
    """Render a label set, optionally with one appended pair already formatted.

    Args:
        names: Label names in declaration order.
        values: Label values in the same order.
        extra: An already-formatted ``name="value"`` pair to append (used for ``le``).

    Returns:
        The bracketed label block, or an empty string when there are no labels.
    """
    pairs = [f'{name}="{_escape_label_value(value)}"' for name, value in zip(names, values, strict=True)]
    if extra:
        pairs.append(extra)
    return "{" + ",".join(pairs) + "}" if pairs else ""


def _format_float(value: float) -> str:
    """Render a float the way the Prometheus text format expects.

    Args:
        value: The number to render.

    Returns:
        ``"+Inf"`` for infinity, otherwise a plain decimal representation.
    """
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return repr(float(value))


def _family_name(metric: _FallbackMetric) -> str:
    """Return the ``# TYPE`` family name for *metric*.

    ``prometheus_client`` declares a counter's family without the ``_total`` suffix and
    emits the suffix only on the sample line. The fallback matches that so a scrape of
    either backend parses identically.

    Args:
        metric: The family being rendered.

    Returns:
        The family name.
    """
    if metric.metric_type == "counter" and metric.name.endswith("_total"):
        return metric.name[: -len("_total")]
    return metric.name


def _render_fallback(target: _FallbackRegistry) -> bytes:
    """Render every registered family in Prometheus text exposition format.

    Args:
        target: The registry to render.

    Returns:
        The UTF-8 encoded exposition document.
    """
    lines: list[str] = []
    for metric in target.families():
        family = _family_name(metric)
        names = metric.labelnames
        lines.append(f"# HELP {family} {metric.documentation}")
        lines.append(f"# TYPE {family} {metric.metric_type}")
        for values, child in metric.samples():
            if metric.metric_type == "histogram":
                buckets, total_sum, count = child.snapshot()
                for bound, cumulative in buckets:
                    label_block = _format_labels(names, values, f'le="{_format_float(bound)}"')
                    lines.append(f"{family}_bucket{label_block} {cumulative}")
                inf_block = _format_labels(names, values, 'le="+Inf"')
                lines.append(f"{family}_bucket{inf_block} {count}")
                plain = _format_labels(names, values)
                lines.append(f"{family}_sum{plain} {_format_float(total_sum)}")
                lines.append(f"{family}_count{plain} {count}")
            else:
                suffix = "_total" if metric.metric_type == "counter" else ""
                plain = _format_labels(names, values)
                lines.append(f"{family}{suffix}{plain} {_format_float(child.value)}")
    return ("\n".join(lines) + "\n").encode("utf-8")


# ======================================================================================
# Registry and idempotent declaration
# ======================================================================================

#: The registry every ApplicantOS collector lives on. Private rather than
#: ``prometheus_client``'s global ``REGISTRY`` so that a scrape of ``/metrics`` returns this
#: application's series and not whatever else happens to be linked into the process.
registry: Any = _prometheus.CollectorRegistry() if _prometheus is not None else _FallbackRegistry()


def _existing(name: str) -> Any | None:
    """Return the collector already registered under *name*, if any.

    Args:
        name: The series name, exactly as declared.

    Returns:
        The existing collector, or ``None``.
    """
    if _prometheus is None:
        return registry.get(name)
    # ``prometheus_client`` indexes every sample name a collector owns, including the
    # ``_total``/``_created`` variants of a counter, so a lookup by the declared name hits.
    collectors: Mapping[str, Any] = getattr(registry, "_names_to_collectors", {})
    return collectors.get(name)


def _register(name: str, build: Any) -> Any:
    """Declare a collector, resolving an already-registered one instead of raising.

    Duplicate registration is not a programming error worth crashing over: it happens when
    this module is imported under two names or reloaded by a development server, and the
    correct response is to keep using the collector that already exists.

    Args:
        name: The series name.
        build: Zero-argument factory constructing the collector.

    Returns:
        The newly built collector, or the one that was already registered.
    """
    try:
        return build()
    except ValueError:
        found = _existing(name)
        if found is None:  # pragma: no cover - a ValueError with no prior registration
            raise
        logger.debug("metrics.collector_reused", metric=name)
        return found


def _counter(name: str, documentation: str, labelnames: Sequence[str] = ()) -> Any:
    """Declare a counter on :data:`registry`.

    Args:
        name: Series name, ending in ``_total`` by convention.
        documentation: Help text.
        labelnames: Label dimensions.

    Returns:
        The counter collector.
    """
    if _prometheus is None:
        def build() -> Any:
            metric = _FallbackCounter(name, documentation, labelnames)
            registry.register(metric)
            return metric
    else:
        def build() -> Any:
            return _prometheus.Counter(
                name, documentation, labelnames=list(labelnames), registry=registry
            )
    return _register(name, build)


def _gauge(name: str, documentation: str, labelnames: Sequence[str] = ()) -> Any:
    """Declare a gauge on :data:`registry`.

    Args:
        name: Series name.
        documentation: Help text.
        labelnames: Label dimensions.

    Returns:
        The gauge collector.
    """
    if _prometheus is None:
        def build() -> Any:
            metric = _FallbackGauge(name, documentation, labelnames)
            registry.register(metric)
            return metric
    else:
        def build() -> Any:
            return _prometheus.Gauge(
                name, documentation, labelnames=list(labelnames), registry=registry
            )
    return _register(name, build)


def _histogram(
    name: str,
    documentation: str,
    labelnames: Sequence[str] = (),
    *,
    buckets: Sequence[float],
) -> Any:
    """Declare a histogram on :data:`registry`.

    Args:
        name: Series name.
        documentation: Help text.
        labelnames: Label dimensions.
        buckets: Ascending upper bounds, excluding ``+Inf``.

    Returns:
        The histogram collector.
    """
    if _prometheus is None:
        def build() -> Any:
            metric = _FallbackHistogram(name, documentation, labelnames, buckets=buckets)
            registry.register(metric)
            return metric
    else:
        def build() -> Any:
            return _prometheus.Histogram(
                name,
                documentation,
                labelnames=list(labelnames),
                buckets=list(buckets),
                registry=registry,
            )
    return _register(name, build)


# ======================================================================================
# The collectors of docs/CONTRACTS.md §16
# ======================================================================================

#: Postings yielded by a provider, before deduplication.
POSTINGS_DISCOVERED: Final[Any] = _counter(
    "applicantos_postings_discovered_total",
    "Job postings returned by a provider, before deduplication.",
    ("provider",),
)

#: Postings that collapsed onto a row that already existed.
POSTINGS_DEDUPED: Final[Any] = _counter(
    "applicantos_postings_deduped_total",
    "Job postings discarded as duplicates of an already-known posting.",
    ("provider",),
)

#: Scoring outcomes, by routing verdict (``apply`` / ``review`` / ``skip``).
SCORES: Final[Any] = _counter(
    "applicantos_scores_total",
    "Postings scored, labelled by the routing verdict the score produced.",
    ("verdict",),
)

#: Applications reaching a given status, by provider.
APPLICATIONS: Final[Any] = _counter(
    "applicantos_applications_total",
    "Applications that reached a status, labelled by status and ATS provider.",
    ("status", "provider"),
)

#: Wall-clock duration of one application flow.
APPLY_DURATION: Final[Any] = _histogram(
    "applicantos_apply_duration_seconds",
    "Wall-clock seconds spent on one application flow.",
    ("provider",),
    buckets=APPLY_DURATION_BUCKETS,
)

#: LLM tokens billed, by model and by prompt/completion.
LLM_TOKENS: Final[Any] = _counter(
    "applicantos_llm_tokens_total",
    "LLM tokens billed, labelled by model and by input/output.",
    ("model", "kind"),
)

#: LLM requests attempted, by model and outcome.
LLM_REQUESTS: Final[Any] = _counter(
    "applicantos_llm_requests_total",
    "LLM requests attempted, labelled by model and outcome.",
    ("model", "outcome"),
)

#: Cache hits, misses, sets, deletes and errors, by namespace.
CACHE_EVENTS: Final[Any] = _counter(
    "applicantos_cache_events_total",
    "Cache operations, labelled by namespace and event.",
    ("namespace", "event"),
)

#: Document renders attempted, by engine and outcome.
DOCUMENTS_RENDERED: Final[Any] = _counter(
    "applicantos_documents_rendered_total",
    "Document renders attempted, labelled by engine and outcome.",
    ("engine", "outcome"),
)

#: Knowledge documents indexed, by source kind.
KNOWLEDGE_DOCUMENTS: Final[Any] = _counter(
    "applicantos_knowledge_documents_total",
    "Knowledge documents indexed, labelled by source kind.",
    ("kind",),
)

#: Duration of one analyzer pass over one knowledge source.
KNOWLEDGE_INDEX_DURATION: Final[Any] = _histogram(
    "applicantos_knowledge_index_duration_seconds",
    "Seconds spent indexing one knowledge source, labelled by analyzer.",
    ("analyzer",),
    buckets=INDEX_DURATION_BUCKETS,
)

#: Depth of the human review queue. A gauge, because it goes down when a human works it.
REVIEW_QUEUE_SIZE: Final[Any] = _gauge(
    "applicantos_review_queue_size",
    "Applications currently parked awaiting a human decision.",
)

#: Duration of one Celery task, by task name and outcome.
TASK_DURATION: Final[Any] = _histogram(
    "applicantos_task_duration_seconds",
    "Seconds spent executing one background task, labelled by task and outcome.",
    ("task", "outcome"),
    buckets=TASK_DURATION_BUCKETS,
)

#: Automation runs currently executing.
SESSION_ACTIVE: Final[Any] = _gauge(
    "applicantos_session_active",
    "Automation runs currently in the running state.",
)

#: Duration of one HTTP request, by matched route, method and status.
HTTP_REQUEST_DURATION: Final[Any] = _histogram(
    "applicantos_http_request_duration_seconds",
    "Seconds spent serving one HTTP request.",
    ("route", "method", "status"),
    buckets=HTTP_DURATION_BUCKETS,
)


# ======================================================================================
# Recorders
# ======================================================================================


def _label(value: Any) -> str:
    """Coerce a label value to a non-empty string.

    Args:
        value: Anything a caller passed as a label — an enum member, ``None``, an int.

    Returns:
        The string form, or :data:`_UNKNOWN_LABEL` when there is nothing to say.
    """
    if value is None:
        return _UNKNOWN_LABEL
    text = getattr(value, "value", value)
    rendered = str(text).strip()
    return rendered or _UNKNOWN_LABEL


def _safe(action: str, operation: Any) -> None:
    """Run a metric mutation, degrading any failure to a debug log.

    Telemetry is never load-bearing: a collector that raises must not fail the submission,
    the render, or the request that was being measured.

    Args:
        action: Short name of what was being recorded, for the log line.
        operation: Zero-argument callable performing the mutation.
    """
    try:
        operation()
    except Exception as exc:  # noqa: BLE001 - a metric must never break the caller
        logger.debug("metrics.record_failed", metric=action, error=str(exc))


def record_posting_discovered(provider: Any, amount: int = 1) -> None:
    """Count postings a provider returned.

    Args:
        provider: The ATS provider name.
        amount: How many postings to add.
    """
    _safe(
        "postings_discovered",
        lambda: POSTINGS_DISCOVERED.labels(provider=_label(provider)).inc(amount),
    )


def record_posting_deduped(provider: Any, amount: int = 1) -> None:
    """Count postings discarded as duplicates.

    Args:
        provider: The ATS provider name.
        amount: How many postings to add.
    """
    _safe(
        "postings_deduped",
        lambda: POSTINGS_DEDUPED.labels(provider=_label(provider)).inc(amount),
    )


def record_score(verdict: Any, amount: int = 1) -> None:
    """Count one scoring outcome.

    Args:
        verdict: ``apply``, ``review`` or ``skip``.
        amount: How many to add.
    """
    _safe("scores", lambda: SCORES.labels(verdict=_label(verdict)).inc(amount))


def record_application(status: Any, provider: Any = None) -> None:
    """Count an application reaching a status.

    Called on every transition, so the counter answers "how many applications have ever
    entered ``needs_review``?" rather than "how many are there now" — the latter is a
    gauge, and :func:`set_review_queue_size` is the one that reports it.

    Args:
        status: The :class:`~app.models.enums.ApplicationStatus` reached.
        provider: The ATS provider behind the posting, when known.
    """
    _safe(
        "applications",
        lambda: APPLICATIONS.labels(
            status=_label(status), provider=_label(provider)
        ).inc(),
    )


def observe_apply(provider: Any, seconds: float) -> None:
    """Record how long one application flow took.

    Args:
        provider: The ATS provider the flow ran against.
        seconds: Wall-clock duration. Negative values are dropped.
    """
    if seconds < 0:
        return
    _safe(
        "apply_duration",
        lambda: APPLY_DURATION.labels(provider=_label(provider)).observe(float(seconds)),
    )


def record_llm_tokens(*, model: str, kind: str, amount: float = 1.0) -> None:
    """Add to ``applicantos_llm_tokens_total{model,kind}``.

    Note:
        The keyword-only signature is fixed by :mod:`app.ai.llm`, which discovers this
        function by name and calls it as ``record_llm_tokens(model=…, kind=…,
        amount=…)``. Do not make any parameter positional.

    Args:
        model: The model identifier.
        kind: :data:`TOKEN_KIND_INPUT` or :data:`TOKEN_KIND_OUTPUT`.
        amount: Number of tokens.
    """
    _safe(
        "llm_tokens",
        lambda: LLM_TOKENS.labels(model=_label(model), kind=_label(kind)).inc(amount),
    )


def record_llm_request(*, model: str, outcome: str, amount: float = 1.0) -> None:
    """Increment ``applicantos_llm_requests_total{model,outcome}``.

    Note:
        As with :func:`record_llm_tokens`, the keyword-only signature is fixed by
        :mod:`app.ai.llm`'s discovery-by-name bridge.

    Args:
        model: The model identifier.
        outcome: ``success``, ``error``, ``cached``, … as the caller classifies it.
        amount: How many requests to add.
    """
    _safe(
        "llm_requests",
        lambda: LLM_REQUESTS.labels(model=_label(model), outcome=_label(outcome)).inc(amount),
    )


def record_llm_usage(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    outcome: str = OUTCOME_SUCCESS,
) -> None:
    """Record one completed LLM call: its token cost and its outcome.

    The single call site a feature author should reach for. It writes all three series
    §16 defines for a model call, so a caller cannot record the tokens and forget the
    request (or the reverse), which is how the two counters drift apart.

    Args:
        model: The model identifier that served the call.
        input_tokens: Prompt tokens billed.
        output_tokens: Completion tokens billed.
        outcome: Outcome label for ``applicantos_llm_requests_total``.
    """
    if input_tokens > 0:
        record_llm_tokens(model=model, kind=TOKEN_KIND_INPUT, amount=input_tokens)
    if output_tokens > 0:
        record_llm_tokens(model=model, kind=TOKEN_KIND_OUTPUT, amount=output_tokens)
    record_llm_request(model=model, outcome=outcome)


def record_cache_event(namespace: str, event: str) -> None:
    """Increment ``applicantos_cache_events_total{namespace,event}``.

    Note:
        The **positional** signature is fixed by :mod:`app.cache.base`, which resolves this
        function by name and calls it as ``record_cache_event(namespace, event)``.

    Args:
        namespace: The cache namespace.
        event: One of the :class:`app.cache.base.CacheEvent` constants.
    """
    _safe(
        "cache_events",
        lambda: CACHE_EVENTS.labels(namespace=_label(namespace), event=_label(event)).inc(),
    )


def record_document_rendered(engine: str, outcome: str) -> None:
    """Count one document render attempt.

    Args:
        engine: The rendering engine (``latex``, ``docx``, ``html``, ``markdown``).
        outcome: ``success`` or ``failure``.
    """
    _safe(
        "documents_rendered",
        lambda: DOCUMENTS_RENDERED.labels(
            engine=_label(engine), outcome=_label(outcome)
        ).inc(),
    )


def record_knowledge_document(kind: Any, amount: int = 1) -> None:
    """Count knowledge documents indexed.

    Args:
        kind: The :class:`~app.models.enums.SourceKind` of the document.
        amount: How many to add.
    """
    _safe(
        "knowledge_documents",
        lambda: KNOWLEDGE_DOCUMENTS.labels(kind=_label(kind)).inc(amount),
    )


def observe_knowledge_index(analyzer: str, seconds: float) -> None:
    """Record how long one indexing pass took.

    Args:
        analyzer: The analyzer plugin name.
        seconds: Wall-clock duration.
    """
    if seconds < 0:
        return
    _safe(
        "knowledge_index_duration",
        lambda: KNOWLEDGE_INDEX_DURATION.labels(analyzer=_label(analyzer)).observe(
            float(seconds)
        ),
    )


def set_review_queue_size(size: int) -> None:
    """Publish the current depth of the human review queue.

    Args:
        size: Number of applications awaiting a human. Negative values are ignored.
    """
    if size < 0:
        return
    _safe("review_queue_size", lambda: REVIEW_QUEUE_SIZE.set(float(size)))


def set_session_active(count: int) -> None:
    """Publish how many automation runs are currently executing.

    Args:
        count: Number of running sessions. Negative values are ignored.
    """
    if count < 0:
        return
    _safe("session_active", lambda: SESSION_ACTIVE.set(float(count)))


def observe_http_request(route: str, method: str, status: int | str, seconds: float) -> None:
    """Record one served HTTP request.

    Args:
        route: The matched route template, never the raw path — a path carrying a UUID
            would make this series unbounded in cardinality.
        method: The HTTP method.
        status: The response status code.
        seconds: Wall-clock duration.
    """
    if seconds < 0:
        return
    _safe(
        "http_request_duration",
        lambda: HTTP_REQUEST_DURATION.labels(
            route=_label(route), method=_label(method), status=_label(status)
        ).observe(float(seconds)),
    )


@contextmanager
def track_task(task: str) -> Iterator[None]:
    """Time a unit of background work and record its outcome.

    Records ``applicantos_task_duration_seconds{task,outcome}`` on the way out, whichever
    way the block leaves. The exception is re-raised untouched — this measures work, it does
    not handle failures::

        with track_task("apply.submit"):
            await pipeline.submit(application_id)

    Args:
        task: The task name, matching the Celery task name in ``docs/CONTRACTS.md`` §15.

    Yields:
        ``None``; the block's own result is not observed.
    """
    started = time.perf_counter()
    outcome = OUTCOME_SUCCESS
    try:
        yield
    except BaseException:
        outcome = OUTCOME_FAILURE
        raise
    finally:
        elapsed = time.perf_counter() - started
        _safe(
            "task_duration",
            lambda: TASK_DURATION.labels(task=_label(task), outcome=outcome).observe(elapsed),
        )


# ======================================================================================
# Exposition
# ======================================================================================


def render_metrics() -> tuple[bytes, str]:
    """Render the registry for a Prometheus scrape.

    Returns:
        The exposition document and the content type to serve it with. Never raises: a
        failing scrape returns an empty document rather than a 500, because a broken
        ``/metrics`` must not be indistinguishable from a broken application.
    """
    try:
        if _prometheus is None:
            return _render_fallback(registry), CONTENT_TYPE
        payload: bytes = _prometheus.generate_latest(registry)
        content_type: str = getattr(_prometheus, "CONTENT_TYPE_LATEST", CONTENT_TYPE)
        return payload, content_type
    except Exception as exc:  # noqa: BLE001 - a scrape must always answer
        logger.warning("metrics.render_failed", error=str(exc))
        return b"", CONTENT_TYPE
