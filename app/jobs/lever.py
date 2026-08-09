"""Lever — job discovery **and real automated submission** (``docs/CONTRACTS.md`` §9).

**Automation posture (binding, golden rule #10).** A Lever job board is a *public
application form*. ``https://jobs.lever.co/<company>/<id>/apply`` renders and accepts the
same form for an anonymous visitor as for anyone else: **no account, no login and no session
are required**, which is why :attr:`LeverProvider.requires_login` is ``False`` and
:attr:`LeverProvider.supports_auto_apply` is ``True``. Discovery reads the public postings
API at ``api.lever.co/v0/postings/<company>`` — the same unauthenticated feed employers
embed in their own careers pages — and every request identifies itself with
:data:`app.jobs.base.USER_AGENT`.

Submission goes through :func:`app.jobs._apply.run_browser_apply`, so the kill switch is
unchanged: nothing is submitted unless ``settings.auto_apply_enabled`` is on **and**
``settings.dry_run`` is off **and** the caller did not ask for a dry run (golden rule #3),
and any question that cannot be answered confidently escalates to a human rather than being
guessed (golden rule #2).

**What the feed looks like.** ``GET /v0/postings/<company>?mode=json`` returns a bare JSON
array of postings, offset-paginated with ``skip`` and ``limit``. A posting is *pre-split*
into the pieces Lever's editor produces — an opening (``descriptionPlain``), a series of
titled bullet lists (``lists``), and a closing note (``additionalPlain``) — so the readable
description this provider stores is those pieces reassembled in reading order, not any one
field on its own. Storing ``descriptionPlain`` alone would silently drop every requirement
and responsibility from the posting, which is exactly the text a resume gets tailored
against.

Two structured fields are worth more than any inference and are used in preference to it:
``categories.commitment`` states the employment type, and ``salaryRange`` states the
advertised band with its own currency and pay interval. Where ``salaryRange`` is absent —
most non-US boards — the range stays ``None``. Lever descriptions are prose, and mining them
for a number produces a confident wrong answer far more often than a right one; "not
disclosed" is the honest result and the scorer knows what to do with it.

The feed offers no server-side keyword, location or freshness filter, and it is not sorted,
so every :class:`~app.jobs.base.SearchQuery` field is applied here — cheaply, against the
raw fragments, before any posting is reassembled into text or converted into a
:class:`~app.jobs.base.RawPosting`.
"""

from __future__ import annotations

import re
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from contextlib import aclosing
from datetime import UTC, datetime
from typing import Any, ClassVar, Final

import structlog

from app.jobs._apply import browser_available, run_browser_apply
from app.jobs._parsing import (
    ANNUAL_HOURS,
    MAX_PLAUSIBLE_ANNUAL_SALARY,
    MIN_PLAUSIBLE_ANNUAL_SALARY,
    clean_text,
    html_to_text,
    infer_arrangement,
    infer_employment_type,
    parse_date,
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
from app.models.enums import (
    ATSProviderName,
    EmploymentType,
    PluginKind,
    WorkArrangement,
)
from app.plugins.base import PluginMeta
from app.plugins.registry import plugin

__all__ = [
    "API_ROOT",
    "APPLY_URL_TEMPLATE",
    "BOARD_PAGE_TTL_SECONDS",
    "HEALTHCHECK_COMPANY",
    "JOB_URL_TEMPLATE",
    "MAX_LOOKUP_COMPANIES",
    "MAX_PAGES_PER_COMPANY",
    "PAGE_SIZE",
    "POSTING_TTL_SECONDS",
    "SELECTOR_PACK",
    "LeverProvider",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Endpoints
# ======================================================================================

#: Root of the public postings API. Documented, unauthenticated, and the same feed employers
#: embed in their own careers pages.
API_ROOT: Final[str] = "https://api.lever.co/v0/postings"

#: Human-facing posting page, used when a posting omits ``hostedUrl``.
JOB_URL_TEMPLATE: Final[str] = "https://jobs.lever.co/{company}/{job_id}"

#: The application form itself, used when a posting omits ``applyUrl``.
APPLY_URL_TEMPLATE: Final[str] = "https://jobs.lever.co/{company}/{job_id}/apply"

#: Selector pack name handed to the browser layer by :meth:`LeverProvider.apply`.
SELECTOR_PACK: Final[str] = "lever"

#: The company board probed by :meth:`LeverProvider.healthcheck`. Large and long-lived, and
#: the probe asks for a single posting, so it costs a few kilobytes.
HEALTHCHECK_COMPANY: Final[str] = "veeva"

#: Seconds allowed for the healthcheck probe. Deliberately short: a readiness endpoint that
#: blocks for thirty seconds is worse than one that reports "not ready".
HEALTHCHECK_TIMEOUT_SECONDS: Final[float] = 8.0


# ======================================================================================
# Paging and caching
# ======================================================================================

#: Postings requested per page. Lever accepts far larger values, but a hundred keeps one
#: cached page to roughly a megabyte and makes a partial run's cache entries reusable by the
#: next run rather than all-or-nothing.
PAGE_SIZE: Final[int] = 100

#: TTL for one page of a company's feed. Shorter than the 30-minute ``jobs.poll_all`` beat
#: interval, so a scheduled discovery run always sees fresh data while repeated lookups
#: inside one run — and a user clicking "discover" twice — are served from cache.
BOARD_PAGE_TTL_SECONDS: Final[int] = 900

#: TTL for one posting fetched by identifier. Longer than a feed page: a single posting is
#: read while scoring or applying, and its text changes far less often than a board's roster.
POSTING_TTL_SECONDS: Final[int] = 3600


# ======================================================================================
# Limits
# ======================================================================================

#: Company boards scanned when resolving a *bare* posting id whose company is not knowable
#: from the input and was not seen during this process's discovery. Each scan reads only the
#: first page of a board and goes through the cache, so the cost is bounded and usually zero
#: immediately after a discovery run.
MAX_LOOKUP_COMPANIES: Final[int] = 40

#: Ceiling on the in-process "which company does this posting id belong to" memo. Reached
#: only after roughly twenty thousand distinct postings, at which point the memo is dropped
#: wholesale rather than grown without bound; the next lookup simply re-scans.
MAX_MEMOIZED_IDS: Final[int] = 20_000

#: Pages read per company in one search. At :data:`PAGE_SIZE` postings each this covers a
#: fifty-thousand-posting board, an order of magnitude beyond the largest real one; it exists
#: so that a feed which never reports exhaustion cannot spin forever.
MAX_PAGES_PER_COMPANY: Final[int] = 500


# ======================================================================================
# URL shapes
# ======================================================================================

#: Lever posting identifiers are UUIDs.
_ID_PATTERN: Final[str] = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

#: A posting on the hosted board, optionally followed by ``/apply`` or a thank-you page.
#: ``jobs.eu.lever.co`` is the EU data-residency host and addresses the same postings.
_HOSTED_JOB_RE: Final[re.Pattern[str]] = re.compile(
    r"\bjobs\.(?:eu\.)?lever\.co/(?P<company>[a-z0-9][a-z0-9._-]*)/(?P<job_id>"
    + _ID_PATTERN
    + r")",
    re.IGNORECASE,
)

#: The API URL itself, so a link copied out of a debugging session round-trips.
_API_JOB_RE: Final[re.Pattern[str]] = re.compile(
    r"\bapi\.(?:eu\.)?lever\.co/v0/postings/(?P<company>[a-z0-9][a-z0-9._-]*)/(?P<job_id>"
    + _ID_PATTERN
    + r")",
    re.IGNORECASE,
)

#: A hosted board URL with no posting in it.
_HOSTED_BOARD_RE: Final[re.Pattern[str]] = re.compile(
    r"\bjobs\.(?:eu\.)?lever\.co/(?P<company>[a-z0-9][a-z0-9._-]*)",
    re.IGNORECASE,
)

#: ``<company>/<id>`` or ``<company>:<id>``, the composite form this provider accepts so that
#: a caller holding both halves never has to build a URL.
_COMPOSITE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<company>[a-z0-9][a-z0-9._-]*)\s*[/:]\s*(?P<job_id>" + _ID_PATTERN + r")$",
    re.IGNORECASE,
)

#: A Lever posting identifier on its own.
_BARE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^" + _ID_PATTERN + r"$", re.IGNORECASE)


# ======================================================================================
# Payload helpers
# ======================================================================================

#: Word separators inside a company token.
_TOKEN_SEPARATOR_RE: Final[re.Pattern[str]] = re.compile(r"[-_\s]+")

#: Everything that is not an ASCII letter, stripped before matching a ``commitment`` value so
#: that ``"Full-Time"``, ``"full time"`` and ``"FullTime"`` all resolve to one key.
_NON_LETTER_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z]")

#: Sort key for a posting whose ``createdAt`` could not be parsed, so that undated postings
#: sort last under ``reverse=True``. An absent date is not a claim of freshness.
_UNDATED: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)

#: Markers looked for inside a ``categories.commitment`` value, in decreasing order of
#: specificity; the first that appears wins.
#:
#: Matched as substrings rather than as whole values on purpose. ``commitment`` is *free
#: text* — every employer types their own — and the boards in ``app.jobs.seeds`` alone
#: produce "Full Time Employee", "Full-time", "Regular Full-Time" and "FullTime" for the same
#: thing. An exact-match table silently fails on all but one spelling, and the fall-through
#: then hands the decision to a description whose benefits paragraph mentions "part-time
#: employees are not eligible" — which is exactly how a director's role gets filed as part
#: time.
#:
#: The order matters for the same reason it does in :mod:`app.jobs._parsing`: an internship is
#: also full-time, and "Full Time Intern" must classify as an internship.
_COMMITMENT_MARKERS: Final[tuple[tuple[str, EmploymentType], ...]] = (
    ("internship", EmploymentType.INTERNSHIP),
    ("intern", EmploymentType.INTERNSHIP),
    ("coop", EmploymentType.INTERNSHIP),
    ("apprentice", EmploymentType.INTERNSHIP),
    ("trainee", EmploymentType.INTERNSHIP),
    ("newgrad", EmploymentType.NEW_GRAD),
    ("graduate", EmploymentType.NEW_GRAD),
    # "Full Time/Part Time" means either is on offer — several hundred postings on the seed
    # boards say exactly that. It must be read before the bare "parttime" marker, or the more
    # restrictive half wins and a role a full-time candidate could take disappears from their
    # results.
    ("fulltimeparttime", EmploymentType.FULL_TIME),
    ("parttimefulltime", EmploymentType.FULL_TIME),
    ("parttime", EmploymentType.PART_TIME),
    ("contractor", EmploymentType.CONTRACT),
    ("contract", EmploymentType.CONTRACT),
    ("consultant", EmploymentType.CONTRACT),
    ("freelance", EmploymentType.CONTRACT),
    ("temporary", EmploymentType.CONTRACT),
    ("temp", EmploymentType.CONTRACT),
    ("seasonal", EmploymentType.CONTRACT),
    ("fixedterm", EmploymentType.CONTRACT),
    ("fulltime", EmploymentType.FULL_TIME),
    ("permanent", EmploymentType.FULL_TIME),
    ("regular", EmploymentType.FULL_TIME),
)

#: ``workplaceType`` values mapped onto the contract's work arrangements.
_WORKPLACE_ARRANGEMENTS: Final[dict[str, WorkArrangement]] = {
    "remote": WorkArrangement.REMOTE,
    "hybrid": WorkArrangement.HYBRID,
    "onsite": WorkArrangement.ONSITE,
    "on-site": WorkArrangement.ONSITE,
    "unspecified": WorkArrangement.UNKNOWN,
}

#: Working days in a year, for a day-rate band. Matches
#: :mod:`app.jobs._parsing`'s convention.
_ANNUAL_WORKING_DAYS: Final[int] = 260

#: Weeks in a year, for a weekly band.
_WEEKS_PER_YEAR: Final[int] = 52

#: ``salaryRange.interval`` values mapped onto the multiplier that annualises them. The hourly
#: multiplier is imported from :mod:`app.jobs._parsing` so that a Lever band and a band parsed
#: out of another provider's free text annualise identically — otherwise two postings for the
#: same job, discovered through two providers, would not compare equal.
_SALARY_INTERVAL_MULTIPLIERS: Final[dict[str, int]] = {
    "per-year-salary": 1,
    "per-half-year-salary": 2,
    "per-quarter-salary": 4,
    "per-month-salary": 12,
    "per-week-salary": _WEEKS_PER_YEAR,
    "per-day-wage": _ANNUAL_WORKING_DAYS,
    "per-hour-wage": ANNUAL_HOURS,
}

#: The interval assumed when ``salaryRange`` states none. Annual is how compensation is
#: advertised by default, and it is the same assumption :func:`app.jobs._parsing.parse_salary`
#: makes.
_DEFAULT_SALARY_INTERVAL: Final[str] = "per-year-salary"

#: Description fragments consulted by the cheap keyword probe, in reading order.
_TEXT_FIELDS: Final[tuple[str, ...]] = (
    "descriptionPlain",
    "description",
    "additionalPlain",
    "additional",
)

#: Separator drawn between the reassembled parts of a description.
_SECTION_SEPARATOR: Final[str] = "\n\n"


def _company_from_token(token: str) -> str:
    """Turn a company token into a presentable employer name.

    The Lever feed never states the employer's name — the token in the URL is the only
    identification there is — so this is the sole source for
    :attr:`~app.jobs.base.RawPosting.company_name`. Company resolution and enrichment happen
    later in the pipeline; a recognisable name is all that is needed to key on.

    Args:
        token: A company token such as ``"lucid-motors"``.

    Returns:
        The de-slugged name (``"Lucid Motors"``), or ``""`` for an empty token.
    """
    parts = [part for part in _TOKEN_SEPARATOR_RE.split(clean_text(token)) if part]
    return " ".join(part[:1].upper() + part[1:] for part in parts)


def _text_of(value: Any) -> str:
    """Return the readable text of one description fragment.

    Args:
        value: A ``…Plain`` field (already plain text) or an HTML fragment. Non-strings and
            empty strings yield ``""``.

    Returns:
        Plain text with paragraph structure preserved.
    """
    return html_to_text(value) if isinstance(value, str) and value else ""


def _description_of(job: Mapping[str, Any]) -> str:
    """Reassemble a Lever posting into one readable description.

    Lever's editor splits a posting into an opening, a series of titled bullet lists, and a
    closing note, and the API returns each separately. All of them are part of what a human
    reads, and the bullet lists in particular are where the requirements live — so every
    piece is joined in reading order, with each list's heading kept as a heading.

    The plain-text variants are preferred where Lever supplies them and the HTML variants
    converted where it does not, so a board that publishes only markup still yields text.

    Args:
        job: One posting from the feed.

    Returns:
        The full description as plain text, or ``""`` when the posting carries none.
    """
    sections: list[str] = []

    opening = _text_of(job.get("descriptionPlain")) or _text_of(job.get("description"))
    if opening:
        sections.append(opening)

    lists = job.get("lists")
    if isinstance(lists, Sequence) and not isinstance(lists, (str, bytes)):
        for entry in lists:
            if not isinstance(entry, Mapping):
                continue
            heading = clean_text(entry.get("text"))
            body = _text_of(entry.get("content"))
            block = _SECTION_SEPARATOR.join(part for part in (heading, body) if part)
            if block:
                sections.append(block)

    closing = _text_of(job.get("additionalPlain")) or _text_of(job.get("additional"))
    if closing:
        sections.append(closing)

    return _SECTION_SEPARATOR.join(sections)


def _locations_of(job: Mapping[str, Any]) -> list[str]:
    """Collect every place a posting says it is in.

    Args:
        job: One posting from the feed.

    Returns:
        Cleaned, de-duplicated location strings, the primary one first. Lever states a single
        ``categories.location`` and, on multi-site requisitions, an ``allLocations`` array
        that repeats it.
    """
    categories = job.get("categories")
    if not isinstance(categories, Mapping):
        return []

    candidates: list[Any] = [categories.get("location")]
    every = categories.get("allLocations")
    if isinstance(every, Sequence) and not isinstance(every, (str, bytes)):
        candidates.extend(every)

    names: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        name = clean_text(candidate)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names


def _commitment_type(commitment: str) -> EmploymentType | None:
    """Map a free-text ``categories.commitment`` value onto an employment type.

    Args:
        commitment: The employer's own wording, e.g. ``"Full Time Employee"``.

    Returns:
        The first :data:`_COMMITMENT_MARKERS` entry whose marker appears in the value, or
        ``None`` when the value says nothing recognisable — in which case the caller falls
        back to inference rather than guessing.
    """
    reduced = _NON_LETTER_RE.sub("", commitment.lower())
    if not reduced:
        return None
    for marker, employment_type in _COMMITMENT_MARKERS:
        if marker in reduced:
            return employment_type
    logger.debug("lever.commitment_unrecognised", commitment=commitment[:60])
    return None


def _employment_type_of(job: Mapping[str, Any], title: str, description: str) -> EmploymentType:
    """Determine what kind of engagement a posting advertises.

    ``categories.commitment`` is the employer's own structured statement and is trusted
    first — with one deliberate exception. An internship *is* full-time, and a board that
    files "Software Engineering Intern, Summer 2026" under ``Full-Time`` is not wrong so much
    as less specific than its own title; reporting it as full-time would lose exactly the
    information a user filtering for internships cares about. So a title that unambiguously
    says internship or new grad wins over a generic full- or part-time commitment, and
    nothing else does.

    Args:
        job: One posting from the feed.
        title: The cleaned role title.
        description: The reassembled description.

    Returns:
        The advertised type, or :attr:`~app.models.enums.EmploymentType.UNKNOWN` when neither
        the structured field nor the text says.
    """
    categories = job.get("categories")
    commitment = clean_text(categories.get("commitment")) if isinstance(categories, Mapping) else ""
    stated = _commitment_type(commitment)

    if stated is None or stated in (EmploymentType.FULL_TIME, EmploymentType.PART_TIME):
        from_title = infer_employment_type(title)
        if from_title in (EmploymentType.INTERNSHIP, EmploymentType.NEW_GRAD):
            return from_title

    if stated is not None:
        return stated
    return infer_employment_type(title, description)


def _arrangement_of(job: Mapping[str, Any], locations: Sequence[str], text: str) -> WorkArrangement:
    """Determine where a role is performed.

    ``workplaceType`` is Lever's structured statement and is trusted outright. Only when it is
    absent or unrecognised does the location string get a vote, and only when that is silent
    too is the description consulted — decreasing order of authority, so that a description
    mentioning "our remote-first culture" cannot override a posting that says it is onsite.

    Args:
        job: One posting from the feed.
        locations: Every location the posting states.
        text: The title and reassembled description, joined.

    Returns:
        The arrangement, or :attr:`~app.models.enums.WorkArrangement.UNKNOWN` when nothing
        says. ``UNKNOWN`` is honest and never a guess; the scorer handles it explicitly.
    """
    stated = _WORKPLACE_ARRANGEMENTS.get(clean_text(job.get("workplaceType")).lower())
    if stated is not None and stated is not WorkArrangement.UNKNOWN:
        return stated

    for location in locations:
        inferred = infer_arrangement(location)
        if inferred is not WorkArrangement.UNKNOWN:
            return inferred
    return infer_arrangement(text)


def _annualize(value: Any, multiplier: int) -> int | None:
    """Annualise one end of an advertised band, discarding implausible figures.

    Args:
        value: The raw bound from ``salaryRange``.
        multiplier: Pay periods per year for the band's interval.

    Returns:
        The annualised amount, or ``None`` when the bound is missing, non-numeric, zero
        (Lever's placeholder on boards that publish the object regardless of disclosure), or
        outside :data:`~app.jobs._parsing.MIN_PLAUSIBLE_ANNUAL_SALARY` …
        :data:`~app.jobs._parsing.MAX_PLAUSIBLE_ANNUAL_SALARY`.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    annual = round(float(value) * multiplier)
    if MIN_PLAUSIBLE_ANNUAL_SALARY <= annual <= MAX_PLAUSIBLE_ANNUAL_SALARY:
        return annual
    return None


def _salary_of(job: Mapping[str, Any]) -> tuple[int | None, int | None, str | None]:
    """Read the advertised compensation band from ``salaryRange``.

    Both bounds are annualised using the same constants as
    :func:`app.jobs._parsing.parse_salary`, so that a Lever band and a band recovered from
    another provider's free text mean the same thing and the user's ``min_salary``
    preference compares like with like.

    Nothing is inferred from the description when the field is absent. Lever descriptions are
    prose, and a number mined out of one is far more often a funding round or a customer count
    than a salary; ``(None, None, …)`` is the honest answer (golden rule #2).

    Args:
        job: One posting from the feed.

    Returns:
        ``(minimum, maximum, currency)``, any element of which may be ``None``.
    """
    band = job.get("salaryRange")
    if not isinstance(band, Mapping):
        return (None, None, None)

    interval = clean_text(band.get("interval")).lower()
    multiplier = _SALARY_INTERVAL_MULTIPLIERS.get(interval)
    if multiplier is None:
        if interval:
            logger.debug("lever.salary_interval_unknown", interval=interval)
        multiplier = _SALARY_INTERVAL_MULTIPLIERS[_DEFAULT_SALARY_INTERVAL]

    minimum = _annualize(band.get("min"), multiplier)
    maximum = _annualize(band.get("max"), multiplier)
    currency = clean_text(band.get("currency")) or None
    return (minimum, maximum, currency)


def _matches_any(terms: Sequence[str], haystacks: Iterable[str]) -> bool:
    """Return whether any of *terms* appears in any of *haystacks*.

    OR semantics, as :class:`~app.jobs.base.SearchQuery` documents for both ``keywords`` and
    ``locations``, and case-insensitive substring matching.

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


def _ordered(jobs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Return one page sorted newest first.

    Length-preserving on purpose: :meth:`~app.jobs.base.ATSProvider.paginate` decides that an
    offset feed is exhausted when a page comes back shorter than the requested size, so the
    callback that feeds it may reorder a page but must never shrink one.

    Args:
        jobs: One page of postings.

    Returns:
        The same postings, newest first, with undated ones last.
    """
    return sorted(jobs, key=lambda job: parse_date(job.get("createdAt")) or _UNDATED, reverse=True)


def _keyword_haystacks(job: Mapping[str, Any], title: str) -> list[str]:
    """Return the strings a keyword filter is matched against.

    Args:
        job: One posting from the feed.
        title: The cleaned role title.

    Returns:
        The title plus every raw description fragment the feed carries. Matching the raw
        fragments — rather than the reassembled text — is what keeps the filter cheap; they
        contain a superset of the readable words.
    """
    haystacks = [title]
    for field_name in _TEXT_FIELDS:
        value = job.get(field_name)
        if isinstance(value, str) and value:
            haystacks.append(value)

    lists = job.get("lists")
    if isinstance(lists, Sequence) and not isinstance(lists, (str, bytes)):
        for entry in lists:
            if not isinstance(entry, Mapping):
                continue
            for key in ("text", "content"):
                value = entry.get(key)
                if isinstance(value, str) and value:
                    haystacks.append(value)
    return haystacks


def _may_be_remote(job: Mapping[str, Any], title: str, locations: Sequence[str]) -> bool:
    """Return whether a posting could plausibly be remote, without reassembling it.

    A deliberate over-approximation used only to skip work. It answers "no" only when Lever's
    structured ``workplaceType`` positively says hybrid or onsite, and otherwise accepts
    anything whose title, location or opening mentions remote work at all; the strict test —
    :func:`_arrangement_of`, which honours the structured field and negation — runs afterwards
    in :meth:`LeverProvider.search`.

    Args:
        job: One posting from the feed.
        title: The cleaned role title.
        locations: Every location the posting states.

    Returns:
        ``True`` when the posting is worth reassembling and testing properly.
    """
    stated = _WORKPLACE_ARRANGEMENTS.get(clean_text(job.get("workplaceType")).lower())
    if stated is WorkArrangement.REMOTE:
        return True
    if stated in (WorkArrangement.HYBRID, WorkArrangement.ONSITE):
        return False
    if "remote" in title.lower() or any("remote" in place.lower() for place in locations):
        return True
    opening = job.get("descriptionPlain")
    return isinstance(opening, str) and "remote" in opening.lower()


def _parse_reference(reference: str) -> tuple[str, str]:
    """Split a caller-supplied reference into ``(company, job_id)``.

    Args:
        reference: A cleaned identifier or URL.

    Returns:
        The company token (``""`` when the reference does not carry one) and the posting
        identifier (``""`` when the reference carries none at all).
    """
    for pattern in (_HOSTED_JOB_RE, _API_JOB_RE):
        match = pattern.search(reference)
        if match:
            return match.group("company"), match.group("job_id")

    match = _COMPOSITE_RE.match(reference)
    if match:
        return match.group("company"), match.group("job_id")

    if _BARE_ID_RE.match(reference):
        return "", reference

    match = _HOSTED_BOARD_RE.search(reference)
    if match:
        # A board URL with no posting in it: addressable, but not a posting.
        return match.group("company"), ""
    return "", ""


@plugin
class LeverProvider(ATSProvider):
    """Discovery and automated submission against Lever job boards.

    One instance per process (providers are plugin-registry singletons), so the HTTP
    connection pool and the posting-id memo are shared across every discovery run.

    Class attributes:
        meta: Plugin identity, registered under the name ``"lever"``.
        name: :attr:`~app.models.enums.ATSProviderName.LEVER`.
        supports_auto_apply: ``True`` — the application form is public and account-free.
        requires_login: ``False`` — neither discovery nor submission needs a session.
        URL_PATTERNS: Every shape a Lever posting URL takes in the wild.
    """

    meta: ClassVar[PluginMeta] = PluginMeta(
        kind=PluginKind.PROVIDER,
        name=ATSProviderName.LEVER.value,
        version="1.0.0",
        display_name="Lever",
        description=(
            "Lever job boards — public JSON postings feed for discovery, public application "
            "form for submission. No account required."
        ),
        author="ApplicantOS",
        capabilities=frozenset({"search", "fetch", "auto_apply"}),
    )
    name: ClassVar[ATSProviderName] = ATSProviderName.LEVER
    supports_auto_apply: ClassVar[bool] = True
    requires_login: ClassVar[bool] = False
    URL_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"\bjobs\.(?:eu\.)?lever\.co/", re.IGNORECASE),
        re.compile(r"\bapi\.(?:eu\.)?lever\.co/v0/postings/", re.IGNORECASE),
    ]

    def __init__(self, settings: Any, **kw: Any) -> None:
        """Construct the provider and its in-process lookup memo.

        Args:
            settings: Application settings, supplied by the plugin registry.
            **kw: Extra construction options, kept on ``self.options``.
        """
        super().__init__(settings, **kw)
        self._company_by_job_id: dict[str, str] = {}

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
        from app.cache import (
            NAMESPACES,
            get_cache,
            make_key,
        )

        cache = get_cache()
        key = make_key(NAMESPACES.POSTING, self.provider_name.value, *key_parts)

        async def factory() -> Any:
            return await self._get_json(url, params=params)

        return await cache.get_or_set(key, factory, ttl=ttl)

    async def _page(self, company: str, skip: int, limit: int) -> list[dict[str, Any]]:
        """Return one page of a company's postings.

        Args:
            company: The company token, e.g. ``"veeva"``.
            skip: How many postings to skip — Lever's offset parameter.
            limit: How many postings to return.

        Returns:
            The postings on that page, in feed order.

        Raises:
            PostingUnavailableError: When the company token is unknown — the normal outcome
                for an employer that has migrated ATS or made its board private.
            ProviderError: On any other provider failure.
        """
        payload = await self._cached_json(
            f"{API_ROOT}/{company}",
            params={"mode": "json", "skip": skip, "limit": limit},
            key_parts=(company, "page", skip, limit),
            ttl=BOARD_PAGE_TTL_SECONDS,
        )
        if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
            self.logger.warning("lever.feed_malformed", company=company, skip=skip)
            return []
        return [job for job in payload if isinstance(job, Mapping)]

    def _pager(
        self, company: str
    ) -> Callable[[int, str | None], Awaitable[tuple[Sequence[Mapping[str, Any]], str | None]]]:
        """Build the page callback :meth:`~app.jobs.base.ATSProvider.paginate` drives.

        The callback returns ``None`` as its cursor, which puts ``paginate`` into offset mode:
        the offset advances by the page length and iteration stops on the first short page.
        The page is *ordered* but never filtered, because that exhaustion test counts what the
        callback returns — a callback that dropped non-matching postings would report a short
        page and stop the feed early.

        Args:
            company: The company token to page through.

        Returns:
            A coroutine callable of ``(offset, cursor)``.
        """

        async def fetch(
            offset: int, cursor: str | None
        ) -> tuple[Sequence[Mapping[str, Any]], str | None]:
            """Fetch and order one page; the cursor is unused in offset mode."""
            return _ordered(await self._page(company, offset, PAGE_SIZE)), None

        return fetch

    def _remember(self, job_id: str, company: str) -> None:
        """Record which company a posting id belongs to, for later bare-id lookups.

        Args:
            job_id: The Lever posting identifier.
            company: The company board it was discovered on.
        """
        if not job_id:
            return
        if len(self._company_by_job_id) >= MAX_MEMOIZED_IDS:
            self.logger.debug("lever.id_memo_reset", entries=len(self._company_by_job_id))
            self._company_by_job_id.clear()
        self._company_by_job_id[job_id.lower()] = company

    # -- mapping ------------------------------------------------------------------------

    def _to_raw(self, job: Mapping[str, Any], company: str) -> RawPosting:
        """Convert one Lever posting into the pipeline's currency.

        Args:
            job: The posting payload, exactly as the feed delivered it.
            company: The company token it came from.

        Returns:
            The :class:`~app.jobs.base.RawPosting`, carrying the untouched payload in
            :attr:`~app.jobs.base.RawPosting.raw` so that a parsing fix can be replayed
            without re-crawling.
        """
        job_id = clean_text(job.get("id"))
        title = clean_text(job.get("text"))
        description = _description_of(job)
        locations = _locations_of(job)
        salary_min, salary_max, currency = _salary_of(job)

        url = clean_text(job.get("hostedUrl")) or JOB_URL_TEMPLATE.format(
            company=company, job_id=job_id
        )
        apply_url = clean_text(job.get("applyUrl")) or APPLY_URL_TEMPLATE.format(
            company=company, job_id=job_id
        )

        return RawPosting(
            provider=ATSProviderName.LEVER,
            external_id=job_id,
            url=url,
            title=title,
            company_name=_company_from_token(company),
            description=description or None,
            location=locations[0] if locations else None,
            work_arrangement=_arrangement_of(job, locations, f"{title}\n{description}"),
            employment_type=_employment_type_of(job, title, description),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            posted_at=job.get("createdAt"),
            apply_url=apply_url if apply_url != url else None,
            raw=dict(job),
        )

    # -- filtering ----------------------------------------------------------------------

    def _accepts(self, job: Mapping[str, Any], q: SearchQuery) -> bool:
        """Apply every cheap filter to one posting.

        Deliberately runs before the posting is reassembled into text: the tests here touch a
        title, a location, a timestamp, a structured workplace type and the raw description
        fragments. Those fragments contain a superset of the words
        :func:`_description_of` would produce, so a keyword probe against them never rejects a
        posting the full-text test would have kept — it only avoids reassembling the ones that
        could not possibly match.

        Args:
            job: One posting from the feed.
            q: The caller's query.

        Returns:
            ``True`` when the posting is worth converting.
        """
        if not clean_text(job.get("id")):
            return False
        if not q.matches_freshness(parse_date(job.get("createdAt"))):
            return False

        locations = _locations_of(job)
        if q.locations and not _matches_any(q.locations, locations):
            return False

        title = clean_text(job.get("text"))
        if q.keywords and not _matches_any(q.keywords, _keyword_haystacks(job, title)):
            return False

        if q.remote_only:
            return _may_be_remote(job, title, locations)
        return True

    # -- the provider contract ----------------------------------------------------------

    async def search(self, q: SearchQuery) -> AsyncIterator[RawPosting]:
        """Yield postings from every requested company board, newest first within each page.

        Company tokens come from ``q.extra`` when the caller named any, and from
        :func:`app.jobs.seeds.boards_from_query`'s curated defaults otherwise. They are polled
        one at a time, and **a board that fails degrades that board only** — an employer that
        has migrated ATS or made its board private answers 404, which is expected, is logged,
        and must never abort a discovery run across the others.

        Each board is offset-paginated through :meth:`~app.jobs.base.ATSProvider.paginate`,
        which stops on the first short page. Ordering is newest-first *within a page* rather
        than globally: Lever's feed is not sorted and exposes no sort parameter, and ordering
        globally would mean draining every page of every board before yielding anything —
        defeating both ``q.limit`` and the lazy reassembly that makes this method cheap.

        Args:
            q: What to look for. Every field is honoured client-side; the postings API offers
                no server-side keyword, location or freshness filter.

        Yields:
            One :class:`~app.jobs.base.RawPosting` per matching advertisement, at most
            ``q.limit`` of them.
        """
        companies = boards_from_query(self.provider_name, q.extra)
        if not companies:
            self.logger.warning("lever.no_companies", extra_keys=sorted(q.extra))
            return

        log = self.logger.bind(companies=len(companies), limit=q.limit)
        log.info("lever.search_started")

        yielded = 0
        for company in companies:
            if yielded >= q.limit:
                break

            from_company = 0
            try:
                # ``aclosing`` matters here: breaking out of the loop at ``q.limit`` leaves
                # the pagination generator suspended, and closing it deterministically is
                # what releases the page it is holding rather than waiting for a collection.
                async with aclosing(
                    self.paginate(
                        self._pager(company),
                        page_size=PAGE_SIZE,
                        max_pages=MAX_PAGES_PER_COMPANY,
                    )
                ) as pages:
                    async for job in pages:
                        if not self._accepts(job, q):
                            continue
                        raw = self._to_raw(job, company)
                        if q.remote_only and raw.work_arrangement is not WorkArrangement.REMOTE:
                            continue
                        self._remember(raw.external_id, company)
                        yielded += 1
                        from_company += 1
                        yield raw
                        if yielded >= q.limit:
                            break
            except ProviderError as exc:
                log.warning(
                    "lever.company_failed",
                    company=company,
                    status_code=exc.status_code,
                    transient=exc.transient,
                    error=str(exc),
                )
                continue

            log.debug("lever.company_scanned", company=company, yielded=from_company)

        log.info("lever.search_finished", yielded=yielded)

    async def fetch_posting(self, id_or_url: str) -> RawPosting | None:
        """Fetch one posting by identifier or by URL.

        Every shape a caller can plausibly hold is accepted:

        * a hosted posting URL — ``jobs.lever.co/<company>/<id>``, with or without a trailing
          ``/apply``, and the ``jobs.eu.lever.co`` data-residency host;
        * the API URL itself;
        * the composite ``<company>/<id>``;
        * a bare posting id.

        A bare id carries no company, and Lever has no company-independent endpoint. It is
        therefore resolved from the memo :meth:`search` fills in and, failing that, by
        scanning the first page of up to :data:`MAX_LOOKUP_COMPANIES` boards. Every one of
        those reads goes through the cache, so immediately after a discovery run the scan
        costs no requests at all.

        Args:
            id_or_url: A Lever posting identifier or a posting URL.

        Returns:
            The posting, or ``None`` when *id_or_url* carries no identifier this provider can
            address, or when a scan found no board claiming it.

        Raises:
            PostingUnavailableError: When the company is known and the posting is gone.
            ProviderError: On any other provider failure.
        """
        reference = clean_text(id_or_url)
        if not reference:
            return None

        company, job_id = _parse_reference(reference)
        if not job_id:
            self.logger.debug("lever.reference_unrecognised", reference=reference[:120])
            return None

        if not company:
            company = self._company_by_job_id.get(job_id.lower(), "")
        if not company:
            return await self._scan_for(job_id)

        payload = await self._cached_json(
            f"{API_ROOT}/{company}/{job_id}",
            key_parts=(company, "job", job_id.lower()),
            ttl=POSTING_TTL_SECONDS,
        )
        job = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(job, Mapping) or not job.get("id"):
            self.logger.warning("lever.posting_malformed", company=company, job_id=job_id)
            return None

        raw = self._to_raw(job, company)
        self._remember(raw.external_id, company)
        return raw

    async def _scan_for(self, job_id: str) -> RawPosting | None:
        """Find which company board claims *job_id*.

        Reads the first page of each candidate board rather than draining it: a scan is a
        best-effort convenience for someone who pasted an identifier without its company, and
        exhausting forty full boards for that would not be.

        Args:
            job_id: The posting identifier.

        Returns:
            The posting, or ``None`` when no scanned board's first page lists it.
        """
        companies = boards_from_query(self.provider_name, self.options)[:MAX_LOOKUP_COMPANIES]
        if not companies:
            return None

        wanted = job_id.lower()
        self.logger.info("lever.bare_id_scan_started", job_id=job_id, companies=len(companies))
        for company in companies:
            try:
                jobs = await self._page(company, 0, PAGE_SIZE)
            except ProviderError as exc:
                self.logger.debug("lever.company_failed", company=company, error=str(exc))
                continue
            for job in jobs:
                if clean_text(job.get("id")).lower() == wanted:
                    self._remember(wanted, company)
                    self.logger.info("lever.bare_id_scan_hit", job_id=job_id, company=company)
                    return self._to_raw(job, company)

        self.logger.info("lever.bare_id_scan_missed", job_id=job_id, companies=len(companies))
        return None

    async def apply(self, ctx: ApplyContext) -> ApplyResult:
        """Submit an application through the public Lever form.

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
        """Report whether the Lever postings API is reachable right now.

        Asks :data:`HEALTHCHECK_COMPANY` for a single posting, with one attempt and a short
        timeout, and deliberately bypasses the cache so that the answer describes the network
        rather than the last fifteen minutes.

        Returns:
            ``True`` when the API answered with a postings array. Browser availability is
            logged alongside but does not decide the result: discovery is useful on a machine
            that can never submit, and failing readiness over a missing Playwright install
            would take the whole provider offline for no reason.
        """
        try:
            payload = await self._get_json(
                f"{API_ROOT}/{HEALTHCHECK_COMPANY}",
                params={"mode": "json", "limit": 1},
                timeout=HEALTHCHECK_TIMEOUT_SECONDS,
                max_attempts=1,
            )
        except ProviderError as exc:
            self.logger.warning(
                "lever.healthcheck_failed",
                company=HEALTHCHECK_COMPANY,
                status_code=exc.status_code,
                error=str(exc),
            )
            return False

        healthy = isinstance(payload, list)
        self.logger.debug(
            "lever.healthcheck",
            company=HEALTHCHECK_COMPANY,
            healthy=healthy,
            postings=len(payload) if isinstance(payload, list) else 0,
            browser_available=browser_available(),
        )
        return healthy
