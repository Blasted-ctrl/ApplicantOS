"""The plugin registry — one place that knows every pluggable component.

Everything pluggable in ApplicantOS is resolved here (``docs/CONTRACTS.md`` §6, golden rule
#5). A concrete ATS provider, LLM client, resume template, parser or knowledge analyzer is
imported only by its own package; every other module asks the registry for it by kind and
name::

    from app.plugins import PluginKind, registry

    provider = registry.get(PluginKind.PROVIDER, "greenhouse")
    for analyzer in registry.all(PluginKind.ANALYZER):
        ...

Behaviour worth knowing:

* **Registration is idempotent.** Registering the same class twice is a no-op, so a module
  imported through two paths cannot fail startup. Registering a *different* class under an
  already-taken ``(kind, name)`` raises :class:`~app.plugins.base.PluginLoadError` rather
  than silently shadowing the first — a name collision between a built-in provider and a
  third-party one must be visible.
* **A class must declare its own ``meta``.** An inherited ``meta`` is rejected, because a
  subclass that forgot to declare one would otherwise register itself as a duplicate of
  its parent.
* **Instances are lazy and shared.** A plugin is constructed on first :meth:`get` and
  cached per ``(kind, name)``, so an HTTP client or a template engine is built once. The
  constructor receives :class:`~app.config.settings.Settings`.
* **Lookups are forgiving, storage is faithful.** Names are matched case-insensitively and
  whitespace-trimmed, while :meth:`describe` reports the name exactly as declared.
* **Thread-safe.** All state is guarded by a reentrant lock, so a plugin constructor may
  itself resolve another plugin, and Celery's thread pool cannot corrupt the index.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from app.config.settings import get_settings
from app.models.enums import PluginKind
from app.plugins.base import (
    Plugin,
    PluginDisabled,
    PluginLoadError,
    PluginMeta,
    PluginNotFound,
)

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.config.settings import Settings

__all__ = ["PluginRegistry", "plugin", "registry"]

logger = structlog.get_logger(__name__)


def _normalize_name(name: str) -> str:
    """Reduce a plugin name to its lookup form.

    Args:
        name: A plugin name from configuration, an API request, or a decorator.

    Returns:
        The trimmed, lowercased name used as the index key.

    Raises:
        TypeError: If *name* is not a string.
    """
    if not isinstance(name, str):
        raise TypeError(f"plugin name must be a string, got {type(name).__name__}")
    return name.strip().lower()


@dataclass(slots=True)
class _Registration:
    """One registered plugin class and its mutable enable/disable state.

    Attributes:
        cls: The registered class.
        meta: The metadata the class declared.
        disabled: Whether :meth:`PluginRegistry.get` currently refuses to return it.
    """

    cls: type[Plugin]
    meta: PluginMeta
    disabled: bool = False


class PluginRegistry:
    """An index of plugin classes by kind and name, with lazily-constructed instances.

    A module-level singleton, :data:`registry`, is what the application uses. Constructing
    a private registry is useful in tests that want an isolated set of plugins.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        """Create an empty registry.

        Args:
            settings: Settings passed to every plugin constructor. When ``None`` (the
                normal case) the process-wide settings are resolved on first use, so
                importing this module never reads the environment.
        """
        self._lock = threading.RLock()
        self._index: dict[PluginKind, dict[str, _Registration]] = {}
        self._instances: dict[tuple[PluginKind, str], Plugin] = {}
        self._settings_override = settings

    # -- configuration ------------------------------------------------------------------

    def configure(self, settings: Settings | None) -> None:
        """Set the settings object handed to plugin constructors.

        Existing instances are discarded so that the next :meth:`get` rebuilds them
        against the new configuration.

        Args:
            settings: The settings to use, or ``None`` to fall back to the process-wide
                singleton.
        """
        with self._lock:
            self._settings_override = settings
            self._instances.clear()

    def _settings(self) -> Settings:
        """Return the settings plugin constructors receive."""
        return self._settings_override if self._settings_override is not None else get_settings()

    # -- registration -------------------------------------------------------------------

    @staticmethod
    def _meta_of(cls: type[Plugin]) -> PluginMeta:
        """Extract and validate the metadata *cls* declares.

        Args:
            cls: A candidate plugin class.

        Returns:
            The declared :class:`~app.plugins.base.PluginMeta`.

        Raises:
            PluginLoadError: If *cls* is not a class, declares no ``meta`` of its own, or
                declares something that is not a :class:`~app.plugins.base.PluginMeta`.
        """
        if not isinstance(cls, type):
            raise PluginLoadError(f"only classes can be registered as plugins, got {cls!r}")

        # ``vars`` rather than ``getattr``: an inherited ``meta`` means the subclass forgot
        # to declare its own, and registering it would duplicate its parent's identity.
        declared = vars(cls).get("meta")
        if declared is None:
            inherited = getattr(cls, "meta", None)
            hint = (
                " (it inherits `meta` from a base class; every registered plugin must"
                " declare its own)"
                if isinstance(inherited, PluginMeta)
                else ""
            )
            raise PluginLoadError(
                f"{cls.__module__}.{cls.__qualname__} declares no `meta` class attribute{hint}"
            )

        if not isinstance(declared, PluginMeta):
            raise PluginLoadError(
                f"{cls.__module__}.{cls.__qualname__}.meta must be a PluginMeta, "
                f"got {type(declared).__name__}"
            )
        if not isinstance(declared.kind, PluginKind):  # pragma: no cover - PluginMeta coerces
            raise PluginLoadError(
                f"{cls.__module__}.{cls.__qualname__}.meta.kind is not a PluginKind",
                name=declared.name,
            )
        return declared

    def register(self, cls: type[Plugin]) -> type[Plugin]:
        """Register a plugin class under its declared ``(kind, name)``.

        Idempotent: registering the same class again returns immediately, so a module
        imported both as a built-in and through an entry point is harmless.

        Args:
            cls: The plugin class. It must declare its own ``meta``.

        Returns:
            *cls*, unchanged, so this can be used as a decorator.

        Raises:
            PluginLoadError: If the class declares no valid ``meta``, or a *different*
                class is already registered under the same kind and name.
        """
        meta = self._meta_of(cls)
        key = _normalize_name(meta.name)

        with self._lock:
            bucket = self._index.setdefault(meta.kind, {})
            existing = bucket.get(key)
            if existing is not None:
                if existing.cls is cls:
                    return cls
                raise PluginLoadError(
                    f"cannot register {cls.__module__}.{cls.__qualname__} as "
                    f"{meta.kind}/{meta.name}: already registered by "
                    f"{existing.cls.__module__}.{existing.cls.__qualname__}",
                    kind=meta.kind,
                    name=meta.name,
                )
            bucket[key] = _Registration(
                cls=cls,
                meta=meta,
                disabled=not meta.enabled_by_default,
            )

        logger.debug(
            "plugin.registered",
            plugin_kind=str(meta.kind),
            plugin=meta.name,
            version=meta.version,
            enabled=meta.enabled_by_default,
            implementation=f"{cls.__module__}.{cls.__qualname__}",
        )
        return cls

    def unregister(self, kind: PluginKind, name: str) -> bool:
        """Remove a plugin and any cached instance of it.

        Args:
            kind: The plugin kind.
            name: The plugin name.

        Returns:
            ``True`` when something was removed.
        """
        resolved_kind = PluginKind(kind)
        key = _normalize_name(name)
        with self._lock:
            removed = self._index.get(resolved_kind, {}).pop(key, None)
            self._instances.pop((resolved_kind, key), None)
        return removed is not None

    def clear(self) -> None:
        """Drop every registration and instance.

        Used by :func:`app.plugins.loader.reload_all` and by tests that need a pristine
        registry. Application code should never call this.
        """
        with self._lock:
            self._index.clear()
            self._instances.clear()

    # -- lookup ---------------------------------------------------------------------------

    def _registration(self, kind: PluginKind, name: str) -> _Registration:
        """Return the registration for ``(kind, name)``.

        Args:
            kind: The plugin kind.
            name: The plugin name, matched case-insensitively.

        Returns:
            The stored registration.

        Raises:
            PluginNotFound: When nothing is registered under that kind and name. The
                message lists what *is* available, which turns a configuration typo into a
                one-line fix.
        """
        bucket = self._index.get(kind, {})
        found = bucket.get(_normalize_name(name))
        if found is not None:
            return found

        available = sorted(registration.meta.name for registration in bucket.values())
        if available:
            detail = f"available {kind} plugins: {', '.join(available)}"
        else:
            detail = (
                f"no {kind} plugins are registered (has app.plugins.loader.load_all() been called?)"
            )
        raise PluginNotFound(f"unknown {kind} plugin {name!r}; {detail}", kind=kind, name=name)

    def get(self, kind: PluginKind, name: str) -> Plugin:
        """Return the shared instance of a plugin, constructing it on first use.

        Args:
            kind: The plugin kind.
            name: The plugin name, matched case-insensitively.

        Returns:
            The cached plugin instance.

        Raises:
            PluginNotFound: If no plugin is registered under that kind and name.
            PluginDisabled: If the plugin is registered but switched off.
            PluginLoadError: If the plugin's constructor raised.
        """
        resolved_kind = PluginKind(kind)
        key = _normalize_name(name)

        with self._lock:
            registration = self._registration(resolved_kind, key)
            if registration.disabled:
                raise PluginDisabled(
                    f"{resolved_kind} plugin {registration.meta.name!r} is disabled",
                    kind=resolved_kind,
                    name=registration.meta.name,
                )

            instance = self._instances.get((resolved_kind, key))
            if instance is None:
                instance = self._instantiate(registration)
                self._instances[(resolved_kind, key)] = instance
            return instance

    def _instantiate(self, registration: _Registration) -> Plugin:
        """Construct one plugin, converting any constructor failure into a plugin error.

        Args:
            registration: The registration to instantiate.

        Returns:
            The new instance.

        Raises:
            PluginLoadError: If the constructor raised, with the original exception
                chained so the traceback survives.
        """
        meta = registration.meta
        try:
            instance = registration.cls(self._settings())
        except Exception as exc:
            logger.warning(
                "plugin.instantiation_failed",
                plugin_kind=str(meta.kind),
                plugin=meta.name,
                error=str(exc),
            )
            raise PluginLoadError(
                f"failed to construct {meta.kind} plugin {meta.name!r}: {exc}",
                kind=meta.kind,
                name=meta.name,
            ) from exc

        logger.debug("plugin.instantiated", plugin_kind=str(meta.kind), plugin=meta.name)
        return instance

    def get_class(self, kind: PluginKind, name: str) -> type[Plugin]:
        """Return a plugin's class without constructing it.

        Useful for introspection — reading class-level attributes such as a provider's
        ``URL_PATTERNS`` or ``supports_auto_apply`` — with no construction cost.

        Args:
            kind: The plugin kind.
            name: The plugin name, matched case-insensitively.

        Returns:
            The registered class, whether or not the plugin is enabled.

        Raises:
            PluginNotFound: If no plugin is registered under that kind and name.
        """
        with self._lock:
            return self._registration(PluginKind(kind), name).cls

    def all(self, kind: PluginKind) -> list[Plugin]:
        """Return an instance of every enabled plugin of *kind*, ordered by name.

        Args:
            kind: The plugin kind.

        Returns:
            The instances. Disabled plugins are omitted.

        Raises:
            PluginLoadError: If any enabled plugin's constructor raised. Failures are not
                swallowed: silently omitting a broken provider would silently narrow a
                discovery run.
        """
        resolved_kind = PluginKind(kind)
        with self._lock:
            enabled = sorted(
                (
                    registration.meta.name
                    for registration in self._index.get(resolved_kind, {}).values()
                    if not registration.disabled
                ),
                key=str.lower,
            )
            return [self.get(resolved_kind, name) for name in enabled]

    def names(self, kind: PluginKind) -> list[str]:
        """Return the registered names for *kind*, enabled or not, ordered by name.

        Args:
            kind: The plugin kind.

        Returns:
            The names exactly as each plugin declared them.
        """
        resolved_kind = PluginKind(kind)
        with self._lock:
            return sorted(
                (
                    registration.meta.name
                    for registration in self._index.get(resolved_kind, {}).values()
                ),
                key=str.lower,
            )

    def describe(self) -> list[PluginMeta]:
        """Return the metadata of every registered plugin, ordered by kind then name.

        Nothing is constructed, so this is safe to call from ``GET /settings/plugins`` even
        when a plugin's constructor would fail for want of an API key.

        Returns:
            One :class:`~app.plugins.base.PluginMeta` per registration.
        """
        with self._lock:
            metas = [
                registration.meta
                for bucket in self._index.values()
                for registration in bucket.values()
            ]
        return sorted(metas, key=lambda meta: (meta.kind.value, meta.name.lower()))

    # -- enable / disable -------------------------------------------------------------------

    def is_registered(self, kind: PluginKind, name: str) -> bool:
        """Return whether a plugin is registered under *kind* and *name*."""
        with self._lock:
            return _normalize_name(name) in self._index.get(PluginKind(kind), {})

    def is_enabled(self, kind: PluginKind, name: str) -> bool:
        """Return whether a registered plugin is currently usable.

        Args:
            kind: The plugin kind.
            name: The plugin name.

        Returns:
            ``True`` when the plugin is registered and not disabled.

        Raises:
            PluginNotFound: If no plugin is registered under that kind and name.
        """
        with self._lock:
            return not self._registration(PluginKind(kind), name).disabled

    def disable(self, kind: PluginKind, name: str) -> None:
        """Switch a plugin off.

        Subsequent :meth:`get` calls raise :class:`~app.plugins.base.PluginDisabled` and
        :meth:`all` skips it. Any cached instance is released, so a provider's HTTP client
        or a model's SDK session is not kept alive by a plugin nobody can reach.

        Args:
            kind: The plugin kind.
            name: The plugin name.

        Raises:
            PluginNotFound: If no plugin is registered under that kind and name.
        """
        resolved_kind = PluginKind(kind)
        key = _normalize_name(name)
        with self._lock:
            registration = self._registration(resolved_kind, key)
            registration.disabled = True
            self._instances.pop((resolved_kind, key), None)
        logger.info(
            "plugin.disabled", plugin_kind=str(resolved_kind), plugin=registration.meta.name
        )

    def enable(self, kind: PluginKind, name: str) -> None:
        """Switch a plugin back on.

        Args:
            kind: The plugin kind.
            name: The plugin name.

        Raises:
            PluginNotFound: If no plugin is registered under that kind and name.
        """
        resolved_kind = PluginKind(kind)
        with self._lock:
            registration = self._registration(resolved_kind, name)
            registration.disabled = False
        logger.info("plugin.enabled", plugin_kind=str(resolved_kind), plugin=registration.meta.name)

    # -- diagnostics ----------------------------------------------------------------------------

    async def healthcheck(self, kind: PluginKind | None = None) -> dict[str, bool]:
        """Probe every enabled plugin and report which ones are usable.

        Backs ``GET /ready``. A plugin whose healthcheck raises is reported as unhealthy
        rather than propagating — a readiness probe must always produce an answer.

        Args:
            kind: Restrict the probe to one kind, or ``None`` to probe all of them.

        Returns:
            A mapping of ``"<kind>/<name>"`` to health, ordered by kind then name.
        """
        kinds = [PluginKind(kind)] if kind is not None else list(PluginKind)
        results: dict[str, bool] = {}
        for resolved_kind in kinds:
            for name in self.names(resolved_kind):
                label = f"{resolved_kind}/{name}"
                try:
                    instance = self.get(resolved_kind, name)
                    results[label] = bool(await instance.healthcheck())
                except PluginDisabled:
                    continue
                except Exception as exc:
                    logger.warning("plugin.healthcheck_failed", plugin=label, error=str(exc))
                    results[label] = False
        return results

    def counts(self) -> dict[str, int]:
        """Return the number of registered plugins per kind, for startup logging."""
        with self._lock:
            return {kind.value: len(bucket) for kind, bucket in sorted(self._index.items())}

    def __contains__(self, identity: tuple[PluginKind, str]) -> bool:
        """Return whether ``(kind, name)`` is registered."""
        kind, name = identity
        return self.is_registered(kind, name)

    def __repr__(self) -> str:
        """Return a description including the per-kind registration counts."""
        return f"<PluginRegistry {self.counts()}>"


#: The process-wide registry. Import this rather than constructing your own, so that every
#: module sees the same plugins.
registry: PluginRegistry = PluginRegistry()


def plugin(cls: type[Plugin]) -> type[Plugin]:
    """Register a class with the process-wide :data:`registry`.

    The decorator form of :meth:`PluginRegistry.register`::

        @plugin
        class LeverProvider(BasePlugin):
            meta = PluginMeta(kind=PluginKind.PROVIDER, name="lever")

    Args:
        cls: The plugin class, declaring its own ``meta``.

    Returns:
        *cls*, unchanged.

    Raises:
        PluginLoadError: If the class declares no valid ``meta``, or its ``(kind, name)``
            is already taken by a different class.
    """
    return registry.register(cls)
