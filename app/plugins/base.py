"""Plugin foundations — metadata, the plugin protocol, and the error hierarchy.

ApplicantOS is plugin-driven by design (``docs/CONTRACTS.md`` §6). Five kinds of component
are pluggable, and every one of them is discovered, instantiated and resolved through this
system rather than by importing a concrete module:

``provider``
    An applicant-tracking system — Greenhouse, Lever, Ashby, Workday, LinkedIn.
``model``
    An LLM client — Anthropic, OpenAI, a local OpenAI-compatible server, or the offline
    ``null`` stub that lets the whole pipeline run with zero API keys.
``template``
    A resume/cover-letter renderer — LaTeX, DOCX, HTML, Markdown.
``analyzer``
    A knowledge source analyzer — GitHub, a personal website, a project folder, a resume,
    a LinkedIn export.
``tracker``
    A status-sync channel — a mailbox or an ATS portal polled for application outcomes.

Document *reading* is deliberately not one of them: the PDF/DOCX/HTML readers in
:mod:`app.knowledge.analyzers.document` are chosen by inspecting the bytes, which a
registry keyed by ``(kind, name)`` cannot do. See
:class:`~app.models.enums.PluginKind`.

Golden rule #5 is what this buys: *never import a concrete provider, analyzer, model or
template from outside its own package.* Adding a fifth ATS or a third resume template must
be a matter of registering a class, never of editing a dispatch table somewhere else.

This module defines the vocabulary. :mod:`app.plugins.registry` holds the instances,
:mod:`app.plugins.loader` finds the classes.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

import structlog

from app.models.enums import PluginKind

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.config.settings import Settings

__all__ = [
    "BasePlugin",
    "Plugin",
    "PluginDisabled",
    "PluginError",
    "PluginLoadError",
    "PluginMeta",
    "PluginNotFound",
]

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PluginMeta:
    """Immutable description of one plugin, declared as a class attribute on it.

    ``meta`` is how a class announces itself: the registry indexes on ``kind`` and
    ``name``, the settings API renders ``display_name``/``description``/``version``, and
    ``capabilities`` lets a caller ask what a plugin can do without instantiating it — for
    example whether a provider can submit an application or only discover postings.

    Attributes:
        kind: Which of the five extension points this plugin implements. A plain string is
            accepted and coerced to the enum member.
        name: Stable identifier, unique within ``kind``. This is what appears in settings,
            in ``UserPreferences.providers_enabled``, and in every ``registry.get`` call,
            so it is part of the persisted state and must not change casually.
        version: Semantic version of the plugin implementation.
        display_name: Human-readable label; falls back to ``name`` via :attr:`label`.
        description: One-line explanation shown in the desktop settings screen.
        author: Attribution for third-party plugins installed via entry points.
        capabilities: Free-form capability tags, e.g. ``{"auto_apply", "search"}``.
        enabled_by_default: Whether the plugin is usable as soon as it registers. A plugin
            registering with ``False`` is present and describable but must be explicitly
            enabled before :meth:`~app.plugins.registry.PluginRegistry.get` returns it.
    """

    kind: PluginKind
    name: str
    version: str = "1.0.0"
    display_name: str = ""
    description: str = ""
    author: str = ""
    capabilities: frozenset[str] = frozenset()
    enabled_by_default: bool = True

    def __post_init__(self) -> None:
        """Validate the identity fields and normalise the loosely-typed ones.

        Only representations are normalised, never values: a ``kind`` given as ``"provider"``
        becomes :attr:`~app.models.enums.PluginKind.PROVIDER` (an equal object), and an
        iterable of capabilities becomes a ``frozenset`` of the same strings.

        Raises:
            ValueError: If ``kind`` is not a valid :class:`~app.models.enums.PluginKind`,
                or ``name`` is empty or blank.
        """
        try:
            coerced_kind = PluginKind(self.kind)
        except ValueError as exc:
            raise ValueError(
                f"PluginMeta.kind must be one of {PluginKind.values()}, got {self.kind!r}"
            ) from exc
        object.__setattr__(self, "kind", coerced_kind)

        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("PluginMeta.name must be a non-empty string")
        object.__setattr__(self, "name", self.name.strip())

        if not isinstance(self.capabilities, frozenset):
            object.__setattr__(self, "capabilities", frozenset(self.capabilities))

    @property
    def label(self) -> str:
        """Human-facing name: :attr:`display_name` when set, otherwise :attr:`name`."""
        return self.display_name or self.name

    @property
    def identity(self) -> tuple[PluginKind, str]:
        """The ``(kind, name)`` pair this plugin is registered under."""
        return (self.kind, self.name)

    def has_capability(self, capability: str) -> bool:
        """Return whether this plugin advertises *capability*.

        Args:
            capability: The capability tag to test for.

        Returns:
            ``True`` when the tag is present in :attr:`capabilities`.
        """
        return capability in self.capabilities

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-ready description, as served by ``GET /settings/plugins``."""
        return {
            "kind": self.kind.value,
            "name": self.name,
            "version": self.version,
            "display_name": self.label,
            "description": self.description,
            "author": self.author,
            "capabilities": sorted(self.capabilities),
            "enabled_by_default": self.enabled_by_default,
        }


# --------------------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------------------


@runtime_checkable
class Plugin(Protocol):
    """The structural type every plugin satisfies (``docs/CONTRACTS.md`` §6).

    A plugin is any class that declares :attr:`meta`, can be constructed from
    :class:`~app.config.settings.Settings`, and can report its own health. Inheriting from
    :class:`BasePlugin` is the convenient route, but nothing requires it — a class that
    matches this shape is a plugin.

    Note:
        ``issubclass`` against this protocol is not supported, because it carries the
        non-method member :attr:`meta`. The registry validates structurally instead.
    """

    meta: ClassVar[PluginMeta]

    def __init__(self, settings: Settings, **kw: Any) -> None:
        """Construct the plugin from application settings and optional extras."""
        ...

    async def healthcheck(self) -> bool:
        """Return whether this plugin is usable right now."""
        ...


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class PluginError(Exception):
    """Base class for every plugin-system failure.

    Attributes:
        kind: The plugin kind involved, when the failure is attributable to one.
        name: The plugin name involved, when the failure is attributable to one.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: PluginKind | None = None,
        name: str | None = None,
    ) -> None:
        """Record the failure and the plugin identity it concerns.

        Args:
            message: Human-readable explanation, shown to the operator.
            kind: The plugin kind involved, if known.
            name: The plugin name involved, if known.
        """
        super().__init__(message)
        self.kind = kind
        self.name = name


class PluginNotFound(PluginError):
    """No plugin is registered under the requested ``(kind, name)``.

    Usually a typo in configuration — a provider listed in
    ``UserPreferences.providers_enabled`` that does not exist — or
    :func:`app.plugins.loader.load_all` not having run yet. The message lists the names
    that *are* available for the kind.
    """


class PluginLoadError(PluginError):
    """A plugin could not be registered, imported, or constructed.

    Raised for a class that declares no ``meta``, a second class claiming an already-taken
    ``(kind, name)``, or a constructor that raised.
    """


class PluginDisabled(PluginError):
    """The plugin exists but is switched off.

    Either it registered with ``enabled_by_default=False``, or
    :meth:`~app.plugins.registry.PluginRegistry.disable` was called. Distinct from
    :class:`PluginNotFound` on purpose: "you turned this off" and "this does not exist"
    call for different fixes.
    """


# --------------------------------------------------------------------------------------
# Convenience base class
# --------------------------------------------------------------------------------------


class BasePlugin(abc.ABC):
    """Convenience base implementing the boilerplate every plugin would otherwise repeat.

    Subclasses declare their own :attr:`meta` — the registry requires each registered class
    to declare it directly, so an inherited ``meta`` is never mistaken for a new plugin —
    and override :meth:`healthcheck` when there is something real to check.

    Example::

        @plugin
        class GreenhouseProvider(BasePlugin):
            meta = PluginMeta(
                kind=PluginKind.PROVIDER,
                name="greenhouse",
                display_name="Greenhouse",
                capabilities=frozenset({"search", "auto_apply"}),
            )

    Attributes:
        settings: The application settings this instance was constructed with.
        options: Any extra keyword arguments the registry or caller passed in.
        logger: A structlog logger pre-bound with this plugin's kind and name.
    """

    meta: ClassVar[PluginMeta]

    def __init__(self, settings: Settings, **kw: Any) -> None:
        """Store the configuration this plugin instance runs against.

        Args:
            settings: Application settings, supplied by the registry.
            **kw: Extra construction options, kept on :attr:`options` for subclasses.
        """
        self.settings = settings
        self.options: dict[str, Any] = dict(kw)
        self.logger = logger.bind(plugin=self.name, plugin_kind=str(self.kind))

    @property
    def name(self) -> str:
        """This plugin's registered name.

        Returns:
            :attr:`PluginMeta.name`, or the class name when ``meta`` was never declared —
            which only happens for an abstract intermediate class that is never registered.
        """
        meta = getattr(type(self), "meta", None)
        return meta.name if isinstance(meta, PluginMeta) else type(self).__name__

    @property
    def kind(self) -> PluginKind | None:
        """This plugin's kind, or ``None`` when ``meta`` was never declared."""
        meta = getattr(type(self), "meta", None)
        return meta.kind if isinstance(meta, PluginMeta) else None

    async def healthcheck(self) -> bool:
        """Report whether this plugin can do its job right now.

        The default answer is ``True``: a plugin with no external dependency — a Markdown
        renderer, the offline null model — is always healthy. Plugins that talk to a
        network service override this to probe it, and ``GET /ready`` aggregates the
        results.

        Returns:
            ``True`` when the plugin is usable.
        """
        return True

    def __repr__(self) -> str:
        """Return ``<ClassName kind/name>`` for logs and error messages."""
        return f"<{type(self).__name__} {self.kind}/{self.name}>"
