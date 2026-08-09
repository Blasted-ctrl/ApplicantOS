"""Shared parsing helpers for ATS provider plugins.

Every provider in ``app/jobs/`` receives the same job in a different shape: Greenhouse sends
HTML in a JSON string, Lever sends pre-split "lists", Ashby sends Markdown-ish HTML, Workday
sends a paginated JSON envelope with epoch milliseconds. This module is where those shapes
converge, so that a :class:`~app.jobs.base.RawPosting` produced by one provider is directly
comparable with one produced by another — which is the precondition for
:mod:`app.jobs.dedupe` working at all.

Six primitives, all pure, all synchronous, all total (they return a documented "I don't
know" value rather than raising on garbage input):

:func:`clean_text`
    Unicode hygiene. Everything else in this module runs its input through it first.
:func:`html_to_text`
    Markup to readable plain text, preserving paragraph structure.
:func:`parse_salary`
    Free-text compensation to ``(min, max, currency)``, annualised.
:func:`infer_arrangement`
    Remote / hybrid / onsite, honouring negations.
:func:`infer_employment_type`
    Internship / new grad / contract / part time / full time.
:func:`parse_date`
    Anything date-shaped to a timezone-aware UTC :class:`~datetime.datetime`.

**Nothing here guesses.** :class:`~app.models.enums.WorkArrangement.UNKNOWN`,
:class:`~app.models.enums.EmploymentType.UNKNOWN`, ``None`` and empty ranges are honest
answers that the scorer and the user's filters can act on; a wrong guess silently corrupts
both (golden rule #2).

The only optional dependency is ``selectolax``, used by :func:`html_to_text` when it is
importable. Its absence costs nothing: the stdlib fallback is a complete implementation, not
a degraded one, and both paths are exercised by the same tests.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser as _StdlibHTMLParser
from typing import Any, Final, Literal

import structlog

from app.models.enums import EmploymentType, WorkArrangement

__all__ = [
    "ANNUAL_HOURS",
    "MAX_PLAUSIBLE_ANNUAL_SALARY",
    "MIN_PLAUSIBLE_ANNUAL_SALARY",
    "clean_text",
    "html_to_text",
    "infer_arrangement",
    "infer_employment_type",
    "normalize_characters",
    "parse_date",
    "parse_salary",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Character normalisation
# ======================================================================================

#: Unicode normal form applied before anything else. NFKC folds compatibility characters
#: (full-width Latin, ligatures, superscripts) onto their ordinary equivalents, so a title
#: pasted out of a PDF compares equal to one typed into a form.
_UNICODE_NORMAL_FORM: Final[Literal["NFKC"]] = "NFKC"

#: Code points that occupy no visual space and carry no meaning in a job posting. Rich-text
#: editors sprinkle them through ATS feeds, where they silently defeat every string
#: comparison, content hash and dedupe key. Listed as numbers rather than literals on
#: purpose: a literal zero-width character in source is invisible to the next reader.
_ZERO_WIDTH_CODE_POINTS: Final[tuple[int, ...]] = (
    0x00AD,  # SOFT HYPHEN
    0x180E,  # MONGOLIAN VOWEL SEPARATOR
    0x200B,  # ZERO WIDTH SPACE
    0x200C,  # ZERO WIDTH NON-JOINER
    0x200D,  # ZERO WIDTH JOINER
    0x200E,  # LEFT-TO-RIGHT MARK
    0x200F,  # RIGHT-TO-LEFT MARK
    0x2060,  # WORD JOINER
    0x2061,  # FUNCTION APPLICATION
    0x2062,  # INVISIBLE TIMES
    0x2063,  # INVISIBLE SEPARATOR
    0x2064,  # INVISIBLE PLUS
    0xFEFF,  # ZERO WIDTH NO-BREAK SPACE (byte-order mark)
)

#: Space characters of every width, including the non-breaking ones ATS feeds are full of.
_SPACE_CODE_POINTS: Final[tuple[int, ...]] = (
    0x00A0,  # NO-BREAK SPACE
    0x1680,  # OGHAM SPACE MARK
    0x2000,  # EN QUAD
    0x2001,  # EM QUAD
    0x2002,  # EN SPACE
    0x2003,  # EM SPACE
    0x2004,  # THREE-PER-EM SPACE
    0x2005,  # FOUR-PER-EM SPACE
    0x2006,  # SIX-PER-EM SPACE
    0x2007,  # FIGURE SPACE
    0x2008,  # PUNCTUATION SPACE
    0x2009,  # THIN SPACE
    0x200A,  # HAIR SPACE
    0x202F,  # NARROW NO-BREAK SPACE
    0x205F,  # MEDIUM MATHEMATICAL SPACE
    0x3000,  # IDEOGRAPHIC SPACE
)

#: Hyphens, dashes and the minus sign, folded to a plain ASCII hyphen. Load-bearing:
#: salary-range detection and title normalisation both split on "-".
_DASH_CODE_POINTS: Final[tuple[int, ...]] = (
    0x2010,  # HYPHEN
    0x2011,  # NON-BREAKING HYPHEN
    0x2012,  # FIGURE DASH
    0x2013,  # EN DASH
    0x2014,  # EM DASH
    0x2015,  # HORIZONTAL BAR
    0x2212,  # MINUS SIGN
    0xFE58,  # SMALL EM DASH
    0xFE63,  # SMALL HYPHEN-MINUS
    0xFF0D,  # FULLWIDTH HYPHEN-MINUS
)

#: Single quotes, apostrophes and primes, folded to "'".
_SINGLE_QUOTE_CODE_POINTS: Final[tuple[int, ...]] = (
    0x00B4,  # ACUTE ACCENT
    0x2018,  # LEFT SINGLE QUOTATION MARK
    0x2019,  # RIGHT SINGLE QUOTATION MARK
    0x201A,  # SINGLE LOW-9 QUOTATION MARK
    0x201B,  # SINGLE HIGH-REVERSED-9 QUOTATION MARK
    0x2032,  # PRIME
    0x2035,  # REVERSED PRIME
    0xFF07,  # FULLWIDTH APOSTROPHE
)

#: Double quotes and guillemets, folded to '"'.
_DOUBLE_QUOTE_CODE_POINTS: Final[tuple[int, ...]] = (
    0x00AB,  # LEFT-POINTING DOUBLE ANGLE QUOTATION MARK
    0x00BB,  # RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK
    0x201C,  # LEFT DOUBLE QUOTATION MARK
    0x201D,  # RIGHT DOUBLE QUOTATION MARK
    0x201E,  # DOUBLE LOW-9 QUOTATION MARK
    0x201F,  # DOUBLE HIGH-REVERSED-9 QUOTATION MARK
    0x2033,  # DOUBLE PRIME
    0x2036,  # REVERSED DOUBLE PRIME
    0xFF02,  # FULLWIDTH QUOTATION MARK
)

#: List bullets, folded to a hyphen so that markers survive as readable plain text.
_BULLET_CODE_POINTS: Final[tuple[int, ...]] = (
    0x00B7,  # MIDDLE DOT
    0x2022,  # BULLET
    0x2023,  # TRIANGULAR BULLET
    0x2043,  # HYPHEN BULLET
    0x2219,  # BULLET OPERATOR
    0x25AA,  # BLACK SMALL SQUARE
    0x25CF,  # BLACK CIRCLE
    0x25E6,  # WHITE BULLET
)

#: HORIZONTAL ELLIPSIS, expanded rather than dropped so a truncated sentence still reads.
_ELLIPSIS_CODE_POINT: Final[int] = 0x2026

#: FRACTION SLASH, folded to "/" so that "$60\u2044hr" parses like "$60/hr".
_FRACTION_SLASH_CODE_POINT: Final[int] = 0x2044

#: Translation table applied character-by-character by :func:`normalize_characters`. Every
#: exotic form collapses onto its ASCII counterpart, so that an en-dash title and a
#: hyphen title normalise identically and therefore dedupe against each other.
_CHARACTER_TRANSLATIONS: Final[dict[int, str | None]] = {
    **{point: None for point in _ZERO_WIDTH_CODE_POINTS},
    **{point: " " for point in _SPACE_CODE_POINTS},
    **{point: "-" for point in _DASH_CODE_POINTS},
    **{point: "'" for point in _SINGLE_QUOTE_CODE_POINTS},
    **{point: '"' for point in _DOUBLE_QUOTE_CODE_POINTS},
    **{point: "-" for point in _BULLET_CODE_POINTS},
    _ELLIPSIS_CODE_POINT: "...",
    _FRACTION_SLASH_CODE_POINT: "/",
    0x0009: " ",  # TAB
    0x000B: "\n",  # LINE TABULATION
    0x000C: "\n",  # FORM FEED
    0x000D: "\n",  # CARRIAGE RETURN
}

#: Control characters that survive translation (everything in Unicode category ``Cc``
#: except the newline we deliberately keep).
_CONTROL_CHARS_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")

#: Runs of any whitespace, for the single-line collapse in :func:`clean_text`.
_WHITESPACE_RUN_RE: Final[re.Pattern[str]] = re.compile(r"\s+")

#: Runs of horizontal whitespace only, for the line-preserving collapse in
#: :func:`html_to_text`.
_HORIZONTAL_SPACE_RUN_RE: Final[re.Pattern[str]] = re.compile(r"[^\S\n]+")

#: Three or more consecutive newlines, collapsed to a single blank line.
_BLANK_LINE_RUN_RE: Final[re.Pattern[str]] = re.compile(r"\n{3,}")


def normalize_characters(value: Any) -> str:
    """Apply Unicode hygiene to *value* without touching its line structure.

    NFKC normalisation, deletion of zero-width characters, folding of exotic spaces, dashes,
    quotes and bullets onto ASCII, and removal of control characters. Newlines survive, so
    this is the right primitive for text whose paragraphs matter; :func:`clean_text` builds
    the single-line version on top of it.

    Args:
        value: Any object. Non-strings are stringified; ``None`` yields ``""``.

    Returns:
        The normalised text, not stripped and not whitespace-collapsed.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if not text:
        return ""
    normalized = unicodedata.normalize(_UNICODE_NORMAL_FORM, text)
    translated = normalized.translate(_CHARACTER_TRANSLATIONS)
    return _CONTROL_CHARS_RE.sub("", translated)


def clean_text(s: Any) -> str:
    """Reduce *s* to a single normalised line.

    Runs :func:`normalize_characters`, then collapses every whitespace run — newlines
    included — to one space and strips the ends. This is the canonical form used for titles,
    company names, locations and every value that feeds a comparison or a hash, because two
    strings that differ only in invisible characters must never produce two database rows.

    Args:
        s: Any object. Non-strings are stringified; ``None`` yields ``""``.

    Returns:
        The cleaned single-line string, possibly empty.

    Example:
        >>> clean_text("  Senior\\u00a0Engineer \\u2013 Platform\\u200b ")
        'Senior Engineer - Platform'
    """
    normalized = normalize_characters(s)
    if not normalized:
        return ""
    return _WHITESPACE_RUN_RE.sub(" ", normalized).strip()


def _tidy_block_text(raw: str) -> str:
    """Normalise extracted document text while keeping its paragraph structure.

    Args:
        raw: Text assembled by one of the two :func:`html_to_text` back ends.

    Returns:
        Text with horizontal whitespace collapsed per line, trailing spaces removed, runs of
        blank lines reduced to one, and the whole thing stripped.
    """
    normalized = normalize_characters(raw)
    collapsed = _HORIZONTAL_SPACE_RUN_RE.sub(" ", normalized)
    lines = [line.strip() for line in collapsed.split("\n")]
    return _BLANK_LINE_RUN_RE.sub("\n\n", "\n".join(lines)).strip()


# ======================================================================================
# HTML to text
# ======================================================================================

#: Elements whose entire subtree is discarded: executable code, styling, metadata and
#: embedded media. Their text content is never part of a job description.
_DROP_TAGS: Final[frozenset[str]] = frozenset(
    {
        "audio",
        "canvas",
        "embed",
        "head",
        "iframe",
        "link",
        "map",
        "meta",
        "noscript",
        "object",
        "script",
        "style",
        "svg",
        "template",
        "title",
        "video",
    }
)

#: Elements that begin a new line of output. Everything not listed here (``span``, ``a``,
#: ``b``, ``em``, ``code``, …) is treated as inline, so ``<p>Hello <b>world</b></p>``
#: renders as one line rather than three.
_BLOCK_TAGS: Final[frozenset[str]] = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "html",
        "legend",
        "li",
        "main",
        "nav",
        "ol",
        "option",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)

#: Elements after which a blank line is emitted, so paragraphs and headings stay visually
#: separated in the stored description.
_PARAGRAPH_TAGS: Final[frozenset[str]] = frozenset(
    {"blockquote", "div", "h1", "h2", "h3", "h4", "h5", "h6", "p", "pre", "section", "table"}
)

#: The tag selectolax reports for a text node.
_SELECTOLAX_TEXT_TAG: Final[str] = "-text"

#: Cheap probe for "does this string contain markup at all?". A description that is already
#: plain text is returned untouched rather than run through a parser that would, for
#: example, eat an unescaped ``<`` in "C++ <string>".
_MARKUP_PROBE_RE: Final[re.Pattern[str]] = re.compile(r"<[a-zA-Z!/?]|&[#a-zA-Z][a-zA-Z0-9]{1,31};")


class _TextExtractor(_StdlibHTMLParser):
    """Stdlib HTML-to-text back end, used when ``selectolax`` is not installed.

    A complete implementation, not a degraded one: it drops the same subtrees, honours the
    same block/inline distinction, and produces byte-identical output to the ``selectolax``
    path for well-formed input.

    Attributes:
        parts: Accumulated text fragments and line breaks, joined by :meth:`result`.
    """

    def __init__(self) -> None:
        """Create an extractor with entity conversion enabled."""
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._drop_depth: int = 0

    # -- tag handling ------------------------------------------------------------------

    def _emit_break(self, tag: str) -> None:
        """Append the line break *tag* introduces, if any.

        The same rule the ``selectolax`` back end applies while traversing, kept in one place
        so the two implementations cannot drift.

        Args:
            tag: Lowercased element name.
        """
        if tag in _PARAGRAPH_TAGS:
            self.parts.append("\n\n")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Open a tag: begin dropping, or emit the appropriate line break.

        Args:
            tag: Lowercased element name.
            attrs: Element attributes (unused; part of the base-class signature).
        """
        if tag in _DROP_TAGS:
            self._drop_depth += 1
            return
        if not self._drop_depth:
            self._emit_break(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Handle a self-closing tag such as ``<br/>``.

        Args:
            tag: Lowercased element name.
            attrs: Element attributes (unused; part of the base-class signature).
        """
        if tag in _DROP_TAGS or self._drop_depth:
            return
        self._emit_break(tag)

    def handle_endtag(self, tag: str) -> None:
        """Close a tag, resuming collection when a dropped subtree ends.

        No break is emitted here. Line breaks are produced on tag *entry* only, in both back
        ends, because ``selectolax`` exposes a pre-order traversal with no exit hook — and
        two installations of ApplicantOS that disagreed about where the newlines go would
        compute different :func:`app.jobs.dedupe.content_hash` values for the same posting.

        Args:
            tag: Lowercased element name.
        """
        if tag in _DROP_TAGS:
            self._drop_depth = max(self._drop_depth - 1, 0)

    def handle_data(self, data: str) -> None:
        """Collect text that is not inside a dropped subtree.

        Args:
            data: Character data with entity references already resolved.
        """
        if self._drop_depth or not data:
            return
        self.parts.append(data)

    def error(self, message: str) -> None:
        """Ignore a malformed-markup report.

        ``html.parser`` inherited this hook from a long-removed strict mode; the modern
        parser never calls it, but overriding it keeps a subclass from ever inheriting an
        abstract-looking method that raises.

        Args:
            message: The parser's complaint.
        """
        logger.debug("parsing.html_malformed", detail=message)

    def result(self) -> str:
        """Return the accumulated text, before normalisation."""
        return "".join(self.parts)


def _html_to_text_selectolax(html: str) -> str | None:
    """Extract text using ``selectolax`` when it is importable.

    Args:
        html: The markup to convert.

    Returns:
        The raw extracted text, or ``None`` when ``selectolax`` is unavailable or the
        document could not be parsed — in which case the caller falls back to the stdlib
        implementation.
    """
    try:
        from selectolax.parser import HTMLParser
    except ImportError:
        return None

    try:
        tree = HTMLParser(html)
        tree.strip_tags(sorted(_DROP_TAGS))
        root = tree.body or tree.root
        if root is None:
            return ""
        parts: list[str] = []
        for node in root.traverse(include_text=True):
            tag = node.tag
            if tag == _SELECTOLAX_TEXT_TAG:
                parts.append(node.text_content or "")
            elif tag in _PARAGRAPH_TAGS:
                parts.append("\n\n")
            elif tag in _BLOCK_TAGS:
                parts.append("\n")
        return "".join(parts)
    except Exception as exc:
        logger.warning("parsing.selectolax_failed", error=str(exc))
        return None


def html_to_text(html: Any) -> str:
    """Convert an HTML fragment or document to readable plain text.

    Script, style, metadata and embedded-media subtrees are discarded; block elements start
    a new line and paragraph-level elements are separated by a blank line, so the stored
    description keeps the structure a human reads it by. Entity references are resolved and
    the result is run through the same Unicode hygiene as every other string in the system.

    ``selectolax`` is used when installed (it is roughly an order of magnitude faster on the
    100 KB descriptions Workday emits) and the stdlib :mod:`html.parser` otherwise. Both
    back ends produce the same output; neither ever raises.

    Args:
        html: Markup, or text that merely might be markup. Non-strings are stringified;
            ``None`` yields ``""``.

    Returns:
        Plain text with paragraph structure preserved.

    Example:
        >>> html_to_text("<p>Build <b>things</b>.</p><ul><li>Python</li><li>Rust</li></ul>")
        'Build things.\\n\\nPython\\nRust'
    """
    if html is None:
        return ""
    source = html if isinstance(html, str) else str(html)
    if not source.strip():
        return ""
    if not _MARKUP_PROBE_RE.search(source):
        # Plain text already. Parsing it would risk eating stray angle brackets from code
        # samples, which job descriptions are full of.
        return _tidy_block_text(source)

    extracted = _html_to_text_selectolax(source)
    if extracted is None:
        extractor = _TextExtractor()
        try:
            extractor.feed(source)
            extractor.close()
        except Exception as exc:
            logger.warning("parsing.html_parse_failed", error=str(exc))
        extracted = extractor.result()
    return _tidy_block_text(extracted)


# ======================================================================================
# Salary parsing
# ======================================================================================

#: Working hours in a year, the US convention (40 h/week x 52 weeks). Used to annualise an
#: hourly rate so that every stored ``salary_min``/``salary_max`` is comparable and the
#: user's ``min_salary`` preference means one thing.
ANNUAL_HOURS: Final[int] = 2080

#: Working days in a year (5 x 52), for day rates.
ANNUAL_WORKING_DAYS: Final[int] = 260

#: Weeks in a year, for weekly rates.
WEEKS_PER_YEAR: Final[int] = 52

#: Months in a year, for monthly rates.
MONTHS_PER_YEAR: Final[int] = 12

#: Below this, an annualised figure is not a salary — it is a headcount, a year, a street
#: number or a bonus percentage that survived extraction.
MIN_PLAUSIBLE_ANNUAL_SALARY: Final[int] = 10_000

#: Above this, an "annualised" figure is a funding round, a revenue target or a mis-parsed
#: identifier. Ten million is comfortably above the highest advertised base salary.
MAX_PLAUSIBLE_ANNUAL_SALARY: Final[int] = 10_000_000

#: Multiplier per pay period, applied before the plausibility check.
_PERIOD_MULTIPLIERS: Final[dict[str, int]] = {
    "hour": ANNUAL_HOURS,
    "day": ANNUAL_WORKING_DAYS,
    "week": WEEKS_PER_YEAR,
    "month": MONTHS_PER_YEAR,
    "year": 1,
}

#: Pay-period detection, most specific first. Matched against the cleaned, lowercased text.
_PERIOD_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("hour", re.compile(r"(?:/\s*(?:hr|hour)s?\b|\bper\s+hour\b|\ban\s+hour\b|\bhourly\b)")),
    ("day", re.compile(r"(?:/\s*days?\b|\bper\s+day\b|\bdaily\s+rate\b|\ba\s+day\b)")),
    ("week", re.compile(r"(?:/\s*(?:wk|week)s?\b|\bper\s+week\b|\bweekly\b|\ba\s+week\b)")),
    ("month", re.compile(r"(?:/\s*(?:mo|month)s?\b|\bper\s+month\b|\bmonthly\b|\ba\s+month\b)")),
    ("year", re.compile(r"(?:/\s*(?:yr|year)s?\b|\bper\s+year\b|\bannual(?:ly)?\b|\bp\.?a\.?\b)")),
)

#: Currency symbols mapped to ISO 4217 codes, scanned longest-first so that ``C$`` is not
#: read as a bare ``$``.
_CURRENCY_SYMBOLS: Final[tuple[tuple[str, str], ...]] = (
    ("US$", "USD"),
    ("CA$", "CAD"),
    ("AU$", "AUD"),
    ("NZ$", "NZD"),
    ("HK$", "HKD"),
    ("NT$", "TWD"),
    ("C$", "CAD"),
    ("A$", "AUD"),
    ("S$", "SGD"),
    ("R$", "BRL"),
    ("₨", "INR"),
    ("$", "USD"),
    ("€", "EUR"),
    ("£", "GBP"),
    ("¥", "JPY"),
    ("₹", "INR"),
    ("₩", "KRW"),
    ("₪", "ILS"),
    ("₺", "TRY"),
    ("₽", "RUB"),
    ("zł", "PLN"),
    ("CHF", "CHF"),
)

#: ISO 4217 codes recognised when spelled out. Checked before symbols, because
#: ``"120k-150k USD"`` carries no symbol at all.
_CURRENCY_CODES: Final[frozenset[str]] = frozenset(
    {
        "AED",
        "AUD",
        "BRL",
        "CAD",
        "CHF",
        "CNY",
        "DKK",
        "EUR",
        "GBP",
        "HKD",
        "ILS",
        "INR",
        "JPY",
        "KRW",
        "MXN",
        "NOK",
        "NZD",
        "PLN",
        "SEK",
        "SGD",
        "TRY",
        "TWD",
        "USD",
        "ZAR",
    }
)

#: ISO code as a standalone word.
_CURRENCY_CODE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(" + "|".join(sorted(_CURRENCY_CODES)) + r")\b"
)

#: Retirement-plan references. ``401(k)`` and ``403(b)`` appear in the benefits paragraph of
#: almost every US posting and would otherwise be extracted as "401 thousand".
_RETIREMENT_PLAN_RE: Final[re.Pattern[str]] = re.compile(
    r"\b40[13]\s*(?:\(\s*[kb]\s*\)|[kb])\b", re.IGNORECASE
)

#: Percentages — equity, bonus targets, 401(k) match rates. Never compensation amounts.
_PERCENTAGE_RE: Final[re.Pattern[str]] = re.compile(r"\d+(?:[.,]\d+)?\s*%")

#: One monetary amount: grouped thousands, a plain or decimal number, and an optional
#: ``k``/``m`` magnitude suffix that must not be the first letter of a following word.
_AMOUNT_RE: Final[re.Pattern[str]] = re.compile(
    r"""
    (?P<number>
        \d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?   # 120,000 / 90.000 / 1.234.567,89
      | \d+(?:[.,]\d+)?                          # 120 / 60.50
    )
    \s*
    (?P<suffix>[kKmM])?(?![A-Za-z0-9])
    """,
    re.VERBOSE,
)

#: Separator between the two ends of a range. Currency symbols and codes are allowed inside
#: it so that ``"$120,000 - $150,000"`` and ``"120k to 150k USD"`` both match.
_RANGE_SEPARATOR_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:-{1,2}|to|through|up\s+to|and)\s*(?:[$€£¥₹₩₪₺₽]|[A-Z]{3}\s+)?\s*$",
    re.IGNORECASE,
)

#: Whatever may sit between an open-ended hint and the number it qualifies: whitespace, a
#: currency symbol, an ISO code, or nothing. Without this, ``"up to £85,000"`` would lose its
#: hint to the intervening ``£`` and be stored as a closed band.
_HINT_GAP: Final[str] = r"\s*(?:[$€£¥₹₩₪₺₽]|[A-Z]{3})?\s*$"

#: Phrases meaning "this number is the top of the band".
_CEILING_HINT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:up\s+to|maximum\s+of|max\.?|as\s+much\s+as|no\s+more\s+than|under)" + _HINT_GAP,
    re.IGNORECASE,
)

#: Phrases meaning "this number is the bottom of the band".
_FLOOR_HINT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:from|starting\s+(?:at|from)|starts\s+at|at\s+least|minimum\s+of|min\.?|"
    r"upwards\s+of|north\s+of|\+)" + _HINT_GAP,
    re.IGNORECASE,
)

#: How far back a floor/ceiling hint is looked for, in characters.
_HINT_WINDOW: Final[int] = 24

#: Grouped-thousands shape, e.g. ``120,000`` or ``1.234.567``.
_GROUPED_THOUSANDS_RE: Final[re.Pattern[str]] = re.compile(r"\d{1,3}(?:[.,]\d{3})+")

#: Grouped thousands followed by a decimal fraction, e.g. ``1.234.567,89``.
_GROUPED_WITH_DECIMALS_RE: Final[re.Pattern[str]] = re.compile(r"\d{1,3}(?:[.,]\d{3})+[.,]\d{1,2}")

#: A plain decimal with one or two fractional digits, e.g. ``60.50``.
_SIMPLE_DECIMAL_RE: Final[re.Pattern[str]] = re.compile(r"\d+[.,]\d{1,2}")

#: Magnitude applied by a ``k`` / ``m`` suffix.
_SUFFIX_MULTIPLIERS: Final[dict[str, int]] = {"k": 1_000, "m": 1_000_000}


def _parse_number(text: str) -> float:
    """Interpret one numeric token, resolving thousands separators from its shape.

    Both ``,`` and ``.`` are used as a thousands separator somewhere in the world, so the
    grouping is inferred from the digit pattern rather than assumed: three digits after the
    last separator means grouping (``90.000`` is ninety thousand euros), one or two mean a
    fraction (``60.50`` is sixty dollars fifty).

    Args:
        text: The matched number, without any magnitude suffix.

    Returns:
        The numeric value.
    """
    if _GROUPED_WITH_DECIMALS_RE.fullmatch(text):
        # The last separator is the decimal point (it is followed by one or two digits);
        # every earlier one is a thousands separator.
        index = max(text.rfind("."), text.rfind(","))
        head, fraction = text[:index], text[index + 1 :]
        return float(re.sub(r"[.,]", "", head) + "." + fraction)
    if _GROUPED_THOUSANDS_RE.fullmatch(text):
        return float(re.sub(r"[.,]", "", text))
    if _SIMPLE_DECIMAL_RE.fullmatch(text):
        return float(text.replace(",", "."))
    return float(re.sub(r"[.,]", "", text))


def _detect_currency(text: str) -> str | None:
    """Identify the currency a compensation string is quoted in.

    Args:
        text: The cleaned compensation text.

    Returns:
        An ISO 4217 code, or ``None`` when the text names no currency. ``None`` is not
        assumed to mean USD: an unlabelled range from a European board would be silently
        mis-scored.
    """
    code_match = _CURRENCY_CODE_RE.search(text.upper())
    if code_match:
        return code_match.group(1)
    for symbol, code in _CURRENCY_SYMBOLS:
        if symbol in text:
            return code
    return None


def _detect_period(text: str) -> str:
    """Identify the pay period a compensation string is quoted in.

    Args:
        text: The cleaned compensation text.

    Returns:
        One of the keys of :data:`_PERIOD_MULTIPLIERS`; ``"year"`` when nothing indicates
        otherwise, which is how compensation is advertised by default.
    """
    lowered = text.lower()
    for period, pattern in _PERIOD_PATTERNS:
        if pattern.search(lowered):
            return period
    return "year"


def _annualize(value: float, period: str) -> int:
    """Convert a per-period amount to an annual one.

    Args:
        value: The advertised amount.
        period: A key of :data:`_PERIOD_MULTIPLIERS`.

    Returns:
        The annualised amount, rounded to the nearest whole unit.
    """
    return round(value * _PERIOD_MULTIPLIERS.get(period, 1))


def _plausible(amount: int) -> bool:
    """Return whether *amount* is credible as an annual salary."""
    return MIN_PLAUSIBLE_ANNUAL_SALARY <= amount <= MAX_PLAUSIBLE_ANNUAL_SALARY


def parse_salary(text: Any) -> tuple[int | None, int | None, str | None]:
    """Extract an annualised compensation range and its currency from free text.

    Handles the shapes ATS feeds actually emit: symbol-prefixed ranges
    (``"$120,000 - $150,000"``), magnitude suffixes (``"120k-150k USD"``), hourly and
    monthly rates (``"$60/hr"``), European grouping (``"€90.000"``), en- and em-dash ranges,
    open-ended bands (``"up to $150,000"``, ``"from $100k"``), and single values.

    Everything is annualised before it is returned, using
    :data:`ANNUAL_HOURS` for hourly rates and the corresponding constants for day, week and
    month rates, so a stored range always means the same thing and the user's ``min_salary``
    preference compares like with like. An annualisation is logged at debug level, because
    an inferred figure is worth being able to audit.

    Amounts outside :data:`MIN_PLAUSIBLE_ANNUAL_SALARY` … :data:`MAX_PLAUSIBLE_ANNUAL_SALARY`
    are discarded rather than stored, as are retirement-plan references (``401(k)``) and
    percentages, which otherwise dominate a benefits paragraph.

    Args:
        text: Any object; typically a compensation line or a whole job description.

    Returns:
        ``(minimum, maximum, currency)``. Any element may be ``None``: an open-ended band
        yields one bound, an unlabelled amount yields no currency, and text with no credible
        figure yields ``(None, None, None)``. A single value is returned as both bounds.

    Example:
        >>> parse_salary("$120,000 - $150,000")
        (120000, 150000, 'USD')
        >>> parse_salary("$60/hr")
        (124800, 124800, 'USD')
        >>> parse_salary("up to €90.000 per year")
        (None, 90000, 'EUR')
    """
    cleaned = clean_text(text)
    if not cleaned:
        return (None, None, None)

    currency = _detect_currency(cleaned)
    period = _detect_period(cleaned)

    # Remove the two constructs that reliably produce false positives, replacing them with
    # spaces so that character offsets used for hint detection stay meaningful.
    scrubbed = _RETIREMENT_PLAN_RE.sub(lambda m: " " * len(m.group(0)), cleaned)
    scrubbed = _PERCENTAGE_RE.sub(lambda m: " " * len(m.group(0)), scrubbed)

    matches = list(_AMOUNT_RE.finditer(scrubbed))
    if not matches:
        return (None, None, currency)

    candidates: list[tuple[int, int, int]] = []  # (annualised, start, end)
    for match in matches:
        raw_value = _parse_number(match.group("number"))
        suffix = match.group("suffix")
        if suffix:
            raw_value *= _SUFFIX_MULTIPLIERS[suffix.lower()]
        annual = _annualize(raw_value, period)
        if _plausible(annual):
            candidates.append((annual, match.start(), match.end()))

    if not candidates:
        return (None, None, currency)

    minimum: int | None
    maximum: int | None

    if len(candidates) >= 2:
        first, second = candidates[0], candidates[1]
        between = scrubbed[first[2] : second[1]]
        if _RANGE_SEPARATOR_RE.match(between):
            minimum, maximum = first[0], second[0]
            if minimum > maximum:
                minimum, maximum = maximum, minimum
            _log_annualization(period, cleaned, minimum, maximum)
            return (minimum, maximum, currency)

    value, start, _end = candidates[0]
    preceding = scrubbed[max(0, start - _HINT_WINDOW) : start]
    if _CEILING_HINT_RE.search(preceding):
        minimum, maximum = None, value
    elif _FLOOR_HINT_RE.search(preceding):
        minimum, maximum = value, None
    else:
        minimum, maximum = value, value
    _log_annualization(period, cleaned, minimum, maximum)
    return (minimum, maximum, currency)


def _log_annualization(
    period: str,
    source: str,
    minimum: int | None,
    maximum: int | None,
) -> None:
    """Record that a non-annual rate was converted, so the figure can be audited.

    Args:
        period: The detected pay period.
        source: The text the figures came from.
        minimum: The annualised lower bound, if any.
        maximum: The annualised upper bound, if any.
    """
    if period == "year":
        return
    logger.debug(
        "parsing.salary_annualized",
        period=period,
        multiplier=_PERIOD_MULTIPLIERS.get(period, 1),
        salary_min=minimum,
        salary_max=maximum,
        source=source[:120],
    )


# ======================================================================================
# Work arrangement
# ======================================================================================

#: Phrases that mean the role is at least partly in an office on a fixed cadence. Checked
#: first: "Hybrid - 3 days remote" is hybrid, not remote.
_HYBRID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:hybrid"
    r"|partially\s+remote|part(?:ly|ial)?[-\s]remote"
    r"|remote[-\s]hybrid|flex(?:ible)?\s+hybrid"
    r"|\d\s*(?:\+\s*)?days?\s+(?:a|per|each)?\s*week\s+in\s+(?:the\s+)?office"
    r"|\d\s*(?:\+\s*)?days?\s+in[-\s](?:the\s+)?office"
    r"|\d\s*(?:\+\s*)?days?\s+on[-\s]?site"
    r"|in\s+(?:the\s+)?office\s+\d\s*days?"
    r")\b",
    re.IGNORECASE,
)

#: Phrases that mean the role is performed away from an office.
_REMOTE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:remote(?:ly|\s*-?\s*first|\s*-?\s*friendly)?"
    r"|work\s+from\s+home|wfh|telecommut(?:e|ing)"
    r"|distributed\s+team|work\s+from\s+anywhere)\b",
    re.IGNORECASE,
)

#: Phrases that mean the role is performed at an employer site.
_ONSITE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:on[-\s]?site|in[-\s]?office|in[-\s]person|on[-\s]premises?"
    r"|office[-\s]based|relocation\s+(?:is\s+)?required)\b",
    re.IGNORECASE,
)

#: Negation cues that, when they precede a remote mention closely enough, invert it.
_NEGATION_BEFORE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:no|not|non|never|without|isn't|aren't|cannot|can't|excludes?|"
    r"unavailable\s+for|ineligible\s+for|does\s+not\s+(?:offer|support|allow))\b"
    r"[^.;!?]{0,30}$",
    re.IGNORECASE,
)

#: Negation cues that follow a remote mention, e.g. "remote work is not available".
_NEGATION_AFTER_RE: Final[re.Pattern[str]] = re.compile(
    r"^[^.;!?]{0,30}?\b(?:is\s+not|are\s+not|not\s+(?:available|offered|an\s+option|"
    r"possible|supported|permitted)|unavailable|prohibited)\b",
    re.IGNORECASE,
)


def infer_arrangement(text: Any) -> WorkArrangement:
    """Classify where a role is performed.

    Precedence is deliberate. A hybrid marker wins outright, because a posting that says
    both "hybrid" and "remote" is describing a hybrid role. Otherwise a *non-negated* remote
    mention wins. Negation is checked in both directions — ``"no remote"``,
    ``"not a remote role"``, ``"remote work is not available"`` — and a remote mention that
    is entirely negated resolves to :attr:`~app.models.enums.WorkArrangement.ONSITE`, since
    that is exactly what the sentence says.

    Args:
        text: Any object; typically a job description, a location string, or both joined.

    Returns:
        The inferred arrangement, or :attr:`~app.models.enums.WorkArrangement.UNKNOWN` when
        the text says nothing about it. ``UNKNOWN`` is an honest answer and never a guess:
        the scorer and the user's ``remote_only`` filter both handle it explicitly.

    Example:
        >>> infer_arrangement("Fully remote, US time zones")
        <WorkArrangement.REMOTE: 'remote'>
        >>> infer_arrangement("This is not a remote role.")
        <WorkArrangement.ONSITE: 'onsite'>
    """
    cleaned = clean_text(text)
    if not cleaned:
        return WorkArrangement.UNKNOWN

    if _HYBRID_RE.search(cleaned):
        return WorkArrangement.HYBRID

    remote_mentions = list(_REMOTE_RE.finditer(cleaned))
    affirmed_remote = False
    for mention in remote_mentions:
        before = cleaned[max(0, mention.start() - 60) : mention.start()]
        after = cleaned[mention.end() : mention.end() + 60]
        negated = bool(_NEGATION_BEFORE_RE.search(before)) or bool(_NEGATION_AFTER_RE.search(after))
        if not negated:
            affirmed_remote = True
            break

    if affirmed_remote:
        return WorkArrangement.REMOTE
    if _ONSITE_RE.search(cleaned):
        return WorkArrangement.ONSITE
    if remote_mentions:
        # Every remote mention was negated, which is a positive statement about the role.
        return WorkArrangement.ONSITE
    return WorkArrangement.UNKNOWN


# ======================================================================================
# Employment type
# ======================================================================================

#: Internship markers. ``\bintern\b`` rather than ``intern`` so that "internal tooling" and
#: "international" do not classify a staff role as an internship.
_INTERNSHIP_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:intern|interns|internship|internships|co[-\s]?op|summer\s+analyst|"
    r"industrial\s+placement|placement\s+year|apprentice(?:ship)?|trainee)\b",
    re.IGNORECASE,
)

#: New-graduate / early-career programme markers.
_NEW_GRAD_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:new\s+grad(?:uate)?s?|recent\s+grad(?:uate)?s?|university\s+grad(?:uate)?s?|"
    r"college\s+grad(?:uate)?s?|campus\s+hire|graduate\s+(?:program|programme|scheme|role)|"
    r"early\s+career|entry[-\s]level\s+grad(?:uate)?|class\s+of\s+20\d{2})\b",
    re.IGNORECASE,
)

#: Contract / temporary markers. The bare word "contract" is excluded because it appears in
#: "employment contract" and "contract negotiation"; a qualifier is required.
_CONTRACT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:contractor|contract\s+(?:role|position|basis|work|opportunity|hire|to\s+hire)|"
    r"contract[-\s]to[-\s]perm|fixed[-\s]term|temp(?:orary)?\s+(?:role|position|assignment)|"
    r"freelance|1099|c2c|corp[-\s]to[-\s]corp|statement\s+of\s+work|"
    r"consultant\s+(?:role|position)|w2\s+contract)\b",
    re.IGNORECASE,
)

#: Part-time markers.
_PART_TIME_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:part[-\s]?time|parttime|0\.[1-9]\s*fte|(?:1[0-9]|2[0-9]|30)\s*(?:-|to)?\s*"
    r"\d{0,2}\s*hours?\s+(?:per|a|each)\s+week)\b",
    re.IGNORECASE,
)

#: Full-time markers.
_FULL_TIME_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:full[-\s]?time|fulltime|permanent(?:\s+(?:role|position|employee))?|"
    r"1\.0\s*fte|40\s*hours?\s+(?:per|a|each)\s+week)\b",
    re.IGNORECASE,
)

#: The classifiers, in the order they are consulted. The first match wins, which is why the
#: specific programme types precede the generic hours-based ones: an internship is also
#: full-time, and reporting it as full-time would lose the information that matters.
_EMPLOYMENT_TYPE_RULES: Final[tuple[tuple[re.Pattern[str], EmploymentType], ...]] = (
    (_INTERNSHIP_RE, EmploymentType.INTERNSHIP),
    (_NEW_GRAD_RE, EmploymentType.NEW_GRAD),
    (_CONTRACT_RE, EmploymentType.CONTRACT),
    (_PART_TIME_RE, EmploymentType.PART_TIME),
    (_FULL_TIME_RE, EmploymentType.FULL_TIME),
)


def _classify_employment(candidate: str) -> EmploymentType | None:
    """Run the employment-type rules over one string.

    Args:
        candidate: Cleaned text to classify.

    Returns:
        The first matching type, or ``None`` when nothing matched.
    """
    if not candidate:
        return None
    for pattern, employment_type in _EMPLOYMENT_TYPE_RULES:
        if pattern.search(candidate):
            return employment_type
    return None


def infer_employment_type(title: Any, text: Any = "") -> EmploymentType:
    """Classify the employment arrangement a posting advertises.

    The title is consulted first and on its own. Titles are curated by the employer and are
    where the arrangement is stated deliberately ("Software Engineer Intern", "Data
    Scientist — Contract"); descriptions mention every other arrangement in passing ("this
    is not an internship", "unlike our contract roles"). Only when the title is silent does
    the description get a vote.

    Args:
        title: The advertised role title.
        text: The job description, optional.

    Returns:
        The inferred type, or :attr:`~app.models.enums.EmploymentType.UNKNOWN` when neither
        the title nor the description says.

    Example:
        >>> infer_employment_type("Software Engineering Intern, Summer 2026")
        <EmploymentType.INTERNSHIP: 'internship'>
        >>> infer_employment_type("Backend Engineer", "This is a full-time permanent role.")
        <EmploymentType.FULL_TIME: 'full_time'>
    """
    cleaned_title = clean_text(title)
    from_title = _classify_employment(cleaned_title)
    if from_title is not None:
        return from_title

    cleaned_text = clean_text(text)
    from_text = _classify_employment(cleaned_text)
    if from_text is not None:
        return from_text
    return EmploymentType.UNKNOWN


# ======================================================================================
# Date parsing
# ======================================================================================

#: Above this, a numeric timestamp is milliseconds rather than seconds. 1e11 seconds is the
#: year 5138 and 1e11 milliseconds is 1973, so the split is unambiguous for any real feed.
_EPOCH_MILLISECONDS_THRESHOLD: Final[float] = 1e11

#: Above this, a numeric timestamp is microseconds.
_EPOCH_MICROSECONDS_THRESHOLD: Final[float] = 1e14

#: Sanity bounds on a parsed year. A posting dated 1900 or 2400 is a parse failure, not a
#: posting; returning ``None`` lets the caller fall back to "unknown" instead of poisoning
#: the freshness filter.
_MIN_PLAUSIBLE_YEAR: Final[int] = 1990
_MAX_PLAUSIBLE_YEAR: Final[int] = 2100

#: Human-readable formats, tried in order. US month/day ordering precedes the European
#: reading because every ATS in scope publishes from a US-hosted tenant; the ambiguity is
#: unresolvable from the string alone and this is the documented tie-break.
_DATE_FORMATS: Final[tuple[str, ...]] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%B %d, %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%Y%m%d",
)

#: A bare integer or decimal, i.e. an epoch timestamp expressed as a string.
_NUMERIC_STRING_RE: Final[re.Pattern[str]] = re.compile(r"^-?\d+(?:\.\d+)?$")


def _as_utc(value: dt.datetime) -> dt.datetime | None:
    """Attach or convert a timezone so the result is unambiguously UTC.

    Args:
        value: A parsed datetime, aware or naive.

    Returns:
        The instant in UTC, or ``None`` when its year is implausible.
    """
    aware = value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value
    utc = aware.astimezone(dt.UTC)
    if not _MIN_PLAUSIBLE_YEAR <= utc.year <= _MAX_PLAUSIBLE_YEAR:
        return None
    return utc


def _from_epoch(number: float) -> dt.datetime | None:
    """Interpret a numeric timestamp, detecting its unit from magnitude.

    Args:
        number: Seconds, milliseconds or microseconds since the Unix epoch.

    Returns:
        The instant in UTC, or ``None`` when it is out of range.
    """
    magnitude = abs(number)
    if magnitude >= _EPOCH_MICROSECONDS_THRESHOLD:
        seconds = number / 1_000_000.0
    elif magnitude >= _EPOCH_MILLISECONDS_THRESHOLD:
        seconds = number / 1_000.0
    else:
        seconds = float(number)
    try:
        return _as_utc(dt.datetime.fromtimestamp(seconds, tz=dt.UTC))
    except (OSError, OverflowError, ValueError):
        return None


def parse_date(value: Any) -> dt.datetime | None:
    """Parse anything date-shaped into a timezone-aware UTC datetime.

    Accepts, in the order they are tried:

    * an existing :class:`~datetime.datetime` (converted; a naive one is read as UTC) or
      :class:`~datetime.date` (midnight UTC);
    * an epoch timestamp as ``int``/``float`` or a numeric string, in seconds, milliseconds
      or microseconds — the unit is inferred from magnitude, which is what makes a Workday
      feed and a Greenhouse feed both parse without provider-specific code;
    * ISO 8601, including the ``Z`` suffix and fractional seconds;
    * RFC 2822, as used by RSS and email-derived feeds;
    * the common human formats listed in :data:`_DATE_FORMATS`.

    Args:
        value: The raw value from a provider payload.

    Returns:
        A timezone-aware UTC datetime, or ``None`` when *value* is empty, unparseable, or
        lands outside :data:`_MIN_PLAUSIBLE_YEAR` … :data:`_MAX_PLAUSIBLE_YEAR`. Never
        raises — a provider feed is untrusted input.

    Example:
        >>> parse_date("2026-02-14T09:30:00Z").isoformat()
        '2026-02-14T09:30:00+00:00'
        >>> parse_date(1770000000000).tzinfo is not None
        True
    """
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return _as_utc(value)
    if isinstance(value, dt.date):
        return _as_utc(dt.datetime(value.year, value.month, value.day))
    if isinstance(value, bool):
        # ``bool`` is an ``int``; a boolean is never a timestamp.
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))

    text = clean_text(value)
    if not text:
        return None

    if _NUMERIC_STRING_RE.match(text):
        try:
            return _from_epoch(float(text))
        except ValueError:
            return None

    iso_candidate = text
    if iso_candidate.endswith(("Z", "z")):
        iso_candidate = f"{iso_candidate[:-1]}+00:00"
    try:
        return _as_utc(dt.datetime.fromisoformat(iso_candidate))
    except ValueError:
        pass

    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        parsed = None
    if parsed is not None:
        return _as_utc(parsed)

    for date_format in _DATE_FORMATS:
        try:
            return _as_utc(dt.datetime.strptime(text, date_format))
        except ValueError:
            continue

    logger.debug("parsing.date_unrecognised", value=text[:64])
    return None
