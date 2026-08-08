"""LinkedIn — read what the user already has, and nothing else.

**Terms of service, stated first because it governs everything below.** LinkedIn's User
Agreement prohibits automated scraping of the service and prohibits automated submission of
applications through it. ApplicantOS therefore **does not log into LinkedIn, does not scrape
LinkedIn, and does not submit applications on LinkedIn.** There is no credential store for
it, no session cookie, no headless browser pointed at it, and no "Easy Apply" automation.
:meth:`LinkedInProvider.apply` raises :class:`~app.jobs.base.UnsupportedFlowError`, always.
Postings discovered here are surfaced for **manual review** — the user opens the link and
applies themselves, with a tailored resume already rendered and waiting
(``docs/CONTRACTS.md`` §9, golden rule #10).

That is not a limitation this module works around. It is the module's design.

What remains is genuinely useful, and it is entirely built from data the user already owns or
that LinkedIn publishes openly:

**The user's own data export.** ``extra["export_path"]`` points at the archive LinkedIn hands
its members on request — a ZIP, an unpacked folder, or a single CSV/JSON file out of one. The
saved-jobs and applied-jobs tables in it are a curated list of roles the user chose, which is
a better relevance signal than any search query. Reading a file the user downloaded is not
scraping; it is the same posture :class:`~app.knowledge.analyzers.LinkedInExportAnalyzer`
takes with the profile side of the same archive.

**A public feed.** ``extra["feed_url"]`` points at an RSS or Atom feed the user subscribes
to. Feeds are published to be read by machines; consuming one is what it is for. Parsing uses
:mod:`xml.etree.ElementTree` from the standard library, so no dependency is added and both
RSS ``<item>`` and Atom ``<entry>`` documents are handled.

**Open Graph metadata on a public posting.** :meth:`LinkedInProvider.fetch_posting` reads the
``og:*`` tags a public job URL serves to any crawler — the same tags that render the preview
card when the link is pasted into a chat window. If the request meets an authentication wall,
a challenge, or LinkedIn's ``999`` bot-defence status, it returns ``None``. It does not retry,
rotate anything, or otherwise attempt to get around the block. A refusal is an answer.

With neither source configured, :meth:`LinkedInProvider.search` yields nothing and logs
``linkedin.no_source_configured`` explaining how to supply one. That is the honest outcome:
the alternative would be to scrape, and this system does not do that.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import re
import zipfile
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar, Final
from urllib.parse import urlsplit
from xml.etree import ElementTree

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
    ProviderError,
    RawPosting,
    SearchQuery,
    UnsupportedFlowError,
)
from app.models.enums import ATSProviderName, PluginKind, WorkArrangement
from app.plugins.base import PluginMeta
from app.plugins.registry import plugin

__all__ = [
    "EXPORT_PATH_KEYS",
    "FEED_URL_KEYS",
    "MAX_EXPORT_BYTES",
    "MAX_FEED_BYTES",
    "NO_SOURCE_MESSAGE",
    "UNTITLED_POSTING",
    "LinkedInProvider",
    "canonical_job_url",
    "extract_job_id",
    "parse_open_graph",
    "split_title_and_company",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Source configuration
# ======================================================================================

#: ``SearchQuery.extra`` keys naming a LinkedIn data export. ``export_path`` is the
#: documented spelling; the aliases exist because a user editing a settings file writes
#: whichever one occurs to them, and a silently ignored key looks like a broken feature.
EXPORT_PATH_KEYS: Final[tuple[str, ...]] = (
    "export_path",
    "linkedin_export",
    "linkedin_export_path",
    "saved_jobs_path",
)

#: ``SearchQuery.extra`` keys naming a public RSS or Atom feed.
FEED_URL_KEYS: Final[tuple[str, ...]] = ("feed_url", "rss_url", "feed", "linkedin_feed")

#: Logged verbatim when neither source is configured, so the log line itself tells an
#: operator how to fix it rather than sending them to the documentation.
NO_SOURCE_MESSAGE: Final[str] = (
    "LinkedIn discovery reads only data you already have: set extra['export_path'] to your "
    "LinkedIn data export (Settings > Data privacy > Get a copy of your data — a .zip, an "
    "unpacked folder, or a single Saved Jobs .csv/.json), or extra['feed_url'] to a public "
    "RSS/Atom job feed. ApplicantOS never logs into or scrapes LinkedIn."
)

#: Largest export file read into memory. A saved-jobs table is kilobytes; this exists so that
#: a mistyped path pointing at a disk image cannot exhaust the process.
MAX_EXPORT_BYTES: Final[int] = 32 * 1024 * 1024

#: Largest feed document parsed. An RSS job feed is tens of kilobytes; a document orders of
#: magnitude larger is either not a feed or is not one worth expanding in memory.
MAX_FEED_BYTES: Final[int] = 16 * 1024 * 1024

#: Filenames inside a LinkedIn export that hold job rows, normalised the same way the file
#: stems are (lowercase, alphanumerics only). LinkedIn has renamed these tables more than
#: once, so several generations are recognised.
_JOB_TABLE_STEMS: Final[frozenset[str]] = frozenset(
    {
        "savedjobs",
        "jobapplications",
        "jobsappliedto",
        "jobapplicationsandsavedjobs",
        "savedjobalerts",
        "jobs",
    }
)

#: Shortest stem allowed to match a known table name by being a *substring* of it. Without
#: this, a one-letter filename would match half the table names by accident.
_MIN_STEM_MATCH_LENGTH: Final[int] = 3

#: Extensions read from an export directory or archive.
_EXPORT_SUFFIXES: Final[frozenset[str]] = frozenset({".csv", ".json"})

#: Text encodings tried, in order. ``utf-8-sig`` transparently removes the byte-order mark
#: Excel writes when a user opens and re-saves a LinkedIn CSV, which would otherwise turn the
#: first column header into ``"﻿Job Title"`` and make it unmatchable.
_TEXT_ENCODINGS: Final[tuple[str, ...]] = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


# ======================================================================================
# Column and field mapping
# ======================================================================================

#: Everything that is not a letter or a digit, removed when normalising a column header so
#: that ``"Job Title"``, ``"job_title"``, ``"jobTitle"`` and ``"Job-Title"`` are one key.
_HEADER_NOISE_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

#: Normalised header aliases per logical field, in preference order.
_COLUMN_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "title": ("jobtitle", "title", "position", "role", "postingtitle", "name"),
    "company": (
        "companyname",
        "company",
        "employer",
        "organization",
        "organisation",
        "companyurn",
    ),
    "url": (
        "joburl",
        "url",
        "joblink",
        "link",
        "postingurl",
        "jobposting",
        "jobpostingurl",
        "applyurl",
    ),
    "location": ("joblocation", "location", "city", "region", "workplace"),
    "posted_at": (
        "saveddate",
        "savedon",
        "applicationdate",
        "dateapplied",
        "listeddate",
        "postedon",
        "posteddate",
        "date",
        "createdat",
    ),
    "description": ("jobdescription", "description", "summary", "notes", "details"),
    "employment_type": ("employmenttype", "jobtype", "worktype", "contracttype"),
    "workplace_type": ("workplacetype", "workplace", "remote", "locationtype"),
    "job_id": ("jobid", "jobpostingid", "postingid", "jobpostingurn", "urn", "id"),
}

#: Every header this module recognises, flattened. Used to find the real header row inside a
#: CSV that LinkedIn prefixed with an explanatory preamble.
_KNOWN_HEADERS: Final[frozenset[str]] = frozenset(
    alias for aliases in _COLUMN_ALIASES.values() for alias in aliases
)

#: Recognised headers a line must carry before it is accepted as a CSV table's header row.
#: Two is enough to be decisive and low enough that an export with unusual column names is
#: still found.
_MIN_HEADER_MATCHES: Final[int] = 2

#: Title given to an export row that carries a link but no role title. Marked visibly rather
#: than invented: the row is real and worth surfacing, and :meth:`LinkedInProvider.
#: fetch_posting` can fill in the rest from the public page later.
UNTITLED_POSTING: Final[str] = "(untitled LinkedIn posting)"

#: JSON keys, at the top level of an export document, that hold the array of job records.
_JSON_COLLECTION_KEYS: Final[tuple[str, ...]] = (
    "savedJobs",
    "saved_jobs",
    "jobApplications",
    "job_applications",
    "jobs",
    "elements",
    "items",
    "results",
    "data",
)


# ======================================================================================
# LinkedIn URL vocabulary
# ======================================================================================

#: The numeric posting identifier, in every URL shape LinkedIn emits: the canonical
#: ``/jobs/view/<id>``, the tracked ``/comm/jobs/view/<id>``, and the search-page
#: ``?currentJobId=<id>`` form a user copies out of the address bar.
_JOB_ID_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"/jobs/view/(\d{6,})", re.IGNORECASE),
    re.compile(r"[?&]currentJobId=(\d{6,})", re.IGNORECASE),
    re.compile(r"jobPosting:(\d{6,})", re.IGNORECASE),
    re.compile(r"^(\d{6,})$"),
)

#: Canonical public URL for a posting, built from its identifier.
_CANONICAL_JOB_URL: Final[str] = "https://www.linkedin.com/jobs/view/{job_id}/"

#: Evidence in a response body that LinkedIn is asking for a login or running a challenge
#: rather than serving the posting. Any of them ends the attempt: this system reads what is
#: public and stops where the public part stops.
_BLOCK_MARKERS: Final[tuple[str, ...]] = (
    "authwall",
    "/checkpoint/challenge",
    "/uas/login",
    "sign in to view",
    "join linkedin to",
    "please verify you",
    "security verification",
    "unusual activity",
)

#: LinkedIn's non-standard bot-defence status. Treated as a block, never as a transient error
#: worth retrying — retrying is precisely what it is asking us not to do.
_BOT_DEFENCE_STATUS: Final[int] = 999

#: Statuses that mean "not for you", all of which resolve to ``None`` rather than an error.
_BLOCKED_STATUSES: Final[frozenset[int]] = frozenset({401, 403, 429, _BOT_DEFENCE_STATUS})

#: How much of a response body is scanned for block markers.
_MAX_BODY_SCAN_CHARS: Final[int] = 200_000

#: ``"<Company> hiring <Title> in <Location>"`` — the shape of LinkedIn's ``og:title``.
_OG_HIRING_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<company>.+?)\s+hiring\s+(?P<title>.+?)(?:\s+in\s+(?P<location>.+?))?$",
    re.IGNORECASE,
)

#: ``"<Title> at <Company>"`` — the shape most job feeds use.
_TITLE_AT_COMPANY_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<title>.+?)\s+(?:at|@|-\s*at)\s+(?P<company>[^|]+?)$", re.IGNORECASE
)

#: Site-name suffixes appended to a page title, stripped before parsing it.
_TITLE_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"\s*[|–-]\s*(?:linkedin|jobs?)\s*$", re.IGNORECASE
)

#: Length of the digest used to identify a posting that carries no identifier of its own.
_SYNTHETIC_ID_LENGTH: Final[int] = 16

#: Prefix marking an identifier this system derived rather than read, so nobody mistakes one
#: for a real LinkedIn job id.
_SYNTHETIC_ID_PREFIX: Final[str] = "local-"


# ======================================================================================
# Feed vocabulary
# ======================================================================================

#: RSS element names holding each field, matched on the local name so that a document's
#: namespace declarations do not have to be enumerated.
_FEED_TITLE_TAGS: Final[tuple[str, ...]] = ("title",)
_FEED_LINK_TAGS: Final[tuple[str, ...]] = ("link", "guid", "id")
_FEED_BODY_TAGS: Final[tuple[str, ...]] = ("description", "summary", "content", "encoded")
_FEED_DATE_TAGS: Final[tuple[str, ...]] = ("pubdate", "published", "updated", "date", "modified")
_FEED_AUTHOR_TAGS: Final[tuple[str, ...]] = ("creator", "author", "source", "publisher", "company")
_FEED_LOCATION_TAGS: Final[tuple[str, ...]] = ("location", "region", "joblocation", "city")

#: Entry container elements: RSS ``<item>``, Atom ``<entry>``, and the ``<job>`` element some
#: job-board feeds use.
_FEED_ENTRY_TAGS: Final[frozenset[str]] = frozenset({"item", "entry", "job"})

#: URL schemes a feed may be fetched over. Anything else — ``file:``, ``ftp:``, a bare
#: ``javascript:`` — is refused rather than resolved.
_ALLOWED_FEED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})


# ======================================================================================
# Small parsing helpers
# ======================================================================================


def _normalize_header(value: Any) -> str:
    """Reduce a column header or JSON key to its comparison form.

    Args:
        value: The raw header.

    Returns:
        The header lowercased with every non-alphanumeric character removed, so that
        spacing, casing and separator style cannot prevent a match.
    """
    return _HEADER_NOISE_RE.sub("", clean_text(value).lower())


def _pick(row: Mapping[str, Any], field: str) -> str:
    """Return the value of one logical field from an export row.

    Args:
        row: The row, already keyed by normalised header.
        field: A key of :data:`_COLUMN_ALIASES`.

    Returns:
        The first alias that carries a value, cleaned; ``""`` when the row has none of them.
    """
    for alias in _COLUMN_ALIASES.get(field, ()):
        value = row.get(alias)
        if value is None:
            continue
        cleaned = clean_text(value)
        if cleaned:
            return cleaned
    return ""


def _normalize_row(row: Mapping[Any, Any]) -> dict[str, Any]:
    """Re-key one export row by normalised header.

    Args:
        row: A ``csv.DictReader`` row or a JSON object.

    Returns:
        The row keyed by normalised header. Unnamed surplus CSV columns — which
        :class:`csv.DictReader` collects under a ``None`` key — are dropped, because they
        belong to no header and cannot be interpreted.
    """
    normalized: dict[str, Any] = {}
    for key, value in row.items():
        if key is None:
            continue
        header = _normalize_header(key)
        if header and header not in normalized:
            normalized[header] = value
    return normalized


def extract_job_id(value: Any) -> str | None:
    """Pull LinkedIn's numeric posting identifier out of a URL or a bare id.

    Args:
        value: A LinkedIn job URL in any of its shapes, or the identifier itself.

    Returns:
        The identifier, or ``None`` when the value carries none.

    Example:
        >>> extract_job_id("https://www.linkedin.com/jobs/view/3912345678/?refId=abc")
        '3912345678'
    """
    text = clean_text(value)
    if not text:
        return None
    for pattern in _JOB_ID_PATTERNS:
        found = pattern.search(text)
        if found:
            return found.group(1)
    return None


def canonical_job_url(job_id: str) -> str:
    """Return the canonical public URL for a LinkedIn posting identifier.

    Args:
        job_id: The numeric identifier.

    Returns:
        The ``https://www.linkedin.com/jobs/view/<id>/`` URL, which is what LinkedIn itself
        redirects every tracked variant to.
    """
    return _CANONICAL_JOB_URL.format(job_id=job_id)


def _synthetic_id(*parts: str) -> str:
    """Derive a stable identifier for a posting that has none of its own.

    Uses :mod:`hashlib` rather than :func:`hash`, which is salted per process and would give
    the same saved job a different identity on every run — producing a duplicate row at each
    poll instead of an idempotent upsert.

    Args:
        *parts: The identifying strings, typically company and title.

    Returns:
        A prefixed hex digest, marked so it is never mistaken for a LinkedIn job id.
    """
    material = "␟".join(part.casefold() for part in parts if part)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{_SYNTHETIC_ID_PREFIX}{digest[:_SYNTHETIC_ID_LENGTH]}"


def split_title_and_company(text: Any) -> tuple[str, str | None]:
    """Separate a role title from an employer name in a single line of text.

    Feed entries and Open Graph titles pack both into one string, in two competing shapes:
    LinkedIn writes ``"Acme hiring Senior Engineer in Berlin"``, while most job feeds write
    ``"Senior Engineer at Acme"``. Both are recognised; anything else is returned as a title
    with no company, because inventing an employer name would put a fabricated company on a
    real application.

    Args:
        text: The combined line.

    Returns:
        ``(title, company)``, where *company* is ``None`` when the text does not name one.

    Example:
        >>> split_title_and_company("Acme Corp hiring Staff Engineer in Berlin | LinkedIn")
        ('Staff Engineer', 'Acme Corp')
        >>> split_title_and_company("Staff Engineer at Acme Corp")
        ('Staff Engineer', 'Acme Corp')
    """
    cleaned = _TITLE_SUFFIX_RE.sub("", clean_text(text)).strip()
    if not cleaned:
        return ("", None)

    hiring = _OG_HIRING_RE.match(cleaned)
    if hiring:
        return (clean_text(hiring.group("title")), clean_text(hiring.group("company")) or None)

    at_company = _TITLE_AT_COMPANY_RE.match(cleaned)
    if at_company:
        return (
            clean_text(at_company.group("title")),
            clean_text(at_company.group("company")) or None,
        )

    return (cleaned, None)


def _location_from_title(text: Any) -> str | None:
    """Return the location LinkedIn embeds in its ``og:title``, when it embeds one.

    Args:
        text: The Open Graph title.

    Returns:
        The location, or ``None``.
    """
    hiring = _OG_HIRING_RE.match(_TITLE_SUFFIX_RE.sub("", clean_text(text)).strip())
    if not hiring:
        return None
    return clean_text(hiring.group("location")) or None


class _MetaTagCollector(HTMLParser):
    """Collect ``<meta>`` tags and the document title, and nothing else.

    A parser rather than a regular expression because meta attributes appear in any order and
    with any quoting, and because this must not be fooled into reading anything beyond the
    metadata — the whole point of :meth:`LinkedInProvider.fetch_posting` is that it reads the
    public preview card and stops there.

    Attributes:
        tags: Meta content keyed by the lowercased ``property`` or ``name`` attribute.
        title: The document's ``<title>`` text.
    """

    def __init__(self) -> None:
        """Create a collector with entity conversion enabled."""
        super().__init__(convert_charrefs=True)
        self.tags: dict[str, str] = {}
        self.title: str = ""
        self._in_title: bool = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record a meta tag, or note that the document title has begun.

        Args:
            tag: Lowercased element name.
            attrs: Element attributes.
        """
        if tag == "title":
            self._in_title = True
            return
        if tag != "meta":
            return
        values = {key.lower(): (value or "") for key, value in attrs if key}
        key = values.get("property") or values.get("name") or values.get("itemprop")
        content = values.get("content", "")
        if key and content and key.lower() not in self.tags:
            self.tags[key.lower()] = content

    def handle_endtag(self, tag: str) -> None:
        """Note that the document title has ended.

        Args:
            tag: Lowercased element name.
        """
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        """Accumulate the document title text.

        Args:
            data: Character data.
        """
        if self._in_title and data:
            self.title += data

    def error(self, message: str) -> None:
        """Ignore a malformed-markup report from the legacy strict-mode hook.

        Args:
            message: The parser's complaint.
        """
        logger.debug("linkedin.meta_parse_warning", detail=message)


def parse_open_graph(html: str) -> dict[str, str]:
    """Extract a page's Open Graph and related metadata.

    Args:
        html: The document markup.

    Returns:
        Metadata keyed by property name — ``og:title``, ``og:description``, ``og:url``,
        ``og:site_name``, ``description``, and ``title`` for the document title. Empty when
        the document carries no metadata; never raises, because a truncated or malformed
        response is an ordinary outcome and must degrade rather than propagate.
    """
    collector = _MetaTagCollector()
    try:
        collector.feed(html)
        collector.close()
    except Exception as exc:  # noqa: BLE001 - malformed markup must not lose the metadata
        logger.debug("linkedin.meta_parse_failed", error=str(exc))

    metadata = dict(collector.tags)
    document_title = clean_text(collector.title)
    if document_title:
        metadata.setdefault("title", document_title)
    return metadata


def _extra_values(extra: Mapping[str, Any] | None, keys: Sequence[str]) -> list[str]:
    """Read a list of strings out of ``SearchQuery.extra`` under any of *keys*.

    Args:
        extra: The query's ``extra`` mapping.
        keys: Accepted key spellings, in preference order.

    Returns:
        The values, de-duplicated and in order. A single string yields one value — paths and
        URLs are never split on a delimiter, because both may legitimately contain one.
    """
    if not isinstance(extra, Mapping):
        return []

    collected: list[str] = []
    seen: set[str] = set()
    for key in keys:
        raw = extra.get(key)
        if raw is None:
            continue
        candidates: Iterable[Any] = (
            [raw] if isinstance(raw, (str, Path)) else raw if isinstance(raw, (list, tuple)) else []
        )
        for candidate in candidates:
            text = str(candidate).strip()
            if text and text not in seen:
                seen.add(text)
                collected.append(text)
    return collected


def _decode(payload: bytes) -> str:
    """Decode export bytes, tolerating a byte-order mark and legacy encodings.

    Args:
        payload: The raw file contents.

    Returns:
        The decoded text. The final encoding in :data:`_TEXT_ENCODINGS` maps every byte, so
        this always succeeds — a mangled character is a far better outcome than losing the
        user's saved jobs to a decode error.
    """
    for encoding in _TEXT_ENCODINGS:
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("latin-1", errors="replace")


def _localname(tag: Any) -> str:
    """Return an XML tag's local name, lowercased and without its namespace.

    Args:
        tag: The tag as :mod:`xml.etree.ElementTree` reports it, e.g.
            ``"{http://www.w3.org/2005/Atom}entry"``.

    Returns:
        The lowercased local name, or ``""`` for a comment or processing instruction, whose
        ``tag`` is a callable rather than a string.
    """
    if not isinstance(tag, str):
        return ""
    return tag.rpartition("}")[2].lower()


def _child_text(element: ElementTree.Element, names: Sequence[str]) -> str:
    """Return the text of the first child element matching any of *names*.

    Args:
        element: The parent element.
        names: Local names to look for, in preference order.

    Returns:
        The child's text, cleaned; ``""`` when no child matches or all are empty.
    """
    wanted = {name.lower() for name in names}
    for child in element:
        if _localname(child.tag) not in wanted:
            continue
        text = clean_text(child.text)
        if text:
            return text
    return ""


def _entry_link(element: ElementTree.Element) -> str:
    """Return the best link for one feed entry.

    Handles both conventions: RSS puts the URL in ``<link>``'s text, Atom puts it in a
    ``<link>`` element's ``href`` attribute and may offer several ``rel`` variants.

    Args:
        element: The ``<item>`` or ``<entry>`` element.

    Returns:
        The URL, or ``""`` when the entry carries none.
    """
    fallback = ""
    for child in element:
        if _localname(child.tag) != "link":
            continue
        href = clean_text(child.attrib.get("href"))
        rel = (child.attrib.get("rel") or "alternate").lower()
        if href and rel == "alternate":
            return href
        if href and not fallback:
            fallback = href
        text = clean_text(child.text)
        if text and not fallback:
            fallback = text
    if fallback:
        return fallback

    for name in ("guid", "id"):
        value = _child_text(element, (name,))
        if value.lower().startswith(("http://", "https://")):
            return value
    return ""


# ======================================================================================
# The provider
# ======================================================================================


@plugin
class LinkedInProvider(ATSProvider):
    """LinkedIn: discovery from user-supplied and public data only, never automation.

    Class attributes:
        supports_auto_apply: Always ``False``. LinkedIn's terms prohibit automated
            application submission, so :meth:`apply` raises
            :class:`~app.jobs.base.UnsupportedFlowError` and every posting routes to manual
            review (``docs/CONTRACTS.md`` §9, golden rule #10).
        requires_login: ``True`` — for the *user*, in their own browser, when they apply.
            This provider holds no credentials and establishes no session.
    """

    meta: ClassVar[PluginMeta] = PluginMeta(
        kind=PluginKind.PROVIDER,
        name=ATSProviderName.LINKEDIN.value,
        version="1.0.0",
        display_name="LinkedIn",
        description=(
            "Reads jobs from your own LinkedIn data export or a public RSS/Atom feed. "
            "ApplicantOS never logs into, scrapes, or applies on LinkedIn: its terms of "
            "service prohibit it, so these postings are surfaced for manual review."
        ),
        author="ApplicantOS",
        capabilities=frozenset({"search", "fetch", "manual_review"}),
    )

    name: ClassVar[ATSProviderName] = ATSProviderName.LINKEDIN
    supports_auto_apply: ClassVar[bool] = False
    requires_login: ClassVar[bool] = True
    URL_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"\blinkedin\.com/jobs\b", re.IGNORECASE),
        re.compile(r"\blinkedin\.com/comm/jobs\b", re.IGNORECASE),
        re.compile(r"\blnkd\.in/", re.IGNORECASE),
    ]

    # -- discovery ----------------------------------------------------------------------

    async def search(self, q: SearchQuery) -> AsyncIterator[RawPosting]:
        """Yield postings from the user's export and from any configured public feed.

        Reads exactly two things, both named in ``q.extra``: ``export_path`` (a file, folder
        or ZIP the user downloaded from LinkedIn) and ``feed_url`` (a public RSS or Atom
        document). No request is ever made to LinkedIn's own servers from here, credentialed
        or otherwise, and no page is scraped.

        The query's filters are applied client-side, with one deliberate exception: keyword
        filtering is applied to feed entries but **not** to export rows. A saved-jobs export
        is a list the user built by hand, one job at a time — it is a stronger statement of
        intent than any keyword list, and silently discarding entries from it because they
        are titled "SWE II" rather than "Software Engineer" would throw away the best signal
        in the system.

        Args:
            q: What to look for.

        Yields:
            One :class:`~app.jobs.base.RawPosting` per posting, at most ``q.limit`` of them.
        """
        export_paths = _extra_values(q.extra, EXPORT_PATH_KEYS)
        feed_urls = _extra_values(q.extra, FEED_URL_KEYS)

        if not export_paths and not feed_urls:
            self.logger.info(
                "linkedin.no_source_configured",
                message=NO_SOURCE_MESSAGE,
                export_keys=list(EXPORT_PATH_KEYS),
                feed_keys=list(FEED_URL_KEYS),
            )
            return

        budget = q.limit
        produced = 0
        seen: set[str] = set()

        for location in export_paths:
            if produced >= budget:
                break
            for raw in await self._load_export(location):
                if not self._accept(raw, q, match_keywords=False) or raw.external_id in seen:
                    continue
                seen.add(raw.external_id)
                yield raw
                produced += 1
                if produced >= budget:
                    break

        for url in feed_urls:
            if produced >= budget:
                break
            for raw in await self._load_feed(url):
                if not self._accept(raw, q, match_keywords=True) or raw.external_id in seen:
                    continue
                seen.add(raw.external_id)
                yield raw
                produced += 1
                if produced >= budget:
                    break

        self.logger.info(
            "linkedin.search_finished",
            exports=len(export_paths),
            feeds=len(feed_urls),
            postings=produced,
        )

    def _accept(self, raw: RawPosting, q: SearchQuery, *, match_keywords: bool) -> bool:
        """Apply the query's client-side filters to one posting.

        Args:
            raw: The candidate posting.
            q: The query.
            match_keywords: Whether keyword filtering applies to this source.

        Returns:
            ``True`` when the posting survives every applicable filter. A filter whose input
            the posting does not carry — no stated location, no known arrangement — passes
            rather than rejects: absence of information is not evidence against a match.
        """
        if not q.matches_freshness(raw.posted_at):
            return False

        if q.remote_only and raw.work_arrangement in {
            WorkArrangement.ONSITE,
            WorkArrangement.HYBRID,
        }:
            return False

        if q.locations and raw.location:
            haystack = raw.location.casefold()
            if not any(entry.casefold() in haystack for entry in q.locations):
                return False

        if match_keywords and q.keywords:
            haystack = " ".join(
                part.casefold()
                for part in (raw.title, raw.company_name, raw.description or "")
                if part
            )
            if not any(keyword.casefold() in haystack for keyword in q.keywords):
                return False

        return True

    # -- the user's own export ----------------------------------------------------------

    async def _load_export(self, location: str) -> list[RawPosting]:
        """Read every job row out of one export path.

        Args:
            location: A path to a CSV file, a JSON file, an unpacked export folder, or the
                ZIP archive LinkedIn delivers.

        Returns:
            The postings found, possibly empty. File-system errors are logged and yield an
            empty list: a mistyped path must not end a discovery run that also has a feed
            configured.
        """
        path = Path(location).expanduser()
        try:
            documents = await asyncio.to_thread(self._read_export_documents, path)
        except (OSError, zipfile.BadZipFile) as exc:
            self.logger.warning("linkedin.export_unreadable", path=str(path), error=str(exc))
            return []

        if not documents:
            self.logger.warning("linkedin.export_empty", path=str(path))
            return []

        postings: list[RawPosting] = []
        skipped = 0
        for name, payload in documents:
            for row in _rows_from_document(name, payload):
                raw = self._row_to_posting(row, source=name)
                if raw is None:
                    skipped += 1
                    continue
                postings.append(raw)

        self.logger.info(
            "linkedin.export_read",
            path=str(path),
            documents=len(documents),
            postings=len(postings),
            skipped=skipped,
        )
        return postings

    def _read_export_documents(self, path: Path) -> list[tuple[str, str]]:
        """Collect the job tables inside an export path. Blocking; run in a worker thread.

        Args:
            path: The export file, folder or archive.

        Returns:
            ``(name, text)`` pairs, one per job table found.

        Raises:
            OSError: If the path cannot be read.
        """
        if not path.exists():
            raise FileNotFoundError(f"LinkedIn export not found: {path}")

        if path.is_dir():
            return [
                (candidate.name, self._read_text_file(candidate))
                for candidate in sorted(path.rglob("*"))
                if candidate.is_file() and _is_job_table(candidate.name)
            ]

        if zipfile.is_zipfile(path):
            return self._read_zip(path)

        if path.suffix.lower() not in _EXPORT_SUFFIXES:
            self.logger.debug("linkedin.export_suffix_unexpected", path=str(path))
        return [(path.name, self._read_text_file(path))]

    def _read_text_file(self, path: Path) -> str:
        """Read one export file as text, tolerating a byte-order mark.

        Args:
            path: The file.

        Returns:
            The decoded contents, truncated at :data:`MAX_EXPORT_BYTES`.

        Raises:
            OSError: If the file cannot be read.
        """
        payload = path.read_bytes()
        if len(payload) > MAX_EXPORT_BYTES:
            self.logger.warning(
                "linkedin.export_truncated", path=str(path), bytes=len(payload)
            )
            payload = payload[:MAX_EXPORT_BYTES]
        return _decode(payload)

    def _read_zip(self, path: Path) -> list[tuple[str, str]]:
        """Read the job tables out of a LinkedIn export archive.

        Args:
            path: The ZIP archive.

        Returns:
            ``(name, text)`` pairs. Entries are size-checked before extraction, so a
            maliciously or accidentally oversized member is skipped rather than decompressed.
        """
        documents: list[tuple[str, str]] = []
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir() or not _is_job_table(info.filename):
                    continue
                if info.file_size > MAX_EXPORT_BYTES:
                    self.logger.warning(
                        "linkedin.export_member_too_large",
                        path=str(path),
                        member=info.filename,
                        bytes=info.file_size,
                    )
                    continue
                with archive.open(info) as member:
                    documents.append((info.filename, _decode(member.read())))
        return documents

    def _row_to_posting(self, row: Mapping[str, Any], *, source: str) -> RawPosting | None:
        """Turn one export row into a posting.

        Args:
            row: The row, keyed by normalised header.
            source: The file the row came from, recorded in
                :attr:`~app.jobs.base.RawPosting.raw` so a parsing question can be traced
                back to its table.

        Returns:
            The posting, or ``None`` when the row names no job — an export contains several
            tables, and the ones that are not job tables have no title.
        """
        title = _pick(row, "title")
        url = _pick(row, "url")
        job_id = extract_job_id(url) or extract_job_id(_pick(row, "job_id"))
        if not title and not job_id:
            return None

        company = _pick(row, "company")
        description = _pick(row, "description") or None
        location = _pick(row, "location") or None
        posted_at = parse_date(_pick(row, "posted_at"))

        if not url and job_id:
            url = canonical_job_url(job_id)
        if not url:
            # No link and no identifier: the row cannot be opened, scored against a real
            # description, or deduplicated against another provider. Inventing a URL would
            # be worse than dropping it.
            return None

        arrangement = infer_arrangement(
            " ".join(part for part in (_pick(row, "workplace_type"), location or "") if part)
        )
        employment_type = infer_employment_type(
            title, " ".join(part for part in (_pick(row, "employment_type"), description or "") if part)
        )
        salary_min, salary_max, currency = parse_salary(description or "")

        return RawPosting(
            provider=ATSProviderName.LINKEDIN,
            external_id=job_id or _synthetic_id(company, title, url),
            url=url,
            title=title or UNTITLED_POSTING,
            company_name=company,
            description=description,
            location=location,
            work_arrangement=arrangement,
            employment_type=employment_type,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            posted_at=posted_at,
            apply_url=None,
            raw={"source": "linkedin_export", "document": source, "row": dict(row)},
        )

    # -- public feeds -------------------------------------------------------------------

    async def _load_feed(self, url: str) -> list[RawPosting]:
        """Fetch and parse one public RSS or Atom feed.

        Args:
            url: The feed URL, as the user supplied it.

        Returns:
            The postings found, possibly empty. A feed that is unreachable or unparseable is
            logged and skipped rather than failing the run.
        """
        if not self._feed_url_allowed(url):
            return []

        try:
            response = await self._request("GET", url)
        except ProviderError as exc:
            self.logger.warning("linkedin.feed_failed", url=url, error=str(exc))
            return []

        payload = response.content
        if len(payload) > MAX_FEED_BYTES:
            self.logger.warning("linkedin.feed_too_large", url=url, bytes=len(payload))
            return []

        try:
            # Parsed from bytes so the document's own encoding declaration is honoured, and
            # with the standard library's parser, which does not resolve external entities.
            root = ElementTree.fromstring(payload)
        except (ElementTree.ParseError, ValueError) as exc:
            self.logger.warning("linkedin.feed_unparseable", url=url, error=str(exc))
            return []

        postings: list[RawPosting] = []
        for entry in _feed_entries(root):
            raw = self._entry_to_posting(entry, feed_url=url)
            if raw is not None:
                postings.append(raw)

        self.logger.info("linkedin.feed_read", url=url, postings=len(postings))
        return postings

    def _feed_url_allowed(self, url: str) -> bool:
        """Return whether a feed URL may be fetched.

        Two things are refused. A scheme other than HTTP(S), because a job feed is a web
        document and resolving ``file:`` on behalf of configuration would read the local
        disk. And a URL carrying embedded credentials, because this provider must never make
        an authenticated request — that is the whole posture of the module.

        Args:
            url: The candidate feed URL.

        Returns:
            ``True`` when the URL is safe to fetch.
        """
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            self.logger.warning("linkedin.feed_rejected", url=url, reason=str(exc))
            return False

        if parsed.scheme.lower() not in _ALLOWED_FEED_SCHEMES:
            self.logger.warning(
                "linkedin.feed_rejected", url=url, reason="scheme_not_allowed",
                scheme=parsed.scheme,
            )
            return False
        if "@" in parsed.netloc:
            self.logger.warning(
                "linkedin.feed_rejected", url=url, reason="credentials_in_url"
            )
            return False
        return True

    def _entry_to_posting(
        self,
        entry: ElementTree.Element,
        *,
        feed_url: str,
    ) -> RawPosting | None:
        """Turn one feed entry into a posting.

        Args:
            entry: The ``<item>`` or ``<entry>`` element.
            feed_url: The feed it came from, recorded for traceability.

        Returns:
            The posting, or ``None`` when the entry has no title or no link.
        """
        headline = _child_text(entry, _FEED_TITLE_TAGS)
        link = _entry_link(entry)
        if not headline or not link:
            return None

        title, company_from_title = split_title_and_company(headline)
        company = _child_text(entry, _FEED_AUTHOR_TAGS) or company_from_title or ""
        body = _child_text(entry, _FEED_BODY_TAGS)
        description = html_to_text(body) or None
        location = (
            _child_text(entry, _FEED_LOCATION_TAGS) or _location_from_title(headline) or None
        )
        posted_at = parse_date(_child_text(entry, _FEED_DATE_TAGS))

        arrangement = infer_arrangement(
            " ".join(part for part in (title, location or "", description or "") if part)
        )
        salary_min, salary_max, currency = parse_salary(description or "")
        job_id = extract_job_id(link)

        return RawPosting(
            provider=ATSProviderName.LINKEDIN,
            external_id=job_id or _synthetic_id(company, title, link),
            url=canonical_job_url(job_id) if job_id else link,
            title=title,
            company_name=company,
            description=description,
            location=location,
            work_arrangement=arrangement,
            employment_type=infer_employment_type(title, description or ""),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            posted_at=posted_at,
            apply_url=link if job_id and link != canonical_job_url(job_id) else None,
            raw={"source": "public_feed", "feed_url": feed_url, "entry_title": headline},
        )

    # -- single posting -----------------------------------------------------------------

    async def fetch_posting(self, id_or_url: str) -> RawPosting | None:
        """Read one public posting's Open Graph metadata, and stop where the public part does.

        This reads exactly what LinkedIn serves to any link-preview crawler: the ``og:*``
        meta tags on a public job page. It sends no cookies, no authorization header and no
        credentials of any kind, and it makes a single attempt — retrying a refusal is
        exactly what a refusal is asking us not to do.

        A login wall, a challenge page, a ``403``, or LinkedIn's ``999`` bot-defence status
        all produce ``None``. Nothing is rotated, retried or disguised to get past them. The
        posting is not lost: the pipeline still holds its URL and the user opens it in their
        own browser, signed in as themselves.

        Args:
            id_or_url: A LinkedIn job URL, or the numeric posting identifier.

        Returns:
            The posting, or ``None`` when the identifier is unrecognised, the page is not
            public, or the metadata names no job.
        """
        text = clean_text(id_or_url)
        if not text:
            return None

        job_id = extract_job_id(text)
        if job_id:
            url = canonical_job_url(job_id)
        elif text.lower().startswith(("http://", "https://")):
            url = text
        else:
            self.logger.debug("linkedin.unrecognised_identifier", identifier=text[:80])
            return None

        try:
            response = await self._request("GET", url, max_attempts=1)
        except ProviderError as exc:
            if exc.status_code in _BLOCKED_STATUSES:
                self.logger.info(
                    "linkedin.posting_not_public", url=url, status_code=exc.status_code
                )
            else:
                self.logger.warning("linkedin.fetch_failed", url=url, error=str(exc))
            return None

        body = response.text[:_MAX_BODY_SCAN_CHARS]
        blocker = _detect_block(body)
        if blocker is not None:
            self.logger.info("linkedin.posting_gated", url=url, marker=blocker)
            return None

        metadata = parse_open_graph(body)
        return self._metadata_to_posting(metadata, url=url, job_id=job_id)

    def _metadata_to_posting(
        self,
        metadata: Mapping[str, str],
        *,
        url: str,
        job_id: str | None,
    ) -> RawPosting | None:
        """Turn Open Graph metadata into a posting.

        Args:
            metadata: The parsed meta tags.
            url: The URL that was fetched.
            job_id: The posting identifier, when one could be extracted from the URL.

        Returns:
            The posting, or ``None`` when the metadata names no job. Only the description is
            taken from the page body's metadata; nothing is read out of the rendered page,
            which is the line between a link preview and scraping.
        """
        headline = clean_text(metadata.get("og:title")) or clean_text(metadata.get("title"))
        title, company = split_title_and_company(headline)
        if not title:
            self.logger.debug("linkedin.metadata_without_title", url=url)
            return None

        description = (
            html_to_text(metadata.get("og:description") or metadata.get("description")) or None
        )
        location = _location_from_title(headline)
        canonical = clean_text(metadata.get("og:url")) or url
        salary_min, salary_max, currency = parse_salary(description or "")

        return RawPosting(
            provider=ATSProviderName.LINKEDIN,
            external_id=job_id or extract_job_id(canonical) or _synthetic_id(company or "", title, canonical),
            url=canonical,
            title=title,
            company_name=company or clean_text(metadata.get("og:site_name")) or "",
            description=description,
            location=location,
            work_arrangement=infer_arrangement(
                " ".join(part for part in (title, location or "", description or "") if part)
            ),
            employment_type=infer_employment_type(title, description or ""),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            posted_at=parse_date(metadata.get("article:published_time")),
            apply_url=None,
            raw={"source": "open_graph", "url": url, "metadata": dict(metadata)},
        )

    # -- posture ------------------------------------------------------------------------

    async def apply(self, ctx: ApplyContext) -> ApplyResult:
        """Refuse to submit, always, because LinkedIn's terms of service say so.

        LinkedIn's User Agreement prohibits automated application submission. ApplicantOS
        honours that: there is no setting that enables this, and no code path that could.
        The pipeline turns this exception into a ``needs_review`` application carrying
        :attr:`~app.models.enums.ReviewReason.UNSUPPORTED_FLOW` and the posting URL, so the
        user applies themselves — in their own browser, signed in as themselves, with the
        tailored resume this system already generated for them (``docs/CONTRACTS.md`` §9,
        golden rule #10).

        Args:
            ctx: The attempt context, used for the URL and the log correlation fields.

        Returns:
            Never returns.

        Raises:
            UnsupportedFlowError: Always.
        """
        self.logger.info("linkedin.manual_review_required", **ctx.log_context())
        raise UnsupportedFlowError(
            "LinkedIn's terms of service prohibit automated application submission, so "
            "ApplicantOS never applies there; apply manually at "
            f"{ctx.posting.target_url}",
            provider=self.provider_name,
            url=ctx.posting.target_url,
        )

    async def healthcheck(self) -> bool:
        """Report whether this provider is usable.

        Always ``True``, and deliberately without a network probe. There is no LinkedIn
        endpoint this system is entitled to poll on a schedule, and a readiness check that
        pinged one would be automated traffic against a service whose terms this module
        exists to respect. Its real dependencies — a file on disk, a feed the user chose —
        are checked when they are used, and a missing one is reported there.

        Returns:
            ``True``.
        """
        return True


# ======================================================================================
# Document helpers
# ======================================================================================


def _is_job_table(filename: str) -> bool:
    """Return whether an export member is one of the tables holding job rows.

    Args:
        filename: The member's name, possibly with directory components.

    Returns:
        ``True`` when the file's extension is readable and its stem matches a known job
        table. The rest of an export — connections, messages, profile — is deliberately not
        read: this provider wants the user's jobs and takes nothing else.
    """
    name = Path(filename).name
    suffix = Path(name).suffix.lower()
    if suffix not in _EXPORT_SUFFIXES:
        return False
    stem = _normalize_header(Path(name).stem)
    if not stem:
        return False
    return any(
        known in stem or (len(stem) >= _MIN_STEM_MATCH_LENGTH and stem in known)
        for known in _JOB_TABLE_STEMS
    )


def _rows_from_document(name: str, payload: str) -> Iterator[dict[str, Any]]:
    """Yield normalised rows from one export document.

    Args:
        name: The document's filename, which decides how it is parsed.
        payload: Its decoded contents.

    Yields:
        One dictionary per record, keyed by normalised header.
    """
    if Path(name).suffix.lower() == ".json":
        yield from _rows_from_json(name, payload)
        return
    yield from _rows_from_csv(name, payload)


def _rows_from_csv(name: str, payload: str) -> Iterator[dict[str, Any]]:
    """Yield normalised rows from a CSV export table.

    LinkedIn prefixes some tables with a "Notes:" preamble before the real header row, so
    parsing starts at the first line that looks like a header rather than at line one.

    Args:
        name: The document's filename, for logging.
        payload: The CSV text, already decoded and BOM-free.

    Yields:
        One dictionary per row.
    """
    text = _skip_preamble(payload)
    if not text.strip():
        return
    try:
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            if isinstance(row, Mapping):
                yield _normalize_row(row)
    except csv.Error as exc:
        logger.warning("linkedin.csv_unparseable", document=name, error=str(exc))


def _rows_from_json(name: str, payload: str) -> Iterator[dict[str, Any]]:
    """Yield normalised rows from a JSON export table.

    Args:
        name: The document's filename, for logging.
        payload: The JSON text.

    Yields:
        One dictionary per record. Both a bare array and an object wrapping one under any of
        :data:`_JSON_COLLECTION_KEYS` are accepted, because LinkedIn's JSON exports and the
        third-party tools that reshape them disagree about which to emit.
    """
    try:
        document = json.loads(payload)
    except ValueError as exc:
        logger.warning("linkedin.json_unparseable", document=name, error=str(exc))
        return

    records: Any = document
    if isinstance(document, Mapping):
        for key in _JSON_COLLECTION_KEYS:
            candidate = document.get(key)
            if isinstance(candidate, list):
                records = candidate
                break
        else:
            records = [document]

    if not isinstance(records, list):
        return
    for record in records:
        if isinstance(record, Mapping):
            yield _normalize_row(record)


def _skip_preamble(payload: str) -> str:
    """Drop any explanatory lines preceding a CSV table's header row.

    Several LinkedIn export tables open with a ``Notes:`` paragraph before the real header,
    and handing that to :class:`csv.DictReader` makes the first sentence the column names.
    The header is found by recognising it rather than by counting lines: the first line that
    carries at least :data:`_MIN_HEADER_MATCHES` known column names is it.

    Args:
        payload: The raw CSV text.

    Returns:
        The text from the header row onwards, or the original text unchanged when no line is
        recognisable — a table with unfamiliar column names must still be handed to the
        reader, which is the only thing that can tell whether it holds anything usable.
    """
    lines = payload.splitlines()
    for index, line in enumerate(lines):
        if "," not in line:
            continue
        matches = sum(1 for field in line.split(",") if _normalize_header(field) in _KNOWN_HEADERS)
        if matches >= _MIN_HEADER_MATCHES:
            return "\n".join(lines[index:])
    return payload


def _feed_entries(root: ElementTree.Element) -> list[ElementTree.Element]:
    """Collect every entry element in a feed document.

    Args:
        root: The parsed document root, an RSS ``<rss>``/``<channel>`` or an Atom ``<feed>``.

    Returns:
        The ``<item>``, ``<entry>`` or ``<job>`` elements, in document order.
    """
    return [element for element in root.iter() if _localname(element.tag) in _FEED_ENTRY_TAGS]


def _detect_block(body: str) -> str | None:
    """Return the block marker found in a response body, if any.

    Args:
        body: The response text, already truncated by the caller.

    Returns:
        The first marker present, or ``None`` when the page appears to be a real posting.
    """
    haystack = body.casefold()
    for marker in _BLOCK_MARKERS:
        if marker in haystack:
            return marker
    return None
