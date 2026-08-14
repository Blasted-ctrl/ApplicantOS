"""Ashby — job discovery **and real automated submission** (``docs/CONTRACTS.md`` §9).

**Automation posture (binding, golden rule #10).** An Ashby job board is a *public
application form*. ``https://jobs.ashbyhq.com/<board>/<uuid>/application`` renders and
accepts the same form for an anonymous visitor as for anyone else: **no account, no login and
no session are required**, which is why :attr:`AshbyProvider.requires_login` is ``False`` and
:attr:`AshbyProvider.supports_auto_apply` is ``True``. Discovery reads the public Job Board
API at ``api.ashbyhq.com/posting-api/job-board/<board>`` — an unauthenticated, documented
endpoint that exists precisely so third parties can republish an employer's open roles — and
every request identifies itself with :data:`app.jobs.base.USER_AGENT`.

Submission goes through :func:`app.jobs._apply.run_browser_apply`, so the kill switch is
unchanged: nothing is submitted unless ``settings.auto_apply_enabled`` is on **and**
``settings.dry_run`` is off **and** the caller did not ask for a dry run (golden rule #3),
and any question that cannot be answered confidently escalates to a human rather than being
guessed (golden rule #2).

**What the feed looks like.** ``GET /posting-api/job-board/<board>?includeCompensation=true``
returns ``{"apiVersion": …, "jobs": [...]}`` — every listed posting for one employer, with no
pagination and no server-side filter of any kind. Ashby is the richest of the three
submission-capable feeds: it states ``employmentType``, ``workplaceType``, ``isRemote``,
``publishedAt``, a primary ``location`` plus ``secondaryLocations``, and — uniquely — a fully
structured ``compensation`` object carrying each component's currency, pay interval and
bounds. Every one of those is used in preference to inferring anything from prose, and the
salary is read from ``summaryComponents`` rather than from a summary string, so a band is
annualised from numbers rather than re-parsed out of ``"$211.4K – $290.6K"``.

Two details are worth stating because they are easy to get wrong. ``isRemote`` is ``true`` on
postings whose ``workplaceType`` is ``Hybrid`` — it means "remote is available", not "this is
a remote role" — so the explicit workplace type wins whenever both are present. And the feed
never names the employer: the board token in the URL is the only identification there is, so
:attr:`~app.jobs.base.RawPosting.company_name` is de-slugged from it and refined later by
company enrichment.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
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
    parse_salary,
)
from app.jobs.base import (
    ApplyContext,
    ApplyResult,
    ATSProvider,
    ProviderError,
    RawPosting,
    SearchQuery,
    fair_share,
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
    "BOARD_FEED_TTL_SECONDS",
    "HEALTHCHECK_BOARD",
    "JOB_URL_TEMPLATE",
    "MAX_LOOKUP_BOARDS",
    "SELECTOR_PACK",
    "AshbyProvider",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Endpoints
# ======================================================================================

#: Root of the public Job Board API. Documented, unauthenticated, and intended for exactly
#: this use — republishing an employer's open roles.
API_ROOT: Final[str] = "https://api.ashbyhq.com/posting-api/job-board"

#: Human-facing posting page, used when a posting omits ``jobUrl``.
JOB_URL_TEMPLATE: Final[str] = "https://jobs.ashbyhq.com/{board}/{job_id}"

#: The application form itself, used when a posting omits ``applyUrl``.
APPLY_URL_TEMPLATE: Final[str] = "https://jobs.ashbyhq.com/{board}/{job_id}/application"

#: Selector pack name handed to the browser layer by :meth:`AshbyProvider.apply`.
SELECTOR_PACK: Final[str] = "ashby"

#: The board probed by :meth:`AshbyProvider.healthcheck`. Large, long-lived, and — being the
#: same board the feed test exercises — a probe whose failure is unambiguous.
HEALTHCHECK_BOARD: Final[str] = "ramp"

#: Seconds allowed for the healthcheck probe. Deliberately short: a readiness endpoint that
#: blocks for thirty seconds is worse than one that reports "not ready".
HEALTHCHECK_TIMEOUT_SECONDS: Final[float] = 10.0


# ======================================================================================
# Caching
# ======================================================================================

#: TTL for a whole board's feed. Shorter than the 30-minute ``jobs.poll_all`` beat interval,
#: so a scheduled discovery run always sees fresh data while repeated lookups inside one run
#: — and a user clicking "discover" twice — are served from cache.
BOARD_FEED_TTL_SECONDS: Final[int] = 900


# ======================================================================================
# Limits
# ======================================================================================

#: Boards scanned when resolving a *bare* posting id whose board is not knowable from the
#: input and was not seen during this process's discovery. Ashby has no single-posting
#: endpoint — ``/job-board/<board>/<id>`` answers 401 — so the board feed is the only way to
#: reach a posting, and every scanned feed goes through the cache.
MAX_LOOKUP_BOARDS: Final[int] = 40

#: Ceiling on the in-process "which board does this posting id belong to" memo. Reached only
#: after roughly twenty thousand distinct postings, at which point the memo is dropped
#: wholesale rather than grown without bound; the next lookup simply re-scans.
MAX_MEMOIZED_IDS: Final[int] = 20_000


# ======================================================================================
# URL shapes
# ======================================================================================

#: Ashby posting identifiers are UUIDs.
_ID_PATTERN: Final[str] = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

#: A posting on the hosted board, optionally followed by ``/application``.
_HOSTED_JOB_RE: Final[re.Pattern[str]] = re.compile(
    r"\bjobs\.ashbyhq\.com/(?P<board>[a-z0-9][a-z0-9._-]*)/(?P<job_id>" + _ID_PATTERN + r")",
    re.IGNORECASE,
)

#: The API URL itself, so a link copied out of a debugging session round-trips.
_API_JOB_RE: Final[re.Pattern[str]] = re.compile(
    r"\bapi\.ashbyhq\.com/posting-api/job-board/(?P<board>[a-z0-9][a-z0-9._-]*)"
    r"[^#]*?[?&]jobPostingId=(?P<job_id>" + _ID_PATTERN + r")",
    re.IGNORECASE,
)

#: A hosted board URL with no posting in it.
_HOSTED_BOARD_RE: Final[re.Pattern[str]] = re.compile(
    r"\bjobs\.ashbyhq\.com/(?P<board>[a-z0-9][a-z0-9._-]*)",
    re.IGNORECASE,
)

#: ``<board>/<id>`` or ``<board>:<id>``, the composite form this provider accepts so that a
#: caller holding both halves never has to build a URL.
_COMPOSITE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<board>[a-z0-9][a-z0-9._-]*)\s*[/:]\s*(?P<job_id>" + _ID_PATTERN + r")$",
    re.IGNORECASE,
)

#: An Ashby posting identifier on its own.
_BARE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^" + _ID_PATTERN + r"$", re.IGNORECASE)


# ======================================================================================
# Payload vocabulary
# ======================================================================================

#: Word separators inside a board token.
_TOKEN_SEPARATOR_RE: Final[re.Pattern[str]] = re.compile(r"[-_\s]+")

#: Everything that is not an ASCII letter, stripped before matching an enumerated value so
#: that ``"FullTime"``, ``"Full-Time"`` and ``"full time"`` all resolve to one key.
_NON_LETTER_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z]")

#: Sort key for a posting whose ``publishedAt`` could not be parsed, so that undated postings
#: sort last under ``reverse=True``. An absent date is not a claim of freshness.
_UNDATED: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)

#: Markers looked for inside an ``employmentType`` value, in decreasing order of specificity;
#: the first that appears wins.
#:
#: Ashby's vocabulary is a controlled enum — ``FullTime``, ``PartTime``, ``Intern``,
#: ``Contract``, ``Temporary`` — so an exact table would work today. Substring matching is
#: used anyway because the cost of a value this table does not know is not a missing field but
#: a *wrong* one: the fall-through hands the decision to a description whose benefits
#: paragraph says "part-time employees are not eligible", and a director's role gets filed as
#: part time.
#:
#: The order matters for the same reason it does in :mod:`app.jobs._parsing`: an internship is
#: also full-time, and a hypothetical "FullTimeIntern" must classify as an internship.
_EMPLOYMENT_MARKERS: Final[tuple[tuple[str, EmploymentType], ...]] = (
    ("internship", EmploymentType.INTERNSHIP),
    ("intern", EmploymentType.INTERNSHIP),
    ("coop", EmploymentType.INTERNSHIP),
    ("apprentice", EmploymentType.INTERNSHIP),
    ("trainee", EmploymentType.INTERNSHIP),
    ("newgrad", EmploymentType.NEW_GRAD),
    ("graduate", EmploymentType.NEW_GRAD),
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
    "inoffice": WorkArrangement.ONSITE,
    "unspecified": WorkArrangement.UNKNOWN,
}

#: The only ``compensationType`` that is a salary. Equity percentages, commission targets and
#: bonus components share the same shape and must never be stored as pay: reporting an equity
#: grant as a salary band would corrupt every downstream comparison.
_SALARY_COMPONENT_TYPE: Final[str] = "salary"

#: Working days in a year, for a day-rate component. Matches :mod:`app.jobs._parsing`'s
#: convention.
_ANNUAL_WORKING_DAYS: Final[int] = 260

#: Weeks in a year, for a weekly component.
_WEEKS_PER_YEAR: Final[int] = 52

#: ``interval`` values mapped onto the multiplier that annualises them. The hourly multiplier
#: comes from :mod:`app.jobs._parsing` so that an Ashby band and a band parsed out of another
#: provider's free text annualise identically — otherwise two postings for the same job,
#: discovered through two providers, would not compare equal.
_INTERVAL_MULTIPLIERS: Final[dict[str, int]] = {
    "1year": 1,
    "6months": 2,
    "3months": 4,
    "1month": 12,
    "1week": _WEEKS_PER_YEAR,
    "1day": _ANNUAL_WORKING_DAYS,
    "1hour": ANNUAL_HOURS,
}

#: The interval assumed when a salary component states none or states an unrecognised one.
#: Annual is how compensation is advertised by default and is the same assumption
#: :func:`app.jobs._parsing.parse_salary` makes.
_DEFAULT_INTERVAL: Final[str] = "1year"


# ======================================================================================
# Payload helpers
# ======================================================================================


def _company_from_token(token: str) -> str:
    """Turn a board token into a presentable employer name.

    The Ashby feed never states the employer's name — the token in the URL is the only
    identification there is — so this is the sole source for
    :attr:`~app.jobs.base.RawPosting.company_name`. Company resolution and enrichment happen
    later in the pipeline; a recognisable name is all that is needed to key on.

    Args:
        token: A board token such as ``"zed-industries"``.

    Returns:
        The de-slugged name (``"Zed Industries"``), or ``""`` for an empty token.
    """
    parts = [part for part in _TOKEN_SEPARATOR_RE.split(clean_text(token)) if part]
    return " ".join(part[:1].upper() + part[1:] for part in parts)


def _enum_key(value: Any) -> str:
    """Reduce an enumerated feed value to the key the lookup tables use.

    Args:
        value: A raw value such as ``"FullTime"``, ``"On-Site"`` or ``"1 YEAR"``.

    Returns:
        The lowercased value with every non-letter removed, or ``""``.
    """
    return _NON_LETTER_RE.sub("", clean_text(value).lower())


def _interval_key(value: Any) -> str:
    """Reduce a compensation ``interval`` to the key :data:`_INTERVAL_MULTIPLIERS` uses.

    Ashby spells intervals ``"1 YEAR"``, ``"1 HOUR"``, ``"NONE"`` and so on, so — unlike
    :func:`_enum_key` — the digits are load-bearing and are kept.

    Args:
        value: The raw ``interval``.

    Returns:
        The lowercased value with whitespace and punctuation removed, e.g. ``"1year"``.
    """
    return re.sub(r"[^a-z0-9]", "", clean_text(value).lower())


def _description_of(job: Mapping[str, Any]) -> str:
    """Return a posting's description as plain text.

    ``descriptionHtml`` is preferred over ``descriptionPlain`` because Ashby's plain variant
    flattens headings into shouted uppercase and drops list structure, while
    :func:`app.jobs._parsing.html_to_text` preserves both — and the structure is what makes a
    description readable in the desktop app and useful to the resume engine.

    Args:
        job: One posting from the feed.

    Returns:
        The description as plain text, or ``""`` when the posting carries none.
    """
    markup = job.get("descriptionHtml")
    if isinstance(markup, str) and markup.strip():
        return html_to_text(markup)
    plain = job.get("descriptionPlain")
    return html_to_text(plain) if isinstance(plain, str) else ""


def _locations_of(job: Mapping[str, Any]) -> list[str]:
    """Collect every place a posting says it is in.

    Args:
        job: One posting from the feed.

    Returns:
        Cleaned, de-duplicated location strings, the primary one first. Ashby states a single
        ``location`` and, on multi-site requisitions, a ``secondaryLocations`` array of
        objects each carrying its own ``location`` string.
    """
    names: list[str] = []
    seen: set[str] = set()

    def add(candidate: Any) -> None:
        """Append one cleaned, previously unseen location."""
        name = clean_text(candidate)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)

    add(job.get("location"))

    secondary = job.get("secondaryLocations")
    if isinstance(secondary, Sequence) and not isinstance(secondary, (str, bytes)):
        for entry in secondary:
            if isinstance(entry, Mapping):
                add(entry.get("location"))
            else:
                add(entry)
    return names


def _employment_marker_type(value: Any) -> EmploymentType | None:
    """Map an ``employmentType`` value onto an employment type.

    Args:
        value: The raw field, e.g. ``"FullTime"``.

    Returns:
        The first :data:`_EMPLOYMENT_MARKERS` entry whose marker appears in the value, or
        ``None`` when the value says nothing recognisable — in which case the caller falls
        back to inference rather than guessing.
    """
    reduced = _enum_key(value)
    if not reduced:
        return None
    for marker, employment_type in _EMPLOYMENT_MARKERS:
        if marker in reduced:
            return employment_type
    logger.debug("ashby.employment_type_unrecognised", employment_type=str(value)[:60])
    return None


def _employment_type_of(job: Mapping[str, Any], title: str, description: str) -> EmploymentType:
    """Determine what kind of engagement a posting advertises.

    ``employmentType`` is the employer's own structured statement and is trusted first — with
    one deliberate exception. An internship *is* full-time, and a board that files "Software
    Engineering Intern, Summer 2026" under ``FullTime`` is not wrong so much as less specific
    than its own title; reporting it as full-time would lose exactly the information a user
    filtering for internships cares about. So a title that unambiguously says internship or
    new grad wins over a generic full- or part-time value, and nothing else does.

    Args:
        job: One posting from the feed.
        title: The cleaned role title.
        description: The description as plain text.

    Returns:
        The advertised type, or :attr:`~app.models.enums.EmploymentType.UNKNOWN` when neither
        the structured field nor the text says.
    """
    stated = _employment_marker_type(job.get("employmentType"))

    if stated is None or stated in (EmploymentType.FULL_TIME, EmploymentType.PART_TIME):
        from_title = infer_employment_type(title)
        if from_title in (EmploymentType.INTERNSHIP, EmploymentType.NEW_GRAD):
            return from_title

    if stated is not None:
        return stated
    return infer_employment_type(title, description)


def _arrangement_of(job: Mapping[str, Any], locations: Sequence[str], text: str) -> WorkArrangement:
    """Determine where a role is performed.

    ``workplaceType`` is Ashby's structured statement and is trusted outright. ``isRemote`` is
    consulted only when it is absent, because the two disagree by design: a hybrid posting
    that *offers* remote days carries ``isRemote: true`` alongside ``workplaceType:
    "Hybrid"``, and reading ``isRemote`` first would file every such role as fully remote.
    Only when both are silent does the location string get a vote, and only when that is
    silent too is the description consulted.

    Args:
        job: One posting from the feed.
        locations: Every location the posting states.
        text: The title and description, joined.

    Returns:
        The arrangement, or :attr:`~app.models.enums.WorkArrangement.UNKNOWN` when nothing
        says. ``UNKNOWN`` is honest and never a guess; the scorer handles it explicitly.
    """
    stated = _WORKPLACE_ARRANGEMENTS.get(_enum_key(job.get("workplaceType")))
    if stated is not None and stated is not WorkArrangement.UNKNOWN:
        return stated

    if job.get("isRemote") is True:
        return WorkArrangement.REMOTE

    for location in locations:
        inferred = infer_arrangement(location)
        if inferred is not WorkArrangement.UNKNOWN:
            return inferred
    return infer_arrangement(text)


def _annualize(value: Any, multiplier: int) -> int | None:
    """Annualise one bound of a salary component, discarding implausible figures.

    Args:
        value: The raw ``minValue`` or ``maxValue``.
        multiplier: Pay periods per year for the component's interval.

    Returns:
        The annualised amount, or ``None`` when the bound is missing, non-numeric, or outside
        :data:`~app.jobs._parsing.MIN_PLAUSIBLE_ANNUAL_SALARY` …
        :data:`~app.jobs._parsing.MAX_PLAUSIBLE_ANNUAL_SALARY`.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    annual = round(float(value) * multiplier)
    if MIN_PLAUSIBLE_ANNUAL_SALARY <= annual <= MAX_PLAUSIBLE_ANNUAL_SALARY:
        return annual
    return None


def _salary_components(compensation: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return every structured component of a posting's compensation package.

    ``summaryComponents`` is the flattened view Ashby computes across tiers and is used when
    present; the per-tier ``components`` are walked otherwise, so a posting that publishes
    tiers without a summary still yields its numbers.

    Args:
        compensation: The posting's ``compensation`` object.

    Returns:
        The component mappings, in feed order.
    """
    summary = compensation.get("summaryComponents")
    if isinstance(summary, Sequence) and not isinstance(summary, (str, bytes)):
        components = [entry for entry in summary if isinstance(entry, Mapping)]
        if components:
            return components

    collected: list[Mapping[str, Any]] = []
    tiers = compensation.get("compensationTiers")
    if isinstance(tiers, Sequence) and not isinstance(tiers, (str, bytes)):
        for tier in tiers:
            if not isinstance(tier, Mapping):
                continue
            entries = tier.get("components")
            if isinstance(entries, Sequence) and not isinstance(entries, (str, bytes)):
                collected.extend(entry for entry in entries if isinstance(entry, Mapping))
    return collected


def _salary_of(job: Mapping[str, Any]) -> tuple[int | None, int | None, str | None]:
    """Read the advertised compensation band from the structured ``compensation`` object.

    Only components whose ``compensationType`` is ``Salary`` are considered — equity
    percentages, commission targets and bonus components share the same shape, and storing one
    as pay would corrupt every downstream comparison. Each surviving component is annualised
    from its own ``interval`` using the same constants as
    :func:`app.jobs._parsing.parse_salary`, and the widest resulting band is returned so that
    a posting quoting several geographic tiers reports the full range it advertises.

    When the object carries no usable numbers, the human-readable summary string Ashby
    computes (``"$211.4K - $290.6K"``) is parsed as a fallback. That string is purpose-built
    for compensation and short, so parsing it carries none of the risk of mining a
    description.

    Args:
        job: One posting from the feed.

    Returns:
        ``(minimum, maximum, currency)``, any element of which may be ``None``.
    """
    compensation = job.get("compensation")
    if not isinstance(compensation, Mapping):
        return (None, None, None)

    minima: list[int] = []
    maxima: list[int] = []
    currency: str | None = None

    for component in _salary_components(compensation):
        if _enum_key(component.get("compensationType")) != _SALARY_COMPONENT_TYPE:
            continue
        interval = _interval_key(component.get("interval"))
        multiplier = _INTERVAL_MULTIPLIERS.get(interval)
        if multiplier is None:
            if interval and interval != "none":
                logger.debug("ashby.salary_interval_unknown", interval=interval)
            multiplier = _INTERVAL_MULTIPLIERS[_DEFAULT_INTERVAL]

        low = _annualize(component.get("minValue"), multiplier)
        high = _annualize(component.get("maxValue"), multiplier)
        if low is None and high is None:
            continue
        if low is not None:
            minima.append(low)
        if high is not None:
            maxima.append(high)
        currency = currency or (clean_text(component.get("currencyCode")) or None)

    if minima or maxima:
        return (min(minima) if minima else None, max(maxima) if maxima else None, currency)

    for key in ("scrapeableCompensationSalarySummary", "compensationTierSummary"):
        summary = compensation.get(key)
        if isinstance(summary, str) and summary.strip():
            low, high, parsed_currency = parse_salary(summary)
            if low is not None or high is not None:
                return (low, high, parsed_currency or currency)
    return (None, None, currency)


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


def _keyword_haystacks(job: Mapping[str, Any], title: str) -> list[str]:
    """Return the strings a keyword filter is matched against.

    Args:
        job: One posting from the feed.
        title: The cleaned role title.

    Returns:
        The title, the department and team labels, and the raw description in whichever form
        the feed carries it. Matching the raw markup — rather than the converted text — is
        what keeps the filter cheap; it contains a superset of the readable words.
    """
    haystacks = [title]
    for field_name in ("department", "team", "descriptionPlain", "descriptionHtml"):
        value = job.get(field_name)
        if isinstance(value, str) and value:
            haystacks.append(value)
    return haystacks


def _may_be_remote(job: Mapping[str, Any], title: str, locations: Sequence[str]) -> bool:
    """Return whether a posting could plausibly be remote, without converting it.

    A deliberate over-approximation used only to skip work. It answers "no" only when Ashby's
    structured ``workplaceType`` positively says hybrid or onsite *and* ``isRemote`` is not
    set, and otherwise accepts anything whose title or locations mention remote work at all;
    the strict test — :func:`_arrangement_of` — runs afterwards in
    :meth:`AshbyProvider.search`.

    Args:
        job: One posting from the feed.
        title: The cleaned role title.
        locations: Every location the posting states.

    Returns:
        ``True`` when the posting is worth converting and testing properly.
    """
    stated = _WORKPLACE_ARRANGEMENTS.get(_enum_key(job.get("workplaceType")))
    if stated is WorkArrangement.REMOTE or job.get("isRemote") is True:
        return True
    if stated in (WorkArrangement.HYBRID, WorkArrangement.ONSITE):
        return False
    if "remote" in title.lower():
        return True
    return any("remote" in place.lower() for place in locations)


def _parse_reference(reference: str) -> tuple[str, str]:
    """Split a caller-supplied reference into ``(board, job_id)``.

    Args:
        reference: A cleaned identifier or URL.

    Returns:
        The board token (``""`` when the reference does not carry one) and the posting
        identifier (``""`` when the reference carries none at all).
    """
    for pattern in (_HOSTED_JOB_RE, _API_JOB_RE):
        match = pattern.search(reference)
        if match:
            return match.group("board"), match.group("job_id")

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


@plugin
class AshbyProvider(ATSProvider):
    """Discovery and automated submission against Ashby job boards.

    One instance per process (providers are plugin-registry singletons), so the HTTP
    connection pool and the posting-id memo are shared across every discovery run.

    Class attributes:
        meta: Plugin identity, registered under the name ``"ashby"``.
        name: :attr:`~app.models.enums.ATSProviderName.ASHBY`.
        supports_auto_apply: ``True`` — the application form is public and account-free.
        requires_login: ``False`` — neither discovery nor submission needs a session.
        URL_PATTERNS: Every shape an Ashby posting URL takes in the wild.
    """

    meta: ClassVar[PluginMeta] = PluginMeta(
        kind=PluginKind.PROVIDER,
        name=ATSProviderName.ASHBY.value,
        version="1.0.0",
        display_name="Ashby",
        description=(
            "Ashby job boards — public JSON feed with structured compensation for "
            "discovery, public application form for submission. No account required."
        ),
        author="ApplicantOS",
        capabilities=frozenset({"search", "fetch", "auto_apply"}),
    )
    name: ClassVar[ATSProviderName] = ATSProviderName.ASHBY
    supports_auto_apply: ClassVar[bool] = True
    requires_login: ClassVar[bool] = False
    URL_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"\bjobs\.ashbyhq\.com/", re.IGNORECASE),
        re.compile(r"\bapi\.ashbyhq\.com/posting-api/", re.IGNORECASE),
        re.compile(r"\bashbyhq\.com/embed/", re.IGNORECASE),
    ]

    def __init__(self, settings: Any, **kw: Any) -> None:
        """Construct the provider and its in-process lookup memo.

        Args:
            settings: Application settings, supplied by the plugin registry.
            **kw: Extra construction options, kept on ``self.options``.
        """
        super().__init__(settings, **kw)
        self._board_by_job_id: dict[str, str] = {}

    # -- transport ----------------------------------------------------------------------

    async def _board_jobs(self, board: str) -> list[Mapping[str, Any]]:
        """Return every listed posting on one board, compensation included.

        Reads through the shared cache under
        :attr:`~app.cache.keys.NAMESPACES.POSTING` (golden rule #9; ``docs/CONTRACTS.md``
        §7). The cache is imported lazily because it pulls in the settings and, potentially, a
        Redis client, and a provider that is registered but never polled should pay for
        neither.

        Postings with ``isListed`` explicitly ``false`` are dropped: Ashby uses that flag for
        roles that exist but are deliberately not advertised, and applying to one would be
        applying to something the employer did not publish.

        Args:
            board: The board token, e.g. ``"ramp"``.

        Returns:
            The listed postings, in the order Ashby returned them.

        Raises:
            PostingUnavailableError: When the board token is unknown — the normal outcome for
                an employer that has migrated ATS or renamed its board.
            ProviderError: On any other provider failure.
        """
        from app.cache import (
            NAMESPACES,
            get_cache,
            make_key,
        )

        url = f"{API_ROOT}/{board}"
        params = {"includeCompensation": "true"}
        cache = get_cache()
        key = make_key(NAMESPACES.POSTING, self.provider_name.value, board, "jobs", "comp")

        async def factory() -> Any:
            return await self._get_json(url, params=params)

        payload = await cache.get_or_set(key, factory, ttl=BOARD_FEED_TTL_SECONDS)
        jobs = payload.get("jobs") if isinstance(payload, Mapping) else None
        if not isinstance(jobs, Sequence) or isinstance(jobs, (str, bytes)):
            self.logger.warning("ashby.board_feed_malformed", board=board)
            return []
        return [
            job for job in jobs if isinstance(job, Mapping) and job.get("isListed") is not False
        ]

    def _remember(self, job_id: str, board: str) -> None:
        """Record which board a posting id belongs to, for later bare-id lookups.

        Args:
            job_id: The Ashby posting identifier.
            board: The board it was discovered on.
        """
        if not job_id:
            return
        if len(self._board_by_job_id) >= MAX_MEMOIZED_IDS:
            self.logger.debug("ashby.id_memo_reset", entries=len(self._board_by_job_id))
            self._board_by_job_id.clear()
        self._board_by_job_id[job_id.lower()] = board

    # -- mapping ------------------------------------------------------------------------

    def _to_raw(self, job: Mapping[str, Any], board: str) -> RawPosting:
        """Convert one Ashby posting into the pipeline's currency.

        Args:
            job: The posting payload, exactly as the feed delivered it.
            board: The board token it came from.

        Returns:
            The :class:`~app.jobs.base.RawPosting`, carrying the untouched payload in
            :attr:`~app.jobs.base.RawPosting.raw` so that a parsing fix can be replayed
            without re-crawling.
        """
        job_id = clean_text(job.get("id"))
        title = clean_text(job.get("title"))
        description = _description_of(job)
        locations = _locations_of(job)
        salary_min, salary_max, currency = _salary_of(job)

        url = clean_text(job.get("jobUrl")) or JOB_URL_TEMPLATE.format(board=board, job_id=job_id)
        apply_url = clean_text(job.get("applyUrl")) or APPLY_URL_TEMPLATE.format(
            board=board, job_id=job_id
        )

        return RawPosting(
            provider=ATSProviderName.ASHBY,
            external_id=job_id,
            url=url,
            title=title,
            company_name=_company_from_token(board),
            description=description or None,
            location=locations[0] if locations else None,
            work_arrangement=_arrangement_of(job, locations, f"{title}\n{description}"),
            employment_type=_employment_type_of(job, title, description),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            posted_at=job.get("publishedAt") or job.get("updatedAt"),
            apply_url=apply_url if apply_url != url else None,
            raw=dict(job),
        )

    # -- filtering ----------------------------------------------------------------------

    def _candidates(
        self, jobs: Sequence[Mapping[str, Any]], q: SearchQuery
    ) -> list[tuple[Mapping[str, Any], datetime | None]]:
        """Apply every cheap filter to a board's feed and order what survives.

        Deliberately runs before any description is converted to text: the tests here touch a
        title, a location, a timestamp, two structured remote flags, and — for keywords — the
        raw description markup, which contains a superset of the words the converted text
        would. A superset probe can only skip work, never drop a posting the full test would
        have kept.

        Args:
            jobs: Every listed posting on one board.
            q: The caller's query.

        Returns:
            ``(job, published_at)`` pairs that passed, newest first. Postings with no usable
            timestamp sort last, because an unknown date is not a claim of freshness.
        """
        survivors: list[tuple[Mapping[str, Any], datetime | None]] = []

        for job in jobs:
            if not clean_text(job.get("id")):
                continue

            published_at = parse_date(job.get("publishedAt") or job.get("updatedAt"))
            if not q.matches_freshness(published_at):
                continue

            locations = _locations_of(job)
            if q.locations and not _matches_any(q.locations, locations):
                continue

            title = clean_text(job.get("title"))
            if q.keywords and not _matches_any(q.keywords, _keyword_haystacks(job, title)):
                continue

            if q.remote_only and not _may_be_remote(job, title, locations):
                continue

            survivors.append((job, published_at))

        survivors.sort(key=lambda pair: pair[1] or _UNDATED, reverse=True)
        return survivors

    # -- the provider contract ----------------------------------------------------------

    async def search(self, q: SearchQuery) -> AsyncIterator[RawPosting]:
        """Yield postings from every requested board, newest first within each board.

        Board tokens come from ``q.extra`` when the caller named any, and from
        :func:`app.jobs.seeds.boards_from_query`'s curated defaults otherwise. They are polled
        one at a time, and **a board that fails degrades that board only** — an employer that
        has migrated ATS answers 404, which is expected, is logged, and must never abort a
        discovery run across the others.

        Ordering is newest-first *within* a board rather than globally: a global ordering
        would require buffering every board's feed before yielding anything, defeating both
        the caller's ``limit`` and the lazy conversion that makes this method cheap.

        Args:
            q: What to look for. Every field is honoured client-side; the Job Board API offers
                no server-side filter at all.

        Yields:
            One :class:`~app.jobs.base.RawPosting` per matching advertisement, at most
            ``q.limit`` of them.
        """
        boards = boards_from_query(self.provider_name, q.extra)
        if not boards:
            self.logger.warning("ashby.no_boards", extra_keys=sorted(q.extra))
            return

        share = fair_share(q.limit, len(boards))
        log = self.logger.bind(boards=len(boards), limit=q.limit, fair_share=share)
        log.info("ashby.search_started")

        # Two passes: fair share first so every board is reached, then uncapped to spend
        # what is left. See `app.jobs.base.fair_share` for why a single shared budget
        # starves the tail of the board list. Board feeds are cached, so the second pass
        # re-reads them rather than re-fetching.
        yielded = 0
        seen: set[str] = set()
        for cap in (share, q.limit):
            if yielded >= q.limit:
                break
            for board in boards:
                if yielded >= q.limit:
                    break

                try:
                    jobs = await self._board_jobs(board)
                except ProviderError as exc:
                    log.warning(
                        "ashby.board_failed",
                        board=board,
                        status_code=exc.status_code,
                        transient=exc.transient,
                        error=str(exc),
                    )
                    continue

                candidates = self._candidates(jobs, q)
                log.debug(
                    "ashby.board_scanned",
                    board=board,
                    postings=len(jobs),
                    candidates=len(candidates),
                )

                from_board = 0
                for job, _published_at in candidates:
                    if yielded >= q.limit or from_board >= cap:
                        break
                    raw = self._to_raw(job, board)
                    if q.remote_only and raw.work_arrangement is not WorkArrangement.REMOTE:
                        continue
                    if raw.external_id in seen:  # already yielded on the first pass
                        continue
                    seen.add(raw.external_id)
                    self._remember(raw.external_id, board)
                    from_board += 1
                    yielded += 1
                    yield raw

        log.info("ashby.search_finished", yielded=yielded)

    async def fetch_posting(self, id_or_url: str) -> RawPosting | None:
        """Fetch one posting by identifier or by URL.

        Every shape a caller can plausibly hold is accepted:

        * a hosted posting URL — ``jobs.ashbyhq.com/<board>/<uuid>``, with or without a
          trailing ``/application``;
        * the API URL carrying ``jobPostingId=<uuid>``;
        * the composite ``<board>/<uuid>``;
        * a bare posting id.

        Ashby publishes **no single-posting endpoint** — ``/job-board/<board>/<uuid>`` answers
        401 — so a posting is always located inside its board's feed. That feed is cached, so
        fetching several postings from one board costs one request in total.

        A bare id carries no board. It is resolved from the memo :meth:`search` fills in and,
        failing that, by scanning up to :data:`MAX_LOOKUP_BOARDS` board feeds; every one of
        those reads goes through the cache, so immediately after a discovery run the scan
        costs no requests at all.

        Args:
            id_or_url: An Ashby posting identifier or a posting URL.

        Returns:
            The posting, or ``None`` when *id_or_url* carries no identifier this provider can
            address, when the board no longer lists the posting, or when a scan found no board
            claiming it.

        Raises:
            PostingUnavailableError: When the board itself is gone.
            ProviderError: On any other provider failure.
        """
        reference = clean_text(id_or_url)
        if not reference:
            return None

        board, job_id = _parse_reference(reference)
        if not job_id:
            self.logger.debug("ashby.reference_unrecognised", reference=reference[:120])
            return None

        if not board:
            board = self._board_by_job_id.get(job_id.lower(), "")
        if not board:
            return await self._scan_for(job_id)

        found = await self._find_on_board(board, job_id)
        if found is None:
            self.logger.info("ashby.posting_not_listed", board=board, job_id=job_id)
            return None
        return found

    async def _find_on_board(self, board: str, job_id: str) -> RawPosting | None:
        """Locate one posting inside a board's feed.

        Args:
            board: The board token.
            job_id: The posting identifier.

        Returns:
            The posting, or ``None`` when the board no longer lists it.

        Raises:
            PostingUnavailableError: When the board itself is gone.
            ProviderError: On any other provider failure.
        """
        wanted = job_id.lower()
        for job in await self._board_jobs(board):
            if clean_text(job.get("id")).lower() == wanted:
                self._remember(wanted, board)
                return self._to_raw(job, board)
        return None

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

        self.logger.info("ashby.bare_id_scan_started", job_id=job_id, boards=len(boards))
        for board in boards:
            try:
                found = await self._find_on_board(board, job_id)
            except ProviderError as exc:
                self.logger.debug("ashby.board_failed", board=board, error=str(exc))
                continue
            if found is not None:
                self.logger.info("ashby.bare_id_scan_hit", job_id=job_id, board=board)
                return found

        self.logger.info("ashby.bare_id_scan_missed", job_id=job_id, boards=len(boards))
        return None

    async def apply(self, ctx: ApplyContext) -> ApplyResult:
        """Submit an application through the public Ashby form.

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
        """Report whether the Ashby Job Board API is reachable right now.

        Probes :data:`HEALTHCHECK_BOARD` without ``includeCompensation``, with a single
        attempt and a short timeout, and deliberately bypasses the cache so that the answer
        describes the network rather than the last fifteen minutes.

        Returns:
            ``True`` when the API answered with a jobs array. Browser availability is logged
            alongside but does not decide the result: discovery is useful on a machine that
            can never submit, and failing readiness over a missing Playwright install would
            take the whole provider offline for no reason.
        """
        try:
            payload = await self._get_json(
                f"{API_ROOT}/{HEALTHCHECK_BOARD}",
                timeout=HEALTHCHECK_TIMEOUT_SECONDS,
                max_attempts=1,
            )
        except ProviderError as exc:
            self.logger.warning(
                "ashby.healthcheck_failed",
                board=HEALTHCHECK_BOARD,
                status_code=exc.status_code,
                error=str(exc),
            )
            return False

        jobs = payload.get("jobs") if isinstance(payload, Mapping) else None
        healthy = isinstance(jobs, Sequence) and not isinstance(jobs, (str, bytes))
        self.logger.debug(
            "ashby.healthcheck",
            board=HEALTHCHECK_BOARD,
            healthy=healthy,
            postings=len(jobs) if healthy and jobs is not None else 0,
            browser_available=browser_available(),
        )
        return healthy
