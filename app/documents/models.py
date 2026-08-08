"""The resume render model — and the shape of ``ResumeVersion.content_json``.

``docs/CONTRACTS.md`` §11 gives these models two jobs at once, and the second one is the
reason they are pydantic rather than dataclasses:

1. **They are what a template renders.** :class:`ResumeDocument` is the only input a
   :class:`~app.documents.renderer.TemplatePlugin` receives, so everything a LaTeX, DOCX,
   HTML or Markdown template needs to lay out a page has to be expressible here.
2. **They are what the database stores.** ``ResumeVersion.content_json`` holds a serialised
   :class:`ResumeDocument` and is retained forever (golden rule #6 — the rendered PDF is
   disposable, the document is not). Every field therefore has to round-trip losslessly
   through ``model_dump_json`` → ``model_validate_json``: no ``datetime``, no ``UUID``, no
   ``Path``, no ``set``. Dates are strings exactly as they will be printed, and identifiers
   are strings.

The other invariant carried here is provenance. :attr:`ResumeEntry.fact_ids` is the chain
that proves golden rule #7 — *nothing is fabricated* — by tying each bullet back to the
:class:`~app.models.knowledge.KnowledgeFact` it was generated from. It defaults to an empty
list so that hand-built fixtures stay convenient, but a document produced by
:class:`~app.ai.resume_engine.ResumeEngine` that leaves it empty is a bug, not a shortcut:
:meth:`ResumeDocument.all_fact_ids` is what an auditor reads.

**Layout estimation.** :meth:`ResumeDocument.estimated_lines` exists so the one-page shrink
loop in :func:`app.documents.render_resume` can predict *before* rendering how much a step
will buy, and so it can decide how many bullets to drop when shrinking type has run out of
road. The estimate is deliberately physical rather than magical: a page is
:data:`PAGE_WIDTH_IN` × :data:`PAGE_HEIGHT_IN` inches, a glyph averages
:data:`AVG_CHAR_WIDTH_EM` of the font size, and a line of type occupies
:data:`LINE_HEIGHT_RATIO` times the font size. That makes both
:func:`chars_per_line` and :func:`lines_per_page` genuine functions of font size *and*
margin, which is exactly what the ladder varies.

This module imports nothing from ``app`` except the shared enums, so it can be used by the
knowledge engine, the AI layer, the API schemas and the renderer without a cycle.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

import structlog
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AVG_CHAR_WIDTH_EM",
    "BASE_CHARS_PER_LINE",
    "BASE_FONT_SIZE_PT",
    "BASE_LINES_PER_PAGE",
    "BASE_MARGIN_IN",
    "BULLET_INDENT_CHARS",
    "CONTACT_BLOCK_LINES",
    "Contact",
    "CoverLetterDocument",
    "ENTRY_SPACING_LINES",
    "LINE_HEIGHT_RATIO",
    "MIN_BULLETS_PER_SECTION",
    "MIN_CHARS_PER_LINE",
    "PAGE_HEIGHT_IN",
    "PAGE_WIDTH_IN",
    "POINTS_PER_INCH",
    "PRIORITIES_META_KEY",
    "ResumeDocument",
    "ResumeEntry",
    "ResumeSection",
    "SECTION_HEADING_LINES",
    "SKILLS_BLOCK_LINES",
    "SUMMARY_BLOCK_LINES",
    "UNRANKED_BULLET_SCORE",
    "chars_per_line",
    "lines_per_page",
    "wrapped_lines",
]

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------------------
# Page geometry — the assumptions behind every estimate in this module
# --------------------------------------------------------------------------------------

#: US Letter, the only paper size ApplicantOS targets. A resume read by a US hiring manager
#: is printed (or not printed) on this.
PAGE_WIDTH_IN: Final[float] = 8.5
PAGE_HEIGHT_IN: Final[float] = 11.0

#: PostScript points per inch. Font sizes are in points, page geometry is in inches.
POINTS_PER_INCH: Final[float] = 72.0

#: Font size the calibration constants below were measured at.
BASE_FONT_SIZE_PT: Final[float] = 10.0

#: Margin the calibration constants below were measured at.
BASE_MARGIN_IN: Final[float] = 0.5

#: Average glyph advance as a fraction of the font size, for the serif/sans faces the
#: built-in templates use. Roughly half an em for mixed-case English prose.
AVG_CHAR_WIDTH_EM: Final[float] = 0.5

#: Baseline-to-baseline distance as a multiple of the font size.
LINE_HEIGHT_RATIO: Final[float] = 1.2

#: **The characters-per-line constant.** How many characters fit on one line of a
#: :data:`BASE_FONT_SIZE_PT` page with :data:`BASE_MARGIN_IN` margins — the anchor point
#: that :func:`chars_per_line` scales from. Derived as
#: ``(8.5 - 2×0.5) inches × 72 pt/in ÷ (0.5 × 10 pt)`` = 108.
BASE_CHARS_PER_LINE: Final[int] = int(
    (PAGE_WIDTH_IN - 2 * BASE_MARGIN_IN) * POINTS_PER_INCH / (AVG_CHAR_WIDTH_EM * BASE_FONT_SIZE_PT)
)

#: Lines that fit on one :data:`BASE_FONT_SIZE_PT` page with :data:`BASE_MARGIN_IN`
#: margins — ``(11 - 2×0.5) inches × 72 pt/in ÷ (1.2 × 10 pt)`` = 60.
BASE_LINES_PER_PAGE: Final[int] = int(
    (PAGE_HEIGHT_IN - 2 * BASE_MARGIN_IN)
    * POINTS_PER_INCH
    / (LINE_HEIGHT_RATIO * BASE_FONT_SIZE_PT)
)

#: Floor on the computed characters-per-line, so an absurd font size or margin cannot make
#: the estimator divide by something near zero and report a million lines.
MIN_CHARS_PER_LINE: Final[int] = 20

#: Characters of horizontal room a bullet loses to its marker and hanging indent.
BULLET_INDENT_CHARS: Final[int] = 4

#: Vertical cost of a section heading: the heading itself plus its rule and the space above
#: it, which together read as about two lines of body text.
SECTION_HEADING_LINES: Final[float] = 2.0

#: Vertical cost of the whitespace between two entries.
ENTRY_SPACING_LINES: Final[float] = 0.5

#: Vertical cost of the name-and-contact header block: name, contact line, links line.
CONTACT_BLOCK_LINES: Final[float] = 3.0

#: Fixed overhead of the summary block (its heading), on top of the wrapped summary text.
SUMMARY_BLOCK_LINES: Final[float] = SECTION_HEADING_LINES

#: Fixed overhead of the skills line (its heading), on top of the wrapped skills text.
SKILLS_BLOCK_LINES: Final[float] = SECTION_HEADING_LINES

#: The fewest bullets :meth:`ResumeDocument.drop_lowest_priority_bullets` will leave a
#: section holding. A section with a heading and no content is worse than a longer resume.
MIN_BULLETS_PER_SECTION: Final[int] = 1

#: Key under which a caller may put a bullet-priority map in :attr:`ResumeDocument.meta`.
#: :func:`app.documents.render_resume` reads it when the shrink ladder reaches the
#: drop-bullets rung.
PRIORITIES_META_KEY: Final[str] = "priorities"

#: Score given to a bullet that an explicit priority map does not mention. Far below any
#: plausible real score, so an incomplete map drops the bullets nobody vouched for first
#: rather than silently re-ordering the ones somebody did.
UNRANKED_BULLET_SCORE: Final[float] = -1e9


def chars_per_line(
    font_size: float = BASE_FONT_SIZE_PT,
    margin_in: float = BASE_MARGIN_IN,
) -> int:
    """Return how many characters fit on one line at *font_size* and *margin_in*.

    Scales :data:`BASE_CHARS_PER_LINE` by the two things that actually change it: the type
    gets smaller (more characters per inch) and the text column gets wider (more inches).
    Smaller type and narrower margins both increase the answer, which is precisely why the
    shrink ladder tries them in that order.

    Args:
        font_size: Body font size in points.
        margin_in: Page margin in inches, applied to both sides.

    Returns:
        Characters per line, never below :data:`MIN_CHARS_PER_LINE`.
    """
    text_width_in = PAGE_WIDTH_IN - 2 * max(0.0, margin_in)
    if text_width_in <= 0 or font_size <= 0:
        return MIN_CHARS_PER_LINE
    base_width_in = PAGE_WIDTH_IN - 2 * BASE_MARGIN_IN
    scaled = (
        BASE_CHARS_PER_LINE * (BASE_FONT_SIZE_PT / font_size) * (text_width_in / base_width_in)
    )
    return max(MIN_CHARS_PER_LINE, int(scaled))


def lines_per_page(
    font_size: float = BASE_FONT_SIZE_PT,
    margin_in: float = BASE_MARGIN_IN,
) -> int:
    """Return how many body lines fit on one page at *font_size* and *margin_in*.

    The companion of :func:`chars_per_line`, and the denominator the shrink loop uses to
    turn an estimated line count into an estimated page count.

    Args:
        font_size: Body font size in points.
        margin_in: Page margin in inches, applied to top and bottom.

    Returns:
        Lines per page, never below 1.
    """
    text_height_in = PAGE_HEIGHT_IN - 2 * max(0.0, margin_in)
    if text_height_in <= 0 or font_size <= 0:
        return 1
    base_height_in = PAGE_HEIGHT_IN - 2 * BASE_MARGIN_IN
    scaled = (
        BASE_LINES_PER_PAGE * (BASE_FONT_SIZE_PT / font_size) * (text_height_in / base_height_in)
    )
    return max(1, int(scaled))


def wrapped_lines(text: str, per_line: int) -> int:
    """Return how many lines *text* occupies when wrapped at *per_line* characters.

    Args:
        text: The text to measure. Empty text occupies no lines at all — an absent summary
            costs nothing, unlike an empty-but-present one.
        per_line: Characters per line, from :func:`chars_per_line`.

    Returns:
        The line count; at least 1 for any non-empty text.
    """
    stripped = text.strip()
    if not stripped:
        return 0
    return max(1, math.ceil(len(stripped) / max(1, per_line)))


# --------------------------------------------------------------------------------------
# Document model
# --------------------------------------------------------------------------------------


class _DocumentModel(BaseModel):
    """Shared configuration for every model in this module.

    ``from_attributes`` lets a model be validated straight off an ORM row or a DTO;
    ``str_strip_whitespace`` normalises the ragged strings an LLM produces (and is
    idempotent, so it cannot break the round-trip guarantee); ``extra="ignore"`` means a
    ``content_json`` written by a newer version of the application still loads instead of
    exploding a resume the user can no longer open.
    """

    model_config = ConfigDict(
        from_attributes=True,
        str_strip_whitespace=True,
        extra="ignore",
        validate_assignment=False,
    )


class Contact(_DocumentModel):
    """The header block of a resume or cover letter — who this is and how to reach them.

    ``model_config`` sets ``from_attributes`` (``docs/CONTRACTS.md`` §11) so that
    ``Contact.model_validate(profile_like)`` works directly against a
    :class:`~app.models.profile.UserProfile`-shaped object or a DTO carrying the same
    attribute names, without this module importing the ORM.

    Attributes:
        name: Full name, printed as the document title.
        email: Primary contact email.
        phone: Phone number, already formatted the way it should be printed.
        location: "City, ST" or similar — what the user wants a recruiter to read, not a
            postal address.
        links: Labelled URLs, conventionally keyed ``github``, ``linkedin``, ``portfolio``
            and ``website``. Order is preserved, so templates print them in the order the
            caller chose. Empty values are dropped on validation rather than printed as
            bare labels.
    """

    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    links: dict[str, str] = Field(default_factory=dict)

    @property
    def has_links(self) -> bool:
        """Whether there is at least one non-empty link to print."""
        return any(url.strip() for url in self.links.values())

    def ordered_links(self, preferred: Sequence[str] = ()) -> list[tuple[str, str]]:
        """Return ``(label, url)`` pairs with the well-known labels first.

        Args:
            preferred: Labels to hoist to the front, in the order given. Defaults to the
                conventional resume ordering.

        Returns:
            Non-empty links only, preferred labels first and the rest in insertion order.
        """
        order = list(preferred) or ["github", "linkedin", "portfolio", "website"]
        present = {key: value for key, value in self.links.items() if value.strip()}
        ranked = sorted(
            present.items(),
            key=lambda item: (order.index(item[0]) if item[0] in order else len(order)),
        )
        return ranked

    def contact_line(self, separator: str = " · ") -> str:
        """Return email/phone/location joined into the single line most templates print.

        Args:
            separator: String placed between the parts.

        Returns:
            The joined line, omitting whichever parts are empty.
        """
        return separator.join(part for part in (self.email, self.phone, self.location) if part)


class ResumeEntry(_DocumentModel):
    """One job, project, degree or award — a header plus its bullets.

    Attributes:
        title: Role or project name; the emphasised half of the header.
        organization: Employer, school or repository owner.
        location: Where it happened.
        date_range: Printed exactly as given, e.g. ``"Jun 2024 – Present"``. A string, not
            a date pair, because the printed form is the thing being stored and because
            real resumes carry ranges no date type can express.
        bullets: The accomplishment lines, in the order they should be printed.
        fact_ids: **The provenance chain.** ``fact_ids[i]`` is the
            :class:`~app.models.knowledge.KnowledgeFact` id that ``bullets[i]`` was
            generated from. It defaults to empty so hand-written fixtures stay ergonomic,
            but a generated entry that leaves it empty has broken golden rule #7 — there is
            then nothing to audit a bullet against.
    """

    title: str = ""
    organization: str = ""
    location: str = ""
    date_range: str = ""
    bullets: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)

    @property
    def is_traceable(self) -> bool:
        """Whether every bullet in this entry has a fact id behind it."""
        return len(self.fact_ids) >= len(self.bullets) and bool(self.bullets)

    def fact_id_for(self, index: int) -> str | None:
        """Return the fact id behind ``bullets[index]``, or ``None`` when untracked.

        Args:
            index: Index into :attr:`bullets`.

        Returns:
            The parallel entry in :attr:`fact_ids`, or ``None`` when the lists are ragged.
        """
        if 0 <= index < len(self.fact_ids):
            return self.fact_ids[index]
        return None

    def header_rows(self) -> list[str]:
        """Return the non-empty header rows, as templates lay them out.

        Returns:
            Up to two rows: ``title`` with ``date_range``, then ``organization`` with
            ``location``. Rows whose parts are all empty are omitted.
        """
        rows = []
        primary = " ".join(part for part in (self.title, self.date_range) if part)
        secondary = " ".join(part for part in (self.organization, self.location) if part)
        if primary:
            rows.append(primary)
        if secondary:
            rows.append(secondary)
        return rows

    def estimated_lines(
        self,
        font_size: float = BASE_FONT_SIZE_PT,
        margin_in: float = BASE_MARGIN_IN,
    ) -> float:
        """Return the estimated vertical cost of this entry, in body lines.

        Args:
            font_size: Body font size in points.
            margin_in: Page margin in inches.

        Returns:
            Header rows plus wrapped bullets plus the whitespace after the entry.
        """
        per_line = chars_per_line(font_size, margin_in)
        bullet_width = max(MIN_CHARS_PER_LINE, per_line - BULLET_INDENT_CHARS)
        total = float(sum(wrapped_lines(row, per_line) for row in self.header_rows()))
        total += float(sum(wrapped_lines(bullet, bullet_width) for bullet in self.bullets))
        return total + ENTRY_SPACING_LINES


class ResumeSection(_DocumentModel):
    """A titled group of entries — "Experience", "Projects", "Education".

    Attributes:
        heading: Printed section heading.
        entries: The entries under it, in print order.
    """

    heading: str = ""
    entries: list[ResumeEntry] = Field(default_factory=list)

    def total_bullets(self) -> int:
        """Return how many bullets this section holds across all of its entries."""
        return sum(len(entry.bullets) for entry in self.entries)

    def estimated_lines(
        self,
        font_size: float = BASE_FONT_SIZE_PT,
        margin_in: float = BASE_MARGIN_IN,
    ) -> float:
        """Return the estimated vertical cost of this section, in body lines.

        Args:
            font_size: Body font size in points.
            margin_in: Page margin in inches.

        Returns:
            The heading's cost plus every entry's cost. A section with no entries costs
            nothing — templates skip it entirely.
        """
        if not self.entries:
            return 0.0
        heading_cost = SECTION_HEADING_LINES if self.heading else 0.0
        return heading_cost + sum(
            entry.estimated_lines(font_size, margin_in) for entry in self.entries
        )


class ResumeDocument(_DocumentModel):
    """A complete resume: everything a template needs, and everything the database keeps.

    This is the shape stored in ``ResumeVersion.content_json`` (``docs/CONTRACTS.md`` §4,
    §11). It round-trips losslessly through JSON, which is what makes golden rule #6 work —
    the rendered PDF can be deleted the moment it has been uploaded, because the document
    can always be re-rendered from here.

    Attributes:
        contact: The header block.
        summary: Optional professional summary paragraph.
        sections: The body, in print order.
        skills_line: The dense one-line skills list resumes conventionally carry, kept
            separate from :attr:`sections` because every template lays it out differently.
        meta: Free-form generation metadata — the posting it was tailored for, the model
            that wrote it, the bullet priorities under :data:`PRIORITIES_META_KEY`. Never
            printed; callers must keep it JSON-serialisable.
    """

    contact: Contact = Field(default_factory=Contact)
    summary: str = ""
    sections: list[ResumeSection] = Field(default_factory=list)
    skills_line: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)

    # -- measurement -------------------------------------------------------------------

    def estimated_lines(
        self,
        font_size: float = BASE_FONT_SIZE_PT,
        margin_in: float = BASE_MARGIN_IN,
    ) -> int:
        """Estimate how many body lines this document occupies when typeset.

        The estimate is what lets :func:`app.documents.render_resume` reason about a shrink
        step before paying for a render, and — more importantly — decide *how many* bullets
        to drop once shrinking type has bottomed out. It is a function of *font_size* and
        *margin_in* precisely so the loop can re-ask the question at each rung of the
        ladder.

        What is counted, in the order a template prints it: the contact block; the summary
        (heading plus wrapped text); every section (heading plus, per entry, its header
        rows, each bullet wrapped at the indented width, and the space after it); and the
        skills line.

        Args:
            font_size: Body font size in points, matching a
                :class:`~app.documents.renderer.ShrinkStep`.
            margin_in: Page margin in inches, matching the same step.

        Returns:
            The estimated line count, rounded up. Compare against
            :func:`lines_per_page` — or call :meth:`estimated_pages`.
        """
        per_line = chars_per_line(font_size, margin_in)
        total = CONTACT_BLOCK_LINES if self._has_contact() else 0.0

        summary_lines = wrapped_lines(self.summary, per_line)
        if summary_lines:
            total += SUMMARY_BLOCK_LINES + summary_lines

        total += sum(section.estimated_lines(font_size, margin_in) for section in self.sections)

        skills_lines = wrapped_lines(self.skills_line, per_line)
        if skills_lines:
            total += SKILLS_BLOCK_LINES + skills_lines

        return math.ceil(total)

    def estimated_pages(
        self,
        font_size: float = BASE_FONT_SIZE_PT,
        margin_in: float = BASE_MARGIN_IN,
    ) -> float:
        """Estimate how many pages this document occupies, as a fraction.

        Args:
            font_size: Body font size in points.
            margin_in: Page margin in inches.

        Returns:
            Estimated lines divided by the page capacity at the same settings. ``1.4`` means
            "forty percent of a page too long", which is the number the shrink loop logs.
        """
        return self.estimated_lines(font_size, margin_in) / lines_per_page(font_size, margin_in)

    def average_bullet_lines(
        self,
        font_size: float = BASE_FONT_SIZE_PT,
        margin_in: float = BASE_MARGIN_IN,
    ) -> float:
        """Return the mean vertical cost of one bullet, in body lines.

        The conversion factor between "this resume is nine lines too long" and "drop four
        bullets".

        Args:
            font_size: Body font size in points.
            margin_in: Page margin in inches.

        Returns:
            The mean wrapped line count over every bullet, at least 1.0. Returns 1.0 when
            the document has no bullets.
        """
        bullets = list(self.iter_bullets())
        if not bullets:
            return 1.0
        width = max(MIN_CHARS_PER_LINE, chars_per_line(font_size, margin_in) - BULLET_INDENT_CHARS)
        total = sum(wrapped_lines(text, width) for _, _, _, text in bullets)
        return max(1.0, total / len(bullets))

    def total_bullets(self) -> int:
        """Return the number of bullets in the whole document."""
        return sum(section.total_bullets() for section in self.sections)

    def all_fact_ids(self) -> list[str]:
        """Return every fact id this document cites, de-duplicated, in document order.

        This is the audit trail: ``ResumeVersion.fact_ids`` is populated from it, and every
        id here must exist in the knowledge graph (golden rule #7).

        Returns:
            Unique, non-empty fact ids in the order they first appear.
        """
        seen: dict[str, None] = {}
        for section in self.sections:
            for entry in section.entries:
                for fact_id in entry.fact_ids:
                    if fact_id and fact_id not in seen:
                        seen[fact_id] = None
        return list(seen)

    def iter_bullets(self) -> Iterable[tuple[int, int, int, str]]:
        """Yield ``(section_index, entry_index, bullet_index, text)`` in document order.

        Returns:
            An iterator over every bullet with its full address, which is what makes
            :meth:`drop_lowest_priority_bullets` able to remove one without a search.
        """
        for s_idx, section in enumerate(self.sections):
            for e_idx, entry in enumerate(section.entries):
                for b_idx, bullet in enumerate(entry.bullets):
                    yield (s_idx, e_idx, b_idx, bullet)

    def content_hash(self) -> str:
        """Return a stable SHA-256 of the document's printable content.

        Used as the cache key for a rendered file (``docs/CONTRACTS.md`` §7). :attr:`meta`
        is excluded: re-tailoring against a different posting changes the metadata but not
        the pixels, and :func:`hash` is never used because it is salted per process.

        Returns:
            A 64-character hex digest.
        """
        payload = self.model_dump_json(exclude={"meta"}).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    # -- shrinking ---------------------------------------------------------------------

    def drop_lowest_priority_bullets(
        self,
        n: int,
        priorities: Mapping[str, float] | Sequence[str] | None = None,
    ) -> int:
        """Remove up to *n* bullets in place, least important first.

        The last rung of the shrink ladder. Called only once smaller type and tighter
        margins have failed, because losing content is strictly worse than losing
        whitespace — and never so aggressively that a section is left with a heading and
        nothing under it (:data:`MIN_BULLETS_PER_SECTION`), which looks like a rendering
        bug to the person reading it.

        Args:
            n: How many bullets to remove. Zero or negative removes nothing.
            priorities: How to rank bullets, highest kept longest. Accepts either a mapping
                of fact id — or, failing that, bullet text — to a numeric score, or a
                sequence of fact ids in descending priority order. ``None`` ranks by
                document order, so the last bullet of the last section goes first, which is
                the convention resume writers already follow.

        Returns:
            How many bullets were actually removed, which can be fewer than *n* when the
            per-section floor blocks further removal.
        """
        if n <= 0:
            return 0

        ranking = _normalize_priorities(priorities)
        candidates: list[tuple[float, int, tuple[int, int, int]]] = []
        for order, (s_idx, e_idx, b_idx, text) in enumerate(self.iter_bullets()):
            fact_id = self.sections[s_idx].entries[e_idx].fact_id_for(b_idx)
            score = _priority_of(ranking, fact_id, text, order)
            candidates.append((score, order, (s_idx, e_idx, b_idx)))

        # Lowest score first; on a tie the later bullet goes first, because a resume is
        # written most-important-first and the tail is the part nobody reads.
        candidates.sort(key=lambda item: (item[0], -item[1]))

        remaining = {
            s_idx: section.total_bullets() for s_idx, section in enumerate(self.sections)
        }
        doomed: set[tuple[int, int, int]] = set()
        for _score, _order, address in candidates:
            if len(doomed) >= n:
                break
            s_idx = address[0]
            if remaining[s_idx] <= MIN_BULLETS_PER_SECTION:
                continue
            doomed.add(address)
            remaining[s_idx] -= 1

        if not doomed:
            logger.warning(
                "documents.no_droppable_bullets",
                requested=n,
                total_bullets=self.total_bullets(),
                sections=len(self.sections),
            )
            return 0

        for s_idx, section in enumerate(self.sections):
            for e_idx, entry in enumerate(section.entries):
                keep = [
                    (bullet, entry.fact_id_for(b_idx))
                    for b_idx, bullet in enumerate(entry.bullets)
                    if (s_idx, e_idx, b_idx) not in doomed
                ]
                if len(keep) == len(entry.bullets):
                    continue
                entry.bullets = [bullet for bullet, _ in keep]
                # Keep the provenance chain parallel to the bullets it explains; an entry
                # that never carried fact ids keeps not carrying them.
                if entry.fact_ids:
                    entry.fact_ids = [fact_id or "" for _, fact_id in keep]

        logger.info(
            "documents.bullets_dropped",
            requested=n,
            dropped=len(doomed),
            remaining=self.total_bullets(),
        )
        return len(doomed)

    def configured_priorities(self) -> Mapping[str, float] | Sequence[str] | None:
        """Return the bullet priorities the caller stashed in :attr:`meta`, if any.

        Returns:
            The value of ``meta[``:data:`PRIORITIES_META_KEY```]`` when it is a mapping or a
            sequence of strings, otherwise ``None`` — a malformed value degrades to document
            order rather than failing a render.
        """
        raw = self.meta.get(PRIORITIES_META_KEY)
        if isinstance(raw, Mapping):
            return {str(key): float(value) for key, value in raw.items() if _is_number(value)}
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            return [str(item) for item in raw]
        if raw is not None:
            logger.warning("documents.priorities_ignored", type=type(raw).__name__)
        return None

    # -- internals ---------------------------------------------------------------------

    def _has_contact(self) -> bool:
        """Whether the contact block prints anything at all."""
        contact = self.contact
        return bool(contact.name or contact.contact_line() or contact.has_links)


class CoverLetterDocument(_DocumentModel):
    """A cover letter — the same contact header, addressed prose instead of sections.

    Attributes:
        contact: The sender's header block, shared with :class:`ResumeDocument` so a letter
            and a resume printed for the same application look like a set.
        recipient: Who it is addressed to, e.g. ``"Hiring Manager"``. Never invented: the
            generic form is the honest default when no name is known.
        company: The employer's name, as printed.
        role: The role applied for, as printed.
        body: The letter itself. Paragraphs are separated by blank lines; see
            :meth:`paragraphs`.
        date: The date as it should be printed. A string, not a ``date``, so the document
            round-trips through JSON unchanged and prints identically years later.
    """

    contact: Contact = Field(default_factory=Contact)
    recipient: str = ""
    company: str = ""
    role: str = ""
    body: str = ""
    date: str = ""

    def paragraphs(self) -> list[str]:
        """Split :attr:`body` into printable paragraphs.

        Returns:
            Non-empty paragraphs with internal line breaks collapsed to single spaces, so a
            hard-wrapped LLM response still typesets as flowing prose.
        """
        chunks = self.body.replace("\r\n", "\n").split("\n\n")
        return [" ".join(chunk.split()) for chunk in chunks if chunk.strip()]

    def salutation(self, prefix: str = "Dear") -> str:
        """Return the greeting line.

        Args:
            prefix: Word before the recipient.

        Returns:
            ``"Dear <recipient>,"``, falling back to ``"Hiring Manager"`` when no recipient
            was supplied.
        """
        return f"{prefix} {self.recipient or 'Hiring Manager'},"

    def content_hash(self) -> str:
        """Return a stable SHA-256 of the letter, for content-addressed render caching."""
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# Priority helpers
# --------------------------------------------------------------------------------------


def _is_number(value: object) -> bool:
    """Return whether *value* is a real number (and not a ``bool`` masquerading as one)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _normalize_priorities(
    priorities: Mapping[str, float] | Sequence[str] | None,
) -> dict[str, float] | None:
    """Reduce the accepted priority shapes to one score map.

    Args:
        priorities: A mapping of key to score, a sequence of keys in descending priority,
            or ``None``.

    Returns:
        A key-to-score mapping, or ``None`` to mean "rank by document order". A sequence is
        scored by descending position, so the first element ranks highest.
    """
    if priorities is None:
        return None
    if isinstance(priorities, Mapping):
        return {
            str(key): float(value) for key, value in priorities.items() if _is_number(value)
        }
    if isinstance(priorities, (str, bytes)):
        return None
    ordered = list(priorities)
    return {str(key): float(len(ordered) - index) for index, key in enumerate(ordered)}


def _priority_of(
    ranking: Mapping[str, float] | None,
    fact_id: str | None,
    text: str,
    order: int,
) -> float:
    """Return the priority score for one bullet.

    Args:
        ranking: The normalised score map, or ``None`` for document order.
        fact_id: The bullet's fact id, when it has one — the preferred key, because the
            text may have been rewritten since the priorities were computed.
        text: The bullet text, used as a secondary key.
        order: The bullet's index in document order.

    Returns:
        The looked-up score, or a document-order score (earlier bullets rank higher) when
        the bullet is absent from the map. An unranked bullet is treated as *lower* priority
        than anything explicitly ranked, so an incomplete map still behaves sensibly.
    """
    if ranking:
        if fact_id and fact_id in ranking:
            return ranking[fact_id]
        if text in ranking:
            return ranking[text]
        return UNRANKED_BULLET_SCORE - order
    return float(-order)
