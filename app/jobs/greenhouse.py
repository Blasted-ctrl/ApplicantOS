"""Greenhouse — job discovery **and real automated submission** (``docs/CONTRACTS.md`` §9).

**Automation posture (binding, golden rule #10).** A Greenhouse job board is a *public
application form*. ``https://job-boards.greenhouse.io/<board>/jobs/<id>`` renders the same
form to an anonymous visitor as to anyone else, and submitting it requires **no account, no
login, no session and no credential of any kind** — which is exactly why
:attr:`GreenhouseProvider.requires_login` is ``False`` and
:attr:`GreenhouseProvider.supports_auto_apply` is ``True``. Discovery reads the public,
unauthenticated Job Board API that employers themselves embed in their careers pages; no
private endpoint is touched, no login wall is circumvented, and every request identifies
itself with :data:`app.jobs.base.USER_AGENT`.

Submission still goes through :func:`app.jobs._apply.run_browser_apply`, so the kill switch
applies unchanged: nothing is ever submitted unless ``settings.auto_apply_enabled`` is on
**and** ``settings.dry_run`` is off **and** the caller did not ask for a dry run (golden
rule #3). Anything the form asks that cannot be answered confidently escalates to manual
review rather than being guessed (golden rule #2).

**What the feed looks like.** ``GET /v1/boards/<board>/jobs?content=true`` returns every
open posting for one employer in a single response, with the description delivered as
*HTML-escaped HTML* — literally ``&lt;p&gt;…&lt;/p&gt;`` — so it is unescaped once and then
converted by :func:`app.jobs._parsing.html_to_text`. There is no server-side keyword,
location or freshness filter and no pagination, so every filter in a
:class:`~app.jobs.base.SearchQuery` is applied here, client-side.

Because that one response can be four megabytes across five hundred postings, this module is
careful about *when* it does expensive work. Cheap tests — title, location, ``updated_at``,
and a substring probe against the still-escaped markup — run over the whole board first, and
only the survivors are unescaped, converted to text, salary-parsed and yielded. A search for
five postings on a five-hundred-posting board therefore parses five descriptions, not five
hundred.

There is no structured compensation field anywhere in the Greenhouse board API, so salary is
recovered from the description — and only from sentences that actually talk about pay (see
:func:`_salary_from_text`). Running a salary parser over a whole job description would happily
report the "$50 million" from an "about us" paragraph as a base salary; not guessing is worth
more than a filled-in field.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from html import unescape
from typing import Any, ClassVar, Final

import structlog

from app.jobs._apply import browser_available, run_browser_apply
from app.jobs._parsing import (
    clean_text,
    html_to_text,
    infer_arrangement,
    infer_employment_type,
    parse_date,
    parse_salary,
)
from app.jobs.base import (
    ApplyContext,
    ApplyResult,
    ATSProvider,
    ProviderError,
    RawPosting,
    SearchQuery,
)
from app.jobs.seeds import boards_from_query
from app.models.enums import ATSProviderName, PluginKind, WorkArrangement
from app.plugins.base import PluginMeta
from app.plugins.registry import plugin

__all__ = [
    "API_ROOT",
    "BOARD_FEED_TTL_SECONDS",
    "BOARD_META_TTL_SECONDS",
    "GreenhouseProvider",
    "HEALTHCHECK_BOARD",
    "JOB_URL_TEMPLATE",
    "MAX_LOOKUP_BOARDS",
    "MAX_SALARY_WINDOWS",
    "MAX_SALARY_WINDOW_PARAGRAPHS",
    "POSTING_TTL_SECONDS",
    "SALARY_WINDOW_CHARS",
    "SALARY_WINDOW_OVERRUN_CHARS",
    "SELECTOR_PACK",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Endpoints
# ======================================================================================

#: Root of the public Job Board API. Documented, unauthenticated, and the same feed
#: employers embed in their own careers pages.
API_ROOT: Final[str] = "https://boards-api.greenhouse.io/v1/boards"

#: ATS-hosted posting page. Employers may configure a "job post URL" on their own domain,
#: in which case this redirects there — which is what the browser layer wants, because the
#: employer's page embeds the very same Greenhouse form.
JOB_URL_TEMPLATE: Final[str] = "https://job-boards.greenhouse.io/{board}/jobs/{job_id}"

#: ATS-hosted board page, used when a posting carries no ``absolute_url`` of its own.
BOARD_URL_TEMPLATE: Final[str] = "https://job-boards.greenhouse.io/{board}"

#: Selector pack name handed to the browser layer by :meth:`GreenhouseProvider.apply`.
SELECTOR_PACK: Final[str] = "greenhouse"

#: The board probed by :meth:`GreenhouseProvider.healthcheck`. Large, long-lived, and its
#: board-metadata document is a few dozen bytes, so the probe is genuinely cheap.
HEALTHCHECK_BOARD: Final[str] = "stripe"

#: Seconds allowed for the healthcheck probe. Deliberately short: a readiness endpoint that
#: blocks for thirty seconds is worse than one that reports "not ready".
HEALTHCHECK_TIMEOUT_SECONDS: Final[float] = 8.0


# ======================================================================================
# Caching
# ======================================================================================

#: TTL for a whole board's posting feed. Shorter than the 30-minute ``jobs.poll_all`` beat
#: interval, so a scheduled discovery run always sees fresh data while the several lookups
#: inside one run — and a user clicking "discover" twice — are served from cache.
BOARD_FEED_TTL_SECONDS: Final[int] = 900

#: TTL for one posting fetched by identifier. Longer than the feed: a single posting is read
#: when scoring or applying, and its text changes far less often than the board's contents.
POSTING_TTL_SECONDS: Final[int] = 3600

#: TTL for a board's metadata document, which is just the employer's display name.
BOARD_META_TTL_SECONDS: Final[int] = 86400


# ======================================================================================
# Limits
# ======================================================================================

#: Boards scanned when resolving a *bare* posting id — one whose board is not knowable from
#: the input and was not seen during this process's discovery. Every scanned feed goes
#: through the cache, so after a discovery run the scan is almost free; on a cold process it
#: costs at most this many requests for one user-initiated lookup.
MAX_LOOKUP_BOARDS: Final[int] = 40

#: Ceiling on the in-process "which board did this posting id come from" memo. Reached only
#: after roughly twenty thousand distinct postings, at which point the memo is dropped
#: wholesale rather than grown without bound; the next lookup simply re-scans.
MAX_MEMOIZED_IDS: Final[int] = 20_000

#: Compensation disclosures examined by :func:`_salary_from_text` before it gives up. A
#: posting states its range once or twice — once per pay zone at most — so an unbounded scan
#: of a 100 KB description is wasted work.
MAX_SALARY_WINDOWS: Final[int] = 12

#: Characters read after a compensation phrase when looking for the figures it introduces.
#: Generous enough for the layouts US pay-transparency disclosures actually use — a heading, a
#: blank line and the amounts, or a heading, a pay-zone caption and then the amounts — and
#: tight enough that an unrelated number further down the section is out of reach.
SALARY_WINDOW_CHARS: Final[int] = 280

#: Extra characters the window may run on for, so that it never ends in the middle of a
#: number. Cutting ``"$71-$95"`` at ``"$71-$9"`` does not lose the figure, it *changes* it —
#: nine dollars an hour instead of ninety-five — which is the one failure mode a salary parser
#: must not have.
SALARY_WINDOW_OVERRUN_CHARS: Final[int] = 32

#: Paragraphs a window may traverse while looking for the figures. The window closes as soon
#: as it has found them (see :func:`_salary_window`); this is the ceiling for a phrase that
#: introduces nothing, so that such a window cannot creep into the next section of the
#: posting.
MAX_SALARY_WINDOW_PARAGRAPHS: Final[int] = 4


# ======================================================================================
# URL shapes
# ======================================================================================

#: ATS-hosted posting URL, covering both the legacy ``boards.greenhouse.io`` host and the
#: current ``job-boards.greenhouse.io`` one.
_HOSTED_JOB_RE: Final[re.Pattern[str]] = re.compile(
    r"\bboards\.greenhouse\.io/(?P<board>[a-z0-9][a-z0-9._-]*)/jobs/(?P<job_id>\d+)",
    re.IGNORECASE,
)

#: The API URL itself, so that a link copied out of a debugging session round-trips.
_API_JOB_RE: Final[re.Pattern[str]] = re.compile(
    r"\bboards-api\.greenhouse\.io/v1/boards/(?P<board>[a-z0-9][a-z0-9._-]*)"
    r"/jobs/(?P<job_id>\d+)",
    re.IGNORECASE,
)

#: The embedded application widget, whose board is in ``for=`` and whose posting is in
#: ``token=``. Written as two lookaheads anchored just past the ``?`` because the two
#: parameters occur in either order; each may therefore be the first parameter or follow an
#: ``&``, and nothing else may be glued to its name.
_EMBED_JOB_RE: Final[re.Pattern[str]] = re.compile(
    r"\bboards\.greenhouse\.io/embed/job_app\?"
    r"(?=(?:[^#]*&)?for=(?P<board>[a-z0-9][a-z0-9._-]*))"
    r"(?=(?:[^#]*&)?token=(?P<job_id>\d+))",
    re.IGNORECASE,
)

#: The form an employer's own careers page uses when it embeds a board: any URL at all,
#: carrying ``?gh_jid=<id>``. The board is not in the URL, so it is inferred from the host.
_GH_JID_RE: Final[re.Pattern[str]] = re.compile(r"[?&]gh_jid=(?P<job_id>\d+)", re.IGNORECASE)

#: An ATS-hosted board URL with no posting in it.
_HOSTED_BOARD_RE: Final[re.Pattern[str]] = re.compile(
    r"\bboards\.greenhouse\.io/(?P<board>[a-z0-9][a-z0-9._-]*)",
    re.IGNORECASE,
)

#: ``<board>/<id>`` or ``<board>:<id>``, the composite form this provider accepts so that a
#: caller holding both halves never has to build a URL.
_COMPOSITE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<board>[a-z0-9][a-z0-9._-]*)\s*[/:]\s*(?P<job_id>\d+)$",
    re.IGNORECASE,
)

#: A Greenhouse posting identifier on its own: a positive integer.
_BARE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^\d+$")

#: Host of any URL, used to infer a board token from an employer's own careers domain.
_HOST_RE: Final[re.Pattern[str]] = re.compile(r"^(?:[a-z][a-z0-9+.-]*:)?//([^/?#]+)", re.IGNORECASE)

#: Hostname labels that are never an employer's name.
_GENERIC_HOST_LABELS: Final[frozenset[str]] = frozenset(
    {"www", "jobs", "careers", "boards", "job-boards", "apply", "job", "career", "hire", "work"}
)

#: Public suffixes stripped when reducing a host to its employer label, longest first.
_HOST_SUFFIXES: Final[tuple[str, ...]] = (
    ".co.uk",
    ".com.au",
    ".co.jp",
    ".com",
    ".org",
    ".net",
    ".io",
    ".ai",
    ".co",
    ".dev",
    ".app",
    ".inc",
)


# ======================================================================================
# Payload helpers
# ======================================================================================

#: Word separators inside a board token.
_TOKEN_SEPARATOR_RE: Final[re.Pattern[str]] = re.compile(r"[-_\s]+")

#: What :func:`app.jobs._parsing.html_to_text` emits between paragraphs.
_PARAGRAPH_BREAK: Final[str] = "\n\n"

#: Cheap probe for "does this text state a monetary amount?", used by :func:`_salary_window`
#: to decide where the disclosure ends. It recognises the four shapes money is written in — a
#: currency symbol before a number, an ISO code before a number, grouped thousands, and a
#: ``k``/``m`` magnitude suffix — and deliberately does *not* recognise a bare run of digits,
#: because a postal code in a pay-zone caption ("New York, NY 10001") would then close the
#: window one paragraph before the figures it was looking for.
_MONEY_PROBE_RE: Final[re.Pattern[str]] = re.compile(
    r"[$€£¥₹₩₪₺₽]\s*\d"
    r"|\b[A-Z]{3}\s*\d"
    r"|\d{1,3}(?:[.,]\d{3})+"
    r"|\d+(?:\.\d+)?\s*[kKmM]\b"
)

#: Phrases that introduce a pay figure and nothing else.
#:
#: Deliberately narrow. The bare words "salary" and "compensation" are *not* here, because
#: they head the benefits paragraph of almost every posting — "we offer competitive
#: compensation and benefits, a $20,000 fertility benefit, …" — and anchoring on them would
#: report a benefit allowance as a base salary. Every phrase below is one that a posting uses
#: only when it is about to state what the role pays.
_COMPENSATION_CONTEXT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:"
    r"(?:base|annual(?:ized|ised)?|expected|anticipated|estimated|starting|target(?:ed)?|"
    r"total|hourly|monthly|projected|advertised|posted)?\s*"
    r"(?:salary|pay|compensation|cash\s+compensation)\s*"
    r"(?:range|band|scale|rate)"
    r"|base\s+(?:salary|pay)"
    r"|(?:salary|pay|compensation)\s+for\s+(?:this|the)\s+(?:role|position|job)"
    r"|(?:expected|anticipated|estimated|starting|advertised)\s+(?:salary|pay|compensation)"
    r"|annual(?:ized|ised)?\s+(?:salary|pay)"
    r"|hourly\s+(?:rate|wage)"
    r"|(?:on[-\s])?target\s+earnings|\bOTE\b"
    r"|remuneration"
    r")",
    re.IGNORECASE,
)

#: Sort key for a posting whose ``updated_at`` could not be parsed. Sorting newest-first
#: with ``reverse=True`` therefore puts undated postings last, which is right: an absent date
#: is not a claim of freshness.
_UNDATED: Final[datetime] = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _company_from_token(token: str) -> str:
    """Turn a board token into a presentable employer name.

    The last-resort source for :attr:`~app.jobs.base.RawPosting.company_name`: Greenhouse
    normally states ``company_name`` on every posting, and the board metadata document
    carries the employer's own spelling, so this is reached only when a board publishes
    neither. Company resolution and enrichment happen later in the pipeline, so a
    approximate-but-recognisable name here is enough to key on.

    Args:
        token: A board token such as ``"zed-industries"``.

    Returns:
        The de-slugged name (``"Zed Industries"``), or ``""`` for an empty token.
    """
    parts = [part for part in _TOKEN_SEPARATOR_RE.split(clean_text(token)) if part]
    return " ".join(part[:1].upper() + part[1:] for part in parts)


def _board_from_host(url: str) -> str:
    """Guess the board token from an employer's own careers host.

    A posting embedded in a company's website is addressed as
    ``https://stripe.com/jobs/search?gh_jid=8023928`` — the identifier is in the query string
    and the board token appears nowhere. The registrable label of the host is the board token
    often enough to be worth trying, and a wrong guess costs one 404 that
    :meth:`~app.jobs.base.ATSProvider._request` turns into a clean
    :class:`~app.jobs.base.PostingUnavailableError`.

    Args:
        url: The URL the identifier came from.

    Returns:
        The inferred token, or ``""`` when the host yields nothing usable — a bare
        ``greenhouse.io`` host, or a purely generic label such as ``jobs.`` or ``careers.``.
    """
    match = _HOST_RE.match(url.strip())
    if not match:
        return ""
    host = match.group(1).split("@")[-1].split(":")[0].strip(".").lower()
    if not host or "greenhouse.io" in host or "grnh.se" in host:
        return ""

    for suffix in _HOST_SUFFIXES:
        if host.endswith(suffix):
            host = host[: -len(suffix)]
            break

    labels = [label for label in host.split(".") if label and label not in _GENERIC_HOST_LABELS]
    return labels[-1] if labels else ""


def _unescape_markup(value: Any) -> str:
    """Undo Greenhouse's HTML escaping of an already-HTML description.

    ``content`` arrives as ``&lt;p&gt;Build things.&lt;/p&gt;`` — markup that has been
    entity-escaped once. Unescaping unconditionally would be wrong for a board that
    (someday) sends raw markup, because a genuine ``&lt;`` inside a code sample would become
    a ``<`` and then be eaten as a tag. Comparing how many escaped and raw angle brackets the
    document contains settles it without guessing: escaped documents have hundreds of the
    former and none of the latter.

    Args:
        value: The raw ``content`` field. Non-strings yield ``""``.

    Returns:
        Markup ready for :func:`app.jobs._parsing.html_to_text`.
    """
    if not isinstance(value, str) or not value:
        return ""
    return unescape(value) if value.count("&lt;") > value.count("<") else value


def _salary_window(text: str, start: int, end: int) -> str:
    """Carve out the text a compensation phrase introduces.

    The window **closes at the paragraph break that follows the first monetary amount**. That
    one rule handles both layouts these disclosures use, and closing on it is what keeps the
    window out of the section that comes next::

        Pay Range                      Base Pay Range:
                                       Zone 1 (Menlo Park, CA; New York, NY)
        $170.40-$255.60 USD
                                       $166,000-$195,000 USD
        About Databricks
        … More than 20,000 …           Click here to learn more about …

    Reading one paragraph too far on the left turns "20,000 organizations" into a
    twenty-thousand-dollar salary; stopping one paragraph too early on the right loses the
    figures entirely. Two further bounds keep an unproductive window from wandering: at most
    :data:`MAX_SALARY_WINDOW_PARAGRAPHS` paragraphs, and at most *end* characters — and where
    that character budget is what runs out, the window overruns by up to
    :data:`SALARY_WINDOW_OVERRUN_CHARS` rather than cutting a number in half, since
    ``"$71-$95"`` truncated to ``"$71-$9"`` does not lose the figure, it changes it.

    Args:
        text: The description.
        start: Index of the compensation phrase.
        end: The nominal end of the window, ``start`` plus the character budget.

    Returns:
        The window text.
    """
    stop = min(end, len(text))

    cursor = start
    for _ in range(MAX_SALARY_WINDOW_PARAGRAPHS):
        found = text.find(_PARAGRAPH_BREAK, cursor, stop)
        if found < 0:
            break
        if _MONEY_PROBE_RE.search(text, start, found):
            return text[start:found]
        cursor = found + len(_PARAGRAPH_BREAK)
    else:
        # Nothing money-shaped in any of them; stop rather than read on into the next section.
        return text[start : cursor - len(_PARAGRAPH_BREAK)]

    limit = min(len(text), end + SALARY_WINDOW_OVERRUN_CHARS)
    while stop < limit and (text[stop].isalnum() or text[stop] in ",.'"):
        stop += 1
    return text[start:stop]


def _salary_from_text(text: str) -> tuple[int | None, int | None, str | None]:
    """Recover an advertised salary range from a job description.

    The Greenhouse board API carries no structured compensation field of any kind, so the
    description is the only source there is. It is also full of numbers that are emphatically
    not salaries — funding rounds, customer counts, revenue targets, benefit allowances — so
    this never parses the description as a whole. Instead it anchors on the phrases that
    introduce a pay figure (:data:`_COMPENSATION_CONTEXT_RE`) and reads only the
    :data:`SALARY_WINDOW_CHARS` characters that follow one.

    A window rather than a sentence, because a sentence is the wrong unit for how these
    disclosures are actually written. The dominant layout is a heading and then the figures
    on their own line::

        Annual Base Salary Range:

        $165,000-$190,000 USD

    Sentence splitting puts the phrase and the numbers in different pieces and finds nothing;
    a window spans the blank line and finds the range.

    The first window yielding a plausible figure wins. Postings that publish several
    geographic zones list the applicable one first, which is what US pay-transparency law
    asks for, and taking the first also limits the damage a stray later number could do.

    Args:
        text: The description, already converted to plain text.

    Returns:
        ``(minimum, maximum, currency)`` exactly as
        :func:`app.jobs._parsing.parse_salary` defines it, or ``(None, None, None)`` when the
        posting discloses nothing. ``None`` is the honest answer and the scorer handles it;
        an invented number would not be recoverable.
    """
    if not text:
        return (None, None, None)

    for index, match in enumerate(_COMPENSATION_CONTEXT_RE.finditer(text)):
        if index >= MAX_SALARY_WINDOWS:
            break
        window = _salary_window(text, match.start(), match.end() + SALARY_WINDOW_CHARS)
        minimum, maximum, currency = parse_salary(window)
        if minimum is not None or maximum is not None:
            return (minimum, maximum, currency)
    return (None, None, None)


def _location_names(job: Mapping[str, Any]) -> list[str]:
    """Collect every place a posting says it is in.

    Greenhouse states one primary location and, separately, the offices the requisition is
    attached to. Both are consulted when matching a caller's location filter, because a
    posting whose primary location is "Remote" is frequently attached to the office that owns
    the headcount.

    Args:
        job: One posting from the board feed.

    Returns:
        Cleaned, non-empty location strings, primary first.
    """
    names: list[str] = []
    primary = job.get("location")
    if isinstance(primary, Mapping):
        names.append(clean_text(primary.get("name")))
    elif isinstance(primary, str):
        names.append(clean_text(primary))

    offices = job.get("offices")
    if isinstance(offices, Sequence) and not isinstance(offices, (str, bytes)):
        for office in offices:
            if isinstance(office, Mapping):
                names.append(clean_text(office.get("name")))

    return [name for name in names if name]


def _matches_any(terms: Sequence[str], haystacks: Iterable[str]) -> bool:
    """Return whether any of *terms* appears in any of *haystacks*.

    OR semantics, as :class:`~app.jobs.base.SearchQuery` documents for both ``keywords`` and
    ``locations``, and case-insensitive substring matching so that ``"ml"`` finds "MLOps" and
    ``"new york"`` finds "New York, NY (HQ)".

    Args:
        terms: The caller's filter values; an empty sequence matches everything, because an
            unrestricted filter must never narrow the result set.
        haystacks: Strings to search.

    Returns:
        ``True`` when the filter is unrestricted or one term matched.
    """
    if not terms:
        return True
    lowered = [haystack.lower() for haystack in haystacks if haystack]
    return any(term.lower() in haystack for term in terms for haystack in lowered)


@plugin
class GreenhouseProvider(ATSProvider):
    """Discovery and automated submission against Greenhouse job boards.

    One instance per process (providers are plugin-registry singletons), so the HTTP
    connection pool and the posting-id memo are shared across every discovery run.

    Class attributes:
        meta: Plugin identity, registered under the name ``"greenhouse"``.
        name: :attr:`~app.models.enums.ATSProviderName.GREENHOUSE`.
        supports_auto_apply: ``True`` — the application form is public and account-free.
        requires_login: ``False`` — neither discovery nor submission needs a session.
        URL_PATTERNS: Every shape a Greenhouse posting URL takes in the wild.
    """

    meta: ClassVar[PluginMeta] = PluginMeta(
        kind=PluginKind.PROVIDER,
        name=ATSProviderName.GREENHOUSE.value,
        version="1.0.0",
        display_name="Greenhouse",
        description=(
            "Greenhouse job boards — public JSON feed for discovery, public application "
            "form for submission. No account required."
        ),
        author="ApplicantOS",
        capabilities=frozenset({"search", "fetch", "auto_apply"}),
    )
    name: ClassVar[ATSProviderName] = ATSProviderName.GREENHOUSE
    supports_auto_apply: ClassVar[bool] = True
    requires_login: ClassVar[bool] = False
    URL_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"\bboards\.greenhouse\.io/", re.IGNORECASE),
        re.compile(r"\bboards-api\.greenhouse\.io/", re.IGNORECASE),
        re.compile(r"\bgreenhouse\.io/(?:embed|boards)/", re.IGNORECASE),
        re.compile(r"[?&]gh_jid=\d+", re.IGNORECASE),
        re.compile(r"\bgrnh\.se/", re.IGNORECASE),
    ]

    def __init__(self, settings: Any, **kw: Any) -> None:
        """Construct the provider and its in-process lookup memos.

        Args:
            settings: Application settings, supplied by the plugin registry.
            **kw: Extra construction options, kept on ``self.options``.
        """
        super().__init__(settings, **kw)
        self._board_by_job_id: dict[str, str] = {}
        self._board_names: dict[str, str] = {}

    # -- transport ----------------------------------------------------------------------

    async def _cached_json(
        self,
        url: str,
        *,
        key_parts: Sequence[Any],
        ttl: int,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """``GET`` a JSON document, reading through the shared cache.

        Golden rule #9: job descriptions are cached by contract (``docs/CONTRACTS.md`` §7),
        under :attr:`~app.cache.keys.NAMESPACES.POSTING`. The cache is imported lazily
        because it pulls in the settings and, potentially, a Redis client; a provider that is
        registered but never polled should pay for neither.

        Args:
            url: Absolute URL to request.
            key_parts: Everything that distinguishes this request from another in the same
                namespace. The provider name is prepended automatically.
            ttl: Lifetime of the cached document, in seconds.
            params: Query parameters, which must also be reflected in *key_parts*.

        Returns:
            The decoded JSON document.

        Raises:
            ProviderError: On any transport or status failure. Cache backends degrade
                silently on their own errors, so a failure here is always the ATS.
        """
        from app.cache import NAMESPACES, get_cache, make_key  # noqa: PLC0415 - lazy by policy

        cache = get_cache()
        key = make_key(NAMESPACES.POSTING, self.provider_name.value, *key_parts)

        async def factory() -> Any:
            return await self._get_json(url, params=params)

        return await cache.get_or_set(key, factory, ttl=ttl)

    async def _board_jobs(self, board: str) -> list[dict[str, Any]]:
        """Return every open posting on one board, descriptions included.

        Args:
            board: The board token, e.g. ``"stripe"``.

        Returns:
            The postings, in the order Greenhouse returned them. Empty when the board exists
            but is currently hiring for nothing.

        Raises:
            PostingUnavailableError: When the board token is unknown — the normal outcome for
                an employer that has migrated ATS or renamed its board.
            ProviderError: On any other provider failure.
        """
        payload = await self._cached_json(
            f"{API_ROOT}/{board}/jobs",
            params={"content": "true"},
            key_parts=(board, "jobs", "content"),
            ttl=BOARD_FEED_TTL_SECONDS,
        )
        jobs = payload.get("jobs") if isinstance(payload, Mapping) else None
        if not isinstance(jobs, Sequence) or isinstance(jobs, (str, bytes)):
            self.logger.warning("greenhouse.board_feed_malformed", board=board)
            return []
        return [job for job in jobs if isinstance(job, Mapping)]

    async def _board_display_name(self, board: str) -> str:
        """Return the employer's own spelling of its name for one board.

        Consulted only when a posting omits ``company_name``, which is rare — so the extra
        request is rare too, and it is both cached and memoised per process.

        Args:
            board: The board token.

        Returns:
            The employer name from the board's metadata document, or the de-slugged token
            when that document is unavailable.
        """
        remembered = self._board_names.get(board)
        if remembered is not None:
            return remembered

        name = ""
        try:
            payload = await self._cached_json(
                f"{API_ROOT}/{board}",
                key_parts=(board, "board"),
                ttl=BOARD_META_TTL_SECONDS,
            )
        except ProviderError as exc:
            self.logger.debug("greenhouse.board_meta_unavailable", board=board, error=str(exc))
        else:
            if isinstance(payload, Mapping):
                name = clean_text(payload.get("name"))

        resolved = name or _company_from_token(board)
        self._board_names[board] = resolved
        return resolved

    async def _company_for(self, job: Mapping[str, Any], board: str) -> str:
        """Resolve the employer name for one posting.

        Args:
            job: The posting payload.
            board: The board it came from.

        Returns:
            ``company_name`` when the posting states it, the board's metadata name when it
            does not, and the de-slugged token as a last resort.
        """
        stated = clean_text(job.get("company_name"))
        return stated or await self._board_display_name(board)

    def _remember(self, job_id: str, board: str) -> None:
        """Record which board a posting id belongs to, for later bare-id lookups.

        Args:
            job_id: The Greenhouse posting identifier.
            board: The board it was discovered on.
        """
        if len(self._board_by_job_id) >= MAX_MEMOIZED_IDS:
            self.logger.debug(
                "greenhouse.id_memo_reset", entries=len(self._board_by_job_id)
            )
            self._board_by_job_id.clear()
        self._board_by_job_id[job_id] = board

    # -- mapping ------------------------------------------------------------------------

    def _to_raw(self, job: Mapping[str, Any], board: str, company: str) -> RawPosting:
        """Convert one Greenhouse posting into the pipeline's currency.

        Args:
            job: The posting payload, exactly as the feed delivered it.
            board: The board token it came from.
            company: The resolved employer name.

        Returns:
            The :class:`~app.jobs.base.RawPosting`, carrying the untouched payload in
            :attr:`~app.jobs.base.RawPosting.raw` so that a parsing fix can be replayed
            without re-crawling.
        """
        job_id = str(job.get("id") or "").strip()
        description = html_to_text(_unescape_markup(job.get("content")))
        locations = _location_names(job)
        location = locations[0] if locations else ""
        title = clean_text(job.get("title"))

        arrangement = infer_arrangement(location)
        if arrangement is WorkArrangement.UNKNOWN:
            arrangement = infer_arrangement(f"{title}\n{description}")

        salary_min, salary_max, currency = _salary_from_text(description)
        hosted_url = JOB_URL_TEMPLATE.format(board=board, job_id=job_id)
        url = clean_text(job.get("absolute_url")) or hosted_url

        return RawPosting(
            provider=ATSProviderName.GREENHOUSE,
            external_id=job_id,
            url=url,
            title=title,
            company_name=company,
            description=description or None,
            location=location or None,
            work_arrangement=arrangement,
            employment_type=infer_employment_type(title, description),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            posted_at=job.get("first_published") or job.get("updated_at"),
            closes_at=job.get("application_deadline"),
            apply_url=hosted_url if hosted_url != url else None,
            raw=dict(job),
        )

    # -- filtering ----------------------------------------------------------------------

    def _candidates(
        self, jobs: Sequence[Mapping[str, Any]], q: SearchQuery
    ) -> list[tuple[Mapping[str, Any], datetime | None]]:
        """Apply every cheap filter to a board's feed and order what survives.

        Deliberately runs before any description is unescaped or converted: the tests here
        touch a title, a location string, a timestamp and — for keywords — a substring probe
        against the still-escaped markup. Escaped markup is a superset of the text a reader
        sees, so the probe never drops a posting the full-text test would have kept; it only
        avoids parsing the ones that could not possibly match.

        Args:
            jobs: Every posting on one board.
            q: The caller's query.

        Returns:
            ``(job, updated_at)`` pairs that passed, newest first. Postings with no usable
            timestamp sort last, because an unknown date is not a claim of freshness.
        """
        keywords = q.keywords
        locations = q.locations
        survivors: list[tuple[Mapping[str, Any], datetime | None]] = []

        for job in jobs:
            if not str(job.get("id") or "").strip():
                continue

            updated_at = parse_date(job.get("updated_at"))
            if not q.matches_freshness(updated_at):
                continue

            title = clean_text(job.get("title"))
            place_names = _location_names(job)
            if locations and not _matches_any(locations, place_names):
                continue

            if keywords:
                content = job.get("content")
                haystacks = [title, content if isinstance(content, str) else ""]
                if not _matches_any(keywords, haystacks):
                    continue

            if q.remote_only and not self._may_be_remote(job, title, place_names):
                continue

            survivors.append((job, updated_at))

        survivors.sort(key=lambda pair: pair[1] or _UNDATED, reverse=True)
        return survivors

    @staticmethod
    def _may_be_remote(
        job: Mapping[str, Any], title: str, place_names: Sequence[str]
    ) -> bool:
        """Return whether a posting could plausibly be remote, without parsing it.

        A deliberate over-approximation used only to skip work: it says "yes" to anything
        whose title, location or raw markup mentions remote work anywhere, and the strict
        test — :func:`app.jobs._parsing.infer_arrangement` over the converted text, which
        honours negation and prefers "hybrid" — is applied afterwards in
        :meth:`search`. Because this never says "no" to a posting the strict test would have
        kept, using it cannot narrow the result set.

        Args:
            job: The posting payload.
            title: The cleaned title.
            place_names: Every location the posting states.

        Returns:
            ``True`` when the posting is worth converting and testing properly.
        """
        if any("remote" in name.lower() for name in place_names):
            return True
        if "remote" in title.lower():
            return True
        content = job.get("content")
        return isinstance(content, str) and "remote" in content.lower()

    # -- the provider contract ----------------------------------------------------------

    async def search(self, q: SearchQuery) -> AsyncIterator[RawPosting]:
        """Yield postings from every requested board, newest first within each board.

        Boards come from ``q.extra`` when the caller named any, and from
        :func:`app.jobs.seeds.boards_from_query`'s curated defaults otherwise. They are
        polled one at a time, and **a board that fails degrades that board only** — an
        employer that has migrated ATS answers 404, which is expected, gets logged, and must
        never abort a discovery run across the other thirty-nine.

        Ordering is newest-first *within* a board rather than globally: a global ordering
        would require buffering every board's feed before yielding anything, which would
        defeat both the caller's ``limit`` and the lazy conversion that makes this method
        cheap.

        Args:
            q: What to look for. ``keywords``, ``locations``, ``posted_within_days``,
                ``remote_only`` and ``limit`` are all honoured client-side, because the board
                API offers no server-side filter at all.

        Yields:
            One :class:`~app.jobs.base.RawPosting` per matching advertisement, at most
            ``q.limit`` of them.
        """
        boards = boards_from_query(self.provider_name, q.extra)
        if not boards:
            self.logger.warning("greenhouse.no_boards", extra_keys=sorted(q.extra))
            return

        log = self.logger.bind(boards=len(boards), limit=q.limit)
        log.info("greenhouse.search_started")

        yielded = 0
        for board in boards:
            if yielded >= q.limit:
                break

            try:
                jobs = await self._board_jobs(board)
            except ProviderError as exc:
                log.warning(
                    "greenhouse.board_failed",
                    board=board,
                    status_code=exc.status_code,
                    transient=exc.transient,
                    error=str(exc),
                )
                continue

            candidates = self._candidates(jobs, q)
            log.debug(
                "greenhouse.board_scanned",
                board=board,
                postings=len(jobs),
                candidates=len(candidates),
            )

            for job, _updated_at in candidates:
                if yielded >= q.limit:
                    break
                company = await self._company_for(job, board)
                raw = self._to_raw(job, board, company)
                if q.remote_only and raw.work_arrangement is not WorkArrangement.REMOTE:
                    continue
                self._remember(raw.external_id, board)
                yielded += 1
                yield raw

        log.info("greenhouse.search_finished", yielded=yielded)

    async def fetch_posting(self, id_or_url: str) -> RawPosting | None:
        """Fetch one posting by identifier or by URL.

        Every shape a caller can plausibly hold is accepted:

        * an ATS-hosted URL — ``boards.greenhouse.io/<board>/jobs/<id>`` or
          ``job-boards.greenhouse.io/<board>/jobs/<id>``;
        * the API URL itself;
        * the embedded widget URL, ``…/embed/job_app?for=<board>&token=<id>``;
        * an employer's own careers URL carrying ``?gh_jid=<id>``, whose board is inferred
          from the host and, failing that, resolved by scan;
        * the composite ``<board>/<id>``;
        * a bare numeric id.

        A bare id has no board in it, and Greenhouse has no board-independent endpoint — the
        same id under the wrong board is a 404. It is therefore resolved from the memo that
        :meth:`search` fills in and, failing that, by scanning up to
        :data:`MAX_LOOKUP_BOARDS` board feeds. Every one of those reads goes through the
        cache, so immediately after a discovery run the scan costs no requests at all.

        Args:
            id_or_url: A Greenhouse posting identifier or a posting URL.

        Returns:
            The posting, or ``None`` when *id_or_url* carries no identifier this provider can
            address, or when a scan found no board claiming it.

        Raises:
            PostingUnavailableError: When the board is known and the posting is gone.
            ProviderError: On any other provider failure.
        """
        reference = clean_text(id_or_url)
        if not reference:
            return None

        board, job_id = self._parse_reference(reference)
        if not job_id:
            self.logger.debug("greenhouse.reference_unrecognised", reference=reference[:120])
            return None

        if not board:
            board = self._board_by_job_id.get(job_id, "")
        if not board:
            return await self._scan_for(job_id)

        payload = await self._cached_json(
            f"{API_ROOT}/{board}/jobs/{job_id}",
            key_parts=(board, "job", job_id),
            ttl=POSTING_TTL_SECONDS,
        )
        if not isinstance(payload, Mapping) or not payload.get("id"):
            self.logger.warning("greenhouse.posting_malformed", board=board, job_id=job_id)
            return None

        self._remember(job_id, board)
        return self._to_raw(payload, board, await self._company_for(payload, board))

    def _parse_reference(self, reference: str) -> tuple[str, str]:
        """Split a caller-supplied reference into ``(board, job_id)``.

        Args:
            reference: The cleaned identifier or URL.

        Returns:
            The board token (``""`` when the reference does not carry one) and the posting
            identifier (``""`` when the reference carries none at all).
        """
        for pattern in (_HOSTED_JOB_RE, _API_JOB_RE, _EMBED_JOB_RE):
            match = pattern.search(reference)
            if match:
                return match.group("board"), match.group("job_id")

        match = _GH_JID_RE.search(reference)
        if match:
            return _board_from_host(reference), match.group("job_id")

        match = _COMPOSITE_RE.match(reference)
        if match:
            return match.group("board"), match.group("job_id")

        if _BARE_ID_RE.match(reference):
            return "", reference

        match = _HOSTED_BOARD_RE.search(reference)
        if match:
            # A board URL with no posting in it: addressable, but not a posting.
            return match.group("board"), ""
        return "", ""

    async def _scan_for(self, job_id: str) -> RawPosting | None:
        """Find which board claims *job_id* by reading candidate board feeds.

        Args:
            job_id: The posting identifier.

        Returns:
            The posting, or ``None`` when no scanned board lists it.
        """
        boards = boards_from_query(self.provider_name, self.options)[:MAX_LOOKUP_BOARDS]
        if not boards:
            return None

        self.logger.info("greenhouse.bare_id_scan_started", job_id=job_id, boards=len(boards))
        for board in boards:
            try:
                jobs = await self._board_jobs(board)
            except ProviderError as exc:
                self.logger.debug(
                    "greenhouse.board_failed", board=board, error=str(exc)
                )
                continue
            for job in jobs:
                if str(job.get("id") or "").strip() == job_id:
                    self._remember(job_id, board)
                    self.logger.info(
                        "greenhouse.bare_id_scan_hit", job_id=job_id, board=board
                    )
                    return self._to_raw(job, board, await self._company_for(job, board))

        self.logger.info("greenhouse.bare_id_scan_missed", job_id=job_id, boards=len(boards))
        return None

    async def apply(self, ctx: ApplyContext) -> ApplyResult:
        """Submit an application through the public Greenhouse form.

        Delegates to :func:`app.jobs._apply.run_browser_apply` so that the kill switch, the
        dry-run guard and the review-escalation rules live in exactly one place
        (``docs/CONTRACTS.md`` §12). Nothing is submitted unless
        ``settings.auto_apply_enabled`` is on and ``settings.dry_run`` is off; anything the
        form asks that cannot be answered confidently escalates to manual review.

        Args:
            ctx: The posting, the profile, the rendered documents and the planned answers.

        Returns:
            The outcome. ``ok=True`` only when an application was genuinely submitted and
            verified.

        Raises:
            UnsupportedFlowError: When browser automation is not installed in this
                deployment, which routes the application to manual review.
            ProviderError: Propagated unchanged from the browser layer.
        """
        return await run_browser_apply(ctx, SELECTOR_PACK)

    async def healthcheck(self) -> bool:
        """Report whether the Greenhouse board API is reachable right now.

        Probes :data:`HEALTHCHECK_BOARD`'s metadata document — a few dozen bytes, no
        descriptions, no pagination — with a single attempt and a short timeout, and
        deliberately bypasses the cache so that the answer describes the network rather than
        the last fifteen minutes.

        Returns:
            ``True`` when the API answered. Browser availability is logged alongside but does
            not decide the result: discovery is useful on a machine that can never submit,
            and a readiness probe that failed for a missing Playwright install would take the
            whole provider offline for no reason.
        """
        try:
            payload = await self._get_json(
                f"{API_ROOT}/{HEALTHCHECK_BOARD}",
                timeout=HEALTHCHECK_TIMEOUT_SECONDS,
                max_attempts=1,
            )
        except ProviderError as exc:
            self.logger.warning(
                "greenhouse.healthcheck_failed",
                board=HEALTHCHECK_BOARD,
                status_code=exc.status_code,
                error=str(exc),
            )
            return False

        healthy = isinstance(payload, Mapping) and bool(payload.get("name"))
        self.logger.debug(
            "greenhouse.healthcheck",
            board=HEALTHCHECK_BOARD,
            healthy=healthy,
            browser_available=browser_available(),
        )
        return healthy
