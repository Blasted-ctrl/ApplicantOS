"""Built-in language-model clients — importing this package registers all four.

``docs/CONTRACTS.md`` §6 makes plugin registration a side effect of import: each concrete
client is decorated with :func:`app.plugins.plugin`, whose body runs when its module is
executed. :data:`app.plugins.loader.BUILTIN_PLUGIN_MODULES` therefore imports *this package*
and nothing deeper, and this module's job is to import the four concrete modules so those
decorators fire.

Golden rule #5 forbids importing a concrete model module from outside this package. The
re-exports below exist for type annotations, ``isinstance`` checks and tests — application
code resolves a client through :func:`app.ai.llm.get_llm` or
:meth:`app.plugins.registry.PluginRegistry.get`, never by constructing one of these classes::

    from app.ai.llm import get_llm

    model = get_llm("fast")

Import order is dependency order: :class:`~app.ai.models.openai.OpenAIModel` is the base of
:class:`~app.ai.models.local.LocalModel`, and :class:`~app.ai.models.null.NullModel` is the
offline fallback every other client degrades to. No provider SDK is imported here — each
client imports its SDK lazily on first use — so this package imports cleanly on a machine
with neither ``anthropic`` nor ``openai`` installed.
"""

from __future__ import annotations

from typing import Final

from app.ai.llm import ModelPlugin
from app.ai.models.anthropic import AnthropicModel
from app.ai.models.openai import OpenAIModel

# LocalModel subclasses OpenAIModel, so its module must be imported after it.
from app.ai.models.local import LocalModel  # isort: skip
from app.ai.models.null import NullModel  # isort: skip

__all__ = [
    "BUILTIN_MODEL_CLASSES",
    "AnthropicModel",
    "LocalModel",
    "NullModel",
    "OpenAIModel",
]

#: Every model plugin this package registers, in the order they were imported. Lets a test
#: assert that ``registry.names(PluginKind.MODEL)`` is complete without reaching into the
#: registry's internals.
BUILTIN_MODEL_CLASSES: Final[tuple[type[ModelPlugin], ...]] = (
    AnthropicModel,
    OpenAIModel,
    LocalModel,
    NullModel,
)
