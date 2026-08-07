"""Employer records — the shared dimension behind postings and applications.

A :class:`Company` is *reference data*, not owned content. The same row is referenced by
every posting from that employer and by every application the user sends there, which is
what makes "applications per company", "block this company", and "is this a defence
contractor?" answerable with a single join instead of string matching across free text.

The load-bearing column is :attr:`Company.normalized_name`. ATS feeds spell the same
employer a dozen ways — ``"Acme, Inc."``, ``"ACME Inc"``, ``"Acme Incorporated"`` — and a
naive ``name`` lookup would create a new row for each spelling, fragmenting the block list
and the analytics. :meth:`Company.normalize` collapses those spellings to one key, and a
unique index on it makes the collapse a database guarantee rather than a convention.

**Deletion posture.** Companies are never *owned* by a posting or an application, so no
cascade points outward from this table. ``job_postings.company_id`` and
``applications.company_id`` are both ``ON DELETE SET NULL``: purging or merging a company
record must never destroy discovered postings or, far worse, application history. See
``docs/CONTRACTS.md`` §4 for the frozen column list.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.database.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.application import Application
    from app.models.posting import JobPosting

__all__ = ["COMPANY_LEGAL_SUFFIXES", "Company"]


# --------------------------------------------------------------------------------------
# Normalisation vocabulary
# --------------------------------------------------------------------------------------

#: Legal-form tokens stripped from the *end* of a company name before it becomes a
#: normalisation key. Frozen deliberately at the set named in ``docs/CONTRACTS.md`` §4 and
#: the module brief: ``app.jobs.dedupe.normalize_company`` must produce the same key for the
#: same input, and every token added here silently changes historical keys.
COMPANY_LEGAL_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        "ag",
        "co",
        "corp",
        "gmbh",
        "inc",
        "incorporated",
        "limited",
        "llc",
        "ltd",
        "plc",
        "pte",
        "pvt",
        "sa",
    }
)

#: ``&`` is expanded rather than deleted so that ``"AT&T"`` and ``"AT and T"`` collapse to
#: the same key instead of to ``"att"`` and ``"at and t"``.
_AMPERSAND_EXPANSION: Final[str] = " and "

#: Everything that is not an ASCII letter or digit becomes a separator. Runs collapse, so
#: ``"Acme,   Inc."`` and ``"Acme - Inc"`` normalise identically.
_SEPARATOR_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

#: Unicode normal form used before ASCII folding, so ``"Nestlé"`` → ``"Nestle"``.
_UNICODE_NORMAL_FORM: Final[str] = "NFKD"

#: Maximum length of the stored display name and of its normalised key.
COMPANY_NAME_MAX_LENGTH: Final[int] = 255

#: Maximum length of a registrable domain (``example.co.uk``), per RFC 1035.
COMPANY_DOMAIN_MAX_LENGTH: Final[int] = 253

#: Maximum length of a headcount bucket label such as ``"1001-5000"``.
COMPANY_SIZE_BUCKET_MAX_LENGTH: Final[int] = 32

#: Maximum length of a free-text industry label.
COMPANY_INDUSTRY_MAX_LENGTH: Final[int] = 128


class Company(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An employer, deduplicated by :attr:`normalized_name`.

    Rows are created by :meth:`app.services.pipeline.Pipeline.ingest` while resolving a
    :class:`~app.jobs.base.RawPosting`, and enriched later (industry, headcount, defence
    posture) by the company-metadata cache. ``enriched_at`` records when that enrichment
    last ran so the enricher can skip fresh rows.

    Note:
        :attr:`metadata_json` is a plain JSON column, not a tracked mutable. Reassign it
        (``company.metadata_json = {**company.metadata_json, "k": v}``) rather than mutating
        it in place, or the flush will not notice the change.
    """

    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(
        String(COMPANY_NAME_MAX_LENGTH),
        nullable=False,
        doc="Display name exactly as the provider spelled it.",
    )
    normalized_name: Mapped[str] = mapped_column(
        String(COMPANY_NAME_MAX_LENGTH),
        nullable=False,
        unique=True,
        index=True,
        doc="Deduplication key produced by Company.normalize(); one row per employer.",
    )
    domain: Mapped[str | None] = mapped_column(
        String(COMPANY_DOMAIN_MAX_LENGTH),
        nullable=True,
        index=True,
        doc="Primary web domain, used as a secondary identity signal during enrichment.",
    )
    industry: Mapped[str | None] = mapped_column(
        String(COMPANY_INDUSTRY_MAX_LENGTH),
        nullable=True,
        doc="Free-text industry label; matched against UserPreferences.blocked_industries.",
    )
    size_bucket: Mapped[str | None] = mapped_column(
        String(COMPANY_SIZE_BUCKET_MAX_LENGTH),
        nullable=True,
        doc="Headcount band label such as '11-50'.",
    )
    employee_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        doc="Best known headcount; drives UserPreferences.skip_startups_under.",
    )
    is_defense: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="0",
        nullable=False,
        doc="True when the employer is a defence or weapons contractor (policy filter).",
    )
    is_startup: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="0",
        nullable=False,
        doc="True when the employer is an early-stage company.",
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        default=dict,
        nullable=False,
        doc="Provider- and enricher-supplied extras that have no first-class column.",
    )
    enriched_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        doc="When company metadata was last refreshed; NULL means never enriched.",
    )

    # -- relationships ---------------------------------------------------------------
    # No delete cascade in either direction: a company is reference data. Merging or
    # purging one must not take postings or application history with it, so both children
    # carry ON DELETE SET NULL and are loaded explicitly when needed.
    postings: Mapped[list[JobPosting]] = relationship(
        "JobPosting",
        back_populates="company",
        passive_deletes=True,
    )
    applications: Mapped[list[Application]] = relationship(
        "Application",
        back_populates="company",
        passive_deletes=True,
    )

    @staticmethod
    def normalize(name: str | None) -> str:
        """Collapse a company name to its deduplication key.

        The transformation is, in order: Unicode decomposition and ASCII folding, case
        folding, ``&`` expansion to ``" and "``, replacement of every non-alphanumeric run
        with a single space, and repeated removal of trailing legal-form tokens listed in
        :data:`COMPANY_LEGAL_SUFFIXES`.

        Suffix removal only ever strips from the end, and never strips the final remaining
        token. That keeps leading occurrences intact (``"Co-operative Bank Ltd"`` →
        ``"co operative bank"``) and keeps a company legitimately named ``"Limited"`` from
        normalising to the empty string.

        Args:
            name: A raw employer name from a provider feed, a resume, or user input.
                ``None`` and blank strings are accepted.

        Returns:
            The normalisation key, or ``""`` when *name* carries no alphanumeric content.

        Example:
            >>> Company.normalize("Acme, Inc.")
            'acme'
            >>> Company.normalize("ACME Incorporated")
            'acme'
            >>> Company.normalize("AT&T Inc")
            'at and t'
        """
        if not name:
            return ""
        folded = unicodedata.normalize(_UNICODE_NORMAL_FORM, name)
        ascii_only = folded.encode("ascii", "ignore").decode("ascii")
        expanded = ascii_only.lower().replace("&", _AMPERSAND_EXPANSION)
        tokens = _SEPARATOR_PATTERN.sub(" ", expanded).split()
        while len(tokens) > 1 and tokens[-1] in COMPANY_LEGAL_SUFFIXES:
            tokens.pop()
        return " ".join(tokens)

    @validates("name")
    def _sync_normalized_name(self, _key: str, value: str | None) -> str | None:
        """Derive :attr:`normalized_name` from every assignment to :attr:`name`.

        :attr:`normalized_name` is ``NOT NULL`` with no default, so a caller who set only
        :attr:`name` used to hit an ``IntegrityError`` at flush time. Keeping the pair in
        step on the model means no caller can populate half of it, matching how
        :meth:`app.models.knowledge.KnowledgeFact.refresh_derived_fields` guards the same
        derived-column pattern.

        Assigning :attr:`normalized_name` explicitly *after* :attr:`name` still wins, which
        preserves the documented ability to supply a key computed elsewhere (for example by
        ``app.jobs.dedupe.normalize_company``). Loading a row from the database does not
        fire this hook, so stored keys are never rewritten on read.

        Args:
            _key: Attribute name (unused; part of the ``validates`` signature).
            value: The display name being assigned.

        Returns:
            *value* unchanged — only the derived column is written here.
        """
        self.normalized_name = self.normalize(value)
        return value

    def __repr__(self) -> str:
        """Return a debugger-friendly summary that never triggers a lazy load."""
        return (
            f"Company(id={self.id!r}, name={self.name!r}, "
            f"normalized_name={self.normalized_name!r})"
        )
