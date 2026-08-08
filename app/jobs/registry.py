"""Provider lookup — the only sanctioned way to reach an ATS integration.

Golden rule #5 says nothing outside ``app/jobs/`` may import a concrete provider module.
This is the door everything else uses instead: the pipeline, the discovery worker, the API
routes and the desktop settings screen all name a provider by string and get an instance
back, so ``app/jobs/greenhouse.py`` can be renamed, replaced or shipped by a third-party
distribution without a single edit outside this package.

Everything here is a thin, provider-typed delegate over :data:`app.plugins.registry.registry`
filtered to :attr:`~app.models.enums.PluginKind.PROVIDER`. Two conveniences justify the
indirection beyond the type narrowing:

* :func:`load_all` is called on every entry point, so a caller can never get
  :class:`~app.plugins.base.PluginNotFound` merely because plugin discovery had not run yet.
  It is idempotent, so the cost after the first call is a lock and a boolean.
* URL routing lives here rather than in each call site. :func:`provider_for_url` asks each
  provider's :meth:`~app.jobs.base.ATSProvider.detect`, and
  :func:`provider_class_for_url` answers the same question from ``URL_PATTERNS`` alone, with
  no instantiation and no HTTP client — which is what the desktop app's "paste a job link"
  box wants.

Ordering is deterministic everywhere: the plugin registry returns providers sorted by name,
so "the first provider that claims this URL" is the same provider on every run and on every
machine. Two providers claiming one URL is a configuration bug, and it is logged as one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from app.models.enums import ATSProviderName, PluginKind
from app.plugins.base import PluginDisabled, PluginNotFound
from app.plugins.loader import load_all
from app.plugins.registry import registry

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.jobs.base import ATSProvider

__all__ = [
    "all_providers",
    "auto_apply_provider_names",
    "available_provider_names",
    "describe_providers",
    "get_provider",
    "get_provider_class",
    "provider_class_for_url",
    "provider_for_url",
    "resolve_providers",
]

logger = structlog.get_logger(__name__)


def _normalize(name: ATSProviderName | str) -> str:
    """Reduce a provider identifier to the string the plugin registry indexes on.

    Args:
        name: An :class:`~app.models.enums.ATSProviderName` or its string value.

    Returns:
        The plain provider name.
    """
    return name.value if isinstance(name, ATSProviderName) else str(name)


def get_provider(name: ATSProviderName | str) -> ATSProvider:
    """Return the shared instance of one provider, constructing it on first use.

    Args:
        name: The provider's registered name, as an
            :class:`~app.models.enums.ATSProviderName` or a string. Matching is
            case-insensitive.

    Returns:
        The cached provider instance. One per name for the lifetime of the process, so its
        HTTP connection pool is reused across discovery runs.

    Raises:
        PluginNotFound: If no provider is registered under that name — typically a stale
            entry in ``UserPreferences.providers_enabled``. The message lists what *is*
            available.
        PluginDisabled: If the provider is registered but switched off in settings.
        PluginLoadError: If the provider's constructor raised.
    """
    load_all()
    return registry.get(PluginKind.PROVIDER, _normalize(name))  # type: ignore[return-value]


def get_provider_class(name: ATSProviderName | str) -> type[ATSProvider]:
    """Return a provider's class without constructing it.

    Reads class-level facts — ``supports_auto_apply``, ``requires_login``, ``URL_PATTERNS``,
    ``meta`` — at no construction cost, and works for a disabled provider too, which is what
    the settings screen needs in order to describe something the user has switched off.

    Args:
        name: The provider's registered name.

    Returns:
        The registered class.

    Raises:
        PluginNotFound: If no provider is registered under that name.
    """
    load_all()
    return registry.get_class(PluginKind.PROVIDER, _normalize(name))  # type: ignore[return-value]


def all_providers() -> list[ATSProvider]:
    """Return an instance of every enabled provider, ordered by name.

    Returns:
        The provider instances. Disabled providers are omitted; the order is deterministic,
        which is what makes a discovery run reproducible.

    Raises:
        PluginLoadError: If any enabled provider's constructor raised. Failures are not
            swallowed — silently omitting a broken provider would silently narrow a discovery
            run, and the user would never learn why a board stopped producing results.
    """
    load_all()
    return registry.all(PluginKind.PROVIDER)  # type: ignore[return-value]


def available_provider_names() -> list[str]:
    """Return every registered provider name, enabled or not, ordered by name.

    Backs ``GET /settings/plugins`` and the provider multi-select in onboarding, both of
    which must show a disabled provider so the user can turn it back on.

    Returns:
        The names exactly as each provider declared them.
    """
    load_all()
    return registry.names(PluginKind.PROVIDER)


def auto_apply_provider_names() -> list[str]:
    """Return the names of providers that can really submit an application.

    Reads ``supports_auto_apply`` off each registered class without constructing anything.
    Per ``docs/CONTRACTS.md`` §9 this is Greenhouse, Lever and Ashby; Workday and LinkedIn
    report ``False`` and route every attempt to manual review (golden rule #10).

    Returns:
        The names of submission-capable providers, ordered by name.
    """
    names: list[str] = []
    for name in available_provider_names():
        try:
            provider_class = get_provider_class(name)
        except PluginNotFound:  # pragma: no cover - the name came from the registry
            continue
        if getattr(provider_class, "supports_auto_apply", False):
            names.append(name)
    return names


def resolve_providers(names: list[str] | list[ATSProviderName] | None) -> list[ATSProvider]:
    """Resolve a caller's provider selection into instances, skipping what is unusable.

    The forgiving counterpart to :func:`get_provider`, for the one call site where partial
    success is right: a discovery run driven by ``UserPreferences.providers_enabled``. A
    preference list can name a provider that has been removed or switched off, and refusing
    to poll *anything* because one entry is stale would be the wrong trade — so unusable
    entries are logged and skipped.

    Args:
        names: The selection, as names or enum members. ``None`` or empty means "every
            enabled provider".

    Returns:
        The resolved instances, in the order requested, de-duplicated.
    """
    if not names:
        return all_providers()

    resolved: list[ATSProvider] = []
    seen: set[str] = set()
    for name in names:
        key = _normalize(name).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            resolved.append(get_provider(key))
        except PluginNotFound:
            logger.warning("jobs.provider_unknown", provider=key)
        except PluginDisabled:
            logger.info("jobs.provider_disabled", provider=key)
    return resolved


def provider_class_for_url(url: str) -> type[ATSProvider] | None:
    """Identify the provider that owns *url* from ``URL_PATTERNS`` alone.

    Synchronous and free: no plugin is constructed and no request is made, so this is what a
    UI should call while the user is still typing. It cannot see a provider that overrides
    :meth:`~app.jobs.base.ATSProvider.detect` with a network probe — use
    :func:`provider_for_url` when correctness matters more than latency.

    Args:
        url: A posting URL.

    Returns:
        The first matching provider class in name order, or ``None``.
    """
    if not url:
        return None
    matches: list[type[ATSProvider]] = []
    for name in available_provider_names():
        try:
            provider_class = get_provider_class(name)
        except PluginNotFound:  # pragma: no cover - the name came from the registry
            continue
        matcher = getattr(provider_class, "matches_url", None)
        if callable(matcher) and matcher(url):
            matches.append(provider_class)

    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "jobs.url_claimed_by_multiple_providers",
            url=url,
            providers=[cls.meta.name for cls in matches],
        )
    return matches[0]


async def provider_for_url(url: str) -> ATSProvider | None:
    """Find the enabled provider that can handle *url*.

    Each enabled provider is asked, in deterministic name order, via its
    :meth:`~app.jobs.base.ATSProvider.detect` — which is asynchronous precisely so that a
    provider whose ownership cannot be decided from the hostname (a careers page that only
    reveals its embedded ATS in the page source) can make a request to find out. The first
    provider to claim the URL wins.

    A provider whose ``detect`` raises is treated as "not mine" rather than being allowed to
    abort the search: one broken integration must not make every pasted link unusable.

    Args:
        url: A posting URL.

    Returns:
        The claiming provider, or ``None`` when no enabled provider recognises the URL — in
        which case the posting is handled manually.
    """
    if not url:
        return None

    claimants: list[ATSProvider] = []
    for provider in all_providers():
        detector: Any = getattr(provider, "detect", None)
        if not callable(detector):  # pragma: no cover - every ATSProvider has detect
            continue
        try:
            if await detector(url):
                claimants.append(provider)
        except Exception as exc:
            logger.warning(
                "jobs.provider_detect_failed",
                provider=getattr(provider.meta, "name", "?"),
                url=url,
                error=str(exc),
            )

    if not claimants:
        logger.debug("jobs.no_provider_for_url", url=url)
        return None
    if len(claimants) > 1:
        logger.warning(
            "jobs.url_claimed_by_multiple_providers",
            url=url,
            providers=[provider.meta.name for provider in claimants],
        )
    return claimants[0]


def describe_providers() -> list[dict[str, Any]]:
    """Describe every registered provider for ``GET /settings/plugins``.

    Nothing is constructed, so this is safe to call even when a provider's constructor would
    fail for want of a credential.

    Returns:
        One JSON-ready dictionary per provider: its :class:`~app.plugins.base.PluginMeta`
        fields plus ``supports_auto_apply``, ``requires_login``, ``enabled``, and the number
        of URL patterns it claims.
    """
    load_all()
    described: list[dict[str, Any]] = []
    for name in available_provider_names():
        try:
            provider_class = get_provider_class(name)
        except PluginNotFound:  # pragma: no cover - the name came from the registry
            continue
        entry = dict(provider_class.meta.as_dict())
        entry.update(
            {
                "supports_auto_apply": bool(getattr(provider_class, "supports_auto_apply", False)),
                "requires_login": bool(getattr(provider_class, "requires_login", False)),
                "enabled": registry.is_enabled(PluginKind.PROVIDER, name),
                "url_pattern_count": len(getattr(provider_class, "URL_PATTERNS", []) or []),
            }
        )
        described.append(entry)
    return described
