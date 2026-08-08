"""Plugin discovery — importing the built-ins, then anything third parties installed.

:func:`load_all` is what turns an empty :data:`~app.plugins.registry.registry` into a
populated one. It runs once, early in application startup (and at the top of each Celery
worker), and does two things in order:

1. **Imports the built-in plugin packages.** Importing ``app.jobs`` executes the
   ``@plugin`` decorators on the Greenhouse, Lever, Ashby, Workday and LinkedIn providers;
   ``app.ai.models`` registers the model clients; ``app.knowledge.analyzers`` the source
   analyzers; ``app.documents`` the resume templates. Each import is guarded: a package
   that does not exist yet — or whose optional dependency is missing — is skipped with a
   log line instead of taking the process down.

2. **Walks the ``applicantos.*`` entry-point groups.** Any installed distribution can
   contribute a provider, model, template, parser or analyzer by declaring an entry point
   in one of the groups in :data:`ENTRY_POINT_GROUPS`. A third-party plugin that fails to
   import, or that turns out not to be a valid plugin, is logged and skipped — one broken
   package must never prevent the application from starting.

The whole function is idempotent and guarded by a lock, so calling it from the FastAPI
lifespan and from a worker bootstrap in the same process is safe. :func:`reload_all` is the
test-and-development counterpart: it re-executes the decorators by reloading the built-in
modules, which plain re-importing cannot do.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import sys
import threading
from collections.abc import Iterable
from typing import Any, Final

import structlog

from app.models.enums import PluginKind
from app.plugins.base import PluginError, PluginMeta
from app.plugins.registry import registry

__all__ = [
    "BUILTIN_PLUGIN_MODULES",
    "ENTRY_POINT_GROUPS",
    "is_loaded",
    "load_all",
    "reload_all",
]

logger = structlog.get_logger(__name__)

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: Entry-point group per plugin kind (``docs/CONTRACTS.md`` §6). A distribution publishing
#: ``[project.entry-points."applicantos.providers"]`` extends ApplicantOS with a new ATS
#: without touching this repository.
ENTRY_POINT_GROUPS: Final[dict[PluginKind, str]] = {
    PluginKind.PROVIDER: "applicantos.providers",
    PluginKind.MODEL: "applicantos.models",
    PluginKind.TEMPLATE: "applicantos.templates",
    PluginKind.PARSER: "applicantos.parsers",
    PluginKind.ANALYZER: "applicantos.analyzers",
}

#: Packages whose import registers the built-in plugins. Importing the package is enough:
#: each package's ``__init__`` imports its concrete modules, whose ``@plugin`` decorators
#: run at import time. Listed in dependency order, cheapest first.
BUILTIN_PLUGIN_MODULES: Final[tuple[str, ...]] = (
    "app.jobs",
    "app.ai.models",
    "app.knowledge.analyzers",
    "app.documents",
)

#: Guards the module-level loaded flag. Reentrant because :func:`reload_all` calls
#: :func:`load_all` while already holding it.
_load_lock: Final[threading.RLock] = threading.RLock()

#: Whether :func:`load_all` has completed. Module-level rather than an attribute so that
#: every importer of this module shares one answer.
_loaded: bool = False


def is_loaded() -> bool:
    """Return whether :func:`load_all` has already run in this process."""
    return _loaded


# --------------------------------------------------------------------------------------
# Built-in packages
# --------------------------------------------------------------------------------------


def _is_missing_module(error: ImportError, module_name: str) -> bool:
    """Distinguish "this package does not exist" from "one of its imports failed".

    Both surface as :class:`ImportError`, but they mean very different things: the first is
    expected during a phased build and while running a slim install, the second is a real
    problem an operator needs to see.

    Args:
        error: The raised import error.
        module_name: The module that was being imported.

    Returns:
        ``True`` when *error* is about *module_name* itself (or a parent of it).
    """
    missing = getattr(error, "name", None)
    if not missing:
        return False
    return module_name == missing or module_name.startswith(f"{missing}.")


def _import_builtins() -> None:
    """Import every built-in plugin package, skipping the ones that are absent."""
    for module_name in BUILTIN_PLUGIN_MODULES:
        try:
            importlib.import_module(module_name)
        except ImportError as exc:
            if _is_missing_module(exc, module_name):
                logger.debug("plugins.builtin_absent", module=module_name)
            else:
                logger.warning(
                    "plugins.builtin_dependency_missing",
                    module=module_name,
                    missing=getattr(exc, "name", None),
                    error=str(exc),
                )
        except Exception as exc:
            logger.warning("plugins.builtin_import_failed", module=module_name, error=str(exc))
        else:
            logger.debug("plugins.builtin_imported", module=module_name)


# --------------------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------------------


def _candidate_classes(loaded: Any) -> tuple[type, ...]:
    """Interpret whatever an entry point resolved to as a sequence of plugin classes.

    Three shapes are supported, in decreasing order of directness: the class itself, a
    zero-argument factory returning a class, and a factory returning an iterable of
    classes (how a distribution ships several providers behind one entry point).

    Args:
        loaded: The object :meth:`importlib.metadata.EntryPoint.load` returned.

    Returns:
        The classes to register; empty when the object is none of the supported shapes.
    """
    if isinstance(loaded, type):
        return (loaded,)

    if callable(loaded):
        try:
            produced = loaded()
        except Exception as exc:
            logger.warning("plugins.entry_point_factory_failed", error=str(exc))
            return ()
        if isinstance(produced, type):
            return (produced,)
        if isinstance(produced, Iterable) and not isinstance(produced, (str, bytes)):
            return tuple(item for item in produced if isinstance(item, type))
        return ()

    return ()


def _register_entry_point_class(kind: PluginKind, group: str, name: str, cls: type) -> bool:
    """Register one class discovered through an entry point.

    Args:
        kind: The plugin kind implied by the entry-point group.
        group: The entry-point group name, for logging.
        name: The entry-point name, for logging.
        cls: The class to register.

    Returns:
        ``True`` when the class was registered.
    """
    declared = vars(cls).get("meta")
    if isinstance(declared, PluginMeta) and declared.kind is not kind:
        # Not fatal: the metadata is authoritative, the group is a hint. Say so loudly,
        # because it is almost always a packaging mistake.
        logger.warning(
            "plugins.entry_point_kind_mismatch",
            group=group,
            entry_point=name,
            declared_kind=str(declared.kind),
            expected_kind=str(kind),
        )
    try:
        registry.register(cls)  # type: ignore[arg-type]
    except PluginError as exc:
        logger.warning(
            "plugins.entry_point_rejected",
            group=group,
            entry_point=name,
            implementation=f"{cls.__module__}.{cls.__qualname__}",
            error=str(exc),
        )
        return False
    logger.info(
        "plugins.entry_point_loaded",
        group=group,
        entry_point=name,
        implementation=f"{cls.__module__}.{cls.__qualname__}",
    )
    return True


def _load_entry_points() -> int:
    """Load every third-party plugin advertised under the ApplicantOS entry-point groups.

    Returns:
        The number of classes successfully registered.
    """
    registered = 0
    for kind, group in ENTRY_POINT_GROUPS.items():
        try:
            discovered = importlib.metadata.entry_points(group=group)
        except Exception as exc:
            logger.warning("plugins.entry_point_scan_failed", group=group, error=str(exc))
            continue

        for entry_point in discovered:
            try:
                loaded = entry_point.load()
            except Exception as exc:
                logger.warning(
                    "plugins.entry_point_import_failed",
                    group=group,
                    entry_point=entry_point.name,
                    error=str(exc),
                )
                continue

            classes = _candidate_classes(loaded)
            if not classes:
                logger.warning(
                    "plugins.entry_point_unusable",
                    group=group,
                    entry_point=entry_point.name,
                    resolved=type(loaded).__name__,
                )
                continue

            for cls in classes:
                if _register_entry_point_class(kind, group, entry_point.name, cls):
                    registered += 1
    return registered


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------


def load_all() -> None:
    """Discover and register every plugin. Idempotent.

    Call once during startup, before anything resolves a plugin. Subsequent calls return
    immediately, so wiring it into both the FastAPI lifespan and the Celery worker
    bootstrap is safe.

    Never raises: a built-in package that is absent, a dependency that is not installed,
    and a third-party plugin that fails to import are all logged and skipped. The
    application must start even when parts of it cannot.
    """
    global _loaded
    with _load_lock:
        if _loaded:
            return
        _import_builtins()
        third_party = _load_entry_points()
        _loaded = True

    logger.info("plugins.loaded", counts=registry.counts(), third_party=third_party)


def reload_all() -> None:
    """Clear the registry and rediscover every plugin from scratch.

    A development and test helper. Plain re-importing cannot re-register anything, because
    an already-imported module is served from :data:`sys.modules` and its ``@plugin``
    decorators do not run again; this function therefore reloads each built-in plugin
    module explicitly before rediscovering.

    Application code must not call this — resetting the registry drops every cached plugin
    instance, including open HTTP clients and SDK sessions.
    """
    global _loaded
    with _load_lock:
        registry.clear()
        _reload_builtin_modules()
        _loaded = False
        load_all()


def _reload_builtin_modules() -> None:
    """Re-execute the already-imported built-in plugin modules and their submodules.

    Submodules are reloaded too — reloading ``app.jobs`` alone would re-run its ``__init__``
    but its ``import app.jobs.greenhouse`` would be served from the module cache, so the
    provider's decorator would never fire again. Parents are reloaded before children so a
    submodule always sees a freshly-initialised package.
    """
    targets = sorted(
        name
        for name in list(sys.modules)
        if any(
            name == package or name.startswith(f"{package}.") for package in BUILTIN_PLUGIN_MODULES
        )
    )
    for name in targets:
        module = sys.modules.get(name)
        if module is None:
            continue
        try:
            importlib.reload(module)
        except Exception as exc:
            logger.warning("plugins.builtin_reload_failed", module=name, error=str(exc))
