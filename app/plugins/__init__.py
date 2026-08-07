"""Plugin system — the extension mechanism the whole application is built on.

Five things are pluggable in ApplicantOS (``docs/CONTRACTS.md`` §6): ``provider`` (an
applicant-tracking system), ``model`` (an LLM client), ``template`` (a document renderer),
``parser``, and ``analyzer`` (a knowledge source analyzer). Golden rule #5 says a concrete
implementation is imported only inside its own package; everything else goes through here::

    from app.plugins import PluginKind, load_all, registry

    load_all()                                             # once, at startup
    greenhouse = registry.get(PluginKind.PROVIDER, "greenhouse")
    for analyzer in registry.all(PluginKind.ANALYZER):
        ...

Writing one is three lines of ceremony::

    from app.plugins import BasePlugin, PluginKind, PluginMeta, plugin

    @plugin
    class AshbyProvider(BasePlugin):
        meta = PluginMeta(
            kind=PluginKind.PROVIDER,
            name="ashby",
            display_name="Ashby",
            capabilities=frozenset({"search", "auto_apply"}),
        )

The submodules split the work: :mod:`app.plugins.base` defines the vocabulary,
:mod:`app.plugins.registry` holds the classes and their instances, and
:mod:`app.plugins.loader` finds them — built-in packages first, then any
``applicantos.*`` entry point a third-party distribution installed.
"""

from __future__ import annotations

from app.models.enums import PluginKind
from app.plugins.base import (
    BasePlugin,
    Plugin,
    PluginDisabled,
    PluginError,
    PluginLoadError,
    PluginMeta,
    PluginNotFound,
)
from app.plugins.loader import (
    BUILTIN_PLUGIN_MODULES,
    ENTRY_POINT_GROUPS,
    is_loaded,
    load_all,
    reload_all,
)
from app.plugins.registry import PluginRegistry, plugin, registry

__all__ = [
    "BUILTIN_PLUGIN_MODULES",
    "BasePlugin",
    "ENTRY_POINT_GROUPS",
    "Plugin",
    "PluginDisabled",
    "PluginError",
    "PluginKind",
    "PluginLoadError",
    "PluginMeta",
    "PluginNotFound",
    "PluginRegistry",
    "is_loaded",
    "load_all",
    "plugin",
    "registry",
    "reload_all",
]
