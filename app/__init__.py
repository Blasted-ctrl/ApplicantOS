"""ApplicantOS — an AI-powered desktop application that automates job applications.

This package is the Python backend: a FastAPI application, a Celery worker fleet, a
knowledge engine that models the user's experience as a graph of facts, a deterministic
job-scoring engine, an ATS provider plugin system, and a Playwright-driven autofill layer.

Sub-package map (see ``docs/CONTRACTS.md`` §0 for the authoritative layout):

``app.config``
    Settings (pydantic-settings), structlog configuration, the default scoring rule pack.
``app.database``
    SQLAlchemy 2.0 declarative base, portable column types, async engine/session plumbing.
``app.models``
    ORM models, declarative mixins, and every cross-module enum.
``app.schemas``
    Pydantic request/response contracts shared with the desktop client.
``app.cache`` / ``app.plugins`` / ``app.storage``
    Cross-cutting infrastructure: caching backends, the plugin registry, blob storage.
``app.knowledge`` / ``app.ai`` / ``app.documents`` / ``app.browser`` / ``app.jobs``
    The domain engines: knowledge extraction, LLM access, document rendering, browser
    automation, and ATS providers.
``app.services`` / ``app.api`` / ``app.workers`` / ``app.observability``
    Orchestration, HTTP/WebSocket surface, background tasks, metrics and middleware.

Nothing in this module imports sub-packages: importing ``app`` must stay free of side
effects so that tooling (Alembic, Celery, tests) can import leaf modules cheaply.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
