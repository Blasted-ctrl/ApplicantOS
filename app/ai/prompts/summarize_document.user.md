Summarise the document below for a personal knowledge base, following the rules in your
instructions. Everything you write must be verifiable by reading that document.

## Document metadata

- Title: $title
- Source kind: $kind
- Origin: $source_uri

Treat this metadata as context only. It is not part of the document's content: never
present a value from it as something the document claims, and if a value reads as
`unknown`, ignore it entirely.

## Budget

- `summary`: at most $max_words words, in 2 to 4 sentences of plain declarative prose.
- `highlights`: at most 6 entries, one line each, each carrying the specific detail — the
  number, the name, the technology — that makes it worth keeping.

Preserve the document's own technical vocabulary verbatim. Those tokens are what makes this
document findable later, so a paraphrase that loses `STM32H7`, `ROS 2 Humble`, or
`PostgreSQL` is a worse summary even if it reads more smoothly.

## What to produce

Return the JSON object described in your instructions: `summary`, `highlights`, `topics`,
`technologies`, `organizations`, `roles`, `date_start`, `date_end`, `confidence`.

Leave a list empty and a date `null` rather than filling it with a guess. If the document
is empty, boilerplate, or auto-generated scaffolding, say exactly that in one sentence and
return empty lists.

## Document

```
$text
```
