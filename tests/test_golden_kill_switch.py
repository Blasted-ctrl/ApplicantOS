"""Golden rule #3 — the kill switch. **The most important test in this repository.**

``docs/SAFETY.md`` makes a specific, falsifiable promise:

    ``AutoFiller.submit`` returns ``False`` *before locating or clicking anything* when
    either switch is closed. Not "logs a warning and continues" — it never touches the DOM.

Every assertion here is against :attr:`tests.fakes.FakePage.clicks` and
:attr:`~tests.fakes.FakePage.lookups`, never against the return value. That is the whole
point: a function can ``return False`` and still have clicked something on the way, and a
test that only checks the return value passes against exactly the bug this rule exists to
exclude. ``lookups == []`` is the stronger claim — the DOM was not even *queried* for a
submit control.

The switch matrix is exhaustive by construction: all four combinations of
``auto_apply_enabled`` and ``dry_run``, plus the caller's own ``dry_run`` argument, plus the
provider-posture gate. Only one cell of the matrix may click.
"""

from __future__ import annotations

import itertools

import pytest

from app.browser.autofill import AutoFiller
from app.browser.selectors import GREENHOUSE, WORKDAY
from tests.fakes import FakePage, FakeSession

#: A page where a real submit button exists and would be clickable if the gate ever opened.
#: Every blocked-path test uses this, so a passing assertion cannot be an artefact of there
#: being nothing to click.
SUBMIT_SELECTOR = "#submit_app"


def _clickable_page() -> FakePage:
    """A page whose submit control is present, findable, and confirms on click."""
    return FakePage(
        present={SUBMIT_SELECTOR, f"form {SUBMIT_SELECTOR}", ".application--confirmation"},
        roles={("button", "Submit Application")},
        text="Thank you for applying",
    )


def _filler(session: FakeSession, pack=GREENHOUSE) -> AutoFiller:
    """An :class:`AutoFiller` over *session* with a pack whose submit selector is present."""
    return AutoFiller(session, resolver=None, pack=pack)


# ======================================================================================
# The matrix
# ======================================================================================


@pytest.mark.parametrize(
    ("auto_apply_enabled", "settings_dry_run", "caller_dry_run"),
    [
        combo
        for combo in itertools.product([False, True], [False, True], [False, True])
        # Exclude the single cell that is *allowed* to submit; it is asserted separately.
        if not (combo[0] is True and combo[1] is False and combo[2] is False)
    ],
)
async def test_submit_never_clicks_unless_both_switches_are_open(
    settings,
    monkeypatch,
    auto_apply_enabled: bool,
    settings_dry_run: bool,
    caller_dry_run: bool,
) -> None:
    """Seven of the eight switch combinations must not touch the page at all."""
    monkeypatch.setattr(settings, "auto_apply_enabled", auto_apply_enabled)
    monkeypatch.setattr(settings, "dry_run", settings_dry_run)

    page = _clickable_page()
    session = FakeSession(page=page)
    filler = _filler(session)

    result = await filler.submit(dry_run=caller_dry_run)

    assert result is False
    # The load-bearing assertions. Not the return value — the recorded interactions.
    assert page.clicks == [], "submit clicked with a closed switch"
    assert page.lookups == [], "submit queried the DOM before checking the switch"
    assert page.writes == [], "submit changed page state with a closed switch"
    assert session.screenshots == [], "submit captured proof before the gate"
    assert filler.submit_attempted is False
    assert filler.submit_clicked is False


async def test_submit_clicks_only_when_both_switches_are_open(
    submission_allowed,
) -> None:
    """The one permitted cell: both switches open and the caller did not ask for a rehearsal.

    Without this the matrix above would pass against an ``AutoFiller.submit`` hard-coded to
    ``return False``, which is safe but useless.
    """
    page = _clickable_page()
    session = FakeSession(page=page)
    filler = _filler(session)

    result = await filler.submit(dry_run=False)

    assert result is True
    assert page.clicks != [], "the permitted path did not click"
    assert filler.submit_attempted is True
    assert filler.submit_clicked is True
    # Golden rule / §12 invariant 3: proof before and after.
    assert session.screenshots == ["before_submit", "after_submit"]


async def test_caller_dry_run_narrows_open_settings(submission_allowed) -> None:
    """``dry_run=True`` from the caller wins even when both settings switches are open.

    The argument is narrowing only: a caller that asks for a rehearsal always gets one.
    """
    page = _clickable_page()
    session = FakeSession(page=page)
    filler = _filler(session)

    assert await filler.submit(dry_run=True) is False
    assert page.clicks == []
    assert page.lookups == []


# ======================================================================================
# The individual switches, named
# ======================================================================================


async def test_auto_apply_disabled_alone_blocks(settings, monkeypatch) -> None:
    """``AUTO_APPLY_ENABLED=false`` blocks even with ``DRY_RUN=false``."""
    monkeypatch.setattr(settings, "auto_apply_enabled", False)
    monkeypatch.setattr(settings, "dry_run", False)

    page = _clickable_page()
    filler = _filler(FakeSession(page=page))

    assert await filler.submit(dry_run=False) is False
    assert page.clicks == []
    assert page.lookups == []


async def test_dry_run_alone_blocks(settings, monkeypatch) -> None:
    """``DRY_RUN=true`` blocks even with ``AUTO_APPLY_ENABLED=true``."""
    monkeypatch.setattr(settings, "auto_apply_enabled", True)
    monkeypatch.setattr(settings, "dry_run", True)

    page = _clickable_page()
    filler = _filler(FakeSession(page=page))

    assert await filler.submit(dry_run=False) is False
    assert page.clicks == []
    assert page.lookups == []


async def test_defaults_are_the_safe_position(settings) -> None:
    """A fresh install refuses to submit without anyone configuring anything.

    ``settings`` applies no override to the two switches beyond the packaged defaults, so
    this is the out-of-the-box posture.
    """
    assert settings.auto_apply_enabled is False
    assert settings.dry_run is True
    assert settings.is_submission_allowed is False

    page = _clickable_page()
    filler = _filler(FakeSession(page=page))

    assert await filler.submit(dry_run=False) is False
    assert page.clicks == []


# ======================================================================================
# Provider posture — golden rule #10 as a fourth switch
# ======================================================================================


async def test_provider_that_forbids_automation_never_clicks(submission_allowed) -> None:
    """Workday's pack forbids automation, so both switches open is still not enough."""
    assert WORKDAY.supports_auto_apply is False

    page = _clickable_page()
    session = FakeSession(page=page)
    filler = _filler(session, pack=WORKDAY)

    assert await filler.submit(dry_run=False) is False
    assert page.clicks == []
    assert page.lookups == []
    assert filler.submit_attempted is False


# ======================================================================================
# The gate is evaluated per attempt, not cached
# ======================================================================================


async def test_flipping_the_switch_shut_takes_effect_immediately(
    submission_allowed, monkeypatch
) -> None:
    """A submission is permitted, then the switch is flipped, and the next attempt refuses.

    ``AutoFiller.submit`` calls ``get_settings()`` on every attempt precisely so that killing
    the switch does not need a process restart. If the value were captured in ``__init__``
    this test fails.
    """
    page = _clickable_page()
    filler = _filler(FakeSession(page=page))

    assert await filler.submit(dry_run=False) is True
    clicks_after_first = len(page.clicks)
    assert clicks_after_first == 1

    monkeypatch.setattr(submission_allowed, "auto_apply_enabled", False)

    assert await filler.submit(dry_run=False) is False
    assert len(page.clicks) == clicks_after_first, "clicked after the switch was killed"


# ======================================================================================
# Only a permitted control may be clicked (§12 invariant 4)
# ======================================================================================


async def test_no_submit_control_means_no_click(submission_allowed) -> None:
    """With both switches open but no recognisable control, nothing is clicked."""
    page = FakePage(present=set(), roles=set())
    filler = _filler(FakeSession(page=page))

    assert await filler.submit(dry_run=False) is False
    assert page.clicks == []
    assert page.lookups != [], "expected the DOM to be searched once the gate opened"
    assert filler.submit_attempted is True
    assert filler.submit_clicked is False


async def test_substring_named_button_is_not_clicked(submission_allowed) -> None:
    """ "Submit your details to our newsletter" must never satisfy a "Submit" match.

    Accessible-name matching is whole-string and exact. A substring fallback would click a
    newsletter signup, and that mistake is not recoverable.
    """
    page = FakePage(
        present=set(),
        roles={("button", "Submit your details to our newsletter")},
    )
    filler = _filler(FakeSession(page=page))

    assert await filler.submit(dry_run=False) is False
    assert page.clicks == []
