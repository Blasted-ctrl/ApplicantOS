"""Resume variants, immutable versions, and the "show me what you would send" preview (§14).

Golden rule #6 shapes this whole group: **a resume is a generated view over the knowledge
graph, never a stored file.** So a :class:`~app.models.resume.Resume` is a *variant* — a
name, a template, some generation config, and no content at all — while every generation is
an immutable :class:`~app.models.resume.ResumeVersion` carrying the structured document, the
:class:`~app.models.knowledge.KnowledgeFact` ids every bullet traces back to (golden rule
#7), the token cost, and the model's reasoning.

That is why ``content_json`` is retained forever and the rendered PDF is not.
``GET /resumes/versions/{id}/download`` streams the artifact **when one still exists** and
returns 404 with an explanatory detail when cleanup has reclaimed it — the version has not
been lost, only its bytes, and it can be re-rendered from ``content_json``.

``POST /resumes/preview`` runs the real tailoring pass — the same
:class:`~app.ai.resume_engine.ResumeEngine`, the same anti-hallucination validation — and
creates **no** :class:`~app.models.application.Application` and submits nothing. It runs
inline rather than on a queue because the user is watching it, and it degrades rather than
fails in two places, both deliberately:

* with ``LLM_PROVIDER=null`` the engine's deterministic fallback selects bullets by impact
  score, which is what makes the zero-API-key path produce a real document;
* if the configured template cannot render on this machine (no ``tectonic``, no LaTeX), the
  dependency-free ``markdown`` template is used instead. A blocked preview because a PDF
  engine is missing is a worse outcome for the user than a plainer document.

With ``posting_id`` omitted the engine writes an untargeted resume from the highest-impact
facts — the "master resume" view, and what the onboarding preview renders.
"""

from __future__ import annotations

import hashlib
import mimetypes
import shutil
import uuid
from pathlib import Path
from typing import Any, Final

import structlog
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession, PaginationDep, SettingsDep
from app.api.routes._support import (
    as_artifact_response,
    load_uploaded_file,
    require_owner,
    resume_download_url,
    storage_path,
)
from app.models.enums import ATSProviderName, DocumentKind
from app.models.file import UploadedFile
from app.models.posting import JobPosting
from app.models.resume import Resume, ResumeVersion
from app.schemas.common import Page, paginate
from app.schemas.resume import (
    DEFAULT_RESUME_TEMPLATE,
    ResumeCreate,
    ResumePreviewRequest,
    ResumeRead,
    ResumeVersionRead,
)

__all__ = ["PREFIX", "TAGS", "router"]

logger = structlog.get_logger(__name__)

#: Path prefix for this group.
PREFIX: Final[str] = "/resumes"

#: OpenAPI tag for this group.
TAGS: Final[list[str]] = ["resumes"]

#: Template that needs nothing but the standard library. Mirrors
#: :data:`app.services.pipeline.FALLBACK_TEMPLATE`; the two must agree, or a preview would
#: render through a different path than the application it is previewing.
FALLBACK_TEMPLATE: Final[str] = "markdown"

#: Format the fallback template emits.
FALLBACK_RENDER_FORMAT: Final[str] = "md"

#: Storage-key prefix previews are catalogued under. Distinct from the per-application
#: prefix so a preview can never be mistaken for a document that was actually submitted.
PREVIEW_KEY_TEMPLATE: Final[str] = "users/{user_id}/previews/{version_id}/{filename}"

#: Name given to the variant created on demand when a user previews before creating one.
DEFAULT_VARIANT_NAME: Final[str] = "Default"

#: Title used for the synthetic posting an untargeted preview is written against.
UNTARGETED_TITLE: Final[str] = "General application"

#: Detail returned when a version's rendered bytes have been reclaimed.
_NO_ARTIFACT_DETAIL: Final[str] = (
    "This version has no rendered file. The structured document it was generated from is "
    "retained and can be rendered again; the PDF itself is disposable and was reclaimed "
    "after submission."
)

router = APIRouter()


# ======================================================================================
# Variants
# ======================================================================================


async def _version_count(session: DbSession, resume_id: uuid.UUID) -> int:
    """Count the versions generated from one variant.

    Args:
        session: The request's database session.
        resume_id: The variant.

    Returns:
        The count. Read with an aggregate rather than by loading the collection, which is
        lazily loaded and would raise inside an async session.
    """
    total = await session.scalar(
        select(func.count()).select_from(ResumeVersion).where(ResumeVersion.resume_id == resume_id)
    )
    return int(total or 0)


async def _to_read(session: DbSession, resume: Resume) -> ResumeRead:
    """Project one variant into its API shape.

    Args:
        session: The request's database session.
        resume: The variant.

    Returns:
        The read model with ``version_count`` populated. ``latest_version`` stays ``None``
        here: a variant list is a picker, and dragging one full document per row across the
        wire to render a name would be the wrong trade.
    """
    item = ResumeRead.model_validate(resume)
    item.version_count = await _version_count(session, resume.id)
    return item


async def _default_variant(
    session: DbSession,
    user: CurrentUser,
    template: str,
) -> Resume:
    """Return the user's default variant, creating one the first time it is needed.

    A user who previews before ever visiting the resume screen still needs somewhere for the
    version to hang; refusing the preview to make them create a variant first would be a
    ceremony with no purpose.

    Args:
        session: The request's database session.
        user: The acting user.
        template: Template for the variant when one has to be created.

    Returns:
        The attached, flushed variant.
    """
    existing = await session.scalar(
        select(Resume)
        .where(Resume.user_id == user.id)
        .order_by(Resume.is_default.desc(), Resume.created_at.asc(), Resume.id.asc())
        .limit(1)
    )
    if existing is not None:
        return existing

    created = Resume(
        user_id=user.id,
        name=DEFAULT_VARIANT_NAME,
        template=template,
        is_default=True,
    )
    session.add(created)
    await session.flush()
    logger.info("api.resume_variant_autocreated", user_id=str(user.id), resume_id=str(created.id))
    return created


async def _resolve_variant(
    session: DbSession,
    user: CurrentUser,
    resume_id: uuid.UUID | None,
) -> Resume:
    """Return the variant a preview should hang off.

    Args:
        session: The request's database session.
        user: The acting user.
        resume_id: The variant the client named, or ``None`` for the user's default.

    Returns:
        The variant.

    Raises:
        HTTPException: ``404`` when *resume_id* names nothing this user owns.
    """
    if resume_id is None:
        return await _default_variant(
            session, user, user.prefs.resume_template or DEFAULT_RESUME_TEMPLATE
        )
    variant = await session.get(Resume, resume_id)
    require_owner(variant.user_id if variant is not None else None, user.id, "resume variant")
    return variant  # type: ignore[return-value]  # require_owner raises when it is None


@router.get(
    "",
    response_model=Page[ResumeRead],
    summary="This user's resume variants",
)
async def list_resumes(
    user: CurrentUser,
    session: DbSession,
    page: PaginationDep,
) -> Page[ResumeRead]:
    """Return the user's variants, default first.

    Args:
        user: The acting user.
        session: The request's database session.
        page: Offset/limit pagination.

    Returns:
        One page of variants, each with the number of versions generated from it.
    """
    statement = select(Resume).where(Resume.user_id == user.id)
    total = await session.scalar(select(func.count()).select_from(statement.subquery()))

    rows = await session.execute(
        statement.order_by(Resume.is_default.desc(), Resume.created_at.asc(), Resume.id.asc())
        .limit(page.limit)
        .offset(page.offset)
    )
    resumes = list(rows.scalars().all())
    items = [await _to_read(session, resume) for resume in resumes]
    return paginate(items, total=int(total or 0), params=page)


@router.post(
    "",
    response_model=ResumeRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a resume variant",
)
async def create_resume(
    payload: ResumeCreate,
    user: CurrentUser,
    session: DbSession,
) -> ResumeRead:
    """Create a named variant — configuration only, with no content.

    Marking a variant default clears the flag on the others in the same statement, because
    "the variant chosen when a request names none" has to be exactly one row; two defaults
    would make tailoring depend on row order.

    Args:
        payload: Name, optional targeting label, template and generation config.
        user: The acting user.
        session: The request's database session.

    Returns:
        The created variant.
    """
    resume = Resume(
        user_id=user.id,
        name=payload.name,
        variant_label=payload.variant_label,
        template=payload.template,
        is_default=payload.is_default,
        config=dict(payload.config),
    )
    session.add(resume)
    await session.flush()

    if payload.is_default:
        await _clear_other_defaults(session, user.id, resume.id)

    await session.commit()
    logger.info(
        "api.resume_variant_created",
        user_id=str(user.id),
        resume_id=str(resume.id),
        template=resume.template,
    )
    return await _to_read(session, resume)


async def _clear_other_defaults(
    session: DbSession,
    user_id: uuid.UUID,
    keep: uuid.UUID,
) -> None:
    """Unset ``is_default`` on every one of this user's variants except *keep*.

    Args:
        session: The request's database session.
        user_id: The owning user.
        keep: The variant that stays default.
    """
    rows = await session.execute(
        select(Resume).where(
            Resume.user_id == user_id,
            Resume.id != keep,
            Resume.is_default.is_(True),
        )
    )
    for other in rows.scalars().all():
        other.is_default = False


# ======================================================================================
# Versions
# ======================================================================================


async def _load_version(
    session: DbSession,
    user: CurrentUser,
    version_id: uuid.UUID,
) -> ResumeVersion:
    """Load one version and confirm it belongs to the acting user.

    Ownership lives on the parent variant, so the check joins through it rather than
    trusting the version row, which carries no ``user_id`` of its own.

    Args:
        session: The request's database session.
        user: The acting user.
        version_id: The version being addressed.

    Returns:
        The version, with its variant loaded.

    Raises:
        HTTPException: ``404`` when the id is unknown or belongs to another profile.
    """
    version = await session.scalar(
        select(ResumeVersion)
        .options(selectinload(ResumeVersion.resume))
        .where(ResumeVersion.id == version_id)
        .limit(1)
    )
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No resume version exists with that id.",
        )
    require_owner(version.resume.user_id if version.resume else None, user.id, "resume version")
    return version


def _version_read(version: ResumeVersion) -> ResumeVersionRead:
    """Project a version into its API shape with a working download link.

    Args:
        version: The ORM row.

    Returns:
        The read model. ``download_url`` is set only when a rendered artifact is still
        attached, so a ``None`` link is an honest "there is nothing to download" rather
        than a link that 404s.
    """
    item = ResumeVersionRead.model_validate(version)
    item.has_artifact = version.file_id is not None
    item.download_url = resume_download_url(version.id) if version.file_id else None
    return item


@router.get(
    "/versions/{version_id}",
    response_model=ResumeVersionRead,
    summary="One generated version, with its provenance",
)
async def read_version(
    version_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
) -> ResumeVersionRead:
    """Return one immutable generation: the document, the fact ids, the cost, the reasoning.

    ``fact_ids`` is the audit trail behind golden rule #7 — every bullet in
    ``content_json`` traces to one of them — and it is what makes "nothing is fabricated"
    checkable rather than aspirational.

    Args:
        version_id: The version to read.
        user: The acting user.
        session: The request's database session.

    Returns:
        The version.

    Raises:
        HTTPException: ``404`` when the id is unknown or belongs to another profile.
    """
    return _version_read(await _load_version(session, user, version_id))


@router.get(
    "/versions/{version_id}/download",
    response_class=FileResponse,
    responses={
        status.HTTP_200_OK: {"content": {"application/octet-stream": {}}},
        status.HTTP_404_NOT_FOUND: {"description": "Unknown id, or the bytes were reclaimed."},
    },
    summary="Download the rendered file",
)
async def download_version(
    version_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    settings: SettingsDep,
) -> FileResponse:
    """Stream the rendered artifact for one version.

    Args:
        version_id: The version whose file is wanted.
        user: The acting user.
        session: The request's database session.
        settings: Runtime configuration, supplying the blob root.

    Returns:
        The file.

    Raises:
        HTTPException: ``404`` when the version is unknown, or when its artifact has been
            reclaimed — the detail says so explicitly, because "the resume is gone" and "the
            PDF was cleaned up and can be regenerated" call for very different reactions.
    """
    version = await _load_version(session, user, version_id)
    if version.file_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_NO_ARTIFACT_DETAIL,
        )
    record = await load_uploaded_file(session, version.file_id)
    return as_artifact_response(record, settings)


# ======================================================================================
# Preview
# ======================================================================================


def _preview_posting(posting: JobPosting | None, user_title: str) -> Any:
    """Build the posting DTO a preview is written against.

    Args:
        posting: The real posting, or ``None`` for an untargeted preview.
        user_title: Role title to use when there is no posting.

    Returns:
        A :class:`~app.jobs.base.JobPostingDTO`. The untargeted one is synthetic and carries
        ``ATSProviderName.MANUAL``, which is the provider reserved for records that came
        from a person rather than a feed — so nothing downstream can mistake it for
        something discovered and applyable.
    """
    from app.jobs.base import JobPostingDTO

    if posting is not None:
        return JobPostingDTO.from_model(posting)
    return JobPostingDTO(
        provider=ATSProviderName.MANUAL,
        external_id="preview",
        url="",
        title=user_title,
        description=None,
    )


async def _render_preview(
    document: Any,
    directory: Path,
    template: str,
    max_pages: int,
) -> Any:
    """Render a preview, degrading to the dependency-free template if the engine is absent.

    Args:
        document: The tailored :class:`~app.documents.models.ResumeDocument`.
        directory: Where to write the file.
        template: The template requested.
        max_pages: Page budget the shrink loop enforces.

    Returns:
        The successful :class:`~app.documents.renderer.RenderResult`.

    Raises:
        DocumentRenderError: If neither the requested template nor the fallback could
            produce a file — mapped to 500 by :mod:`app.api.errors`, which quotes the
            engine's message but never its stderr.
    """
    from app.documents.renderer import DocumentRenderError, render_resume

    requested = (template or FALLBACK_TEMPLATE).strip()
    ladder: tuple[tuple[str, str], ...] = (
        ((FALLBACK_TEMPLATE, FALLBACK_RENDER_FORMAT),)
        if requested.lower() == FALLBACK_TEMPLATE
        else ((requested, "pdf"), (FALLBACK_TEMPLATE, FALLBACK_RENDER_FORMAT))
    )

    last: DocumentRenderError = DocumentRenderError(
        "No resume template could be attempted for this preview.",
        engine=requested,
        template=requested,
    )
    for candidate, fmt in ladder:
        out = directory / f"resume.{fmt}"
        try:
            return await render_resume(
                document, out, template=candidate, fmt=fmt, max_pages=max_pages
            )
        except DocumentRenderError as exc:
            last = exc
            logger.warning(
                "api.preview_render_degraded",
                template=candidate,
                fmt=fmt,
                error=str(exc),
            )
    raise last


async def _store_preview(
    session: DbSession,
    user_id: uuid.UUID,
    version_id: uuid.UUID,
    source: Path,
    settings: SettingsDep,
) -> UploadedFile:
    """Copy a rendered preview into the blob store and catalogue it.

    Args:
        session: The request's database session.
        user_id: The owning user; every artifact lives beneath them, which is what keeps
            one profile's documents out of another's scope.
        version_id: The version the file belongs to, used as the key's scope.
        source: The rendered file on disk.
        settings: Runtime configuration, supplying the blob root.

    Returns:
        The catalogued, flushed file record.

    Raises:
        OSError: If the copy fails.
    """
    key = PREVIEW_KEY_TEMPLATE.format(user_id=user_id, version_id=version_id, filename=source.name)
    destination = storage_path(settings, key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)

    digest = hashlib.sha256()
    with destination.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)

    record = UploadedFile(
        user_id=user_id,
        kind=DocumentKind.MASTER_RESUME,
        filename=source.name,
        content_type=mimetypes.guess_type(source.name)[0] or "application/octet-stream",
        size_bytes=destination.stat().st_size,
        storage_key=key,
        sha256=digest.hexdigest(),
        backend="local",
    )
    session.add(record)
    await session.flush()
    return record


@router.post(
    "/preview",
    response_model=ResumeVersionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Tailor a resume without applying to anything",
    description="Runs the real tailoring pass. Creates no application and submits nothing.",
)
async def preview_resume(
    payload: ResumePreviewRequest,
    user: CurrentUser,
    session: DbSession,
    settings: SettingsDep,
) -> ResumeVersionRead:
    """Generate — and optionally render — a resume the user can inspect before committing.

    Everything the apply pipeline would do, minus the application: knowledge retrieval,
    tailoring, fact-id validation, the one-page shrink loop. The result is persisted as a
    :class:`~app.models.resume.ResumeVersion` with ``application_id`` left ``NULL``, so it
    appears in the version history and can be downloaded and compared like any other
    generation — a preview that vanished on refresh would be useless for the one job
    previews have, which is deciding between two documents.

    Args:
        payload: Posting to target (optional), variant, template override, bullet budget and
            whether to render.
        user: The acting user.
        session: The request's database session.
        settings: Runtime configuration.

    Returns:
        The persisted version. ``download_url`` is present only when ``render`` was true and
        a file was produced.

    Raises:
        HTTPException: ``404`` when ``posting_id`` or ``resume_id`` names nothing this user
            can see.
    """
    from app.ai.llm import get_llm
    from app.ai.resume_engine import ResumeEngine, TailorRequest
    from app.cache import get_cache
    from app.jobs.base import UserProfileDTO
    from app.knowledge.retrieval import KnowledgeRetriever

    posting: JobPosting | None = None
    if payload.posting_id is not None:
        posting = await session.scalar(
            select(JobPosting)
            .options(selectinload(JobPosting.company))
            .where(JobPosting.id == payload.posting_id)
            .limit(1)
        )
        if posting is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No posting exists with that id.",
            )

    prefs = user.prefs
    variant = await _resolve_variant(session, user, payload.resume_id)

    template = (
        payload.template or variant.template or prefs.resume_template or settings.resume_template
    ).strip()

    engine = ResumeEngine(
        session,
        get_llm("reasoning"),
        KnowledgeRetriever(session),
        get_cache(),
    )
    tailored = await engine.tailor(
        TailorRequest(
            user=UserProfileDTO.from_model(user),
            posting=_preview_posting(posting, _desired_title(user)),
            prefs=prefs,
            template=template,
            max_bullets=payload.max_bullets,
            variant_label=payload.variant_label or variant.variant_label,
        )
    )

    version = ResumeVersion(
        resume_id=variant.id,
        application_id=None,
        version_number=await _next_version_number(session, variant.id),
        content_json=tailored.document.model_dump(mode="json"),
        render_format=payload.render_format,
        fact_ids=[str(fact_id) for fact_id in tailored.selected_fact_ids],
        token_usage=dict(tailored.token_usage or {}),
        reasoning=tailored.reasoning or None,
    )
    session.add(version)
    await session.flush()

    if payload.render:
        directory = settings.data_path / "previews" / str(version.id)
        directory.mkdir(parents=True, exist_ok=True)
        render = await _render_preview(
            tailored.document, directory, template, int(settings.resume_max_pages)
        )
        stored = await _store_preview(session, user.id, version.id, render.path, settings)
        version.file_id = stored.id
        version.render_format = Path(render.path).suffix.lstrip(".") or payload.render_format

    await session.commit()
    logger.info(
        "api.resume_previewed",
        user_id=str(user.id),
        version_id=str(version.id),
        posting_id=str(payload.posting_id) if payload.posting_id else None,
        bullets=tailored.document.total_bullets(),
        facts=len(tailored.selected_fact_ids),
        degraded=tailored.degraded,
        rendered=payload.render,
    )
    return _version_read(version)


def _desired_title(user: CurrentUser) -> str:
    """Return the role title an untargeted preview is written against.

    Args:
        user: The acting user.

    Returns:
        The first entry of ``profile.desired_roles``, or a neutral label. Read from the
        profile rather than invented, so the untargeted document is aimed at the work the
        user said they want instead of at nothing in particular.
    """
    profile = user.profile
    roles = list(getattr(profile, "desired_roles", None) or []) if profile is not None else []
    for role in roles:
        text = str(role).strip()
        if text:
            return text
    return UNTARGETED_TITLE


async def _next_version_number(session: DbSession, resume_id: uuid.UUID) -> int:
    """Return the next version number for one variant.

    Args:
        session: The request's database session.
        resume_id: The variant.

    Returns:
        One past the highest existing number, starting at 1.
    """
    highest = await session.scalar(
        select(func.max(ResumeVersion.version_number)).where(ResumeVersion.resume_id == resume_id)
    )
    return int(highest or 0) + 1
