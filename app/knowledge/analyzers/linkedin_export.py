"""Parsing the user's own LinkedIn data export — never LinkedIn itself.

**This analyzer reads a file the user downloaded and handed to us. It performs no network
requests, holds no credentials, and touches no LinkedIn page, endpoint or API. There is no
scraping here and there must never be.** LinkedIn's terms prohibit automated access; their
*Get a copy of your data* export exists precisely so that a person can take their own
information elsewhere, and that is the only route this product uses. The same posture is
recorded for the LinkedIn job provider in ``docs/CONTRACTS.md`` §9.

Input is whatever the user actually has: the ``.zip`` LinkedIn emails them, or a folder they
already unzipped. Both give the same result. Inside are a handful of CSVs whose headers
LinkedIn has changed several times over the years, so every file is matched by base name
(case-insensitively, ignoring the export's wrapping folder) and every column through a
per-file alias map — ``Started On``, ``Start Date`` and ``Start`` all mean the same thing.
UTF-8 BOMs and the "Notes:" preamble rows some exports carry are handled; a file that is
absent, empty or unreadable is skipped with a line in
:attr:`~app.knowledge.analyzers.base.AnalysisResult.errors` rather than failing the run.

Dates arrive as ``"Mon YYYY"`` and are normalised to ``YYYY-MM`` through the same
:func:`~app.knowledge.extractors.extract_dates` the rest of the engine uses, so a LinkedIn
position and a resume bullet describing the same job sort identically.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
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
    SourceAccessDenied,
    SourceRef,
    SourceUnavailableError,
    compute_fingerprint,
)
from app.knowledge.analyzers.document import (
    decode_bytes,
    knowledge_extractor,
    local_path_for,
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
    "EXPORT_FILES",
    "FIELD_ALIASES",
    "MAX_ROWS_PER_FILE",
    "LinkedInExportAnalyzer",
    "normalize_export_date",
    "parse_export_csv",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# The export's shape
# ======================================================================================

#: Base names of the CSVs this analyzer understands, lowercased. Anything else in the export
#: — messages, connections, ad targeting, login history — is deliberately ignored: it is not
#: evidence of what the user can do, and indexing it would put private correspondence into a
#: knowledge base that feeds resume generation.
EXPORT_FILES: Final[tuple[str, ...]] = (
    "profile.csv",
    "positions.csv",
    "education.csv",
    "skills.csv",
    "projects.csv",
    "certifications.csv",
    "honors.csv",
    "languages.csv",
    "publications.csv",
)

#: Per-file column alias map: canonical field → the header spellings LinkedIn has shipped.
#: Headers are compared after case folding, punctuation removal and whitespace collapsing,
#: so ``"Company Name"``, ``"company_name"`` and ``"COMPANY NAME "`` all match ``company``.
FIELD_ALIASES: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "profile.csv": {
        "first_name": ("first name", "firstname", "given name"),
        "last_name": ("last name", "lastname", "surname", "family name"),
        "headline": ("headline",),
        "summary": ("summary", "about"),
        "industry": ("industry",),
        "location": ("geo location", "location", "address"),
        "websites": ("websites", "website", "web sites"),
    },
    "positions.csv": {
        "organization": ("company name", "company", "organization", "organisation"),
        "role": ("title", "position", "position title", "role", "job title"),
        "description": ("description", "summary", "details"),
        "location": ("location", "company location"),
        "started_on": ("started on", "start date", "started", "start", "from"),
        "finished_on": ("finished on", "end date", "finished", "end", "to"),
    },
    "education.csv": {
        "organization": ("school name", "school", "institution", "organization"),
        "degree": ("degree name", "degree", "qualification"),
        "field_of_study": ("field of study", "major", "study field"),
        "notes": ("notes", "description", "activities and societies", "activities"),
        "started_on": ("start date", "started on", "started", "start", "from"),
        "finished_on": ("end date", "finished on", "finished", "end", "to"),
    },
    "skills.csv": {
        "name": ("name", "skill", "skill name"),
    },
    "projects.csv": {
        "name": ("title", "name", "project name"),
        "description": ("description", "summary"),
        "url": ("url", "link"),
        "started_on": ("started on", "start date", "started", "start"),
        "finished_on": ("finished on", "end date", "finished", "end"),
    },
    "certifications.csv": {
        "name": ("name", "certification name", "title"),
        "authority": ("authority", "issuing organization", "issuer", "organization"),
        "url": ("url", "link"),
        "license_number": ("license number", "credential id", "licence number"),
        "started_on": ("started on", "start date", "issued on", "issue date", "started"),
        "finished_on": ("finished on", "end date", "expires on", "expiration date"),
    },
    "honors.csv": {
        "name": ("title", "name", "honor", "award"),
        "description": ("description", "summary"),
        "issued_on": ("issued on", "issue date", "date", "issued"),
    },
    "languages.csv": {
        "name": ("name", "language"),
        "proficiency": ("proficiency", "level", "proficiency level"),
    },
    "publications.csv": {
        "name": ("name", "title"),
        "description": ("description", "summary"),
        "publisher": ("publisher", "publication"),
        "url": ("url", "link"),
        "published_on": ("published on", "publication date", "date", "published"),
    },
}

#: Most rows read from any one file. An export can carry hundreds of endorsed skills;
#: past this point the tail is noise and the graph is better without it.
MAX_ROWS_PER_FILE: Final[int] = 500

#: Rows inspected while looking for the real header. Some exports open with a "Notes:"
#: preamble before the column names.
_MAX_PREAMBLE_ROWS: Final[int] = 6

#: Largest member read out of a zip, as a guard against a decompression bomb.
MAX_MEMBER_BYTES: Final[int] = 32 * 1024 * 1024

#: Chunk size used when digesting a zip for the change probe.
_HASH_CHUNK_BYTES: Final[int] = 1024 * 1024

#: Domain-separation tags for the two fingerprint shapes.
_ZIP_FINGERPRINT_TAG: Final[str] = "analyzer.linkedin.zip.v1"
_DIRECTORY_FINGERPRINT_TAG: Final[str] = "analyzer.linkedin.dir.v1"

#: Scheme used for the per-file document uris, so they are stable and obviously synthetic.
_URI_SCHEME: Final[str] = "linkedin"

#: Confidence for a value read straight out of the user's own export. High: LinkedIn is
#: recording what the user typed about themselves, not an inference about them.
_EXPORT_CONFIDENCE: Final[float] = 0.9

#: Values LinkedIn writes for "this is still true".
_ONGOING_VALUES: Final[frozenset[str]] = frozenset({"present", "current", "ongoing", "-", "n/a"})

#: Longest entity name taken from an export field.
_MAX_NAME_CHARS: Final[int] = 120

_HEADER_NOISE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9 ]+")
_WHITESPACE_RUN: Final[re.Pattern[str]] = re.compile(r"\s+")


# ======================================================================================
# CSV reading
# ======================================================================================


def _normalize_header(value: str) -> str:
    """Reduce a CSV column name to its comparable form.

    Args:
        value: The header cell exactly as the file spells it, BOM included.

    Returns:
        Lowercase alphanumerics and single spaces.
    """
    cleaned = (value or "").replace("﻿", "").casefold()
    return " ".join(_HEADER_NOISE.sub(" ", cleaned).split())


def _locate_header(lines: list[str], known: frozenset[str]) -> int:
    """Return the index of the line holding the column headers.

    Args:
        lines: The file's lines, newlines retained.
        known: Every normalised header this file may declare.

    Returns:
        The index of the header line; ``0`` when no preamble was detected, which makes the
        first line the header exactly as :class:`csv.DictReader` would assume anyway.
    """
    for index, line in enumerate(lines[:_MAX_PREAMBLE_ROWS]):
        try:
            cells = next(csv.reader([line]), [])
        except csv.Error:
            continue
        if any(_normalize_header(cell) in known for cell in cells):
            return index
    return 0


def parse_export_csv(text: str, aliases: dict[str, tuple[str, ...]]) -> list[dict[str, str]]:
    """Parse one export CSV into rows keyed by normalised column name.

    Args:
        text: The decoded file contents.
        aliases: The file's canonical-field → header-spellings map, used to find the header
            row when the export carries a preamble.

    Returns:
        Up to :data:`MAX_ROWS_PER_FILE` non-empty rows, each mapping a normalised header to
        a trimmed value.
    """
    lines = (text or "").splitlines(keepends=True)
    if not lines:
        return []

    known = frozenset(alias for spellings in aliases.values() for alias in spellings)
    start = _locate_header(lines, known)
    reader = csv.DictReader(io.StringIO("".join(lines[start:])))

    rows: list[dict[str, str]] = []
    try:
        for raw in reader:
            if len(rows) >= MAX_ROWS_PER_FILE:
                break
            row = {
                _normalize_header(key): _WHITESPACE_RUN.sub(" ", str(value)).strip()
                for key, value in raw.items()
                if key and isinstance(value, str)
            }
            if any(row.values()):
                rows.append(row)
    except csv.Error as exc:
        logger.warning("linkedin.csv_parse_failed", error=str(exc), rows=len(rows))
    return rows


def _field(row: dict[str, str], aliases: dict[str, tuple[str, ...]], name: str) -> str | None:
    """Read one canonical field out of a row, trying every known spelling.

    Args:
        row: A row keyed by normalised header.
        aliases: The file's alias map.
        name: The canonical field name.

    Returns:
        The trimmed value, or ``None`` when the column is absent or blank.
    """
    for spelling in aliases.get(name, ()):
        value = row.get(spelling)
        if value:
            return value
    return None


def normalize_export_date(value: str | None) -> str | None:
    """Normalise a LinkedIn date to ``YYYY-MM`` (or ``YYYY``).

    LinkedIn writes ``"Jan 2024"``, sometimes a bare ``"2024"``, and ``""`` or ``"Present"``
    for ongoing periods. Parsing goes through the engine's shared date reader so a LinkedIn
    position and a resume line describing the same job normalise identically.

    Args:
        value: The raw cell.

    Returns:
        The normalised date, or ``None`` for a blank or open-ended value.
    """
    if not value:
        return None
    candidate = value.strip()
    if not candidate or candidate.casefold() in _ONGOING_VALUES:
        return None
    start, _ = extract_dates(candidate)
    return start


def _clean_name(value: str | None) -> str | None:
    """Trim an export value into a usable entity name.

    Args:
        value: The raw cell.

    Returns:
        The trimmed name, or ``None`` when it is blank or implausibly long.
    """
    if not value:
        return None
    cleaned = _WHITESPACE_RUN.sub(" ", value).strip(" \t.,;:|-–—")
    if not cleaned or len(cleaned) > _MAX_NAME_CHARS:
        return None
    return cleaned


# ======================================================================================
# The archive
# ======================================================================================


@dataclass(slots=True)
class _ExportArchive:
    """The set of export files found, however the user supplied them.

    Attributes:
        root: The zip or directory the user pointed at.
        is_zip: Whether *root* is a zip archive.
        members: Canonical lowercased base name → the member path (inside the zip) or the
            absolute path (on disk).
        stats: Per-file ``(size, stamp)`` — the stamp being the nanosecond mtime on disk and
            the stored CRC inside a zip. Used by the directory fingerprint; a zip is
            digested from its own bytes instead.
        errors: Recoverable problems encountered while opening the export.
    """

    root: Path
    is_zip: bool
    members: dict[str, str] = field(default_factory=dict)
    stats: dict[str, tuple[int, int]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def read(self, filename: str) -> str | None:
        """Read one export file as text.

        Args:
            filename: The canonical lowercased base name, e.g. ``"positions.csv"``.

        Returns:
            The decoded contents, or ``None`` when the file is absent or unreadable.
        """
        member = self.members.get(filename)
        if member is None:
            return None
        try:
            if self.is_zip:
                with zipfile.ZipFile(self.root) as archive:
                    info = archive.getinfo(member)
                    if info.file_size > MAX_MEMBER_BYTES:
                        self.errors.append(
                            f"{filename} is {info.file_size:,} bytes and was not read."
                        )
                        return None
                    data = archive.read(member)
            else:
                data = Path(member).read_bytes()
        except (OSError, zipfile.BadZipFile, KeyError) as exc:
            self.errors.append(f"{filename} could not be read ({exc}).")
            return None
        return decode_bytes(data)


def _open_archive(path: Path) -> _ExportArchive:
    """Locate the export files inside a zip or an already-extracted directory.

    LinkedIn wraps everything in a ``Complete_LinkedInDataExport_<date>`` folder, and users
    unzip it in whatever shape they like, so files are matched on base name at any depth.

    Args:
        path: The zip or directory the user supplied.

    Returns:
        The archive handle.

    Raises:
        SourceUnavailableError: If *path* is a zip that cannot be opened.
    """
    wanted = frozenset(EXPORT_FILES)
    if path.is_dir():
        archive = _ExportArchive(root=path, is_zip=False)
        for candidate in path.rglob("*"):
            name = candidate.name.lower()
            if name not in wanted or not candidate.is_file():
                continue
            if name in archive.members:
                continue
            archive.members[name] = str(candidate)
            try:
                stat = candidate.stat()
            except OSError:
                continue
            archive.stats[name] = (stat.st_size, stat.st_mtime_ns)
        return archive

    archive = _ExportArchive(root=path, is_zip=True)
    try:
        with zipfile.ZipFile(path) as zip_file:
            for info in zip_file.infolist():
                if info.is_dir():
                    continue
                name = Path(info.filename).name.lower()
                if name in wanted and name not in archive.members:
                    archive.members[name] = info.filename
                    archive.stats[name] = (info.file_size, info.CRC)
    except (zipfile.BadZipFile, OSError) as exc:
        raise SourceUnavailableError(
            f"{path} is not a readable LinkedIn export archive ({exc})"
        ) from exc
    return archive


# ======================================================================================
# The analyzer
# ======================================================================================


@plugin
class LinkedInExportAnalyzer(Analyzer):
    """Turns the user's official LinkedIn data export into knowledge.

    Reads ``Profile``, ``Positions``, ``Education``, ``Skills``, ``Projects``,
    ``Certifications``, ``Honors``, ``Languages`` and ``Publications`` from a ``.zip`` or an
    extracted directory. Everything else in the export is ignored on purpose.

    **No scraping, ever.** See the module docstring: this analyzer performs no network I/O
    of any kind.

    Configuration recognised on :attr:`~app.knowledge.analyzers.base.SourceRef.config`:

    ``name``
        Overrides the person name used to anchor edges, when ``Profile.csv`` is absent.
    """

    meta: ClassVar[PluginMeta] = PluginMeta(
        kind=PluginKind.ANALYZER,
        name="linkedin_export",
        version="1.0.0",
        display_name="LinkedIn export",
        description=(
            "Reads the ZIP or folder from LinkedIn's 'Get a copy of your data' export. "
            "User-supplied file only — never scrapes LinkedIn."
        ),
        capabilities=frozenset({"zip", "directory", "offline", "no_network"}),
    )

    source_kinds: ClassVar[frozenset[SourceKind]] = frozenset({SourceKind.LINKEDIN_EXPORT})

    def __init__(self, settings: Settings, **kw: Any) -> None:
        """Construct the analyzer.

        Args:
            settings: Application settings, supplied by the plugin registry.
            **kw: Extra construction options, kept on :attr:`options`.
        """
        super().__init__(settings, **kw)
        self._extractor: KnowledgeExtractor | None = None

    # -- resolution ---------------------------------------------------------------------

    def _resolve(self, source: SourceRef) -> Path:
        """Resolve the export path the user supplied.

        Args:
            source: The source being analyzed.

        Returns:
            The absolute path to the zip or directory.

        Raises:
            SourceUnavailableError: If the uri is empty or the path does not exist.
            SourceAccessDenied: If the path exists but cannot be read.
        """
        uri = source.uri.strip()
        if not uri:
            raise SourceUnavailableError("LinkedIn export source has no path", source=source)
        path = local_path_for(uri)
        try:
            return path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise SourceUnavailableError(
                f"{path} does not exist; re-download the export from LinkedIn's "
                "'Get a copy of your data' page and point this source at the ZIP",
                source=source,
            ) from exc
        except PermissionError as exc:
            raise SourceAccessDenied(
                f"{path} cannot be read; grant this application access to the file",
                source=source,
            ) from exc
        except OSError as exc:
            raise SourceUnavailableError(
                f"{path} could not be resolved ({exc.strerror or exc})", source=source
            ) from exc

    # -- change detection ---------------------------------------------------------------

    async def fingerprint(self, source: SourceRef) -> str:
        """Probe the export for changes without parsing it.

        Args:
            source: The export source.

        Returns:
            A digest of the zip's bytes, or of every export file's ``(name, size, mtime)``
            for an extracted directory; the never-matching identity digest when the export
            is not currently readable.
        """
        try:
            path = self._resolve(source)
        except (SourceUnavailableError, SourceAccessDenied) as exc:
            logger.debug("linkedin.probe_unavailable", uri=source.uri, error=str(exc))
            return await super().fingerprint(source)

        if path.is_file():
            digest = hashlib.sha256()
            try:
                with path.open("rb") as handle:
                    for block in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
                        digest.update(block)
            except OSError:
                return await super().fingerprint(source)
            return compute_fingerprint(_ZIP_FINGERPRINT_TAG, digest.hexdigest())

        archive = _open_archive(path)
        if not archive.stats:
            return await super().fingerprint(source)
        return compute_fingerprint(
            _DIRECTORY_FINGERPRINT_TAG,
            sorted((name, size, stamp) for name, (size, stamp) in archive.stats.items()),
        )

    # -- analysis -----------------------------------------------------------------------

    async def analyze(self, source: SourceRef) -> AnalysisResult:
        """Parse the export and return everything the user recorded on LinkedIn.

        Args:
            source: A ``linkedin_export`` source pointing at the export ``.zip`` or the
                directory it was extracted to.

        Returns:
            One document per parsed CSV, plus organizations, roles, degrees, projects,
            certifications, awards, publications, languages and skills, joined by
            ``worked_at``, ``studied_at``, ``built``, ``earned``, ``published`` and
            ``used_in`` edges.

        Raises:
            SourceUnavailableError: If the export does not exist or is not readable.
            SourceAccessDenied: If it cannot be opened for permission reasons.
        """
        self.require_supported(source)
        path = self._resolve(source)
        archive = _open_archive(path)

        result = AnalysisResult()
        if not archive.members:
            result.record_error(
                f"{path} contains none of the expected LinkedIn export files "
                f"({', '.join(EXPORT_FILES)}). Point this source at the ZIP LinkedIn "
                "emailed you, or at the folder you extracted it to."
            )
            result.fingerprint = await self.fingerprint(source)
            return result

        person = _clean_name(source.option("name")) or self._person_name(archive)

        handlers = (
            ("profile.csv", self._handle_profile),
            ("positions.csv", self._handle_positions),
            ("education.csv", self._handle_education),
            ("projects.csv", self._handle_projects),
            ("skills.csv", self._handle_skills),
            ("certifications.csv", self._handle_certifications),
            ("honors.csv", self._handle_honors),
            ("publications.csv", self._handle_publications),
            ("languages.csv", self._handle_languages),
        )

        if person:
            result.entities.append(
                ExtractedEntity(
                    kind=EntityKind.PERSON,
                    name=person,
                    attributes={"source": "linkedin_export"},
                    confidence=_EXPORT_CONFIDENCE,
                )
            )

        for filename, handler in handlers:
            text = archive.read(filename)
            if text is None:
                continue
            aliases = FIELD_ALIASES[filename]
            rows = parse_export_csv(text, aliases)
            if not rows:
                result.record_error(f"{filename} was present but held no usable rows.")
                continue
            blocks = await handler(result, rows, aliases, person, filename)
            if blocks:
                result.documents.append(
                    ExtractedDocument(
                        uri=f"{_URI_SCHEME}://{filename}",
                        title=f"LinkedIn — {Path(filename).stem.title()}",
                        text="\n\n".join(blocks),
                        kind=SourceKind.LINKEDIN_EXPORT,
                        metadata={
                            "analyzer": self.name,
                            "export_path": path.as_posix(),
                            "file": filename,
                            "rows": len(rows),
                        },
                    )
                )

        for message in archive.errors:
            result.record_error(message)

        result.deduplicate()
        result.fingerprint = await self.fingerprint(source)
        logger.info(
            "linkedin.analyzed",
            export=str(path),
            files=len(archive.members),
            person=bool(person),
            **result.counts(),
        )
        return result

    # -- per-file handlers ----------------------------------------------------------------

    @staticmethod
    def _person_name(archive: _ExportArchive) -> str | None:
        """Read the export owner's name from ``Profile.csv``.

        Args:
            archive: The opened export.

        Returns:
            ``"First Last"``, or ``None`` when the profile file is absent or nameless.
        """
        text = archive.read("profile.csv")
        if not text:
            return None
        aliases = FIELD_ALIASES["profile.csv"]
        rows = parse_export_csv(text, aliases)
        if not rows:
            return None
        first = _field(rows[0], aliases, "first_name") or ""
        last = _field(rows[0], aliases, "last_name") or ""
        return _clean_name(f"{first} {last}")

    async def _handle_profile(
        self,
        result: AnalysisResult,
        rows: list[dict[str, str]],
        aliases: dict[str, tuple[str, ...]],
        person: str | None,
        filename: str,
    ) -> list[str]:
        """Record the profile headline and summary.

        Args:
            result: The result being assembled.
            rows: Parsed rows.
            aliases: The file's alias map.
            person: The export owner's name.
            filename: The file being handled, for provenance.

        Returns:
            Text blocks for the file's document.
        """
        row = rows[0]
        headline = _field(row, aliases, "headline")
        summary = _field(row, aliases, "summary")
        industry = _field(row, aliases, "industry")
        location = _field(row, aliases, "location")
        websites = _field(row, aliases, "websites")

        if person:
            result.entities.append(
                ExtractedEntity(
                    kind=EntityKind.PERSON,
                    name=person,
                    summary=headline,
                    attributes={
                        key: value
                        for key, value in {
                            "headline": headline,
                            "industry": industry,
                            "location": location,
                            "websites": websites,
                            "source": "linkedin_export",
                        }.items()
                        if value
                    },
                    confidence=_EXPORT_CONFIDENCE,
                )
            )

        blocks = [
            part for part in (person, headline, location, industry, summary, websites) if part
        ]
        if summary:
            extracted = await self._knowledge().extract(
                summary,
                kind=FactKind.ACCOMPLISHMENT,
                context={"source_uri": f"{_URI_SCHEME}://{filename}"},
            )
            result.merge(extracted)
        return blocks

    async def _handle_positions(
        self,
        result: AnalysisResult,
        rows: list[dict[str, str]],
        aliases: dict[str, tuple[str, ...]],
        person: str | None,
        filename: str,
    ) -> list[str]:
        """Emit employers, roles, ``worked_at`` edges and accomplishment facts.

        Args:
            result: The result being assembled.
            rows: Parsed rows.
            aliases: The file's alias map.
            person: The export owner's name.
            filename: The file being handled, for provenance.

        Returns:
            Text blocks for the file's document.
        """
        source_uri = f"{_URI_SCHEME}://{filename}"
        blocks: list[str] = []
        for row in rows:
            organization = _clean_name(_field(row, aliases, "organization"))
            role = _clean_name(_field(row, aliases, "role"))
            if not (organization or role):
                continue
            location = _field(row, aliases, "location")
            start = normalize_export_date(_field(row, aliases, "started_on"))
            end = normalize_export_date(_field(row, aliases, "finished_on"))
            description = _field(row, aliases, "description") or ""

            header = " — ".join(part for part in (role, organization) if part)
            period = " – ".join(part for part in (start, end or "Present") if part)
            blocks.append(
                "\n".join(part for part in (header, location, period, description) if part)
            )

            if organization:
                result.entities.append(
                    ExtractedEntity(
                        kind=EntityKind.ORGANIZATION,
                        name=organization,
                        attributes={
                            key: value
                            for key, value in {
                                "location": location,
                                "date_start": start,
                                "date_end": end,
                                "source": "linkedin_export",
                            }.items()
                            if value
                        },
                        confidence=_EXPORT_CONFIDENCE,
                    )
                )
            if role:
                result.entities.append(
                    ExtractedEntity(
                        kind=EntityKind.ROLE,
                        name=role,
                        attributes={
                            key: value
                            for key, value in {
                                "organization": organization,
                                "date_start": start,
                                "date_end": end,
                            }.items()
                            if value
                        },
                        confidence=_EXPORT_CONFIDENCE,
                    )
                )
            if organization and role:
                result.edges.append(
                    ExtractedEdge(
                        source=(EntityKind.ROLE, role),
                        target=(EntityKind.ORGANIZATION, organization),
                        relation=RelationKind.WORKED_AT,
                        evidence={"analyzer": self.name, "file": filename},
                    )
                )
            if organization and person:
                result.edges.append(
                    ExtractedEdge(
                        source=(EntityKind.PERSON, person),
                        target=(EntityKind.ORGANIZATION, organization),
                        relation=RelationKind.WORKED_AT,
                        evidence={"analyzer": self.name, "file": filename},
                    )
                )

            result.facts.append(
                _line_fact(
                    text=header or organization or role or "",
                    kind=FactKind.RESPONSIBILITY,
                    organization=organization,
                    role=role,
                    date_start=start,
                    date_end=end,
                    source_uri=source_uri,
                )
            )

            if description.strip():
                extracted = await self._knowledge().extract(
                    description,
                    kind=FactKind.ACCOMPLISHMENT,
                    context={
                        "organization": organization,
                        "role": role,
                        "source_uri": source_uri,
                    },
                )
                for fact in extracted.facts:
                    if fact.date_start is None:
                        fact.date_start, fact.date_end = start, end
                    fact.organization = fact.organization or organization
                    fact.role = fact.role or role
                result.merge(extracted)

                if organization:
                    for name in extract_skills(description):
                        result.edges.append(
                            ExtractedEdge(
                                source=(skill_entity_kind(name), name),
                                target=(EntityKind.ORGANIZATION, organization),
                                relation=RelationKind.USED_IN,
                                evidence={"analyzer": self.name, "file": filename},
                            )
                        )
        return blocks

    async def _handle_education(
        self,
        result: AnalysisResult,
        rows: list[dict[str, str]],
        aliases: dict[str, tuple[str, ...]],
        person: str | None,
        filename: str,
    ) -> list[str]:
        """Emit schools, degrees, ``studied_at`` edges and education facts.

        Args:
            result: The result being assembled.
            rows: Parsed rows.
            aliases: The file's alias map.
            person: The export owner's name.
            filename: The file being handled, for provenance.

        Returns:
            Text blocks for the file's document.
        """
        source_uri = f"{_URI_SCHEME}://{filename}"
        blocks: list[str] = []
        for row in rows:
            school = _clean_name(_field(row, aliases, "organization"))
            degree = _clean_name(_field(row, aliases, "degree"))
            field_of_study = _clean_name(_field(row, aliases, "field_of_study"))
            notes = _field(row, aliases, "notes") or ""
            start = normalize_export_date(_field(row, aliases, "started_on"))
            end = normalize_export_date(_field(row, aliases, "finished_on"))
            if not (school or degree):
                continue

            qualification = ", ".join(part for part in (degree, field_of_study) if part) or None
            header = " — ".join(part for part in (qualification, school) if part)
            blocks.append("\n".join(part for part in (header, notes) if part))

            if school:
                result.entities.append(
                    ExtractedEntity(
                        kind=EntityKind.ORGANIZATION,
                        name=school,
                        attributes={
                            key: value
                            for key, value in {
                                "date_start": start,
                                "date_end": end,
                                "source": "linkedin_export",
                            }.items()
                            if value
                        },
                        confidence=_EXPORT_CONFIDENCE,
                    )
                )
            if qualification:
                result.entities.append(
                    ExtractedEntity(
                        kind=EntityKind.EDUCATION,
                        name=qualification,
                        attributes={
                            key: value
                            for key, value in {
                                "organization": school,
                                "field_of_study": field_of_study,
                                "date_start": start,
                                "date_end": end,
                            }.items()
                            if value
                        },
                        confidence=_EXPORT_CONFIDENCE,
                    )
                )
                if school:
                    result.edges.append(
                        ExtractedEdge(
                            source=(EntityKind.EDUCATION, qualification),
                            target=(EntityKind.ORGANIZATION, school),
                            relation=RelationKind.STUDIED_AT,
                            evidence={"analyzer": self.name, "file": filename},
                        )
                    )
            if school and person:
                result.edges.append(
                    ExtractedEdge(
                        source=(EntityKind.PERSON, person),
                        target=(EntityKind.ORGANIZATION, school),
                        relation=RelationKind.STUDIED_AT,
                        evidence={"analyzer": self.name, "file": filename},
                    )
                )

            result.facts.append(
                _line_fact(
                    text=header,
                    kind=FactKind.EDUCATION_ITEM,
                    organization=school,
                    role=qualification,
                    date_start=start,
                    date_end=end,
                    source_uri=source_uri,
                )
            )
            if notes.strip():
                extracted = await self._knowledge().extract(
                    notes,
                    kind=FactKind.EDUCATION_ITEM,
                    context={
                        "organization": school,
                        "role": qualification,
                        "source_uri": source_uri,
                    },
                )
                for fact in extracted.facts:
                    if fact.date_start is None:
                        fact.date_start, fact.date_end = start, end
                result.merge(extracted)
        return blocks

    async def _handle_projects(
        self,
        result: AnalysisResult,
        rows: list[dict[str, str]],
        aliases: dict[str, tuple[str, ...]],
        person: str | None,
        filename: str,
    ) -> list[str]:
        """Emit projects, their technologies and their accomplishment facts.

        Args:
            result: The result being assembled.
            rows: Parsed rows.
            aliases: The file's alias map.
            person: The export owner's name.
            filename: The file being handled, for provenance.

        Returns:
            Text blocks for the file's document.
        """
        source_uri = f"{_URI_SCHEME}://{filename}"
        blocks: list[str] = []
        for row in rows:
            name = _clean_name(_field(row, aliases, "name"))
            if not name:
                continue
            description = _field(row, aliases, "description") or ""
            url = _field(row, aliases, "url")
            start = normalize_export_date(_field(row, aliases, "started_on"))
            end = normalize_export_date(_field(row, aliases, "finished_on"))
            blocks.append("\n".join(part for part in (name, url, description) if part))

            result.entities.append(
                ExtractedEntity(
                    kind=EntityKind.PROJECT,
                    name=name,
                    summary=description[:400] or None,
                    attributes={
                        key: value
                        for key, value in {
                            "url": url,
                            "date_start": start,
                            "date_end": end,
                            "source": "linkedin_export",
                        }.items()
                        if value
                    },
                    confidence=_EXPORT_CONFIDENCE,
                )
            )
            if person:
                result.edges.append(
                    ExtractedEdge(
                        source=(EntityKind.PERSON, person),
                        target=(EntityKind.PROJECT, name),
                        relation=RelationKind.BUILT,
                        evidence={"analyzer": self.name, "file": filename},
                    )
                )
            for skill in extract_skills(f"{name}\n{description}"):
                result.edges.append(
                    ExtractedEdge(
                        source=(skill_entity_kind(skill), skill),
                        target=(EntityKind.PROJECT, name),
                        relation=RelationKind.USED_IN,
                        evidence={"analyzer": self.name, "file": filename},
                    )
                )

            if description.strip():
                extracted = await self._knowledge().extract(
                    description,
                    kind=FactKind.ACCOMPLISHMENT,
                    context={"organization": name, "source_uri": source_uri},
                )
                for fact in extracted.facts:
                    if fact.date_start is None:
                        fact.date_start, fact.date_end = start, end
                result.merge(extracted)
            else:
                result.facts.append(
                    _line_fact(
                        text=name,
                        kind=FactKind.ACCOMPLISHMENT,
                        organization=name,
                        role=None,
                        date_start=start,
                        date_end=end,
                        source_uri=source_uri,
                    )
                )
        return blocks

    async def _handle_skills(
        self,
        result: AnalysisResult,
        rows: list[dict[str, str]],
        aliases: dict[str, tuple[str, ...]],
        person: str | None,
        filename: str,
    ) -> list[str]:
        """Emit a graph node for each skill the user listed on LinkedIn.

        Names the shared vocabulary recognises are canonicalised so they merge with the same
        skill seen in a README or a resume. An unrecognised name becomes a
        :attr:`~app.models.enums.EntityKind.SKILL` rather than a ``TECHNOLOGY``, because
        LinkedIn's skill list is full of capabilities ("Technical Writing", "Teamwork") and
        calling those technologies would make the graph read as nonsense.

        Args:
            result: The result being assembled.
            rows: Parsed rows.
            aliases: The file's alias map.
            person: The export owner's name (unused; skills attach to no one else).
            filename: The file being handled, for provenance.

        Returns:
            Text blocks for the file's document.
        """
        names: list[str] = []
        for row in rows:
            raw = _clean_name(_field(row, aliases, "name"))
            if not raw:
                continue
            canonical = canonical_skill(raw)
            name = canonical or raw
            kind = skill_entity_kind(canonical) if canonical else EntityKind.SKILL
            result.entities.append(
                ExtractedEntity(
                    kind=kind,
                    name=name,
                    aliases=[raw] if raw != name else [],
                    attributes={"source": "linkedin_export"},
                    confidence=_EXPORT_CONFIDENCE,
                )
            )
            names.append(name)
        logger.debug("linkedin.skills", count=len(names), person=bool(person), file=filename)
        return [", ".join(names)] if names else []

    async def _handle_certifications(
        self,
        result: AnalysisResult,
        rows: list[dict[str, str]],
        aliases: dict[str, tuple[str, ...]],
        person: str | None,
        filename: str,
    ) -> list[str]:
        """Emit certifications and the ``earned`` edges to them.

        Args:
            result: The result being assembled.
            rows: Parsed rows.
            aliases: The file's alias map.
            person: The export owner's name.
            filename: The file being handled, for provenance.

        Returns:
            Text blocks for the file's document.
        """
        return self._emit_named_items(
            result,
            rows,
            aliases,
            person,
            filename,
            entity_kind=EntityKind.CERTIFICATION,
            relation=RelationKind.EARNED,
            fact_kind=FactKind.AWARD,
            date_field="started_on",
            context_field="authority",
        )

    async def _handle_honors(
        self,
        result: AnalysisResult,
        rows: list[dict[str, str]],
        aliases: dict[str, tuple[str, ...]],
        person: str | None,
        filename: str,
    ) -> list[str]:
        """Emit awards and the ``earned`` edges to them.

        Args:
            result: The result being assembled.
            rows: Parsed rows.
            aliases: The file's alias map.
            person: The export owner's name.
            filename: The file being handled, for provenance.

        Returns:
            Text blocks for the file's document.
        """
        return self._emit_named_items(
            result,
            rows,
            aliases,
            person,
            filename,
            entity_kind=EntityKind.AWARD,
            relation=RelationKind.EARNED,
            fact_kind=FactKind.AWARD,
            date_field="issued_on",
            context_field="description",
        )

    async def _handle_publications(
        self,
        result: AnalysisResult,
        rows: list[dict[str, str]],
        aliases: dict[str, tuple[str, ...]],
        person: str | None,
        filename: str,
    ) -> list[str]:
        """Emit publications and the ``published`` edges to them.

        Args:
            result: The result being assembled.
            rows: Parsed rows.
            aliases: The file's alias map.
            person: The export owner's name.
            filename: The file being handled, for provenance.

        Returns:
            Text blocks for the file's document.
        """
        return self._emit_named_items(
            result,
            rows,
            aliases,
            person,
            filename,
            entity_kind=EntityKind.PUBLICATION,
            relation=RelationKind.PUBLISHED,
            fact_kind=FactKind.PUBLICATION_ITEM,
            date_field="published_on",
            context_field="publisher",
        )

    async def _handle_languages(
        self,
        result: AnalysisResult,
        rows: list[dict[str, str]],
        aliases: dict[str, tuple[str, ...]],
        person: str | None,
        filename: str,
    ) -> list[str]:
        """Emit a ``language`` entity per spoken language, with its proficiency.

        Args:
            result: The result being assembled.
            rows: Parsed rows.
            aliases: The file's alias map.
            person: The export owner's name (unused).
            filename: The file being handled, for provenance.

        Returns:
            Text blocks for the file's document.
        """
        blocks: list[str] = []
        for row in rows:
            name = _clean_name(_field(row, aliases, "name"))
            if not name:
                continue
            proficiency = _field(row, aliases, "proficiency")
            result.entities.append(
                ExtractedEntity(
                    kind=EntityKind.LANGUAGE,
                    name=name,
                    attributes={
                        key: value
                        for key, value in {
                            "proficiency": proficiency,
                            "source": "linkedin_export",
                        }.items()
                        if value
                    },
                    confidence=_EXPORT_CONFIDENCE,
                )
            )
            blocks.append(f"{name} — {proficiency}" if proficiency else name)
        logger.debug("linkedin.languages", count=len(blocks), person=bool(person), file=filename)
        return blocks

    def _emit_named_items(
        self,
        result: AnalysisResult,
        rows: list[dict[str, str]],
        aliases: dict[str, tuple[str, ...]],
        person: str | None,
        filename: str,
        *,
        entity_kind: EntityKind,
        relation: RelationKind,
        fact_kind: FactKind,
        date_field: str,
        context_field: str,
    ) -> list[str]:
        """Emit the one shape shared by certifications, honors and publications.

        All three are "a named thing, a date, and one line of context", so they differ only
        in which entity kind, relation and fact kind they produce — which is what the
        keyword arguments carry.

        Args:
            result: The result being assembled.
            rows: Parsed rows.
            aliases: The file's alias map.
            person: The export owner's name.
            filename: The file being handled, for provenance.
            entity_kind: Node type to emit.
            relation: Relation from the person to the node.
            fact_kind: Claim category for the emitted fact.
            date_field: Canonical field holding the date.
            context_field: Canonical field holding the one-line context.

        Returns:
            Text blocks for the file's document.
        """
        source_uri = f"{_URI_SCHEME}://{filename}"
        blocks: list[str] = []
        for row in rows:
            name = _clean_name(_field(row, aliases, "name"))
            if not name:
                continue
            context = _field(row, aliases, context_field)
            date = normalize_export_date(_field(row, aliases, date_field))
            url = _field(row, aliases, "url")

            result.entities.append(
                ExtractedEntity(
                    kind=entity_kind,
                    name=name,
                    summary=context,
                    attributes={
                        key: value
                        for key, value in {
                            "date": date,
                            "url": url,
                            "issuer": context,
                            "source": "linkedin_export",
                        }.items()
                        if value
                    },
                    confidence=_EXPORT_CONFIDENCE,
                )
            )
            if person:
                result.edges.append(
                    ExtractedEdge(
                        source=(EntityKind.PERSON, person),
                        target=(entity_kind, name),
                        relation=relation,
                        evidence={"analyzer": self.name, "file": filename},
                    )
                )
            text = " — ".join(part for part in (name, context) if part)
            result.facts.append(
                _line_fact(
                    text=text,
                    kind=fact_kind,
                    organization=context if entity_kind is EntityKind.CERTIFICATION else None,
                    role=None,
                    date_start=date,
                    date_end=None,
                    source_uri=source_uri,
                )
            )
            blocks.append(text)
        return blocks

    # -- internals ------------------------------------------------------------------------

    def _knowledge(self) -> KnowledgeExtractor:
        """Return this instance's extractor, building it once.

        Returns:
            The shared-cache-backed :class:`~app.knowledge.extractors.KnowledgeExtractor`.
        """
        if self._extractor is None:
            self._extractor = knowledge_extractor()
        return self._extractor


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
    """Build a fact from one export row, deterministically.

    An export row *is* the claim — "Firmware Engineering Intern — Acme Robotics" — so it is
    not split into bullets. Skills, metrics and the impact score still come from the shared
    extractors, so a LinkedIn fact ranks against a README fact on the same scale.

    Args:
        text: The claim, verbatim.
        kind: The claim category.
        organization: Employer, school or issuer, when the row names one.
        role: Title or qualification, when the row names one.
        date_start: Period start.
        date_end: Period end.
        source_uri: Provenance uri.

    Returns:
        The fact.
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
        confidence=_EXPORT_CONFIDENCE,
        source_uri=source_uri,
    )
