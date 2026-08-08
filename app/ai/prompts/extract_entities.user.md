Extract the entities and relationships stated in the source text below, following the rules
in your instructions. Emit only what the text literally names.

## Allowed values

- Entity kinds: $entity_kinds
- Relation kinds: $relation_kinds

Use these strings exactly. If nothing in the list fits an entity, leave that entity out
rather than forcing it into the closest kind.

## What to produce

For each entity: `name` (copied from the source), `kind`, an optional one-line `summary`
grounded in the source, any other spellings the source used in `aliases`, and a
`confidence` between 0.0 and 1.0.

For each edge: `source_name`, `source_kind`, `target_name`, `target_kind`, `relation`, and
a `weight` between 0.0 and 1.0. Both endpoints must be entities you listed, and the
relationship must be asserted by the text — not by what you know about these technologies
in general.

Return the JSON object described in your instructions. Return empty arrays rather than
inventing content when the text names nothing concrete.

## Source text

```
$text
```
