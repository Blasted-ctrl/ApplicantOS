"""Route modules, aggregated by walking this package rather than by hand.

``docs/CONTRACTS.md`` §14 lists twelve route groups. Maintaining an import list for them is
the kind of chore that silently rots: a new module gets written, its ``include_router`` line
is forgotten, and the endpoint is missing from a build nobody tested. So :func:`build_api_router`
walks the package instead. Adding a route group means adding a file — nothing else.

Each module in this package declares four module-level names:

``router``
    The :class:`~fastapi.APIRouter`. Required; a module without one is skipped.
``PREFIX``
    Path prefix, e.g. ``"/postings"``. Defaults to ``""``.
``TAGS``
    OpenAPI tags. Defaults to the module's own name.
``MOUNT_AT_ROOT``
    ``True`` for the two groups §14 places *outside* ``/api/v1`` — ``health`` (``/health``,
    ``/ready``, ``/metrics``) and ``events`` (``/ws``). Those are collected by
    :func:`root_routers` and mounted by :func:`app.main.create_app` at the root instead.

Modules are visited in sorted order so the generated OpenAPI document is stable across
runs — a diffable schema is worth more than a hand-picked ordering. Route *matching* order
within a module is declaration order, which is where the static-before-parameterised rule
matters, and that is a per-module concern.

A module that fails to import is logged and skipped rather than taking the process down. One
broken route group must not cost the user the other eleven, and the log line names exactly
what is missing.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType
from typing import Final

import structlog
from fastapi import APIRouter

__all__ = [
    "MOUNT_AT_ROOT_ATTRIBUTE",
    "PREFIX_ATTRIBUTE",
    "ROUTER_ATTRIBUTE",
    "TAGS_ATTRIBUTE",
    "api_router",
    "build_api_router",
    "iter_route_modules",
    "root_routers",
]

logger = structlog.get_logger(__name__)

#: Module attribute holding the router.
ROUTER_ATTRIBUTE: Final[str] = "router"

#: Module attribute holding the path prefix.
PREFIX_ATTRIBUTE: Final[str] = "PREFIX"

#: Module attribute holding the OpenAPI tags.
TAGS_ATTRIBUTE: Final[str] = "TAGS"

#: Module attribute marking a group that lives outside ``/api/v1``.
MOUNT_AT_ROOT_ATTRIBUTE: Final[str] = "MOUNT_AT_ROOT"


def iter_route_modules() -> list[ModuleType]:
    """Import every route module in this package, in sorted order.

    Returns:
        The successfully imported modules. A module that raised on import is logged and
        omitted, so one broken group does not take the API down.
    """
    modules: list[ModuleType] = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda entry: entry.name):
        if info.name.startswith("_"):
            continue
        full_name = f"{__name__}.{info.name}"
        try:
            modules.append(importlib.import_module(full_name))
        except Exception as exc:  # noqa: BLE001 - one broken group, not eleven
            logger.error(
                "api.route_module_failed",
                module=full_name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
    return modules


def _mounts_at_root(module: ModuleType) -> bool:
    """Return whether *module*'s routes live outside ``/api/v1``.

    Args:
        module: A route module.

    Returns:
        ``True`` for ``health`` and ``events``.
    """
    return bool(getattr(module, MOUNT_AT_ROOT_ATTRIBUTE, False))


def _router_of(module: ModuleType) -> APIRouter | None:
    """Return the router *module* declares, if it declares one.

    Args:
        module: A route module.

    Returns:
        The router, or ``None``.
    """
    candidate = getattr(module, ROUTER_ATTRIBUTE, None)
    return candidate if isinstance(candidate, APIRouter) else None


def build_api_router() -> APIRouter:
    """Aggregate every versioned route group into one router.

    Returns:
        A router carrying the twelve ``/api/v1`` groups, each under its declared prefix and
        tags. The two root-mounted groups are excluded — see :func:`root_routers`.
    """
    aggregate = APIRouter()
    mounted: list[str] = []
    for module in iter_route_modules():
        if _mounts_at_root(module):
            continue
        router = _router_of(module)
        if router is None:
            continue
        name = module.__name__.rsplit(".", 1)[-1]
        aggregate.include_router(
            router,
            prefix=getattr(module, PREFIX_ATTRIBUTE, ""),
            tags=list(getattr(module, TAGS_ATTRIBUTE, [name])),
        )
        mounted.append(name)
    logger.debug("api.routers_mounted", groups=mounted)
    return aggregate


def root_routers() -> list[APIRouter]:
    """Return the routers that mount at ``/`` rather than under ``/api/v1``.

    ``docs/CONTRACTS.md`` §14: ``/health``, ``/ready``, ``/metrics`` and ``/ws`` are
    deliberately unversioned. A process supervisor polling ``/health`` and a Prometheus
    scrape must not have to track an API version.

    Returns:
        The root-mounted routers, in module order.
    """
    return [
        router
        for module in iter_route_modules()
        if _mounts_at_root(module) and (router := _router_of(module)) is not None
    ]


#: The aggregated ``/api/v1`` router. Built at import so that the route table is fixed
#: before the first request, and so an import failure surfaces at startup.
api_router: APIRouter = build_api_router()
