"""Managing résumé variants, not merely selecting among them.

A run can now choose which variant it tailors from, but the only things a user could do to a
variant were create it and list it. There was no rename, no retarget, no way to change which
one is default, no way to remove one, and no ``GET /resumes/{id}`` at all — a gap
``docs/DEFINITION_OF_DONE.md`` had already flagged.

**The delete is the part with teeth.** ``resumes`` cascades onto ``resume_versions``, and
``applications.resume_version_id`` is ``ON DELETE SET NULL``, so deleting a variant would
quietly erase which résumé a submitted application actually sent. On the development machine
that is not theoretical: 22 versions exist, 11 are named by an application, and one of those
belongs to the product's single confirmed submission. Golden rule #6 says ``content_json`` is
the permanent artefact; ``docs/CONTRACTS.md`` §11 requires the résumé *used*. So the delete
refuses rather than cascading.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.enums import ApplicationStatus
from app.models.resume import Resume, ResumeVersion


@pytest.fixture
async def variants(session, user) -> dict[str, Resume]:
    """Two variants: a default and a second one."""
    rows = {
        "default": Resume(
            user_id=user.id, name="Software", template="modern", is_default=True, config={}
        ),
        "robotics": Resume(
            user_id=user.id,
            name="Robotics",
            variant_label="robotics",
            template="technical",
            is_default=False,
            config={},
        ),
    }
    for row in rows.values():
        session.add(row)
    await session.commit()
    return rows


async def _version_for(session, resume: Resume, application=None) -> ResumeVersion:
    """Persist one version of *resume*, optionally attached to an application."""
    version = ResumeVersion(
        resume_id=resume.id,
        version_number=1,
        content_json={},
        application_id=application.id if application is not None else None,
    )
    session.add(version)
    await session.flush()
    if application is not None:
        application.resume_version_id = version.id
    await session.commit()
    return version


# ======================================================================================
# Reading one
# ======================================================================================


async def test_a_single_variant_can_be_read(api_client, variants) -> None:
    """``GET /resumes/{id}`` did not exist; the list was the only way to see one."""
    response = await api_client.get(f"/api/v1/resumes/{variants['robotics'].id}")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Robotics"
    assert body["template"] == "technical"


async def test_reading_an_unknown_variant_is_404(api_client) -> None:
    """A missing id is not a server error."""
    response = await api_client.get(f"/api/v1/resumes/{uuid.uuid4()}")

    assert response.status_code == 404


async def test_the_versions_route_is_not_shadowed(api_client) -> None:
    """``/resumes/{id}`` and ``/resumes/versions/{id}`` must not collide.

    They do not, because one path segment cannot match two — but adding a single-segment
    route beneath ``/resumes`` is exactly the change that would break version reads, and it
    would break them as a 422 that looks like a client bug.
    """
    response = await api_client.get(f"/api/v1/resumes/versions/{uuid.uuid4()}")

    assert response.status_code == 404


# ======================================================================================
# Updating
# ======================================================================================


async def test_a_variant_can_be_renamed(api_client, variants) -> None:
    """The plainest thing a user wants and could not do."""
    response = await api_client.patch(
        f"/api/v1/resumes/{variants['robotics'].id}", json={"name": "Robotics & Controls"}
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Robotics & Controls"


async def test_renaming_does_not_clear_the_targeting_label(api_client, session, variants) -> None:
    """The reason this is a PATCH and not a PUT.

    ``variant_label`` is nullable, so under a replace "clear the label" and "say nothing
    about the label" are the same request — and a rename would silently untarget the variant.
    """
    await api_client.patch(
        f"/api/v1/resumes/{variants['robotics'].id}", json={"name": "Renamed"}
    )
    await session.refresh(variants["robotics"])

    assert variants["robotics"].variant_label == "robotics"


async def test_the_label_can_be_cleared_explicitly(api_client, session, variants) -> None:
    """An empty string is the request that means "remove it"."""
    await api_client.patch(
        f"/api/v1/resumes/{variants['robotics'].id}", json={"variant_label": ""}
    )
    await session.refresh(variants["robotics"])

    assert variants["robotics"].variant_label is None


async def test_making_one_default_clears_the_others(api_client, session, variants) -> None:
    """"The variant chosen when a request names none" has to be exactly one row.

    Two defaults would make tailoring depend on row order — which is the same invariant the
    create path enforces, through the same helper.
    """
    response = await api_client.patch(
        f"/api/v1/resumes/{variants['robotics'].id}", json={"is_default": True}
    )

    assert response.status_code == 200
    await session.refresh(variants["default"])
    await session.refresh(variants["robotics"])
    assert variants["robotics"].is_default is True
    assert variants["default"].is_default is False


async def test_unsetting_default_is_refused(api_client, variants) -> None:
    """A user does not want *no* default; they want a different one."""
    response = await api_client.patch(
        f"/api/v1/resumes/{variants['default'].id}", json={"is_default": False}
    )

    assert response.status_code == 400


async def test_config_is_replaced_wholesale(api_client, session, variants) -> None:
    """JSON columns are not change tracked, so a partial merge would not be flushed."""
    await api_client.patch(
        f"/api/v1/resumes/{variants['robotics'].id}", json={"config": {"max_bullets": 6}}
    )
    await session.refresh(variants["robotics"])

    assert variants["robotics"].config == {"max_bullets": 6}


async def test_an_empty_patch_changes_nothing(api_client, session, variants) -> None:
    """Every field is optional, so a body with none of them is a no-op, not a wipe."""
    response = await api_client.patch(f"/api/v1/resumes/{variants['robotics'].id}", json={})

    assert response.status_code == 200
    await session.refresh(variants["robotics"])
    assert variants["robotics"].name == "Robotics"
    assert variants["robotics"].variant_label == "robotics"
    assert variants["robotics"].template == "technical"


# ======================================================================================
# Deleting — the part that refuses
# ======================================================================================


async def test_deleting_a_variant_whose_resume_was_submitted_is_refused(
    api_client, session, variants, posting, make_application
) -> None:
    """The whole reason this endpoint has a guard.

    ``resumes`` cascades onto ``resume_versions``, and ``applications.resume_version_id`` is
    ``ON DELETE SET NULL`` — so without this the application would quietly stop knowing which
    résumé the employer received.
    """
    application = await make_application(posting, status=ApplicationStatus.CONFIRMED)
    await _version_for(session, variants["robotics"], application)

    response = await api_client.delete(f"/api/v1/resumes/{variants['robotics'].id}")

    assert response.status_code == 409
    await session.refresh(application)
    assert application.resume_version_id is not None


async def test_the_refusal_says_what_to_do_instead(
    api_client, session, variants, posting, make_application
) -> None:
    """A refusal a user cannot act on is only half a refusal."""
    application = await make_application(posting, status=ApplicationStatus.SUBMITTED)
    await _version_for(session, variants["robotics"], application)

    body = (await api_client.delete(f"/api/v1/resumes/{variants['robotics'].id}")).json()

    assert "Rename it instead" in body["detail"]


async def test_a_variant_with_only_drafts_can_be_deleted(
    api_client, session, variants, posting, make_application
) -> None:
    """A version generated and never sent is working material, not evidence."""
    draft = await make_application(posting, status=ApplicationStatus.READY)
    await _version_for(session, variants["robotics"], draft)

    response = await api_client.delete(f"/api/v1/resumes/{variants['robotics'].id}")

    assert response.status_code == 200
    assert response.json()["data"]["versions_deleted"] == 1
    assert await session.get(Resume, variants["robotics"].id) is None


async def test_an_untouched_variant_can_be_deleted(api_client, session, variants) -> None:
    """The ordinary case: a variant that was never generated from."""
    response = await api_client.delete(f"/api/v1/resumes/{variants['robotics'].id}")

    assert response.status_code == 200
    assert await session.get(Resume, variants["robotics"].id) is None


async def test_deleting_the_default_promotes_another(api_client, session, variants) -> None:
    """The picker is never left with no default while variants exist."""
    response = await api_client.delete(f"/api/v1/resumes/{variants['default'].id}")

    assert response.status_code == 200
    await session.refresh(variants["robotics"])
    assert variants["robotics"].is_default is True


async def test_deleting_the_last_variant_leaves_none(api_client, session, user, variants) -> None:
    """A user may legitimately have zero; the pipeline creates one on first use."""
    for row in variants.values():
        assert (await api_client.delete(f"/api/v1/resumes/{row.id}")).status_code == 200

    from sqlalchemy import func, select

    remaining = await session.scalar(
        select(func.count(Resume.id)).where(Resume.user_id == user.id)
    )
    assert remaining == 0


async def test_deleting_an_unknown_variant_is_404(api_client) -> None:
    """A missing id is not a server error."""
    assert (await api_client.delete(f"/api/v1/resumes/{uuid.uuid4()}")).status_code == 404
