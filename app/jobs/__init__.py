"""Job discovery — the ATS provider plugins and the door everything else uses to reach them.

Importing this package is what makes the providers exist. Each concrete module below runs a
``@plugin`` decorator at import time, which registers it with
:data:`app.plugins.registry.registry`; :func:`app.plugins.loader.load_all` imports this
package for exactly that reason, and nothing else in the codebase ever imports a provider
module directly (``docs/CONTRACTS.md`` §6, golden rule #5). To reach a provider, ask for it
by name::

    from app.jobs import get_provider, provider_for_url

    greenhouse = get_provider("greenhouse")
    owner = await provider_for_url("https://boards.greenhouse.io/acme/jobs/123")

Each provider import is guarded. A module that is absent, or whose optional dependency is
not installed, is logged and skipped rather than taking down the package — which keeps the
tree importable while it is being built out and keeps a slim install working when one
integration cannot load. What *is* present still registers, so discovery degrades by one
provider instead of stopping.

**Auto-apply posture, restated here because it is the most consequential fact about this
package** (``docs/CONTRACTS.md`` §9, golden rule #10):

===========  ==============  =========================================================
Provider     Auto-apply      Why
===========  ==============  =========================================================
greenhouse   yes             Public board API; a single-page form this system can fill.
lever        yes             Public postings API; a single-page form.
ashby        yes             Public job-board API; a single-page form.
workday      **no**          Account-gated, multi-step, configured per employer.
linkedin     **no**          Terms of service prohibit scraping and automated applying.
===========  ==============  =========================================================

Workday and LinkedIn raise :class:`~app.jobs.base.UnsupportedFlowError` from
:meth:`~app.jobs.base.ATSProvider.apply`, which the pipeline converts into a manual-review
item carrying the URL. Each module's docstring states its own posture in full.
"""

from __future__ import annotations

import importlib
from typing import Final

import structlog

from app.jobs.base import (
    ApplyContext,
    ApplyResult,
    ATSProvider,
    FormField,
    JobPostingDTO,
    PostingUnavailableError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    RawPosting,
    SearchQuery,
    UnsupportedFlowError,
    UserProfileDTO,
)
from app.jobs.registry import (
    all_providers,
    auto_apply_provider_names,
    available_provider_names,
    describe_providers,
    get_provider,
    get_provider_class,
    provider_class_for_url,
    provider_for_url,
    resolve_providers,
)

__all__ = [
    "PROVIDER_MODULES",
    "ATSProvider",
    "ApplyContext",
    "ApplyResult",
    "FormField",
    "JobPostingDTO",
    "PostingUnavailableError",
    "ProviderAuthError",
    "ProviderError",
    "ProviderRateLimitError",
    "RawPosting",
    "SearchQuery",
    "UnsupportedFlowError",
    "UserProfileDTO",
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

#: The concrete provider modules, imported for their registration side effect. Ordered so
#: that the three submission-capable integrations load first: if an environment can only load
#: some of them, the ones that can complete an application end to end are the ones worth
#: having.
PROVIDER_MODULES: Final[tuple[str, ...]] = (
    "app.jobs.greenhouse",
    "app.jobs.lever",
    "app.jobs.ashby",
    "app.jobs.workday",
    "app.jobs.linkedin",
)


def _register_builtin_providers() -> None:
    """Import every built-in provider module so its ``@plugin`` decorator runs.

    Each import is guarded individually. An :class:`ImportError` naming the provider module
    itself means the module is not present — expected while the tree is being built — and is
    logged at debug. An :class:`ImportError` naming something else means an installed
    provider has a missing dependency, which is a real problem an operator needs to see and
    is logged at warning. Either way the remaining providers still register: one broken
    integration must never mean zero discovery.
    """
    for module_name in PROVIDER_MODULES:
        try:
            importlib.import_module(module_name)
        except ImportError as exc:
            missing = getattr(exc, "name", None)
            if missing and (module_name == missing or module_name.startswith(f"{missing}.")):
                logger.debug("jobs.provider_module_absent", module=module_name)
            else:
                logger.warning(
                    "jobs.provider_dependency_missing",
                    module=module_name,
                    missing=missing,
                    error=str(exc),
                )
        except Exception as exc:  # noqa: BLE001 - one bad module must not empty the registry
            logger.warning(
                "jobs.provider_import_failed",
                module=module_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )


_register_builtin_providers()
