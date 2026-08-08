"""The user's identity, application profile and automation policy (``docs/CONTRACTS.md`` §14).

Two documents live behind these four endpoints, and keeping them apart is deliberate:

``/profile``
    The **answer sheet**. Every column of :class:`~app.models.profile.UserProfile` — contact
    details, work authorisation, compensation bands, education, the EEO answers — read by
    :mod:`app.browser.autofill` before it ever asks a model anything. A fact that lives here
    is a fact the automation never has to guess.

``/profile/preferences``
    The **policy**. :class:`~app.models.user.UserPreferences`, stored as JSON on
    ``users.preferences``: score floor, daily cap, blocked companies, and the per-user half
    of the kill switch (``auto_apply``).

``GET /profile`` returns the whole :class:`~app.schemas.user.UserRead` rather than the bare
profile, because a user who has not finished onboarding has **no profile row at all** and a
404 there would be wrong — the account exists, the answer sheet is simply blank. The nested
``profile`` field being ``None`` says exactly that, and ``PUT /profile`` creates the row on
first write.

**EEO fields are written, never inferred** (golden rule: never guess). ``gender``,
``race_ethnicity``, ``disability_status`` and ``veteran_status`` accept
:data:`~app.models.profile.DECLINE_TO_SELF_IDENTIFY` and nothing on this path derives them
from anything else. ``None`` means "not answered"; the sentinel means "asked, declined", and
only the sentinel is ever submitted on a form.

There is no ``ProfileService``: both writes are a validated partial applied to one row, so
these handlers own their unit of work and commit it. Every other route group delegates the
commit to its service.
"""

from __future__ import annotations

from typing import Final

import structlog
from fastapi import APIRouter, status

from app.api.deps import CurrentUser, DbSession
from app.models.profile import UserProfile
from app.schemas.user import (
    PreferencesRead,
    PreferencesUpdate,
    ProfileUpdate,
    UserRead,
)

__all__ = ["PREFIX", "TAGS", "router"]

logger = structlog.get_logger(__name__)

#: Path prefix for this group.
PREFIX: Final[str] = "/profile"

#: OpenAPI tag for this group.
TAGS: Final[list[str]] = ["profile"]

router = APIRouter()


async def _profile_row(session: DbSession, user: CurrentUser) -> UserProfile:
    """Return the acting user's profile row, creating an empty one on first write.

    An empty profile is a valid state, not an error: the autofiller reports its blank
    fields as unanswerable and routes those applications to review, which is the correct
    behaviour for a user who has skipped the wizard.

    Args:
        session: The request's database session.
        user: The acting user, whose ``profile`` relationship is eagerly loaded.

    Returns:
        The attached, flushed profile row.
    """
    if user.profile is not None:
        return user.profile
    profile = UserProfile(user_id=user.id)
    session.add(profile)
    await session.flush()
    await session.refresh(user, attribute_names=["profile"])
    logger.info("api.profile_created", user_id=str(user.id))
    return profile


@router.get(
    "",
    response_model=UserRead,
    summary="The acting user, with their profile and preferences",
)
async def read_profile(user: CurrentUser) -> UserRead:
    """Return identity, the application profile, and the automation policy in one payload.

    One request rather than three: the desktop settings screen renders all of it together,
    and the three documents are always read as a set.

    Args:
        user: The acting user.

    Returns:
        The user, with ``profile`` ``None`` until the wizard or a ``PUT`` has written one.
    """
    return UserRead.model_validate(user)


@router.put(
    "",
    response_model=UserRead,
    summary="Update the application profile",
    description="A partial update; fields the client omits are left untouched.",
)
async def update_profile(
    payload: ProfileUpdate,
    user: CurrentUser,
    session: DbSession,
) -> UserRead:
    """Apply a partial update to the acting user's profile.

    ``model_dump(exclude_unset=True)`` is what keeps *absent* and *explicitly null*
    distinguishable: omitting ``phone`` leaves the stored number alone, while sending
    ``"phone": null`` clears it. A whole-object PUT that treated the two the same would
    silently wipe every field a narrow edit screen did not know about.

    Args:
        payload: The fields to change.
        user: The acting user.
        session: The request's database session.

    Returns:
        The user after the write, so the client can re-render without a second request.
    """
    changes = payload.model_dump(exclude_unset=True)
    profile = await _profile_row(session, user)
    for name, value in changes.items():
        setattr(profile, name, value)
    await session.commit()
    await session.refresh(user, attribute_names=["profile"])

    logger.info(
        "api.profile_updated",
        user_id=str(user.id),
        fields=sorted(changes),
    )
    return UserRead.model_validate(user)


@router.get(
    "/preferences",
    response_model=PreferencesRead,
    summary="The automation policy this user's runs obey",
)
async def read_preferences(user: CurrentUser) -> PreferencesRead:
    """Return the stored preference document, with every default filled in.

    Args:
        user: The acting user.

    Returns:
        The policy. A user who has configured nothing still gets a complete document, and
        every default in it is the conservative position — ``auto_apply`` off and
        ``require_no_sponsorship`` on.
    """
    return PreferencesRead.model_validate(user.prefs.model_dump(mode="json"))


@router.put(
    "/preferences",
    response_model=PreferencesRead,
    responses={status.HTTP_400_BAD_REQUEST: {"description": "A value failed validation."}},
    summary="Update the automation policy",
)
async def update_preferences(
    payload: PreferencesUpdate,
    user: CurrentUser,
    session: DbSession,
) -> PreferencesRead:
    """Merge a partial update into the stored preference document.

    The merge goes through :meth:`app.models.user.User.update_prefs`, which re-validates the
    **whole** document rather than the changed keys. That matters because preferences
    constrain each other; validating only the delta would let an invalid combination reach
    the column and fail on the next read instead of on this write.

    ``auto_apply`` is only half of the kill switch. Turning it on here still cannot cause a
    submission: ``settings.auto_apply_enabled`` and ``settings.dry_run`` are evaluated
    independently inside :meth:`app.services.pipeline.Pipeline.submit`, and both default
    closed.

    Args:
        payload: The preference fields to change.
        user: The acting user.
        session: The request's database session.

    Returns:
        The merged, re-validated policy.

    Raises:
        ValueError: If a value fails validation — mapped to 400 by
            :mod:`app.api.errors`. Raised rather than dropped so a misspelled or
            out-of-range setting cannot look like it simply did not take effect.
    """
    changes = payload.model_dump(exclude_unset=True)
    merged = user.update_prefs(**changes)
    await session.commit()

    logger.info(
        "api.preferences_updated",
        user_id=str(user.id),
        fields=sorted(changes),
        auto_apply=merged.auto_apply,
    )
    return PreferencesRead.model_validate(merged.model_dump(mode="json"))
