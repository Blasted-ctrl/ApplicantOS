"""OpenAI client (``model`` plugin ``openai``).

Registered under ``PluginKind.MODEL`` and reachable only through
:func:`app.ai.llm.get_llm` or the plugin registry (golden rule #5).

Structured output uses ``response_format={"type": "json_object"}``, the JSON mode supported
by every chat-completions model that has it. The API requires the string ``json`` to appear
somewhere in the conversation when that mode is active — normally satisfied by the schema
block :meth:`~app.ai.llm.ModelPlugin.complete_json` appends to the system prompt, and
enforced here regardless so a caller passing ``json_schema`` by hand cannot trip the
validator.

This class is also the base of :class:`~app.ai.models.local.LocalModel`: every
OpenAI-compatible server (Ollama, LM Studio, vLLM, llama.cpp) speaks the same
chat-completions dialect, so the subclass overrides only credentials, base URL, and model
naming. The three hooks that exist for it — :meth:`OpenAIModel._api_key`,
:meth:`OpenAIModel._base_url` and :meth:`OpenAIModel.model_for_tier` — are the whole
difference between a hosted and a local deployment.

The SDK is imported lazily, so ApplicantOS imports and runs with the ``openai`` package
absent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Final

import structlog

from app.ai.llm import GuardedModelPlugin, LLMError, LLMResponse
from app.models.enums import PluginKind
from app.plugins import PluginMeta, plugin

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.config.settings import Settings

__all__ = ["OpenAIModel"]

logger = structlog.get_logger(__name__)

#: ``response_format`` value that constrains a reply to a JSON object.
JSON_OBJECT_RESPONSE_FORMAT: Final[dict[str, str]] = {"type": "json_object"}

#: The API rejects JSON mode unless this token appears in the conversation.
JSON_MODE_KEYWORD: Final[str] = "json"

#: Appended to the system prompt when JSON mode is requested but nothing in the prompts
#: mentions JSON, which the API treats as a validation error.
JSON_MODE_REMINDER: Final[str] = "Respond with a single JSON object."

#: Retries live in :class:`~app.ai.llm.GuardedModelPlugin`; the SDK's own loop is disabled
#: so the two backoff policies cannot multiply.
SDK_MAX_RETRIES: Final[int] = 0

#: Request field naming the completion-length cap on the classic chat-completions API.
MAX_TOKENS_FIELD: Final[str] = "max_tokens"

#: Its replacement on the newer reasoning models, which reject the classic name outright.
MAX_COMPLETION_TOKENS_FIELD: Final[str] = "max_completion_tokens"

#: Substrings identifying the "use max_completion_tokens instead" rejection, which is a
#: 400 the SDK raises rather than something detectable up front from the model name.
MAX_TOKENS_REJECTION_MARKERS: Final[tuple[str, ...]] = (
    "max_completion_tokens",
    "unsupported_parameter",
    "unsupported parameter",
)

#: Placeholder credential for OpenAI-compatible servers that authenticate nothing. The SDK
#: refuses to construct without *some* key, so one is always supplied.
UNUSED_API_KEY: Final[str] = "not-needed"


@plugin
class OpenAIModel(GuardedModelPlugin):
    """GPT models via the official ``openai`` async SDK.

    Attributes:
        tier: Inherited from :class:`~app.ai.llm.ModelPlugin`; selects between
            ``settings.llm_model_reasoning`` and ``settings.llm_model_fast``.
    """

    meta: ClassVar[PluginMeta] = PluginMeta(
        kind=PluginKind.MODEL,
        name="openai",
        display_name="OpenAI",
        description="OpenAI chat-completions models with JSON mode.",
        author="ApplicantOS",
        capabilities=frozenset({"completion", "json_object", "streaming_capable"}),
    )

    def __init__(self, settings: Settings, **kw: Any) -> None:
        """Create the client without touching the network or importing the SDK.

        Args:
            settings: Application settings supplying credentials, timeout and model names.
            **kw: Extra options; ``tier`` selects the model tier.
        """
        super().__init__(settings, **kw)
        self._sdk_client: Any | None = None
        #: Set once the API tells us this deployment wants ``max_completion_tokens``.
        self._uses_max_completion_tokens = False

    # -- configuration hooks (overridden by LocalModel) ------------------------------------

    def _api_key(self) -> str:
        """Return the API key to authenticate with.

        Returns:
            ``settings.openai_api_key``, stripped.

        Raises:
            LLMError: When no key is configured.
        """
        api_key = (self.settings.openai_api_key or "").strip()
        if not api_key:
            raise LLMError(
                "OPENAI_API_KEY is not set; set it or set LLM_PROVIDER=null",
                model=self.model,
            )
        return api_key

    def _base_url(self) -> str | None:
        """Return the API base URL, or ``None`` for the SDK default (``api.openai.com``)."""
        return None

    # -- client -------------------------------------------------------------------------------

    def _client(self) -> Any:
        """Return the shared ``AsyncOpenAI`` client, constructing it on first use.

        Returns:
            The SDK client.

        Raises:
            LLMError: If the ``openai`` package is not installed, or credentials are
                missing. Both are configuration problems, not transient ones.
        """
        if self._sdk_client is not None:
            return self._sdk_client

        try:
            import openai
        except ImportError as exc:  # pragma: no cover - exercised only without the SDK
            raise LLMError(
                "the 'openai' package is not installed; "
                "install it or set LLM_PROVIDER=null to run offline",
                model=self.model,
            ) from exc

        options: dict[str, Any] = {
            "api_key": self._api_key(),
            "timeout": float(self.settings.llm_timeout_seconds),
            "max_retries": SDK_MAX_RETRIES,
        }
        base_url = self._base_url()
        if base_url:
            options["base_url"] = base_url

        self._sdk_client = openai.AsyncOpenAI(**options)
        return self._sdk_client

    # -- completion -----------------------------------------------------------------------------

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
        """Perform one chat-completions call.

        Args:
            model: Resolved model identifier.
            system: The system prompt, sent as the ``system`` message.
            prompt: The user message.
            max_tokens: Maximum completion length.
            temperature: Sampling temperature.
            json_schema: When supplied, switches the reply into JSON mode. The schema itself
                travels in the system prompt (added by
                :meth:`~app.ai.llm.ModelPlugin.complete_json`); JSON mode guarantees only
                that the reply is *a* JSON object.

        Returns:
            The reply text and the usage the API reported.
        """
        wants_json = json_schema is not None
        messages: list[dict[str, str]] = []
        system_text = self._system_for_json_mode(system, prompt) if wants_json else system
        if system_text.strip():
            messages.append({"role": "system", "content": system_text})
        messages.append({"role": "user", "content": prompt})

        request: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if wants_json:
            request["response_format"] = dict(JSON_OBJECT_RESPONSE_FORMAT)

        response = await self._create_with_token_limit(request, max_tokens)
        return LLMResponse(
            text=self._extract_text(response),
            input_tokens=self._usage_of(response, ("prompt_tokens", "input_tokens")),
            output_tokens=self._usage_of(response, ("completion_tokens", "output_tokens")),
            model=str(getattr(response, "model", model)),
            cached=False,
            raw=response,
        )

    async def _create_with_token_limit(self, request: dict[str, Any], max_tokens: int) -> Any:
        """Send *request*, naming the completion cap whichever way this deployment accepts.

        The classic ``max_tokens`` field is rejected by the newer reasoning models, which
        want ``max_completion_tokens`` instead. There is no reliable way to tell from the
        model name — deployments and aliases vary — so the first rejection is caught and the
        preference is remembered on the instance for every subsequent call.

        Args:
            request: The fully-formed request, without any token-limit field.
            max_tokens: Maximum completion length.

        Returns:
            The SDK response object.

        Raises:
            Exception: Whatever the SDK raised, for :func:`app.ai.llm.classify_error` to
                normalise — except the specific rejection this method retries.
        """
        client = self._client()
        field = (
            MAX_COMPLETION_TOKENS_FIELD
            if self._uses_max_completion_tokens
            else MAX_TOKENS_FIELD
        )
        try:
            return await client.chat.completions.create(**request, **{field: max_tokens})
        except Exception as exc:  # noqa: BLE001 - inspected, then re-raised or retried
            if field == MAX_COMPLETION_TOKENS_FIELD or not self._rejects_max_tokens(exc):
                raise
            self._uses_max_completion_tokens = True
            logger.info("openai.switched_token_limit_field", model=request.get("model"))
        return await client.chat.completions.create(
            **request, **{MAX_COMPLETION_TOKENS_FIELD: max_tokens}
        )

    @staticmethod
    def _rejects_max_tokens(exc: BaseException) -> bool:
        """Return whether *exc* is the API complaining about ``max_tokens`` specifically.

        Args:
            exc: The exception the SDK raised.

        Returns:
            ``True`` when the message names ``max_completion_tokens`` or reports an
            unsupported parameter, which is how this rejection is worded.
        """
        message = str(exc).lower()
        return MAX_TOKENS_FIELD in message and any(
            marker in message for marker in MAX_TOKENS_REJECTION_MARKERS
        )

    @staticmethod
    def _system_for_json_mode(system: str, prompt: str) -> str:
        """Guarantee the conversation mentions JSON, as JSON mode requires.

        Args:
            system: The caller's system prompt.
            prompt: The user message.

        Returns:
            The system prompt, with a one-line reminder appended only when neither prompt
            already contains the word "json".
        """
        if JSON_MODE_KEYWORD in f"{system}\n{prompt}".lower():
            return system
        return f"{system.rstrip()}\n\n{JSON_MODE_REMINDER}" if system.strip() else JSON_MODE_REMINDER

    @staticmethod
    def _extract_text(response: Any) -> str:
        """Return the assistant message content from a chat-completions response.

        Args:
            response: The SDK response object.

        Returns:
            The first choice's message content, or an empty string when the model returned
            no content (a pure refusal, or a completion truncated at zero tokens).
        """
        choices = getattr(response, "choices", None) or ()
        for choice in choices:
            message = getattr(choice, "message", None)
            content = getattr(message, "content", None)
            if content:
                return str(content)
        return ""

    @staticmethod
    def _usage_of(response: Any, field_names: tuple[str, ...]) -> int:
        """Read a token count from a response's ``usage`` block.

        Args:
            response: The SDK response object.
            field_names: Candidate attribute names, in priority order — the chat API reports
                ``prompt_tokens``/``completion_tokens`` while some compatible servers copy
                the responses API's ``input_tokens``/``output_tokens``.

        Returns:
            The token count, or ``0`` when the server reported no usage at all (common for
            self-hosted OpenAI-compatible endpoints).
        """
        usage = getattr(response, "usage", None)
        if usage is None:
            return 0
        for name in field_names:
            value = getattr(usage, name, None)
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    # -- diagnostics ---------------------------------------------------------------------------

    async def healthcheck(self) -> bool:
        """Report whether this client is configured well enough to be used.

        Offline by design: ``GET /ready`` probes every plugin and must not make a billable
        network call per probe.

        Returns:
            ``True`` when the SDK is importable and credentials are present.
        """
        try:
            self._client()
        except LLMError as exc:
            logger.debug("openai.healthcheck_failed", error=str(exc))
            return False
        return True
