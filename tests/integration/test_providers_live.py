"""G1 — the providers, against the APIs they actually talk to.

Every other provider test in this repository runs against a recorded payload. That proves the
parsing is right; it cannot prove the *feed* is still the shape it was recorded from, and it
cannot notice a board that quietly stopped existing. Greenhouse could rename ``jobs`` to
``postings`` tomorrow and the whole unit suite would stay green while discovery returned
nothing forever.

So these tests hit the real thing. They are marked ``integration`` and therefore excluded from
the default run (``pytest`` alone stays hermetic and offline); ``.github/workflows/
integration.yml`` runs them nightly, on its own workflow, so that a provider outage reddens a
scheduled job rather than somebody's pull request.

**They assert on shape, never on a job.** Any specific posting will be gone next week, so
nothing here names a title, an identifier or a count. What is asserted is that at least one
posting parses and that the fields the pipeline genuinely depends on came back populated:
``external_id`` (``UNIQUE(provider, external_id)`` and the strongest dedupe signal), ``url``
(where the user is sent), ``title`` and ``company_name`` (what the scorer and the résumé
engine read), a non-empty ``description`` (what the résumé is tailored against), and a
``posted_at`` that is a real timezone-aware instant rather than a string the freshness filter
will silently ignore.

**A network failure skips; a schema failure fails.** The distinction is the point. A timeout
or a DNS error says nothing about the provider's contract and must not turn a nightly job red
for a laptop on a train, so it raises ``pytest.skip`` with the reason. A ``200`` whose body is
the wrong shape is exactly what this file exists to catch, and it fails.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from app.jobs.base import (
    ProviderError,
    RawPosting,
    SearchQuery,
    UnsupportedFlowError,
)
from app.jobs.registry import get_provider
from app.models.enums import ATSProviderName

pytestmark = pytest.mark.integration


# ======================================================================================
# The boards these tests point at
# ======================================================================================

#: Boards chosen for size and longevity rather than for interest: the bigger and older a
#: board is, the less likely a red test means "this employer stopped hiring" instead of
#: "the feed changed". Each one is in ``app.jobs.seeds`` and each was carrying hundreds of
#: postings when this file was written.
GREENHOUSE_BOARD = "stripe"
LEVER_COMPANY = "veeva"
ASHBY_BOARD = "ramp"
WORKDAY_TENANT = "nvidia"

#: A Lever token that is not a board. Lever answers ``404`` for one of these, which the
#: provider turns into a skipped board — the assertion is that a dead token costs nothing
#: more than that. Deliberately unregistrable-looking so it cannot start existing.
LEVER_DEAD_COMPANY = "applicantos-not-a-real-lever-board"

#: Postings requested per live test. Small on purpose: this is a contract check, not a crawl,
#: and every one of these requests is against somebody else's unauthenticated API.
LIVE_LIMIT = 5

#: Freshness window for the live queries. Wide enough that a board with a slow month still
#: returns something, narrow enough to stay a realistic query.
LIVE_POSTED_WITHIN_DAYS = 365

#: The earliest ``posted_at`` that can be believed. A provider that hands back the Unix epoch
#: because a date failed to parse would otherwise sail through a "not None" assertion.
EARLIEST_CREDIBLE_POSTING = datetime(2000, 1, 1, tzinfo=UTC)

#: Header names that would mean a request carried a credential.
CREDENTIAL_HEADERS = ("authorization", "cookie", "x-li-identity", "csrf-token")


def live_query(**overrides: Any) -> SearchQuery:
    """Build the query the live tests issue.

    Args:
        **overrides: Fields to replace on the default query.

    Returns:
        A deliberately unrestrictive query — no keywords, no locations — so that a red test
        means the *feed* changed rather than that nobody happens to be hiring a "software
        engineer" on that board this week.
    """
    fields: dict[str, Any] = {
        "limit": LIVE_LIMIT,
        "posted_within_days": LIVE_POSTED_WITHIN_DAYS,
    }
    fields.update(overrides)
    return SearchQuery(**fields)


async def collect(stream: AsyncIterator[RawPosting], limit: int = LIVE_LIMIT) -> list[RawPosting]:
    """Drain a provider's ``search`` stream, turning a network failure into a skip.

    Args:
        stream: The provider's async iterator.
        limit: Stop after this many postings.

    Returns:
        The postings, at most *limit* of them.

    Raises:
        Skipped: When the provider raised a *transient* :class:`~app.jobs.base.ProviderError`
            — a timeout, a transport failure, a 5xx, a rate limit. None of those says the
            contract changed, and a test that failed on them would be red for reasons the
            code cannot fix. A non-transient failure is re-raised, because a 400 or a body
            that would not decode is precisely the signal this file exists to carry.
    """
    postings: list[RawPosting] = []
    try:
        async for raw in stream:
            postings.append(raw)
            if len(postings) >= limit:
                break
    except ProviderError as exc:
        if exc.transient:
            pytest.skip(f"provider unreachable ({type(exc).__name__}: {exc})")
        raise
    return postings


def assert_pipeline_fields(raw: RawPosting, provider: ATSProviderName) -> None:
    """Assert that one live posting carries everything the pipeline reads from it.

    Args:
        raw: A posting straight out of the provider.
        provider: The provider it must claim to come from.

    Raises:
        AssertionError: If any field the pipeline depends on is missing or malformed. The
            messages name the field and the provider, because the first thing a reader of a
            nightly failure needs to know is which board changed and what about it.
    """
    label = f"{provider.value}:{raw.external_id or '<no id>'}"

    assert raw.provider is provider, f"{label}: provider is {raw.provider!r}"
    assert raw.external_id, f"{label}: external_id is empty — UNIQUE(provider, external_id) fails"
    assert raw.url.startswith("http"), f"{label}: url is not absolute: {raw.url!r}"
    assert raw.title, f"{label}: title is empty"
    assert raw.company_name, f"{label}: company_name is empty"

    description = raw.description or ""
    assert description.strip(), f"{label}: description is empty — nothing to tailor a résumé to"

    posted_at = raw.posted_at
    assert isinstance(posted_at, datetime), f"{label}: posted_at is {posted_at!r}, not a datetime"
    assert posted_at.tzinfo is not None, f"{label}: posted_at {posted_at!r} is naive"
    assert posted_at > EARLIEST_CREDIBLE_POSTING, f"{label}: posted_at {posted_at!r} is implausible"

    # `target_url` is what the browser layer opens. It has to be addressable whether or not
    # the feed supplied a separate apply URL.
    assert raw.target_url.startswith("http"), f"{label}: target_url is not absolute"


def assert_parses(postings: Sequence[RawPosting], provider: ATSProviderName) -> None:
    """Assert that a live board produced usable postings.

    Args:
        postings: What the provider yielded.
        provider: The provider under test.

    Raises:
        AssertionError: If the board yielded nothing, or if any posting is unusable.
    """
    assert postings, (
        f"{provider.value} returned no postings from a board that should have many — either "
        "the seed token is dead or the feed's shape changed"
    )
    for raw in postings:
        assert_pipeline_fields(raw, provider)


# ======================================================================================
# The three providers that can really submit
# ======================================================================================


async def test_greenhouse_board_parses() -> None:
    """A live Greenhouse board yields postings the pipeline can use."""
    provider = get_provider(ATSProviderName.GREENHOUSE)
    query = live_query(extra={"greenhouse": [GREENHOUSE_BOARD]})
    postings = await collect(provider.search(query))
    assert_parses(postings, ATSProviderName.GREENHOUSE)


async def test_lever_board_parses() -> None:
    """A live Lever board yields postings the pipeline can use."""
    provider = get_provider(ATSProviderName.LEVER)
    query = live_query(extra={"lever": [LEVER_COMPANY]})
    postings = await collect(provider.search(query))
    assert_parses(postings, ATSProviderName.LEVER)


async def test_ashby_board_parses() -> None:
    """A live Ashby board yields postings the pipeline can use."""
    provider = get_provider(ATSProviderName.ASHBY)
    query = live_query(extra={"ashby": [ASHBY_BOARD]})
    postings = await collect(provider.search(query))
    assert_parses(postings, ATSProviderName.ASHBY)


@pytest.mark.parametrize(
    "provider_name",
    [ATSProviderName.GREENHOUSE, ATSProviderName.LEVER, ATSProviderName.ASHBY],
)
async def test_healthcheck_answers_from_the_live_api(provider_name: ATSProviderName) -> None:
    """``GET /ready`` aggregates these, so a live one has to be able to say "yes"."""
    provider = get_provider(provider_name)
    assert await provider.healthcheck() is True, (
        f"{provider_name.value} healthcheck failed against the live API"
    )


async def test_a_dead_lever_token_costs_only_that_board() -> None:
    """A token that is not a Lever board must degrade to zero postings, never to an error.

    Seed tokens go stale — 28 of the 33 shipped before 2026-08-09 had — and the contract in
    ``app/jobs/seeds.py`` is that a stale one costs a wasted request and nothing else.
    """
    provider = get_provider(ATSProviderName.LEVER)
    query = live_query(extra={"lever": [LEVER_DEAD_COMPANY]})
    postings = await collect(provider.search(query))
    assert postings == []


async def test_fetch_posting_round_trips_a_live_url() -> None:
    """A URL from a live search can be handed back to the provider and re-fetched.

    This is the path the desktop app's "paste a job link" box and every re-score take, and it
    is where a URL-shape change shows up first — the search half can keep working while
    ``fetch_posting`` stops recognising the URLs the search half just produced.
    """
    provider = get_provider(ATSProviderName.GREENHOUSE)
    postings = await collect(provider.search(live_query(extra={"greenhouse": [GREENHOUSE_BOARD]})))
    assert_parses(postings, ATSProviderName.GREENHOUSE)

    try:
        refetched = await provider.fetch_posting(postings[0].url)
    except ProviderError as exc:
        if exc.transient:
            pytest.skip(f"provider unreachable ({type(exc).__name__}: {exc})")
        raise

    assert refetched is not None, f"fetch_posting could not resolve {postings[0].url!r}"
    assert refetched.external_id == postings[0].external_id
    assert_pipeline_fields(refetched, ATSProviderName.GREENHOUSE)


# ======================================================================================
# Workday — discovery only, and the CXS endpoint is the whole of it
# ======================================================================================


async def test_workday_cxs_endpoint_serves_a_real_tenant() -> None:
    """One real Workday tenant resolves and its CXS endpoint yields usable postings.

    Workday is the provider with the most to go wrong, because a tenant's shard and
    career-site name are not derivable and have to be discovered. When that discovery broke —
    the tenant root began answering ``406`` to every request — Workday returned nothing for
    every tenant and no test noticed, because none of them left the process. This one does.
    """
    provider = get_provider(ATSProviderName.WORKDAY)
    query = live_query(extra={"workday": [WORKDAY_TENANT]})
    postings = await collect(provider.search(query))
    assert_parses(postings, ATSProviderName.WORKDAY)

    external_id = postings[0].external_id
    assert WORKDAY_TENANT in external_id.lower(), (
        f"Workday external_id {external_id!r} does not carry its tenant — requisition ids are "
        "only unique within an employer, so UNIQUE(provider, external_id) needs both"
    )


async def test_workday_refuses_to_submit() -> None:
    """Workday's account-gated flow routes to a human, live or not (golden rule #10)."""
    provider = get_provider(ATSProviderName.WORKDAY)
    assert provider.supports_auto_apply is False


# ======================================================================================
# LinkedIn — the provider whose contract is what it does *not* do
# ======================================================================================


class RequestRecorder:
    """Records every outbound request a provider makes, without changing what it does.

    Attributes:
        calls: ``(method, url, headers)`` per request, in order.
    """

    def __init__(self, original: Any) -> None:
        """Wrap a provider's transport method.

        Args:
            original: The provider's bound ``_request``, delegated to after recording.
        """
        self._original = original
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def __call__(self, method: str, url: str, **kw: Any) -> Any:
        """Record one call and delegate to the real transport.

        Args:
            method: HTTP method.
            url: Absolute URL.
            **kw: Everything else the provider passed through.

        Returns:
            Whatever the real transport returned.
        """
        supplied = dict(kw.get("headers") or {})
        headers = {str(key).lower(): str(value) for key, value in supplied.items()}
        self.calls.append((method, url, headers))
        return await self._original(method, url, **kw)

    def describe(self) -> list[tuple[str, str]]:
        """Return the recorded calls as ``(method, url)`` pairs, for failure messages."""
        return [(method, url) for method, url, _ in self.calls]


def assert_no_credentialed_call(recorder: RequestRecorder) -> None:
    """Assert that nothing the recorder saw was a credentialed LinkedIn request.

    Args:
        recorder: The recorder wrapped around a provider's transport.

    Raises:
        AssertionError: If any request targeted LinkedIn's own servers, or carried one of
            :data:`CREDENTIAL_HEADERS`, or embedded credentials in its authority.
    """
    for method, url, headers in recorder.calls:
        lowered = url.lower()
        assert "linkedin.com" not in lowered, f"{method} {url} targets LinkedIn's own servers"
        assert "@" not in lowered.split("//", 1)[-1].split("/", 1)[0], (
            f"{method} {url} carries credentials in its authority"
        )
        for header in CREDENTIAL_HEADERS:
            assert header not in headers, f"{method} {url} carried a {header} header"


async def test_linkedin_constructs_no_credentialed_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LinkedIn discovery never talks to LinkedIn, and never carries a credential.

    Golden rule #10 in its strictest form. LinkedIn's terms prohibit automated scraping and
    submission, so this provider reads a user-supplied export or a public feed and nothing
    else. The assertion is behavioural rather than declarative: the transport is wrapped and
    every request the provider would have made is inspected — first in the state a fresh
    install is in (no source configured at all), then when handed a feed URL with a username
    and password embedded in it, which is the shape a "just put your session in the URL"
    workaround would take.
    """
    provider = get_provider(ATSProviderName.LINKEDIN)
    recorder = RequestRecorder(provider._request)
    monkeypatch.setattr(provider, "_request", recorder)

    unconfigured = await collect(provider.search(live_query()))
    assert unconfigured == [], "LinkedIn discovered postings with no source configured"
    assert recorder.calls == [], (
        f"LinkedIn made an outbound request with no source configured: {recorder.describe()}"
    )

    credentialed = await collect(
        provider.search(
            live_query(extra={"feed_url": "https://user:secret@www.linkedin.com/jobs/feed.rss"})
        )
    )
    assert credentialed == []
    assert recorder.calls == [], (
        f"LinkedIn fetched a credentialed feed URL instead of refusing it: {recorder.describe()}"
    )
    assert_no_credentialed_call(recorder)


async def test_linkedin_refuses_to_submit() -> None:
    """``apply`` raises rather than automating a flow the terms prohibit."""
    from app.jobs.base import ApplyContext, JobPostingDTO, UserProfileDTO

    provider = get_provider(ATSProviderName.LINKEDIN)
    assert provider.supports_auto_apply is False

    context = ApplyContext(
        application_id=uuid.uuid4(),
        posting=JobPostingDTO(
            provider=ATSProviderName.LINKEDIN,
            external_id="4000000000",
            url="https://www.linkedin.com/jobs/view/4000000000/",
            title="Embedded Software Engineer",
        ),
        user=UserProfileDTO(full_name="Ada Lovelace", email="ada@example.invalid"),
        dry_run=True,
    )

    with pytest.raises(UnsupportedFlowError):
        await provider.apply(context)
