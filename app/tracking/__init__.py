"""Application status sync — the loop that closes itself (``docs/CONTRACTS.md`` §17).

ApplicantOS claims to learn what gets interviews. That is only true if outcomes actually
reach the database, and until now they only did when the user typed them in. This package
reads them from the user's mailbox instead: a Microsoft rejection updates itself whether or
not anybody opens the app.

The pipeline, and why it is four pieces rather than one
-------------------------------------------------------

.. code-block:: text

    StatusTracker.poll   ->  StatusSignal      what arrived
    StatusClassifier     ->  Classification    what it means
    SignalMatcher        ->  Match             which application it is about
    StatusSyncService    ->  Application       the only thing that writes

Each step can be wrong in a different way, so each step carries its own confidence and its
own evidence, and the service refuses to act on any of them below
``settings.status_sync_min_confidence`` — it stores the signal for one-click confirmation
instead. Marking the wrong job rejected is worse than asking (golden rule #2).

Email is the channel, and only email
------------------------------------

Every ATS — and LinkedIn itself — notifies by email, so one mail pipeline covers LinkedIn,
Glassdoor, Indeed and every ATS at once, including sources that were never integrated.
Glassdoor has no application-status surface and LinkedIn's terms prohibit automated
scraping, so **neither is scraped** (golden rule #10).

Reading a person's mailbox is the most invasive thing this product does, so §17.8 fixes six
invariants and :mod:`app.tracking.email.base` documents which line of code enforces each:
read-only always, narrow queries never a sweep, minimal retention, credentials in the OS
keychain and never the database, never log a body or an address, and nothing read until the
user explicitly connects a mailbox.

Importing this package
----------------------

Importing ``app.tracking`` registers the tracker plugins, exactly as importing ``app.jobs``
registers the ATS providers — that is why :data:`app.plugins.loader.BUILTIN_PLUGIN_MODULES`
names it, and why nothing else imports a concrete tracker module (golden rule #5)::

    from app.models.enums import PluginKind
    from app.plugins.registry import registry

    tracker = registry.get(PluginKind.TRACKER, "email")

The re-exports below resolve on first attribute access rather than at import time (PEP 562).
The mailbox adapters underneath depend on nothing but the standard library — no ORM, no
enums, no settings — and keeping the package door lazy preserves that: a mailbox adapter can
be imported, audited and unit-tested without loading the database layer, and a tracker
module that fails to import leaves the rest of the package working instead of taking the
whole plugin loader down with it.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, Final

import structlog

if TYPE_CHECKING:  # pragma: no cover - re-exported lazily at runtime, eagerly for type-checkers
    from app.tracking.base import (
        ATS_RELAY_DOMAINS,
        SNIPPET_MAX_CHARS,
        Classification,
        Match,
        StatusSignal,
        StatusTracker,
        SyncReport,
        TrackerAuthError,
        TrackerConfigurationError,
        TrackerError,
        TrackerUnavailableError,
        is_relay_domain,
    )
    from app.tracking.email import (
        GmailMailbox,
        ImapMailbox,
        MailBox,
        MailboxError,
        OutlookMailbox,
        RawMessage,
        build_query,
        extract_text,
    )

logger = structlog.get_logger(__name__)

#: Modules imported for their registration side effect when this package is imported. Each
#: is guarded: a tracker whose optional dependency is missing is logged and skipped, so the
#: application still starts with one fewer source rather than not at all — the same posture
#: ``app/jobs/__init__.py`` takes for ATS providers.
TRACKER_MODULES: Final[tuple[str, ...]] = (
    "app.tracking.trackers.email_tracker",
    "app.tracking.trackers.portal_tracker",
)

#: Public name → module that defines it. Consulted by :func:`__getattr__`, which is what
#: makes ``from app.tracking import StatusSignal`` work without importing every submodule.
_EXPORTS: Final[dict[str, str]] = {
    # Contracts (§17.3)
    "StatusSignal": "app.tracking.base",
    "Classification": "app.tracking.base",
    "Match": "app.tracking.base",
    "SyncReport": "app.tracking.base",
    "StatusTracker": "app.tracking.base",
    "ATS_RELAY_DOMAINS": "app.tracking.base",
    "SNIPPET_MAX_CHARS": "app.tracking.base",
    "is_relay_domain": "app.tracking.base",
    "TrackerError": "app.tracking.base",
    "TrackerAuthError": "app.tracking.base",
    "TrackerConfigurationError": "app.tracking.base",
    "TrackerUnavailableError": "app.tracking.base",
    # Mailboxes (§17.3, §17.8)
    "MailBox": "app.tracking.email",
    "RawMessage": "app.tracking.email",
    "MailboxError": "app.tracking.email",
    "GmailMailbox": "app.tracking.email",
    "ImapMailbox": "app.tracking.email",
    "OutlookMailbox": "app.tracking.email",
    "build_query": "app.tracking.email",
    "extract_text": "app.tracking.email",
}

__all__ = [
    # Contracts
    "StatusSignal",
    "Classification",
    "Match",
    "SyncReport",
    "StatusTracker",
    "ATS_RELAY_DOMAINS",
    "SNIPPET_MAX_CHARS",
    "is_relay_domain",
    # Errors
    "TrackerError",
    "TrackerAuthError",
    "TrackerConfigurationError",
    "TrackerUnavailableError",
    # Mailboxes
    "MailBox",
    "RawMessage",
    "MailboxError",
    "GmailMailbox",
    "ImapMailbox",
    "OutlookMailbox",
    "build_query",
    "extract_text",
    # Registration
    "TRACKER_MODULES",
    "load_trackers",
]


def load_trackers() -> None:
    """Import every built-in tracker module so its ``@plugin`` decorator runs.

    Called once when this package is imported. Idempotent, because a module already in
    ``sys.modules`` is not re-executed.

    Never raises. A tracker module that is absent (the tree is still being built out) or
    whose optional dependency is not installed is logged and skipped: the plugin loader must
    be able to start the application with a partial set of sources, exactly as it does for
    ATS providers.
    """
    for module_name in TRACKER_MODULES:
        try:
            importlib.import_module(module_name)
        except ImportError as exc:
            logger.debug("tracking.tracker_absent", module=module_name, error=str(exc))
        except Exception as exc:
            logger.warning("tracking.tracker_import_failed", module=module_name, error=str(exc))


def __getattr__(name: str) -> Any:
    """Resolve a public name to its defining module on first access (PEP 562).

    Args:
        name: The attribute being looked up on ``app.tracking``.

    Returns:
        The re-exported object.

    Raises:
        AttributeError: If *name* is not part of this package's public surface, or its
            module is not present in this tree yet — the message says which module was
            expected to define it, which is what a half-built checkout needs to hear.
    """
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise AttributeError(
            f"{name!r} is defined in {module_name!r}, which could not be imported: {exc}"
        ) from exc
    value = getattr(module, name)
    globals()[name] = value  # cache, so the next access is a plain global lookup
    return value


def __dir__() -> list[str]:
    """Return the package's public names, so ``dir()`` and completion see the lazy ones."""
    return sorted(__all__)


load_trackers()
