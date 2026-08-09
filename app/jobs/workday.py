"""Workday — discovery only, and deliberately so.

**Auto-apply posture (binding, ``docs/CONTRACTS.md`` §9, golden rule #10):**
:meth:`WorkdayProvider.apply` raises :class:`~app.jobs.base.UnsupportedFlowError`. Every
Workday posting this provider discovers is surfaced for **manual review**. That is a product
decision, not a missing feature, and it will not change.

Three independent facts make automated Workday submission impossible to do reliably *or*
safely:

**It is account-gated.** Applying requires creating an account inside the employer's own
Workday tenant — a password, an emailed verification link, and often a security question or
a one-time code. There is no guest path on most tenants. Automating that means storing and
replaying credentials against dozens of separate tenants, each of which is a distinct
employer relationship the user owns and we do not.

**It is multi-step, and the steps are tenant-configured.** A Workday application is a wizard:
"My Information", "My Experience", "Application Questions", "Voluntary Disclosures",
"Self Identify", "Review". Each employer switches sections on and off, reorders them, adds
custom questionnaires, and rewrites the wording. Two tenants running the same Workday version
present two different applications. A selector pack cannot encode a form whose shape is
configuration data — :data:`app.browser.selectors.WORKDAY` exists for field discovery and
blocker detection, and it declares ``supports_auto_apply=False``.

**The failure mode is unacceptable.** A half-completed Workday application is not a no-op.
It creates a real candidate record against a real requisition, visible to a real recruiter,
with whatever the automation managed to enter before it lost its way — and the "Review" step
means anything submitted was, formally, reviewed and confirmed by the applicant. Guessing
there is worse than asking (golden rule #2), so this provider always asks.

What it *does* do, thoroughly, is find the jobs.

Discovery runs against Workday's CXS JSON API — the same endpoints the career site's own
front end calls, which is why the data is complete and correctly typed rather than scraped
out of rendered HTML:

``POST {host}/wday/cxs/{tenant}/{site}/jobs``
    Body ``{"appliedFacets": {}, "limit": 20, "offset": N, "searchText": "..."}``; returns
    ``{"total": …, "jobPostings": [{"title", "externalPath", "locationsText", "postedOn",
    "bulletFields"}]}``.
``GET {host}/wday/cxs/{tenant}/{site}/job/{path}``
    The full posting: description, requisition id, time type, remote type, hiring
    organisation and canonical external URL.

The one genuinely hard part is that a Workday URL is
``https://{tenant}.wd{N}.myworkdayjobs.com/{site}``, where the shard ``wd{N}`` and the career
site name are per-tenant and cannot be derived from the employer name.
:mod:`app.jobs.seeds` therefore ships bare tenant tokens and says so; this module resolves
the rest by probing and **verifying** — every candidate shard/site pair is confirmed by a
real CXS response before it is used, so nothing here is a guess that could silently point
discovery at the wrong employer. Resolutions are cached, positively and negatively, and the
number of new tenants resolved per run is capped so a cold start cannot turn into a probe
storm.

**How resolution actually works, and why it is `robots.txt`.** Measured against the live
service on 2026-08-09: the tenant *root* — ``https://{tenant}.wd{N}.myworkdayjobs.com/`` —
answers **406 for every tenant, on every shard, with any User-Agent**, browser strings
included. An earlier version of this module resolved by fetching that root and reading the
career-site name out of the single-page app it used to bootstrap; since that page no longer
exists, resolution failed for every tenant and Workday discovery returned nothing, silently,
on every install. Three endpoints do still answer, and between them they give an exact
answer instead of a guess:

``GET {host}/robots.txt``
    **200 only on the shard the tenant lives on**; every other shard answers 422, and a
    migrated tenant answers 410 ``ERR_TENANT_MIGRATED``. So one ~200-byte request per
    candidate shard identifies the shard. Better still, its ``Sitemap:`` and ``Allow:``
    lines *name the career sites* — ``Allow: /NVIDIAExternalCareerSite/`` — which is the one
    thing that cannot be guessed. Reading it is also the correct posture: it is the file the
    employer publishes for exactly this audience, and a path it ``Disallow``\\ s is never
    used here, even though the CXS API would serve it.
``POST {host}/wday/cxs/{tenant}/{site}/jobs``
    The confirmation. **404** means the shard is right and the site name is wrong, **422**
    means the shard is wrong, and **200** with a posting envelope is a resolution.
``GET {host}/wday/cxs/{tenant}/{site}/job/{path}``
    The posting itself.

Conventional site names (:data:`FALLBACK_SITE_NAMES`, plus the tenant-derived forms in
:func:`site_name_candidates`) are still tried, but only *after* ``robots.txt`` has been
asked and only on a shard already confirmed — they are a fallback for a tenant whose
``robots.txt`` names nothing, not the primary mechanism.

``postedOn`` arrives as human phrasing — "Posted Today", "Posted 3 Days Ago",
"Posted 30+ Days Ago". :func:`resolve_posted_on` turns that back into a real instant so the
freshness filter, the scorer and the desktop app all see a date like every other provider's.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Final
from urllib.parse import unquote, urlsplit

import structlog

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
    PostingUnavailableError,
    ProviderError,
    RawPosting,
    SearchQuery,
    UnsupportedFlowError,
)
from app.jobs.seeds import boards_from_query
from app.models.enums import (
    ATSProviderName,
    EmploymentType,
    PluginKind,
    WorkArrangement,
)
from app.plugins.base import PluginMeta
from app.plugins.registry import plugin

__all__ = [
    "CXS_PAGE_SIZE",
    "EXTERNAL_ID_SEPARATOR",
    "FALLBACK_SITE_NAMES",
    "MAX_ROBOTS_CHARS",
    "ROBOTS_PATH",
    "SHARD_CANDIDATES",
    "SITE_NAME_TEMPLATES",
    "WORKDAY_HOSTS",
    "WRONG_SHARD_STATUS",
    "BoardToken",
    "CareerSite",
    "JobRef",
    "WorkdayProvider",
    "career_sites_from_robots",
    "normalize_external_path",
    "parse_board_token",
    "resolve_posted_on",
    "site_name_candidates",
    "split_workday_url",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Endpoint constants
# ======================================================================================

#: Postings requested per CXS page. Workday silently clamps anything larger to 20, so asking
#: for more would make :meth:`~app.jobs.base.ATSProvider.paginate`'s "a short page means the
#: feed is exhausted" test fire on the very first page and truncate every board to 20 jobs.
CXS_PAGE_SIZE: Final[int] = 20

#: Hostnames Workday serves career sites from. ``myworkdayjobs.com`` is the classic one;
#: ``myworkdaysite.com`` is the newer domain some tenants have migrated to.
WORKDAY_HOSTS: Final[tuple[str, ...]] = ("myworkdayjobs.com", "myworkdaysite.com")

#: Shards probed when resolving a bare tenant token, in descending order of how often they
#: appear in the wild. A tenant lives on exactly one shard, and there is no directory to look
#: it up in, so the only honest options are to probe or to ship a table of guesses.
SHARD_CANDIDATES: Final[tuple[str, ...]] = (
    "wd1",
    "wd3",
    "wd5",
    "wd2",
    "wd12",
    "wd10",
    "wd101",
    "wd103",
    "wd102",
    "wd104",
    "wd105",
)

#: Tenant-independent career-site names, tried when ``robots.txt`` names none. These are
#: *candidates*, never assumptions: each is confirmed against the live CXS jobs endpoint
#: before it is used, and an unconfirmed name is discarded. ``External`` leads because it is
#: by far the most common (Intel and Dell both use it).
FALLBACK_SITE_NAMES: Final[tuple[str, ...]] = (
    "External",
    "Careers",
    "careers",
    "External_Careers",
    "ExternalCareerSite",
    "External_Career_Site",
)

#: Career-site names built from the tenant token itself, as ``str.format`` templates taking
#: ``tenant`` (the token), ``Tenant`` (capitalised) and ``TENANT`` (upper-cased). Every one
#: was observed on a real board: ``NVIDIAExternalCareerSite``, ``targetcareers``,
#: ``Cisco_Careers``, ``disneycareer``. Like :data:`FALLBACK_SITE_NAMES` these are only ever
#: tried on a shard that has already answered, and only after ``robots.txt`` has been asked.
SITE_NAME_TEMPLATES: Final[tuple[str, ...]] = (
    "{TENANT}ExternalCareerSite",
    "{Tenant}ExternalCareerSite",
    "{tenant}ExternalCareerSite",
    "{Tenant}Careers",
    "{tenant}careers",
    "{Tenant}_Careers",
    "{tenant}career",
)

#: Path of the file consulted to find a tenant's shard and career sites. See the module
#: docstring: on the right shard it answers 200 and names the sites; on every other shard it
#: answers 422.
ROBOTS_PATH: Final[str] = "/robots.txt"

#: How much of a ``robots.txt`` is parsed. Workday's are a few hundred bytes; the cap exists
#: so that a misconfigured host serving something enormous cannot cost a discovery run.
MAX_ROBOTS_CHARS: Final[int] = 64_000

#: ``Sitemap: https://host/<Site>/siteMap.xml`` — the strongest signal a Workday
#: ``robots.txt`` carries, because a tenant lists exactly the boards it wants indexed.
_ROBOTS_SITEMAP_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*Sitemap\s*:\s*https?://[^/\s]+/([A-Za-z0-9][A-Za-z0-9._-]*)/",
    re.IGNORECASE | re.MULTILINE,
)

#: ``Allow: /<Site>/`` — the same information stated as a crawl rule. ``Disallow`` lines are
#: deliberately **not** read: a board the employer asked crawlers to stay out of is not one
#: this provider will poll, even though the CXS API would serve it (golden rule #10).
_ROBOTS_ALLOW_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*Allow\s*:\s*/([A-Za-z0-9][A-Za-z0-9._-]*)/",
    re.IGNORECASE | re.MULTILINE,
)

#: Path segments that can never be a career-site name, because Workday owns them.
_RESERVED_SEGMENTS: Final[frozenset[str]] = frozenset(
    {"api", "details", "en-us", "job", "static", "wday", "assets", "favicon.ico"}
)

#: A locale prefix such as ``en-US`` or ``fr-CA``, which sits between the host and the career
#: site on a human-facing Workday URL. Lowercase language on purpose: a career site named
#: ``US`` must not be mistaken for a locale.
_LOCALE_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z]{2}(?:-[A-Za-z]{2,4})?$")

#: ``{tenant}.wd{N}.{workday-host}`` — the canonical career-site hostname.
_SHARDED_HOST_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<tenant>[a-z0-9][a-z0-9_-]*)\.(?P<shard>wd\d+)\.(?:"
    + "|".join(host.replace(".", r"\.") for host in WORKDAY_HOSTS)
    + r")$",
    re.IGNORECASE,
)

#: ``{tenant}.{workday-host}`` — seen on a handful of tenants and on shortened links. The
#: shard is unknown and gets probed.
_UNSHARDED_HOST_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<tenant>[a-z0-9][a-z0-9_-]*)\.(?:"
    + "|".join(host.replace(".", r"\.") for host in WORKDAY_HOSTS)
    + r")$",
    re.IGNORECASE,
)

#: Trailing path segments that belong to the application wizard rather than to the posting,
#: stripped so that a pasted "apply" link still identifies the job.
_APPLY_SEGMENTS: Final[frozenset[str]] = frozenset(
    {"apply", "applymanually", "autofillwithresume", "login", "signin"}
)

#: The status a Workday host returns for a tenant that does not live on that shard. It is
#: what makes a one-request-per-shard probe possible: 422 means "wrong shard", while 404 on
#: the CXS jobs endpoint means "right shard, wrong career site".
WRONG_SHARD_STATUS: Final[int] = 422

#: Separator between the tenant and the requisition id inside
#: :attr:`~app.jobs.base.RawPosting.external_id`. The tenant is part of the identity because
#: requisition ids are only unique *within* an employer — two tenants both numbering from
#: ``JR1001`` must not collide on ``UNIQUE(provider, external_id)``.
EXTERNAL_ID_SEPARATOR: Final[str] = ":"


# ======================================================================================
# Discovery-budget constants
# ======================================================================================

#: New tenants resolved in a single :meth:`WorkdayProvider.search` call. Resolving a tenant
#: costs up to one request per shard, so an uncapped cold start across every seeded tenant
#: would spend several minutes probing before it discovered its first job. Capping it means a
#: run always makes progress *and* always finishes; the tenants that did not fit resolve on
#: the next poll and are then cached.
MAX_SITE_RESOLUTIONS_PER_SEARCH: Final[int] = 8

#: How long a confirmed shard/site resolution is trusted. A tenant migrating shards is a
#: multi-year event; a week is short enough to recover from one and long enough that
#: resolution is a cold-start cost rather than a per-run one.
SITE_CACHE_TTL_SECONDS: Final[int] = 7 * 24 * 60 * 60

#: How long a failed resolution is remembered. Short, because "this tenant is not on Workday
#: today" is a much less durable fact than "this tenant is on wd5" — a board can appear at
#: any time — but long enough to stop a dead seed token from being re-probed every poll.
SITE_MISS_CACHE_TTL_SECONDS: Final[int] = 6 * 60 * 60

#: Stubs fetched per keyword before filtering, as a multiple of the postings still wanted.
#: Freshness, location and arrangement filters run client-side (the CXS facet API takes
#: opaque location ids that cannot be derived from a place name), so asking for exactly the
#: number wanted would under-deliver whenever anything is filtered out.
FILTER_OVERFETCH: Final[int] = 4

#: Hard ceiling on stubs pulled for one keyword against one board, whatever the overfetch
#: factor works out to. Bounds the cost of a query that filters out almost everything.
MAX_STUBS_PER_QUERY: Final[int] = 500

#: ``locationsText`` values of the form "3 Locations", which say nothing about *where*.
_LOCATION_COUNT_RE: Final[re.Pattern[str]] = re.compile(r"^\d+\s+locations?$", re.IGNORECASE)


# ======================================================================================
# Relative-date resolution
# ======================================================================================

#: Seconds per unit named in a Workday ``postedOn`` phrase. Months and years are nominal —
#: the source is a rounded human phrase, so a calendar-exact conversion would imply a
#: precision the input never had.
_RELATIVE_UNIT_SECONDS: Final[dict[str, int]] = {
    "second": 1,
    "sec": 1,
    "minute": 60,
    "min": 60,
    "hour": 3600,
    "hr": 3600,
    "day": 86_400,
    "week": 604_800,
    "wk": 604_800,
    "month": 30 * 86_400,
    "mo": 30 * 86_400,
    "year": 365 * 86_400,
    "yr": 365 * 86_400,
}

#: "3 Days Ago", "30+ Days Ago", "12 hours ago". The ``+`` is part of Workday's oldest
#: bucket and is captured so the caller knows the age is a lower bound.
_RELATIVE_AGE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?P<count>\d+)\s*(?P<more>\+)?\s*(?P<unit>"
    + "|".join(sorted(_RELATIVE_UNIT_SECONDS, key=len, reverse=True))
    + r")s?\b",
    re.IGNORECASE,
)

#: Phrases meaning "now", with the offset each one implies.
_NAMED_OFFSETS: Final[tuple[tuple[re.Pattern[str], timedelta], ...]] = (
    (re.compile(r"\b(?:just\s+posted|today)\b", re.IGNORECASE), timedelta(0)),
    (re.compile(r"\byesterday\b", re.IGNORECASE), timedelta(days=1)),
)


def resolve_posted_on(value: Any, *, now: datetime | None = None) -> datetime | None:
    """Turn a Workday ``postedOn`` phrase into a real timezone-aware UTC instant.

    Workday's search API does not publish a posting date. It publishes the sentence a human
    reads on the card — "Posted Today", "Posted 3 Days Ago", "Posted 30+ Days Ago" — which is
    useless to the freshness filter, to the scorer and to any sort order until it is resolved
    back into a date. This is where that happens, so that a Workday posting carries the same
    kind of ``posted_at`` as a Greenhouse one.

    A ``+`` marks Workday's oldest bucket and means "at least this old". The most recent
    instant consistent with the statement is returned, because the alternative — assuming the
    worst — would silently drop an entire bucket out of every freshness window, including the
    postings in it that really are exactly thirty days old.

    Args:
        value: The raw ``postedOn`` value, or any other date-shaped value. Absolute dates are
            handled too, via :func:`app.jobs._parsing.parse_date`, because some tenants
            localise the phrase into a plain date.
        now: The instant relative phrasing is measured from. Defaults to the current time;
            injectable so the resolution is testable without freezing the clock.

    Returns:
        A timezone-aware UTC datetime, or ``None`` when the value says nothing about time.
        ``None`` is honest and is treated as "unknown" by
        :meth:`~app.jobs.base.SearchQuery.matches_freshness`, never as "stale".

    Example:
        >>> reference = datetime(2026, 3, 10, tzinfo=timezone.utc)
        >>> resolve_posted_on("Posted 3 Days Ago", now=reference).date().isoformat()
        '2026-03-07'
        >>> resolve_posted_on("Posted Today", now=reference) == reference
        True
    """
    text = clean_text(value)
    if not text:
        return None

    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)

    match = _RELATIVE_AGE_RE.search(text)
    if match:
        unit_seconds = _RELATIVE_UNIT_SECONDS[match.group("unit").lower()]
        try:
            count = int(match.group("count"))
        except ValueError:  # pragma: no cover - the group is \d+
            return None
        age = timedelta(seconds=count * unit_seconds)
        if match.group("more"):
            logger.debug("workday.posted_on_lower_bound", value=text, days=age.days)
        return reference - age

    for pattern, offset in _NAMED_OFFSETS:
        if pattern.search(text):
            return reference - offset

    return parse_date(value)


# ======================================================================================
# Career sites and job references
# ======================================================================================


@dataclass(frozen=True, slots=True)
class CareerSite:
    """One resolved Workday career site: the addressable unit discovery polls.

    A Workday tenant is an employer, and a career site is one job board inside it. Large
    employers run several (external, internal, university), so the pair is what identifies a
    board — the tenant alone does not.

    Attributes:
        tenant: The Workday tenant, which is also the employer identity in this provider.
        shard: The Workday cluster the tenant is hosted on, ``wd1`` … ``wd105``.
        site: The career-site name, as it appears in the URL path.
        host: The full hostname, kept explicitly rather than recomputed, because a tenant may
            live on either domain in :data:`WORKDAY_HOSTS`.
    """

    tenant: str
    shard: str
    site: str
    host: str

    @property
    def jobs_url(self) -> str:
        """The CXS search endpoint this board is polled through."""
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}/jobs"

    @property
    def public_url(self) -> str:
        """The human-facing board URL, for logs and for a "view the board" link."""
        return f"https://{self.host}/en-US/{self.site}"

    @property
    def token(self) -> str:
        """A compact identifier used as a log field and as a cache key part."""
        return f"{self.tenant}.{self.shard}/{self.site}"

    def job(self, path: str) -> JobRef:
        """Return a reference to one posting on this board.

        Args:
            path: The posting path, relative to ``/job/`` and without a leading slash.

        Returns:
            The reference.
        """
        return JobRef(site=self, path=path)

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-ready form, as stored in the resolution cache."""
        return {"tenant": self.tenant, "shard": self.shard, "site": self.site, "host": self.host}

    @classmethod
    def from_dict(cls, payload: Any) -> CareerSite | None:
        """Rebuild a site from its cached form.

        Args:
            payload: A mapping previously produced by :meth:`as_dict`.

        Returns:
            The site, or ``None`` when the payload is missing a field — a cache entry written
            by an older version must degrade to a re-resolution, never to a broken URL.
        """
        if not isinstance(payload, Mapping):
            return None
        values = [str(payload.get(key, "")).strip() for key in ("tenant", "shard", "site", "host")]
        if not all(values):
            return None
        tenant, shard, site, host = values
        return cls(tenant=tenant, shard=shard, site=site, host=host)


@dataclass(frozen=True, slots=True)
class JobRef:
    """A reference to one Workday posting, sufficient to fetch or to link to it.

    Attributes:
        site: The board the posting lives on.
        path: The posting path relative to ``/job/``, e.g.
            ``"US-CA-Santa-Clara/Senior-Engineer_JR1959401"``.
    """

    site: CareerSite
    path: str

    @property
    def detail_url(self) -> str:
        """The CXS endpoint returning the full posting."""
        return (
            f"https://{self.site.host}/wday/cxs/{self.site.tenant}/{self.site.site}/job/{self.path}"
        )

    @property
    def public_url(self) -> str:
        """The human-facing posting URL."""
        return f"{self.site.public_url}/job/{self.path}"

    @property
    def requisition_hint(self) -> str | None:
        """The requisition id embedded in the path, when Workday put one there.

        Workday names a posting ``<Job-Title>_<REQ-ID>``, so the tail after the last
        underscore is the requisition id on the overwhelming majority of tenants. Used only
        as a fallback identity when the detail payload is unavailable.

        Returns:
            The trailing identifier, or ``None`` when the path does not carry one.
        """
        tail = self.path.rsplit("/", 1)[-1]
        candidate = tail.rsplit("_", 1)[-1] if "_" in tail else ""
        return candidate or None


def normalize_external_path(value: Any) -> str | None:
    """Reduce a Workday ``externalPath`` to the path segment after ``/job/``.

    Workday returns ``"/job/US-CA-Santa-Clara/Senior-Engineer_JR1959401"``; the CXS detail
    endpoint wants everything after ``/job/``. Some tenants return the same posting under
    ``/details/`` instead, and both forms must resolve to the same reference or the same job
    would be ingested twice under two identities.

    Args:
        value: The raw ``externalPath`` from a search result, or a path fragment.

    Returns:
        The normalised path with no leading or trailing slash, or ``None`` when the value
        carries no path at all.
    """
    text = clean_text(value)
    if not text:
        return None
    segments = [segment for segment in text.split("/") if segment]
    if segments and segments[0].lower() in {"job", "details"}:
        segments = segments[1:]
    while segments and segments[-1].lower() in _APPLY_SEGMENTS:
        segments = segments[:-1]
    return "/".join(segments) or None


def split_workday_url(url: str) -> tuple[CareerSite, str | None] | None:
    """Take a Workday URL apart into a career site and, when present, a posting path.

    Every URL shape this system meets is accepted: the human-facing
    ``https://nvidia.wd5.myworkdayjobs.com/en-US/Site/job/Location/Title_JR1``, the newer
    ``/details/`` variant, the wizard's ``/apply`` suffix, the CXS API form
    ``/wday/cxs/{tenant}/{site}/job/…``, and a bare board URL with no posting at all.

    Args:
        url: An absolute URL, or a hostname-and-path fragment with the scheme omitted.

    Returns:
        ``(site, path)`` where *path* is ``None`` for a board URL, or ``None`` when *url* is
        not a Workday address or names no career site. A URL whose host omits the shard
        yields a site with an empty :attr:`CareerSite.shard`, which the provider resolves by
        probing rather than by inventing a shard number.
    """
    candidate = clean_text(url)
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None

    host = parsed.netloc.rpartition("@")[2].split(":", 1)[0].strip().lower()
    if not host:
        return None

    sharded = _SHARDED_HOST_RE.match(host)
    if sharded:
        tenant, shard = sharded.group("tenant").lower(), sharded.group("shard").lower()
    else:
        unsharded = _UNSHARDED_HOST_RE.match(host)
        if not unsharded:
            return None
        tenant, shard = unsharded.group("tenant").lower(), ""

    segments = [unquote(segment) for segment in parsed.path.split("/") if segment]

    if len(segments) >= 4 and segments[0].lower() == "wday" and segments[1].lower() == "cxs":
        tenant, site, rest = segments[2], segments[3], segments[4:]
    else:
        if segments and _LOCALE_RE.match(segments[0]):
            segments = segments[1:]
        if not segments:
            return None
        site, rest = segments[0], segments[1:]

    if site.lower() in _RESERVED_SEGMENTS:
        return None

    path = normalize_external_path("/".join(rest)) if rest else None
    return CareerSite(tenant=tenant, shard=shard, site=site, host=host), path


@dataclass(frozen=True, slots=True)
class BoardToken:
    """A board token taken apart, with whatever is unknown left empty.

    :mod:`app.jobs.seeds` ships bare tenants, users paste URLs, and configuration files
    carry everything in between. This is the common shape they all reduce to, so that
    :meth:`WorkdayProvider._discover_site` has one thing to reason about rather than five.

    Attributes:
        tenant: The Workday tenant. Never empty on a usable token.
        shard: The cluster, ``wd1`` … ``wd105``, or ``""`` when it must be probed for.
        site: The career-site name, or ``""`` when it must be discovered.
        host: The full hostname when the token named one, else ``""``.
    """

    tenant: str
    shard: str = ""
    site: str = ""
    host: str = ""


#: A token that names no Workday host: ``nvidia``, ``nvidia.wd5``, ``nvidia/External``,
#: ``nvidia:External``, ``nvidia.wd5/External``. Both ``/`` and ``:`` are accepted as the
#: tenant/site separator because both are natural to write in a configuration file.
_BARE_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<tenant>[a-z0-9][a-z0-9_-]*)"
    r"(?:\.(?P<shard>wd\d+))?"
    r"(?:[/:](?P<site>[A-Za-z0-9][A-Za-z0-9._-]*))?$",
    re.IGNORECASE,
)


def parse_board_token(token: str) -> BoardToken | None:
    """Take a board token apart into whatever it actually specifies.

    Args:
        token: A tenant name, a ``tenant/site`` pair, a hostname, or a full career-site or
            posting URL.

    Returns:
        The parsed token, or ``None`` when it names no plausible Workday tenant. Nothing is
        invented: an unspecified shard or career site comes back empty and is resolved by
        probing, never by assumption.

    Example:
        >>> parse_board_token("nvidia").tenant
        'nvidia'
        >>> parse_board_token("https://nvidia.wd5.myworkdayjobs.com/en-US/Site/job/X_JR1").site
        'Site'
    """
    text = clean_text(token).strip("/")
    if not text:
        return None

    lowered = text.lower()
    if "://" in lowered or any(domain in lowered for domain in WORKDAY_HOSTS):
        parsed = split_workday_url(text)
        if parsed is not None:
            site, _path = parsed
            return BoardToken(tenant=site.tenant, shard=site.shard, site=site.site, host=site.host)
        host = text.split("://")[-1].split("/", 1)[0].rpartition("@")[2].split(":", 1)[0].lower()
        sharded = _SHARDED_HOST_RE.match(host)
        if sharded:
            return BoardToken(
                tenant=sharded.group("tenant").lower(),
                shard=sharded.group("shard").lower(),
                host=host,
            )
        unsharded = _UNSHARDED_HOST_RE.match(host)
        if unsharded:
            return BoardToken(tenant=unsharded.group("tenant").lower())
        return None

    bare = _BARE_TOKEN_RE.match(text)
    if not bare:
        return None
    site_name = bare.group("site") or ""
    if site_name.lower() in _RESERVED_SEGMENTS:
        site_name = ""
    return BoardToken(
        tenant=bare.group("tenant").lower(),
        shard=(bare.group("shard") or "").lower(),
        site=site_name,
    )


def _ordered(candidates: Sequence[str], preferred: str) -> list[str]:
    """Return *candidates* with *preferred* moved to the front.

    Args:
        candidates: The default order.
        preferred: A value to try first; ignored when empty or unknown.

    Returns:
        The reordered list, with no duplicates.
    """
    if not preferred:
        return list(candidates)
    return [preferred, *(item for item in candidates if item != preferred)]


def _domain_order(preferred_host: str) -> tuple[str, ...]:
    """Return the Workday domains to sweep, most likely first.

    Args:
        preferred_host: A hostname the caller already knows, or ``""``.

    Returns:
        :data:`WORKDAY_HOSTS`, with the domain implied by *preferred_host* moved to the
        front when there is one.
    """
    host = (preferred_host or "").lower()
    for domain in WORKDAY_HOSTS:
        if host.endswith(domain):
            return (domain, *(other for other in WORKDAY_HOSTS if other != domain))
    return WORKDAY_HOSTS


def _usable_site_names(names: Iterable[str]) -> list[str]:
    """Filter and de-duplicate career-site name candidates, preserving order.

    Args:
        names: Raw candidates, from any source.

    Returns:
        The names that could plausibly be a career site: non-empty, not one of Workday's own
        reserved path segments, and not a locale prefix such as ``en-US``.
    """
    usable: list[str] = []
    for candidate in names:
        name = (candidate or "").strip().strip("/")
        if not name or name.lower() in _RESERVED_SEGMENTS or _LOCALE_RE.match(name):
            continue
        if name not in usable:
            usable.append(name)
    return usable


def career_sites_from_robots(body: str) -> list[str]:
    """Extract the career-site names a tenant's ``robots.txt`` publishes.

    This is the authoritative source, and the polite one. A Workday ``robots.txt`` names each
    board it wants indexed twice — once as a ``Sitemap:`` URL and once as an ``Allow:`` rule
    — which is the only public place a name like ``NVIDIAExternalCareerSite`` or
    ``disneycareer`` can be read rather than guessed.

    ``Disallow`` lines are ignored on purpose. Nike's file, for example, disallows ``/nke/``,
    ``/nke2/`` and ``/nke4/`` and allows nothing; those are real board names and the CXS API
    would serve them, but the employer has asked automated clients to stay away and this
    provider honours that (golden rule #10). The tenant is then left unresolved, which is the
    correct outcome.

    Args:
        body: The ``robots.txt`` body, already truncated by the caller.

    Returns:
        Site names, ``Sitemap:`` entries first, de-duplicated and in file order. Empty when
        the file names none.
    """
    found = [match.group(1) for match in _ROBOTS_SITEMAP_RE.finditer(body)]
    found.extend(match.group(1) for match in _ROBOTS_ALLOW_RE.finditer(body))
    return _usable_site_names(found)


def site_name_candidates(tenant: str, *, preferred: str = "") -> list[str]:
    """Return the career-site names worth trying for *tenant*, most likely first.

    Only used when ``robots.txt`` named nothing usable, and only on a shard that has already
    answered — so this is a bounded guess-and-check over a confirmed host, never a way to
    address an employer that was never found.

    Args:
        tenant: The Workday tenant token.
        preferred: A name the caller already has reason to believe in; tried first.

    Returns:
        The tenant-derived names from :data:`SITE_NAME_TEMPLATES` interleaved with the
        conventional ones in :data:`FALLBACK_SITE_NAMES`, de-duplicated. Every one is still
        confirmed against the live CXS endpoint before use.
    """
    formatted = {
        "tenant": tenant,
        "Tenant": tenant[:1].upper() + tenant[1:],
        "TENANT": tenant.upper(),
    }
    derived = [template.format(**formatted) for template in SITE_NAME_TEMPLATES]
    return _usable_site_names([preferred, FALLBACK_SITE_NAMES[0], *derived, *FALLBACK_SITE_NAMES])


# ======================================================================================
# The provider
# ======================================================================================


@plugin
class WorkdayProvider(ATSProvider):
    """Workday career sites: full discovery, and manual review for every application.

    Class attributes:
        supports_auto_apply: Always ``False``. See the module docstring — Workday's flow is
            account-gated, multi-step and tenant-configured, so :meth:`apply` raises
            :class:`~app.jobs.base.UnsupportedFlowError` and the posting goes to a human
            (``docs/CONTRACTS.md`` §9, golden rule #10).
        requires_login: ``True``. Discovery needs no account — the CXS endpoints are the
            public career site's own API — but applying does, which is exactly the fact that
            makes automation unsafe.
    """

    meta: ClassVar[PluginMeta] = PluginMeta(
        kind=PluginKind.PROVIDER,
        name=ATSProviderName.WORKDAY.value,
        version="1.0.0",
        display_name="Workday",
        description=(
            "Discovers postings from Workday career sites via the public CXS JSON API. "
            "Applications are surfaced for manual review: Workday's flow is account-gated "
            "and configured per employer, so it is never submitted automatically."
        ),
        author="ApplicantOS",
        capabilities=frozenset({"search", "fetch", "manual_review"}),
    )

    name: ClassVar[ATSProviderName] = ATSProviderName.WORKDAY
    supports_auto_apply: ClassVar[bool] = False
    requires_login: ClassVar[bool] = True
    URL_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"[a-z0-9][a-z0-9_-]*\.wd\d+\.myworkdayjobs\.com", re.IGNORECASE),
        re.compile(r"[a-z0-9][a-z0-9_-]*\.wd\d+\.myworkdaysite\.com", re.IGNORECASE),
        re.compile(r"\bmyworkdayjobs\.com\b", re.IGNORECASE),
        re.compile(r"\bmyworkdaysite\.com\b", re.IGNORECASE),
        re.compile(r"/wday/cxs/", re.IGNORECASE),
    ]

    def __init__(self, settings: Any, **kw: Any) -> None:
        """Construct the provider and its per-process career-site resolution cache.

        Args:
            settings: Application settings, supplied by the plugin registry.
            **kw: Extra construction options, kept on ``self.options``.
        """
        super().__init__(settings, **kw)
        #: Resolved boards for this process. ``None`` records a confirmed failure, so a dead
        #: seed token is probed once per process rather than once per discovery run.
        self._sites: dict[str, CareerSite | None] = {}

    # -- discovery ----------------------------------------------------------------------

    async def search(self, q: SearchQuery) -> AsyncIterator[RawPosting]:
        """Yield postings from every Workday board the query selects.

        Boards come from ``q.extra`` when the caller named any, and from
        :func:`app.jobs.seeds.boards_from_query` otherwise. Each board is polled
        independently, and a board that has moved, closed or renamed is skipped with a log
        line rather than failing the run — a stale tenant token is expected and costs one
        request.

        Keywords are issued as separate searches rather than concatenated, because Workday's
        ``searchText`` narrows rather than widens: a single query of "backend engineer data
        scientist" matches nothing, while two queries match what the user meant. Location,
        freshness and remote filters are applied client-side, because the CXS facet API keys
        on opaque location identifiers that cannot be derived from a place name.

        Args:
            q: What to look for.

        Yields:
            One :class:`~app.jobs.base.RawPosting` per posting, at most ``q.limit`` of them.
        """
        tokens = boards_from_query(self.provider_name, q.extra)
        if not tokens:
            self.logger.info("workday.no_boards_configured")
            return

        budget = q.limit
        produced = 0
        failures = 0
        seen: set[tuple[str, str]] = set()
        resolutions = 0

        for token in tokens:
            if produced >= budget:
                break

            try:
                site, probed = await self._resolve_site_tracked(
                    token, allow_probe=resolutions < MAX_SITE_RESOLUTIONS_PER_SEARCH
                )
            except ProviderError as exc:
                failures += 1
                self.logger.warning("workday.board_unresolved", board=token, error=str(exc))
                continue
            if probed:
                resolutions += 1
            if site is None:
                continue

            try:
                async for raw in self._search_site(site, q, budget - produced, seen):
                    yield raw
                    produced += 1
                    if produced >= budget:
                        break
            except ProviderError as exc:
                # Every Workday tenant is a separate host with its own availability and its
                # own rate limit, so one failing board must not end the run for the others
                # (see the module docstring of app.jobs.seeds).
                failures += 1
                self.logger.warning(
                    "workday.board_failed",
                    board=site.token,
                    error=str(exc),
                    status_code=exc.status_code,
                )

        self.logger.info(
            "workday.search_finished",
            boards=len(tokens),
            resolved=resolutions,
            postings=produced,
            failures=failures,
        )

    async def _search_site(
        self,
        site: CareerSite,
        q: SearchQuery,
        budget: int,
        seen: set[tuple[str, str]],
    ) -> AsyncIterator[RawPosting]:
        """Yield postings from one career site.

        Args:
            site: The board to poll.
            q: The query, used for its keywords and its client-side filters.
            budget: Maximum postings to yield from this board.
            seen: ``(tenant, path)`` pairs already yielded in this run, so a posting matched
                by two keywords is emitted once.

        Yields:
            One :class:`~app.jobs.base.RawPosting` per surviving posting.
        """
        produced = 0
        searches = q.keywords or [""]

        for search_text in searches:
            if produced >= budget:
                return

            wanted = budget - produced
            stub_limit = min(max(wanted, 1) * FILTER_OVERFETCH, MAX_STUBS_PER_QUERY)

            async def fetch(
                offset: int,
                cursor: str | None,
                text: str = search_text,
            ) -> tuple[Sequence[Any], str | None]:
                """Fetch one CXS page. Signature is fixed by :meth:`ATSProvider.paginate`."""
                payload = {
                    "appliedFacets": {},
                    "limit": CXS_PAGE_SIZE,
                    "offset": offset,
                    "searchText": text,
                }
                response = await self._request("POST", site.jobs_url, json=payload)
                document = self._decode(response, site.jobs_url)
                postings = document.get("jobPostings")
                return (postings if isinstance(postings, list) else []), None

            async for stub in self.paginate(fetch, limit=stub_limit, page_size=CXS_PAGE_SIZE):
                if not isinstance(stub, Mapping):
                    continue
                path = normalize_external_path(stub.get("externalPath"))
                if not path:
                    continue
                key = (site.tenant, path)
                if key in seen:
                    continue
                seen.add(key)

                raw = await self._build_posting(site.job(path), stub, q)
                if raw is None:
                    continue

                yield raw
                produced += 1
                if produced >= budget:
                    return

    async def _build_posting(
        self,
        ref: JobRef,
        stub: Mapping[str, Any],
        q: SearchQuery,
    ) -> RawPosting | None:
        """Turn one search result into a full :class:`~app.jobs.base.RawPosting`.

        The cheap filters run against the search stub first, so a posting the query already
        excludes never costs a detail request. What survives is fetched in full, because the
        description is what the scorer and the resume engine actually read.

        Args:
            ref: The posting reference.
            stub: The search result, as returned in ``jobPostings``.
            q: The query, for its client-side filters.

        Returns:
            The posting, or ``None`` when it is filtered out, has vanished, or carries no
            usable title.
        """
        posted_at = resolve_posted_on(stub.get("postedOn"))
        if not q.matches_freshness(posted_at):
            return None

        locations_text = clean_text(stub.get("locationsText"))
        if (
            q.locations
            and locations_text
            and not _LOCATION_COUNT_RE.match(locations_text)
            and not _matches_locations(q.locations, locations_text)
        ):
            return None

        detail = await self._fetch_detail(ref)
        return self._to_raw(ref, stub, detail, q, posted_at=posted_at)

    async def _fetch_detail(self, ref: JobRef) -> dict[str, Any] | None:
        """Fetch one posting's full CXS payload.

        Args:
            ref: The posting reference.

        Returns:
            The decoded payload, or ``None`` when the posting has been withdrawn or the
            request failed. A failure degrades to the search stub rather than losing the
            posting: a title, a company and a URL are still worth surfacing, and the
            pipeline can re-fetch later.
        """
        try:
            response = await self._request("GET", ref.detail_url)
        except PostingUnavailableError:
            self.logger.debug("workday.posting_gone", url=ref.public_url)
            return None
        except ProviderError as exc:
            self.logger.warning("workday.detail_failed", url=ref.detail_url, error=str(exc))
            return None
        try:
            return self._decode(response, ref.detail_url)
        except ProviderError as exc:
            self.logger.warning("workday.detail_unparseable", url=ref.detail_url, error=str(exc))
            return None

    def _to_raw(
        self,
        ref: JobRef,
        stub: Mapping[str, Any],
        detail: Mapping[str, Any] | None,
        q: SearchQuery,
        *,
        posted_at: datetime | None,
    ) -> RawPosting | None:
        """Assemble a :class:`~app.jobs.base.RawPosting` from a stub and its detail payload.

        Args:
            ref: The posting reference.
            stub: The search result.
            detail: The full CXS payload, or ``None`` when it could not be fetched.
            q: The query, for the arrangement filter that needs the description.
            posted_at: The instant already resolved from the stub's ``postedOn``.

        Returns:
            The posting, or ``None`` when it has no title or the arrangement filter rejects
            it.
        """
        info: Mapping[str, Any] = {}
        organisation: Mapping[str, Any] = {}
        if isinstance(detail, Mapping):
            candidate_info = detail.get("jobPostingInfo")
            if isinstance(candidate_info, Mapping):
                info = candidate_info
            candidate_org = detail.get("hiringOrganization")
            if isinstance(candidate_org, Mapping):
                organisation = candidate_org

        title = clean_text(info.get("title")) or clean_text(stub.get("title"))
        if not title:
            self.logger.debug("workday.posting_without_title", url=ref.public_url)
            return None

        description = html_to_text(info.get("jobDescription")) or None
        location = (
            clean_text(info.get("location"))
            or _informative_location(clean_text(stub.get("locationsText")))
            or clean_text(stub.get("locationsText"))
            or None
        )

        arrangement = _map_remote_type(info.get("remoteType"))
        if arrangement is WorkArrangement.UNKNOWN:
            arrangement = infer_arrangement(
                " ".join(part for part in (title, location or "", description or "") if part)
            )
        if q.remote_only and arrangement in {WorkArrangement.ONSITE, WorkArrangement.HYBRID}:
            return None

        employment_type = infer_employment_type(title, description or "")
        if employment_type is EmploymentType.UNKNOWN:
            employment_type = _map_time_type(info.get("timeType"))

        salary_min, salary_max, currency = parse_salary(description or "")

        resolved_posted = (
            posted_at
            or resolve_posted_on(info.get("postedOn"))
            or parse_date(info.get("startDate"))
        )

        company = (
            clean_text(organisation.get("name"))
            or clean_text(stub.get("company"))
            or _tenant_display_name(ref.site.tenant)
        )

        requisition = (
            clean_text(info.get("jobReqId"))
            or _first_bullet(stub.get("bulletFields"))
            or ref.requisition_hint
            or ref.path
        )

        return RawPosting(
            provider=ATSProviderName.WORKDAY,
            external_id=f"{ref.site.tenant}{EXTERNAL_ID_SEPARATOR}{requisition}",
            url=clean_text(info.get("externalUrl")) or ref.public_url,
            title=title,
            company_name=company,
            description=description,
            location=location,
            work_arrangement=arrangement,
            employment_type=employment_type,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            posted_at=resolved_posted,
            closes_at=parse_date(info.get("endDate")),
            apply_url=None,
            raw={
                "tenant": ref.site.tenant,
                "shard": ref.site.shard,
                "site": ref.site.site,
                "host": ref.site.host,
                "external_path": ref.path,
                "stub": dict(stub),
                "detail": dict(detail) if isinstance(detail, Mapping) else None,
            },
        )

    # -- single posting -----------------------------------------------------------------

    async def fetch_posting(self, id_or_url: str) -> RawPosting | None:
        """Fetch one Workday posting by URL or by ``{tenant}:{requisition}`` identifier.

        Both forms occur. The desktop app pastes a career-site URL; the pipeline re-fetches
        by the ``external_id`` it stored. A URL is resolved directly. An identifier is
        resolved by searching the tenant's board for the requisition, because a Workday
        posting is addressed by its URL path — which a requisition id does not contain — and
        searching for it is the honest way to find it again.

        Args:
            id_or_url: A Workday posting URL, or ``"{tenant}:{requisition}"``.

        Returns:
            The posting, or ``None`` when the input is well-formed but unknown to Workday.

        Raises:
            PostingUnavailableError: When the posting existed and has been withdrawn.
            ProviderError: On any other provider failure.
        """
        text = clean_text(id_or_url)
        if not text:
            return None

        parsed = split_workday_url(text)
        if parsed is not None:
            site, path = parsed
            if path is None:
                self.logger.debug("workday.url_is_a_board_not_a_posting", url=text)
                return None
            if not site.shard:
                resolved = await self._resolve_site(site.tenant)
                if resolved is None:
                    return None
                site = CareerSite(
                    tenant=resolved.tenant,
                    shard=resolved.shard,
                    site=site.site or resolved.site,
                    host=resolved.host,
                )
            ref = site.job(path)
            detail = await self._fetch_detail(ref)
            if detail is None:
                return None
            return self._to_raw(ref, {}, detail, SearchQuery(), posted_at=None)

        tenant, separator, requisition = text.partition(EXTERNAL_ID_SEPARATOR)
        if not separator or not tenant.strip() or not requisition.strip():
            self.logger.debug("workday.unrecognised_identifier", identifier=text[:80])
            return None
        return await self._fetch_by_requisition(tenant.strip(), requisition.strip())

    async def _fetch_by_requisition(self, tenant: str, requisition: str) -> RawPosting | None:
        """Find one posting by requisition id within a tenant's board.

        Args:
            tenant: The Workday tenant.
            requisition: The requisition identifier, e.g. ``"JR1959401"``.

        Returns:
            The posting, or ``None`` when the board no longer advertises it.
        """
        site = await self._resolve_site(tenant)
        if site is None:
            return None

        payload = {
            "appliedFacets": {},
            "limit": CXS_PAGE_SIZE,
            "offset": 0,
            "searchText": requisition,
        }
        response = await self._request("POST", site.jobs_url, json=payload)
        document = self._decode(response, site.jobs_url)
        postings = document.get("jobPostings")
        wanted = requisition.casefold()

        for stub in postings if isinstance(postings, list) else []:
            if not isinstance(stub, Mapping):
                continue
            path = normalize_external_path(stub.get("externalPath"))
            if not path:
                continue
            ref = site.job(path)
            bullets = {value.casefold() for value in _bullet_values(stub.get("bulletFields"))}
            hint = (ref.requisition_hint or "").casefold()
            if wanted not in bullets and wanted != hint:
                continue
            detail = await self._fetch_detail(ref)
            return self._to_raw(
                ref, stub, detail, SearchQuery(), posted_at=resolve_posted_on(stub.get("postedOn"))
            )

        self.logger.debug("workday.requisition_not_found", tenant=tenant, requisition=requisition)
        return None

    # -- career-site resolution ---------------------------------------------------------

    async def _resolve_site(self, token: str) -> CareerSite | None:
        """Resolve a board token into a confirmed :class:`CareerSite`.

        Accepts every form a token arrives in: a full posting or board URL, a
        ``host/site`` fragment, ``tenant/site``, and — the case :mod:`app.jobs.seeds` ships —
        a bare tenant name whose shard and career site are unknown.

        Nothing is assumed. A candidate shard/site pair becomes a resolution only after the
        live CXS jobs endpoint answers with a posting envelope, so a wrong guess is
        discarded rather than silently pointing discovery at a URL that does not exist.
        Results are memoised for the process and cached durably, including the negative
        result.

        Args:
            token: The board token.

        Returns:
            The confirmed site, or ``None`` when the token names nothing reachable.
        """
        site, _probed = await self._resolve_site_tracked(token, allow_probe=True)
        return site

    async def _resolve_site_tracked(
        self,
        token: str,
        *,
        allow_probe: bool,
    ) -> tuple[CareerSite | None, bool]:
        """Resolve a board token, reporting whether it cost a network probe.

        :meth:`search` needs the distinction: a resolution served from the memo or from the
        durable cache is free and should never count against
        :data:`MAX_SITE_RESOLUTIONS_PER_SEARCH`, while a probe is the expensive thing that
        budget exists to bound.

        Args:
            token: The board token.
            allow_probe: Whether a cache miss may fall through to probing. When ``False`` a
                miss returns ``(None, False)`` and the token is retried on the next run.

        Returns:
            ``(site, probed)``.
        """
        key = _token_key(token)
        if key in self._sites:
            return self._sites[key], False

        cached = await self._read_cached_site(key)
        if cached is not None:
            site = None if isinstance(cached, str) else cached
            self._sites[key] = site
            return site, False

        if not allow_probe:
            self.logger.debug("workday.resolution_budget_reached", board=token)
            return None, False

        site = await self._discover_site(token)
        self._sites[key] = site
        await self._write_cached_site(key, site)
        if site is None:
            self.logger.info("workday.board_unresolvable", board=token)
        else:
            self.logger.info("workday.board_resolved", board=token, site=site.token)
        return site, True

    async def _discover_site(self, token: str) -> CareerSite | None:
        """Work out which shard and career site a token refers to, and confirm it.

        Args:
            token: The board token.

        Returns:
            The confirmed site, or ``None``.
        """
        partial = parse_board_token(token)
        if partial is None:
            self.logger.debug("workday.token_unparseable", board=token)
            return None

        if partial.shard and partial.site:
            hosts = (
                (partial.host,)
                if partial.host
                else tuple(f"{partial.tenant}.{partial.shard}.{domain}" for domain in WORKDAY_HOSTS)
            )
            for host in hosts:
                candidate = CareerSite(
                    tenant=partial.tenant, shard=partial.shard, site=partial.site, host=host
                )
                if await self._confirm(candidate):
                    return candidate

        return await self._probe_shards(
            partial.tenant,
            preferred_site=partial.site,
            preferred_shard=partial.shard,
            preferred_host=partial.host,
        )

    async def _probe_shards(
        self,
        tenant: str,
        *,
        preferred_site: str = "",
        preferred_shard: str = "",
        preferred_host: str = "",
    ) -> CareerSite | None:
        """Find the shard a tenant lives on, and the career site it publishes.

        ``robots.txt`` is requested on each candidate host. It answers ``200`` only on the
        shard the tenant actually lives on — every other shard answers ``422`` — so one small
        request per shard settles the question that used to require fetching a quarter of a
        megabyte of single-page-app shell. The file then *names* the tenant's boards in its
        ``Sitemap:`` and ``Allow:`` lines, which is the only public place a name like
        ``NVIDIAExternalCareerSite`` can be read rather than guessed.

        Only when the file names nothing usable does the bounded convention list from
        :func:`site_name_candidates` get tried, and only against the shard that already
        answered. Every candidate, from either source, is confirmed by a real CXS response
        before it is returned.

        The secondary Workday domain is only swept when the primary one produced no response
        at all, so the common case costs one request per candidate shard rather than two.

        Args:
            tenant: The Workday tenant.
            preferred_site: A career-site name the caller already knows, tried first.
            preferred_shard: A shard the caller already knows, probed first.
            preferred_host: A hostname the caller already knows, which fixes the domain.

        Returns:
            The confirmed site, or ``None`` when no shard answers, or when the shard answers
            and no career-site name confirms.
        """
        shards = _ordered(SHARD_CANDIDATES, preferred_shard)

        for domain in _domain_order(preferred_host):
            for shard in shards:
                host = f"{tenant}.{shard}.{domain}"
                robots = await self._fetch_robots(host)
                if robots is None:
                    continue

                published = career_sites_from_robots(robots)
                for name in _usable_site_names([preferred_site, *published]):
                    site = CareerSite(tenant=tenant, shard=shard, site=name, host=host)
                    if await self._confirm(site):
                        self.logger.debug(
                            "workday.site_resolved_from_robots", tenant=tenant, site=site.token
                        )
                        return site

                for name in site_name_candidates(tenant, preferred=preferred_site):
                    site = CareerSite(tenant=tenant, shard=shard, site=name, host=host)
                    if await self._confirm(site):
                        self.logger.debug(
                            "workday.site_resolved_by_convention", tenant=tenant, site=site.token
                        )
                        return site

                # The shard is right and no name confirmed. Sweeping the remaining shards
                # cannot help — a tenant lives on exactly one — and every one of them would
                # answer 422, so stopping here saves ten pointless requests.
                self.logger.info(
                    "workday.site_name_unresolved",
                    tenant=tenant,
                    host=host,
                    published=published,
                )
                return None

        self.logger.debug("workday.no_shard_found", tenant=tenant, shards=len(shards))
        return None

    async def _fetch_robots(self, host: str) -> str | None:
        """Fetch a candidate host's ``robots.txt``.

        Args:
            host: The candidate hostname.

        Returns:
            The truncated file body, or ``None`` when the host does not serve this tenant —
            ``422`` for the wrong shard, ``410`` ``ERR_TENANT_MIGRATED`` for a tenant that has
            moved, or a DNS failure for a shard that does not exist at all. A single attempt
            only: probing is a guess-and-check loop, and retrying a host that is simply the
            wrong shard would multiply the cost of resolution by the retry budget.
        """
        url = f"https://{host}{ROBOTS_PATH}"
        try:
            response = await self._request("GET", url, max_attempts=1)
        except ProviderError as exc:
            self.logger.debug(
                "workday.shard_probe_miss", host=host, status_code=exc.status_code, error=str(exc)
            )
            return None
        return response.text[:MAX_ROBOTS_CHARS]

    async def _confirm(self, site: CareerSite) -> bool:
        """Verify that a candidate board really serves the Workday jobs API.

        Args:
            site: The candidate.

        Returns:
            ``True`` when the CXS jobs endpoint answers with a posting envelope. A ``404``
            here means the shard is right and this particular career-site name is not, and a
            :data:`WRONG_SHARD_STATUS` means the tenant is not on this shard at all; both are
            logged with the status so a resolution failure is diagnosable from the log alone.
            A single attempt, for the same reason as :meth:`_fetch_robots`.
        """
        payload = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
        try:
            response = await self._request("POST", site.jobs_url, json=payload, max_attempts=1)
            document = self._decode(response, site.jobs_url)
        except ProviderError as exc:
            self.logger.debug(
                "workday.site_probe_miss",
                site=site.token,
                status_code=exc.status_code,
                error=str(exc),
            )
            return False
        return "jobPostings" in document or "total" in document

    async def _read_cached_site(self, key: str) -> CareerSite | str | None:
        """Read a durable resolution for *key*.

        Args:
            key: The normalised board token.

        Returns:
            The cached :class:`CareerSite`, the literal ``"miss"`` for a cached failure, or
            ``None`` when nothing is cached. A cache backend that is unavailable — no Redis
            in a slim install — is not an error: resolution simply costs its probes again.
        """
        payload = await self._cache_get(key)
        if payload is None:
            return None
        if isinstance(payload, Mapping) and payload.get("resolved") is False:
            return "miss"
        return CareerSite.from_dict(payload)

    async def _write_cached_site(self, key: str, site: CareerSite | None) -> None:
        """Record a resolution durably, positive or negative.

        Args:
            key: The normalised board token.
            site: The confirmed site, or ``None`` for a confirmed failure.
        """
        if site is None:
            await self._cache_set(key, {"resolved": False}, SITE_MISS_CACHE_TTL_SECONDS)
        else:
            await self._cache_set(key, site.as_dict(), SITE_CACHE_TTL_SECONDS)

    async def _cache_get(self, key: str) -> Any | None:
        """Read one entry from the shared cache, tolerating an unavailable backend.

        Args:
            key: The normalised board token.

        Returns:
            The stored value, or ``None`` on a miss or a backend failure.
        """
        try:
            from app.cache import (
                NAMESPACES,
                get_cache,
                make_key,
            )

            return await get_cache().get(make_key(NAMESPACES.POSTING, "workday", "site", key))
        except Exception as exc:
            self.logger.debug("workday.site_cache_unavailable", error=str(exc))
            return None

    async def _cache_set(self, key: str, value: Any, ttl: int) -> None:
        """Write one entry to the shared cache, tolerating an unavailable backend.

        Args:
            key: The normalised board token.
            value: The JSON-ready value to store.
            ttl: Lifetime in seconds.
        """
        try:
            from app.cache import (
                NAMESPACES,
                get_cache,
                make_key,
            )

            await get_cache().set(
                make_key(NAMESPACES.POSTING, "workday", "site", key), value, ttl=ttl
            )
        except Exception as exc:
            self.logger.debug("workday.site_cache_write_failed", error=str(exc))

    # -- transport helper ---------------------------------------------------------------

    def _decode(self, response: Any, url: str) -> dict[str, Any]:
        """Decode a CXS response body as a JSON object.

        Args:
            response: The ``httpx`` response.
            url: The URL it came from, for the error message.

        Returns:
            The decoded object.

        Raises:
            ProviderError: When the body is not JSON, or is JSON but not an object — a career
                site answering 200 with an HTML error page is a provider failure, not a
                parsing bug at the call site.
        """
        try:
            document = response.json()
        except ValueError as exc:
            raise ProviderError(
                f"workday returned a non-JSON body from {url}: {exc}",
                provider=self.provider_name,
                status_code=getattr(response, "status_code", None),
                url=url,
            ) from exc
        if not isinstance(document, dict):
            raise ProviderError(
                f"workday returned {type(document).__name__} rather than an object from {url}",
                provider=self.provider_name,
                status_code=getattr(response, "status_code", None),
                url=url,
            )
        return document

    # -- posture ------------------------------------------------------------------------

    async def apply(self, ctx: ApplyContext) -> ApplyResult:
        """Refuse to submit, always, and say why.

        Workday applications are not automated by this system. The reasons are set out in
        full in the module docstring: the flow is gated behind a per-tenant account, it is a
        wizard whose steps each employer configures independently, and a partially completed
        attempt leaves a real candidate record against a real requisition. There is no
        configuration that turns this on, because there is no version of it that is
        simultaneously reliable and safe.

        The posting is not lost. The pipeline converts this exception into a
        ``needs_review`` application carrying
        :attr:`~app.models.enums.ReviewReason.UNSUPPORTED_FLOW` and the URL, so the user
        applies in one click from the review queue with a tailored resume already rendered
        (``docs/CONTRACTS.md`` §9, golden rule #10).

        Args:
            ctx: The attempt context, used for the URL and the log correlation fields.

        Returns:
            Never returns.

        Raises:
            UnsupportedFlowError: Always.
        """
        self.logger.info("workday.manual_review_required", **ctx.log_context())
        raise UnsupportedFlowError(
            "Workday applications are account-gated and configured per employer, so "
            "ApplicantOS never submits them automatically; apply manually at "
            f"{ctx.posting.target_url}",
            provider=self.provider_name,
            url=ctx.posting.target_url,
        )

    async def healthcheck(self) -> bool:
        """Report whether this provider is usable.

        Always ``True``. Workday has no tenant-independent endpoint: "is Workday up" is only
        answerable per career site, and a readiness probe that polled every configured tenant
        would be both slow and a good way to get rate-limited by all of them at once. A board
        that is genuinely unreachable surfaces as a ``workday.board_failed`` log line during
        discovery, which is where the information is actionable.

        Returns:
            ``True``.
        """
        return True


# ======================================================================================
# Value mapping helpers
# ======================================================================================


def _token_key(token: str) -> str:
    """Reduce a board token to its cache and memo key.

    Args:
        token: A board token in any accepted form.

    Returns:
        The trimmed, lowercased token with surrounding slashes removed.
    """
    return clean_text(token).strip("/").lower()


def _tenant_display_name(tenant: str) -> str:
    """Render a tenant token as a human-readable employer name.

    Only a fallback: :attr:`hiringOrganization.name` from the detail payload is used whenever
    it is present, and company resolution normalises whatever arrives here anyway
    (:func:`app.jobs.dedupe.normalize_company`).

    Args:
        tenant: The Workday tenant token.

    Returns:
        The token with separators turned into spaces and each word capitalised.
    """
    words = [word for word in re.split(r"[-_]+", tenant) if word]
    return " ".join(word.capitalize() for word in words) or tenant


def _bullet_values(value: Any) -> list[str]:
    """Return the cleaned entries of a search result's ``bulletFields``.

    Workday puts the requisition id first and, on many tenants, the time type second.

    Args:
        value: The raw ``bulletFields`` value.

    Returns:
        The non-empty entries, in order. Empty for anything that is not a list.
    """
    if not isinstance(value, (list, tuple)):
        return []
    return [cleaned for cleaned in (clean_text(item) for item in value) if cleaned]


def _first_bullet(value: Any) -> str | None:
    """Return the first ``bulletFields`` entry, which is the requisition id.

    Args:
        value: The raw ``bulletFields`` value.

    Returns:
        The first entry, or ``None``.
    """
    bullets = _bullet_values(value)
    return bullets[0] if bullets else None


def _informative_location(text: str) -> str | None:
    """Return *text* when it names a place, rather than counting places.

    Args:
        text: A cleaned ``locationsText`` value.

    Returns:
        The text, or ``None`` when it is a bare count such as ``"3 Locations"``.
    """
    if not text or _LOCATION_COUNT_RE.match(text):
        return None
    return text


def _matches_locations(wanted: Iterable[str], *texts: str) -> bool:
    """Return whether any requested location appears in any of *texts*.

    Args:
        wanted: The locations the query asked for.
        *texts: Location strings from the posting.

    Returns:
        ``True`` when no location was requested, or when one matches case-insensitively.
    """
    requested = [entry.casefold() for entry in wanted if entry]
    if not requested:
        return True
    haystack = " | ".join(text.casefold() for text in texts if text)
    if not haystack:
        return False
    return any(entry in haystack for entry in requested)


#: Workday's ``remoteType`` vocabulary. The values are tenant-configurable labels, so they
#: are matched on a substring of the lowercased value rather than by equality.
_REMOTE_TYPE_RULES: Final[tuple[tuple[str, WorkArrangement], ...]] = (
    ("hybrid", WorkArrangement.HYBRID),
    ("flexible", WorkArrangement.HYBRID),
    ("remote", WorkArrangement.REMOTE),
    ("field", WorkArrangement.REMOTE),
    ("on-site", WorkArrangement.ONSITE),
    ("onsite", WorkArrangement.ONSITE),
    ("on site", WorkArrangement.ONSITE),
    ("in office", WorkArrangement.ONSITE),
)


def _map_remote_type(value: Any) -> WorkArrangement:
    """Map Workday's ``remoteType`` onto a :class:`~app.models.enums.WorkArrangement`.

    Args:
        value: The raw ``remoteType``, e.g. ``"Remote"`` or ``"Flexible / Hybrid"``.

    Returns:
        The arrangement, or :attr:`~app.models.enums.WorkArrangement.UNKNOWN` when the label
        says nothing recognisable. ``UNKNOWN`` sends the caller to text inference rather than
        to a guess.
    """
    text = clean_text(value).lower()
    if not text:
        return WorkArrangement.UNKNOWN
    for needle, arrangement in _REMOTE_TYPE_RULES:
        if needle in text:
            return arrangement
    return WorkArrangement.UNKNOWN


#: Workday's ``timeType`` vocabulary, consulted only when the title and description said
#: nothing more specific — an internship is also "Full time", and reporting it as full time
#: would lose the fact that matters.
_TIME_TYPE_RULES: Final[tuple[tuple[str, EmploymentType], ...]] = (
    ("part time", EmploymentType.PART_TIME),
    ("part-time", EmploymentType.PART_TIME),
    ("intern", EmploymentType.INTERNSHIP),
    ("fixed term", EmploymentType.CONTRACT),
    ("temporary", EmploymentType.CONTRACT),
    ("contingent", EmploymentType.CONTRACT),
    ("full time", EmploymentType.FULL_TIME),
    ("full-time", EmploymentType.FULL_TIME),
)


def _map_time_type(value: Any) -> EmploymentType:
    """Map Workday's ``timeType`` onto an :class:`~app.models.enums.EmploymentType`.

    Args:
        value: The raw ``timeType``, e.g. ``"Full time"``.

    Returns:
        The employment type, or :attr:`~app.models.enums.EmploymentType.UNKNOWN`.
    """
    text = clean_text(value).lower()
    if not text:
        return EmploymentType.UNKNOWN
    for needle, employment_type in _TIME_TYPE_RULES:
        if needle in text:
            return employment_type
    return EmploymentType.UNKNOWN
