"""Golden rule #6 — knowledge is the source of truth; the rendered file is disposable.

    ``ResumeVersion.content_json`` is kept forever; the rendered PDF is disposable.

``Pipeline.cleanup_application`` is that rule expressed as a method, and it has to get both
halves right. Deleting too little leaves a person's full contact details and employment
history sitting in a file after it has been sent — a liability with no upside. Deleting too
much destroys the only record of what was actually submitted, and it is not regenerable: the
knowledge graph moves on, so re-tailoring next month produces a *different* document.

So every test here asserts the pair: the bytes are gone **and** ``content_json`` is byte-for-
byte what it was. The screenshots are checked too — they are evidence of submission, not
output, and a cleanup that swept them up would destroy the proof the product promises.
"""

from __future__ import annotations

import copy

import pytest

from app.models.enums import ApplicationStatus, DocumentKind
from app.models.file import UploadedFile
from app.models.resume import Resume, ResumeVersion
from app.services.pipeline import Pipeline

#: A realistic tailored-resume document. The exact shape does not matter; that it survives
#: unchanged does.
CONTENT_JSON: dict = {
    "contact": {"name": "Ada Lovelace", "email": "ada@example.com"},
    "summary": "Backend engineer with a bias for latency work.",
    "sections": [
        {
            "heading": "Experience",
            "entries": [
                {
                    "title": "Backend Engineer",
                    "organization": "Acme Robotics",
                    "date_range": "2022 — 2024",
                    "bullets": ["Cut p99 checkout latency from 840ms to 120ms."],
                    "fact_ids": ["11111111-1111-1111-1111-111111111111"],
                }
            ],
        }
    ],
    "skills_line": "Python, Redis, PostgreSQL",
    "meta": {"template": "modern"},
}


@pytest.fixture
async def rendered_application(session, settings, user, posting, make_application):
    """An application with a rendered resume on disk, catalogued and linked.

    Returns a bundle of everything the assertions need: the application, the version, the
    file row, and the real path the bytes were written to.
    """
    application = await make_application(posting, status=ApplicationStatus.SUBMITTED)

    resume = Resume(user_id=user.id, name="Default", template="modern", is_default=True)
    session.add(resume)
    await session.flush()

    storage_key = f"resumes/{application.id}/tailored.pdf"
    path = settings.storage_root / storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"%PDF-1.7 rendered resume bytes"
    path.write_bytes(payload)

    uploaded = UploadedFile(
        user_id=user.id,
        kind=DocumentKind.TAILORED_RESUME,
        filename="tailored.pdf",
        content_type="application/pdf",
        size_bytes=len(payload),
        storage_key=storage_key,
        sha256="0" * 64,
        backend="local",
    )
    session.add(uploaded)
    await session.flush()

    version = ResumeVersion(
        resume_id=resume.id,
        application_id=application.id,
        version_number=1,
        content_json=copy.deepcopy(CONTENT_JSON),
        render_format="pdf",
        file_id=uploaded.id,
        fact_ids=["11111111-1111-1111-1111-111111111111"],
    )
    session.add(version)

    application.resume_version_id = version.id
    await session.commit()
    await session.refresh(application)
    await session.refresh(version)
    await session.refresh(uploaded)

    return {
        "application": application,
        "version": version,
        "file": uploaded,
        "path": path,
    }


# ======================================================================================
# The render goes
# ======================================================================================


async def test_cleanup_deletes_the_rendered_bytes(session, settings, rendered_application) -> None:
    """The PDF is removed from storage."""
    path = rendered_application["path"]
    assert path.is_file(), "fixture did not write the render"

    await Pipeline(session, settings).cleanup_application(rendered_application["application"].id)

    assert not path.exists(), "the rendered resume survived cleanup"


async def test_cleanup_unlinks_and_soft_deletes_the_file_row(
    session, settings, rendered_application
) -> None:
    """Nothing is left pointing at bytes that no longer exist."""
    version = rendered_application["version"]
    uploaded = rendered_application["file"]

    await Pipeline(session, settings).cleanup_application(rendered_application["application"].id)

    await session.refresh(version)
    await session.refresh(uploaded)

    assert version.file_id is None
    assert uploaded.deleted_at is not None


async def test_cleanup_retires_the_version_from_the_documents_view(
    session, settings, rendered_application
) -> None:
    """``deleted_at`` is stamped, so the UI stops offering a download that would 404."""
    version = rendered_application["version"]
    assert version.deleted_at is None

    await Pipeline(session, settings).cleanup_application(rendered_application["application"].id)

    await session.refresh(version)
    assert version.deleted_at is not None


# ======================================================================================
# `content_json` stays — the half that makes the deletion safe
# ======================================================================================


async def test_cleanup_preserves_content_json_exactly(
    session, settings, rendered_application
) -> None:
    """**The point of the rule.** The structured resume is the record; the file was a view.

    Asserted as an exact equality against a deep copy taken before the call, because a
    cleanup that merely left the key present but emptied would pass a truthiness check.
    """
    version = rendered_application["version"]
    before = copy.deepcopy(version.content_json)

    await Pipeline(session, settings).cleanup_application(rendered_application["application"].id)

    await session.refresh(version)
    assert version.content_json == before
    assert version.content_json == CONTENT_JSON
    assert version.content_json["sections"][0]["entries"][0]["bullets"], (
        "content_json survived as an empty husk"
    )


async def test_cleanup_preserves_the_fact_trail(session, settings, rendered_application) -> None:
    """The traceability of golden rule #7 outlives the file it was rendered into."""
    version = rendered_application["version"]
    before = list(version.fact_ids)

    await Pipeline(session, settings).cleanup_application(rendered_application["application"].id)

    await session.refresh(version)
    assert version.fact_ids == before
    assert version.fact_ids


async def test_the_version_row_itself_survives(session, settings, rendered_application) -> None:
    """Soft-deleted, never hard-deleted: the row is the history."""
    from sqlalchemy import select

    version_id = rendered_application["version"].id

    await Pipeline(session, settings).cleanup_application(rendered_application["application"].id)

    still_there = await session.scalar(select(ResumeVersion).where(ResumeVersion.id == version_id))
    assert still_there is not None


# ======================================================================================
# Evidence is not output
# ======================================================================================


async def test_cleanup_leaves_the_confirmation_screenshot_alone(
    session, settings, user, posting, make_application
) -> None:
    """Proof of submission is evidence. If a company says they never received it, it is
    the only thing that settles the question."""
    application = await make_application(posting, status=ApplicationStatus.SUBMITTED)

    storage_key = f"screenshots/{application.id}/after_submit.png"
    path = settings.storage_root / storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG proof")

    screenshot = UploadedFile(
        user_id=user.id,
        kind=DocumentKind.SCREENSHOT,
        filename="after_submit.png",
        content_type="image/png",
        size_bytes=10,
        storage_key=storage_key,
        sha256="1" * 64,
        backend="local",
    )
    session.add(screenshot)
    await session.flush()
    application.confirmation_screenshot_id = screenshot.id
    await session.commit()

    await Pipeline(session, settings).cleanup_application(application.id)

    await session.refresh(screenshot)
    assert path.is_file(), "the proof-of-submission screenshot was deleted"
    assert screenshot.deleted_at is None


# ======================================================================================
# Idempotence
# ======================================================================================


async def test_cleanup_is_idempotent(session, settings, rendered_application) -> None:
    """A second pass is a no-op rather than an error — schedulers call this repeatedly."""
    application_id = rendered_application["application"].id
    pipeline = Pipeline(session, settings)

    await pipeline.cleanup_application(application_id)
    await pipeline.cleanup_application(application_id)  # must not raise

    await session.refresh(rendered_application["version"])
    assert rendered_application["version"].content_json == CONTENT_JSON


async def test_cleanup_of_an_application_with_no_render_is_a_noop(
    session, settings, application
) -> None:
    """An application that never produced a document cleans up without complaint."""
    await Pipeline(session, settings).cleanup_application(application.id)


async def test_cleanup_of_an_unknown_application_raises(session, settings) -> None:
    """A missing application is a caller error, not a silent success."""
    import uuid as uuid_module

    with pytest.raises(LookupError):
        await Pipeline(session, settings).cleanup_application(uuid_module.uuid4())
