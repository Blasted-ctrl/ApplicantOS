"""AI layer — every model call, every embedding, and every prompt in ApplicantOS.

``docs/CONTRACTS.md`` §10 splits this package four ways, and importing it gives you all
four::

    from app.ai import get_llm, get_embedder, load_prompt

    model = get_llm("reasoning")
    reply = await model.complete_json(
        system=load_prompt("extract_facts.system"),
        prompt=load_prompt("extract_facts.user", kind="resume", text=document),
        schema=FACTS_SCHEMA,
    )
    vectors = await embed_texts([chunk.text for chunk in chunks])

:mod:`app.ai.llm`
    The one door every completion goes through: :class:`~app.ai.llm.ModelPlugin`,
    deterministic caching, the daily :class:`~app.ai.llm.TokenBudget`, retries with
    backoff, and defensive JSON parsing. :func:`~app.ai.llm.get_llm` resolves the client.
:mod:`app.ai.models`
    The four concrete clients. Importing the package registers them as ``model`` plugins;
    nothing outside it may import a client module directly (golden rule #5).
:mod:`app.ai.embeddings`
    Vectors: :func:`~app.ai.embeddings.get_embedder`,
    :func:`~app.ai.embeddings.embed_texts` (the cached batching path everything should
    use), and the similarity helpers :func:`~app.ai.embeddings.cosine` and
    :func:`~app.ai.embeddings.top_k`.
:mod:`app.ai.prompts`
    Prompts as reviewable ``.md`` files, rendered by
    :func:`~app.ai.prompts.load_prompt`.
:mod:`app.ai.untrusted`
    The ``docs/CONTRACTS.md`` §10b chokepoint. Every externally-sourced string passes
    through :func:`~app.ai.untrusted.sanitize_external_text` before it can reach a prompt,
    and :func:`~app.ai.untrusted.contains_pii` reports what personal data a string carries
    so a caller can decide to leave it out.
:mod:`app.ai.memory_prompt`
    The other direction: what the user has already taught the system, screened for personal
    data and rendered into a **system** prompt by
    :func:`~app.ai.memory_prompt.build_memory_block`, and credited by
    :func:`~app.ai.memory_prompt.reinforce_used` once the work it shaped is known not to have
    needed a human. Not re-exported here — like :mod:`app.ai.field_answer` and
    :mod:`app.ai.resume_engine`, it is imported by the call site that needs it.

**Everything degrades to an offline path.** With no API keys and no provider SDKs
installed, :func:`~app.ai.llm.get_llm` returns the deterministic
:class:`~app.ai.models.null.NullModel` and
:func:`~app.ai.embeddings.get_embedder` returns
:class:`~app.ai.embeddings.HashingEmbedder`, each after exactly one warning. The whole
product runs end to end from there — that is a hard requirement of this codebase, not a
degraded mode to apologise for.

Import order below is dependency order: :mod:`~app.ai.llm` first (it depends only on the
cache and the plugin registry), then :mod:`~app.ai.embeddings` and
:mod:`~app.ai.prompts`, then :mod:`~app.ai.models`, whose clients import all three.
"""

from __future__ import annotations

from app.ai.llm import (
    GuardedModelPlugin,
    LLMError,
    LLMParseError,
    LLMRateLimited,
    LLMResponse,
    LLMTimeout,
    ModelPlugin,
    ModelTier,
    TokenBudget,
    TokenBudgetExceeded,
    get_budget,
    get_llm,
    reset_budget,
    reset_llm_cache,
)

from app.ai.embeddings import (  # isort: skip
    BaseEmbedder,
    Embedder,
    EmbeddingDimensionMismatch,
    EmbeddingError,
    HashingEmbedder,
    LocalEmbedder,
    OpenAIEmbedder,
    cosine,
    embed_texts,
    get_embedder,
    reset_embedder_cache,
    tokenize,
    top_k,
)
from app.ai.prompts import (  # isort: skip
    PromptError,
    PromptNotFound,
    available_prompts,
    load_prompt,
)
from app.ai.untrusted import (  # isort: skip
    InjectionRisk,
    InjectionVerdict,
    PiiCategory,
    PiiVerdict,
    UntrustedContentError,
    contact_allowlist,
    contains_pii,
    sanitize_external_text,
    sanitize_or_raise,
)

# Imported last and for its registration side effect: executing the four client modules is
# what puts them in the plugin registry. Re-exported so `app.ai.NullModel` resolves, but
# application code must still go through `get_llm` or the registry.
from app.ai.models import (  # isort: skip
    AnthropicModel,
    LocalModel,
    NullModel,
    OpenAIModel,
)

__all__ = [
    "AnthropicModel",
    "BaseEmbedder",
    "Embedder",
    "EmbeddingDimensionMismatch",
    "EmbeddingError",
    "GuardedModelPlugin",
    "HashingEmbedder",
    "InjectionRisk",
    "InjectionVerdict",
    "LLMError",
    "LLMParseError",
    "LLMRateLimited",
    "LLMResponse",
    "LLMTimeout",
    "LocalEmbedder",
    "LocalModel",
    "ModelPlugin",
    "ModelTier",
    "NullModel",
    "OpenAIEmbedder",
    "OpenAIModel",
    "PiiCategory",
    "PiiVerdict",
    "PromptError",
    "PromptNotFound",
    "TokenBudget",
    "TokenBudgetExceeded",
    "UntrustedContentError",
    "available_prompts",
    "contact_allowlist",
    "contains_pii",
    "cosine",
    "embed_texts",
    "get_budget",
    "get_embedder",
    "get_llm",
    "load_prompt",
    "reset_budget",
    "reset_embedder_cache",
    "reset_llm_cache",
    "sanitize_external_text",
    "sanitize_or_raise",
    "tokenize",
    "top_k",
]
