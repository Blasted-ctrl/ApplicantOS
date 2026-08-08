"""Parsing an existing resume — the fastest way to bootstrap the knowledge base.

Onboarding cannot begin with an empty graph. A user who already has a resume should be able
to hand it over and immediately see their own experience, education, projects and skills as
structured knowledge — and then never edit that PDF again, because from this point on the
resume is a *generated view* of the graph (golden rule #6).

The parse is four passes, each of which degrades independently:

1. **Text.** ``.pdf`` via ``pypdf``, ``.docx`` via ``python-docx`` (paragraphs *and* tables
   — a large minority of resumes are laid out in an invisible table and yield nothing at all
   from a paragraphs-only reader), ``.md``/``.txt``/``.html`` directly. All of it goes
   through :mod:`app.knowledge.analyzers.document`, which is the one file reader in the
   system. A PDF with no text layer says so out loud rather than producing an empty graph.
2. **Contact block.** The lines above the first section heading yield name, email, phone,
   city/state and LinkedIn/GitHub/portfolio links. They are attached to the document's
   metadata so onboarding can prefill the profile from an uploaded resume.
3. **Sections.** Headings are matched against :data:`SECTION_ALIASES` with wide tolerance for
   case, punctuation, markdown markers, ``HEADING: inline content``, and the letter-spaced
   ``E X P E R I E N C E`` that PDF extraction produces.
4. **Entries and bullets.** Within experience-shaped sections, the organization / role /
   location / date-range line is parsed in the layouts resumes actually use, and the bullets
   beneath it are attributed to it.

Facts go through :class:`~app.knowledge.extractors.KnowledgeExtractor`, which validates every
model-produced claim against the source text and falls back to the deterministic rule-based
path whenever no model is configured — so a resume parses completely with zero API keys.

Nothing here invents anything. A field the resume does not state is left ``None``; a date
that is not written down is not guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final

import structlog

from app.knowledge.analyzers.base import (
    AnalysisResult,
    Analyzer,
    ExtractedDocument,
    ExtractedEdge,
    ExtractedEntity,
    ExtractedFact,
    SourceRef,
    SourceUnavailableError,
    compute_fingerprint,
)
from app.knowledge.analyzers.document import (
    extract_text_from_path,
    fetch_text_from_url,
    file_probe_fingerprint,
    knowledge_extractor,
    local_path_for,
    looks_like_url,
)
from app.knowledge.extractors import (
    canonical_skill,
    classify_skills,
    extract_dates,
    extract_metrics,
    extract_skills,
    score_impact,
    skill_entity_kind,
)
from app.models.enums import EntityKind, FactKind, PluginKind, RelationKind, SourceKind
from app.plugins.base import PluginMeta
from app.plugins.registry import plugin

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.config.settings import Settings
    from app.knowledge.extractors import KnowledgeExtractor

__all__ = [
    "ENTRY_SECTIONS",
    "SECTION_ALIASES",
    "SECTION_FACT_KINDS",
    "ContactBlock",
    "ResumeEntry",
    "ResumeParser",
    "ResumeSection",
    "parse_contact_block",
    "parse_entries",
    "segment_sections",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Section vocabulary
# ======================================================================================

#: Canonical section → every heading that means it. Matched case- and punctuation-
#: insensitively after normalisation, so ``"TECHNICAL SKILLS:"``, ``"Technical Skills"`` and
#: ``"## technical skills"`` all resolve to ``SKILLS``.
SECTION_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "SUMMARY": (
        "summary",
        "professional summary",
        "executive summary",
        "profile",
        "professional profile",
        "about",
        "about me",
        "objective",
        "career objective",
        "overview",
        "personal statement",
    ),
    "EXPERIENCE": (
        "experience",
        "work experience",
        "professional experience",
        "employment",
        "employment history",
        "work history",
        "relevant experience",
        "industry experience",
        "internships",
        "internship experience",
        "internship",
        "engineering experience",
        "research experience",
        "career history",
        "professional background",
        "work",
        "relevant work experience",
        "technical experience",
    ),
    "EDUCATION": (
        "education",
        "academic background",
        "academics",
        "education and training",
        "educational background",
        "degrees",
        "academic history",
    ),
    "PROJECTS": (
        "projects",
        "personal projects",
        "technical projects",
        "selected projects",
        "project experience",
        "side projects",
        "engineering projects",
        "academic projects",
        "notable projects",
        "portfolio",
        "featured projects",
        "independent projects",
    ),
    "SKILLS": (
        "skills",
        "technical skills",
        "core competencies",
        "competencies",
        "technologies",
        "technical proficiencies",
        "proficiencies",
        "tools and technologies",
        "areas of expertise",
        "expertise",
        "technical expertise",
        "skills and tools",
        "tools",
        "technical skills and tools",
        "software and tools",
    ),
    "LEADERSHIP": (
        "leadership",
        "leadership experience",
        "leadership and activities",
        "activities",
        "extracurricular activities",
        "extracurriculars",
        "involvement",
        "campus involvement",
        "volunteer experience",
        "volunteering",
        "community service",
        "service",
        "organizations",
        "clubs",
        "leadership and involvement",
        "activities and leadership",
    ),
    "AWARDS": (
        "awards",
        "honors",
        "honors and awards",
        "awards and honors",
        "achievements",
        "accomplishments",
        "recognition",
        "scholarships",
        "awards and recognition",
        "honors and recognition",
    ),
    "PUBLICATIONS": (
        "publications",
        "papers",
        "research publications",
        "conference papers",
        "presentations",
        "patents",
        "publications and presentations",
        "research",
    ),
    "CERTIFICATIONS": (
        "certifications",
        "certificates",
        "licenses",
        "licenses and certifications",
        "certifications and licenses",
        "credentials",
        "training",
        "professional development",
    ),
    "LANGUAGES": ("languages", "language proficiency", "spoken languages", "language skills"),
    "COURSEWORK": (
        "coursework",
        "relevant coursework",
        "relevant courses",
        "courses",
        "selected coursework",
        "key coursework",
    ),
    "INTERESTS": ("interests", "hobbies", "interests and hobbies", "personal interests"),
}

#: Fact category emitted for each section. ``CERTIFICATIONS`` maps to ``AWARD`` because
#: ``docs/CONTRACTS.md`` §3 defines no ``certification`` :class:`FactKind`; the certification
#: itself is still a first-class ``certification`` entity. Recorded in
#: ``docs/OPEN_QUESTIONS.md``.
SECTION_FACT_KINDS: Final[dict[str, FactKind]] = {
    "EXPERIENCE": FactKind.ACCOMPLISHMENT,
    "PROJECTS": FactKind.ACCOMPLISHMENT,
    "EDUCATION": FactKind.EDUCATION_ITEM,
    "COURSEWORK": FactKind.EDUCATION_ITEM,
    "LEADERSHIP": FactKind.LEADERSHIP_ITEM,
    "AWARDS": FactKind.AWARD,
    "CERTIFICATIONS": FactKind.AWARD,
    "PUBLICATIONS": FactKind.PUBLICATION_ITEM,
    "SKILLS": FactKind.SKILL_USAGE,
}

#: Sections whose content is organised as dated entries with bullets beneath them.
ENTRY_SECTIONS: Final[frozenset[str]] = frozenset(
    {"EXPERIENCE", "PROJECTS", "EDUCATION", "LEADERSHIP"}
)

#: Sections deliberately not turned into facts. A summary is self-description rather than a
#: verifiable claim ("motivated engineer seeking..."), and interests are not resume bullets;
#: both remain in the document text, where retrieval can still reach them.
_NON_FACT_SECTIONS: Final[frozenset[str]] = frozenset({"SUMMARY", "INTERESTS"})

#: Sections whose *content* is written as ``Label: item, item`` lines. Inside one of these,
#: ``"Languages: C, C++, Python"`` is a skills category — not the start of a LANGUAGES
#: section — so the inline-heading form is suppressed there. Without this, a technical
#: skills block silently turns every programming language into a spoken language.
_LABELLED_CONTENT_SECTIONS: Final[frozenset[str]] = frozenset(
    {"SKILLS", "LANGUAGES", "COURSEWORK", "INTERESTS"}
)

#: Canonical section for each recognised heading, built once from :data:`SECTION_ALIASES`.
_SECTION_LOOKUP: Final[dict[str, str]] = {
    alias: canonical
    for canonical, aliases in SECTION_ALIASES.items()
    for alias in (canonical.lower(), *aliases)
}

#: Longest a line may be and still be considered a section heading.
MAX_HEADING_CHARS: Final[int] = 60

#: Most lines above the first heading treated as the contact block.
MAX_CONTACT_LINES: Final[int] = 12

#: Most header lines gathered before an entry's bullets begin.
_MAX_ENTRY_HEADER_LINES: Final[int] = 3

#: Longest name an entity extracted from a resume line may have.
_MAX_ENTITY_NAME_CHARS: Final[int] = 90

#: Confidence for a field read directly off the page.
_PARSED_CONFIDENCE: Final[float] = 0.85

#: Confidence for a fact assembled from a resume line without model assistance.
_LINE_FACT_CONFIDENCE: Final[float] = 0.8

#: Domain-separation tag for the resume fingerprint of a fetched (rather than local) resume.
_RESUME_FINGERPRINT_TAG: Final[str] = "analyzer.resume.v1"


# ======================================================================================
# Line-level patterns
# ======================================================================================

_EMAIL: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

#: US and international phone shapes, with optional country code and extension.
_PHONE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\d\-])(?:\+?\d{1,3}[\s.\-]?)?"
    r"(?:\(\d{3}\)|\d{3})[\s.\-]?\d{3}[\s.\-]?\d{4}"
    r"(?:\s*(?:x|ext\.?)\s*\d{1,5})?(?![\d\-])"
)

_LINKEDIN: Final[re.Pattern[str]] = re.compile(
    r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/(?:in|pub|profile)/[A-Za-z0-9_%\-./]+",
    re.IGNORECASE,
)
_GITHUB: Final[re.Pattern[str]] = re.compile(
    r"(?:https?://)?(?:www\.)?github\.(?:com|io)/[A-Za-z0-9_\-./]*",
    re.IGNORECASE,
)
_URL: Final[re.Pattern[str]] = re.compile(
    r"(?:https?://|www\.)[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+", re.IGNORECASE
)
#: A bare domain used as a portfolio link ("alexchen.dev", "portfolio.io/alex").
_BARE_DOMAIN: Final[re.Pattern[str]] = re.compile(
    r"\b[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"\.(?:dev|io|com|net|org|me|xyz|app|tech|page|site|co)\b(?:/[A-Za-z0-9\-._~/]*)?"
)

#: ``City, ST`` / ``City, State`` / ``City, Country``, plus the arrangement words that stand
#: in for a place on a modern resume.
_LOCATION: Final[re.Pattern[str]] = re.compile(
    r"^(?P<city>[A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,2}),\s*"
    r"(?P<region>[A-Z]{2}|[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\.?$"
)
_LOCATION_TAIL: Final[re.Pattern[str]] = re.compile(
    r",\s*(?P<location>[A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,2},\s*"
    r"(?:[A-Z]{2}|[A-Z][a-z]+))\.?$"
)
_ARRANGEMENT_WORDS: Final[frozenset[str]] = frozenset(
    {"remote", "hybrid", "on-site", "onsite", "in-person"}
)

#: One date atom, in every shape a resume header uses.
_DATE_ATOM_TEXT: Final[str] = (
    r"(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?:of\s+)?(?:19|20)\d{2}"
    r"|(?:spring|summer|fall|autumn|winter)\s+(?:of\s+)?(?:19|20)\d{2}"
    r"|\d{1,2}[/-](?:19|20)\d{2}"
    r"|(?:19|20)\d{2})"
)

#: A whole date range as it is written on the page, so it can be cut out of a header line
#: before the remaining pieces are split on separators. Without this step, the ``-`` in
#: "Jun 2024 - Aug 2024" would be mistaken for a column separator.
_DATE_RANGE_TEXT: Final[re.Pattern[str]] = re.compile(
    rf"{_DATE_ATOM_TEXT}"
    rf"(?:\s*(?:-{{1,2}}|–|—|~|to|through|thru|until|till)\s*"
    rf"(?:present|current|currently|ongoing|now|today|{_DATE_ATOM_TEXT}))?",
    re.IGNORECASE,
)

#: Bullet markers seen on resumes, including the ones PDF extraction emits.
_BULLET_LINE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:[-*+•‣▪▫◦·●○▸►▶*·]\s*|\(?\d{1,2}[.)]\s+|[oO]\s{2,})"
)

#: Column separators inside an entry header.
_HEADER_SPLIT: Final[re.Pattern[str]] = re.compile(r"\s*[|•·‖]\s*|\t+|\s{2,}|\s+[–—]+\s+|\s+-\s+")

#: Words that identify a piece of a header line as a job title.
_ROLE_KEYWORDS: Final[tuple[str, ...]] = (
    "engineer",
    "engineering",
    "developer",
    "programmer",
    "scientist",
    "analyst",
    "intern",
    "internship",
    "co-op",
    "coop",
    "manager",
    "director",
    "lead",
    "leader",
    "president",
    "vice president",
    "treasurer",
    "secretary",
    "captain",
    "chair",
    "founder",
    "co-founder",
    "consultant",
    "designer",
    "architect",
    "researcher",
    "research assistant",
    "teaching assistant",
    "assistant",
    "associate",
    "specialist",
    "technician",
    "administrator",
    "coordinator",
    "supervisor",
    "instructor",
    "tutor",
    "mentor",
    "volunteer",
    "apprentice",
    "fellow",
    "officer",
    "member",
    "ambassador",
    "operator",
    "machinist",
    "trainee",
    "contractor",
    "freelance",
    "head",
)

#: Words that identify a piece of a header line as a school.
_SCHOOL_KEYWORDS: Final[tuple[str, ...]] = (
    "university",
    "college",
    "institute",
    "school",
    "academy",
    "polytechnic",
    "conservatory",
    "seminary",
    "universität",
    "universite",
    "universidad",
)

#: Words that identify a piece of a header line as a degree.
_DEGREE_KEYWORDS: Final[tuple[str, ...]] = (
    "bachelor",
    "master",
    "doctor",
    "doctorate",
    "associate",
    "diploma",
    "certificate",
    "b.s",
    "bs.",
    "b.sc",
    "bsc",
    "b.a",
    "ba.",
    "m.s",
    "ms.",
    "m.sc",
    "msc",
    "m.eng",
    "meng",
    "b.eng",
    "beng",
    "mba",
    "ph.d",
    "phd",
    "high school diploma",
    "minor in",
    "major in",
    "candidate",
)

#: Splits a comma/slash-separated list into items ("C, C++, Python" / "English / Spanish").
_LIST_SPLIT: Final[re.Pattern[str]] = re.compile(r"\s*[,;/|·•]\s*|\s+and\s+")

#: ``Category: item, item`` on a skills line.
_LABELLED_LINE: Final[re.Pattern[str]] = re.compile(r"^\s*(?P<label>[^:]{2,40}):\s*(?P<body>.+)$")

#: Where a run-in heading ends and its content begins. ``:`` covers ``"SKILLS: Python"``;
#: ``|`` covers the table-laid-out resume, whose heading cell and content cell arrive on one
#: line (see :func:`~app.knowledge.analyzers.document.read_docx_text`).
_INLINE_HEADING_BOUNDARY: Final[re.Pattern[str]] = re.compile(r"\s*[:|]\s*")

#: Trailing separators trimmed from an extracted entity name.
_NAME_TRIM: Final[str] = " \t.,;:|-–—•*"

#: Where an entity name stops inside a free-form item line.
_NAME_STOP: Final[re.Pattern[str]] = re.compile(r"\s*[|•·]|\s+[–—]\s+|\s+-\s+|\s*\(|,\s")

#: ``GPA: 3.91/4.00`` and its variants, which resumes put on the degree line.
_GPA: Final[re.Pattern[str]] = re.compile(
    r"\bGPA\b[:\s]*(?P<gpa>[0-5](?:\.\d{1,3})?)\s*(?:/\s*(?P<scale>[0-5](?:\.\d{1,2})?))?",
    re.IGNORECASE,
)

_WHITESPACE_RUN: Final[re.Pattern[str]] = re.compile(r"\s+")
_HEADING_NOISE: Final[re.Pattern[str]] = re.compile(r"[^a-z ]+")

#: Markdown decoration stripped before a line is considered as a heading. List markers are
#: deliberately *not* included: ``"- Relevant coursework: ..."`` is a bullet inside the
#: education section, and treating it as a heading would move its content into a section of
#: its own. ``**BOLD**`` survives because :data:`_BULLET_LINE` requires a space after ``*``.
_HEADING_PREFIX: Final[re.Pattern[str]] = re.compile(r"^[#>*\s]+")


# ======================================================================================
# Parsed shapes
# ======================================================================================


@dataclass(slots=True)
class ContactBlock:
    """The header block of a resume — who this is and how to reach them.

    Attached verbatim to the document's metadata so
    :class:`~app.services.onboarding_service.OnboardingService` can prefill the user profile
    from an uploaded resume instead of asking for details the user already typed once.

    Attributes:
        name: Full name, when the top of the resume reads like one.
        email: First email address found.
        phone: First phone number found, as written.
        location: ``City, ST``-shaped location, or an arrangement word ("Remote").
        linkedin: LinkedIn profile URL.
        github: GitHub profile URL.
        portfolio: Personal site or portfolio URL.
        websites: Every other URL found in the block.
        lines: The raw header lines, kept for auditing what was parsed out of them.
    """

    name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    linkedin: str | None = None
    github: str | None = None
    portfolio: str | None = None
    websites: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-ready mapping stored on the document's metadata.

        Returns:
            Every field, including the ones that are ``None`` — an explicit null is what
            tells onboarding "the resume does not say", which is different from "not parsed".
        """
        return {
            "name": self.name,
            "email": self.email,
            "phone": self.phone,
            "location": self.location,
            "linkedin": self.linkedin,
            "github": self.github,
            "portfolio": self.portfolio,
            "websites": list(self.websites),
            "lines": list(self.lines),
        }

    @property
    def is_empty(self) -> bool:
        """Whether nothing identifying was recovered."""
        return not any((self.name, self.email, self.phone, self.linkedin, self.github))


@dataclass(slots=True)
class ResumeEntry:
    """One dated item within a section: a job, a degree, a project, a leadership role.

    Attributes:
        organization: Employer, school, club or project name.
        role: Title held, or the degree earned.
        location: Where it happened.
        date_start: Period start, normalised to ``YYYY-MM`` or ``YYYY``.
        date_end: Period end, or ``None`` for ongoing work.
        date_text: The date range exactly as the resume wrote it.
        gpa: Grade point average, exactly as written ("3.91/4.00"), when the entry states
            one. Kept because it is a field employers filter on and it never survives being
            treated as a nameless extra column.
        header_lines: The raw lines the fields above were parsed from.
        bullets: The claims beneath the header, markers removed.
    """

    organization: str | None = None
    role: str | None = None
    location: str | None = None
    date_start: str | None = None
    date_end: str | None = None
    date_text: str | None = None
    gpa: str | None = None
    header_lines: list[str] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """Whether this entry carries neither a header nor any bullet."""
        return not (self.organization or self.role or self.bullets)

    def body(self) -> str:
        """Return the entry's bullets as one block of text, for fact extraction.

        Returns:
            One bullet per line, marker-free.
        """
        return "\n".join(f"- {bullet}" for bullet in self.bullets)


@dataclass(slots=True)
class ResumeSection:
    """One heading and everything under it.

    Attributes:
        name: Canonical section name from :data:`SECTION_ALIASES`, or ``"OTHER"`` for a
            heading the vocabulary does not know.
        heading: The heading exactly as the resume wrote it.
        lines: The content lines beneath it, blank lines removed.
    """

    name: str
    heading: str
    lines: list[str] = field(default_factory=list)

    def text(self) -> str:
        """Return the section's content as one block of text."""
        return "\n".join(self.lines)


# ======================================================================================
# Normalisation helpers
# ======================================================================================


def _despace(text: str) -> str:
    """Undo the letter spacing PDF extraction produces for display-styled headings.

    ``"E X P E R I E N C E"`` and ``"W O R K   E X P E R I E N C E"`` are what a PDF with
    tracked-out capitals extracts as; both must be recognised as headings. Groups separated
    by two or more spaces are treated as words, and a group whose every token is a single
    character is closed up.

    Args:
        text: One line.

    Returns:
        The line with letter spacing removed, or the line unchanged when it has none.
    """
    groups = re.split(r"\s{2,}", text.strip())
    rebuilt: list[str] = []
    for group in groups:
        tokens = group.split()
        if len(tokens) >= 2 and all(len(token) == 1 for token in tokens):
            rebuilt.append("".join(tokens))
        else:
            rebuilt.append(group)
    return " ".join(part for part in rebuilt if part)


def _normalize_heading(line: str) -> str:
    """Reduce a candidate heading to its comparable form.

    Args:
        line: One line of the resume.

    Returns:
        Lowercase words only, with markdown markers, digits, punctuation and letter spacing
        removed. ``&`` becomes ``and``, so ``"Leadership & Activities"`` and
        ``"Leadership and Activities"`` are one heading rather than two.
    """
    text = _HEADING_PREFIX.sub("", line.strip())
    text = _despace(text).casefold().replace("&", " and ")
    text = _HEADING_NOISE.sub(" ", text)
    return " ".join(text.split())


def _detect_heading(line: str, *, inside: str | None = None) -> tuple[str, str] | None:
    """Return the section a line announces, if it announces one.

    Handles a bare heading, a heading with trailing punctuation or rule characters, and the
    run-in forms where the content shares the heading's line — ``"SKILLS: Python, C++"`` and
    the ``"AWARDS | Eagle Scout, 2021"`` that a table-laid-out resume produces when a heading
    cell and a content cell sit in the same row. A bullet is never a heading, and the run-in
    form is suppressed inside a section whose own content is written as ``Label: items`` —
    see :data:`_LABELLED_CONTENT_SECTIONS`.

    Args:
        line: One line of the resume.
        inside: Canonical name of the section currently being filled, or ``None`` when the
            parser is still in the header block.

    Returns:
        ``(canonical section, inline content)`` — the content being ``""`` for a bare
        heading — or ``None`` when the line is not a heading.
    """
    stripped = line.strip()
    if not stripped or (inside is not None and _is_bullet(stripped)):
        return None

    if len(stripped) <= MAX_HEADING_CHARS:
        canonical = _SECTION_LOOKUP.get(_normalize_heading(stripped))
        if canonical:
            return (canonical, "")

    if inside in _LABELLED_CONTENT_SECTIONS:
        return None

    boundary = _INLINE_HEADING_BOUNDARY.search(stripped)
    if boundary is None or boundary.start() > MAX_HEADING_CHARS:
        return None
    body = stripped[boundary.end() :].strip()
    if not body:
        return None
    canonical = _SECTION_LOOKUP.get(_normalize_heading(stripped[: boundary.start()]))
    return (canonical, body) if canonical else None


def _is_bullet(line: str) -> bool:
    """Return whether a line carries a bullet marker.

    Args:
        line: One line of the resume.

    Returns:
        ``True`` when the line begins with a list marker.
    """
    return _BULLET_LINE.match(line) is not None


def _strip_bullet(line: str) -> str:
    """Return a bullet's text with its marker removed.

    Args:
        line: A bullet line.

    Returns:
        The claim, whitespace-collapsed.
    """
    return _WHITESPACE_RUN.sub(" ", _BULLET_LINE.sub("", line, count=1)).strip()


def _clean_name(value: str) -> str | None:
    """Trim a parsed entity name and reject it when it is prose rather than a name.

    Args:
        value: A raw piece of a resume line.

    Returns:
        The cleaned name, or ``None`` when it is empty or too long to be one.
    """
    cleaned = _WHITESPACE_RUN.sub(" ", value or "").strip(_NAME_TRIM)
    if not cleaned or len(cleaned) > _MAX_ENTITY_NAME_CHARS:
        return None
    return cleaned


def _leading_name(line: str) -> str | None:
    """Return the entity name at the head of a free-form item line.

    ``"Eagle Scout — Boy Scouts of America, 2021"`` names an award called "Eagle Scout"; the
    rest is context. The name is therefore taken up to the first separator, bracket or comma.

    Args:
        line: One item line.

    Returns:
        The leading name, or ``None`` when nothing usable is there.
    """
    stop = _NAME_STOP.search(line)
    return _clean_name(line[: stop.start()] if stop else line)


def _contains(text: str, keywords: tuple[str, ...]) -> bool:
    """Return whether *text* contains any of *keywords*, case-insensitively.

    Args:
        text: The text to search.
        keywords: Lowercase keywords.

    Returns:
        ``True`` on the first hit.
    """
    lowered = text.casefold()
    return any(keyword in lowered for keyword in keywords)


# ======================================================================================
# Contact block
# ======================================================================================


def _looks_like_person_name(line: str) -> bool:
    """Return whether a header line reads like a person's name.

    Args:
        line: One line from the contact block.

    Returns:
        ``True`` for two to five alphabetic, capitalised or upper-case words with no digits,
        no ``@`` and no URL — which is what the top line of a resume almost always is.
    """
    candidate = _despace(line).strip(" .|•·")
    if not candidate or len(candidate) > 60 or any(ch.isdigit() for ch in candidate):
        return False
    if "@" in candidate or "/" in candidate or "http" in candidate.lower():
        return False
    words = candidate.split()
    if not 2 <= len(words) <= 5:
        return False
    return all(
        word.replace("-", "").replace(".", "").replace("'", "").isalpha()
        and (word[0].isupper() or word.isupper())
        for word in words
    )


def _normalize_link(value: str) -> str:
    """Return a link with a scheme, so stored links are directly openable.

    Args:
        value: A URL or bare domain as written on the resume.

    Returns:
        The link prefixed with ``https://`` when it had no scheme.
    """
    cleaned = value.strip().rstrip(").,;")
    if cleaned.lower().startswith(("http://", "https://")):
        return cleaned
    return f"https://{cleaned}"


def parse_contact_block(lines: list[str]) -> ContactBlock:
    """Parse the header block above a resume's first section heading.

    Args:
        lines: The header lines, in order.

    Returns:
        Everything identifying that the block states. Fields the resume does not contain
        stay ``None`` — onboarding asks for those, rather than being handed a guess.
    """
    contact = ContactBlock(lines=[line.strip() for line in lines if line.strip()])
    joined = "\n".join(contact.lines)

    email = _EMAIL.search(joined)
    if email:
        contact.email = email.group(0)
    phone = _PHONE.search(joined)
    if phone:
        contact.phone = _WHITESPACE_RUN.sub(" ", phone.group(0)).strip()
    linkedin = _LINKEDIN.search(joined)
    if linkedin:
        contact.linkedin = _normalize_link(linkedin.group(0))
    github = _GITHUB.search(joined)
    if github:
        contact.github = _normalize_link(github.group(0))

    consumed = {contact.linkedin, contact.github}
    for match in _URL.finditer(joined):
        link = _normalize_link(match.group(0))
        if link in consumed or "linkedin.com" in link or "github." in link:
            continue
        consumed.add(link)
        contact.websites.append(link)
    if not contact.websites:
        for match in _BARE_DOMAIN.finditer(joined):
            if "@" in joined[max(0, match.start() - 1) : match.start() + 1]:
                continue
            link = _normalize_link(match.group(0))
            if link in consumed or "linkedin.com" in link or "github." in link:
                continue
            consumed.add(link)
            contact.websites.append(link)
    if contact.websites:
        contact.portfolio = contact.websites[0]

    for line in contact.lines:
        if contact.name is None and _looks_like_person_name(line):
            contact.name = _despace(line).strip(" .|•·")
            continue
        if contact.location is None:
            for piece in _HEADER_SPLIT.split(line):
                candidate = piece.strip(" .|•·")
                if not candidate:
                    continue
                if candidate.casefold() in _ARRANGEMENT_WORDS or _LOCATION.match(candidate):
                    contact.location = candidate
                    break
    return contact


# ======================================================================================
# Segmentation
# ======================================================================================


def segment_sections(text: str) -> tuple[list[str], list[ResumeSection]]:
    """Split a resume's text into its header block and its sections.

    Args:
        text: The full extracted resume text.

    Returns:
        ``(header lines, sections)``. The header lines are everything above the first
        recognised heading, capped at :data:`MAX_CONTACT_LINES`; sections appear in document
        order.
    """
    header: list[str] = []
    sections: list[ResumeSection] = []
    current: ResumeSection | None = None

    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        heading = _detect_heading(line, inside=current.name if current else None)
        if heading is not None:
            canonical, inline = heading
            current = ResumeSection(name=canonical, heading=line.strip())
            if inline:
                current.lines.append(inline)
            sections.append(current)
            continue

        if current is None:
            if len(header) < MAX_CONTACT_LINES:
                header.append(line)
            continue
        current.lines.append(line)

    return header, sections


# ======================================================================================
# Entry parsing
# ======================================================================================


def _extract_date_range(line: str) -> tuple[str | None, str | None, str | None, str]:
    """Cut the date range out of a header line and normalise it.

    Args:
        line: One header line.

    Returns:
        ``(date_start, date_end, date_text, remainder)``. The remainder is the line with the
        matched range removed, so the separators around it can no longer be mistaken for
        column boundaries.
    """
    matches = list(_DATE_RANGE_TEXT.finditer(line))
    if not matches:
        return (None, None, None, line)

    chosen = max(matches, key=lambda match: len(match.group(0)))
    date_text = chosen.group(0).strip()
    start, end = extract_dates(date_text)
    remainder = f"{line[: chosen.start()]} {line[chosen.end() :]}"
    return (start, end, date_text, remainder)


def _peel_location(piece: str) -> tuple[str, str | None]:
    """Split a trailing ``, City, ST`` off a header piece.

    ``"Purdue University, West Lafayette, IN"`` is one column holding two facts; the school
    and the place must not be merged into a single organization name.

    Args:
        piece: One piece of a header line.

    Returns:
        ``(piece without the location, location)``.
    """
    match = _LOCATION_TAIL.search(piece)
    if match is None:
        return (piece, None)
    return (piece[: match.start()].strip(), match.group("location").strip())


def _classify_pieces(
    pieces: list[str], *, education: bool
) -> tuple[str | None, str | None, str | None]:
    """Assign the non-date pieces of a header line to organization, role and location.

    Supports the layouts resumes actually use::

        Acme Robotics — Austin, TX                     Jun 2024 – Aug 2024
        Firmware Engineering Intern
        Firmware Intern, Acme Robotics | Austin, TX | Jun 2024 – Aug 2024
        Purdue University, West Lafayette, IN          Expected May 2027
        B.S. Computer Engineering, Minor in Mathematics

    Args:
        pieces: The header line's pieces, date range already removed.
        education: Whether the enclosing section is ``EDUCATION``, where the school is the
            organization and the degree takes the role slot.

    Returns:
        ``(organization, role, location)``; any of them ``None`` when the line does not say.
    """
    organization: str | None = None
    role: str | None = None
    location: str | None = None
    remaining: list[str] = []

    for raw_piece in pieces:
        piece, peeled = _peel_location(raw_piece.strip(_NAME_TRIM))
        if peeled and location is None:
            location = peeled
        piece = piece.strip(_NAME_TRIM)
        if not piece:
            continue
        if location is None and (piece.casefold() in _ARRANGEMENT_WORDS or _LOCATION.match(piece)):
            location = piece
            continue
        remaining.append(piece)

    if education:
        for piece in remaining:
            if organization is None and _contains(piece, _SCHOOL_KEYWORDS):
                organization = _clean_name(piece)
            elif role is None and _contains(piece, _DEGREE_KEYWORDS):
                role = _clean_name(piece)
    else:
        for piece in remaining:
            if role is None and _contains(piece, _ROLE_KEYWORDS):
                role = _clean_name(piece)
                continue
            if organization is None:
                organization = _clean_name(piece)

    leftovers = [piece for piece in remaining if _clean_name(piece) not in (organization, role)]
    if organization is None and leftovers:
        organization = _clean_name(leftovers[0])
        leftovers = leftovers[1:]
    if role is None and leftovers:
        role = _clean_name(leftovers[0])
    return (organization, role, location)


def _parse_entry_header(entry: ResumeEntry, *, education: bool) -> None:
    """Fill an entry's fields from its header lines, in place.

    Earlier lines win: a resume that writes the employer above the title means the employer,
    and a later line only fills a slot the first one left empty.

    Args:
        entry: The entry whose :attr:`ResumeEntry.header_lines` are to be parsed.
        education: Whether the enclosing section is ``EDUCATION``.
    """
    for line in entry.header_lines:
        gpa = _GPA.search(line)
        if gpa and entry.gpa is None:
            scale = gpa.group("scale")
            entry.gpa = f"{gpa.group('gpa')}/{scale}" if scale else gpa.group("gpa")
            line = f"{line[: gpa.start()]} {line[gpa.end() :]}"

        start, end, date_text, remainder = _extract_date_range(line)
        if entry.date_start is None and start is not None:
            entry.date_start, entry.date_end, entry.date_text = start, end, date_text

        pieces = [piece for piece in _HEADER_SPLIT.split(remainder) if piece.strip()]
        organization, role, location = _classify_pieces(pieces, education=education)
        entry.organization = entry.organization or organization
        entry.role = entry.role or role
        entry.location = entry.location or location


def parse_entries(section: ResumeSection) -> list[ResumeEntry]:
    """Group a section's lines into dated entries with their bullets.

    A non-bullet line starts (or continues) an entry header; a bullet line attaches to the
    entry above it. A non-bullet line that follows bullets and reads like a continuation —
    lower-case first word, no date of its own — is folded back onto the bullet it wraps,
    which is what PDF extraction of a two-line bullet produces.

    Args:
        section: The section to parse.

    Returns:
        The entries, in document order. Never contains an empty entry.
    """
    education = section.name == "EDUCATION"
    entries: list[ResumeEntry] = []
    current = ResumeEntry()

    def flush() -> None:
        """Finish the entry under construction, if it has anything in it."""
        nonlocal current
        if not current.is_empty or current.header_lines:
            _parse_entry_header(current, education=education)
            if not current.is_empty:
                entries.append(current)
        current = ResumeEntry()

    for line in section.lines:
        stripped = line.strip()
        if not stripped:
            continue

        if _is_bullet(stripped):
            current.bullets.append(_strip_bullet(stripped))
            continue

        if current.bullets:
            first_word = stripped.split(" ", 1)[0]
            wraps = first_word[:1].islower() and not _DATE_RANGE_TEXT.search(stripped)
            if wraps:
                current.bullets[-1] = f"{current.bullets[-1]} {stripped}"
                continue
            flush()

        if len(current.header_lines) >= _MAX_ENTRY_HEADER_LINES:
            flush()
        current.header_lines.append(stripped)

    flush()
    return entries


# ======================================================================================
# The analyzer
# ======================================================================================


@plugin
class ResumeParser(Analyzer):
    """Turns an existing resume or cover letter into facts, entities and edges.

    Handles :attr:`~app.models.enums.SourceKind.RESUME` and
    :attr:`~app.models.enums.SourceKind.COVER_LETTER`. A resume is segmented into sections
    and entries; a cover letter is prose and goes straight to the extractor.

    Configuration recognised on :attr:`~app.knowledge.analyzers.base.SourceRef.config`:

    ``name``
        Overrides the person name used to anchor ``worked_at``/``studied_at``/``earned``
        edges, when the resume's own header does not state one.
    """

    meta: ClassVar[PluginMeta] = PluginMeta(
        kind=PluginKind.ANALYZER,
        name="resume_parser",
        version="1.0.0",
        display_name="Resume",
        description=(
            "Parses a resume or cover letter (PDF, DOCX, Markdown or text) into sections, "
            "entries, facts and a contact block."
        ),
        capabilities=frozenset({"pdf", "docx", "markdown", "contact_block", "offline"}),
    )

    source_kinds: ClassVar[frozenset[SourceKind]] = frozenset(
        {SourceKind.RESUME, SourceKind.COVER_LETTER}
    )

    def __init__(self, settings: Settings, **kw: Any) -> None:
        """Construct the analyzer.

        Args:
            settings: Application settings, supplied by the plugin registry.
            **kw: Extra construction options, kept on :attr:`options`.
        """
        super().__init__(settings, **kw)
        self._extractor: KnowledgeExtractor | None = None

    # -- change detection ---------------------------------------------------------------

    async def fingerprint(self, source: SourceRef) -> str:
        """Probe the resume file for changes without re-parsing it.

        Args:
            source: The resume source.

        Returns:
            A ``(path, size, mtime)`` digest for a local file, or the never-matching
            identity digest for a remote or missing one.
        """
        uri = source.uri.strip()
        if not uri or looks_like_url(uri):
            return await super().fingerprint(source)
        return file_probe_fingerprint(local_path_for(uri)) or await super().fingerprint(source)

    # -- analysis -----------------------------------------------------------------------

    async def analyze(self, source: SourceRef) -> AnalysisResult:
        """Parse the resume at *source* and return everything it states.

        Args:
            source: A ``resume`` or ``cover_letter`` source pointing at a local file or an
                ``http(s)`` URL.

        Returns:
            One document — carrying the parsed contact block in its metadata — plus facts
            for every bullet, organizations, roles, degrees, awards, certifications,
            publications, languages and skills, joined by ``worked_at``, ``studied_at``,
            ``used_in``, ``earned``, ``led`` and ``published`` edges.

        Raises:
            SourceUnavailableError: If the file does not exist or the URL is unreachable.
            SourceAccessDenied: If it cannot be read.
        """
        self.require_supported(source)
        result = AnalysisResult()
        uri = source.uri.strip()
        if not uri:
            raise SourceUnavailableError("resume source has no path", source=source)

        if looks_like_url(uri):
            extraction = await fetch_text_from_url(uri)
            canonical_uri = str(extraction.metadata.get("final_url") or uri)
            fallback_title = Path(canonical_uri).name or canonical_uri
            probe = ""
        else:
            path = local_path_for(uri)
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise SourceUnavailableError(
                    f"{path} does not exist or cannot be resolved ({exc.strerror or exc})",
                    source=source,
                ) from exc
            if not resolved.is_file():
                raise SourceUnavailableError(f"{resolved} is not a file", source=source)
            extraction = extract_text_from_path(resolved)
            canonical_uri = resolved.as_posix()
            fallback_title = resolved.name
            probe = file_probe_fingerprint(resolved)

        for message in extraction.errors:
            result.record_error(message)

        if not extraction.has_text:
            result.fingerprint = probe or compute_fingerprint(
                _RESUME_FINGERPRINT_TAG, canonical_uri, "empty"
            )
            logger.warning("resume.no_text", uri=canonical_uri, errors=len(result.errors))
            return result

        header_lines, sections = segment_sections(extraction.text)
        contact = parse_contact_block(header_lines)
        person = source.option("name") or contact.name

        result.documents.append(
            ExtractedDocument(
                uri=canonical_uri,
                title=self._title(contact, source, fallback_title),
                text=extraction.text,
                kind=source.kind,
                metadata={
                    "analyzer": self.name,
                    "source_uri": uri,
                    "contact": contact.as_dict(),
                    "sections": [section.name for section in sections],
                    **{
                        key: value
                        for key, value in extraction.metadata.items()
                        if value is not None and key != "links"
                    },
                },
            )
        )

        if person:
            result.entities.append(
                ExtractedEntity(
                    kind=EntityKind.PERSON,
                    name=person,
                    attributes={
                        key: value
                        for key, value in contact.as_dict().items()
                        if key not in {"name", "lines"} and value
                    },
                    confidence=_PARSED_CONFIDENCE,
                )
            )

        if source.kind is SourceKind.COVER_LETTER or not sections:
            await self._analyze_prose(result, extraction.text, canonical_uri, person)
        else:
            for section in sections:
                await self._analyze_section(result, section, canonical_uri, person)

        result.deduplicate()
        result.fingerprint = probe or result.content_fingerprint()
        logger.info(
            "resume.analyzed",
            uri=canonical_uri,
            sections=len(sections),
            contact=not contact.is_empty,
            **result.counts(),
        )
        return result

    # -- section dispatch ---------------------------------------------------------------

    async def _analyze_section(
        self,
        result: AnalysisResult,
        section: ResumeSection,
        source_uri: str,
        person: str | None,
    ) -> None:
        """Extract everything one section states and merge it into *result*.

        Args:
            result: The result being assembled.
            section: The section to process.
            source_uri: Provenance uri for every fact produced.
            person: The resume owner's name, when known, for person-anchored edges.
        """
        if section.name in _NON_FACT_SECTIONS or not section.lines:
            return
        if section.name == "SKILLS":
            self._emit_skills(result, section, source_uri)
            return
        if section.name == "LANGUAGES":
            self._emit_languages(result, section)
            return
        if section.name in ENTRY_SECTIONS:
            for entry in parse_entries(section):
                await self._emit_entry(result, section, entry, source_uri, person)
            return
        self._emit_items(result, section, source_uri, person)

    # -- entry-shaped sections ------------------------------------------------------------

    async def _emit_entry(
        self,
        result: AnalysisResult,
        section: ResumeSection,
        entry: ResumeEntry,
        source_uri: str,
        person: str | None,
    ) -> None:
        """Emit the entities, edges and facts of one dated entry.

        Args:
            result: The result being assembled.
            section: The enclosing section, which decides the fact kind and the edge shapes.
            entry: The parsed entry.
            source_uri: Provenance uri for every fact produced.
            person: The resume owner's name, when known.
        """
        fact_kind = SECTION_FACT_KINDS.get(section.name, FactKind.ACCOMPLISHMENT)
        is_project = section.name == "PROJECTS"
        anchor_kind = EntityKind.PROJECT if is_project else EntityKind.ORGANIZATION
        anchor = entry.organization or (entry.role if is_project else None)

        # On a project entry the second column is a subtitle ("Aster Quadruped — 12-DOF
        # Legged Robot"), not a job title, so it becomes the project's summary rather than a
        # role that would then be attached to every fact.
        subtitle = entry.role if is_project and entry.organization else None
        role = None if is_project else entry.role

        if anchor:
            result.entities.append(
                ExtractedEntity(
                    kind=anchor_kind,
                    name=anchor,
                    summary=subtitle,
                    attributes={
                        key: value
                        for key, value in {
                            "location": entry.location,
                            "date_start": entry.date_start,
                            "date_end": entry.date_end,
                            "date_text": entry.date_text,
                            "gpa": entry.gpa,
                            "section": section.name,
                        }.items()
                        if value
                    },
                    confidence=_PARSED_CONFIDENCE,
                )
            )

        if entry.role and not is_project:
            result.entities.append(
                ExtractedEntity(
                    kind=EntityKind.EDUCATION if section.name == "EDUCATION" else EntityKind.ROLE,
                    name=entry.role,
                    attributes={
                        key: value
                        for key, value in {
                            "organization": entry.organization,
                            "date_start": entry.date_start,
                            "date_end": entry.date_end,
                        }.items()
                        if value
                    },
                    confidence=_PARSED_CONFIDENCE,
                )
            )

        self._emit_entry_edges(result, section, entry, person, anchor, anchor_kind, is_project)

        # The entry's own header always becomes one clean fact for EDUCATION (the degree is
        # the claim) and for any entry with no bullets — assembled from the *parsed* fields
        # rather than the raw line, so a two-column header does not arrive as one run-on
        # sentence with a GPA welded to a date range.
        headline = " — ".join(part for part in (role or subtitle, anchor) if part)
        if headline and (section.name == "EDUCATION" or not entry.bullets):
            result.facts.append(
                self._line_fact(
                    text=f"{headline} (GPA {entry.gpa})" if entry.gpa else headline,
                    kind=fact_kind,
                    organization=entry.organization,
                    role=role,
                    date_start=entry.date_start,
                    date_end=entry.date_end,
                    source_uri=source_uri,
                )
            )

        text = entry.body()
        if not text.strip():
            return

        extracted = await self._knowledge().extract(
            text,
            kind=fact_kind,
            context={
                "organization": anchor or entry.organization,
                "role": role,
                "source_uri": source_uri,
            },
        )
        for fact in extracted.facts:
            if fact.date_start is None:
                fact.date_start, fact.date_end = entry.date_start, entry.date_end
            fact.organization = fact.organization or anchor or entry.organization
            fact.role = fact.role or role
        result.merge(extracted)

        if anchor:
            for name in extract_skills(text):
                kind = skill_entity_kind(name)
                result.edges.append(
                    ExtractedEdge(
                        source=(kind, name),
                        target=(anchor_kind, anchor),
                        relation=RelationKind.USED_IN,
                        evidence={"analyzer": self.name, "section": section.name},
                    )
                )

    def _emit_entry_edges(
        self,
        result: AnalysisResult,
        section: ResumeSection,
        entry: ResumeEntry,
        person: str | None,
        anchor: str | None,
        anchor_kind: EntityKind,
        is_project: bool,
    ) -> None:
        """Emit the relationship edges implied by one entry.

        Args:
            result: The result being assembled.
            section: The enclosing section.
            entry: The parsed entry.
            person: The resume owner's name, when known.
            anchor: The organization or project the entry is about.
            anchor_kind: Which of the two *anchor* is.
            is_project: Whether the enclosing section is ``PROJECTS``.
        """
        if anchor is None:
            return
        evidence = {"analyzer": self.name, "section": section.name}

        if is_project:
            if person:
                result.edges.append(
                    ExtractedEdge(
                        source=(EntityKind.PERSON, person),
                        target=(EntityKind.PROJECT, anchor),
                        relation=RelationKind.BUILT,
                        evidence=evidence,
                    )
                )
            return

        if section.name == "EDUCATION":
            if entry.role:
                result.edges.append(
                    ExtractedEdge(
                        source=(EntityKind.EDUCATION, entry.role),
                        target=(EntityKind.ORGANIZATION, anchor),
                        relation=RelationKind.STUDIED_AT,
                        evidence=evidence,
                    )
                )
            if person:
                result.edges.append(
                    ExtractedEdge(
                        source=(EntityKind.PERSON, person),
                        target=(EntityKind.ORGANIZATION, anchor),
                        relation=RelationKind.STUDIED_AT,
                        evidence=evidence,
                    )
                )
            return

        relation = RelationKind.LED if section.name == "LEADERSHIP" else RelationKind.WORKED_AT
        if entry.role:
            result.edges.append(
                ExtractedEdge(
                    source=(EntityKind.ROLE, entry.role),
                    target=(anchor_kind, anchor),
                    relation=RelationKind.WORKED_AT,
                    evidence=evidence,
                )
            )
        if person:
            result.edges.append(
                ExtractedEdge(
                    source=(EntityKind.PERSON, person),
                    target=(anchor_kind, anchor),
                    relation=relation,
                    evidence=evidence,
                )
            )

    # -- list-shaped sections -------------------------------------------------------------

    def _emit_items(
        self,
        result: AnalysisResult,
        section: ResumeSection,
        source_uri: str,
        person: str | None,
    ) -> None:
        """Emit one entity and one fact per line of a list-shaped section.

        Awards, certifications and publications are single-line items rather than dated
        entries with bullets, so each line is both the fact and the entity's name.

        Args:
            result: The result being assembled.
            section: The section to process.
            source_uri: Provenance uri for every fact produced.
            person: The resume owner's name, when known.
        """
        entity_kind, relation = {
            "AWARDS": (EntityKind.AWARD, RelationKind.EARNED),
            "CERTIFICATIONS": (EntityKind.CERTIFICATION, RelationKind.EARNED),
            "PUBLICATIONS": (EntityKind.PUBLICATION, RelationKind.PUBLISHED),
            "COURSEWORK": (EntityKind.COURSE, RelationKind.RELATED_TO),
        }.get(section.name, (EntityKind.INTEREST, RelationKind.RELATED_TO))
        fact_kind = SECTION_FACT_KINDS.get(section.name, FactKind.ACCOMPLISHMENT)

        items: list[str] = []
        for line in section.lines:
            text = (
                _strip_bullet(line) if _is_bullet(line) else _WHITESPACE_RUN.sub(" ", line).strip()
            )
            if not text:
                continue
            if section.name == "COURSEWORK":
                items.extend(part for part in _LIST_SPLIT.split(text) if part.strip())
            else:
                items.append(text)

        for item in items:
            name = _leading_name(item)
            if name is None:
                continue
            start, end = extract_dates(item)
            result.entities.append(
                ExtractedEntity(
                    kind=entity_kind,
                    name=name,
                    summary=item if item != name else None,
                    attributes={key: value for key, value in {"date": start}.items() if value},
                    confidence=_PARSED_CONFIDENCE,
                )
            )
            if person:
                result.edges.append(
                    ExtractedEdge(
                        source=(EntityKind.PERSON, person),
                        target=(entity_kind, name),
                        relation=relation,
                        evidence={"analyzer": self.name, "section": section.name},
                    )
                )
            result.facts.append(
                self._line_fact(
                    text=item,
                    kind=fact_kind,
                    organization=None,
                    role=None,
                    date_start=start,
                    date_end=end,
                    source_uri=source_uri,
                )
            )

    def _emit_skills(self, result: AnalysisResult, section: ResumeSection, source_uri: str) -> None:
        """Emit skill entities and one ``skill_usage`` fact per skills line.

        The line is kept whole ("Languages: C, C++, Python, MATLAB") because the grouping is
        information the resume engine needs to regenerate a skills line that reads like the
        user's own. Individual names are additionally canonicalised into graph nodes.

        Args:
            result: The result being assembled.
            section: The ``SKILLS`` section.
            source_uri: Provenance uri for every fact produced.
        """
        for line in section.lines:
            text = (
                _strip_bullet(line) if _is_bullet(line) else _WHITESPACE_RUN.sub(" ", line).strip()
            )
            if not text:
                continue

            labelled = _LABELLED_LINE.match(text)
            body = labelled.group("body") if labelled else text
            names = extract_skills(body)
            for name in names:
                kind = skill_entity_kind(name)
                result.entities.append(
                    ExtractedEntity(
                        kind=kind,
                        name=name,
                        attributes={"declared_in": "resume skills section"},
                        confidence=_PARSED_CONFIDENCE,
                    )
                )
            for raw in _LIST_SPLIT.split(body):
                candidate = _clean_name(raw)
                if candidate is None or len(candidate.split()) > 4:
                    continue
                canonical = canonical_skill(candidate)
                if canonical is None:
                    result.entities.append(
                        ExtractedEntity(
                            kind=EntityKind.TECHNOLOGY,
                            name=candidate,
                            attributes={"declared_in": "resume skills section"},
                            confidence=_PARSED_CONFIDENCE - 0.15,
                        )
                    )

            result.facts.append(
                self._line_fact(
                    text=text,
                    kind=FactKind.SKILL_USAGE,
                    organization=None,
                    role=labelled.group("label").strip() if labelled else None,
                    date_start=None,
                    date_end=None,
                    source_uri=source_uri,
                )
            )

    def _emit_languages(self, result: AnalysisResult, section: ResumeSection) -> None:
        """Emit a ``language`` entity for each spoken language listed.

        Args:
            result: The result being assembled.
            section: The ``LANGUAGES`` section.
        """
        for line in section.lines:
            text = _strip_bullet(line) if _is_bullet(line) else line
            labelled = _LABELLED_LINE.match(text)
            body = labelled.group("body") if labelled else text
            for raw in _LIST_SPLIT.split(body):
                candidate = _clean_name(raw)
                if candidate is None or len(candidate.split()) > 3:
                    continue
                proficiency = re.search(r"\(([^)]+)\)", candidate)
                name = _clean_name(re.sub(r"\([^)]*\)", "", candidate))
                if not name:
                    continue
                result.entities.append(
                    ExtractedEntity(
                        kind=EntityKind.LANGUAGE,
                        name=name,
                        attributes=(
                            {"proficiency": proficiency.group(1).strip()} if proficiency else {}
                        ),
                        confidence=_PARSED_CONFIDENCE,
                    )
                )

    # -- prose (cover letters, and resumes with no recognisable headings) -----------------

    async def _analyze_prose(
        self,
        result: AnalysisResult,
        text: str,
        source_uri: str,
        person: str | None,
    ) -> None:
        """Extract knowledge from a document with no section structure.

        Args:
            result: The result being assembled.
            text: The full document text.
            source_uri: Provenance uri for every fact produced.
            person: The resume owner's name, when known.
        """
        extracted = await self._knowledge().extract(
            text,
            kind=FactKind.ACCOMPLISHMENT,
            context={"source_uri": source_uri},
        )
        result.merge(extracted)
        logger.debug(
            "resume.prose_extracted", uri=source_uri, facts=len(extracted.facts), person=person
        )

    # -- internals ------------------------------------------------------------------------

    @staticmethod
    def _title(contact: ContactBlock, source: SourceRef, fallback: str) -> str:
        """Return the document title.

        Args:
            contact: The parsed contact block.
            source: The source, for its label and kind.
            fallback: The filename, used when nothing better is known.

        Returns:
            ``"<name> — Resume"`` when the header names a person, else the source label, else
            the filename.
        """
        if source.label:
            return source.label
        label = "Cover Letter" if source.kind is SourceKind.COVER_LETTER else "Resume"
        return f"{contact.name} — {label}" if contact.name else fallback

    @staticmethod
    def _line_fact(
        *,
        text: str,
        kind: FactKind,
        organization: str | None,
        role: str | None,
        date_start: str | None,
        date_end: str | None,
        source_uri: str,
    ) -> ExtractedFact:
        """Build a fact directly from one resume line, with no model involved.

        Used where a line *is* the claim — an award, a certification, a degree, a skills
        grouping — and splitting it into bullets would destroy it.

        Args:
            text: The line, verbatim.
            kind: The claim category.
            organization: Employer or school, when the section implies one.
            role: Title or degree, when the section implies one.
            date_start: Period start, when the line states one.
            date_end: Period end, when the line states one.
            source_uri: Provenance uri.

        Returns:
            The fact, scored and annotated by the shared deterministic extractors.
        """
        names = extract_skills(text)
        skills, technologies = classify_skills(names)
        metrics = extract_metrics(text)
        return ExtractedFact(
            kind=kind,
            text=text,
            skills=skills,
            technologies=technologies,
            metrics=metrics,
            organization=organization,
            role=role,
            date_start=date_start,
            date_end=date_end,
            impact_score=score_impact(text, names, metrics),
            confidence=_LINE_FACT_CONFIDENCE,
            source_uri=source_uri,
        )

    def _knowledge(self) -> KnowledgeExtractor:
        """Return this instance's extractor, building it once.

        Returns:
            The shared-cache-backed :class:`~app.knowledge.extractors.KnowledgeExtractor`.
        """
        if self._extractor is None:
            self._extractor = knowledge_extractor()
        return self._extractor
