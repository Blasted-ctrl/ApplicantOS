"""Deduplication — what stops the same job being processed, scored, or applied to twice.

The same opening reaches ApplicantOS through several doors. A company posts to Greenhouse;
the posting is syndicated to LinkedIn; an aggregator republishes it with UTM parameters
bolted on; the next poll re-fetches all three. Without the primitives here, the user would
see one role four times, pay for four scoring runs and four tailored resumes, and — worst of
all — the "never apply twice" guarantee would rest on nothing but a URL string comparison.

Two questions are answered here, and keeping them apart is the whole design:

**"Is this the same posting?"** — :func:`dedupe_key`, backed by
``UNIQUE(job_postings.dedupe_key)``. A *hard* identity claim. Because the database enforces
it, a wrong answer here is unrecoverable: two genuinely different openings that collapse onto
one key mean the user is never shown one of them, and never finds out. The key is therefore
deliberately conservative.

**"Are these probably the same posting?"** — :func:`similarity` and :func:`is_duplicate`. A
*soft* judgement used by :class:`app.services.dedupe_service` to merge across providers, to
suggest merges to a human, and to suppress near-duplicates in the feed. A wrong answer here
is visible and reversible.

**"Has this posting's text changed?"** — :func:`content_hash`, which is a different question
again. It has nothing to do with identity: it decides whether a cached score or a cached
tailored resume is still valid (golden rule #9).

Every function is pure, synchronous, deterministic and stable across processes — nothing here
uses :func:`hash`, which is salted per interpreter and would silently change every key on
restart.

Note:
    :func:`normalize_company` is kept in step with
    :meth:`app.models.company.Company.normalize` deliberately, so that a posting's dedupe key
    and its employer's ``normalized_name`` agree. This module cannot import that class —
    ``app/jobs`` never imports ORM classes (``docs/CONTRACTS.md`` §9) — so the vocabulary is
    restated in :data:`LEGAL_SUFFIXES`, which is a superset: it adds the ``bv``/``ab``/``oy``/
    ``corporation`` forms named in this module's brief.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import TYPE_CHECKING, Any, Final, Literal
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

import structlog

from app.jobs._parsing import clean_text

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.jobs.base import RawPosting

__all__ = [
    "DEFAULT_SIMILARITY_THRESHOLD",
    "LEGAL_SUFFIXES",
    "TRACKING_PARAMS",
    "TRACKING_PARAM_PREFIXES",
    "canonical_url",
    "content_hash",
    "dedupe_key",
    "is_duplicate",
    "normalize_company",
    "normalize_location",
    "normalize_title",
    "similarity",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Shared normalisation vocabulary
# ======================================================================================

#: Unicode normal form applied before ASCII folding, so that ``"Nestlé"`` and ``"Nestle"``
#: produce one key rather than two employer rows.
_UNICODE_NORMAL_FORM: Final[Literal["NFKD"]] = "NFKD"

#: Everything that is not an ASCII letter or digit is a separator. Runs collapse, so
#: ``"Acme,   Inc."`` and ``"Acme - Inc"`` normalise identically.
_SEPARATOR_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

#: ``&`` is expanded rather than deleted, so ``"AT&T"`` and ``"AT and T"`` agree instead of
#: becoming ``"att"`` and ``"at and t"``.
_AMPERSAND_EXPANSION: Final[str] = " and "

#: Field separator inside a composite dedupe key, before hashing. A character that cannot
#: appear in any normalised component, so ``("ab", "c")`` and ``("a", "bc")`` cannot collide.
_KEY_SEPARATOR: Final[str] = "|"


def _fold_ascii(value: str) -> str:
    """Decompose accents and drop everything that is not ASCII.

    Args:
        value: Any text.

    Returns:
        The ASCII-folded form: ``"Zürich"`` becomes ``"Zurich"``, ``"東京"`` becomes ``""``.
    """
    decomposed = unicodedata.normalize(_UNICODE_NORMAL_FORM, value)
    return decomposed.encode("ascii", "ignore").decode("ascii")


def _tokenize(value: str) -> list[str]:
    """Reduce text to lowercase alphanumeric tokens.

    Args:
        value: Any text, already cleaned.

    Returns:
        The tokens, in order, with punctuation and case removed.
    """
    folded = _fold_ascii(value).lower().replace("&", _AMPERSAND_EXPANSION)
    return _SEPARATOR_RE.sub(" ", folded).split()


def _sha256(value: str) -> str:
    """Return the hexadecimal SHA-256 digest of *value*.

    :mod:`hashlib` rather than :func:`hash`: the built-in is salted per process, so a key
    derived from it would change on every restart and every ``UNIQUE(dedupe_key)`` row would
    be orphaned.

    Args:
        value: The string to digest.

    Returns:
        A 64-character lowercase hexadecimal digest.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ======================================================================================
# URL canonicalisation
# ======================================================================================

#: Query parameters stripped by :func:`canonical_url`. Analytics and referral tags: they
#: change on every share of a link without changing which job it points at, so leaving them
#: in would make one posting look like a dozen. Matched case-insensitively.
TRACKING_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "fbclid",
        "gclid",
        "gh_jid",
        "gh_src",
        "lever-origin",
        "lever-source",
        "recommendedflavor",
        "ref",
        "source",
        "src",
        "trackingid",
        "trk",
    }
)

#: Parameter *prefixes* stripped by :func:`canonical_url`, covering the whole ``utm_*``
#: family (``utm_source``, ``utm_medium``, ``utm_campaign``, ``utm_term``, ``utm_content``,
#: and every vendor extension of it) without enumerating it.
TRACKING_PARAM_PREFIXES: Final[tuple[str, ...]] = ("utm_",)

#: Default ports removed from the authority, since ``https://x:443/y`` and ``https://x/y``
#: are the same resource.
_DEFAULT_PORTS: Final[dict[str, str]] = {"http": "80", "https": "443"}

#: Scheme assumed when a URL arrives without one, which is how they arrive from HTML
#: attributes and from users pasting into the desktop app.
_DEFAULT_SCHEME: Final[str] = "https"

#: Matches a leading ``host.tld`` with no scheme and no leading slash.
_SCHEMELESS_RE: Final[re.Pattern[str]] = re.compile(
    r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?::\d+)?(?:[/?#]|$)", re.IGNORECASE
)


def _is_tracking_param(name: str) -> bool:
    """Return whether a query parameter is pure tracking noise.

    Args:
        name: The parameter name, in any case.

    Returns:
        ``True`` when the name is listed in :data:`TRACKING_PARAMS` or begins with one of
        :data:`TRACKING_PARAM_PREFIXES`.
    """
    lowered = name.strip().lower()
    if lowered in TRACKING_PARAMS:
        return True
    return any(lowered.startswith(prefix) for prefix in TRACKING_PARAM_PREFIXES)


def canonical_url(url: Any) -> str:
    """Reduce a posting URL to a stable, comparable form.

    The transformation is: lowercase the scheme and host, drop a default port and a trailing
    dot on the host, drop the fragment, drop every tracking parameter, and drop a trailing
    slash from the path (except on the root, where it is the path). Remaining query
    parameters keep their original order and case, because for many boards a parameter *is*
    the posting identifier and reordering would produce a URL that no longer resolves.

    Args:
        url: A URL from a feed, an HTML attribute, or a user paste. Non-strings are
            stringified; ``None`` and blanks yield ``""``.

    Returns:
        The canonical form, or the cleaned input unchanged when it cannot be parsed as a URL.

    Warning:
        ``gh_jid`` is on the strip list because it is used as a referral tag on shared
        Greenhouse links. On *embedded* Greenhouse boards it is instead the job identifier,
        so a provider must take its ``external_id`` and ``apply_url`` from the feed payload
        and never re-derive them from a canonicalised URL. Identity does not depend on this
        function: :func:`dedupe_key` prefers ``provider`` + ``external_id``.

    Example:
        >>> canonical_url("HTTPS://Boards.Greenhouse.IO/acme/jobs/4012?utm_source=x&gh_src=y#top")
        'https://boards.greenhouse.io/acme/jobs/4012'
    """
    text = clean_text(url)
    if not text:
        return ""

    candidate = text
    if "://" not in candidate and _SCHEMELESS_RE.match(candidate):
        candidate = f"{_DEFAULT_SCHEME}://{candidate}"

    try:
        parts = urlsplit(candidate)
    except ValueError:
        logger.debug("dedupe.url_unparseable", url=text[:120])
        return text

    if not parts.netloc:
        return text

    scheme = parts.scheme.lower() or _DEFAULT_SCHEME

    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return text
    authority = host
    if parts.port is not None and str(parts.port) != _DEFAULT_PORTS.get(scheme):
        authority = f"{host}:{parts.port}"
    # Credentials embedded in a URL are not part of a posting's identity, and echoing them
    # into a stored key or a log line would be a secret leak (golden rule #4).
    if parts.username:
        logger.debug("dedupe.url_credentials_stripped", host=host)

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    kept = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(name)
    ]
    query = urlencode(kept, doseq=False)

    return urlunsplit(SplitResult(scheme, authority, path, query, ""))


# ======================================================================================
# Company, title and location normalisation
# ======================================================================================

#: Legal-form tokens stripped from the *end* of a company name. A superset of
#: :data:`app.models.company.COMPANY_LEGAL_SUFFIXES` — it adds ``corporation`` and the
#: Dutch/Swedish/Finnish forms ``bv``, ``ab`` and ``oy`` — so every key that model produces
#: is also produced here, and a handful of European employers additionally collapse.
LEGAL_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        "ab",
        "ag",
        "bv",
        "co",
        "corp",
        "corporation",
        "gmbh",
        "inc",
        "incorporated",
        "limited",
        "llc",
        "ltd",
        "oy",
        "plc",
        "pte",
        "pvt",
        "sa",
    }
)


def normalize_company(name: Any) -> str:
    """Collapse an employer name to its deduplication key.

    ATS feeds spell one employer a dozen ways — ``"Acme, Inc."``, ``"ACME Inc"``,
    ``"Acme Incorporated"``, ``"Acme"`` — and each spelling would otherwise become its own
    company row, fragmenting the block list, the analytics and the dedupe key.

    The transformation is Unicode decomposition and ASCII folding, case folding, ``&``
    expansion, replacement of every non-alphanumeric run with a single space, and repeated
    removal of trailing tokens listed in :data:`LEGAL_SUFFIXES`. Suffixes are only ever
    stripped from the end, and never the final remaining token, so ``"Co-operative Bank
    Ltd"`` keeps its leading ``co`` and a company genuinely named ``"Limited"`` does not
    normalise to nothing.

    Args:
        name: A raw employer name. Non-strings are stringified; ``None`` yields ``""``.

    Returns:
        The normalisation key, or ``""`` when the name carries no ASCII alphanumerics.

    Example:
        >>> normalize_company("Acme, Inc.")
        'acme'
        >>> normalize_company("AT&T Corporation")
        'at and t'
    """
    cleaned = clean_text(name)
    if not cleaned:
        return ""
    tokens = _tokenize(cleaned)
    while len(tokens) > 1 and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


#: Standalone requisition identifiers: ``#4821``, ``REQ-10233``, ``JR 99812``, ``ID12345``.
#: These change between syndications of the same role and must not reach the key.
_REQ_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:#\s*\d{2,}"
    r"|\b(?:req(?:uisition)?|job|jr|posting|position|vacancy|id|ref)\s*(?:id\s*)?[-_#:]?\s*\d{2,}\b"
    r"|\b[A-Z]{1,4}[-_]\d{3,}\b)",
    re.IGNORECASE,
)

#: Anything wrapped in round or square brackets. Job titles use them for requisition ids,
#: office names, contract flags and duplicated seniority — never for the role itself.
_BRACKETED_RE: Final[re.Pattern[str]] = re.compile(r"\([^()]*\)|\[[^\[\]]*\]|\{[^{}]*\}")

#: The separators employers use to append a location or a team to a title. Spaces are
#: required on both sides so that "Full-Stack" and "Front-End" are never split.
_TITLE_SEPARATOR_RE: Final[re.Pattern[str]] = re.compile(r"\s+[-|/@~]\s+")

#: Geographic names, country and region codes. A trailing segment containing one of these
#: says *where* the job is, not *what* it is.
_PLACE_HINT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:us|usa|u\.s\.?a?|united\s+states|uk|united\s+kingdom|canada|mexico|brazil"
    r"|emea|apac|latam|amer|noram|europe|asia|africa|india|germany|france|spain|italy"
    r"|portugal|poland|netherlands|belgium|switzerland|sweden|norway|denmark|finland"
    r"|ireland|israel|australia|new\s+zealand|singapore|japan|china|korea|taiwan"
    r"|nyc|sf|bay\s+area|silicon\s+valley|london|berlin|munich|paris|amsterdam|dublin"
    r"|barcelona|madrid|lisbon|warsaw|krakow|zurich|stockholm|copenhagen|helsinki|oslo"
    r"|toronto|montreal|vancouver|austin|seattle|boston|chicago|denver|atlanta|miami"
    r"|dallas|houston|phoenix|portland|philadelphia|pittsburgh|raleigh|nashville"
    r"|new\s+york|san\s+francisco|san\s+jose|san\s+diego|los\s+angeles|washington"
    r"|bangalore|bengaluru|hyderabad|pune|chennai|mumbai|delhi|gurgaon|noida"
    r"|tel\s+aviv|sydney|melbourne|tokyo|seoul|shanghai|beijing|hong\s+kong"
    r"|multiple\s+locations|various\s+locations|based\s+in)\b",
    re.IGNORECASE,
)

#: Work-arrangement words. Also a "where", not a "what".
_ARRANGEMENT_HINT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:remote|hybrid|onsite|on[-\s]?site|in[-\s]?office|in[-\s]person|anywhere"
    r"|worldwide|global|distributed|wfh|field[-\s]based|home[-\s]based)\b",
    re.IGNORECASE,
)

#: Words that make a trailing segment part of the *role*. Their presence vetoes stripping,
#: so "Backend Engineer - Remote Infrastructure Engineer" never loses its second half.
_ROLE_WORD_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:engineer|engineering|developer|scientist|analyst|architect|manager|management"
    r"|designer|design|researcher|research|lead|director|specialist|consultant|administrator"
    r"|technician|intern|internship|associate|apprentice|coordinator|strategist|writer"
    r"|recruiter|accountant|counsel|nurse|therapist|teacher|instructor|programme?|program)\b",
    re.IGNORECASE,
)

#: The ``City, Region`` shape: two letter-only fragments separated by a comma.
_CITY_REGION_RE: Final[re.Pattern[str]] = re.compile(
    r"^([a-z .'\-]{2,30}),\s*([a-z .'\-]{2,30})$", re.IGNORECASE
)

#: Longest a region abbreviation may be to count as one on its own: "NY", "CA", "UK", "ON".
_MAX_REGION_CODE_LENGTH: Final[int] = 3

#: Longest a trailing segment may be, in tokens, and still be read as a location. A five-word
#: tail is a description, not an office.
_MAX_LOCATION_TAIL_TOKENS: Final[int] = 5

#: Trailing seniority level expressed as a Roman numeral. ``"Engineer II"`` and
#: ``"Engineer III"`` are the same role at different levels and are usually two rows of the
#: same requisition, so the numeral is dropped from the identity key.
_TRAILING_ROMAN_RE: Final[re.Pattern[str]] = re.compile(
    r"\s+(?:i{1,3}|iv|v|vi{1,3}|ix|x)$", re.IGNORECASE
)

#: How many trailing segments :func:`normalize_title` will strip. Two covers
#: ``"SWE - Backend - Remote, US"`` without eating a hyphen-rich real title.
_MAX_TAIL_STRIPS: Final[int] = 2


def _is_city_region(segment: str) -> bool:
    """Return whether *segment* has the ``City, Region`` shape.

    Args:
        segment: A candidate tail containing exactly one comma.

    Returns:
        ``True`` when the part after the comma is a short region code (``"NY"``, ``"UK"``) or
        a recognised place name (``"Germany"``). Requiring that of the *second* fragment is
        what keeps ``"Backend, Remote"`` — which has the same letters-comma-letters shape —
        from being mistaken for a city and a state.
    """
    match = _CITY_REGION_RE.match(segment.strip())
    if match is None:
        return False
    region = match.group(2).strip()
    if len(region.replace(".", "")) <= _MAX_REGION_CODE_LENGTH:
        return True
    return bool(_PLACE_HINT_RE.fullmatch(region) or _PLACE_HINT_RE.search(region))


def _looks_like_location(segment: str) -> bool:
    """Return whether a trailing title segment names a place rather than a specialisation.

    Getting this wrong in the permissive direction is what merges two genuinely different
    roles, so the test is deliberately narrow: a tail is a location only when it is short,
    contains no role vocabulary, and either has the ``City, Region`` shape or is a single
    fragment naming a place or a work arrangement.

    Args:
        segment: The candidate tail, already cleaned.

    Returns:
        ``True`` when the segment says where the job is rather than what it is.
    """
    candidate = segment.strip().rstrip(".,;")
    if not candidate:
        return False
    if len(candidate.split()) > _MAX_LOCATION_TAIL_TOKENS:
        return False
    if _ROLE_WORD_RE.search(candidate):
        return False
    if "," in candidate:
        # A multi-fragment tail must look like an address; a bare hint anywhere inside it is
        # not enough, or "Backend, Remote" would take "Backend" with it.
        return _is_city_region(candidate)
    return bool(_PLACE_HINT_RE.search(candidate) or _ARRANGEMENT_HINT_RE.search(candidate))


def _strip_one_tail(text: str) -> str | None:
    """Remove one trailing location suffix from a title, if there is one.

    Two separator styles are tried, in order. The *last* dash/pipe/slash/``@`` is tried
    first, because that is where employers append the office. Failing that, each comma is
    tried from the left, longest tail first, which handles ``"Software Engineer, New York,
    NY"`` without also eating ``"Backend"`` out of ``"Engineer, Backend, Remote"``.

    Args:
        text: The title so far.

    Returns:
        The title with one tail removed, or ``None`` when no trailing segment qualifies.
    """
    last_separator = None
    for match in _TITLE_SEPARATOR_RE.finditer(text):
        last_separator = match
    if last_separator is not None:
        head = text[: last_separator.start()].strip()
        tail = text[last_separator.end() :]
        if head and _looks_like_location(tail):
            return head

    fragments = [fragment.strip() for fragment in text.split(",")]
    if len(fragments) >= 2:
        for start in range(1, len(fragments)):
            head = ", ".join(fragments[:start]).strip()
            tail = ", ".join(fragments[start:]).strip()
            if head and _looks_like_location(tail):
                return head
    return None


def normalize_title(title: Any) -> str:
    """Collapse a role title to its deduplication key.

    Employers decorate the same title differently on every board. This strips the decoration
    and keeps the role:

    1. Requisition identifiers, wherever they appear: ``(R12345)``, ``[JR-99]``, ``#4821``,
       ``REQ 10233``.
    2. Every parenthetical and bracketed group, which is where boards put office names,
       contract flags and duplicated seniority.
    3. Trailing location and work-arrangement suffixes after a final dash, pipe, slash, ``@``
       or comma — but only when the tail actually names a place, so
       ``"Staff Engineer - Platform"`` keeps its specialisation while
       ``"Staff Engineer - Remote, US"`` does not keep its tail.
    4. A trailing seniority Roman numeral.

    The result is lowercased, ASCII-folded and whitespace-collapsed.

    Args:
        title: A raw role title. Non-strings are stringified; ``None`` yields ``""``.

    Returns:
        The normalisation key, or ``""`` when nothing survives — which happens for a title
        that was *only* a requisition id, and correctly forces :func:`dedupe_key` to lean on
        the provider identifier instead.

    Example:
        >>> normalize_title("Senior Software Engineer (R12345) - New York, NY")
        'senior software engineer'
        >>> normalize_title("Backend Engineer III | Remote")
        'backend engineer'
    """
    cleaned = clean_text(title)
    if not cleaned:
        return ""

    stripped = _REQ_ID_RE.sub(" ", cleaned)
    stripped = _BRACKETED_RE.sub(" ", stripped)
    stripped = clean_text(stripped)

    for _ in range(_MAX_TAIL_STRIPS):
        head = _strip_one_tail(stripped)
        if head is None:
            break
        stripped = head.rstrip("-|/@~, \t")

    normalized = " ".join(_tokenize(stripped))
    while True:
        trimmed = _TRAILING_ROMAN_RE.sub("", normalized)
        if trimmed == normalized or not trimmed:
            break
        normalized = trimmed
    return normalized.strip()


def normalize_location(location: Any) -> str:
    """Collapse a location string to a comparable key.

    Deliberately shallow: no geocoding, no state-abbreviation expansion, no aliasing of
    ``"NYC"`` to ``"New York"``. It removes punctuation and case so that
    ``"New York, NY"`` and ``"New York NY"`` agree, and stops there, because a location that
    is *guessed* to be equivalent would merge two genuinely different offices of the same
    employer — exactly the failure :func:`dedupe_key` must not make.

    Args:
        location: A raw location string. ``None`` yields ``""``.

    Returns:
        The normalisation key.

    Example:
        >>> normalize_location("New York, NY (Hybrid)")
        'new york ny hybrid'
    """
    return " ".join(_tokenize(clean_text(location)))


# ======================================================================================
# Hashing and keys
# ======================================================================================


def content_hash(p: RawPosting) -> str:
    """Digest the advertised *content* of a posting.

    This answers "has this job's text changed since I last looked at it?", not "is this the
    same job?". A changed hash invalidates the cached :class:`~app.models.score.JobScore` and
    the cached tailored resume for that posting; an unchanged hash means a re-poll costs
    nothing (golden rule #9).

    Title and company are normalised through :func:`normalize_title` and
    :func:`normalize_company`, and the description is whitespace-collapsed and lowercased.
    Case and formatting therefore do not invalidate a score — nothing downstream is
    case-sensitive — while an edited requirement or a changed employer does.

    Args:
        p: The posting, or any object exposing ``title``, ``company_name`` and
            ``description``.

    Returns:
        A 64-character lowercase hexadecimal SHA-256 digest.
    """
    payload = _KEY_SEPARATOR.join(
        (
            normalize_title(getattr(p, "title", "")),
            normalize_company(getattr(p, "company_name", "")),
            clean_text(getattr(p, "description", "") or "").lower(),
        )
    )
    return _sha256(payload)


def dedupe_key(p: RawPosting) -> str:
    """Compute the hard identity key for a posting.

    Backs ``UNIQUE(job_postings.dedupe_key)``, so this is the one function in the module
    whose mistakes are unrecoverable: two distinct openings that collapse onto one key mean
    the user is silently never shown one of them.

    **The choice.** When the provider supplied an ``external_id``, the key is
    ``sha256("<provider>|<external_id>")``. That pair is already the provider's own identity
    for the posting and is guaranteed unique by ``UNIQUE(provider, external_id)``, so the key
    is exact, stable across re-polls and re-parses, and *incapable* of merging two different
    jobs. Only when there is no external id — a manually entered posting, a scraped careers
    page — does it fall back to ``sha256("<company>|<title>|<location>")``, the triple a human
    would use to say "that's the same job".

    **What this deliberately does not do.** The provider-scoped form cannot match the same
    role syndicated to two different ATSs, and that is intentional. Cross-provider merging is
    a *judgement*, not an identity claim, and it belongs to :func:`is_duplicate` and
    :class:`app.services.dedupe_service`, where a wrong answer is visible, reversible, and
    reviewable by a human. Encoding that judgement in a unique constraint would trade a
    visible duplicate — mildly annoying — for an invisible disappearance, which is the one
    failure the user can never detect.

    Args:
        p: The posting, or any object exposing ``provider``, ``external_id``, ``title``,
            ``company_name`` and ``location``.

    Returns:
        A 64-character lowercase hexadecimal SHA-256 digest.
    """
    provider = getattr(p, "provider", "")
    provider_value = getattr(provider, "value", provider)
    external_id = clean_text(getattr(p, "external_id", ""))

    if external_id:
        return _sha256(f"{provider_value}{_KEY_SEPARATOR}{external_id}")

    fallback = _KEY_SEPARATOR.join(
        (
            normalize_company(getattr(p, "company_name", "")),
            normalize_title(getattr(p, "title", "")),
            normalize_location(getattr(p, "location", "")),
        )
    )
    logger.debug(
        "dedupe.key_from_content",
        provider=str(provider_value),
        title=clean_text(getattr(p, "title", ""))[:80],
    )
    return _sha256(fallback)


# ======================================================================================
# Similarity
# ======================================================================================

#: Jaccard score at or above which two postings are treated as the same job. Set high on
#: purpose. Normalised titles are short — three or four tokens — so at 0.92 a single token of
#: difference already fails, which means the threshold effectively demands identical token
#: sets. That lets a near-duplicate through occasionally, and the user sees one extra row;
#: the looser alternative merges two real openings and the user sees neither.
DEFAULT_SIMILARITY_THRESHOLD: Final[float] = 0.92


def _identity_tokens(posting: Any) -> frozenset[str]:
    """Return the token bag a posting is compared by.

    Args:
        posting: Any object exposing ``title`` and ``company_name``.

    Returns:
        The union of the normalised title's tokens and the normalised company's tokens.
    """
    title_tokens = normalize_title(getattr(posting, "title", "")).split()
    company_tokens = normalize_company(getattr(posting, "company_name", "")).split()
    return frozenset(title_tokens) | frozenset(company_tokens)


def similarity(a: RawPosting, b: RawPosting) -> float:
    """Score how alike two postings are, from 0.0 to 1.0.

    Token-set Jaccard over the normalised title plus the normalised company: the size of the
    intersection divided by the size of the union. Set-based rather than sequence-based
    because boards reorder title components freely (``"Engineer, Backend"`` versus
    ``"Backend Engineer"``) and that reordering carries no meaning.

    Including the company in the same bag is what keeps the score honest across employers:
    two different roles at one company share only the company tokens, and the same role at
    two companies shares only the title tokens. Neither reaches
    :data:`DEFAULT_SIMILARITY_THRESHOLD`.

    Args:
        a: One posting, or any object exposing ``title`` and ``company_name``.
        b: The other.

    Returns:
        The Jaccard coefficient, or ``0.0`` when either posting normalises to no tokens at
        all — an unknown is never scored as a match.

    Example:
        >>> from app.jobs.base import RawPosting
        >>> from app.models.enums import ATSProviderName
        >>> make = lambda t, c: RawPosting(ATSProviderName.MANUAL, "", "", t, c)
        >>> similarity(make("Senior Engineer (R1)", "Acme Inc"),
        ...            make("Senior Engineer - Remote", "Acme"))
        1.0
    """
    tokens_a = _identity_tokens(a)
    tokens_b = _identity_tokens(b)
    if not tokens_a or not tokens_b:
        return 0.0
    union = tokens_a | tokens_b
    if not union:
        return 0.0
    return len(tokens_a & tokens_b) / len(union)


def is_duplicate(
    a: RawPosting,
    b: RawPosting,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> bool:
    """Judge whether two postings are the same opening.

    Cheap certainties are checked before the fuzzy comparison, because each of them is an
    exact identity claim that no similarity score should be allowed to override:

    1. The same ``provider`` and ``external_id`` — the provider's own identity.
    2. The same :func:`dedupe_key`.
    3. The same :func:`content_hash` — identical advertised text.

    Otherwise the decision is :func:`similarity` against *threshold*, plus one guard: two
    postings whose normalised employers are both known and different are never duplicates,
    however alike their titles read. ``"Software Engineer" @ Acme`` and
    ``"Software Engineer" @ Globex`` are two jobs, and merging them would hide a real
    opening.

    Args:
        a: One posting.
        b: The other.
        threshold: Minimum :func:`similarity` for a fuzzy match.

    Returns:
        ``True`` when the two describe the same opening.
    """
    if a is b:
        return True

    provider_a = getattr(getattr(a, "provider", None), "value", getattr(a, "provider", None))
    provider_b = getattr(getattr(b, "provider", None), "value", getattr(b, "provider", None))
    external_a = clean_text(getattr(a, "external_id", ""))
    external_b = clean_text(getattr(b, "external_id", ""))
    if external_a and external_a == external_b and provider_a == provider_b:
        return True

    if dedupe_key(a) == dedupe_key(b):
        return True

    if content_hash(a) == content_hash(b):
        return True

    company_a = normalize_company(getattr(a, "company_name", ""))
    company_b = normalize_company(getattr(b, "company_name", ""))
    if company_a and company_b and company_a != company_b:
        return False

    return similarity(a, b) >= threshold
