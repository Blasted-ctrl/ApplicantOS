"""Test doubles that **record** rather than merely answer.

The distinction matters more here than in most projects. ``docs/SAFETY.md`` promises that
``AutoFiller.submit`` returns ``False`` *without touching the DOM* when either switch is
closed — and a function can return ``False`` and still have clicked something on the way.
Asserting on a return value would pass against a version that clicks first and reports
failure afterwards, which is exactly the bug the promise exists to exclude.

So :class:`FakePage` keeps an ordered :attr:`~FakePage.actions` log and separate views over
it — :attr:`~FakePage.clicks`, :attr:`~FakePage.fills`, :attr:`~FakePage.lookups`,
:attr:`~FakePage.uploads` — and the kill-switch test asserts ``fake_page.clicks == []`` and
``fake_page.lookups == []``. The second is the stronger claim: no locator was even built.

Nothing in this module imports Playwright, httpx-at-import, or any provider SDK. The whole
suite runs on a machine with no browser and no network.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "FakeLocator",
    "FakePage",
    "FakeSession",
    "FakeStorage",
    "MockRouter",
    "PageTransition",
    "RecordingLLM",
    "build_mock_transport",
    "discovery_payload",
    "form_control",
]


# ======================================================================================
# Browser doubles
# ======================================================================================


@dataclass
class Action:
    """One thing the page was asked to do."""

    kind: str
    target: str
    value: Any = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Action({self.kind!r}, {self.target!r}, {self.value!r})"


@dataclass
class PageTransition:
    """What the page becomes after the **first** click — i.e. after a real submit.

    Without this a fake page is frozen, and a frozen page cannot tell the two halves of the
    apply path apart: ``AutoFiller.submit`` reads the page to decide whether a marker
    appeared, and ``ApplicationVerifier.verify`` reads it again to decide whether the
    employer really has the application. Both would see the *pre-submit* page, so a
    confirmation could never be distinguished from silence.

    Every field is optional and replaces the corresponding page state wholesale; a field left
    ``None`` is untouched. Applied once, so a second click (the kill-switch test flips a
    switch and clicks again) does not compound it.

    Attributes:
        present: The selectors that exist afterwards — normally the confirmation element,
            and normally *not* the submit button, which the confirmation page has replaced.
        text: The visible copy afterwards, matched against the pack's success and error
            markers and scanned for a confirmation id.
        url: The URL afterwards, which is the only machine-readable evidence some ATSs give.
        html: The markup afterwards.
    """

    present: set[str] | None = None
    text: str | None = None
    url: str | None = None
    html: str | None = None


class FakeLocator:
    """A duck-typed Playwright ``Locator`` that reports its actions to the page.

    Built by :meth:`FakePage.locator` with the *alternative it matched* rather than the whole
    query, so an action taken through a selector list — which is what every
    :class:`~app.browser.selectors.SelectorPack` submit selector is — records the element the
    browser would really have acted on.
    """

    def __init__(self, page: FakePage, selector: str, *, matches: int) -> None:
        self._page = page
        self._selector = selector
        self._matches = matches

    @property
    def first(self) -> FakeLocator:
        """Playwright's ``.first``; the same double, narrowed to one element."""
        return self

    async def count(self) -> int:
        """How many elements this locator matches."""
        return self._matches

    async def is_visible(self, **_kwargs: Any) -> bool:
        """Whether this locator matches something on the page."""
        return self._matches > 0

    async def click(self, **_kwargs: Any) -> None:
        """Record a click, then move the page on.

        **This is what the kill-switch test asserts never happens.** The transition runs
        after the action is recorded, so a page that becomes a confirmation still shows a
        click in the log.
        """
        if self._matches < 1:
            raise RuntimeError(f"locator {self._selector!r} matches nothing; cannot click")
        self._page.actions.append(Action("click", self._selector))
        self._page.apply_transition()

    async def fill(self, value: str, **_kwargs: Any) -> None:
        """Record a value typed into a control."""
        if self._matches < 1:
            raise RuntimeError(f"locator {self._selector!r} matches nothing; cannot fill")
        self._page.actions.append(Action("fill", self._selector, value))

    async def check(self, **_kwargs: Any) -> None:
        """Record a box ticked."""
        self._page.actions.append(Action("check", self._selector, True))

    async def uncheck(self, **_kwargs: Any) -> None:
        """Record a box cleared."""
        self._page.actions.append(Action("check", self._selector, False))

    async def select_option(self, **kwargs: Any) -> None:
        """Record an option chosen from a ``<select>``."""
        self._page.actions.append(
            Action("select", self._selector, kwargs.get("label") or kwargs.get("value"))
        )

    async def set_input_files(self, files: Any, **_kwargs: Any) -> None:
        """Record a document offered to a file input."""
        if self._selector in self._page.reject_uploads:
            raise RuntimeError(f"upload rejected by {self._selector}")
        self._page.actions.append(Action("upload", self._selector, str(files)))
        self._page.uploaded_files.setdefault(self._selector, []).append(Path(str(files)).name)


class FakePage:
    """A duck-typed Playwright ``Page``: answers queries, records interactions.

    Attributes:
        actions: Every interaction in order — the evidence the safety tests assert on.
        present: Selectors that "exist" on this page. Anything else matches zero elements,
            which is how a test says "there is no submit button here".
        roles: ``(role, accessible name)`` pairs the page offers, for ``get_by_role``.
        discovery: The payload :data:`~app.browser.autofill.DISCOVERY_SCRIPT` should return.
        text: The page's visible text, used for success/error marker matching.
        url: The current URL.
        after_submit: What the page becomes once something is clicked, or ``None`` for a page
            that never changes.
    """

    def __init__(
        self,
        *,
        present: set[str] | None = None,
        roles: set[tuple[str, str]] | None = None,
        discovery: dict[str, Any] | None = None,
        text: str = "",
        html: str = "<html><body></body></html>",
        url: str = "https://boards.greenhouse.io/acme/jobs/1",
        after_submit: PageTransition | None = None,
    ) -> None:
        self.actions: list[Action] = []
        self.present: set[str] = set(present or ())
        self.roles: set[tuple[str, str]] = set(roles or ())
        self.discovery: dict[str, Any] = discovery or {"root": "form", "matched": 1, "controls": []}
        self.text = text
        self._html = html
        self.url = url
        self.uploaded_files: dict[str, list[str]] = {}
        self.reject_uploads: set[str] = set()
        self.after_submit = after_submit
        self.transitioned = False

    # -- state ------------------------------------------------------------------------

    @property
    def html(self) -> str:
        """The serialised DOM, as ``BrowserSession.html`` exposes it."""
        return self._html

    def apply_transition(self) -> bool:
        """Move the page to its post-submit state, at most once.

        Returns:
            Whether anything changed. Called from :meth:`FakeLocator.click`; a page with no
            :attr:`after_submit` is left exactly as it was, which is what keeps every
            existing kill-switch assertion true.
        """
        transition = self.after_submit
        if transition is None or self.transitioned:
            return False
        self.transitioned = True
        if transition.present is not None:
            self.present = set(transition.present)
        if transition.text is not None:
            self.text = transition.text
        if transition.url is not None:
            self.url = transition.url
        if transition.html is not None:
            self._html = transition.html
        return True

    # -- recorded views ---------------------------------------------------------------

    @property
    def clicks(self) -> list[str]:
        """Selectors that were clicked, in order. **Must be empty when a switch is closed.**"""
        return [a.target for a in self.actions if a.kind == "click"]

    @property
    def fills(self) -> list[tuple[str, Any]]:
        """``(selector, value)`` pairs written into the form."""
        return [(a.target, a.value) for a in self.actions if a.kind == "fill"]

    @property
    def checks(self) -> list[tuple[str, Any]]:
        """``(selector, state)`` pairs for boxes ticked or cleared."""
        return [(a.target, a.value) for a in self.actions if a.kind == "check"]

    @property
    def selections(self) -> list[tuple[str, Any]]:
        """``(selector, option)`` pairs chosen from dropdowns."""
        return [(a.target, a.value) for a in self.actions if a.kind == "select"]

    @property
    def uploads(self) -> list[tuple[str, Any]]:
        """``(selector, path)`` pairs offered to file inputs."""
        return [(a.target, a.value) for a in self.actions if a.kind == "upload"]

    @property
    def lookups(self) -> list[str]:
        """Every ``locator`` / ``get_by_role`` call.

        Emptier than :attr:`clicks` and therefore a stronger assertion: it proves the DOM was
        never even queried for a submit control, which is what ``docs/SAFETY.md`` claims.
        """
        return [a.target for a in self.actions if a.kind == "lookup"]

    @property
    def writes(self) -> list[Action]:
        """Every action that changed page state — everything except lookups and evaluates."""
        return [a for a in self.actions if a.kind not in ("lookup", "evaluate")]

    # -- Playwright surface -----------------------------------------------------------

    def locator(self, selector: str) -> FakeLocator:
        """Build a locator, recording the lookup.

        A comma-separated selector list matches when **any** alternative is present, as CSS
        does — and the resulting locator carries that alternative, so the recorded click names
        the control rather than the query. Every pack's submit selector is such a list, so
        without this the pack path (§12 invariant 4) could never be exercised.
        """
        self.actions.append(Action("lookup", selector))
        alternatives = [part.strip() for part in selector.split(",") if part.strip()]
        matched = next((part for part in alternatives if part in self.present), None)
        return FakeLocator(self, matched or selector, matches=1 if matched else 0)

    def get_by_role(self, role: str, *, name: str = "", exact: bool = False) -> FakeLocator:
        """Build an accessible-name locator, recording the lookup."""
        target = f"role={role}[name={name!r},exact={exact}]"
        self.actions.append(Action("lookup", target))
        if exact:
            matched = (role, name) in self.roles
        else:
            matched = any(r == role and name in n for r, n in self.roles)
        return FakeLocator(self, target, matches=1 if matched else 0)

    async def evaluate(self, script: str, argument: Any = None) -> Any:
        """Run one of the two known scripts and answer from canned state."""
        from app.browser.autofill import DISCOVERY_MARKER, UPLOAD_MARKER

        self.actions.append(Action("evaluate", "script"))
        if DISCOVERY_MARKER in script:
            return self.discovery
        if UPLOAD_MARKER in script:
            selector = (argument or {}).get("selector", "")
            files = self.uploaded_files.get(selector, [])
            return {"files": files, "value": files[0] if files else ""}
        return None

    async def content(self) -> str:
        """The serialised DOM."""
        return self._html

    async def inner_text(self, _selector: str) -> str:
        """The page's visible text."""
        return self.text


class FakeSession:
    """A duck-typed ``BrowserSession``: a page, screenshots, and blocker detection.

    Carries the whole surface ``docs/CONTRACTS.md`` §12 gives a session — ``page``,
    ``goto``, ``screenshot``, ``html``, ``detect_blockers``, ``dismiss_cookie_banner`` — so a
    complete apply attempt can be driven through
    :func:`app.browser.apply.run_apply` with no Playwright installed.

    Attributes:
        screenshots: Artifact names captured, in order. ``"verification"`` appearing here is
            proof that :class:`~app.browser.verification.ApplicationVerifier` ran; its
            *absence* after a dry run is proof that it did not.
    """

    def __init__(
        self,
        *,
        page: FakePage | None = None,
        blockers: set[str] | None = None,
        blocker_error: Exception | None = None,
        artifacts_dir: Path | None = None,
    ) -> None:
        self.page = page if page is not None else FakePage()
        self._blockers = set(blockers or ())
        self._blocker_error = blocker_error
        self.screenshots: list[str] = []
        self.artifacts_dir = artifacts_dir

    async def screenshot(self, name: str, full_page: bool = True) -> Path:
        """Record a proof capture and return a plausible path."""
        self.screenshots.append(name)
        base = self.artifacts_dir or Path.cwd()
        return base / f"{name}.png"

    async def detect_blockers(self, _pack: Any = None) -> set[str]:
        """Report captcha / MFA / login walls, or raise when the test wants a failed probe.

        The pack argument is accepted and ignored, exactly as
        ``BrowserSession.detect_blockers`` accepts an optional one: the autofiller calls this
        with no arguments and the apply driver calls it with the provider's pack.
        """
        if self._blocker_error is not None:
            raise self._blocker_error
        return set(self._blockers)

    async def dismiss_cookie_banner(self, pack: Any) -> bool:
        """Click the pack's consent-banner control, if this page is showing one.

        Mirrors the real implementation — locate, check visibility, click — so a test that
        puts a banner on the page sees the click recorded like any other.
        """
        selector = str(getattr(pack, "cookie_banner", "") or "").strip()
        if not selector:
            return False
        control = self.page.locator(selector).first
        if not await control.is_visible():
            return False
        await control.click()
        return True

    @property
    def html(self) -> str:
        """The page markup, as ``BrowserSession`` exposes it."""
        return self.page.html

    async def goto(self, url: str, **_kwargs: Any) -> None:
        """Navigate, recording the destination."""
        self.page.url = url


# ======================================================================================
# Discovery payloads
# ======================================================================================


def form_control(
    selector: str,
    *,
    label: str = "",
    tag: str = "input",
    control_type: str = "text",
    name: str = "",
    required: bool = False,
    options: list[dict[str, str]] | None = None,
    max_length: int | None = None,
    placeholder: str = "",
    heading: str = "",
    hidden: bool = False,
    disabled: bool = False,
) -> dict[str, Any]:
    """Build one raw control descriptor, exactly as ``DISCOVERY_SCRIPT`` emits them.

    The script does DOM access only and every policy decision — which label wins, which
    :class:`~app.models.enums.FieldKind` applies, what counts as required — is made in Python
    by :meth:`~app.browser.autofill.AutoFiller.discover_fields`. That is what makes a canned
    descriptor a faithful stand-in for a real page.

    Args:
        selector: The control's unique CSS selector.
        label: Its ``<label for>`` text.
        tag: ``input``, ``textarea`` or ``select``.
        control_type: The ``type`` attribute, or the tag name for non-inputs.
        name: The ``name`` attribute, which is what groups radios and checkboxes.
        required: Whether the control carries ``required``.
        options: ``{"value": …, "label": …}`` entries, for a ``<select>``.
        max_length: The advertised character limit.
        placeholder: Placeholder text, which doubles as the field's hint.
        heading: The nearest preceding heading, which is a radio group's question.
        hidden: Whether the control is hidden, and therefore skipped.
        disabled: Whether the control is disabled, and therefore skipped.

    Returns:
        The descriptor.
    """
    return {
        "selector": selector,
        "tag": tag,
        "type": control_type,
        "name": name,
        "id": selector.lstrip("#"),
        "value": "",
        "disabled": disabled,
        "readOnly": False,
        "hidden": hidden,
        "required": required,
        "ariaRequired": False,
        "maxLength": max_length,
        "multiple": False,
        "labelFor": label,
        "labelWrap": "",
        "ariaLabel": "",
        "ariaLabelledBy": "",
        "placeholder": placeholder,
        "title": "",
        "heading": heading,
        "options": options or [],
    }


def discovery_payload(*controls: dict[str, Any], root: str = "#application_form") -> dict[str, Any]:
    """Wrap control descriptors in the envelope ``DISCOVERY_SCRIPT`` returns.

    Args:
        controls: Descriptors from :func:`form_control`.
        root: The selector of the form the controls were found in.

    Returns:
        The payload :meth:`FakePage.evaluate` answers the discovery script with.
    """
    return {"root": root, "matched": 1, "controls": list(controls)}


# ======================================================================================
# LLM double
# ======================================================================================


class RecordingLLM:
    """A deterministic model client that records prompts and replays canned responses.

    Satisfies the parts of ``ModelPlugin`` the AI layer actually calls. ``complete_json``
    pops from :attr:`responses` when the test queued one and otherwise returns
    :attr:`default_json`, so a test that only cares about *validation* can queue exactly the
    malformed payload it wants to see rejected.
    """

    def __init__(
        self,
        *,
        responses: list[Any] | None = None,
        default_json: dict[str, Any] | None = None,
        text: str = "",
        error: Exception | None = None,
    ) -> None:
        self.responses: list[Any] = list(responses or [])
        self.default_json: dict[str, Any] = default_json or {"bullets": [], "summary": ""}
        self.text = text
        self.error = error
        self.prompts: list[dict[str, Any]] = []
        self.calls: int = 0

    @property
    def model(self) -> str:
        """Model identifier, for token accounting."""
        return "recording-test-model"

    def count_tokens(self, text: str) -> int:
        """A stable, dependency-free token estimate."""
        return max(1, len(text) // 4)

    async def complete(self, **kwargs: Any) -> Any:
        """Return a canned :class:`~app.ai.llm.LLMResponse`-shaped object."""
        from app.ai.llm import LLMResponse

        self.calls += 1
        self.prompts.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        payload = self.responses.pop(0) if self.responses else self.text
        body = payload if isinstance(payload, str) else json.dumps(payload)
        return LLMResponse(
            text=body,
            input_tokens=self.count_tokens(str(kwargs.get("prompt", ""))),
            output_tokens=self.count_tokens(body),
            model=self.model,
            cached=False,
            raw={},
        )

    async def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        """Return the next queued JSON payload, or :attr:`default_json`."""
        self.calls += 1
        self.prompts.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        if self.responses:
            payload = self.responses.pop(0)
            return payload if isinstance(payload, dict) else json.loads(payload)
        return dict(self.default_json)

    async def healthcheck(self) -> bool:
        """Always available — it is a local object."""
        return True


# ======================================================================================
# Storage double
# ======================================================================================


class FakeStorage:
    """An in-memory ``StorageBackend`` that also records deletions.

    ``test_golden_cleanup`` needs to prove a *render* was deleted while ``content_json``
    survived, so :attr:`deleted` is the assertion surface.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path("/fake-storage")
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.deleted: list[str] = []

    async def put(self, key: str, data: bytes, *, content_type: str | None = None, **_kw: Any):
        """Store bytes under *key*."""
        self.objects[key] = bytes(data)
        self.content_types[key] = content_type or "application/octet-stream"
        return self._stored(key)

    async def put_file(self, key: str, path: Path, *, content_type: str | None = None, **_kw: Any):
        """Store a file's bytes under *key*."""
        return await self.put(key, Path(path).read_bytes(), content_type=content_type)

    async def get(self, key: str) -> bytes:
        """Read the bytes stored under *key*."""
        from app.storage.base import ObjectNotFoundError

        if key not in self.objects:
            raise ObjectNotFoundError(f"no object at {key}")
        return self.objects[key]

    async def delete(self, key: str) -> bool:
        """Delete *key*, recording the fact."""
        self.deleted.append(key)
        return self.objects.pop(key, None) is not None

    async def exists(self, key: str) -> bool:
        """Whether *key* is stored."""
        return key in self.objects

    async def url(self, key: str, **_kw: Any) -> str:
        """A file URL for *key*."""
        return f"file://{self.root}/{key}"

    def _stored(self, key: str):
        from app.storage.base import StoredObject, sha256_bytes

        data = self.objects[key]
        return StoredObject(
            key=key,
            size_bytes=len(data),
            content_type=self.content_types[key],
            sha256=sha256_bytes(data),
            backend="fake",
        )


# ======================================================================================
# HTTP double
# ======================================================================================


@dataclass
class _Route:
    """One registered response."""

    pattern: re.Pattern[str]
    status: int
    payload: Any
    text: str | None = None


@dataclass
class MockRouter:
    """A tiny router over ``httpx.MockTransport``.

    Registered patterns are matched against the full request URL in registration order; an
    unmatched request raises, because a test that silently reaches the real internet is a
    test that will fail in CI for the wrong reason.
    """

    routes: list[_Route] = field(default_factory=list)
    requests: list[Any] = field(default_factory=list)

    def route(
        self,
        fragment: str,
        *,
        json: Any = None,
        text: str | None = None,
        status: int = 200,
    ) -> MockRouter:
        """Register a response for any URL containing *fragment* (a regex)."""
        self.routes.append(_Route(re.compile(fragment), status, json, text))
        return self

    def handler(self, request: Any) -> Any:
        """The ``MockTransport`` handler."""
        import httpx

        self.requests.append(request)
        url = str(request.url)
        for route in self.routes:
            if route.pattern.search(url):
                if route.text is not None:
                    return httpx.Response(route.status, text=route.text)
                return httpx.Response(route.status, json=route.payload)
        raise AssertionError(f"unmocked HTTP request to {url}")

    @property
    def transport(self) -> Any:
        """An ``httpx.MockTransport`` bound to :meth:`handler`."""
        import httpx

        return httpx.MockTransport(self.handler)


def build_mock_transport() -> MockRouter:
    """Return a fresh :class:`MockRouter`."""
    return MockRouter()
