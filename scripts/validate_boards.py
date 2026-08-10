#!/usr/bin/env python3
"""Validate every board token in :data:`app.jobs.seeds.DEFAULT_BOARDS` against its live API.

    python -m scripts.validate_boards
    python -m scripts.validate_boards --provider lever
    python -m scripts.validate_boards --provider lever --tokens sardine,anthropic
    python -m scripts.validate_boards --json boards.json

``app/jobs/seeds.py`` is the reason discovery finds anything at all on a fresh install: no
ATS in this project offers a global "search every employer" endpoint, so a provider with no
tokens has nowhere to look. That makes the seed lists a *functional* dependency, and an
untested one — a token stops working the day the employer migrates ATS, renames its board or
stops hiring, and nothing in the unit suite can notice because every provider test runs
against a recorded payload.

**The specific failure this script exists to catch.** Measured on 2026-08-09, before any of
it was fixed: **28 of the 33 shipped Lever tokens, 11 of 40 Greenhouse, 7 of 34 Ashby and all
37 Workday tenants** discovered nothing. Lever discovery in particular was returning zero on
every install, permanently, and saying nothing about it.

A dead board comes in two flavours and only one of them is honest. Most answer ``404``
(``netflix`` and ``attentive`` on Lever; ``clay`` and ``glean`` on Ashby), which a provider
can log. The rest answer ``200`` with an empty array — ``highspot`` and ``plaid`` on Lever,
``mercury`` on Ashby — because they are real boards with nothing published, and *that* is
indistinguishable from "this employer is not hiring this week". Both count as dead here: a
token that discovers nothing is not earning its request. See
:data:`app.jobs.lever.EVENT_BOARD_EMPTY` for the log line that at least makes the second kind
visible at runtime.

**How each provider is counted.**

* **Greenhouse** — one ``GET`` of the board's jobs feed; the whole roster comes back in a
  single document.
* **Ashby** — the same, one ``GET`` of the posting-api job board.
* **Lever** — offset-paginated, so pages of :data:`LEVER_PAGE_SIZE` are read until a short
  one arrives or :data:`MAX_LEVER_PAGES` is reached. A board that hits the ceiling is
  reported as truncated rather than as an exact number.
* **Workday** — has no shard-independent feed URL (the ``wd<N>`` shard and the career-site
  name are per tenant and have to be discovered), so it is validated through the real
  :class:`~app.jobs.base.ATSProvider` obtained from :mod:`app.jobs.registry`, asking for at
  most :data:`WORKDAY_SAMPLE_LIMIT` postings. The count is therefore a *sample*: it answers
  "does this tenant still serve postings", not "how many". It is also the one probe here that
  exercises a whole provider rather than a URL template — which is how it caught the tenant
  root beginning to answer ``406`` and taking every Workday resolution down with it.

  **Resolutions are cached, including the negative ones** (six hours, per
  ``app.jobs.workday.SITE_MISS_CACHE_TTL_SECONDS``). A sweep run after a resolution bug is
  fixed will happily replay the failures the broken version recorded, so clear the cache
  directory before trusting a Workday re-run.

**Politeness.** These are other people's APIs and none of this is authenticated. Every
request is sequential, separated by :data:`DEFAULT_DELAY_SECONDS`, and identifies itself with
:data:`USER_AGENT` — the product's own agent string plus this script's name, so an operator
reading their access log can tell exactly what the traffic is.

Exits ``0`` when every polled token returned at least one posting and ``1`` when any token is
dead, so it can gate a release or run on a schedule.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from app.jobs.base import USER_AGENT as PRODUCT_USER_AGENT
from app.jobs.seeds import SEED_API_TEMPLATES, default_boards
from app.models.enums import ATSProviderName

__all__ = [
    "DEFAULT_DELAY_SECONDS",
    "LEVER_PAGE_SIZE",
    "MAX_LEVER_PAGES",
    "REQUEST_TIMEOUT_SECONDS",
    "STATUS_EMPTY",
    "STATUS_ERROR",
    "STATUS_LIVE",
    "STATUS_MISSING",
    "USER_AGENT",
    "WORKDAY_SAMPLE_LIMIT",
    "BoardResult",
    "ProbeFailure",
    "main",
    "validate_provider",
]


# ======================================================================================
# Request policy
# ======================================================================================

#: Sent on every request. The product's agent string plus this script's name: the traffic is
#: ours either way, and an operator who wants to tell a validation sweep apart from a real
#: discovery run should be able to.
USER_AGENT: Final[str] = f"{PRODUCT_USER_AGENT} scripts/validate_boards"

#: Seconds allowed for one request. Generous: a slow board is not a dead board, and a
#: false "error" here would get a working token deleted from the seed list.
REQUEST_TIMEOUT_SECONDS: Final[float] = 20.0

#: Pause between requests. Sequential plus a delay keeps a full sweep well inside anything a
#: public feed would consider abusive.
DEFAULT_DELAY_SECONDS: Final[float] = 0.4

#: Postings requested per Lever page. Matches ``app.jobs.lever.PAGE_SIZE`` so that the count
#: reported here is reached the same way discovery reaches it.
LEVER_PAGE_SIZE: Final[int] = 100

#: Pages read per Lever board before the count is reported as truncated. Ten pages is a
#: thousand postings — an order of magnitude past the largest seeded board.
MAX_LEVER_PAGES: Final[int] = 10

#: Postings asked for when sampling a Workday tenant. Each one costs a detail request, and
#: the question being answered is "does this board still serve postings", not "how many".
WORKDAY_SAMPLE_LIMIT: Final[int] = 3

#: HTTP statuses that mean the board is not there. Greenhouse and Ashby use them; Lever does
#: not, which is the whole reason :data:`STATUS_EMPTY` has to exist as a separate verdict.
MISSING_STATUS_CODES: Final[frozenset[int]] = frozenset({404, 410})


# ======================================================================================
# Verdicts
# ======================================================================================

#: The board answered and carries at least one posting.
STATUS_LIVE: Final[str] = "live"

#: The board answered and carries nothing. On Lever this is indistinguishable from an unknown
#: company, which is why it counts as dead for the purposes of the seed list.
STATUS_EMPTY: Final[str] = "empty"

#: The provider said the board does not exist.
STATUS_MISSING: Final[str] = "missing"

#: The request failed for a reason that says nothing about the board — a timeout, a 5xx, a
#: body that was not JSON. Never grounds for deleting a token.
STATUS_ERROR: Final[str] = "error"

#: Verdicts that mean "this token discovers nothing today".
DEAD_STATUSES: Final[frozenset[str]] = frozenset({STATUS_EMPTY, STATUS_MISSING})

#: Providers reachable through a plain public JSON feed, in report order.
FEED_PROVIDERS: Final[tuple[str, ...]] = (
    ATSProviderName.GREENHOUSE.value,
    ATSProviderName.LEVER.value,
    ATSProviderName.ASHBY.value,
)

#: Every provider this script can validate, in report order.
VALIDATABLE_PROVIDERS: Final[tuple[str, ...]] = (
    *FEED_PROVIDERS,
    ATSProviderName.WORKDAY.value,
)

GREEN: Final[str] = "\033[32m"
YELLOW: Final[str] = "\033[33m"
RED: Final[str] = "\033[31m"
DIM: Final[str] = "\033[2m"
RESET: Final[str] = "\033[0m"

#: Colour per verdict, for the table.
_VERDICT_COLOURS: Final[dict[str, str]] = {
    STATUS_LIVE: GREEN,
    STATUS_EMPTY: RED,
    STATUS_MISSING: RED,
    STATUS_ERROR: YELLOW,
}


class ProbeFailure(Exception):
    """A request failed in a way that says nothing about whether the board exists.

    Distinguished from an empty or missing board on purpose: a timeout must never be the
    reason a working token is deleted from ``app/jobs/seeds.py``.
    """


@dataclass(slots=True)
class BoardResult:
    """What one board token answered with.

    Attributes:
        provider: The provider the token belongs to.
        token: The token exactly as it appears in :data:`~app.jobs.seeds.DEFAULT_BOARDS`.
        status: One of :data:`STATUS_LIVE`, :data:`STATUS_EMPTY`, :data:`STATUS_MISSING`,
            :data:`STATUS_ERROR`.
        count: Postings the board returned. ``0`` for every non-live verdict.
        truncated: Whether the count stopped at a page or sample ceiling rather than at the
            end of the board, so ``count`` is a floor rather than a total.
        detail: Human-readable explanation, populated for errors and missing boards.
        seconds: Wall-clock time the probe took.
    """

    provider: str
    token: str
    status: str
    count: int = 0
    truncated: bool = False
    detail: str = ""
    seconds: float = 0.0

    @property
    def dead(self) -> bool:
        """Whether this token discovers nothing today."""
        return self.status in DEAD_STATUSES

    def as_dict(self) -> dict[str, Any]:
        """Return the result as a JSON-ready mapping."""
        return {
            "provider": self.provider,
            "token": self.token,
            "status": self.status,
            "count": self.count,
            "truncated": self.truncated,
            "detail": self.detail,
            "seconds": round(self.seconds, 3),
        }


# ======================================================================================
# Transport
# ======================================================================================


def _fetch_json(url: str, *, timeout: float) -> tuple[int, Any]:
    """``GET`` *url* and decode the body as JSON.

    Uses :mod:`urllib` rather than ``httpx`` so that this script has exactly the same
    dependency footprint as ``scripts/smoke_test.py``: it must be runnable against a bare
    checkout, because "is our seed list still alive?" is a question worth answering before
    anything is installed.

    Args:
        url: Absolute URL to request.
        timeout: Seconds allowed for the request.

    Returns:
        ``(status_code, payload)``. The payload is ``None`` for an error status, whose body
        is not worth decoding.

    Raises:
        ProbeFailure: On a transport failure, a timeout, or a 2xx body that is not JSON.
    """
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            body = response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProbeFailure(f"transport failure: {exc}") from exc

    try:
        return status, json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProbeFailure(f"non-JSON body from {url}: {exc}") from exc


def _feed_url(provider: str, token: str, params: Mapping[str, Any] | None = None) -> str:
    """Build one provider's public feed URL for *token*.

    Args:
        provider: The provider name.
        token: The board token.
        params: Query parameters to append.

    Returns:
        The absolute URL, taken from :data:`~app.jobs.seeds.SEED_API_TEMPLATES` so that this
        script validates the URL shape the product actually ships.
    """
    url = SEED_API_TEMPLATES[provider].format(token=urllib.parse.quote(token, safe=""))
    if params:
        url = f"{url}?{urllib.parse.urlencode(dict(params))}"
    return url


# ======================================================================================
# Per-provider probes
# ======================================================================================


def _jobs_array(payload: Any) -> list[Any] | None:
    """Return the ``jobs`` array from a Greenhouse or Ashby envelope.

    Args:
        payload: The decoded response document.

    Returns:
        The array, or ``None`` when the document is not the expected envelope — which is a
        schema change, not an empty board, and must be reported as an error.
    """
    if not isinstance(payload, Mapping):
        return None
    jobs = payload.get("jobs")
    return jobs if isinstance(jobs, list) else None


def _probe_envelope(provider: str, token: str, *, timeout: float) -> BoardResult:
    """Probe a provider whose whole roster arrives in one ``{"jobs": [...]}`` document.

    Args:
        provider: ``greenhouse`` or ``ashby``.
        token: The board token.
        timeout: Seconds allowed for the request.

    Returns:
        The verdict for this token.
    """
    started = time.monotonic()
    url = _feed_url(provider, token)
    try:
        status, payload = _fetch_json(url, timeout=timeout)
    except ProbeFailure as exc:
        return BoardResult(provider, token, STATUS_ERROR, detail=str(exc))

    elapsed = time.monotonic() - started
    if status in MISSING_STATUS_CODES:
        return BoardResult(
            provider, token, STATUS_MISSING, detail=f"HTTP {status}", seconds=elapsed
        )
    if payload is None:
        return BoardResult(provider, token, STATUS_ERROR, detail=f"HTTP {status}", seconds=elapsed)

    jobs = _jobs_array(payload)
    if jobs is None:
        return BoardResult(
            provider,
            token,
            STATUS_ERROR,
            detail="response carries no 'jobs' array — the feed schema may have changed",
            seconds=elapsed,
        )
    count = len(jobs)
    return BoardResult(
        provider,
        token,
        STATUS_LIVE if count else STATUS_EMPTY,
        count=count,
        seconds=elapsed,
    )


def _probe_lever(token: str, *, timeout: float, delay: float) -> BoardResult:
    """Probe one Lever company board, paging until the feed is exhausted.

    Lever's feed is offset-paginated and returns a bare array, so the roster size is only
    knowable by reading pages until a short one arrives.

    Args:
        token: The company token.
        timeout: Seconds allowed for each request.
        delay: Pause between pages.

    Returns:
        The verdict. :data:`STATUS_EMPTY` covers both "this company has no openings" and
        "this company token is not a Lever board", because Lever's API does not distinguish
        them — which is precisely why an empty Lever board must be treated as dead.
    """
    started = time.monotonic()
    provider = ATSProviderName.LEVER.value
    total = 0

    for page in range(MAX_LEVER_PAGES):
        params = {"mode": "json", "skip": total, "limit": LEVER_PAGE_SIZE}
        url = _feed_url(provider, token, params)
        try:
            status, payload = _fetch_json(url, timeout=timeout)
        except ProbeFailure as exc:
            return BoardResult(provider, token, STATUS_ERROR, count=total, detail=str(exc))

        if status in MISSING_STATUS_CODES:
            return BoardResult(
                provider,
                token,
                STATUS_MISSING,
                detail=f"HTTP {status}",
                seconds=time.monotonic() - started,
            )
        if not isinstance(payload, list):
            return BoardResult(
                provider,
                token,
                STATUS_ERROR,
                detail=f"HTTP {status}: expected a JSON array, got {type(payload).__name__}",
                seconds=time.monotonic() - started,
            )

        total += len(payload)
        if len(payload) < LEVER_PAGE_SIZE:
            break
        if page + 1 < MAX_LEVER_PAGES:
            time.sleep(delay)
    else:
        return BoardResult(
            provider,
            token,
            STATUS_LIVE,
            count=total,
            truncated=True,
            detail=f"stopped at the {MAX_LEVER_PAGES}-page ceiling",
            seconds=time.monotonic() - started,
        )

    return BoardResult(
        provider,
        token,
        STATUS_LIVE if total else STATUS_EMPTY,
        count=total,
        seconds=time.monotonic() - started,
    )


async def _probe_workday(token: str) -> BoardResult:
    """Probe one Workday tenant through the real provider.

    Workday publishes no shard-independent feed URL — the ``wd<N>`` shard and the career-site
    name vary per tenant and have to be discovered — so this goes through
    :func:`app.jobs.registry.get_provider` and asks the provider itself, which is the only
    code that knows how to resolve a bare tenant token. That also makes this the one probe
    here that exercises a real provider end to end.

    Args:
        token: The Workday tenant token.

    Returns:
        The verdict. ``count`` is a sample capped at :data:`WORKDAY_SAMPLE_LIMIT`, so a live
        board is always reported as truncated.
    """
    from app.jobs.base import ProviderError, SearchQuery
    from app.jobs.registry import get_provider

    provider_name = ATSProviderName.WORKDAY.value
    started = time.monotonic()
    query = SearchQuery(limit=WORKDAY_SAMPLE_LIMIT, extra={provider_name: [token]})

    found = 0
    try:
        provider = get_provider(provider_name)
        async for _ in provider.search(query):
            found += 1
    except ProviderError as exc:
        return BoardResult(
            provider_name,
            token,
            STATUS_ERROR,
            detail=str(exc),
            seconds=time.monotonic() - started,
        )

    elapsed = time.monotonic() - started
    if not found:
        return BoardResult(
            provider_name,
            token,
            STATUS_EMPTY,
            detail="no career site resolved, or the resolved site served no postings",
            seconds=elapsed,
        )
    return BoardResult(
        provider_name,
        token,
        STATUS_LIVE,
        count=found,
        truncated=True,
        detail=f"sampled, capped at {WORKDAY_SAMPLE_LIMIT}",
        seconds=elapsed,
    )


# ======================================================================================
# Sweeps
# ======================================================================================


def validate_provider(
    provider: str,
    tokens: Sequence[str],
    *,
    delay: float = DEFAULT_DELAY_SECONDS,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    verbose: bool = True,
) -> list[BoardResult]:
    """Probe every token of one provider, sequentially.

    Args:
        provider: The provider name.
        tokens: The board tokens to probe.
        delay: Pause between tokens.
        timeout: Seconds allowed for each request.
        verbose: Print each verdict as it arrives, so a long sweep shows progress.

    Returns:
        One :class:`BoardResult` per token, in the order given.

    Raises:
        KeyError: If *provider* has no probe — the caller should have filtered against
            :data:`VALIDATABLE_PROVIDERS`.
    """
    if provider == ATSProviderName.WORKDAY.value:
        return asyncio.run(_validate_workday(tokens, delay=delay, verbose=verbose))
    if provider not in FEED_PROVIDERS:
        raise KeyError(f"no probe for provider {provider!r}")

    results: list[BoardResult] = []
    for index, token in enumerate(tokens):
        if provider == ATSProviderName.LEVER.value:
            result = _probe_lever(token, timeout=timeout, delay=delay)
        else:
            result = _probe_envelope(provider, token, timeout=timeout)
        results.append(result)
        if verbose:
            print(f"  {_render_row(result)}", flush=True)
        if index + 1 < len(tokens):
            time.sleep(delay)
    return results


async def _validate_workday(
    tokens: Sequence[str],
    *,
    delay: float,
    verbose: bool,
) -> list[BoardResult]:
    """Probe every Workday tenant, sequentially, inside one event loop.

    Args:
        tokens: The tenant tokens.
        delay: Pause between tenants.
        verbose: Print each verdict as it arrives.

    Returns:
        One :class:`BoardResult` per tenant.
    """
    results: list[BoardResult] = []
    for index, token in enumerate(tokens):
        result = await _probe_workday(token)
        results.append(result)
        if verbose:
            print(f"  {_render_row(result)}", flush=True)
        if index + 1 < len(tokens):
            await asyncio.sleep(delay)
    return results


# ======================================================================================
# Rendering
# ======================================================================================


def _paint(text: str, colour: str) -> str:
    """Wrap *text* in an ANSI colour when the stream is a terminal."""
    if not sys.stdout.isatty():
        return text
    return f"{colour}{text}{RESET}"


def _render_row(result: BoardResult) -> str:
    """Render one result as a single aligned table row."""
    colour = _VERDICT_COLOURS.get(result.status, RESET)
    count = f"{result.count}+" if result.truncated else str(result.count)
    if result.status != STATUS_LIVE and result.count == 0:
        count = "-"
    row = f"{result.token:<24} {_paint(f'{result.status:<8}', colour)} {count:>7}"
    if result.detail:
        row = f"{row}  {_paint(result.detail[:70], DIM)}"
    return row


@dataclass(slots=True)
class _Summary:
    """Aggregated verdicts for one provider.

    Attributes:
        provider: The provider name.
        results: Every probe result, in probe order.
    """

    provider: str
    results: list[BoardResult] = field(default_factory=list)

    @property
    def live(self) -> list[BoardResult]:
        """Results whose board carried at least one posting."""
        return [item for item in self.results if item.status == STATUS_LIVE]

    @property
    def dead(self) -> list[BoardResult]:
        """Results whose board carried nothing, or did not exist."""
        return [item for item in self.results if item.dead]

    @property
    def errored(self) -> list[BoardResult]:
        """Results that failed for reasons unrelated to the board."""
        return [item for item in self.results if item.status == STATUS_ERROR]

    @property
    def postings(self) -> int:
        """Total postings seen across every live board."""
        return sum(item.count for item in self.results)


def _render_summary(summaries: Sequence[_Summary]) -> None:
    """Print the per-provider totals and the dead-token lists.

    Args:
        summaries: One summary per provider that was swept.
    """
    print()
    print("=" * 78)
    print(f"{'provider':<14}{'boards':>8}{'live':>8}{'dead':>8}{'error':>8}{'postings':>12}")
    print("-" * 78)
    for summary in summaries:
        print(
            f"{summary.provider:<14}"
            f"{len(summary.results):>8}"
            f"{len(summary.live):>8}"
            f"{len(summary.dead):>8}"
            f"{len(summary.errored):>8}"
            f"{summary.postings:>12}"
        )
    print("=" * 78)

    for summary in summaries:
        if summary.dead:
            tokens = ", ".join(item.token for item in summary.dead)
            print()
            print(_paint(f"{summary.provider}: {len(summary.dead)} dead token(s)", RED))
            print(f"  {tokens}")
        if summary.errored:
            tokens = ", ".join(item.token for item in summary.errored)
            print()
            print(_paint(f"{summary.provider}: {len(summary.errored)} token(s) errored", YELLOW))
            print(f"  {tokens}")


# ======================================================================================
# Entry point
# ======================================================================================


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments, or ``None`` to read :data:`sys.argv`.

    Returns:
        The parsed namespace.
    """
    parser = argparse.ArgumentParser(
        prog="python -m scripts.validate_boards",
        description="Probe every shipped board token against its live provider API.",
    )
    parser.add_argument(
        "--provider",
        action="append",
        choices=sorted(VALIDATABLE_PROVIDERS),
        help="Validate only this provider; repeatable. Defaults to all of them.",
    )
    parser.add_argument(
        "--tokens",
        default="",
        help=(
            "Comma-separated tokens to probe instead of the shipped list. Requires exactly "
            "one --provider; used to verify a candidate replacement before adding it."
        ),
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=f"Seconds between requests (default {DEFAULT_DELAY_SECONDS}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=REQUEST_TIMEOUT_SECONDS,
        help=f"Seconds allowed per request (default {REQUEST_TIMEOUT_SECONDS}).",
    )
    parser.add_argument("--json", default="", help="Also write the full results to this path.")
    parser.add_argument("--quiet", action="store_true", help="Print the summary only.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Sweep the requested providers and report.

    Args:
        argv: Command-line arguments, or ``None`` to read :data:`sys.argv`.

    Returns:
        ``0`` when every polled token returned at least one posting, ``1`` when any token was
        empty or missing, and ``2`` on a usage error.
    """
    # This script prints live job titles, which are not ASCII: an em dash, an accented
    # name, a CJK office location. Windows still defaults stdout to cp1252, so printing one
    # raised UnicodeEncodeError partway through the sweep — after ~139 of 156 boards, with no
    # summary and a traceback instead of a result. Fail-soft rather than fail-late: a console
    # that cannot render a character should degrade that character, never lose the report.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:  # pragma: no branch - always present on 3.7+
            reconfigure(encoding="utf-8", errors="replace")

    args = _parse_args(argv)
    providers = [
        name for name in VALIDATABLE_PROVIDERS if not args.provider or name in args.provider
    ]

    explicit = [token.strip() for token in args.tokens.split(",") if token.strip()]
    if explicit and len(providers) != 1:
        print("--tokens requires exactly one --provider", file=sys.stderr)
        return 2

    summaries: list[_Summary] = []
    for provider in providers:
        tokens = explicit or default_boards(provider)
        if not tokens:
            continue
        if not args.quiet:
            source = "supplied" if explicit else "shipped"
            print()
            print(f"{provider} — {len(tokens)} {source} token(s)")
        results = validate_provider(
            provider,
            tokens,
            delay=args.delay,
            timeout=args.timeout,
            verbose=not args.quiet,
        )
        summaries.append(_Summary(provider, results))

    _render_summary(summaries)

    if args.json:
        payload = {
            summary.provider: [item.as_dict() for item in summary.results] for summary in summaries
        }
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        print()
        print(f"wrote {args.json}")

    dead = sum(len(summary.dead) for summary in summaries)
    return 1 if dead else 0


if __name__ == "__main__":
    raise SystemExit(main())
