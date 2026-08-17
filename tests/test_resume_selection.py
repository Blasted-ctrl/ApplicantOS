"""Choosing which résumé a run tailors from (§11, and Phase 2's multi-résumé mode).

The product is meant to hold several variants — a software one, a robotics one, an
electrical one — and to run through them. Neither half worked. Nothing anywhere resolved a
variant for an apply: ``Pipeline._resume_container`` selected strictly by ``is_default``,
so a user with four variants had three that could never be applied with, and the variant's
own ``template`` and ``variant_label`` columns were read by nobody — the template came from
``prefs.resume_template`` and the label from ``prefs.resume_variant``.

``prefs.resume_variant`` is the sharpest case. It is documented as "the Resume variant to
tailor from" and resolved to *nothing*: it was passed to the engine as a free-text label and
no query ever looked a row up by it, so setting it changed the wording of a prompt and not
which résumé was used.

The chain is four fallbacks, most specific first, and each one is a different question:

1. the variant this **run** was started with — an instruction;
2. the variant ``prefs.resume_variant`` names — a standing preference;
3. the user's default;
4. their oldest, or a new one.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.resume import Resume
from app.services.pipeline import Pipeline
from app.services.session_service import SessionService


@pytest.fixture
async def variants(session, user) -> dict[str, Resume]:
    """Three variants: a default, a named one, and one nothing points at."""
    rows = {
        "default": Resume(
            user_id=user.id,
            name="Software Engineering",
            variant_label="backend",
            template="modern",
            is_default=True,
            config={},
        ),
        "robotics": Resume(
            user_id=user.id,
            name="Robotics Engineering",
            variant_label="robotics",
            template="technical",
            is_default=False,
            config={},
        ),
        "electrical": Resume(
            user_id=user.id,
            name="Electrical Engineering",
            variant_label="electrical",
            template="classic",
            is_default=False,
            config={},
        ),
    }
    for row in rows.values():
        session.add(row)
    await session.commit()
    return rows


# ======================================================================================
# The selection chain
# ======================================================================================


async def test_a_run_can_name_the_variant_it_tailors_from(
    session, settings, user, variants
) -> None:
    """The instruction wins over every standing preference."""
    chosen = await Pipeline(session, settings)._resume_container(
        user, "modern", variants["robotics"].id
    )

    assert chosen.id == variants["robotics"].id


async def test_the_named_preference_finally_resolves_to_a_row(
    session, settings, user, variants
) -> None:
    """``prefs.resume_variant`` selected nothing at all before this.

    It was documented as naming the variant to tailor from, and was only ever forwarded to
    the engine as free text — so a user who set it got their default variant and a slightly
    different prompt.
    """
    user.preferences = {**dict(user.preferences or {}), "resume_variant": "Electrical Engineering"}
    await session.commit()

    chosen = await Pipeline(session, settings)._resume_container(user, "modern")

    assert chosen.id == variants["electrical"].id


async def test_the_run_outranks_the_preference(session, settings, user, variants) -> None:
    """A run is an instruction for this run; a preference is a standing default."""
    user.preferences = {**dict(user.preferences or {}), "resume_variant": "Electrical Engineering"}
    await session.commit()

    chosen = await Pipeline(session, settings)._resume_container(
        user, "modern", variants["robotics"].id
    )

    assert chosen.id == variants["robotics"].id


async def test_the_default_is_used_when_nothing_names_one(
    session, settings, user, variants
) -> None:
    """The behaviour every run had before, preserved exactly."""
    chosen = await Pipeline(session, settings)._resume_container(user, "modern")

    assert chosen.id == variants["default"].id


async def test_a_preference_naming_no_variant_falls_through(
    session, settings, user, variants
) -> None:
    """A renamed or deleted variant must not stop a run, only stop being chosen."""
    user.preferences = {**dict(user.preferences or {}), "resume_variant": "Nonexistent"}
    await session.commit()

    chosen = await Pipeline(session, settings)._resume_container(user, "modern")

    assert chosen.id == variants["default"].id


async def test_a_user_with_no_variant_gets_one(session, settings, user) -> None:
    """Users who have never opened the résumé screen still have to be able to apply."""
    chosen = await Pipeline(session, settings)._resume_container(user, "modern")

    assert chosen.user_id == user.id
    assert chosen.is_default is True


# ======================================================================================
# Ownership — the half that matters if it is wrong
# ======================================================================================


async def test_another_users_variant_is_never_used(session, settings, user, variants) -> None:
    """The second lock on the door the API already refuses at.

    Honouring it would tailor from a résumé belonging to somebody else and send it under
    this user's name. Ignoring it and falling through is the safe failure.
    """
    from app.models.user import User

    stranger = User(email="mallory@example.com", full_name="Mallory", preferences={})
    session.add(stranger)
    await session.flush()
    theirs = Resume(user_id=stranger.id, name="Theirs", template="modern", is_default=True)
    session.add(theirs)
    await session.commit()

    chosen = await Pipeline(session, settings)._resume_container(user, "modern", theirs.id)

    assert chosen.id == variants["default"].id


async def test_starting_a_run_with_a_foreign_variant_is_refused(session, user, variants) -> None:
    """Refused, not silently ignored.

    A run that quietly tailored from a different résumé than the one asked for would send
    documents the user did not choose, and nothing in the result would say so.
    """
    from app.models.user import User

    stranger = User(email="mallory2@example.com", full_name="Mallory", preferences={})
    session.add(stranger)
    await session.flush()
    theirs = Resume(user_id=stranger.id, name="Theirs", template="modern")
    session.add(theirs)
    await session.commit()

    with pytest.raises(LookupError):
        await SessionService(session).start(user.id, "manual", resume_id=theirs.id)


async def test_starting_a_run_with_an_unknown_variant_is_refused(session, user) -> None:
    """Same rule for an id that names nothing at all."""
    with pytest.raises(LookupError):
        await SessionService(session).start(user.id, "manual", resume_id=uuid.uuid4())


async def test_a_run_records_the_variant_it_was_given(session, user, variants) -> None:
    """The run row carries it, so every application the run prepares resolves the same one."""
    run = await SessionService(session).start(
        user.id, "manual", resume_id=variants["robotics"].id
    )

    assert run.resume_id == variants["robotics"].id


async def test_a_run_that_names_none_records_none(session, user, variants) -> None:
    """NULL means "fall back", which is what every run did before it could be given one."""
    run = await SessionService(session).start(user.id, "manual")

    assert run.resume_id is None


# ======================================================================================
# The wiring: a run's choice has to reach the generator
# ======================================================================================


async def test_the_runs_variant_reaches_document_generation(
    session, settings, user, posting, variants, monkeypatch
) -> None:
    """The whole chain in one assertion: request → run row → prepare → generator.

    Every other test here checks a link. This checks that they are joined, which is the part
    that was missing entirely — `prepare` never asked the run which résumé to use.
    """
    seen: list[uuid.UUID | None] = []

    async def _record(self, application, user_, posting_, resume_id=None):
        seen.append(resume_id)
        return {"bullets": 4, "facts": 4}

    monkeypatch.setattr(Pipeline, "_generate_documents", _record)
    run = await SessionService(session).start(
        user.id, "manual", resume_id=variants["robotics"].id
    )

    await Pipeline(session, settings).prepare(posting.id, user.id, session_id=run.id)

    assert seen == [variants["robotics"].id]


async def test_work_outside_a_run_names_no_variant(
    session, settings, user, posting, variants, monkeypatch
) -> None:
    """An application created by hand falls back to the preference and then the default."""
    seen: list[uuid.UUID | None] = []

    async def _record(self, application, user_, posting_, resume_id=None):
        seen.append(resume_id)
        return {"bullets": 4, "facts": 4}

    monkeypatch.setattr(Pipeline, "_generate_documents", _record)

    await Pipeline(session, settings).prepare(posting.id, user.id)

    assert seen == [None]


async def test_the_variant_supplies_its_own_template(session, settings, user, variants) -> None:
    """A variant that cannot carry its own configuration is a name, not a variant.

    `Resume.template` existed and was ignored: the template came from
    `prefs.resume_template`, so selecting a "Robotics" variant configured for the technical
    template rendered it under the software one.
    """
    chosen = await Pipeline(session, settings)._resume_container(
        user, "modern", variants["robotics"].id
    )

    assert chosen.template == "technical"
    assert chosen.variant_label == "robotics"


# ======================================================================================
# Starting a run when one is already going
# ======================================================================================


async def test_joining_an_existing_run_says_so(api_client, session, user, variants) -> None:
    """`start` is idempotent, which made its response ambiguous.

    It hands back the run already in flight rather than opening a second one — right
    behaviour — and returned "Run started and queued." either way, while the trigger, the
    caps and the résumé variant the request named were quietly dropped. Silently ignoring a
    résumé choice is a much bigger thing to be vague about than a cap.
    """
    first = await api_client.post(
        "/api/v1/sessions/start", json={"resume_id": str(variants["default"].id)}
    )
    assert first.status_code == 202
    assert first.json()["data"]["adopted"] is False

    second = await api_client.post(
        "/api/v1/sessions/start", json={"resume_id": str(variants["robotics"].id)}
    )

    body = second.json()
    assert body["data"]["adopted"] is True
    assert "already in flight" in body["message"]
    # And the run really did keep its original variant, which is what the message warns about.
    assert body["data"]["session"]["resume_id"] == str(variants["default"].id)


async def test_a_foreign_variant_is_refused_over_http(api_client, session, user) -> None:
    """The API refuses before the pipeline's own ownership filter is ever consulted."""
    from app.models.user import User

    stranger = User(email="mallory3@example.com", full_name="Mallory", preferences={})
    session.add(stranger)
    await session.flush()
    theirs = Resume(user_id=stranger.id, name="Theirs", template="modern")
    session.add(theirs)
    await session.commit()

    response = await api_client.post(
        "/api/v1/sessions/start", json={"resume_id": str(theirs.id)}
    )

    assert response.status_code == 404


async def test_two_variants_sharing_a_name_resolve_deterministically(
    session, settings, user, variants
) -> None:
    """`resumes` constrains nothing but its primary key, so duplicate names are legal.

    Oldest-first is what makes the choice stable. A preference that resolved to a different
    résumé on different runs would be worse than one that resolved to the "wrong" one
    consistently — the user can see and fix the second.
    """
    duplicate = Resume(
        user_id=user.id,
        name="Robotics Engineering",
        template="classic",
        is_default=False,
        config={},
    )
    session.add(duplicate)
    await session.commit()

    user.preferences = {**dict(user.preferences or {}), "resume_variant": "Robotics Engineering"}
    await session.commit()

    first = await Pipeline(session, settings)._resume_container(user, "modern")
    second = await Pipeline(session, settings)._resume_container(user, "modern")

    assert first.id == second.id == variants["robotics"].id


async def test_a_retry_keeps_the_variant_its_run_chose(
    session, settings, user, posting, variants, monkeypatch
) -> None:
    """`POST /applications/{id}/retry` dispatches without a session id.

    Surfaced by a review that then dismissed it as pre-existing — correctly, since before
    per-run selection everything used the default. It is a real inconsistency now: an
    application a robotics run prepared would re-tailor from the *software* variant on
    retry, with nothing saying so. The row remembers which run it belongs to.
    """
    seen: list[uuid.UUID | None] = []

    async def _record(self, application, user_, posting_, resume_id=None):
        seen.append(resume_id)
        return {"bullets": 4, "facts": 4}

    monkeypatch.setattr(Pipeline, "_generate_documents", _record)
    run = await SessionService(session).start(
        user.id, "manual", resume_id=variants["robotics"].id
    )

    pipeline = Pipeline(session, settings)
    application = await pipeline.prepare(posting.id, user.id, session_id=run.id)
    # Send it back to a preparable state, as a retry does, and prepare again with no run.
    application.status = __import__(
        "app.models.enums", fromlist=["ApplicationStatus"]
    ).ApplicationStatus.FAILED
    await session.commit()

    await pipeline.prepare(posting.id, user.id)

    assert seen == [variants["robotics"].id, variants["robotics"].id]
