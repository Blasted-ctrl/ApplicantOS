Tailor a one-page résumé for the posting below, selecting and rewriting **only** the facts
supplied in the fact list. Return the JSON object described in your instructions.

## The posting

- Title: $posting_title
- Company: $company
- Location: $posting_location
- Arrangement: $work_arrangement

### Description

```
$posting
```

### Skills this posting names

$target_skills

## Budget

- Bullets: at most **$max_bullets** across the whole résumé.
- Length: one page. Bullets past the budget are dropped by the system, lowest impact first.

## The facts you may use

Each line is one fact, in this format:

```
<fact_id> | <kind> | <organization> | <role> | <dates> | <text> | skills: <skills> | tech: <technologies> | metrics: <metrics>
```

A dash (`—`) means the fact does not state that field. **This list is the complete universe
of things this résumé may claim.**

```
$facts
```

## What to produce

| field | meaning |
| --- | --- |
| `summary` | 1–2 sentences supported by the selected facts and the posting, or `""` |
| `sections` | conventional headings only, in the order given in your instructions |
| `entries` | one per (organisation, role, period); header fields copied from the facts |
| `bullets` | one per selected fact, each carrying that fact's `fact_id` |
| `skills_line` | comma-separated, drawn only from the selected facts' skills and technologies |
| `reasoning` | which facts you chose, and which you dropped and why |

Before you answer, check every bullet against three questions:

1. Is its `fact_id` present, verbatim, in the list above?
2. Does every number in it appear in that fact's own text or metrics?
3. Would the person who wrote that fact recognise the bullet as the same claim?

If any answer is no, fix the bullet or drop it. Selecting fewer facts is always allowed.
Inventing one never is.
