"""Anthropic Claude client (``model`` plugin ``anthropic``).

The default reasoning provider. Registered under ``PluginKind.MODEL`` and reachable only
through :func:`app.ai.llm.get_llm` or the plugin registry — golden rule #5 forbids importing
this module from outside :mod:`app.ai.models`.

Two details of the Messages API are easy to get wrong and are handled explicitly here:

* **The system prompt is a top-level parameter**, not a message with ``role="system"``.
  Passing it as a message is a validation error, so the caller's system prompt goes to
  ``system=`` and ``messages`` contains only the user turn.
* **Structured output is expressed as a forced tool call.** When ``json_schema`` is
  supplied, the schema becomes the ``input_schema`` of a single synthetic tool and
  ``tool_choice`` forces the model to call it. The tool's ``input`` is then a JSON value
  the API itself validated against the schema, which is far more reliable than asking for
  JSON in prose. The ``tool_use`` block is serialised back to text so that callers — and
  :meth:`~app.ai.llm.ModelPlugin.complete_json` — see the same string shape they would get
  from any other provider.

The SDK is imported lazily inside :meth:`AnthropicModel._client`, so ApplicantOS imports
and runs with the ``anthropic`` package absent; a missing key or a missing SDK simply routes
:func:`~app.ai.llm.get_llm` to the offline null model.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar, Final

import structlog

from app.ai.llm import GuardedModelPlugin, LLMError, LLMResponse
from app.models.enums import PluginKind
from app.plugins import PluginMeta, plugin

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.config.settings import Settings

__all__ = ["AnthropicModel"]

logger = structlog.get_logger(__name__)

#: Name of the synthetic tool used to force schema-conforming output.
STRUCTURED_OUTPUT_TOOL: Final[str] = "emit_result"

#: Description attached to that tool. Claude reads tool descriptions, so this is not filler.
STRUCTURED_OUTPUT_TOOL_DESCRIPTION: Final[str] = (
    "Return the final answer as structured data. Call this tool exactly once, with every "
    "required property populated from the material you were given. Do not call it with "
    "placeholder or invented values."
)

#: Content-block types the Messages API returns that this client knows how to read.
TEXT_BLOCK_TYPE: Final[str] = "text"
TOOL_USE_BLOCK_TYPE: Final[str] = "tool_use"

#: Retries are handled by :class:`~app.ai.llm.GuardedModelPlugin`, so the SDK's own retry
#: loop is switched off — two nested backoff policies would multiply, not compose.
SDK_MAX_RETRIES: Final[int] = 0


@plugin
class AnthropicModel(GuardedModelPlugin):
    """Claude via the official ``anthropic`` async SDK.

    Attributes:
        tier: Inherited from :class:`~app.ai.llm.ModelPlugin`; selects between
            ``settings.llm_model_reasoning`` and ``settings.llm_model_fast``.
    """

    meta: ClassVar[PluginMeta] = PluginMeta(
        kind=PluginKind.MODEL,
        name="anthropic",
        display_name="Anthropic Claude",
        description="Claude models via the official Anthropic Messages API.",
        author="ApplicantOS",
        capabilities=frozenset({"completion", "json_schema", "tool_use", "streaming_capable"}),
    )

    def __init__(self, settings: Settings, **kw: Any) -> None:
        """Create the client without touching the network or importing the SDK.

        Args:
            settings: Application settings supplying the API key, timeout and model names.
            **kw: Extra options; ``tier`` selects the model tier.
        """
        super().__init__(settings, **kw)
        self._sdk_client: Any | None = None

    # -- client ---------------------------------------------------------------------------

    def _client(self) -> Any:
        """Return the shared ``AsyncAnthropic`` client, constructing it on first use.

        Returns:
            The SDK client.

        Raises:
            LLMError: If the ``anthropic`` package is not installed or no API key is
                configured. Both are configuration problems, not transient ones.
        """
        if self._sdk_client is not None:
            return self._sdk_client

        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - exercised only without the SDK
            raise LLMError(
                "the 'anthropic' package is not installed; "
                "install it or set LLM_PROVIDER=null to run offline",
                model=self.model,
            ) from exc

        api_key = (self.settings.anthropic_api_key or "").strip()
        if not api_key:
            raise LLMError(
                "ANTHROPIC_API_KEY is not set; set it or set LLM_PROVIDER=null",
                model=self.model,
            )

        self._sdk_client = anthropic.AsyncAnthropic(
            api_key=api_key,
            timeout=float(self.settings.llm_timeout_seconds),
            max_retries=SDK_MAX_RETRIES,
        )
        return self._sdk_client

    # -- completion -------------------------------------------------------------------------

    async def _invoke(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float,
        json_schema: dict[str, Any] | None,
    ) -> LLMResponse:
        """Perform one Messages API call.

        Args:
            model: Resolved model identifier, e.g. ``claude-sonnet-4-5``.
            system: The system prompt — sent as the top-level ``system`` parameter.
            prompt: The user message.
            max_tokens: Maximum completion length.
            temperature: Sampling temperature.
            json_schema: When supplied, forces a call to the synthetic
                :data:`STRUCTURED_OUTPUT_TOOL` whose ``input_schema`` is this schema.

        Returns:
            The reply text (the tool input, serialised, for a structured call) and the usage
            the API reported.
        """
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        # Anthropic takes the system prompt at the top level, never as a message role.
        if system.strip():
            request["system"] = system
        if json_schema is not None:
            request["tools"] = [
                {
                    "name": STRUCTURED_OUTPUT_TOOL,
                    "description": STRUCTURED_OUTPUT_TOOL_DESCRIPTION,
                    "input_schema": json_schema,
                }
            ]
            request["tool_choice"] = {"type": "tool", "name": STRUCTURED_OUTPUT_TOOL}

        response = await self._client().messages.create(**request)
        text = self._extract_text(response)
        usage = getattr(response, "usage", None)
        return LLMResponse(
            text=text,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            model=str(getattr(response, "model", model)),
            cached=False,
            raw=response,
        )

    @staticmethod
    def _extract_text(response: Any) -> str:
        """Flatten a Messages API response into a single string.

        A ``tool_use`` block carries the structured answer as a Python object; it is
        serialised to JSON so every provider in this package returns text of the same shape.
        Text blocks are concatenated in order.

        Args:
            response: The SDK response object.

        Returns:
            The reply as text — the JSON document for a structured call, otherwise the
            concatenated text blocks.
        """
        parts: list[str] = []
        for block in getattr(response, "content", None) or ():
            block_type = getattr(block, "type", None)
            if block_type == TOOL_USE_BLOCK_TYPE:
                payload = getattr(block, "input", None)
                # A forced tool call is the answer; return it alone rather than mixed with
                # any preamble text the model also produced.
                return json.dumps(payload, ensure_ascii=False)
            if block_type == TEXT_BLOCK_TYPE:
                parts.append(str(getattr(block, "text", "")))
        return "".join(parts)

    # -- diagnostics --------------------------------------------------------------------------

    async def healthcheck(self) -> bool:
        """Report whether this client is configured well enough to be used.

        Deliberately offline: ``GET /ready`` probes every plugin, and a network round trip
        per readiness check would be both slow and billable. Configuration problems — no
        SDK, no API key — are exactly what this needs to surface.

        Returns:
            ``True`` when the SDK is importable and an API key is present.
        """
        try:
            self._client()
        except LLMError as exc:
            logger.debug("anthropic.healthcheck_failed", error=str(exc))
            return False
        return True
