"""The single seam between an ATS provider and the browser automation layer.

Greenhouse, Lever and Ashby all submit real applications, and all three do it the same way:
open the application form in a real browser, fill it, upload the documents, verify, submit.
The differences between them are *selectors*, not logic — which is why none of them drives
Playwright itself. They call :func:`run_browser_apply` with the name of their selector pack
and inherit one implementation of the part that matters.

That part is the safety envelope (``docs/CONTRACTS.md`` §12, golden rules #2 and #3):

* **The kill switch is applied here, before the browser is even started.** A submission
  requires ``settings.auto_apply_enabled`` *and* ``not settings.dry_run`` *and*
  ``not ctx.dry_run``. If any of the three says no, the context handed to the browser layer
  is rewritten with ``dry_run=True``, so the form is filled and inspected but the submit
  control is never clicked. Both switches default to the safe position, and this function
  cannot be persuaded to widen them — it only ever narrows.
* **The escalation posture is applied here.** An outcome that is not a confirmed submission
  becomes ``NEEDS_REVIEW`` rather than a failure or, worse, an optimistic success.
* **Unavailability is honest.** ``app.browser`` is built in a later phase and may not be
  installed at all in a slim deployment. Its absence raises :class:`UnsupportedFlowError`
  with a message naming the URL a human can apply at, which the pipeline turns into a review
  item — exactly what Workday and LinkedIn already do.

The import of ``app.browser`` is lazy and by name. That keeps ``app/jobs`` importable — and
therefore every provider registrable, and discovery fully functional — on a machine with no
Playwright, no browser binaries, and no intention of ever submitting anything.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import time
from collections.abc import Callable
from typing import Any, Final

import structlog

from app.jobs.base import ApplyContext, ApplyResult, ProviderError, UnsupportedFlowError
from app.models.enums import ApplicationStatus, ReviewReason

__all__ = ["BROWSER_ENTRY_POINTS", "BROWSER_PACKAGE", "browser_available", "run_browser_apply"]

logger = structlog.get_logger(__name__)

#: The package that owns browser automation. Resolved by name at call time so that importing
#: ``app.jobs`` never imports Playwright.
BROWSER_PACKAGE: Final[str] = "app.browser"

#: Callables looked for in :data:`BROWSER_PACKAGE`, in order of preference. The browser
#: package exposes one of these as its apply entry point; naming several keeps this seam from
#: dictating a single spelling to a package built in a different phase.
BROWSER_ENTRY_POINTS: Final[tuple[str, ...]] = (
    "run_apply",
    "run_browser_apply",
    "apply_with_browser",
    "apply",
)

#: Parameter names through which the selector pack is offered, whichever the entry point
#: happens to declare. An entry point that declares none simply does not receive it.
_SELECTOR_PACK_PARAMS: Final[tuple[str, ...]] = (
    "selector_pack",
    "selector_pack_name",
    "pack",
    "selectors",
)


def _load_entry_point() -> tuple[Callable[..., Any], str] | None:
    """Resolve the browser package's apply entry point.

    Returns:
        The callable and the attribute name it was found under, or ``None`` when the package
        is absent or exposes none of :data:`BROWSER_ENTRY_POINTS`.
    """
    try:
        module = importlib.import_module(BROWSER_PACKAGE)
    except ImportError as exc:
        logger.debug("apply.browser_package_absent", package=BROWSER_PACKAGE, error=str(exc))
        return None

    for attribute in BROWSER_ENTRY_POINTS:
        candidate = getattr(module, attribute, None)
        if callable(candidate):
            return candidate, attribute

    logger.warning(
        "apply.browser_entry_point_missing",
        package=BROWSER_PACKAGE,
        looked_for=list(BROWSER_ENTRY_POINTS),
    )
    return None


def browser_available() -> bool:
    """Return whether automated submission is possible in this installation.

    Cheap enough for a healthcheck: it imports :data:`BROWSER_PACKAGE` (which is itself
    expected to import Playwright lazily) and looks for an entry point. Providers that
    advertise ``supports_auto_apply`` can use it to report honestly from
    :meth:`~app.jobs.base.ATSProvider.healthcheck` rather than discovering the gap at
    submission time.

    Returns:
        ``True`` when the browser layer is installed and exposes an apply entry point.
    """
    return _load_entry_point() is not None


def _guarded_context(ctx: ApplyContext) -> tuple[ApplyContext, bool]:
    """Apply the kill switch to *ctx*, returning the context the browser layer may use.

    Golden rule #3: a real submission requires ``auto_apply_enabled=True`` **and**
    ``dry_run=False``, and both default to the safe position. This narrows and never widens —
    a caller that already asked for a dry run always gets one, whatever settings say.

    Args:
        ctx: The context the provider was given.

    Returns:
        ``(context, submission_allowed)``. When submission is not allowed the returned
        context is a copy with ``dry_run=True``, so the browser layer's own guard is a second
        line of defence rather than the only one.
    """
    from app.config.settings import get_settings

    settings = get_settings()
    allowed = bool(settings.is_submission_allowed) and not ctx.dry_run
    if allowed:
        return ctx, True

    logger.info(
        "apply.dry_run_enforced",
        auto_apply_enabled=bool(settings.auto_apply_enabled),
        settings_dry_run=bool(settings.dry_run),
        context_dry_run=bool(ctx.dry_run),
        **ctx.log_context(),
    )
    if ctx.dry_run:
        return ctx, False
    return dataclasses.replace(ctx, dry_run=True), False


def _call_kwargs(entry: Callable[..., Any], selector_pack_name: str) -> dict[str, Any]:
    """Build the keyword arguments *entry* is willing to accept.

    Introspection rather than a ``TypeError`` retry: the browser package is written in a
    later phase, and guessing its parameter names by trial would swallow a genuine signature
    error inside the entry point itself.

    Args:
        entry: The resolved browser entry point.
        selector_pack_name: The provider's selector pack name.

    Returns:
        Keyword arguments to pass. Empty when the entry point declares no matching
        parameter and no ``**kwargs``.
    """
    try:
        signature = inspect.signature(entry)
    except (TypeError, ValueError):  # pragma: no cover - builtins have no signature
        return {}

    parameters = signature.parameters
    accepts_var_keyword = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    for name in _SELECTOR_PACK_PARAMS:
        if name in parameters:
            return {name: selector_pack_name}
    if accepts_var_keyword:
        return {_SELECTOR_PACK_PARAMS[0]: selector_pack_name}
    return {}


def _coerce_result(value: Any, *, duration_seconds: float) -> ApplyResult:
    """Normalise whatever the browser layer returned into an :class:`ApplyResult`.

    Args:
        value: The entry point's return value.
        duration_seconds: Measured wall-clock duration, filled in when the browser layer did
            not time itself.

    Returns:
        The result. Anything unrecognisable becomes a ``needs_review`` outcome rather than a
        success — an ambiguous answer about whether an application was submitted must always
        resolve towards asking a human (golden rule #2).
    """
    if isinstance(value, ApplyResult):
        if not value.duration_seconds:
            value.duration_seconds = duration_seconds
        return value

    if isinstance(value, dict):
        try:
            result = ApplyResult(**value)
        except TypeError as exc:
            logger.warning("apply.result_shape_unexpected", error=str(exc))
        else:
            if not result.duration_seconds:
                result.duration_seconds = duration_seconds
            return result

    logger.warning("apply.result_unrecognised", returned=type(value).__name__)
    return ApplyResult.needs_review(
        ReviewReason.VERIFICATION_FAILED,
        error=(
            f"{BROWSER_PACKAGE} returned {type(value).__name__} instead of an ApplyResult; "
            "the submission outcome could not be established"
        ),
        duration_seconds=duration_seconds,
    )


async def run_browser_apply(ctx: ApplyContext, selector_pack_name: str) -> ApplyResult:
    """Drive one application through the browser layer, under the safety envelope.

    This is what a submission-capable provider's
    :meth:`~app.jobs.base.ATSProvider.apply` should return::

        async def apply(self, ctx: ApplyContext) -> ApplyResult:
            return await run_browser_apply(ctx, "greenhouse")

    The kill switch is evaluated first, so a run with ``auto_apply_enabled=False`` or
    ``dry_run=True`` fills and inspects the form and stops short of the submit control,
    returning ``ok=False``. Nothing about that path is a failure; it is the default posture
    of the product.

    Args:
        ctx: Everything the attempt needs — the posting, the profile, the rendered documents,
            the planned answers.
        selector_pack_name: The provider's selector pack, resolved inside the browser layer.
            Only a control located through that pack (or by an exact accessible-name match)
            may ever be clicked.

    Returns:
        The outcome. ``ok=True`` only when an application was genuinely submitted and
        verified.

    Raises:
        UnsupportedFlowError: When the browser layer is not installed, or exposes no apply
            entry point. The pipeline converts this into a ``needs_review`` application
            carrying :attr:`~app.models.enums.ReviewReason.UNSUPPORTED_FLOW`, with the URL a
            human can apply at.
        ProviderError: Propagated unchanged from the browser layer, so the pipeline's typed
            handling — rate limits, authentication, a vanished posting — still applies.
    """
    log = logger.bind(selector_pack=selector_pack_name, **ctx.log_context())

    resolved = _load_entry_point()
    if resolved is None:
        raise UnsupportedFlowError(
            f"browser automation is unavailable ({BROWSER_PACKAGE} is not installed or exposes "
            f"no apply entry point); apply manually at {ctx.posting.target_url}",
            provider=ctx.posting.provider,
            url=ctx.posting.target_url,
        )
    entry, entry_name = resolved

    guarded, submission_allowed = _guarded_context(ctx)
    log = log.bind(entry_point=entry_name, submission_allowed=submission_allowed)
    log.info("apply.browser_started")

    started = time.monotonic()
    try:
        returned = await entry(guarded, **_call_kwargs(entry, selector_pack_name))
    except UnsupportedFlowError:
        raise
    except ProviderError:
        # Typed provider failures carry their own review reason and retry posture; the
        # pipeline is the right place to interpret them.
        raise
    except Exception as exc:
        duration = time.monotonic() - started
        log.warning(
            "apply.browser_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            duration_seconds=round(duration, 3),
        )
        return ApplyResult.failed(
            f"browser automation failed: {type(exc).__name__}: {exc}",
            duration_seconds=duration,
        )

    duration = time.monotonic() - started
    result = _coerce_result(returned, duration_seconds=duration)

    if not submission_allowed and result.ok:
        # Defensive: the browser layer reported a submission that the kill switch forbade.
        # Trust the switch, not the report, and make the contradiction loud.
        log.error("apply.submission_reported_while_blocked", status=str(result.status))
        result = ApplyResult.needs_review(
            ReviewReason.POLICY_BLOCK,
            error=(
                "the browser layer reported a submission while automated submission was "
                "disabled; the outcome must be confirmed by a human"
            ),
            screenshot_paths=result.screenshot_paths,
            duration_seconds=result.duration_seconds,
            browser_log=result.browser_log,
        )

    if result.status is ApplicationStatus.NEEDS_REVIEW and result.review_reason is None:
        # A review item with no stated reason is unusable to the human who picks it up.
        result.review_reason = ReviewReason.AMBIGUOUS_ANSWER

    log.info(
        "apply.browser_finished",
        ok=result.ok,
        status=str(result.status),
        review_reason=str(result.review_reason) if result.review_reason else None,
        unanswered_fields=len(result.unanswered_fields),
        duration_seconds=round(result.duration_seconds, 3),
    )
    return result
