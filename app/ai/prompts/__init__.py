"""Prompt library — every instruction ApplicantOS gives a language model, as data.

Prompts are the highest-leverage, most-frequently-edited text in an AI product, and the
part a non-programmer is most likely to want to read. They therefore live as ``.md`` files
beside this module rather than as string literals buried in the code that calls them: they
are reviewable in a diff, readable on their own, and editable without touching Python.

Each prompt is one file, ``<name>.md``, loaded by name::

    from app.ai.prompts import load_prompt

    system = load_prompt("extract_facts.system")
    user = load_prompt("extract_facts.user", kind="resume", text=document_text)

Substitution is :meth:`string.Template.safe_substitute`, so placeholders are ``$name`` or
``${name}``. Two consequences, both deliberate:

* **Stray braces survive.** A prompt is full of JSON — schemas, worked examples, expected
  replies. With :meth:`str.format` every one of those braces would have to be doubled, and
  the first person to paste an example in would break the prompt at runtime. With
  ``$`` placeholders the JSON is written exactly as the model should produce it.
* **A missing variable is not fatal.** ``safe_substitute`` leaves an unknown placeholder in
  place rather than raising, so a caller that forgets one gets a slightly odd prompt
  instead of a crashed indexing run. :func:`missing_variables` is available for callers
  that want to check rather than hope.

File reads are memoised (:func:`clear_prompt_cache` drops the memo), so a prompt used on
every chunk of a large document is read from disk exactly once.

**Interoperation with :mod:`app.knowledge.extractors`.** That module renders its prompts
with :meth:`str.format` and probes this package for overrides by attribute name. The
module-level ``__getattr__`` below therefore exposes the four extraction prompts under the
names it looks for — :data:`EXTRACT_FACTS_SYSTEM`, ``EXTRACT_FACTS``,
``EXTRACT_ENTITIES_SYSTEM`` and ``EXTRACT_ENTITIES`` — translating ``$name`` placeholders
into ``{name}`` and escaping literal braces on the way out, via
:func:`as_format_template`. That is what makes the files in this directory the prompts the
fact extractor actually runs, rather than documentation of prompts it ignores.
"""

from __future__ import annotations

import re
from functools import cache, lru_cache
from pathlib import Path
from string import Template
from typing import Any, Final

import structlog

__all__ = [
    "PROMPT_DIR",
    "PROMPT_SUFFIX",
    "PromptError",
    "PromptNotFound",
    "as_format_template",
    "available_prompts",
    "clear_prompt_cache",
    "load_prompt",
    "missing_variables",
    "prompt_path",
    "prompt_text",
    "prompt_variables",
]

logger = structlog.get_logger(__name__)

#: Directory holding the prompt files: this package's own directory.
PROMPT_DIR: Final[Path] = Path(__file__).resolve().parent

#: Extension every prompt file carries. Markdown because prompts *are* markdown — headings
#: and fenced code blocks are how a model is shown structure.
PROMPT_SUFFIX: Final[str] = ".md"

#: Encoding prompt files are read with. Fixed rather than locale-dependent: a prompt
#: containing an em dash must load identically on every machine.
PROMPT_ENCODING: Final[str] = "utf-8"

#: A legal prompt name: letters, digits, and the three separators used by the naming
#: convention ``<topic>.<role>`` (``extract_facts.system``). Anchored, so no path
#: separator, no drive letter, no leading dot and no ``..`` can appear — a prompt name
#: often originates in configuration, and must never be able to read an arbitrary file.
_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

#: Rejected outright, whatever :data:`_NAME_PATTERN` says about the rest of the name.
_FORBIDDEN_IN_NAME: Final[tuple[str, ...]] = ("..", "/", "\\")

#: Module attribute names :mod:`app.knowledge.extractors` probes for, mapped to the prompt
#: that answers each. See this module's docstring.
_CONSTANT_ALIASES: Final[dict[str, str]] = {
    "EXTRACT_FACTS_SYSTEM": "extract_facts.system",
    "EXTRACT_FACTS": "extract_facts.user",
    "EXTRACT_ENTITIES_SYSTEM": "extract_entities.system",
    "EXTRACT_ENTITIES": "extract_entities.user",
    "SUMMARIZE_DOCUMENT_SYSTEM": "summarize_document.system",
    "SUMMARIZE_DOCUMENT": "summarize_document.user",
}

#: Alias suffix marking a system prompt. System prompts are never rendered with
#: :meth:`str.format` by their callers, so they are exposed verbatim — braces and all.
_SYSTEM_ALIAS_SUFFIX: Final[str] = "_SYSTEM"


class PromptError(Exception):
    """Base class for every prompt-library failure."""


class PromptNotFound(PromptError):
    """No prompt file exists for the requested name.

    Attributes:
        name: The name that was asked for.
    """

    def __init__(self, name: str, *, available: tuple[str, ...] = ()) -> None:
        """Report the miss, listing what does exist.

        Args:
            name: The requested prompt name.
            available: The names that are present, for the message.
        """
        known = ", ".join(available) if available else "none"
        super().__init__(f"no prompt named {name!r} in {PROMPT_DIR}; available: {known}")
        self.name = name


def _validate_name(name: str) -> str:
    """Return *name* after checking it can only ever name a file in :data:`PROMPT_DIR`.

    Args:
        name: The prompt name, without the ``.md`` suffix.

    Returns:
        The name, unchanged.

    Raises:
        PromptError: If the name is empty, contains a path separator or ``..``, or uses a
            character outside :data:`_NAME_PATTERN`.
    """
    if not isinstance(name, str) or not name.strip():
        raise PromptError("prompt name must be a non-empty string")
    candidate = name.strip()
    if any(token in candidate for token in _FORBIDDEN_IN_NAME):
        raise PromptError(f"illegal prompt name {name!r}")
    if not _NAME_PATTERN.fullmatch(candidate):
        raise PromptError(
            f"illegal prompt name {name!r}; expected letters, digits, '.', '_' or '-'"
        )
    return candidate


def prompt_path(name: str) -> Path:
    """Return the file backing the prompt called *name*.

    Args:
        name: The prompt name, without the ``.md`` suffix.

    Returns:
        The absolute path, whether or not the file exists.

    Raises:
        PromptError: If the name is not a legal prompt name.
    """
    return PROMPT_DIR / f"{_validate_name(name)}{PROMPT_SUFFIX}"


@cache
def prompt_text(name: str) -> str:
    """Return the raw text of the prompt called *name*.

    Memoised: a prompt used once per chunk of a large document is read from disk once.

    Args:
        name: The prompt name, without the ``.md`` suffix.

    Returns:
        The file's contents with trailing whitespace stripped. Trailing blank lines in a
        prompt are invisible in an editor and meaningful to a tokenizer, so they go.

    Raises:
        PromptError: If the name is not a legal prompt name.
        PromptNotFound: If no such file exists.
        OSError: If the file exists but cannot be read.
    """
    path = prompt_path(name)
    try:
        return path.read_text(encoding=PROMPT_ENCODING).rstrip()
    except FileNotFoundError as exc:
        raise PromptNotFound(name, available=tuple(available_prompts())) from exc


def load_prompt(name: str, **variables: Any) -> str:
    """Return the prompt called *name* with its placeholders filled in.

    Args:
        name: The prompt name, without the ``.md`` suffix — for example
            ``"extract_facts.system"``.
        **variables: Values for the prompt's ``$name`` / ``${name}`` placeholders.
            Non-string values are rendered with :func:`str`.

    Returns:
        The rendered prompt. Placeholders with no matching variable are left in place
        (:meth:`string.Template.safe_substitute`), and ``$$`` becomes a literal ``$``.

    Raises:
        PromptError: If the name is not a legal prompt name.
        PromptNotFound: If no such file exists.
    """
    rendered = Template(prompt_text(name)).safe_substitute(variables)
    unfilled = missing_variables(name, variables)
    if unfilled:
        logger.debug("prompts.placeholders_unfilled", prompt=name, variables=unfilled)
    return rendered


@cache
def prompt_variables(name: str) -> tuple[str, ...]:
    """Return the placeholder names the prompt called *name* declares.

    Args:
        name: The prompt name, without the ``.md`` suffix.

    Returns:
        The distinct placeholder names, in first-appearance order.

    Raises:
        PromptError: If the name is not a legal prompt name.
        PromptNotFound: If no such file exists.
    """
    found: list[str] = []
    for match in Template.pattern.finditer(prompt_text(name)):
        variable = match.group("named") or match.group("braced")
        if variable and variable not in found:
            found.append(variable)
    return tuple(found)


def missing_variables(name: str, variables: dict[str, Any]) -> tuple[str, ...]:
    """Return the placeholders of *name* that *variables* does not supply.

    Lets a caller assert that it is rendering a prompt completely, which
    ``safe_substitute`` deliberately will not do for it.

    Args:
        name: The prompt name, without the ``.md`` suffix.
        variables: The values the caller intends to substitute.

    Returns:
        The unfilled placeholder names, in first-appearance order.

    Raises:
        PromptError: If the name is not a legal prompt name.
        PromptNotFound: If no such file exists.
    """
    return tuple(variable for variable in prompt_variables(name) if variable not in variables)


def _escape_braces(text: str) -> str:
    """Double every brace in *text* so :meth:`str.format` reproduces it verbatim.

    Args:
        text: Literal text from a prompt.

    Returns:
        The text with ``{`` and ``}`` doubled.
    """
    return text.replace("{", "{{").replace("}", "}}")


@cache
def as_format_template(name: str) -> str:
    """Return the prompt called *name* rewritten for :meth:`str.format`.

    ``$variable`` and ``${variable}`` become ``{variable}``, ``$$`` becomes ``$``, and
    every literal brace — of which a prompt containing a JSON schema has many — is doubled
    so that ``format`` emits it unchanged. This is the bridge to callers that render with
    ``format`` rather than :class:`string.Template`, principally
    :mod:`app.knowledge.extractors`.

    Args:
        name: The prompt name, without the ``.md`` suffix.

    Returns:
        The rewritten template.

    Raises:
        PromptError: If the name is not a legal prompt name.
        PromptNotFound: If no such file exists.
    """
    text = prompt_text(name)
    parts: list[str] = []
    cursor = 0
    for match in Template.pattern.finditer(text):
        parts.append(_escape_braces(text[cursor : match.start()]))
        cursor = match.end()
        variable = match.group("named") or match.group("braced")
        if variable:
            parts.append("{" + variable + "}")
        elif match.group("escaped") is not None:
            parts.append("$")
        else:
            # An `invalid` match: a '$' that starts no placeholder. Keep it literal.
            parts.append(_escape_braces(match.group()))
    parts.append(_escape_braces(text[cursor:]))
    return "".join(parts)


def available_prompts() -> list[str]:
    """Return the names of every prompt in this package.

    Returns:
        The names without their ``.md`` suffix, sorted. Empty when the directory is
        missing, which cannot happen in a packaged install but can in an exotic zip
        importer — callers degrade rather than crash.
    """
    try:
        return sorted(path.stem for path in PROMPT_DIR.glob(f"*{PROMPT_SUFFIX}"))
    except OSError as exc:  # pragma: no cover - unreadable package directory
        logger.warning("prompts.listing_failed", directory=str(PROMPT_DIR), error=str(exc))
        return []


def clear_prompt_cache() -> None:
    """Drop the memoised file reads so edited prompts are picked up.

    For tests and for a development loop that edits a prompt while the API is running.
    """
    prompt_text.cache_clear()
    prompt_variables.cache_clear()
    as_format_template.cache_clear()


def __getattr__(name: str) -> str:
    """Expose the extraction prompts as module constants, on demand.

    :mod:`app.knowledge.extractors` looks for overrides by attribute name; this answers
    those lookups from the ``.md`` files, converting the two user prompts into
    :meth:`str.format` syntax because that is how their caller renders them. Lazy rather
    than computed at import time, so a missing or unreadable prompt file degrades this
    package to "provides no override" instead of breaking every import of :mod:`app.ai`.

    Args:
        name: The attribute being looked up.

    Returns:
        The prompt text.

    Raises:
        AttributeError: For any name that is not a known alias, and for a known alias
            whose file is missing or unreadable — :func:`getattr` with a default is how
            callers probe for these, and that only suppresses :class:`AttributeError`.
    """
    prompt = _CONSTANT_ALIASES.get(name)
    if prompt is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        if name.endswith(_SYSTEM_ALIAS_SUFFIX):
            return prompt_text(prompt)
        return as_format_template(prompt)
    except (PromptError, OSError) as exc:
        logger.warning("prompts.alias_unavailable", alias=name, prompt=prompt, error=str(exc))
        raise AttributeError(f"module {__name__!r} cannot provide {name!r}: {exc}") from exc


def __dir__() -> list[str]:
    """Return this module's attributes, including the lazily-provided aliases."""
    return sorted({*globals(), *_CONSTANT_ALIASES})
