"""The domain half of ``/metrics`` (``docs/CONTRACTS.md`` §16) — its **producers**.

``app/observability/metrics.py`` declaring a collector proves nothing. A registered series
that is never incremented renders on ``/metrics`` as a flat zero, which is indistinguishable
from a system doing no work — and that is exactly the defect this file exists to keep closed.
So every test here drives the **real** code path that owns a recorder and asserts the recorder
fired, with the labels §16 declares.

Two layers, deliberately:

1. **Call-site tests.** The recorder is replaced with a spy in the *consuming* module's
   namespace and the real service method is driven. Delete the call and the test goes red —
   which is the only property that makes a producer test worth writing.
2. **Exposition tests.** The funnel is driven end to end with no spies at all, and the actual
   bytes :func:`~app.observability.metrics.render_metrics` would serve are parsed. The claim
   under test is "a scrape sees a non-zero value", not "a Python function was called".

One more invariant runs through the file: **telemetry is never load-bearing.** A collector
that raises must degrade to a debug log, never fail the submission, render, transition or
index it was measuring. Those tests break the collector on purpose and assert the work still
happened.
"""

from __future__ import annotations

import ast
import asyncio
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect as sa_inspect

import app.documents.renderer as renderer_module
import app.knowledge.indexer as indexer_module
import app.services.application_service as application_module
import app.services.dedupe_service as dedupe_module
import app.services.discovery_service as discovery_module
import app.services.pipeline as pipeline_module
from app.documents.models import Contact, ResumeDocument, ResumeEntry, ResumeSection
from app.documents.renderer import DocumentRenderError, render_cover_letter, render_resume
from app.knowledge.analyzers.base import (
    AnalysisResult,
    ExtractedDocument,
    ExtractedFact,
    SourceRef,
)
from app.knowledge.indexer import KnowledgeIndexer
from app.knowledge.vector.memory_store import InMemoryVectorStore
from app.models.enums import (
    ApplicationStatus,
    ATSProviderName,
    EmploymentType,
    FactKind,
    ReviewReason,
    SourceKind,
    WorkArrangement,
)
from app.observability.metrics import render_metrics
from app.services.application_service import ApplicationService, InvalidTransition
from app.services.dedupe_service import DedupeService
from app.services.discovery_service import DiscoveryService
from app.services.pipeline import Pipeline

#: Every domain recorder §16 declares, mapped to the module that is supposed to produce it.
#: This is the inventory that filed G12: each of these was a live registration with no
#: caller, and the whole funnel therefore read zero on a working system.
DOMAIN_PRODUCERS: dict[str, str] = {
    "record_posting_discovered": "services/dedupe_service.py",
    "record_posting_deduped": "services/dedupe_service.py",
    "record_score": "services/discovery_service.py",
    "record_application": "services/application_service.py",
    "observe_apply": "services/pipeline.py",
    "record_document_rendered": "documents/renderer.py",
    "record_knowledge_document": "knowledge/indexer.py",
    "observe_knowledge_index": "knowledge/indexer.py",
}


# ======================================================================================
# Spying
# ======================================================================================


class Spy:
    """Records every call made to it, so a test can assert on labels and not just counts.

    Attributes:
        calls: One ``(args, kwargs)`` pair per invocation, in order.
    """

    def __init__(self) -> None:
        """Start with an empty log."""
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        """Record one invocation and return nothing, exactly as a recorder does."""
        self.calls.append((args, kwargs))

    @property
    def count(self) -> int:
        """How many times the recorder was called."""
        return len(self.calls)

    @property
    def first_labels(self) -> tuple[Any, ...]:
        """The positional arguments of the first recorded call."""
        return self.calls[0][0]


class ExplodingCollector:
    """A collector whose every operation raises, standing in for a broken registry.

    Substituted for a real collector to prove the ``_safe`` wrapper around each recorder is
    doing its job: a Prometheus failure must not propagate into the work being measured.
    """

    #: What the substitute raises, matching the message ``prometheus_client`` uses.
    message: str = "duplicated timeseries in registry"

    def labels(self, *_args: Any, **_kwargs: Any) -> Any:
        """Raise instead of returning a child series."""
        raise RuntimeError(self.message)

    def inc(self, *_args: Any, **_kwargs: Any) -> None:
        """Raise instead of incrementing."""
        raise RuntimeError(self.message)

    def observe(self, *_args: Any, **_kwargs: Any) -> None:
        """Raise instead of observing."""
        raise RuntimeError(self.message)


def spy_on(module: Any, name: str, monkeypatch: pytest.MonkeyPatch) -> Spy:
    """Replace recorder *name* in *module*'s namespace with a :class:`Spy`.

    Patching the consuming module rather than :mod:`app.observability.metrics` is deliberate:
    the call sites bind the recorder at import time, so this is the only patch point that
    proves *this* module calls it.

    Args:
        module: The module that owns the call site.
        name: The recorder's name as imported there.
        monkeypatch: The fixture performing the patch.

    Returns:
        The installed spy.
    """
    spy = Spy()
    monkeypatch.setattr(module, name, spy)
    return spy


def break_collector(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace collector *name* on the metrics module with :class:`ExplodingCollector`.

    Args:
        name: The collector's module-level name, e.g. ``"APPLICATIONS"``.
        monkeypatch: The fixture performing the patch.
    """
    monkeypatch.setattr(f"app.observability.metrics.{name}", ExplodingCollector())


# ======================================================================================
# Reading the exposition
# ======================================================================================

#: One sample line of the Prometheus text format: name, optional label block, value.
_SAMPLE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?[ \t]+(?P<value>[^ \t]+)$"
)

#: One ``name="value"`` pair inside a label block.
_LABEL = re.compile(r'(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:[^"\\]|\\.)*)"')


def scrape() -> list[tuple[str, dict[str, str], float]]:
    """Parse what a Prometheus scrape of this process would actually receive.

    Goes through :func:`~app.observability.metrics.render_metrics` rather than reading the
    collectors, so the test exercises the same bytes ``GET /metrics`` serves — under either
    backend, the real ``prometheus_client`` or the built-in fallback.

    Returns:
        ``(name, labels, value)`` for every sample line.
    """
    payload, _content_type = render_metrics()
    samples: list[tuple[str, dict[str, str], float]] = []
    for line in payload.decode("utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE.match(line)
        if match is None:  # pragma: no cover - a malformed exposition is its own failure
            continue
        labels = {
            pair.group("key"): pair.group("value")
            for pair in _LABEL.finditer(match.group("labels") or "")
        }
        try:
            value = float(match.group("value"))
        except ValueError:  # pragma: no cover - "+Inf" only appears as a bucket bound
            continue
        samples.append((match.group("name"), labels, value))
    return samples


def series_value(name: str, **labels: str) -> float:
    """Sum the scraped value of *name* over every sample matching *labels*.

    Args:
        name: The sample name, including any ``_total`` / ``_count`` suffix.
        **labels: Label values that must match. Unnamed dimensions are ignored, so a partial
            selector sums across the rest.

    Returns:
        The summed value, or ``0.0`` when nothing matches.
    """
    total = 0.0
    for sample_name, sample_labels, value in scrape():
        if sample_name != name:
            continue
        if all(sample_labels.get(key) == want for key, want in labels.items()):
            total += value
    return total


# ======================================================================================
# Helpers and fixtures
# ======================================================================================


def raw_posting(**overrides: Any) -> Any:
    """Build a :class:`~app.jobs.base.RawPosting` with defaults good enough to ingest.

    Args:
        **overrides: Fields to replace.

    Returns:
        The posting.
    """
    from app.jobs.base import RawPosting

    # The employer is unique per call so the fuzzy tier cannot silently collapse this
    # posting onto one another fixture happened to create; every dedupe asserted here is
    # the deliberate one.
    marker = uuid.uuid4().hex[:10]
    values: dict[str, Any] = {
        "provider": ATSProviderName.GREENHOUSE,
        "external_id": f"gh-{marker}",
        "url": f"https://boards.greenhouse.io/acme/jobs/{marker}",
        "title": "Senior Backend Engineer",
        "company_name": f"Acme Robotics {marker}, Inc.",
        "description": "Python, FastAPI and PostgreSQL. Remote within the US.",
        "location": "Remote — US",
        "work_arrangement": WorkArrangement.REMOTE,
        "employment_type": EmploymentType.FULL_TIME,
    }
    values.update(overrides)
    return RawPosting(**values)


def resume_document() -> ResumeDocument:
    """A small, complete resume the zero-dependency Markdown template can render."""
    return ResumeDocument(
        contact=Contact(name="Ada Lovelace", email="ada@example.com"),
        summary="Backend engineer.",
        sections=[
            ResumeSection(
                heading="Experience",
                entries=[
                    ResumeEntry(
                        title="Backend Engineer",
                        organization="Acme Robotics",
                        date_range="2022 — 2024",
                        bullets=["Cut p95 latency by 40%."],
                        fact_ids=["f1"],
                    )
                ],
            )
        ],
        skills_line="Python, Redis",
    )


class RecordingProvider:
    """An ATS provider whose ``apply`` outcome the test chooses.

    Attributes:
        calls: How many times ``apply`` was reached.
    """

    #: Seconds ``apply`` spends before returning, so the histogram cannot record a zero.
    duration_seconds: float = 0.01

    def __init__(self, *, raises: BaseException | None = None) -> None:
        """Configure the outcome.

        Args:
            raises: Raise this instead of returning, to exercise a failure path.
        """
        self.calls = 0
        self._raises = raises

    async def apply(self, _context: Any) -> Any:
        """Record the attempt, spend measurable time, then succeed or raise as configured."""
        self.calls += 1
        await asyncio.sleep(self.duration_seconds)
        if self._raises is not None:
            raise self._raises
        from app.jobs.base import ApplyResult

        return ApplyResult(ok=True, status=ApplicationStatus.SUBMITTED)


class StubAnalyzer:
    """A knowledge analyzer with a fixed result, so the indexer is what is under test.

    Attributes:
        analyze_calls: How many times the full analysis ran.
    """

    name = "stub"
    source_kinds = frozenset({SourceKind.GITHUB_REPO})

    #: The source every document and fingerprint refers to.
    uri = "https://github.com/ada/analytical-engine"

    def __init__(self) -> None:
        """Start unanalyzed."""
        self.analyze_calls = 0

    async def analyze(self, _source: SourceRef) -> AnalysisResult:
        """Return one document and one fact."""
        self.analyze_calls += 1
        return AnalysisResult(
            documents=[
                ExtractedDocument(
                    uri=self.uri,
                    title="analytical-engine",
                    text=(
                        "A Python implementation of the analytical engine, built with "
                        "FastAPI and PostgreSQL and handling 12000 requests per second."
                    ),
                    kind=SourceKind.GITHUB_REPO,
                )
            ],
            facts=[
                ExtractedFact(
                    kind=FactKind.ACCOMPLISHMENT,
                    text="Built an analytical engine handling 12000 requests per second.",
                    skills=["Python"],
                    technologies=["Python"],
                    confidence=0.8,
                )
            ],
            fingerprint="fp-1",
        )

    async def fingerprint(self, _source: SourceRef) -> str:
        """Return a fixed fingerprint, so the second pass takes the skip path."""
        return "fp-1"

    def supports(self, _source: SourceRef) -> bool:
        """Accept anything; resolution is patched, not exercised."""
        return True

    async def healthcheck(self) -> bool:
        """Always healthy."""
        return True


@pytest.fixture
def analyzer() -> StubAnalyzer:
    """The stub analyzer, shared by the indexer and the assertions."""
    return StubAnalyzer()


@pytest.fixture
def indexer(session, settings, analyzer, monkeypatch) -> KnowledgeIndexer:
    """An indexer wired to the stub analyzer, an in-memory vector store and a private cache."""
    from app.ai.embeddings import HashingEmbedder
    from app.cache.memory import MemoryCache

    monkeypatch.setattr(indexer_module, "analyzer_for", lambda _ref: analyzer)
    return KnowledgeIndexer(
        session,
        settings,
        embedder=HashingEmbedder(),
        vector_store=InMemoryVectorStore(),
        cache=MemoryCache(),
    )


@pytest.fixture
async def source(indexer, user, analyzer):
    """A registered GitHub source, not yet indexed."""
    return await indexer.add_source(
        user.id,
        SourceRef(kind=SourceKind.GITHUB_REPO, uri=analyzer.uri, label="analytical-engine"),
    )


@pytest.fixture
def submittable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub document materialisation so a submit test reaches the provider."""

    async def _materialize(self, application, posting):
        return None, None

    monkeypatch.setattr(Pipeline, "_materialize_documents", _materialize)


# ======================================================================================
# 1. Discovery — postings discovered and deduped
# ======================================================================================


async def test_a_new_posting_is_counted_as_discovered(session, monkeypatch) -> None:
    """``applicantos_postings_discovered_total{provider}`` counts what a provider returned."""
    discovered = spy_on(dedupe_module, "record_posting_discovered", monkeypatch)
    deduped = spy_on(dedupe_module, "record_posting_deduped", monkeypatch)

    _posting, created = await DedupeService(session).upsert(raw_posting())

    assert created is True
    assert discovered.count == 1
    assert discovered.first_labels[0] is ATSProviderName.GREENHOUSE
    assert deduped.count == 0, "a first sighting was counted as a duplicate"


async def test_a_repeated_posting_is_counted_as_discovered_and_deduped(
    session, monkeypatch
) -> None:
    """The deduped counter is a *subset* of the discovered one, as §16's help text says."""
    service = DedupeService(session)
    raw = raw_posting()
    await service.upsert(raw)

    discovered = spy_on(dedupe_module, "record_posting_discovered", monkeypatch)
    deduped = spy_on(dedupe_module, "record_posting_deduped", monkeypatch)

    _posting, created = await service.upsert(raw)

    assert created is False
    assert discovered.count == 1, "a re-sighting is still something the provider returned"
    assert deduped.count == 1
    assert deduped.first_labels[0] is ATSProviderName.GREENHOUSE


async def test_the_provider_is_the_only_posting_label(session, monkeypatch) -> None:
    """Cardinality: nothing posting-specific may ever reach a label."""
    discovered = spy_on(dedupe_module, "record_posting_discovered", monkeypatch)

    await DedupeService(session).upsert(raw_posting())

    args, kwargs = discovered.calls[0]
    assert kwargs == {}
    assert len(args) == 1, "an extra label dimension would multiply the series"
    assert str(args[0]) in {member.value for member in ATSProviderName}


async def test_a_broken_counter_does_not_lose_a_posting(session, monkeypatch) -> None:
    """Telemetry is never load-bearing: ingestion survives a collector that raises."""
    break_collector("POSTINGS_DISCOVERED", monkeypatch)
    break_collector("POSTINGS_DEDUPED", monkeypatch)

    posting, created = await DedupeService(session).upsert(raw_posting())

    assert created is True
    assert posting.id is not None


# ======================================================================================
# 2. Scoring
# ======================================================================================


async def test_scoring_counts_every_posting_by_verdict(
    session, settings, user, make_posting, monkeypatch
) -> None:
    """``applicantos_scores_total{verdict}`` is produced by the real scoring pass."""
    scores = spy_on(discovery_module, "record_score", monkeypatch)

    first = await make_posting()
    second = await make_posting()

    counted = await DiscoveryService(session, settings).score_new(user.id, [first.id, second.id])

    assert counted == 2
    assert scores.count >= 1, "score_new produced no applicantos_scores_total sample"
    assert sum(call[0][1] for call in scores.calls) == 2, "the tally does not add up"
    verdicts = {str(call[0][0]) for call in scores.calls}
    assert verdicts <= {"apply", "review", "skip"}, f"unbounded verdict label: {verdicts}"


async def test_scoring_nothing_records_nothing(session, settings, user, monkeypatch) -> None:
    """An empty batch must not invent a sample; a flat line is meaningful here."""
    scores = spy_on(discovery_module, "record_score", monkeypatch)

    counted = await DiscoveryService(session, settings).score_new(user.id, [])

    assert counted == 0
    assert scores.count == 0


# ======================================================================================
# 3. Applications — the transition chokepoint
# ======================================================================================


async def test_every_transition_counts_the_application(session, application, monkeypatch) -> None:
    """**Mutation target.** Remove the call in ``transition`` and this goes red.

    ``transition`` is the single sanctioned mutation point for ``applications.status``, so it
    is the only place a status counter can be produced without missing a path.
    """
    counted = spy_on(application_module, "record_application", monkeypatch)

    await ApplicationService(session).transition(application, ApplicationStatus.SUBMITTING)

    assert counted.count == 1
    status, provider = counted.first_labels
    assert status is ApplicationStatus.SUBMITTING
    assert provider == ATSProviderName.GREENHOUSE.value


async def test_the_counter_follows_the_state_machine(session, application, monkeypatch) -> None:
    """Three moves, three samples — including the escalation to review."""
    counted = spy_on(application_module, "record_application", monkeypatch)
    service = ApplicationService(session)

    await service.transition(application, ApplicationStatus.SUBMITTING)
    await service.transition(
        application, ApplicationStatus.NEEDS_REVIEW, reason=ReviewReason.CAPTCHA
    )
    await service.transition(application, ApplicationStatus.READY)

    assert [call[0][0] for call in counted.calls] == [
        ApplicationStatus.SUBMITTING,
        ApplicationStatus.NEEDS_REVIEW,
        ApplicationStatus.READY,
    ]


async def test_the_provider_label_survives_a_freshly_created_application(
    session, user, make_posting, monkeypatch
) -> None:
    """An application ``create_or_get`` just inserted has no loaded ``posting``.

    That is the row ``Pipeline.prepare`` transitions, and it is worth its own test because
    the safe answer — ``unknown`` — is quietly wrong: the Grafana panel filters on
    ``provider=~"$provider"``, so an unlabelled sample vanishes from the chart instead of
    showing up as an oddity. It was observed as ``provider="unknown"`` on a live scrape
    before the identity-map fallback was added.
    """
    target = await make_posting()
    service = ApplicationService(session)

    fresh, created = await service.create_or_get(user.id, target.id)
    assert created is True
    assert "posting" in sa_inspect(fresh).unloaded, "the path this test exists for is gone"

    counted = spy_on(application_module, "record_application", monkeypatch)
    await service.transition(fresh, ApplicationStatus.PREPARING)

    assert counted.first_labels[1] == ATSProviderName.GREENHOUSE.value


async def test_a_refused_transition_counts_nothing(
    session, make_application, posting, monkeypatch
) -> None:
    """A move the state machine rejects never happened, so it must not be counted."""
    submitted = await make_application(posting, status=ApplicationStatus.SUBMITTED)
    counted = spy_on(application_module, "record_application", monkeypatch)

    with pytest.raises(InvalidTransition):
        await ApplicationService(session).transition(submitted, ApplicationStatus.READY)

    assert counted.count == 0, "a refused transition incremented the counter"


async def test_a_broken_counter_does_not_block_a_transition(
    session, application, monkeypatch
) -> None:
    """Telemetry is never load-bearing: the status change still commits and persists."""
    break_collector("APPLICATIONS", monkeypatch)

    await ApplicationService(session).transition(application, ApplicationStatus.SUBMITTING)

    await session.refresh(application)
    assert application.status is ApplicationStatus.SUBMITTING


# ======================================================================================
# 4. Apply duration
# ======================================================================================


async def test_a_successful_submission_observes_the_apply_duration(
    session,
    submission_allowed,
    submittable,
    monkeypatch,
    make_posting,
    make_score,
    make_application,
) -> None:
    """**Mutation target.** Remove the call in ``submit`` and this goes red."""
    observed = spy_on(pipeline_module, "observe_apply", monkeypatch)

    posting = await make_posting()
    await make_score(posting, normalized=91)
    application = await make_application(posting, status=ApplicationStatus.READY)

    provider = RecordingProvider()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: provider))

    result = await Pipeline(session, submission_allowed).submit(application.id)

    assert provider.calls == 1, "the test did not reach the provider, so it proves nothing"
    assert result.submitted is True
    assert observed.count == 1
    name, seconds = observed.first_labels
    assert name == ATSProviderName.GREENHOUSE.value
    assert seconds > 0.0, "a zero duration means the histogram was not timing the attempt"


async def test_a_failed_attempt_still_observes_the_apply_duration(
    session,
    submission_allowed,
    submittable,
    monkeypatch,
    make_posting,
    make_score,
    make_application,
) -> None:
    """An apply that burns time and then escalates is exactly what the histogram is for."""
    from app.jobs.base import ProviderError

    observed = spy_on(pipeline_module, "observe_apply", monkeypatch)

    posting = await make_posting()
    await make_score(posting, normalized=91)
    application = await make_application(posting, status=ApplicationStatus.READY)

    provider = RecordingProvider(raises=ProviderError("the form never loaded"))
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: provider))

    result = await Pipeline(session, submission_allowed).submit(application.id)

    assert provider.calls == 1
    assert result.submitted is False
    assert observed.count == 1, "a failed attempt was not timed"
    assert observed.first_labels[1] > 0.0


async def test_a_cancelled_attempt_still_observes_the_apply_duration(
    session,
    submission_allowed,
    submittable,
    monkeypatch,
    make_posting,
    make_score,
    make_application,
) -> None:
    """Cancellation unwinds through the same ``finally``; the time was still spent."""
    observed = spy_on(pipeline_module, "observe_apply", monkeypatch)

    posting = await make_posting()
    await make_score(posting, normalized=91)
    application = await make_application(posting, status=ApplicationStatus.READY)

    provider = RecordingProvider(raises=asyncio.CancelledError())
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: provider))

    with pytest.raises(asyncio.CancelledError):
        await Pipeline(session, submission_allowed).submit(application.id)

    assert observed.count == 1


async def test_a_guard_that_refuses_before_the_provider_observes_nothing(
    session, settings, monkeypatch, make_posting, make_score, make_application
) -> None:
    """No attempt was made, so timing one would be a lie — the kill switch is closed here."""
    observed = spy_on(pipeline_module, "observe_apply", monkeypatch)

    posting = await make_posting()
    await make_score(posting, normalized=91)
    application = await make_application(posting, status=ApplicationStatus.READY)

    provider = RecordingProvider()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: provider))

    result = await Pipeline(session, settings).submit(application.id)

    assert provider.calls == 0
    assert result.submitted is False
    assert observed.count == 0


async def test_a_broken_histogram_does_not_fail_a_submission(
    session,
    submission_allowed,
    submittable,
    monkeypatch,
    make_posting,
    make_score,
    make_application,
) -> None:
    """The headline promise: a Prometheus error must not fail an application submission."""
    break_collector("APPLY_DURATION", monkeypatch)

    posting = await make_posting()
    await make_score(posting, normalized=91)
    application = await make_application(posting, status=ApplicationStatus.READY)

    provider = RecordingProvider()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: provider))

    result = await Pipeline(session, submission_allowed).submit(application.id)

    assert provider.calls == 1
    assert result.submitted is True


# ======================================================================================
# 5. Document renders
# ======================================================================================


async def test_a_successful_render_is_counted_with_its_engine(
    tmp_path: Path, monkeypatch
) -> None:
    """``applicantos_documents_rendered_total{engine,outcome}`` on the happy path."""
    rendered = spy_on(renderer_module, "record_document_rendered", monkeypatch)

    result = await render_resume(
        resume_document(), tmp_path / "resume.md", template="markdown", fmt="md"
    )

    assert result.path.is_file()
    assert rendered.count == 1
    engine, outcome = rendered.first_labels
    assert engine == result.engine
    assert outcome == "success"


async def test_a_failed_render_is_counted_too(tmp_path: Path, monkeypatch) -> None:
    """The failure path is the half a success-only counter silently hides."""
    rendered = spy_on(renderer_module, "record_document_rendered", monkeypatch)

    with pytest.raises(DocumentRenderError):
        await render_resume(
            resume_document(), tmp_path / "resume.pdf", template="markdown", fmt="pdf"
        )

    assert rendered.count == 1
    engine, outcome = rendered.first_labels
    assert outcome == "failure"
    assert engine == "unknown", "a render that never reached an engine must say so"


async def test_the_shrink_ladder_counts_one_document_not_one_rung(
    tmp_path: Path, monkeypatch
) -> None:
    """Meaning as much as cardinality: the series counts documents, not internal retries."""
    rendered = spy_on(renderer_module, "record_document_rendered", monkeypatch)

    document = resume_document()
    document.sections[0].entries[0].bullets.extend(
        f"Shipped subsystem {index}." for index in range(60)
    )

    await render_resume(document, tmp_path / "long.md", template="markdown", fmt="md")

    assert rendered.count == 1


async def test_a_cover_letter_render_is_counted(tmp_path: Path, monkeypatch) -> None:
    """The second render entry point produces the same series."""
    rendered = spy_on(renderer_module, "record_document_rendered", monkeypatch)

    result = await render_cover_letter(
        "Dear hiring team,\n\nI would like to apply.",
        Contact(name="Ada Lovelace", email="ada@example.com"),
        tmp_path / "letter.md",
        template="markdown",
        fmt="md",
    )

    assert rendered.count == 1
    engine, outcome = rendered.first_labels
    assert engine == result.engine
    assert outcome == "success"


async def test_a_broken_counter_does_not_lose_the_rendered_file(
    tmp_path: Path, monkeypatch
) -> None:
    """Telemetry is never load-bearing: the document still exists on disk."""
    break_collector("DOCUMENTS_RENDERED", monkeypatch)

    result = await render_resume(
        resume_document(), tmp_path / "resume.md", template="markdown", fmt="md"
    )

    assert result.path.is_file()


# ======================================================================================
# 6. Knowledge indexing
# ======================================================================================


async def test_indexing_counts_its_documents_and_times_the_pass(
    indexer, source, monkeypatch
) -> None:
    """Both §16 knowledge series come out of one ``index_source`` call."""
    documents = spy_on(indexer_module, "record_knowledge_document", monkeypatch)
    timed = spy_on(indexer_module, "observe_knowledge_index", monkeypatch)

    report = await indexer.index_source(source.id)

    assert report.failed is False
    assert report.documents == 1
    assert sum(call[0][1] for call in documents.calls) == report.documents
    assert documents.first_labels[0] is SourceKind.GITHUB_REPO
    assert timed.count == 1
    analyzer_name, seconds = timed.first_labels
    assert analyzer_name == "stub"
    assert seconds >= 0.0


async def test_a_skipped_pass_is_still_timed_but_indexes_nothing(
    indexer, source, monkeypatch
) -> None:
    """The fingerprint skip is the millisecond case the bucket ladder was built for."""
    await indexer.index_source(source.id)

    documents = spy_on(indexer_module, "record_knowledge_document", monkeypatch)
    timed = spy_on(indexer_module, "observe_knowledge_index", monkeypatch)

    report = await indexer.index_source(source.id)

    assert report.skipped is True
    assert documents.count == 0, "a skipped pass counted documents it never touched"
    assert timed.count == 1
    assert timed.first_labels[0] == "stub"


async def test_a_failed_pass_is_timed(indexer, source, analyzer, monkeypatch) -> None:
    """A pass that failed after five minutes is the one an operator most wants to see."""

    async def _explode(_ref):
        raise RuntimeError("github is unreachable")

    monkeypatch.setattr(analyzer, "analyze", _explode)
    timed = spy_on(indexer_module, "observe_knowledge_index", monkeypatch)

    report = await indexer.index_source(source.id)

    assert report.failed is True
    assert timed.count == 1
    assert timed.first_labels[0] == "stub"


async def test_a_broken_collector_does_not_fail_an_index(indexer, source, monkeypatch) -> None:
    """Telemetry is never load-bearing: the source still indexes."""
    break_collector("KNOWLEDGE_DOCUMENTS", monkeypatch)
    break_collector("KNOWLEDGE_INDEX_DURATION", monkeypatch)

    report = await indexer.index_source(source.id)

    assert report.failed is False
    assert report.documents == 1


# ======================================================================================
# 7. The exposition — what a scrape actually sees
# ======================================================================================


async def test_the_funnel_reaches_the_scrape_with_non_zero_values(
    session,
    settings,
    user,
    make_posting,
    application,
    tmp_path: Path,
) -> None:
    """The end-to-end claim: drive the funnel, then parse the bytes ``/metrics`` serves.

    No spies. Every recorder runs for real against the process-wide registry, and the
    assertions are on the rendered exposition — because a counter that is registered but
    never incremented is precisely the defect these producers exist to close.
    """
    before = {
        "discovered": series_value("applicantos_postings_discovered_total", provider="greenhouse"),
        "deduped": series_value("applicantos_postings_deduped_total", provider="greenhouse"),
        "scores": series_value("applicantos_scores_total"),
        "applications": series_value(
            "applicantos_applications_total", status="submitting", provider="greenhouse"
        ),
        "renders": series_value("applicantos_documents_rendered_total", outcome="success"),
    }

    raw = raw_posting()
    dedupe = DedupeService(session)
    await dedupe.upsert(raw)
    await dedupe.upsert(raw)

    posting = await make_posting()
    await DiscoveryService(session, settings).score_new(user.id, [posting.id])

    await ApplicationService(session).transition(application, ApplicationStatus.SUBMITTING)

    await render_resume(resume_document(), tmp_path / "resume.md", template="markdown", fmt="md")

    after = {
        "discovered": series_value("applicantos_postings_discovered_total", provider="greenhouse"),
        "deduped": series_value("applicantos_postings_deduped_total", provider="greenhouse"),
        "scores": series_value("applicantos_scores_total"),
        "applications": series_value(
            "applicantos_applications_total", status="submitting", provider="greenhouse"
        ),
        "renders": series_value("applicantos_documents_rendered_total", outcome="success"),
    }

    assert after["discovered"] - before["discovered"] == 2.0
    assert after["deduped"] - before["deduped"] == 1.0
    assert after["scores"] - before["scores"] == 1.0
    assert after["applications"] - before["applications"] == 1.0
    assert after["renders"] - before["renders"] == 1.0
    assert all(value > 0.0 for value in after.values()), "a funnel series scraped as zero"


async def test_the_apply_histogram_reaches_the_scrape(
    session,
    submission_allowed,
    submittable,
    monkeypatch,
    make_posting,
    make_score,
    make_application,
) -> None:
    """A histogram is only useful if its ``_count`` and ``_sum`` both move."""
    before_count = series_value("applicantos_apply_duration_seconds_count", provider="greenhouse")
    before_sum = series_value("applicantos_apply_duration_seconds_sum", provider="greenhouse")

    posting = await make_posting()
    await make_score(posting, normalized=91)
    application = await make_application(posting, status=ApplicationStatus.READY)
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: RecordingProvider()))

    await Pipeline(session, submission_allowed).submit(application.id)

    assert (
        series_value("applicantos_apply_duration_seconds_count", provider="greenhouse")
        - before_count
        == 1.0
    )
    assert (
        series_value("applicantos_apply_duration_seconds_sum", provider="greenhouse") > before_sum
    )


# ======================================================================================
# 8. The standing anti-decoration guard
# ======================================================================================


def _calls_recorder(path: Path, recorder: str) -> bool:
    """Return whether *path* contains a real call to *recorder*.

    Parsed with :mod:`ast` rather than string-matched, for the same reason
    ``test_golden_plugin_isolation`` parses imports: a mention inside a docstring, a comment
    or an import statement must not count as a producer. That distinction is the whole
    subject of this file.

    Args:
        path: The module to scan.
        recorder: The recorder function's name.

    Returns:
        ``True`` when the module calls it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == recorder:
            return True
        if isinstance(func, ast.Attribute) and func.attr == recorder:
            return True
    return False


@pytest.mark.parametrize(("recorder", "relative"), sorted(DOMAIN_PRODUCERS.items()))
def test_the_domain_recorder_is_called_by_the_module_that_owns_it(
    recorder: str, relative: str
) -> None:
    """G12 as a standing check: none of these eight may go back to having no caller.

    The behavioural tests above are the real evidence. This one exists because the failure
    mode is *silent* — deleting a call breaks no feature, fails no type check, and shows up
    only as an empty Grafana panel weeks later.
    """
    module = Path(__file__).resolve().parent.parent / "app" / relative
    assert module.is_file(), f"{relative} does not exist"
    assert _calls_recorder(module, recorder), f"app/{relative} no longer calls {recorder}()"
