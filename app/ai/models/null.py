"""The offline model (``model`` plugin ``null``) — how ApplicantOS runs with zero API keys.

``docs/CONTRACTS.md`` §10 makes this load-bearing: *"the entire pipeline must run end-to-end
with zero API keys."* Every fallback path in :func:`app.ai.llm.get_llm` lands here, so this
class is not a stub — it is a small, deterministic, genuinely useful text engine.

Three behaviours make the offline path exercise the real happy path rather than a
degenerate one:

**Determinism.** Every reply is a pure function of ``(model, system, prompt, schema)``. The
seed is a BLAKE2b digest of those four, and all variability comes from a
:class:`random.Random` seeded with it — never from the global :mod:`random` state. The same
call in a test tomorrow produces the same bytes.

**Schema awareness.** Given a ``json_schema`` this walks the schema and synthesises a
structurally valid instance: it resolves ``$ref``, merges ``allOf``, picks a branch of
``anyOf``/``oneOf``, honours ``const``, ``enum``, ``default``, ``type``, ``properties``,
``required``, ``items``, ``prefixItems``, ``minItems``/``maxItems``,
``minLength``/``maxLength``, ``minimum``/``maximum`` and the common ``format`` values.
Downstream validation therefore succeeds and the code under it is really executed.

**Grounding in the prompt.** Nothing is invented that the prompt already supplies. Ids in
the prompt — UUIDs, ``fact_id: …`` pairs, ``fact-…`` slugs — are echoed back into id-shaped
fields, so a resume engine that checks "every returned ``fact_id`` was in the retrieved set"
sees a clean pass instead of a wall of hallucination warnings. Enum values mentioned in the
prompt are preferred over arbitrary ones. Free-text fields are filled with sentences lifted
verbatim from the prompt, and short name-shaped fields with its most salient keywords, which
turns the offline path into a real extractive summariser rather than a lorem-ipsum
generator.

Zero tokens are billed, and nothing is cached: regenerating is cheaper than a cache lookup.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
import re
from collections import Counter
from typing import TYPE_CHECKING, Any, ClassVar, Final

import structlog

from app.ai.embeddings import STOPWORDS, tokenize
from app.ai.llm import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    OUTCOME_SUCCESS,
    LLMResponse,
    ModelPlugin,
    record_llm_request,
)
from app.models.enums import PluginKind
from app.plugins import PluginMeta, plugin

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from collections.abc import Sequence

__all__ = ["NullModel", "extract_keywords", "extract_sentences", "summarize"]

logger = structlog.get_logger(__name__)

# ======================================================================================
# Constants
# ======================================================================================

#: Model identifier reported on every reply, and used as the metric label.
NULL_MODEL_ID: Final[str] = "null"

#: Ceiling on schema recursion, which also terminates a self-referential ``$ref``.
MAX_SCHEMA_DEPTH: Final[int] = 12

#: How many elements to synthesise for an array with no ``minItems``.
DEFAULT_ARRAY_ITEMS: Final[int] = 2

#: Hard cap on synthesised array length, whatever ``minItems`` asks for. A schema demanding
#: a thousand elements is a schema bug, and the offline path must stay fast.
MAX_ARRAY_ITEMS: Final[int] = 8

#: Shortest fragment of the prompt accepted as a "sentence" worth reusing.
MIN_SENTENCE_CHARS: Final[int] = 24

#: Longest sentence echoed into a text field, before truncation on a word boundary.
MAX_SENTENCE_CHARS: Final[int] = 240

#: How many salient keywords are kept for short, name-shaped fields.
MAX_KEYWORDS: Final[int] = 24

#: How many identifiers are harvested from a prompt.
MAX_IDENTIFIERS: Final[int] = 64

#: Sentences used by :func:`summarize` and by the no-schema reply.
SUMMARY_SENTENCES: Final[int] = 3

#: Ceiling on the no-schema reply, so a huge prompt cannot produce a huge "summary".
SUMMARY_MAX_CHARS: Final[int] = 600

#: Bounds used for a numeric field the schema does not constrain.
DEFAULT_INTEGER_MINIMUM: Final[int] = 0
DEFAULT_INTEGER_MAXIMUM: Final[int] = 10

#: Bounds used for a ``confidence``-shaped number, which is a probability by convention
#: everywhere in this codebase (``ExtractedFact.confidence``, ``AnswerPlan.confidence``).
CONFIDENCE_MINIMUM: Final[float] = 0.55
CONFIDENCE_MAXIMUM: Final[float] = 0.95

#: Decimal places on synthesised floats, so replies stay readable and stable.
FLOAT_PRECISION: Final[int] = 2

#: Fixed date used for ``format: date`` fields — a real date, in the past, unambiguous.
PLACEHOLDER_DATE: Final[str] = "2024-01-15"

#: Fallbacks when the prompt yields nothing usable for a field.
PLACEHOLDER_TEXT: Final[str] = "No source material was available for this field."
PLACEHOLDER_NAME: Final[str] = "unspecified"

#: Property-name fragments that mark a field as holding an identifier.
ID_FIELD_NAMES: Final[frozenset[str]] = frozenset({"id", "ids", "uuid", "key", "ref"})
ID_FIELD_SUFFIXES: Final[tuple[str, ...]] = ("_id", "_ids", "_uuid", "_key", "_ref")

#: Property-name fragments that mark a field as holding a short label.
NAME_FIELD_TOKENS: Final[tuple[str, ...]] = (
    "name",
    "title",
    "label",
    "heading",
    "organization",
    "organisation",
    "company",
    "employer",
    "role",
    "skill",
    "technology",
    "topic",
    "tag",
    "keyword",
    "language",
    "tool",
)

#: Property-name fragments that mark a field as holding free text.
TEXT_FIELD_TOKENS: Final[tuple[str, ...]] = (
    "text",
    "summary",
    "description",
    "detail",
    "rationale",
    "reason",
    "body",
    "content",
    "bullet",
    "note",
    "evidence",
    "point",
    "answer",
    "explanation",
    "metric",
    "impact",
)

#: Property-name fragments that mark a field as holding a date.
DATE_FIELD_TOKENS: Final[tuple[str, ...]] = ("date", "_at", "start", "end", "since", "until")

#: Property-name fragment marking a probability-shaped number.
CONFIDENCE_FIELD_TOKEN: Final[str] = "confidence"

#: JSON Schema ``format`` values this synthesiser produces real examples for.
FORMAT_EXAMPLES: Final[dict[str, str]] = {
    "date": PLACEHOLDER_DATE,
    "date-time": f"{PLACEHOLDER_DATE}T09:00:00+00:00",
    "time": "09:00:00",
    "email": "candidate@example.com",
    "uri": "https://example.com/",
    "url": "https://example.com/",
    "uri-reference": "/example",
    "hostname": "example.com",
    "ipv4": "127.0.0.1",
}

#: Keys under which a schema may keep its local definitions.
DEFINITION_CONTAINERS: Final[tuple[str, ...]] = ("$defs", "definitions")

#: A canonical UUID, as emitted by ATS feeds, database rows and our own knowledge graph.
_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

#: ``fact_id: F-12``, ``"source_id": "abc"``, ``ids = [x]`` — a labelled identifier.
_LABELLED_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:\bids?\b|\b[a-z][a-z0-9_]*_ids?\b)[\"']?\s*[:=]\s*[\[\"']*([A-Za-z0-9][A-Za-z0-9._:-]{1,63})",
    re.IGNORECASE,
)

#: ``fact-1a2b``, ``chunk_7`` — a prefixed identifier slug used across the knowledge engine.
_SLUG_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:fact|doc|document|chunk|entity|edge|node|source|src|posting|job|resume)"
    r"[-_][A-Za-z0-9][A-Za-z0-9._-]{0,62}\b"
)

#: Sentence boundary: terminal punctuation followed by space, or any newline.
_SENTENCE_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"(?<=[.!?])\s+|[\r\n]+")

#: Leading markdown/bullet decoration stripped from a harvested sentence.
_BULLET_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(?:[-*+•>]|#{1,6}|\d+[.)])\s*")

#: Runs of whitespace, collapsed to a single space.
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")

#: Characters trimmed from the end of a harvested identifier.
_ID_TRAILING_CHARS: Final[str] = ".,;:-_\"'"


# ======================================================================================
# Text utilities
# ======================================================================================


def extract_sentences(text: str) -> list[str]:
    """Split *text* into reusable sentences, in order of appearance.

    Markdown decoration is stripped, whitespace collapsed, over-long sentences truncated on
    a word boundary, and fragments shorter than :data:`MIN_SENTENCE_CHARS` dropped —
    headings and table cells make poor prose.

    Args:
        text: Arbitrary prompt text.

    Returns:
        The sentences, deduplicated, preserving order.
    """
    sentences: list[str] = []
    seen: set[str] = set()
    for raw in _SENTENCE_SPLIT_RE.split(text):
        candidate = _WHITESPACE_RE.sub(" ", _BULLET_PREFIX_RE.sub("", raw)).strip()
        if len(candidate) < MIN_SENTENCE_CHARS:
            continue
        if len(candidate) > MAX_SENTENCE_CHARS:
            candidate = candidate[:MAX_SENTENCE_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "…"
        if candidate not in seen:
            seen.add(candidate)
            sentences.append(candidate)
    return sentences


def extract_keywords(text: str, limit: int = MAX_KEYWORDS) -> list[str]:
    """Return the most salient words in *text*, most frequent first.

    Shares :func:`app.ai.embeddings.tokenize` and :data:`app.ai.embeddings.STOPWORDS` with
    the embedder, so "salient" means the same thing in both places.

    Args:
        text: Arbitrary prompt text.
        limit: Maximum number of keywords to return.

    Returns:
        Lowercase keywords, ordered by descending frequency and then by first appearance,
        so the result is fully deterministic.
    """
    first_seen: dict[str, int] = {}
    counts: Counter[str] = Counter()
    for position, token in enumerate(tokenize(text)):
        if token in STOPWORDS or len(token) < 3 or token.isdigit():
            continue
        counts[token] += 1
        first_seen.setdefault(token, position)
    ranked = sorted(counts, key=lambda word: (-counts[word], first_seen[word]))
    return ranked[:limit]


def summarize(
    text: str,
    *,
    max_sentences: int = SUMMARY_SENTENCES,
    max_chars: int = SUMMARY_MAX_CHARS,
) -> str:
    """Produce a deterministic extractive summary of *text*.

    Sentences are scored by the summed frequency of their salient words, normalised by
    length so a long sentence does not win by sheer mass. The best few are returned in their
    original order, which keeps the summary readable.

    Args:
        text: The text to summarise.
        max_sentences: How many sentences to keep.
        max_chars: Ceiling on the returned string.

    Returns:
        The summary, or a truncated copy of *text* when it contains no usable sentence.
    """
    sentences = extract_sentences(text)
    if not sentences:
        collapsed = _WHITESPACE_RE.sub(" ", text).strip()
        return collapsed[:max_chars]

    weights = Counter(
        token for token in tokenize(text) if token not in STOPWORDS and len(token) >= 3
    )
    scored: list[tuple[float, int]] = []
    for index, sentence in enumerate(sentences):
        tokens = [token for token in tokenize(sentence) if token not in STOPWORDS]
        if not tokens:
            scored.append((0.0, index))
            continue
        scored.append((sum(weights[token] for token in tokens) / len(tokens), index))

    chosen = sorted(
        sorted(scored, key=lambda item: (-item[0], item[1]))[:max_sentences],
        key=lambda item: item[1],
    )
    summary = " ".join(sentences[index] for _, index in chosen)
    return summary[:max_chars]


def _harvest_identifiers(text: str) -> list[str]:
    """Collect identifier-looking tokens from *text*, in order of appearance.

    Three shapes are recognised: canonical UUIDs, values labelled by an ``id``/``*_id`` key,
    and the prefixed slugs the knowledge engine uses (``fact-…``, ``chunk_…``).

    Args:
        text: Prompt text that may enumerate ids the model is expected to reference.

    Returns:
        Up to :data:`MAX_IDENTIFIERS` identifiers, deduplicated, ordered by first appearance.
    """
    found: list[tuple[int, str]] = []
    for pattern, group in ((_UUID_RE, 0), (_LABELLED_ID_RE, 1), (_SLUG_ID_RE, 0)):
        for match in pattern.finditer(text):
            value = match.group(group).strip().strip(_ID_TRAILING_CHARS)
            if value:
                found.append((match.start(group), value))

    ordered: list[str] = []
    seen: set[str] = set()
    for _, value in sorted(found, key=lambda item: item[0]):
        if value not in seen:
            seen.add(value)
            ordered.append(value)
        if len(ordered) >= MAX_IDENTIFIERS:
            break
    return ordered


# ======================================================================================
# Prompt context
# ======================================================================================


class _PromptContext:
    """The reusable material a prompt supplies, so nothing has to be invented.

    Holds three cycling pools — identifiers, sentences and keywords — plus the prompt text
    itself for substring tests (used to prefer an enum value the prompt actually mentions).
    Each pool hands out its next member on every request and wraps around, so sibling array
    elements differ from one another while the whole sequence stays deterministic.
    """

    def __init__(self, prompt: str, system: str = "") -> None:
        """Harvest reusable material from the prompts.

        Args:
            prompt: The user message — the primary source, since it carries the document,
                the fact list, or whatever the model is being asked about.
            system: The system prompt, used only as a fallback when the user message is
                empty or yields nothing.
        """
        source = prompt if prompt.strip() else system
        self.text: str = source
        self.lowered: str = source.lower()
        self._identifiers = _harvest_identifiers(source)
        self._sentences = extract_sentences(source)
        self._keywords = extract_keywords(source)
        self._cursors: dict[str, int] = {}

    def _next(self, pool_name: str, pool: Sequence[str]) -> str | None:
        """Return the next member of *pool*, cycling.

        Args:
            pool_name: Cursor identity for the pool.
            pool: The pool to draw from.

        Returns:
            The next member, or ``None`` when the pool is empty.
        """
        if not pool:
            return None
        cursor = self._cursors.get(pool_name, 0)
        self._cursors[pool_name] = cursor + 1
        return pool[cursor % len(pool)]

    @property
    def has_identifiers(self) -> bool:
        """Whether the prompt supplied any identifiers to echo back."""
        return bool(self._identifiers)

    def next_identifier(self) -> str | None:
        """Return the next identifier harvested from the prompt, or ``None``."""
        return self._next("identifier", self._identifiers)

    def next_sentence(self) -> str | None:
        """Return the next sentence lifted from the prompt, or ``None``."""
        return self._next("sentence", self._sentences)

    def next_keyword(self) -> str | None:
        """Return the next salient keyword from the prompt, or ``None``."""
        return self._next("keyword", self._keywords)

    def mentions(self, value: str) -> bool:
        """Return whether the prompt mentions *value*, case-insensitively.

        Args:
            value: A candidate string, typically an enum member.

        Returns:
            ``True`` when the prompt contains it.
        """
        return bool(value) and value.lower() in self.lowered


# ======================================================================================
# Schema synthesis
# ======================================================================================


class _SchemaSynthesizer:
    """Builds one structurally valid instance of a JSON Schema, grounded in a prompt.

    Not a general-purpose schema engine: it covers the JSON Schema subset ApplicantOS uses
    in :mod:`app.ai.prompts` and in provider tool definitions, and degrades to a sensible
    value for anything it does not recognise, because a synthesiser that raised would take
    the whole offline path down.
    """

    def __init__(self, root: dict[str, Any], context: _PromptContext, rng: random.Random) -> None:
        """Prepare a synthesiser for one call.

        Args:
            root: The root schema, which also resolves ``$ref`` pointers.
            context: Material harvested from the prompt.
            rng: Seeded generator; the only source of variability.
        """
        self._root = root
        self._context = context
        self._rng = rng

    # -- entry point ---------------------------------------------------------------------

    def build(self) -> Any:
        """Synthesise an instance of the root schema."""
        return self._value(self._root, name="", depth=0)

    # -- schema plumbing --------------------------------------------------------------------

    def _resolve(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Resolve ``$ref`` and flatten ``allOf`` into a single effective schema.

        Args:
            schema: A (sub)schema.

        Returns:
            The effective schema, with local ``$ref`` pointers followed and ``allOf``
            branches merged left to right. Unresolvable references degrade to the schema
            with the ``$ref`` key removed rather than raising.
        """
        current = schema
        for _ in range(MAX_SCHEMA_DEPTH):
            reference = current.get("$ref")
            if not isinstance(reference, str):
                break
            target = self._dereference(reference)
            if target is None:
                current = {key: value for key, value in current.items() if key != "$ref"}
                break
            siblings = {key: value for key, value in current.items() if key != "$ref"}
            current = {**target, **siblings}

        branches = current.get("allOf")
        if isinstance(branches, list) and branches:
            merged: dict[str, Any] = {}
            for branch in branches:
                if isinstance(branch, dict):
                    merged = self._merge(merged, self._resolve(branch))
            current = self._merge(merged, {k: v for k, v in current.items() if k != "allOf"})
        return current

    def _dereference(self, reference: str) -> dict[str, Any] | None:
        """Follow a local JSON pointer such as ``#/$defs/Fact``.

        Args:
            reference: The ``$ref`` value. Only same-document references are supported;
                remote schemas cannot be fetched offline, which is the whole point here.

        Returns:
            The referenced subschema, or ``None``.
        """
        if not reference.startswith("#"):
            return None
        node: Any = self._root
        for segment in reference.lstrip("#/").split("/"):
            if not segment:
                continue
            token = segment.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and token in node:
                node = node[token]
            elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
                node = node[int(token)]
            else:
                return None
        return node if isinstance(node, dict) else None

    @staticmethod
    def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
        """Merge two schemas, with *overlay* winning.

        ``properties`` are merged key-wise and ``required`` unioned; every other keyword is
        replaced. That is enough for the ``allOf`` composition used in practice.

        Args:
            base: The lower-precedence schema.
            overlay: The higher-precedence schema.

        Returns:
            The merged schema.
        """
        merged = {**base, **overlay}
        if "properties" in base and "properties" in overlay:
            merged["properties"] = {**base["properties"], **overlay["properties"]}
        if "required" in base and "required" in overlay:
            merged["required"] = list(dict.fromkeys([*base["required"], *overlay["required"]]))
        return merged

    @staticmethod
    def _type_of(schema: dict[str, Any]) -> str:
        """Decide which JSON type to synthesise for *schema*.

        Args:
            schema: An effective (resolved) schema.

        Returns:
            One of ``object``, ``array``, ``string``, ``integer``, ``number``, ``boolean``,
            ``null``. A schema with no ``type`` is classified by its structural keywords —
            ``properties`` implies an object, ``items`` an array — and falls back to
            ``string``, which every consumer can render.
        """
        declared = schema.get("type")
        if isinstance(declared, str):
            return declared
        if isinstance(declared, (list, tuple)):
            for candidate in declared:
                if isinstance(candidate, str) and candidate != "null":
                    return candidate
            return "null"
        if "properties" in schema or "additionalProperties" in schema:
            return "object"
        if "items" in schema or "prefixItems" in schema:
            return "array"
        return "string"

    # -- value synthesis ----------------------------------------------------------------------

    def _value(self, schema: Any, *, name: str, depth: int) -> Any:
        """Synthesise a value for *schema*.

        Args:
            schema: A (sub)schema; booleans and non-mappings are tolerated.
            name: The property name this value will be stored under, which drives the
                grounding heuristics (ids, names, free text, dates).
            depth: Current recursion depth.

        Returns:
            A JSON-native value.
        """
        if depth > MAX_SCHEMA_DEPTH or schema is False:
            return None
        if schema is True or not isinstance(schema, dict):
            return self._string({}, name=name)

        resolved = self._resolve(schema)

        if "const" in resolved:
            return resolved["const"]
        choices = resolved.get("enum")
        if isinstance(choices, list) and choices:
            return self._enum(choices)
        if "default" in resolved:
            return resolved["default"]

        for keyword in ("oneOf", "anyOf"):
            branches = resolved.get(keyword)
            if isinstance(branches, list) and branches:
                return self._value(self._pick_branch(branches), name=name, depth=depth + 1)

        kind = self._type_of(resolved)
        if kind == "object":
            return self._object(resolved, depth=depth)
        if kind == "array":
            return self._array(resolved, name=name, depth=depth)
        if kind == "boolean":
            return self._rng.random() < 0.5
        if kind == "integer":
            return self._integer(resolved, name=name)
        if kind == "number":
            return self._number(resolved, name=name)
        if kind == "null":
            return None
        return self._string(resolved, name=name)

    def _enum(self, choices: list[Any]) -> Any:
        """Choose an enum member, preferring one the prompt mentions.

        Args:
            choices: The declared ``enum`` values.

        Returns:
            The first choice the prompt mentions, otherwise a seeded pick. Preferring a
            mentioned value is what makes an offline classification agree with its input
            instead of contradicting it.
        """
        for choice in choices:
            if isinstance(choice, str) and self._context.mentions(choice):
                return choice
        return choices[self._rng.randrange(len(choices))]

    def _pick_branch(self, branches: list[Any]) -> Any:
        """Choose a branch of ``anyOf``/``oneOf``.

        Args:
            branches: The declared branches.

        Returns:
            The first branch that is not the ``null`` type — an optional field is far more
            useful populated than empty — or the first branch when they are all nullable.
        """
        for branch in branches:
            if isinstance(branch, dict) and branch.get("type") != "null":
                return branch
        return branches[0]

    def _object(self, schema: dict[str, Any], *, depth: int) -> dict[str, Any]:
        """Synthesise an object, populating every declared and every required property.

        Args:
            schema: The effective object schema.
            depth: Current recursion depth.

        Returns:
            The object. Declared properties are emitted in declaration order; a name listed
            in ``required`` but never declared still gets a value, because omitting it would
            produce an instance that fails its own schema.
        """
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        required = [str(item) for item in required] if isinstance(required, list) else []

        result: dict[str, Any] = {}
        for key, subschema in properties.items():
            result[str(key)] = self._value(subschema, name=str(key), depth=depth + 1)
        for key in required:
            if key not in result:
                result[key] = self._value({}, name=key, depth=depth + 1)
        return result

    def _array(self, schema: dict[str, Any], *, name: str, depth: int) -> list[Any]:
        """Synthesise an array honouring ``prefixItems``, ``minItems`` and ``maxItems``.

        Args:
            schema: The effective array schema.
            name: The property name, used for grounding.
            depth: Current recursion depth.

        Returns:
            The array. An id-shaped array is filled from the prompt's identifiers, one per
            element, so downstream "was this id in the retrieved set?" checks pass.
        """
        minimum = self._bounded_int(schema.get("minItems"), DEFAULT_ARRAY_ITEMS)
        maximum = self._bounded_int(schema.get("maxItems"), MAX_ARRAY_ITEMS)
        count = max(min(max(minimum, 1), maximum, MAX_ARRAY_ITEMS), 0)

        prefix = schema.get("prefixItems")
        items: list[Any] = []
        if isinstance(prefix, list):
            for index, subschema in enumerate(prefix):
                items.append(self._value(subschema, name=f"{name}[{index}]", depth=depth + 1))
            count = max(count, len(items))

        item_schema = schema.get("items", {})
        singular = name[:-1] if name.endswith("s") and len(name) > 1 else name
        while len(items) < count:
            items.append(self._value(item_schema, name=singular, depth=depth + 1))
        return items[:maximum]

    @staticmethod
    def _bounded_int(value: Any, fallback: int) -> int:
        """Return *value* as a non-negative integer, or *fallback* when it is unusable."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return fallback
        return max(int(value), 0)

    def _integer(self, schema: dict[str, Any], *, name: str) -> int:
        """Synthesise an integer inside the schema's bounds.

        Args:
            schema: The effective schema.
            name: The property name, for logging symmetry with the other synthesisers.

        Returns:
            A value in ``[minimum, maximum]``, defaulting to
            ``[DEFAULT_INTEGER_MINIMUM, DEFAULT_INTEGER_MAXIMUM]``.
        """
        low = schema.get("minimum", DEFAULT_INTEGER_MINIMUM)
        high = schema.get("maximum", DEFAULT_INTEGER_MAXIMUM)
        low = int(low) if isinstance(low, (int, float)) and not isinstance(low, bool) else 0
        high = int(high) if isinstance(high, (int, float)) and not isinstance(high, bool) else low
        if high < low:
            high = low
        return self._rng.randint(low, high)

    def _number(self, schema: dict[str, Any], *, name: str) -> float:
        """Synthesise a float inside the schema's bounds.

        Args:
            schema: The effective schema.
            name: The property name; a ``confidence``-shaped field is kept in the
                plausible-probability band rather than the schema's full range.

        Returns:
            A value rounded to :data:`FLOAT_PRECISION` decimal places.
        """
        if CONFIDENCE_FIELD_TOKEN in name.lower():
            low, high = CONFIDENCE_MINIMUM, CONFIDENCE_MAXIMUM
        else:
            low = schema.get("minimum", 0.0)
            high = schema.get("maximum", 1.0)
            low = float(low) if isinstance(low, (int, float)) and not isinstance(low, bool) else 0.0
            high = float(high) if isinstance(high, (int, float)) and not isinstance(high, bool) else 1.0
        if high < low:
            high = low
        return round(self._rng.uniform(low, high), FLOAT_PRECISION)

    def _string(self, schema: dict[str, Any], *, name: str) -> str:
        """Synthesise a string, grounded in the prompt wherever the field name allows it.

        Resolution order: an explicit ``format``, then the field-name heuristics (identifier
        → echo a prompt id; date-shaped → a real date; name-shaped → a salient keyword;
        text-shaped or unclassified → a sentence from the prompt), then the length bounds
        are applied.

        Args:
            schema: The effective schema.
            name: The property name.

        Returns:
            The string, padded to ``minLength`` and truncated to ``maxLength``.
        """
        declared_format = schema.get("format")
        if isinstance(declared_format, str):
            if declared_format == "uuid":
                return self._uuid()
            example = FORMAT_EXAMPLES.get(declared_format)
            if example is not None:
                return example

        lowered = name.lower()
        if self._is_identifier_field(lowered):
            value = self._context.next_identifier()
            return value if value is not None else self._uuid()
        if any(token in lowered for token in DATE_FIELD_TOKENS):
            return PLACEHOLDER_DATE
        if any(token in lowered for token in NAME_FIELD_TOKENS):
            value = self._context.next_keyword()
            return self._bound(value or PLACEHOLDER_NAME, schema)
        if any(token in lowered for token in TEXT_FIELD_TOKENS) or not lowered:
            value = self._context.next_sentence()
            return self._bound(value or summarize(self._context.text) or PLACEHOLDER_TEXT, schema)

        value = self._context.next_sentence() or self._context.next_keyword()
        return self._bound(value or PLACEHOLDER_TEXT, schema)

    @staticmethod
    def _is_identifier_field(lowered_name: str) -> bool:
        """Return whether a property name denotes an identifier.

        Args:
            lowered_name: The lowercased property name.

        Returns:
            ``True`` for ``id``/``ids``/``uuid``/``key``/``ref`` and any ``*_id``-style name.
        """
        return lowered_name in ID_FIELD_NAMES or lowered_name.endswith(ID_FIELD_SUFFIXES)

    def _uuid(self) -> str:
        """Return a deterministic, canonically formatted UUID drawn from the seeded RNG."""
        digits = f"{self._rng.getrandbits(128):032x}"
        return "-".join(
            (digits[:8], digits[8:12], digits[12:16], digits[16:20], digits[20:32])
        )

    @staticmethod
    def _bound(value: str, schema: dict[str, Any]) -> str:
        """Apply ``minLength`` and ``maxLength`` to *value*.

        Args:
            value: The candidate string.
            schema: The effective schema.

        Returns:
            The string, truncated on a word boundary where possible and padded with a
            repeat of itself when it is too short.
        """
        maximum = schema.get("maxLength")
        if isinstance(maximum, int) and not isinstance(maximum, bool) and 0 < maximum < len(value):
            truncated = value[:maximum]
            head = truncated.rsplit(" ", 1)[0]
            value = head if len(head) >= maximum // 2 else truncated
        minimum = schema.get("minLength")
        if isinstance(minimum, int) and not isinstance(minimum, bool) and len(value) < minimum:
            padding = value or PLACEHOLDER_NAME
            while len(value) < minimum:
                value = f"{value} {padding}".strip()
            value = value[:minimum] if isinstance(maximum, int) and maximum else value
        return value


# ======================================================================================
# The plugin
# ======================================================================================


@plugin
class NullModel(ModelPlugin):
    """A deterministic, offline, schema-aware model. Zero keys, zero tokens, zero network.

    Subclasses :class:`~app.ai.llm.ModelPlugin` rather than
    :class:`~app.ai.llm.GuardedModelPlugin` on purpose: with no provider there is nothing to
    retry, nothing to bill, and nothing worth caching — regenerating is cheaper than a cache
    round trip, and the reply is a pure function of its inputs anyway.
    """

    meta: ClassVar[PluginMeta] = PluginMeta(
        kind=PluginKind.MODEL,
        name=NULL_MODEL_ID,
        display_name="Offline (null) model",
        description=(
            "Deterministic offline model. Synthesises schema-valid JSON and extractive "
            "summaries from the prompt itself, so ApplicantOS runs end to end with no "
            "API keys."
        ),
        author="ApplicantOS",
        capabilities=frozenset({"completion", "json_schema", "offline", "deterministic", "free"}),
    )

    def model_for_tier(self, tier: str) -> str:
        """Return the single offline model identifier, whichever tier was asked for.

        Args:
            tier: The requested tier; accepted and ignored.

        Returns:
            :data:`NULL_MODEL_ID`, so metrics and cache keys stay stable across tiers.
        """
        return NULL_MODEL_ID

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Produce a deterministic reply.

        Args:
            system: The system prompt. Used for the seed, and as fallback source material
                when *prompt* is empty.
            prompt: The user message — the source of every id, sentence and keyword in the
                reply.
            max_tokens: Accepted and ignored; nothing is generated token by token.
            temperature: Accepted and ignored; the reply is always deterministic.
            json_schema: When supplied, the reply is a JSON document synthesised to satisfy
                it. Otherwise the reply is an extractive summary of the prompt.

        Returns:
            The reply, with zero token usage and ``cached=False``.
        """
        context = _PromptContext(prompt, system)
        rng = random.Random(self._seed(system, prompt, json_schema))

        if json_schema is not None:
            payload = _SchemaSynthesizer(json_schema, context, rng).build()
            text = json.dumps(payload, ensure_ascii=False)
            logger.debug(
                "null_model.synthesized",
                schema_type=json_schema.get("type"),
                identifiers=context.has_identifiers,
            )
        else:
            text = summarize(context.text) or PLACEHOLDER_TEXT
            logger.debug("null_model.summarized", characters=len(text))

        record_llm_request(NULL_MODEL_ID, OUTCOME_SUCCESS)
        return LLMResponse(
            text=text,
            input_tokens=0,
            output_tokens=0,
            model=NULL_MODEL_ID,
            cached=False,
            raw={
                "provider": NULL_MODEL_ID,
                "deterministic": True,
                "generated_at": dt.datetime.now(dt.UTC).isoformat(),
                "schema_supplied": json_schema is not None,
            },
        )

    @staticmethod
    def _seed(system: str, prompt: str, json_schema: dict[str, Any] | None) -> int:
        """Derive the RNG seed from the full call signature.

        Uses BLAKE2b rather than :func:`hash`, whose per-process randomisation would make
        replies differ between runs — the one thing this model must never do.

        Args:
            system: The system prompt.
            prompt: The user message.
            json_schema: The requested schema, if any.

        Returns:
            A 64-bit seed.
        """
        digest = hashlib.blake2b(digest_size=8)
        digest.update(system.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(prompt.encode("utf-8"))
        digest.update(b"\x00")
        if json_schema is not None:
            digest.update(
                json.dumps(json_schema, sort_keys=True, default=str).encode("utf-8")
            )
        return int.from_bytes(digest.digest(), "big")

    async def healthcheck(self) -> bool:
        """Always healthy: there is nothing to configure and nothing to reach."""
        return True
