"""Browser automation — the package that drives a real application form.

:func:`~app.browser.apply.run_apply` is this package's front door and the apply entry point
:func:`app.jobs._apply.run_browser_apply` resolves by name. It opens a session, fills the
form, uploads the documents, submits only past both switches, and then asks
:class:`~app.browser.verification.ApplicationVerifier` whether the employer really has the
application — which is the whole reason the driver exists (``docs/SAFETY.md`` promises proof
of submission, and a click is not proof).

**Importing this package must never import Playwright**, and does not. Every module here
either is pure data (:mod:`~app.browser.selectors`), talks to a duck-typed session
(:mod:`~app.browser.autofill`, :mod:`~app.browser.verification`), writes files
(:mod:`~app.browser.recorder`), or imports Playwright inside the function that starts a
browser (:mod:`~app.browser.playwright_runner`). A discovery-only or headless-server
deployment therefore imports ``app.browser``, registers every provider, and never installs a
browser binary. When such a deployment does reach a submission, :func:`~app.browser.apply.run_apply`
raises :class:`~app.jobs.base.UnsupportedFlowError` naming the URL a human can apply at —
nothing silently pretends to have submitted an application (golden rules #2 and #10).
"""

from __future__ import annotations

from app.browser.apply import run_apply
from app.browser.autofill import AutoFiller, FieldResolver, UploadFailedError
from app.browser.playwright_runner import (
    BrowserArtifacts,
    BrowserAutomationUnavailable,
    BrowserSession,
    BrowserSessionError,
)
from app.browser.recorder import ArtifactRecorder
from app.browser.selectors import SelectorPack, pack_for
from app.browser.verification import ApplicationVerifier, VerificationResult

__all__ = [
    "ApplicationVerifier",
    "ArtifactRecorder",
    "AutoFiller",
    "BrowserArtifacts",
    "BrowserAutomationUnavailable",
    "BrowserSession",
    "BrowserSessionError",
    "FieldResolver",
    "SelectorPack",
    "UploadFailedError",
    "VerificationResult",
    "pack_for",
    "run_apply",
]
