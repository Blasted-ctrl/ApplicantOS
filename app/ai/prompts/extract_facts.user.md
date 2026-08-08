Extract the accomplishments stated in the source text below, following the rules in your
instructions. Everything you emit must be supported by wording that is literally present in
that text.

## Context

- Default fact kind when nothing more specific fits: `$kind`
- Allowed fact kinds: $fact_kinds
- Known organization (use this exact value; do not invent an alternative): $organization
- Known role (use this exact value; do not invent an alternative): $role

If a context value above is `unknown`, treat the corresponding field as unknown: copy it
from the source text if the source states it, otherwise set it to `null`. Never carry the
literal word "unknown" into a field.

## What to produce

One entry per atomic claim, with:

| field | meaning |
| --- | --- |
| `text` | the claim in the source's own words, one past-tense sentence |
| `kind` | one of the allowed fact kinds above |
| `skills` | disciplines demonstrated, e.g. firmware, sensor fusion, technical writing |
| `technologies` | named tools actually mentioned, e.g. FreeRTOS, PyTorch, PostgreSQL |
| `metrics` | quantified outcomes, quoted exactly as the source wrote them |
| `organization` | only if stated or given above, else `null` |
| `role` | only if stated or given above, else `null` |
| `date_start` | `YYYY-MM` or `YYYY` if stated, else `null` |
| `date_end` | `YYYY-MM` or `YYYY`, or `null` for ongoing work |
| `confidence` | 0.0–1.0, how directly the source supports the claim |

Return the JSON object described in your instructions. Return an empty `facts` array rather
than inventing content when the text contains no verifiable claim about this person.

## Source text

```
$text
```
