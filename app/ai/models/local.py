"""Local model client (``model`` plugin ``local``).

Points the OpenAI-compatible chat-completions dialect at a server running on the user's own
machine: Ollama (``http://localhost:11434/v1``, the default), LM Studio, vLLM, llama.cpp's
server, or anything else that speaks the same API. Nothing leaves the machine, no key is
needed, and no token is billed — which makes this the privacy-preserving middle ground
between a hosted frontier model and the deterministic :class:`~app.ai.models.null.NullModel`.

Only three things differ from :class:`~app.ai.models.openai.OpenAIModel`, and each is one
overridden hook: the base URL comes from ``settings.local_llm_base_url``, the credential is
a placeholder because local servers authenticate nothing, and both tiers resolve to
``settings.llm_model_local`` — a local deployment normally serves exactly one model, so
asking for the "fast" tier must not silently request a model that is not loaded.
"""

from __future__ import annotations

from typing import ClassVar, Final

import structlog

from app.ai.models.openai import UNUSED_API_KEY, OpenAIModel
from app.models.enums import PluginKind
from app.plugins import PluginMeta, plugin

__all__ = ["LocalModel"]

logger = structlog.get_logger(__name__)

#: Path suffix an OpenAI-compatible base URL is expected to carry. Ollama and LM Studio both
#: expose their compatibility layer under ``/v1``; a base URL missing it is a common
#: misconfiguration worth one warning at construction time.
OPENAI_COMPATIBLE_PATH_SUFFIX: Final[str] = "/v1"


@plugin
class LocalModel(OpenAIModel):
    """A local OpenAI-compatible server (Ollama, LM Studio, vLLM, llama.cpp).

    Attributes:
        tier: Inherited from :class:`~app.ai.llm.ModelPlugin`, but ignored when resolving
            the model name — see :meth:`model_for_tier`.
    """

    meta: ClassVar[PluginMeta] = PluginMeta(
        kind=PluginKind.MODEL,
        name="local",
        display_name="Local model",
        description=(
            "A local OpenAI-compatible server such as Ollama, LM Studio or vLLM. "
            "No API key, no network egress, no token cost."
        ),
        author="ApplicantOS",
        capabilities=frozenset({"completion", "json_object", "offline", "private"}),
    )

    # -- configuration hooks ---------------------------------------------------------------

    def _api_key(self) -> str:
        """Return the placeholder credential local servers ignore.

        Returns:
            :data:`~app.ai.models.openai.UNUSED_API_KEY`. The SDK refuses to construct
            without *some* key, and a local server never checks it.
        """
        return UNUSED_API_KEY

    def _base_url(self) -> str:
        """Return the configured local endpoint.

        Returns:
            ``settings.local_llm_base_url``, trailing slash removed. A URL that does not end
            in ``/v1`` is served anyway but logged once, because it is almost always a
            misconfiguration.
        """
        base_url = str(self.settings.local_llm_base_url).rstrip("/")
        if not base_url.endswith(OPENAI_COMPATIBLE_PATH_SUFFIX):
            logger.warning(
                "local_model.unexpected_base_url",
                base_url=base_url,
                expected_suffix=OPENAI_COMPATIBLE_PATH_SUFFIX,
                detail="OpenAI-compatible servers usually expose their API under /v1",
            )
        return base_url

    def model_for_tier(self, tier: str) -> str:
        """Return the single local model, whichever tier was asked for.

        A local deployment serves the models it has pulled. Honouring the reasoning/fast
        split here would request ``claude-sonnet-4-5`` from Ollama and fail; the tier is
        therefore collapsed deliberately.

        Args:
            tier: The requested tier; accepted and ignored.

        Returns:
            ``settings.llm_model_local``.
        """
        return self.settings.llm_model_local

    # -- diagnostics -----------------------------------------------------------------------

    async def healthcheck(self) -> bool:
        """Report whether the local client can be constructed.

        Like every other model plugin this stays offline — it does not probe the local
        server, because ``GET /ready`` must answer promptly and a server that is merely
        asleep is not a configuration error.

        Returns:
            ``True`` when the ``openai`` SDK is importable and a base URL is configured.
        """
        if not str(self.settings.local_llm_base_url).strip():
            logger.debug("local_model.healthcheck_failed", error="LOCAL_LLM_BASE_URL is empty")
            return False
        return await super().healthcheck()
