"""Browser automation — the package that drives a real application form.

**In this phase the package contains selector data only.** The Playwright runtime described
in ``docs/CONTRACTS.md`` §12 — :class:`BrowserSession`, :class:`AutoFiller`,
:class:`ApplicationVerifier`, :class:`ArtifactRecorder` — lands in a later phase and will be
re-exported from here alongside what is already published.

That ordering is deliberate rather than incidental. :mod:`app.jobs.selectors`-shaped
knowledge is needed by the provider plugins *now*, and Playwright is a heavyweight optional
dependency that a discovery-only or headless-server deployment has no reason to install. So
this ``__init__`` publishes exactly two names — :class:`~app.browser.selectors.SelectorPack`
and :func:`~app.browser.selectors.pack_for` — and imports nothing that is not in the standard
library or already a hard dependency. Importing ``app.browser`` on a machine with no
Playwright, no browser binaries and no display therefore works, which is what keeps
``app/jobs`` importable and every provider registrable in a slim install.

Until the runtime arrives, :func:`app.jobs._apply.run_browser_apply` finds no apply entry
point here and raises :class:`~app.jobs.base.UnsupportedFlowError`, which the pipeline turns
into a manual-review item naming the URL a human can apply at. Nothing silently pretends to
have submitted an application — the same posture Workday and LinkedIn hold permanently
(golden rules #2 and #10).
"""

from __future__ import annotations

from app.browser.playwright_runner import (
    BrowserArtifacts,
    BrowserAutomationUnavailable,
    BrowserSession,
    BrowserSessionError,
)
from app.browser.recorder import ArtifactRecorder
from app.browser.selectors import SelectorPack, pack_for

__all__ = [
    "ArtifactRecorder",
    "BrowserArtifacts",
    "BrowserAutomationUnavailable",
    "BrowserSession",
    "BrowserSessionError",
    "SelectorPack",
    "pack_for",
]
