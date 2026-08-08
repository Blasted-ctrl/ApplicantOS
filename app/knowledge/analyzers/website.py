"""Website analyzer (``analyzer`` plugin ``website``) — a polite crawler for the user's own site.

A personal site is where the work that never reached a repository lives: the write-up of a
capstone, the photo essay about a PCB revision, the "what I'm building" page. This analyzer
reads it and turns it into the same shape every other analyzer produces — a document per
page, a ``project`` node per detected project page, technology nodes and ``used_in`` edges,
and facts recovered from the prose.

**Politeness is a correctness property here, not a courtesy.** ApplicantOS runs on the
user's machine, under the user's IP, against sites that may not be the user's own. So:

* ``robots.txt`` is fetched once, cached, and honoured — including its ``Crawl-delay``. A
  root path the site disallows raises
  :class:`~app.knowledge.analyzers.base.SourceAccessDenied` rather than being crawled anyway.
* Requests are paced to roughly one per second with jitter, so the traffic never looks like
  a burst.
* The crawl is same-origin, bounded by ``website_crawl_max_pages`` and
  ``website_crawl_max_depth``, and additionally bounded by a total byte budget, so no
  configuration mistake can turn an index run into a download of the internet.
* The user agent identifies the software honestly (see
  :data:`~app.knowledge.analyzers.base.HTTP_USER_AGENT`), because a site owner who wants to
  block this must be able to.

**Failure is per page.** A timeout, a 500, a PDF where HTML was expected, a redirect that
leaves the site — each is recorded in
:attr:`~app.knowledge.analyzers.base.AnalysisResult.errors` and the crawl continues. Only a
root that cannot be read at all, or a ``robots.txt`` that says no, ends the run.

**Change detection** is a ``HEAD`` of the root for its ``ETag``/``Last-Modified``, falling
back to hashing the root's body when the server offers neither. See
:meth:`WebsiteAnalyzer.fingerprint` for what that does and does not catch.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Final
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import structlog

from app.cache.keys import NAMESPACES, make_key
from app.knowledge.analyzers._text import (
    html_to_text,
    is_asset_url,
    is_http_url,
    normalize_url,
    same_origin,
)
from app.knowledge.analyzers.base import (
    HTTP_USER_AGENT,
    AnalysisResult,
    Analyzer,
    AnalyzerError,
    ExtractedDocument,
    ExtractedEdge,
    ExtractedEntity,
    SourceAccessDenied,
    SourceRef,
    SourceUnavailableError,
    compute_fingerprint,
    http_client,
)
from app.knowledge.extractors import (
    KnowledgeExtractor,
    detect_project_name,
    extract_skills,
)
from app.models.enums import EntityKind, FactKind, PluginKind, RelationKind, SourceKind
from app.plugins import PluginMeta, plugin

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.cache.base import Cache
    from app.config.settings import Settings

__all__ = [
    "MAX_TOTAL_BYTES",
    "PROJECT_PATH_MARKERS",
    "REQUEST_INTERVAL_SECONDS",
    "ROBOTS_PATH",
    "WebsiteAnalyzer",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: Where a site publishes its crawling rules.
ROBOTS_PATH: Final[str] = "/robots.txt"

#: Baseline seconds between two requests to the same site. One request per second is the
#: convention a small personal site is built to survive.
REQUEST_INTERVAL_SECONDS: Final[float] = 1.0

#: Extra random delay added to every interval, in seconds. Jitter matters: a perfectly
#: periodic request pattern is what naive bot detection looks for, and evenly-spaced load is
#: not actually kinder than slightly uneven load.
REQUEST_JITTER_SECONDS: Final[float] = 0.4

#: Ceiling on a ``Crawl-delay`` the analyzer will honour. A site asking for 300 seconds
#: between requests is asking for a crawl that takes three hours; the delay is respected up
#: to this bound and the shortfall is recorded so the operator can see it happened.
MAX_CRAWL_DELAY_SECONDS: Final[float] = 10.0

#: Byte budget for the whole crawl, and for any single page. Independent of the page count,
#: because forty image-heavy pages can be two orders of magnitude larger than forty text
#: pages, and memory is the resource that actually runs out.
MAX_TOTAL_BYTES: Final[int] = 8_000_000
MAX_PAGE_BYTES: Final[int] = 2_000_000

#: Content types worth parsing. Everything else is recorded as a skipped page: this analyzer
#: extracts HTML, and handing a PDF to an HTML parser produces convincing nonsense.
HTML_CONTENT_TYPES: Final[frozenset[str]] = frozenset(
    {"text/html", "application/xhtml+xml", "application/xml", "text/xml", "text/plain"}
)

#: ``Accept`` header sent with every page request.
HTML_ACCEPT: Final[str] = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"

#: Encoding assumed when a server declares none. UTF-8 is right for the overwhelming
#: majority of the modern web, and decoding is lossy-tolerant so a wrong guess degrades a
#: few characters rather than losing the page.
DEFAULT_ENCODING: Final[str] = "utf-8"

#: Lifetime of a cached page body. Short compared with the GitHub cache because a site has
#: no equivalent of ``pushed_at`` to invalidate against; the entry's real job is to carry an
#: ``ETag`` into the next run's ``If-None-Match``.
CACHE_TTL_SECONDS: Final[int] = 6 * 60 * 60

#: Lifetime of a cached ``robots.txt``. A day: crawling rules change rarely, and re-fetching
#: them on every page would be its own small rudeness.
ROBOTS_CACHE_TTL_SECONDS: Final[int] = 24 * 60 * 60

#: Cache-key discriminators for the two kinds of cached response.
PAGE_CACHE_TAG: Final[str] = "website.page.v1"
ROBOTS_CACHE_TAG: Final[str] = "website.robots.v1"

#: Domain-separation tag for this analyzer's fingerprints.
FINGERPRINT_TAG: Final[str] = "analyzer.website.v1"

#: URL path segments that announce a project write-up. Matched against whole segments, so
#: ``/projects/slam-rover`` qualifies and ``/networking`` does not.
PROJECT_PATH_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "project",
        "projects",
        "work",
        "works",
        "portfolio",
        "case-study",
        "case-studies",
        "casestudy",
        "casestudies",
        "build",
        "builds",
        "made",
        "making",
        "writeup",
        "writeups",
    }
)

#: How many recognised technologies a page must name before its heading alone is enough to
#: call it a project write-up. Three is the point at which a page stops reading like a bio
#: ("I like Python") and starts reading like a build log.
MIN_PROJECT_TECHNOLOGIES: Final[int] = 3

#: Characters of extracted text below which a page carries no prose at all — a redirect
#: stub, a bare image gallery, a "loading…" shell. Deliberately low: a short page is still a
#: page, and a threshold tuned to exclude *thin* content would silently drop the one-
#: paragraph project note this engine exists to capture.
MIN_PAGE_CHARS: Final[int] = 60

#: Edge weight for a technology named on a project page. Lower than the GitHub analyzer's
#: language weights on purpose: "this word appears on the page about the robot" is weaker
#: evidence than "GitHub measured 82% of this repository in this language".
EDGE_WEIGHT_PROJECT_TECHNOLOGY: Final[float] = 1.0

#: Confidence for a project inferred from a page. Below the GitHub analyzer's
#: :data:`~app.knowledge.analyzers.github.API_CONFIDENCE`, because a repository provably
#: exists whereas a project page is a heuristic reading of a heading.
PROJECT_CONFIDENCE: Final[float] = 0.65

#: HTTP statuses for which a ``robots.txt`` means "you may not crawl this site at all",
#: per RFC 9309 §2.3.1.3. Any other failure — 404, a 500, a timeout — means "no rules
#: published", which is an allowance.
ROBOTS_DENY_STATUSES: Final[frozenset[int]] = frozenset({401, 403})

#: Attempts per page request, including the first.
MAX_ATTEMPTS: Final[int] = 2

#: Backoff between page attempts, in seconds.
RETRY_BACKOFF_SECONDS: Final[float] = 1.0

#: Characters of a page used as a project's summary when the document declares no
#: description of its own.
MAX_SUMMARY_CHARS: Final[int] = 280


# ======================================================================================
# Errors
# ======================================================================================


class _PageUnavailable(AnalyzerError):
    """One page could not be read.

    Private, and never escapes :meth:`WebsiteAnalyzer.analyze`: the crawl records the
    message in :attr:`~app.knowledge.analyzers.base.AnalysisResult.errors` and moves to the
    next URL. A crawl that returns thirty-nine pages and one apology is a success.
    """


# ======================================================================================
# Crawl machinery
# ======================================================================================


@dataclass(slots=True)
class _Page:
    """One successfully fetched page.

    Attributes:
        url: The normalised URL the response actually came from, after redirects. This — not
            the requested URL — is what identifies the document, so two spellings that
            redirect to one page produce one document.
        body: The decoded response body.
        etag: The response's ``ETag``, or the empty string.
        last_modified: The response's ``Last-Modified``, or the empty string.
        content_type: The response's media type, lowercased and without parameters.
        byte_length: Size of the body in bytes, charged against the crawl's budget.
        from_cache: Whether the body came from a cache entry revalidated with ``ETag``.
    """

    url: str
    body: str
    etag: str = ""
    last_modified: str = ""
    content_type: str = ""
    byte_length: int = 0
    from_cache: bool = False


@dataclass(slots=True)
class _Pacer:
    """Enforces a minimum gap between requests, with jitter.

    Created per crawl rather than per analyzer, so two concurrent crawls of different sites
    do not throttle each other and neither inherits the other's clock.

    Attributes:
        delay: Minimum seconds between requests.
        rng: Jitter source. Instance-local so this never perturbs the global
            :mod:`random` state that some other component may be relying on.
    """

    delay: float = REQUEST_INTERVAL_SECONDS
    rng: random.Random = field(default_factory=random.Random)
    _last: float = 0.0

    async def wait(self) -> None:
        """Sleep until the next request is due.

        Returns immediately for the first request of a crawl, and thereafter sleeps only for
        whatever is left of the interval after the previous request's own duration — a page
        that took two seconds to download has already paid the delay.
        """
        target = self.delay + self.rng.uniform(0.0, REQUEST_JITTER_SECONDS)
        if self._last:
            remaining = target - (time.monotonic() - self._last)
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last = time.monotonic()


@dataclass(slots=True)
class _Budget:
    """What is left of a crawl's allowances.

    Attributes:
        pages: Pages still allowed.
        depth: Maximum link depth from the root.
        remaining_bytes: Bytes still allowed across the whole crawl.
    """

    pages: int
    depth: int
    remaining_bytes: int = MAX_TOTAL_BYTES

    def spend(self, page_bytes: int) -> None:
        """Charge one fetched page against the budget.

        Args:
            page_bytes: Size of the page just downloaded.
        """
        self.pages -= 1
        self.remaining_bytes -= page_bytes

    @property
    def exhausted(self) -> bool:
        """Whether either the page count or the byte budget is spent."""
        return self.pages <= 0 or self.remaining_bytes <= 0


def _origin_of(url: str) -> str:
    """Return the scheme-and-authority prefix of *url*.

    Args:
        url: An absolute URL.

    Returns:
        ``"https://example.com"``, or the empty string when *url* names no host.
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def _media_type(content_type: str) -> str:
    """Reduce a ``Content-Type`` header to its media type.

    Args:
        content_type: The raw header value, e.g. ``"text/html; charset=utf-8"``.

    Returns:
        ``"text/html"``, lowercased and stripped.
    """
    return content_type.split(";", 1)[0].strip().lower()


def _humanize_segment(segment: str) -> str:
    """Turn a URL slug into a display name.

    Args:
        segment: A path segment such as ``"slam-rover"`` or ``"pcb_rev_c.html"``.

    Returns:
        ``"Slam Rover"`` — separators become spaces, a file extension is dropped, and each
        word is capitalised only when it is entirely lowercase, so ``"STM32-driver"`` keeps
        its capitals.
    """
    stem = segment.rsplit(".", 1)[0] if "." in segment else segment
    words = [word for word in stem.replace("-", " ").replace("_", " ").split() if word]
    return " ".join(word.capitalize() if word.islower() else word for word in words)


# ======================================================================================
# The analyzer
# ======================================================================================


@plugin
class WebsiteAnalyzer(Analyzer):
    """Crawls a personal site or portfolio page and extracts what it says about the user.

    Handles :attr:`~app.models.enums.SourceKind.PERSONAL_WEBSITE` — breadth-first from the
    root, same-origin, within the configured page and depth limits — and
    :attr:`~app.models.enums.SourceKind.PORTFOLIO_PAGE`, which defaults to a depth of zero
    because pointing at *one page* should index that page, not discover a site through it.
    Either default is overridable per source.

    Per-source options, read from ``KnowledgeSource.config``:

    ``max_pages`` / ``max_depth``
        Override ``settings.website_crawl_max_pages`` / ``website_crawl_max_depth``.
    ``request_interval``
        Override the base pacing in seconds. A site's own ``Crawl-delay`` always wins when
        it is longer.

    Attributes:
        source_kinds: ``personal_website`` and ``portfolio_page``.
    """

    meta: ClassVar[PluginMeta] = PluginMeta(
        kind=PluginKind.ANALYZER,
        name="website",
        display_name="Personal website",
        description=(
            "Politely crawls a personal site or portfolio page, honouring robots.txt, and "
            "extracts project write-ups and prose."
        ),
        capabilities=frozenset({"http", "crawl", "robots", "fingerprint", "etag_cache"}),
    )
    source_kinds: ClassVar[frozenset[SourceKind]] = frozenset(
        {SourceKind.PERSONAL_WEBSITE, SourceKind.PORTFOLIO_PAGE}
    )

    def __init__(self, settings: Settings, **kw: Any) -> None:
        """Construct the analyzer.

        Args:
            settings: Application settings; ``website_crawl_max_pages`` and
                ``website_crawl_max_depth`` are read from it.
            **kw: Extra construction options, kept on
                :attr:`~app.plugins.base.BasePlugin.options`.
        """
        super().__init__(settings, **kw)
        self._cache: Cache | None = None
        self._cache_resolved = False
        self._extractor: KnowledgeExtractor | None = None

    # -- collaborators ---------------------------------------------------------------------

    def _get_cache(self) -> Cache | None:
        """Return the process cache, resolving it once.

        Returns:
            The shared cache, or ``None`` when one cannot be built — in which case every
            page is fetched fresh.
        """
        if self._cache_resolved:
            return self._cache
        self._cache_resolved = True
        try:
            from app.cache import get_cache

            self._cache = get_cache()
        except Exception as exc:  # noqa: BLE001 - an absent cache is a slowdown, not a fault
            logger.info("website.cache_unavailable", error=str(exc))
            self._cache = None
        return self._cache

    def _get_extractor(self) -> KnowledgeExtractor:
        """Return the fact extractor, building it once.

        Returns:
            A :class:`~app.knowledge.extractors.KnowledgeExtractor` sharing this analyzer's
            cache. It resolves a model lazily and falls back to deterministic rules when
            there is none, so a crawl works with zero API keys.
        """
        if self._extractor is None:
            self._extractor = KnowledgeExtractor(cache=self._get_cache())
        return self._extractor

    # -- plugin surface ----------------------------------------------------------------------

    def supports(self, source: SourceRef) -> bool:
        """Return whether this analyzer can handle *source*.

        Args:
            source: The candidate source.

        Returns:
            ``True`` when the kind matches and the URI is (or can be made) an HTTP URL. An
            empty URI is accepted so that
            :func:`~app.knowledge.analyzers.base.get_analyzer` — which probes with a bare
            kind — can still resolve this analyzer.
        """
        if not super().supports(source):
            return False
        if not source.uri:
            return True
        try:
            return bool(self._root_url(source))
        except AnalyzerError:
            return False

    async def healthcheck(self) -> bool:
        """Report whether this analyzer can run.

        Returns:
            ``True`` when ``httpx`` is importable. There is no service to probe — the only
            host that matters is whichever one the user configured.
        """
        try:
            import httpx  # noqa: F401 - presence is the entire check
        except ImportError:
            logger.info("website.healthcheck_failed", reason="httpx_not_installed")
            return False
        return True

    # -- configuration --------------------------------------------------------------------------

    def _root_url(self, source: SourceRef) -> str:
        """Resolve a source URI to the absolute URL the crawl starts from.

        Args:
            source: The source being analyzed.

        Returns:
            The normalised root URL. A bare host (``example.com``) is promoted to ``https``,
            which is both the safer default and what the user meant.

        Raises:
            AnalyzerError: If the URI is empty or is not an HTTP address.
        """
        raw = (source.uri or "").strip()
        if not raw:
            raise AnalyzerError("a website source needs a URL", source=source)
        if "://" not in raw:
            raw = f"https://{raw}"
        url = normalize_url(raw)
        if not is_http_url(url):
            raise AnalyzerError(
                f"{source.uri!r} is not an http(s) address", source=source
            )
        return url

    def _budget(self, source: SourceRef) -> _Budget:
        """Build the crawl budget for *source*.

        Args:
            source: The source being analyzed.

        Returns:
            The page, depth and byte allowances. A ``portfolio_page`` source defaults to
            depth 0 — the page itself and nothing else — while a ``personal_website``
            source uses ``settings.website_crawl_max_depth``.
        """
        default_depth = (
            0
            if source.kind is SourceKind.PORTFOLIO_PAGE
            else int(getattr(self.settings, "website_crawl_max_depth", 3))
        )
        return _Budget(
            pages=max(1, self._option_int(source, "max_pages", int(
                getattr(self.settings, "website_crawl_max_pages", 40)
            ))),
            depth=max(0, self._option_int(source, "max_depth", default_depth, allow_zero=True)),
        )

    def _option_int(
        self, source: SourceRef, name: str, default: int, *, allow_zero: bool = False
    ) -> int:
        """Read an integer option off the source's config.

        Args:
            source: The source being analyzed.
            name: The option key.
            default: Value used when the option is absent or unusable.
            allow_zero: Whether zero is a meaningful value (it is, for ``max_depth``).

        Returns:
            The configured value, or *default*.
        """
        try:
            value = int(source.option(name, default))
        except (TypeError, ValueError):
            return default
        if value < 0 or (value == 0 and not allow_zero):
            return default
        return value

    # -- caching -----------------------------------------------------------------------------------

    async def _read_cached(self, key: str) -> dict[str, Any] | None:
        """Return a cached response envelope.

        Args:
            key: The cache key.

        Returns:
            The stored envelope, or ``None`` on a miss or a cache failure.
        """
        cache = self._get_cache()
        if cache is None:
            return None
        try:
            entry = await cache.get(key)
        except Exception as exc:  # noqa: BLE001 - a failing cache degrades to a miss
            logger.debug("website.cache_read_failed", error=str(exc))
            return None
        return entry if isinstance(entry, dict) else None

    async def _write_cached(self, key: str, envelope: dict[str, Any], ttl: int) -> None:
        """Store a response envelope.

        Args:
            key: The cache key.
            envelope: The JSON-serialisable envelope to store.
            ttl: Lifetime in seconds.
        """
        cache = self._get_cache()
        if cache is None:
            return
        try:
            await cache.set(key, envelope, ttl=ttl)
        except Exception as exc:  # noqa: BLE001 - failing to cache is never fatal
            logger.debug("website.cache_write_failed", error=str(exc))

    # -- robots.txt ---------------------------------------------------------------------------------

    async def _load_robots(self, origin: str, result: AnalysisResult) -> RobotFileParser:
        """Fetch and parse a site's ``robots.txt``, once per origin.

        Args:
            origin: The site's scheme-and-authority prefix.
            result: The accumulator, for recording a fetch that failed in an interesting way.

        Returns:
            A parser. A site that publishes no rules, or whose ``robots.txt`` could not be
            fetched, yields a parser that allows everything — which is what RFC 9309 §2.3.1
            prescribes for a 404 and for a network failure. The two statuses that mean
            "stay out entirely" (:data:`ROBOTS_DENY_STATUSES`) yield a parser that disallows
            everything.
        """
        parser = RobotFileParser()
        url = f"{origin}{ROBOTS_PATH}"
        parser.set_url(url)

        key = make_key(NAMESPACES.HTTP, ROBOTS_CACHE_TAG, url)
        cached = await self._read_cached(key)
        if cached is not None and "text" in cached:
            self._apply_robots(parser, int(cached.get("status", 200)), str(cached.get("text", "")))
            return parser

        import httpx

        try:
            client = http_client()
            response = await client.get(url, headers={"Accept": "text/plain,*/*;q=0.1"})
        except httpx.HTTPError as exc:
            logger.info("website.robots_unreachable", url=url, error=str(exc))
            result.record_error(f"robots.txt at {url} could not be fetched ({exc}); assuming no rules.")
            parser.parse([])
            return parser

        text = response.text if response.status_code < 400 else ""
        self._apply_robots(parser, response.status_code, text)
        await self._write_cached(
            key, {"status": response.status_code, "text": text}, ROBOTS_CACHE_TTL_SECONDS
        )
        return parser

    @staticmethod
    def _apply_robots(parser: RobotFileParser, status: int, text: str) -> None:
        """Load one ``robots.txt`` response into *parser*.

        Args:
            parser: The parser to populate.
            status: The HTTP status the file was served with.
            text: The file's contents, or the empty string when it was not served.
        """
        if status in ROBOTS_DENY_STATUSES:
            parser.disallow_all = True
            return
        parser.parse(text.splitlines() if status < 400 else [])

    def _crawl_delay(self, parser: RobotFileParser, source: SourceRef, result: AnalysisResult) -> float:
        """Return the pacing interval to use, honouring the site's ``Crawl-delay``.

        Args:
            parser: The site's parsed rules.
            source: The source being analyzed, for its ``request_interval`` override.
            result: The accumulator, for recording a delay that had to be capped.

        Returns:
            Seconds between requests: the larger of the configured interval and the site's
            own ``Crawl-delay``, bounded by :data:`MAX_CRAWL_DELAY_SECONDS`.
        """
        configured = REQUEST_INTERVAL_SECONDS
        override = source.option("request_interval", None)
        if isinstance(override, (int, float)) and override > 0:
            configured = float(override)

        try:
            requested = parser.crawl_delay(HTTP_USER_AGENT)
        except Exception as exc:  # noqa: BLE001 - a malformed directive is not our problem
            logger.debug("website.crawl_delay_unreadable", error=str(exc))
            requested = None
        if requested is None:
            return configured

        delay = float(requested)
        if delay > MAX_CRAWL_DELAY_SECONDS:
            result.record_error(
                f"robots.txt requests a {delay:.0f}s crawl delay; capped at "
                f"{MAX_CRAWL_DELAY_SECONDS:.0f}s, so this crawl is faster than the site asked."
            )
            delay = MAX_CRAWL_DELAY_SECONDS
        return max(configured, delay)

    # -- fetching -------------------------------------------------------------------------------------

    async def _fetch(self, url: str, *, origin: str | None) -> _Page:
        """Fetch one page, conditionally and within the size cap.

        Streams the response so that the media type can be checked, and an oversized body
        abandoned, before the whole thing is in memory.

        Args:
            url: The absolute URL to fetch.
            origin: The crawl's origin. When given, a redirect that lands elsewhere is
                refused; when ``None`` (the root fetch) a redirect anywhere is followed, so
                that a site which has moved is still indexable.

        Returns:
            The fetched page.

        Raises:
            _PageUnavailable: For a transport failure, a 4xx or 5xx, a non-HTML media type,
                an off-origin redirect, or a body over :data:`MAX_PAGE_BYTES`.
        """
        import httpx

        key = make_key(NAMESPACES.HTTP, PAGE_CACHE_TAG, url)
        cached = await self._read_cached(key)
        headers = {"Accept": HTML_ACCEPT}
        if cached:
            if cached.get("etag"):
                headers["If-None-Match"] = str(cached["etag"])
            elif cached.get("last_modified"):
                headers["If-Modified-Since"] = str(cached["last_modified"])

        client = http_client()
        last_error: str = "unknown error"
        for attempt in range(MAX_ATTEMPTS):
            try:
                return await self._fetch_once(client, url, headers, cached, origin, key)
            except _PageUnavailable:
                raise
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.debug("website.fetch_failed", url=url, attempt=attempt, error=last_error)
            if attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS)
        raise _PageUnavailable(f"{url} could not be fetched ({last_error})")

    async def _fetch_once(
        self,
        client: Any,
        url: str,
        headers: dict[str, str],
        cached: dict[str, Any] | None,
        origin: str | None,
        key: str,
    ) -> _Page:
        """Perform one streamed GET and turn the response into a :class:`_Page`.

        Args:
            client: The shared ``httpx.AsyncClient``.
            url: The absolute URL to fetch.
            headers: Request headers, including any conditional validator.
            cached: The cached envelope, used to answer a ``304``.
            origin: The crawl origin, or ``None`` to allow any redirect.
            key: The cache key to store a fresh body under.

        Returns:
            The fetched page.

        Raises:
            _PageUnavailable: For a status, media type, origin or size this crawl refuses.
        """
        async with client.stream("GET", url, headers=headers) as response:
            final_url = normalize_url(str(response.url))

            if response.status_code == 304:
                if cached is None:  # pragma: no cover - only reachable if a proxy invents it
                    raise _PageUnavailable(
                        f"{url} answered 304 Not Modified without being asked a conditional "
                        "question; there is no cached body to serve"
                    )
                logger.debug("website.not_modified", url=url)
                return _Page(
                    url=normalize_url(str(cached.get("url") or final_url)),
                    body=str(cached.get("body", "")),
                    etag=str(cached.get("etag", "")),
                    last_modified=str(cached.get("last_modified", "")),
                    content_type=str(cached.get("content_type", "")),
                    byte_length=int(cached.get("byte_length", 0)),
                    from_cache=True,
                )

            if response.status_code >= 400:
                raise _PageUnavailable(f"{url} returned HTTP {response.status_code}")

            if origin is not None and not same_origin(final_url, origin):
                raise _PageUnavailable(
                    f"{url} redirected off-origin to {final_url}; not following it"
                )

            media_type = _media_type(response.headers.get("content-type", ""))
            if media_type and media_type not in HTML_CONTENT_TYPES:
                raise _PageUnavailable(f"{final_url} is {media_type}, not HTML; skipped")

            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_PAGE_BYTES:
                raise _PageUnavailable(
                    f"{final_url} declares {declared} bytes, over the {MAX_PAGE_BYTES} byte "
                    "per-page limit; skipped"
                )

            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_PAGE_BYTES:
                    raise _PageUnavailable(
                        f"{final_url} exceeds the {MAX_PAGE_BYTES} byte per-page limit; skipped"
                    )
                chunks.append(chunk)

            raw = b"".join(chunks)
            body = self._decode(raw, response.charset_encoding)
            page = _Page(
                url=final_url,
                body=body,
                etag=response.headers.get("etag", ""),
                last_modified=response.headers.get("last-modified", ""),
                content_type=media_type,
                byte_length=total,
            )

        await self._write_cached(
            key,
            {
                "url": page.url,
                "body": page.body,
                "etag": page.etag,
                "last_modified": page.last_modified,
                "content_type": page.content_type,
                "byte_length": page.byte_length,
            },
            CACHE_TTL_SECONDS,
        )
        return page

    @staticmethod
    def _decode(raw: bytes, declared_encoding: str | None) -> str:
        """Decode a response body to text.

        Args:
            raw: The response bytes.
            declared_encoding: The charset the server declared, if any.

        Returns:
            The decoded text. An unknown or absent charset falls back to
            :data:`DEFAULT_ENCODING`, and undecodable bytes become replacement characters
            rather than costing the whole page.
        """
        for encoding in (declared_encoding, DEFAULT_ENCODING):
            if not encoding:
                continue
            try:
                return raw.decode(encoding, errors="replace")
            except LookupError:
                continue
        return raw.decode(DEFAULT_ENCODING, errors="replace")

    # -- fingerprinting -----------------------------------------------------------------------------------

    def _compose_fingerprint(
        self, root: str, etag: str, last_modified: str, body: str | None
    ) -> str:
        """Digest whatever the root offers as a change signal.

        Validators win when the server provides them, because they are exactly what the
        server means by "this changed". Only when there are none does the body get hashed —
        and then the *hash* is mixed in rather than the body, so the digest costs the same
        for a 2 KB page and a 2 MB one.

        Args:
            root: The normalised root URL.
            etag: The root's ``ETag``, or the empty string.
            last_modified: The root's ``Last-Modified``, or the empty string.
            body: The root's body, used only when there is no validator.

        Returns:
            A 64-character hex digest.
        """
        if etag or last_modified:
            return compute_fingerprint(FINGERPRINT_TAG, root, etag, last_modified)
        return compute_fingerprint(FINGERPRINT_TAG, root, "", "", compute_fingerprint(body or ""))

    async def fingerprint(self, source: SourceRef) -> str:
        """Cheaply probe whether the site's root has changed.

        One ``HEAD`` of the root. When the server answers with an ``ETag`` or a
        ``Last-Modified`` that is the whole probe; when it answers with neither (or refuses
        ``HEAD`` at all, which some static hosts do) the root is fetched and its body
        hashed. Either way the cost is one request instead of a forty-page crawl.

        Two limitations, both deliberate and both recorded in ``docs/OPEN_QUESTIONS.md``:

        * **The root is a proxy for the site.** A new project page added without touching
          the root's bytes will not be noticed until the periodic full re-index. Detecting
          it properly would mean crawling to find out whether crawling is needed.
        * **A ``HEAD`` that omits validators a ``GET`` would have sent** produces a digest
          that :meth:`analyze` will not reproduce, so the source re-indexes once. That is
          the safe direction: wasted work rather than a frozen knowledge base.

        Args:
            source: The source to probe.

        Returns:
            The digest :meth:`analyze` stores for an unchanged site, or the base class's
            never-matching identity digest when the probe itself failed.
        """
        import httpx

        try:
            root = self._root_url(source)
        except AnalyzerError as exc:
            logger.info("website.fingerprint_unresolved", uri=source.uri, error=str(exc))
            return await super().fingerprint(source)

        try:
            client = http_client()
            response = await client.head(root, headers={"Accept": HTML_ACCEPT})
        except httpx.HTTPError as exc:
            logger.info("website.fingerprint_head_failed", url=root, error=str(exc))
        else:
            if response.status_code < 400:
                etag = response.headers.get("etag", "")
                last_modified = response.headers.get("last-modified", "")
                if etag or last_modified:
                    return self._compose_fingerprint(root, etag, last_modified, None)

        try:
            page = await self._fetch(root, origin=None)
        except AnalyzerError as exc:
            logger.info("website.fingerprint_unavailable", url=root, error=str(exc))
            return await super().fingerprint(source)
        return self._compose_fingerprint(root, page.etag, page.last_modified, page.body)

    # -- analysis --------------------------------------------------------------------------------------------

    async def analyze(self, source: SourceRef) -> AnalysisResult:
        """Crawl the site and extract everything it says about the user.

        Args:
            source: The source to analyze.

        Returns:
            One document per readable page, a ``project`` node per detected project page
            with ``used_in`` edges to the technologies it names, and facts recovered from
            every page's prose — plus one line in
            :attr:`~app.knowledge.analyzers.base.AnalysisResult.errors` for each page that
            was skipped, and why.

        Raises:
            AnalyzerError: If the URI is not an HTTP address.
            SourceAccessDenied: If ``robots.txt`` disallows the root.
            SourceUnavailableError: If the root itself cannot be read, since there is then
                nothing to crawl.
        """
        if source.kind not in type(self).source_kinds:
            self.require_supported(source)

        result = AnalysisResult()
        root = self._root_url(source)
        budget = self._budget(source)

        origin = _origin_of(root)
        robots = await self._load_robots(origin, result)
        self._require_allowed(robots, root, source)

        try:
            root_page = await self._fetch(root, origin=None)
        except AnalyzerError as exc:
            raise SourceUnavailableError(
                f"the site root {root} could not be read: {exc}", source=source
            ) from exc

        if not same_origin(root_page.url, root):
            # The site has moved. Adopt where it actually lives — otherwise every internal
            # link on the page it served would look off-origin and the crawl would end here.
            logger.info("website.root_moved", requested=root, final=root_page.url)
            result.record_error(f"{root} redirects to {root_page.url}; crawling there instead.")
            origin = _origin_of(root_page.url)
            robots = await self._load_robots(origin, result)
            self._require_allowed(robots, root_page.url, source)

        result.fingerprint = self._compose_fingerprint(
            root, root_page.etag, root_page.last_modified, root_page.body
        )

        pacer = _Pacer(delay=self._crawl_delay(robots, source, result))
        visited: set[str] = {root, root_page.url}
        queue: deque[tuple[str, int]] = deque()

        await self._absorb(root_page, depth=0, source=source, result=result, budget=budget,
                           origin=origin, visited=visited, queue=queue)

        while queue and not budget.exhausted:
            url, depth = queue.popleft()
            if not robots.can_fetch(HTTP_USER_AGENT, url):
                logger.debug("website.robots_disallowed", url=url)
                continue
            await pacer.wait()
            try:
                page = await self._fetch(url, origin=origin)
            except AnalyzerError as exc:
                result.record_error(str(exc))
                continue
            if page.url in visited and page.url != url:
                continue
            visited.add(page.url)
            await self._absorb(page, depth=depth, source=source, result=result, budget=budget,
                               origin=origin, visited=visited, queue=queue)

        if budget.remaining_bytes <= 0:
            result.record_error(
                f"crawl stopped after the {MAX_TOTAL_BYTES} byte download budget was spent."
            )

        result.deduplicate()
        logger.info("website.analyzed", root=root, origin=origin, **result.counts())
        return result

    def _require_allowed(self, robots: RobotFileParser, url: str, source: SourceRef) -> None:
        """Raise unless ``robots.txt`` permits fetching *url*.

        Args:
            robots: The site's parsed rules.
            url: The URL about to be fetched.
            source: The source being analyzed, attached to the error.

        Raises:
            SourceAccessDenied: When the site disallows this user agent. The remedy belongs
                to the site owner, not to this installation, and pretending otherwise would
                make the crawler exactly the kind of thing ``robots.txt`` exists to stop.
        """
        if robots.can_fetch(HTTP_USER_AGENT, url):
            return
        raise SourceAccessDenied(
            f"robots.txt at {_origin_of(url)}{ROBOTS_PATH} disallows {url} for "
            f"{HTTP_USER_AGENT!r}. This source cannot be indexed until the site permits it.",
            source=source,
        )

    async def _absorb(
        self,
        page: _Page,
        *,
        depth: int,
        source: SourceRef,
        result: AnalysisResult,
        budget: _Budget,
        origin: str,
        visited: set[str],
        queue: deque[tuple[str, int]],
    ) -> None:
        """Turn one fetched page into knowledge and enqueue its links.

        Args:
            page: The fetched page.
            depth: Its link distance from the root.
            source: The source being analyzed.
            result: The accumulator to append to.
            budget: The crawl budget, charged for this page.
            origin: The crawl origin, which links must share.
            visited: URLs already seen; extended with everything enqueued here.
            queue: The breadth-first frontier.
        """
        budget.spend(page.byte_length)
        text, metadata = html_to_text(page.body, base_url=page.url)

        if depth < budget.depth:
            self._enqueue_links(metadata.get("links") or [], depth + 1, origin, visited, queue)

        if len(text) < MIN_PAGE_CHARS:
            logger.debug("website.page_too_thin", url=page.url, chars=len(text))
            return

        project = self._detect_project(page.url, text, metadata)
        segments = [segment for segment in urlsplit(page.url).path.split("/") if segment]
        document = ExtractedDocument(
            uri=page.url,
            title=(
                metadata.get("title")
                or (_humanize_segment(segments[-1]) if segments else "")
                or page.url
            ),
            text=text,
            kind=SourceKind.PORTFOLIO_PAGE if project else source.kind,
            metadata={
                "url": page.url,
                "depth": depth,
                "description": metadata.get("description"),
                "headings": metadata.get("headings"),
                "lang": metadata.get("lang"),
                "canonical": metadata.get("canonical"),
                "scope": metadata.get("scope"),
                "parser": metadata.get("parser"),
                "content_type": page.content_type,
                "etag": page.etag,
                "last_modified": page.last_modified,
                "byte_length": page.byte_length,
                "links_found": len(metadata.get("links") or []),
                "project": project,
                "source_kind": source.kind.value,
            },
        )
        result.documents.append(document)

        extracted = await self._get_extractor().extract(
            text,
            kind=FactKind.ACCOMPLISHMENT,
            context={"organization": project, "source_uri": page.url},
        )
        result.facts.extend(extracted.facts)

        technologies = [
            entity
            for entity in extracted.entities
            if entity.kind in (EntityKind.SKILL, EntityKind.TECHNOLOGY)
        ]
        result.entities.extend(technologies)

        if project is None:
            return

        project_entity = ExtractedEntity(
            kind=EntityKind.PROJECT,
            name=project,
            summary=self._summary(metadata, text),
            attributes={
                "url": page.url,
                "page_title": metadata.get("title") or "",
                "detected_from": "website",
            },
            confidence=PROJECT_CONFIDENCE,
        )
        result.entities.append(project_entity)
        for entity in technologies:
            result.edges.append(
                ExtractedEdge(
                    # Technology -> project, matching the direction documented on
                    # ExtractedEdge ("PyTorch used_in PoseNet") and emitted by
                    # extract_entities_rule_based(); see app/knowledge/analyzers/github.py
                    # for the full justification.
                    source=entity.identity,
                    target=project_entity.identity,
                    relation=RelationKind.USED_IN,
                    weight=EDGE_WEIGHT_PROJECT_TECHNOLOGY,
                    evidence={"source": "website", "url": page.url, "project": project},
                )
            )

    def _enqueue_links(
        self,
        links: list[str],
        depth: int,
        origin: str,
        visited: set[str],
        queue: deque[tuple[str, int]],
    ) -> None:
        """Add a page's usable outbound links to the frontier.

        Args:
            links: Normalised links from the page.
            depth: The depth to enqueue them at.
            origin: The crawl origin; anything else is another site's problem.
            visited: URLs already seen or queued, extended in place.
            queue: The breadth-first frontier.
        """
        for link in links:
            if link in visited or not is_http_url(link):
                continue
            if not same_origin(link, origin) or is_asset_url(link):
                continue
            visited.add(link)
            queue.append((link, depth))

    # -- interpretation ------------------------------------------------------------------------------------------

    def _detect_project(self, url: str, text: str, metadata: dict[str, Any]) -> str | None:
        """Decide whether a page describes one project, and name it.

        Two independent signals qualify a page, because portfolios are built both ways:

        * its URL path contains a segment from :data:`PROJECT_PATH_MARKERS`
          (``/projects/slam-rover``, ``/work/pcb-rev-c``); or
        * it carries a heading *and* names at least
          :data:`MIN_PROJECT_TECHNOLOGIES` recognised technologies, which is what a build
          write-up looks like and what an about-page does not.

        The site root never qualifies: a homepage listing five technologies is a homepage.

        Naming then runs through :func:`~app.knowledge.extractors.detect_project_name`, which
        also serves as the filter — it rejects the structural titles ("Projects", "Work",
        "Portfolio") that an *index* page carries, so a listing page is correctly not turned
        into a project node called "Projects".

        Args:
            url: The page's normalised URL.
            text: Its extracted text.
            metadata: Its extracted metadata.

        Returns:
            The project's name, or ``None`` when the page is not a project write-up.
        """
        path = urlsplit(url).path
        segments = [segment.lower() for segment in path.split("/") if segment]
        if not segments:
            return None

        by_path = any(segment in PROJECT_PATH_MARKERS for segment in segments)
        by_shape = bool(metadata.get("headings")) and (
            len(extract_skills(text)) >= MIN_PROJECT_TECHNOLOGIES
        )
        if not (by_path or by_shape):
            return None

        for candidate in (
            text,
            f"# {metadata.get('title') or ''}",
            f"# {_humanize_segment(segments[-1])}",
        ):
            name = detect_project_name(candidate)
            if name:
                return name
        return None

    @staticmethod
    def _summary(metadata: dict[str, Any], text: str) -> str | None:
        """Return a one-line description of a project page.

        Args:
            metadata: The page's extracted metadata.
            text: Its extracted text.

        Returns:
            The page's meta description when it has one, otherwise its opening prose
            truncated to :data:`MAX_SUMMARY_CHARS`, otherwise ``None``.
        """
        description = metadata.get("description")
        if isinstance(description, str) and description.strip():
            return description.strip()[:MAX_SUMMARY_CHARS]
        for block in text.split("\n\n"):
            candidate = block.strip()
            if candidate and not candidate.startswith("#"):
                return candidate[:MAX_SUMMARY_CHARS]
        return None
