"""The HTTP boundary — FastAPI routes, dependencies, error mapping and the event bus.

``docs/CONTRACTS.md`` §14 freezes every path in this package. The division of labour is
strict, and it is what keeps the API layer thin:

* **Routes parse, delegate and serialise.** A handler validates its inputs, calls one
  service method, and returns a schema. No handler owns business logic, and no handler
  builds a query that a service should own.
* **Services raise, handlers do not catch.** :class:`LookupError` means 404 and
  :class:`ValueError` means 400, mapped once in :mod:`app.api.errors`. A route with its own
  ``try/except`` around a service call is a route inventing a second error contract.
* **Work is enqueued by name.** Endpoints that start long operations call
  :func:`app.api.tasks.dispatch`, which sends a Celery task by its string name. This package
  never imports :mod:`app.workers` — the API process and the worker process are separately
  deployable, and an import would couple their lifecycles.

Nothing here is imported by the service layer, the workers, or the engines. The arrow points
one way.
"""

from __future__ import annotations

__all__: list[str] = []
