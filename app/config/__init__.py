"""Configuration package — settings, logging, and the default scoring rule pack.

Import the process-wide configuration from here rather than reaching into the submodules::

    from app.config import settings, get_logger

    logger = get_logger(__name__)

Exports:
    ``Settings`` / ``settings`` / ``get_settings``
        Typed runtime configuration (``docs/CONTRACTS.md`` §1).
    ``configure_logging`` / ``get_logger`` / ``bind_context`` / ``clear_context``
        The structlog pipeline and its ambient-context helpers.
    ``REDACTED`` / ``SENSITIVE_KEY_PATTERNS``
        The secret-redaction vocabulary, exported so tests and log sinks can assert on it.
    ``DEFAULT_SCORING_RULES_PATH``
        Filesystem location of the packaged ``scoring_rules.yaml`` rule pack.

Import order matters: ``settings`` is imported before ``logging`` because the logging
pipeline is configured *from* settings and must never be a dependency of it.
"""

from __future__ import annotations

from app.config.logging import (
    REDACTED,
    SENSITIVE_KEY_PATTERNS,
    bind_context,
    clear_context,
    configure_logging,
    get_logger,
)
from app.config.settings import (
    DEFAULT_SCORING_RULES_PATH,
    Settings,
    get_settings,
    settings,
)

__all__ = [
    "DEFAULT_SCORING_RULES_PATH",
    "REDACTED",
    "SENSITIVE_KEY_PATTERNS",
    "Settings",
    "bind_context",
    "clear_context",
    "configure_logging",
    "get_logger",
    "get_settings",
    "settings",
]
