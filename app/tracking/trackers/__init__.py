"""Concrete status trackers (``docs/CONTRACTS.md`` §17).

One module per outcome channel, each registering itself with :data:`app.plugins.registry`
through the ``@plugin`` decorator. Nothing outside this package imports a module from it —
golden rule #5 — so a caller reaches a tracker by name::

    from app.models.enums import PluginKind
    from app.plugins.registry import registry

    tracker = registry.get(PluginKind.TRACKER, "email")

Importing :mod:`app.tracking` is what causes these modules to be imported, and each import is
individually guarded there: a tracker whose optional dependency is missing leaves the
application running with one fewer source rather than not running at all.
"""

from __future__ import annotations

__all__: list[str] = []
