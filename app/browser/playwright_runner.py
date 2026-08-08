"""The Playwright runtime — one browser session, opened safely and closed unconditionally.

``docs/CONTRACTS.md`` §12. :class:`BrowserSession` is the only place in ApplicantOS that
starts a browser. Everything above it — field discovery, autofilling, submission
verification — receives an already-open :class:`~playwright.async_api.Page` and never worries
about how it got there, which is what keeps the launch options, the artifact layout and the
teardown guarantees written down exactly once.

Three properties of this module matter more than its API:

**Playwright is imported lazily, inside :meth:`BrowserSession.__aenter__`.** ``app.browser``
must import on a machine that has no Playwright, no browser binaries and no display — that is
the normal state of a discovery-only deployment, and of a developer who has not run
``playwright install`` yet. Nothing at module scope here touches the third-party package, so
:mod:`app.browser.selectors` stays importable, every ATS provider stays registrable, and
:func:`app.jobs._apply.run_browser_apply` gets an honest
:class:`~app.jobs.base.UnsupportedFlowError` instead of an import crash halfway through an
apply attempt.

**Teardown is unconditional.** A leaked Chromium process holds a profile lock, and the next
session then fails to start for a reason that has nothing to do with the code that changed.
:meth:`BrowserSession.__aexit__` therefore stops tracing, saves the trace, closes the
context, closes the browser and stops Playwright as five independently-guarded steps: a
failure in any one of them is logged and the remaining four still run. That holds when the
body of the ``async with`` raised, and it holds when the surrounding task was cancelled —
:class:`asyncio.CancelledError` is caught per step and re-raised at the end rather than being
allowed to skip the close calls.

**Blocker detection is conservative and never fatal.** :meth:`BrowserSession.detect_blockers`
answers one question — "is there something here a human has to handle?" — and answers it in
the direction of asking a human. Each probe is guarded on its own, so a selector that a
future Chromium rejects degrades that single signal instead of aborting detection and
letting an apply attempt walk into a captcha. Golden rule #2: never guess.

This module deliberately ships **no** bot-evasion flags. A realistic viewport, User-Agent and
locale are here because an ATS form genuinely renders differently without them; patching
``navigator.webdriver`` or spoofing a fingerprint would be an attempt to defeat a site's
stated wishes, which is the opposite of this project's ToS posture (golden rule #10). When a
site says no — captcha, login wall, Cloudflare interstitial — the application goes to a
human.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import structlog

from app.browser.selectors import SelectorPack

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from types import TracebackType

    from playwright.async_api import Browser, BrowserContext, Page, Playwright, Response

    from app.config.settings import Settings

__all__ = [
    "BLOCKERS",
    "BLOCKER_CAPTCHA",
    "BLOCKER_CLOUDFLARE",
    "BLOCKER_LOGIN_WALL",
    "BLOCKER_MFA",
    "BrowserArtifacts",
    "BrowserAutomationUnavailable",
    "BrowserSession",
    "BrowserSessionError",
    "DESKTOP_USER_AGENT",
    "HAR_FILENAME",
    "PLAYWRIGHT_INSTALL_HINT",
    "TRACE_FILENAME",
    "VIEWPORT_HEIGHT",
    "VIEWPORT_WIDTH",
    "WaitState",
    "slugify_artifact_name",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: Viewport width in CSS pixels. 1440x900 is an ordinary laptop, which matters: ATS forms
#: switch to a mobile layout with different selectors below roughly 900px, and the packs in
#: :mod:`app.browser.selectors` describe the desktop layout.
VIEWPORT_WIDTH: Final[int] = 1440

#: Viewport height in CSS pixels. See :data:`VIEWPORT_WIDTH`.
VIEWPORT_HEIGHT: Final[int] = 900

#: A current desktop Chrome User-Agent. Playwright's default advertises ``HeadlessChrome``,
#: which some employer sites serve a degraded page to — the form then renders without its
#: JavaScript-driven controls and field discovery finds nothing. Keep the major version
#: roughly in step with the bundled Chromium; a wildly stale value is its own tell.
DESKTOP_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

#: Context locale. US English keeps date pickers, number formats and the phrasing of the
#: success markers in :mod:`app.browser.selectors` aligned with what the packs expect.
DEFAULT_LOCALE: Final[str] = "en-US"

#: Filename of the Playwright trace written inside the artifacts directory when
#: ``settings.debug`` is on. Open it with ``playwright show-trace <path>``.
TRACE_FILENAME: Final[str] = "trace.zip"

#: Filename of the HTTP archive recorded alongside the trace in debug mode.
HAR_FILENAME: Final[str] = "network.har"

#: Filename stem used when a screenshot name slugifies to nothing at all.
FALLBACK_SCREENSHOT_STEM: Final[str] = "screenshot"

#: Extension of every screenshot this session writes.
SCREENSHOT_SUFFIX: Final[str] = ".png"

#: Width of the zero-padded ordinal prefixed to each screenshot filename, which is what makes
#: a session's captures sort into the order they were taken. Shared with
#: :class:`app.browser.recorder.ArtifactRecorder` so that the two writers sharing one
#: directory produce comparable names; the manifest's ``recorded_at`` remains the
#: authoritative cross-writer timeline.
SCREENSHOT_ORDINAL_WIDTH: Final[int] = 3

#: Longest slug retained from a caller-supplied screenshot name. Windows caps a full path at
#: 260 characters by default and artifact directories are already nested several levels deep.
MAX_SLUG_LENGTH: Final[int] = 60

#: Timeout for a DOM probe that must not block — blocker detection, cookie-banner dismissal.
#: These run on a page that may be mid-navigation, and waiting the full 30s default for a
#: selector that is legitimately absent would dominate the duration of an apply attempt.
PROBE_TIMEOUT_MS: Final[int] = 1_500

#: Timeout for the cookie-banner click, which must actually land but must not stall the run.
BANNER_CLICK_TIMEOUT_MS: Final[int] = 3_000

#: Per-step timeout during teardown. A wedged browser process must not hang shutdown; the
#: OS reaps the subprocess when Playwright's transport is dropped.
CLEANUP_TIMEOUT_SECONDS: Final[float] = 20.0

#: Most elements any single probe inspects for visibility. A page with two hundred password
#: inputs is not a page this system should be spending seconds on.
MAX_PROBE_ELEMENTS: Final[int] = 8

#: Blocker: a captcha or bot challenge is on the page.
BLOCKER_CAPTCHA: Final[str] = "captcha"

#: Blocker: the site is asking for a second authentication factor.
BLOCKER_MFA: Final[str] = "mfa"

#: Blocker: the application is behind a sign-in form.
BLOCKER_LOGIN_WALL: Final[str] = "login_wall"

#: Blocker: a Cloudflare interstitial is between us and the page.
BLOCKER_CLOUDFLARE: Final[str] = "cloudflare"

#: Every blocker :meth:`BrowserSession.detect_blockers` can report (``docs/CONTRACTS.md`` §12).
BLOCKERS: Final[frozenset[str]] = frozenset(
    {BLOCKER_CAPTCHA, BLOCKER_MFA, BLOCKER_LOGIN_WALL, BLOCKER_CLOUDFLARE}
)

#: What to install when Playwright is missing. Both halves are needed: the Python package
#: alone cannot launch anything without the browser binaries.
PLAYWRIGHT_INSTALL_HINT: Final[str] = (
    "install it with 'pip install playwright' followed by 'playwright install chromium'"
)

#: Load states :meth:`BrowserSession.goto` accepts, matching Playwright's own vocabulary.
WaitState = Literal["commit", "domcontentloaded", "load", "networkidle"]

#: Captcha widgets probed for when no :class:`~app.browser.selectors.SelectorPack` is
#: supplied. Narrower than a pack's ``captcha_markers`` on purpose: this list matches the
#: *visible challenge widgets* of the three vendors that appear in front of ATS forms, not
#: the invisible score-based variants whose presence does not block a submission.
_DEFAULT_CAPTCHA_MARKERS: Final[tuple[str, ...]] = (
    "iframe[src*='recaptcha']",
    "iframe[title*='reCAPTCHA']",
    "iframe[src*='hcaptcha']",
    "iframe[title*='hCaptcha']",
    "iframe[src*='challenges.cloudflare.com']",
    "iframe[title*='Widget containing a Cloudflare security challenge']",
    "div[class*='g-recaptcha']",
    "div[class*='h-captcha']",
    "div[class*='cf-turnstile']",
)

#: Cloudflare's interstitial, which serves a challenge body instead of the page that was
#: asked for. Distinct from :data:`BLOCKER_CAPTCHA` because the remedy differs: a captcha
#: needs a human to solve one puzzle on a page that is otherwise there, an interstitial means
#: the page never loaded at all.
#:
#: The Turnstile iframe is deliberately *absent* here even though Cloudflare ships it. A
#: Turnstile widget embedded in an application form is a captcha on a page that loaded fine,
#: and :data:`_DEFAULT_CAPTCHA_MARKERS` already reports it as one; listing it in both places
#: only produced two names for one obstacle.
_CLOUDFLARE_MARKERS: Final[tuple[str, ...]] = (
    "#challenge-running",
    "#cf-challenge-running",
    "#challenge-form",
    "#cf-error-details",
    "#cf-wrapper",
)

#: Visible copy Cloudflare shows while it decides about a request.
_CLOUDFLARE_TEXT_MARKERS: Final[tuple[str, ...]] = (
    "just a moment",
    "checking your browser before accessing",
    "verifying you are human",
    "enable javascript and cookies to continue",
)

#: Visible copy that means a second authentication factor is being demanded. Matched against
#: page text because MFA prompts carry no stable markup across the identity providers ATSs
#: federate to.
_MFA_TEXT_MARKERS: Final[tuple[str, ...]] = (
    "verification code",
    "two-factor",
    "two factor",
    "2-step verification",
    "authenticator app",
    "authenticator",
    "one-time code",
    "one-time passcode",
    "security code we sent",
)

#: The control that means "sign in first".
_PASSWORD_INPUT_SELECTOR: Final[str] = "input[type='password']"

#: Evidence that the page really is an application form, which is what turns a password field
#: from a login wall into an ordinary "create an account while you apply" control. Kept
#: broad: a false positive here only costs an escalation this system would rather make.
_APPLY_FORM_MARKERS: Final[tuple[str, ...]] = (
    "input[type='file']",
    "#application_form",
    "form#application-form",
    "form[action*='application']",
    "form[action*='/apply']",
    "[class*='application-form']",
    "[class*='ashby-application-form']",
    "[data-automation-id='jobApplicationForm']",
)

#: Context options a Playwright older than the one this module was written against may not
#: accept. Dropped and retried rather than crashing the run (see
#: :meth:`BrowserSession._open_context`).
_OPTIONAL_CONTEXT_KEYS: Final[tuple[str, ...]] = (
    "record_har_path",
    "record_har_content",
    "record_har_mode",
)

#: Runs of characters that are not slug material, collapsed to a single hyphen.
_SLUG_SEPARATOR_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")


# ======================================================================================
# Errors
# ======================================================================================


class BrowserSessionError(RuntimeError):
    """A browser session could not be started, used, or shut down cleanly.

    Raised rather than returned because every caller of this module already sits inside
    :func:`app.jobs._apply.run_browser_apply`, which converts any exception into a reported
    :class:`~app.jobs.base.ApplyResult` — an ambiguous browser outcome must never be read as
    a successful submission (golden rule #2).
    """


class BrowserAutomationUnavailable(BrowserSessionError):
    """Playwright, or its browser binaries, are not installed in this environment.

    Its own type so a slim deployment can be told apart from a genuine automation failure:
    the first is a supported configuration that routes applications to manual review, the
    second is a bug.
    """


# ======================================================================================
# Artifacts
# ======================================================================================


@dataclass(slots=True)
class BrowserArtifacts:
    """Everything one browser session left on disk (``docs/CONTRACTS.md`` §12).

    Attributes:
        screenshots: Every capture taken through :meth:`BrowserSession.screenshot`, in the
            order they were taken. §12 rule 3 requires one before and one after every submit
            attempt, and these paths are what
            :attr:`app.jobs.base.ApplyResult.screenshot_paths` is built from.
        trace: The Playwright trace, when ``settings.debug`` enabled tracing and the trace
            was successfully written during teardown. ``None`` otherwise.
        har: The HTTP archive, when debug mode enabled recording. Bodies are omitted — an
            application form POSTs the user's answers, and this file is not the place for
            them.
    """

    screenshots: list[Path] = field(default_factory=list)
    trace: Path | None = None
    har: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe view of the artifacts, for a manifest or a log line.

        Returns:
            A mapping with ``screenshots`` as a list of strings and ``trace`` / ``har`` as a
            string or ``None``.
        """
        return {
            "screenshots": [str(path) for path in self.screenshots],
            "trace": str(self.trace) if self.trace else None,
            "har": str(self.har) if self.har else None,
        }


def slugify_artifact_name(name: str, *, fallback: str = FALLBACK_SCREENSHOT_STEM) -> str:
    """Reduce *name* to a lowercase, hyphenated, filesystem-safe stem.

    Artifact names come from call sites like ``"before submit"`` or from a posting title, so
    they contain spaces, punctuation, and occasionally a path separator. This collapses them
    to ``[a-z0-9-]`` and truncates, which makes the result safe to join onto a directory on
    every platform — a slug can contain neither ``/`` nor ``\\`` nor ``..``, so it cannot
    escape the directory it is written into.

    Args:
        name: A human-written label.
        fallback: Stem to use when *name* contains no slug material at all.

    Returns:
        The slug, never empty and never longer than :data:`MAX_SLUG_LENGTH`.

    Example:
        >>> slugify_artifact_name("Before submit — Greenhouse")
        'before-submit-greenhouse'
        >>> slugify_artifact_name("../../etc/passwd")
        'etc-passwd'
    """
    collapsed = _SLUG_SEPARATOR_RE.sub("-", (name or "").strip().lower()).strip("-")
    if not collapsed:
        return fallback
    return collapsed[:MAX_SLUG_LENGTH].strip("-") or fallback


# ======================================================================================
# The session
# ======================================================================================


class BrowserSession:
    """One Chromium session: an async context manager around a page and its artifacts.

    Use it as a context manager and nothing else — :attr:`page` is unavailable outside the
    ``async with`` block, by design, because that is the only window in which teardown is
    guaranteed::

        async with BrowserSession(settings, artifacts_dir=recorder.directory) as session:
            await session.goto(posting.apply_url)
            await session.dismiss_cookie_banner(pack)
            blockers = await session.detect_blockers(pack)
            if blockers:
                return ApplyResult.needs_review(ReviewReason.CAPTCHA)
            await session.screenshot("before-submit")

    The session is not re-entrant and not reusable: a second ``__aenter__`` raises. One
    session, one application attempt, one artifacts directory.

    Attributes:
        settings: The runtime configuration this session was launched under.
        artifacts: Paths to everything written to disk, populated as the session runs.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        storage_state: Path | None = None,
        artifacts_dir: Path | None = None,
    ) -> None:
        """Configure a session without starting anything.

        The constructor touches neither Playwright nor the network; it only resolves the
        artifacts directory, which is created eagerly so that a screenshot taken in an
        exception handler cannot fail on a missing parent.

        Args:
            settings: Runtime configuration. ``playwright_headless``,
                ``playwright_slow_mo_ms``, ``playwright_timeout_ms``, ``screenshot_dir`` and
                ``debug`` are read.
            storage_state: A Playwright storage-state file (cookies and local storage) to
                seed the context with, for a site the user has already signed into. A path
                that does not exist is logged and ignored rather than raised: a missing
                cookie jar must not be the thing that fails an application.
            artifacts_dir: Where screenshots, the trace and the HAR are written. Defaults to
                ``settings.screenshot_path``; the pipeline normally passes the per-application
                directory owned by :class:`app.browser.recorder.ArtifactRecorder`.
        """
        self.settings = settings
        self._storage_state = Path(storage_state) if storage_state is not None else None
        self._artifacts_dir = self._resolve_artifacts_dir(settings, artifacts_dir)
        self.artifacts = BrowserArtifacts()

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._tracing_started = False
        self._screenshot_ordinal = 0
        self._entered = False

    # ---------------------------------------------------------------------------------
    # Construction helpers
    # ---------------------------------------------------------------------------------

    @staticmethod
    def _resolve_artifacts_dir(settings: Settings, override: Path | None) -> Path:
        """Return the directory artifacts are written to, created if absent.

        Args:
            settings: Runtime configuration, consulted for ``screenshot_path``.
            override: An explicit directory, or ``None`` to use the settings default.

        Returns:
            An existing, absolute directory.
        """
        directory = Path(override) if override is not None else Path(settings.screenshot_path)
        directory.mkdir(parents=True, exist_ok=True)
        return directory.resolve()

    @property
    def artifacts_dir(self) -> Path:
        """The directory this session writes screenshots, traces and HARs into."""
        return self._artifacts_dir

    @property
    def page(self) -> Page:
        """The open page (``docs/CONTRACTS.md`` §12).

        Returns:
            The single page this session drives.

        Raises:
            BrowserSessionError: When accessed outside the ``async with`` block. Typed
                rather than left as an ``AttributeError`` on ``None`` so the failure names
                the actual mistake.
        """
        if self._page is None:
            raise BrowserSessionError(
                "BrowserSession.page is only available inside 'async with BrowserSession(...)'"
            )
        return self._page

    @property
    def context(self) -> BrowserContext:
        """The browser context backing :attr:`page`.

        Returns:
            The context, which owns cookies, storage state and downloads.

        Raises:
            BrowserSessionError: When the session has not been started.
        """
        if self._context is None:
            raise BrowserSessionError(
                "BrowserSession.context is only available inside "
                "'async with BrowserSession(...)'"
            )
        return self._context

    @property
    def is_open(self) -> bool:
        """Whether the session is currently started."""
        return self._page is not None

    # ---------------------------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------------------------

    async def __aenter__(self) -> BrowserSession:
        """Start Playwright, launch Chromium, and open a configured page.

        Returns:
            This session, ready to drive.

        Raises:
            BrowserAutomationUnavailable: When the ``playwright`` package is not importable,
                or its Chromium binary has not been downloaded.
            BrowserSessionError: When the session has already been entered.
        """
        if self._entered:
            raise BrowserSessionError("BrowserSession is not re-entrant; construct a new one")
        self._entered = True

        playwright_api = self._import_playwright()
        log = logger.bind(
            headless=bool(self.settings.playwright_headless),
            artifacts_dir=str(self._artifacts_dir),
        )
        self._playwright = await playwright_api.async_playwright().start()
        try:
            self._browser = await self._launch_browser(self._playwright)
            self._context = await self._open_context(self._browser)
            self._apply_default_timeouts(self._context)
            await self._start_tracing(self._context)
            self._page = await self._context.new_page()
        except BaseException:
            # Anything at all — a missing browser binary, a cancelled task — must not leave
            # a half-started Playwright driver and a Chromium process behind.
            await self._teardown()
            raise
        log.info("browser.session_started", tracing=self._tracing_started)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Stop tracing, save the trace, and close everything — unconditionally.

        Never suppresses: returns ``False`` so an exception raised inside the ``async with``
        body propagates unchanged. A cancellation that arrives *during* teardown is caught
        per step (so the remaining steps still run) and re-raised afterwards, so the task is
        still observably cancelled.

        Args:
            exc_type: Type of the exception leaving the ``with`` body, if any.
            exc: The exception instance, if any.
            traceback: Its traceback, if any.

        Returns:
            ``False``, always.

        Raises:
            asyncio.CancelledError: When teardown itself was cancelled and no other
                exception is already propagating.
        """
        cancelled = await self._teardown()
        if cancelled and exc_type is None:
            raise asyncio.CancelledError("BrowserSession teardown was cancelled")
        return False

    @staticmethod
    def _import_playwright() -> Any:
        """Import ``playwright.async_api``, lazily and with a useful failure.

        Returns:
            The ``playwright.async_api`` module.

        Raises:
            BrowserAutomationUnavailable: When the package is not installed. The message
                names the install command, because "No module named 'playwright'" arriving
                out of the middle of an apply attempt explains nothing about what to do.
        """
        try:
            import playwright.async_api as playwright_api  # noqa: PLC0415 - lazy by policy
        except ImportError as exc:
            raise BrowserAutomationUnavailable(
                f"Playwright is not installed, so no application can be submitted; "
                f"{PLAYWRIGHT_INSTALL_HINT}"
            ) from exc
        return playwright_api

    async def _launch_browser(self, playwright: Playwright) -> Browser:
        """Launch Chromium with the configured headless and slow-motion settings.

        Args:
            playwright: The started Playwright driver.

        Returns:
            The launched browser.

        Raises:
            BrowserAutomationUnavailable: When the Chromium binary has not been downloaded.
                Playwright reports this as a generic ``Error``, which is indistinguishable
                from a real launch failure to a caller that has not read the message.
        """
        try:
            return await playwright.chromium.launch(
                headless=bool(self.settings.playwright_headless),
                slow_mo=float(self.settings.playwright_slow_mo_ms or 0),
            )
        except Exception as exc:
            message = str(exc)
            if "playwright install" in message.lower() or "executable doesn't exist" in (
                message.lower()
            ):
                raise BrowserAutomationUnavailable(
                    f"Chromium is not installed for Playwright; {PLAYWRIGHT_INSTALL_HINT}"
                ) from exc
            raise BrowserSessionError(f"could not launch Chromium: {exc}") from exc

    def _context_options(self) -> dict[str, Any]:
        """Build the keyword arguments for ``browser.new_context``.

        Returns:
            The context options: a desktop viewport, a real desktop User-Agent, downloads
            accepted (an ATS confirmation is sometimes a PDF), a US English locale, the
            supplied storage state when it exists, and HAR recording in debug mode with
            bodies omitted.
        """
        options: dict[str, Any] = {
            "viewport": {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            "user_agent": DESKTOP_USER_AGENT,
            "accept_downloads": True,
            "locale": DEFAULT_LOCALE,
        }

        state = self._storage_state
        if state is not None:
            if state.is_file():
                options["storage_state"] = str(state)
            else:
                logger.warning("browser.storage_state_missing", path=str(state))

        if self.settings.debug:
            har_path = self._artifacts_dir / HAR_FILENAME
            options["record_har_path"] = str(har_path)
            # A form POST carries the applicant's answers. Record the shape of the traffic
            # for debugging, never its contents (golden rule #4).
            options["record_har_content"] = "omit"
            options["record_har_mode"] = "minimal"
            self.artifacts.har = har_path

        return options

    async def _open_context(self, browser: Browser) -> BrowserContext:
        """Create the browser context, degrading gracefully on an older Playwright.

        Args:
            browser: The launched browser.

        Returns:
            The new context.

        Raises:
            BrowserSessionError: When the context cannot be created even without the
                optional options.
        """
        options = self._context_options()
        try:
            return await browser.new_context(**options)
        except TypeError as exc:
            dropped = [key for key in _OPTIONAL_CONTEXT_KEYS if key in options]
            if not dropped:
                raise BrowserSessionError(f"could not create a browser context: {exc}") from exc
            logger.warning("browser.context_options_unsupported", dropped=dropped, error=str(exc))
            for key in dropped:
                options.pop(key, None)
            self.artifacts.har = None
            try:
                return await browser.new_context(**options)
            except Exception as retry_exc:
                raise BrowserSessionError(
                    f"could not create a browser context: {retry_exc}"
                ) from retry_exc

    def _apply_default_timeouts(self, context: BrowserContext) -> None:
        """Set the context-wide action and navigation timeouts from settings.

        Applied to the context rather than the page so that any page the site opens — a
        popup consent flow, a document preview — inherits them too.

        Args:
            context: The freshly created context.
        """
        timeout_ms = float(self.settings.playwright_timeout_ms)
        context.set_default_timeout(timeout_ms)
        context.set_default_navigation_timeout(timeout_ms)

    async def _start_tracing(self, context: BrowserContext) -> None:
        """Begin a Playwright trace when ``settings.debug`` is on.

        Sources are excluded: a trace is an artifact that may be attached to a bug report,
        and embedding this repository's source into it serves nobody.

        Args:
            context: The context to trace.
        """
        if not self.settings.debug:
            return
        try:
            await context.tracing.start(screenshots=True, snapshots=True, sources=False)
        except Exception as exc:  # noqa: BLE001 - tracing is diagnostics, never load-bearing
            logger.warning("browser.tracing_start_failed", error=str(exc))
            return
        self._tracing_started = True

    async def _teardown(self) -> bool:
        """Stop tracing and close everything, each step guarded independently.

        Returns:
            Whether any step was interrupted by :class:`asyncio.CancelledError`, so
            :meth:`__aexit__` can re-raise it after the remaining steps have run.
        """
        cancelled = False
        cancelled |= await self._run_cleanup_step("tracing_stop", self._stop_tracing())

        context, self._context = self._context, None
        if context is not None:
            cancelled |= await self._run_cleanup_step("context_close", context.close())

        browser, self._browser = self._browser, None
        if browser is not None:
            cancelled |= await self._run_cleanup_step("browser_close", browser.close())

        playwright, self._playwright = self._playwright, None
        if playwright is not None:
            cancelled |= await self._run_cleanup_step("playwright_stop", playwright.stop())

        self._page = None
        if self.artifacts.har is not None and not self.artifacts.har.is_file():
            # The HAR is only flushed on context close; if that step failed, do not leave a
            # path in the manifest pointing at a file that was never written.
            self.artifacts.har = None

        logger.debug(
            "browser.session_closed",
            screenshots=len(self.artifacts.screenshots),
            trace=str(self.artifacts.trace) if self.artifacts.trace else None,
            cancelled=cancelled,
        )
        return cancelled

    async def _stop_tracing(self) -> None:
        """Stop tracing and write the trace file, if tracing was ever started."""
        if not self._tracing_started or self._context is None:
            return
        self._tracing_started = False
        trace_path = self._artifacts_dir / TRACE_FILENAME
        await self._context.tracing.stop(path=str(trace_path))
        if trace_path.is_file():
            self.artifacts.trace = trace_path

    @staticmethod
    async def _run_cleanup_step(step: str, coroutine: Any) -> bool:
        """Await one teardown step, swallowing its failure so the next step still runs.

        Args:
            step: Name of the step, for the log line.
            coroutine: The awaitable performing it.

        Returns:
            ``True`` when the step was cancelled, so the caller can re-raise afterwards.
            ``False`` when it succeeded or failed for any other reason.
        """
        try:
            await asyncio.wait_for(coroutine, timeout=CLEANUP_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            logger.warning("browser.cleanup_cancelled", step=step)
            return True
        except TimeoutError:
            logger.warning("browser.cleanup_timeout", step=step, seconds=CLEANUP_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - one failed close must not skip the others
            logger.warning(
                "browser.cleanup_failed", step=step, error=str(exc), error_type=type(exc).__name__
            )
        return False

    # ---------------------------------------------------------------------------------
    # Navigation and capture
    # ---------------------------------------------------------------------------------

    async def goto(self, url: str, *, wait: WaitState = "domcontentloaded") -> Response | None:
        """Navigate to *url* (``docs/CONTRACTS.md`` §12).

        ``domcontentloaded`` is the default rather than ``load`` because ATS application
        pages are single-page applications whose ``load`` event waits on analytics beacons
        and font CDNs that have nothing to do with the form being ready.

        Args:
            url: Absolute URL of the application form.
            wait: Which load state to wait for.

        Returns:
            The main-frame response, or ``None`` when the navigation resulted in no response
            (a same-document navigation, for instance).

        Raises:
            BrowserSessionError: When the navigation fails or times out. The caller decides
                whether that is a dead posting or a transient failure.
        """
        log = logger.bind(url=url, wait=wait)
        try:
            response = await self.page.goto(url, wait_until=wait)
        except Exception as exc:
            log.warning("browser.goto_failed", error=str(exc), error_type=type(exc).__name__)
            raise BrowserSessionError(f"could not open {url}: {exc}") from exc
        log.debug("browser.navigated", status=response.status if response else None)
        return response

    async def screenshot(self, name: str, *, full_page: bool = True) -> Path:
        """Capture the page to the artifacts directory (``docs/CONTRACTS.md`` §12).

        Filenames are ``NNN-slug.png`` with a per-session monotonic ordinal, so the captures
        read in the order they were taken — which is exactly what a human resolving a review
        item needs. The path is appended to :attr:`artifacts`.``screenshots``.

        A full-page capture of an unusually tall page can exceed Chromium's surface limit;
        that case falls back to a viewport capture rather than losing the evidence entirely.

        Args:
            name: A human-readable label, slugified into the filename.
            full_page: Whether to capture beyond the viewport.

        Returns:
            The path written.

        Raises:
            BrowserSessionError: When even the viewport capture fails. Use
                :meth:`try_screenshot` on paths where a missing screenshot must not abort
                the attempt.
        """
        self._screenshot_ordinal += 1
        stem = slugify_artifact_name(name)
        filename = f"{self._screenshot_ordinal:0{SCREENSHOT_ORDINAL_WIDTH}d}-{stem}"
        target = self._artifacts_dir / f"{filename}{SCREENSHOT_SUFFIX}"

        try:
            await self.page.screenshot(path=str(target), full_page=full_page)
        except Exception as exc:
            # Retried immediately below without ``full_page``, or re-raised as a typed error.
            if not full_page:
                raise BrowserSessionError(f"could not capture screenshot {name!r}: {exc}") from exc
            logger.warning("browser.full_page_screenshot_failed", name=name, error=str(exc))
            try:
                await self.page.screenshot(path=str(target), full_page=False)
            except Exception as retry_exc:
                raise BrowserSessionError(
                    f"could not capture screenshot {name!r}: {retry_exc}"
                ) from retry_exc

        self.artifacts.screenshots.append(target)
        logger.debug(
            "browser.screenshot",
            name=name,
            path=str(target),
            bytes=target.stat().st_size if target.is_file() else 0,
        )
        return target

    async def try_screenshot(self, name: str, *, full_page: bool = True) -> Path | None:
        """Capture the page, returning ``None`` instead of raising on failure.

        For the "always capture before and after submit" rule (§12 rule 3) and for exception
        handlers, where the screenshot is evidence about a failure that has already happened
        and must not become a second failure on top of it.

        Args:
            name: A human-readable label, slugified into the filename.
            full_page: Whether to capture beyond the viewport.

        Returns:
            The path written, or ``None`` when the capture failed.
        """
        try:
            return await self.screenshot(name, full_page=full_page)
        except BrowserSessionError as exc:
            logger.warning("browser.screenshot_skipped", name=name, error=str(exc))
            return None

    async def save_storage_state(self, path: Path) -> Path:
        """Write the context's cookies and local storage to *path*.

        Lets a session that a human signed in through be replayed later without asking for
        the credentials again — the file is the credential, so it belongs under the user's
        local data directory and nowhere else.

        Args:
            path: Destination file. Parent directories are created.

        Returns:
            The path written.

        Raises:
            BrowserSessionError: When the state could not be captured or written.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self.context.storage_state(path=str(target))
        except Exception as exc:
            raise BrowserSessionError(f"could not save storage state to {target}: {exc}") from exc
        logger.info("browser.storage_state_saved", path=str(target))
        return target

    async def html(self) -> str:
        """Return the page's full serialised HTML.

        Returns:
            The document markup, or ``""`` when the page could not be serialised (which
            happens mid-navigation). Never raises: this feeds verification and artifact
            recording, and an empty string is a usable "no evidence" answer there.
        """
        try:
            return await self.page.content()
        except Exception as exc:  # noqa: BLE001 - an unreadable page is an empty page here
            logger.debug("browser.html_unavailable", error=str(exc))
            return ""

    async def visible_text(self) -> str:
        """Return the page's rendered text.

        Used by blocker detection and available to verification, which matches a pack's
        text success markers against it.

        Returns:
            The body's ``innerText``, or ``""`` when it could not be read.
        """
        try:
            return await self.page.inner_text("body", timeout=PROBE_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 - probes never raise
            logger.debug("browser.visible_text_unavailable", error=str(exc))
            return ""

    @property
    def url(self) -> str:
        """The page's current URL, or ``""`` when the session is not open."""
        if self._page is None:
            return ""
        try:
            return self._page.url
        except Exception as exc:  # noqa: BLE001 - a closed page has no URL
            logger.debug("browser.url_unavailable", error=str(exc))
            return ""

    # ---------------------------------------------------------------------------------
    # Page conditions
    # ---------------------------------------------------------------------------------

    async def dismiss_cookie_banner(self, pack: SelectorPack) -> bool:
        """Click *pack*'s consent-banner dismiss control, if one is showing.

        Consent banners render above the form and swallow the first click, which then looks
        like a field that would not fill. Dismissing one is the only click this module makes
        outside :class:`~app.browser.autofill.AutoFiller` — and it is safe precisely because
        the selector comes from the pack rather than from a guess.

        Args:
            pack: The provider's selector pack.

        Returns:
            ``True`` when a banner was found and clicked, ``False`` when there was none,
            when the pack declares no banner selector, or when the click failed. Never
            raises: a banner that would not dismiss is a reason to carry on and find out
            whether the form is usable anyway.
        """
        selector = (pack.cookie_banner or "").strip()
        if not selector:
            return False

        try:
            control = self.page.locator(selector).first
            if not await control.is_visible():
                return False
            await control.click(timeout=BANNER_CLICK_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 - a stuck banner is not a failed application
            logger.debug("browser.cookie_banner_not_dismissed", pack=pack.name, error=str(exc))
            return False

        logger.info("browser.cookie_banner_dismissed", pack=pack.name)
        return True

    async def detect_blockers(self, pack: SelectorPack | None = None) -> set[str]:
        """Report anything on the page that requires a human (``docs/CONTRACTS.md`` §12).

        Returns a subset of :data:`BLOCKERS`. Any non-empty result is an escalation to
        manual review, never an attempt to work around the obstacle (golden rule #2).

        The four probes are independent and individually guarded, so a selector that a
        future Chromium rejects, or a page that navigates mid-probe, costs one signal rather
        than the whole detection — which would otherwise mean walking into a captcha
        believing the page was clear.

        Args:
            pack: The provider's selector pack, whose ``captcha_markers`` and ``form_root``
                sharpen two of the probes. Optional so the method matches the §12 signature
                and can be called on a page whose ATS is not yet known.

        Returns:
            The blockers found: any of ``{"captcha", "mfa", "login_wall", "cloudflare"}``.
        """
        blockers: set[str] = set()
        text = (await self.visible_text()).casefold()

        if await self._probe_captcha(pack):
            blockers.add(BLOCKER_CAPTCHA)
        if await self._probe_cloudflare(text):
            blockers.add(BLOCKER_CLOUDFLARE)
        if self._probe_mfa(text):
            blockers.add(BLOCKER_MFA)
        if await self._probe_login_wall(pack):
            blockers.add(BLOCKER_LOGIN_WALL)

        if blockers:
            logger.info(
                "browser.blockers_detected",
                blockers=sorted(blockers),
                url=self.url,
                pack=pack.name if pack else None,
            )
        return blockers

    async def _probe_captcha(self, pack: SelectorPack | None) -> bool:
        """Return whether a captcha or bot-challenge widget is on the page.

        Args:
            pack: The provider's pack, whose ``captcha_markers`` are preferred when present.

        Returns:
            ``True`` when any marker matches.
        """
        markers = pack.captcha_markers if pack and pack.captcha_markers else ()
        return await self._any_present(markers or _DEFAULT_CAPTCHA_MARKERS)

    async def _probe_cloudflare(self, text: str) -> bool:
        """Return whether a Cloudflare interstitial is showing.

        Args:
            text: Already-casefolded page text.

        Returns:
            ``True`` when a challenge element or its copy is present.
        """
        if any(marker in text for marker in _CLOUDFLARE_TEXT_MARKERS):
            return True
        return await self._any_present(_CLOUDFLARE_MARKERS)

    @staticmethod
    def _probe_mfa(text: str) -> bool:
        """Return whether the page is asking for a second authentication factor.

        Args:
            text: Already-casefolded page text.

        Returns:
            ``True`` when any MFA phrasing appears.
        """
        return any(marker in text for marker in _MFA_TEXT_MARKERS)

    async def _probe_login_wall(self, pack: SelectorPack | None) -> bool:
        """Return whether a sign-in form stands between this session and the application.

        A visible password field is the signal, qualified by one exception: an application
        form that offers to create an account inline also has one, and that is not a wall —
        the form is right there and fillable. So the probe only fires when no application
        form is present on the page.

        Args:
            pack: The provider's pack, whose ``form_root`` is the most reliable evidence
                that the application form itself is on screen.

        Returns:
            ``True`` when a visible password field exists and no application form does.
        """
        if not await self._any_visible(_PASSWORD_INPUT_SELECTOR):
            return False

        form_markers = list(_APPLY_FORM_MARKERS)
        if pack and pack.form_root:
            form_markers.insert(0, pack.form_root)
        return not await self._any_present(tuple(form_markers))

    async def _any_present(self, selectors: tuple[str, ...]) -> bool:
        """Return whether any of *selectors* matches at least one element.

        Args:
            selectors: CSS selectors to try, each guarded on its own.

        Returns:
            ``True`` on the first match. A selector Chromium rejects is logged and skipped.
        """
        for selector in selectors:
            if not selector:
                continue
            try:
                if await self.page.locator(selector).count() > 0:
                    logger.debug("browser.probe_matched", selector=selector)
                    return True
            except Exception as exc:  # noqa: BLE001 - one bad selector must not end detection
                logger.debug("browser.probe_failed", selector=selector, error=str(exc))
        return False

    async def _any_visible(self, selector: str) -> bool:
        """Return whether *selector* matches at least one element the user can see.

        Args:
            selector: A CSS selector.

        Returns:
            ``True`` when one of the first :data:`MAX_PROBE_ELEMENTS` matches is visible.
        """
        try:
            locator = self.page.locator(selector)
            total = min(await locator.count(), MAX_PROBE_ELEMENTS)
            for index in range(total):
                if await locator.nth(index).is_visible():
                    return True
        except Exception as exc:  # noqa: BLE001 - probes never raise
            logger.debug("browser.visibility_probe_failed", selector=selector, error=str(exc))
        return False

    def __repr__(self) -> str:
        """Return ``<BrowserSession open=… screenshots=… dir=…>`` for logs."""
        return (
            f"<BrowserSession open={self.is_open} "
            f"screenshots={len(self.artifacts.screenshots)} dir={self._artifacts_dir}>"
        )
