# Adding an ATS Provider

A complete worked tutorial. We will build **Hirebase**, a fictional applicant tracking system, from
an empty file to a registered provider that discovers postings, routes from a URL, and submits an
application through the browser layer.

Hirebase is invented, but everything else here is real: the base class, the helpers, the registry,
the selector pack, and the verification commands all exist in this tree and work as shown. If you
follow this against a real ATS instead, only the parsing changes.

**Adding a provider requires zero changes to the core engine.** That is the claim this document
exists to demonstrate — and to keep honest.

---

## 0. Before you write anything: decide the posture

The first decision is not technical.

> **Does this platform's terms of service permit automated submission?**

If the answer is no, or unclear, you set `supports_auto_apply = False`, you say so in the module
docstring, and `apply()` raises `UnsupportedFlowError` so the pipeline routes to manual review.
That is golden rule 10, and it is a feature: the user still gets discovery, scoring, a tailored
resume and a one-click link, and nobody's account gets banned.

The existing postures:

| Provider | Discovery | Auto-apply | Why |
|---|---|---|---|
| Greenhouse | Public job-board API | ✅ | Public application form, no account required |
| Lever | Public postings API | ✅ | Same |
| Ashby | Public posting API | ✅ | Same |
| Workday | CXS JSON endpoint | ❌ | Account-gated multi-step flow; routes to manual review |
| LinkedIn | User's own export or a public feed **only** | ❌ | ToS prohibits automated scraping and submission |

For this tutorial, assume Hirebase publishes a documented public API and its terms permit
programmatic applications. So: `supports_auto_apply = True`.

---

## 1. Add the provider name

`app/models/enums.py`:

```python
class ATSProviderName(StrEnum):
    LINKEDIN = "linkedin"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKDAY = "workday"
    HIREBASE = "hirebase"  # <- new
    MANUAL = "manual"
```

Mirror it immediately in `desktop/src/lib/api/types.ts`:

```ts
export const ATS_PROVIDER_NAMES = [
  'linkedin', 'greenhouse', 'lever', 'ashby', 'workday', 'hirebase', 'manual',
] as const;

export const AUTO_APPLY_PROVIDERS: ReadonlySet<ATSProviderName> = new Set<ATSProviderName>([
  'greenhouse', 'lever', 'ashby', 'hirebase',
]);
```

Enum drift between Python and TypeScript does not fail loudly — it produces a filter that matches
nothing and a badge with no colour, while the network tab shows a healthy 200. Do it now, in the
same commit.

Then generate the migration, because the enum column is a `VARCHAR` sized to the longest value:

```bash
SQLITE_MODE=true alembic revision --autogenerate -m "add hirebase provider"
```

Read the generated migration before committing it. Autogenerate reliably misses server defaults,
`ondelete` behaviour and index names.

---

## 2. The module skeleton

Create `app/jobs/hirebase.py`. The docstring is not decoration — it is where the automation
posture is recorded, and the `safety-reviewer` agent reads it.

```python
"""Hirebase — job discovery **and real automated submission** (``docs/CONTRACTS.md`` §9).

**Automation posture (binding, golden rule #10).** A Hirebase job board is a *public
application form*: ``https://hirebase.io/b/<board>/jobs/<id>`` renders the same form to an
anonymous visitor as to anyone else, and submitting it requires no account, no login and no
credential. Hirebase's published API terms permit programmatic discovery and submission at
polite rates. That is why :attr:`HirebaseProvider.supports_auto_apply` is ``True`` and
:attr:`HirebaseProvider.requires_login` is ``False``.

Submission still goes through :func:`app.jobs._apply.run_browser_apply`, so the kill switch
applies unchanged: nothing is submitted unless ``settings.auto_apply_enabled`` is on **and**
``settings.dry_run`` is off **and** the caller did not ask for a dry run (golden rule #3).
Anything the form asks that cannot be answered confidently escalates to manual review rather
than being guessed (golden rule #2).

**What the feed looks like.** ``GET /api/v2/boards/<board>/jobs`` returns one page of open
postings with an opaque ``next_cursor``. Descriptions arrive as HTML and are converted by
:func:`app.jobs._parsing.html_to_text`. Compensation is a structured object with an explicit
``period``, so unlike Greenhouse there is no need to mine the description for a salary.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, ClassVar, Final

import structlog

from app.jobs._apply import browser_available, run_browser_apply
from app.jobs._parsing import (
    clean_text,
    html_to_text,
    infer_arrangement,
    infer_employment_type,
    parse_date,
)
from app.jobs.base import (
    ApplyContext,
    ApplyResult,
    ATSProvider,
    PostingUnavailableError,
    ProviderError,
    RawPosting,
    SearchQuery,
)
from app.jobs.seeds import boards_from_query
from app.models.enums import ATSProviderName, PluginKind
from app.plugins.base import PluginMeta
from app.plugins.registry import plugin

__all__ = ["API_ROOT", "HirebaseProvider"]

logger = structlog.get_logger(__name__)

#: Root of the public Hirebase board API.
API_ROOT: Final[str] = "https://api.hirebase.io/api/v2"

#: Human-facing posting URL. ``{board}`` and ``{job_id}`` are filled per posting.
JOB_URL_TEMPLATE: Final[str] = "https://hirebase.io/b/{board}/jobs/{job_id}"

#: How long a board feed stays cached. Boards change slowly; a poll every 30 minutes against
#: a 15-minute cache means at most two upstream requests per board per hour.
FEED_TTL_SECONDS: Final[int] = 900

#: Postings requested per page. The API caps this at 100.
PAGE_SIZE: Final[int] = 100

#: Board used by :meth:`HirebaseProvider.healthcheck`. A stable, public, high-traffic board,
#: so a healthcheck failure means Hirebase is down rather than that one employer left.
HEALTHCHECK_BOARD: Final[str] = "hirebase"
```

Named constants, not magic numbers. Every one of those gets referenced below.

---

## 3. The class and its identity

```python
@plugin
class HirebaseProvider(ATSProvider):
    """Discovery and automated submission against Hirebase job boards.

    One instance per process — providers are plugin-registry singletons — so the HTTP
    connection pool and the board memo are shared across every discovery run.
    """

    meta: ClassVar[PluginMeta] = PluginMeta(
        kind=PluginKind.PROVIDER,
        name=ATSProviderName.HIREBASE.value,
        version="1.0.0",
        display_name="Hirebase",
        description=(
            "Hirebase job boards — public JSON feed for discovery, public application "
            "form for submission. No account required."
        ),
        author="ApplicantOS",
        capabilities=frozenset({"search", "fetch", "auto_apply"}),
    )
    name: ClassVar[ATSProviderName] = ATSProviderName.HIREBASE
    supports_auto_apply: ClassVar[bool] = True
    requires_login: ClassVar[bool] = False
    URL_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"\bhirebase\.io/b/", re.IGNORECASE),
        re.compile(r"\bapi\.hirebase\.io/", re.IGNORECASE),
        re.compile(r"[?&]hb_jid=\d+", re.IGNORECASE),
    ]

    def __init__(self, settings: Any, **kw: Any) -> None:
        super().__init__(settings, **kw)
        self._board_by_job_id: dict[str, str] = {}
```

Four things are happening here.

**`@plugin` is the registration.** It is `registry.register` under another name. There is no
central list of providers to edit; importing the module is what registers it, and
`app/jobs/__init__.py` imports every provider module so `load_all()` finds them.

**`URL_PATTERNS` powers `provider_for_url`.** When the user pastes a job link, this is how the
system decides who owns it. Include every shape the URL takes in the wild — the canonical form,
the API host, and the query-parameter form that appears in aggregator links. Greenhouse has five
patterns for exactly this reason.

**`capabilities` is advisory metadata** shown in `GET /settings/plugins`. `supports_auto_apply` is
the flag the pipeline actually gates on.

**Instance state is a memo, not a cache of results.** `_board_by_job_id` remembers which board a
job id came from so `fetch_posting("12345")` does not have to sweep every board. Real caching goes
through `app/cache`.

---

## 4. `search()` — the discovery path

`search` is an **async generator**. It yields lazily, respects `query.limit`, and never lets one
bad posting kill the run.

```python
def _boards(self, q: SearchQuery) -> list[str]:
    """Boards to poll: the user's own tokens, else the packaged seed list."""
    return boards_from_query(self.provider_name, q.extra)


async def _cached_json(
    self,
    url: str,
    *,
    key_parts: Sequence[Any],
    ttl: int,
    params: Mapping[str, Any] | None = None,
) -> Any:
    """``GET`` a JSON document, reading through the shared cache.

    Golden rule #9: job descriptions are cached by contract (``docs/CONTRACTS.md`` §7),
    under ``NAMESPACES.POSTING``. The cache import is lazy because it pulls in settings
    and possibly a Redis client — a provider that is registered but never polled should
    pay for neither.
    """
    from app.cache import NAMESPACES, get_cache, make_key

    cache = get_cache()
    key = make_key(NAMESPACES.POSTING, self.provider_name.value, *key_parts)

    async def factory() -> Any:
        return await self._get_json(url, params=params)

    return await cache.get_or_set(key, factory, ttl=ttl)


async def _feed(self, board: str, cursor: str | None) -> Mapping[str, Any]:
    """One page of a board feed."""
    params: dict[str, Any] = {"limit": PAGE_SIZE}
    if cursor:
        params["cursor"] = cursor
    payload = await self._cached_json(
        f"{API_ROOT}/boards/{board}/jobs",
        key_parts=("feed", board, cursor or ""),
        ttl=FEED_TTL_SECONDS,
        params=params,
    )
    if not isinstance(payload, Mapping):
        raise ProviderError(f"hirebase board {board!r} returned a non-object payload")
    return payload


async def search(self, q: SearchQuery) -> AsyncIterator[RawPosting]:
    """Yield postings matching *q* from every configured board.

    Args:
        q: The search. ``q.extra['boards']`` overrides the seed list.

    Yields:
        One :class:`~app.jobs.base.RawPosting` per surviving posting.

    Raises:
        ProviderRateLimitError: Propagated from the transport so the worker backs off.
        ProviderAuthError: If Hirebase starts requiring a key.
    """
    yielded = 0
    for board in self._boards(q):
        cursor: str | None = None
        while True:
            try:
                page = await self._feed(board, cursor)
            except PostingUnavailableError:
                # A board that 404s has been renamed or migrated to another ATS.
                # Expected, never an error: skip it and poll the rest.
                logger.info("hirebase.board_missing", board=board)
                break

            for entry in page.get("jobs", []):
                # Cheap tests first, over the *unparsed* entry. A five-hundred-posting
                # board should parse five descriptions for a five-result search, not
                # five hundred.
                if not self._matches_cheaply(entry, q):
                    continue
                posting = self._to_posting(board, entry)
                if posting is None:
                    continue
                if not q.matches_freshness(posting.posted_at):
                    continue
                yield posting
                yielded += 1
                if q.limit and yielded >= q.limit:
                    return

            cursor = page.get("next_cursor")
            if not cursor:
                break
```

Three habits worth copying:

- **Cheap filters before expensive parsing.** Title and location tests run against the raw JSON;
  only survivors get their HTML converted to text and their dates parsed. Greenhouse does the same
  thing and it is the difference between a four-megabyte response costing 40ms and costing 4s.
- **A missing board is information, not a failure.** Employers migrate between ATSs and rename
  boards. `_request` raises `PostingUnavailableError` for a 404; catching it here and continuing
  costs one wasted request per poll and nothing else.
- **`q.limit` stops the generator**, it does not filter a list afterwards. That is the point of
  yielding.

### Parsing one entry

```python
def _to_posting(self, board: str, entry: Mapping[str, Any]) -> RawPosting | None:
    """Convert one feed entry into a :class:`RawPosting`, or ``None`` if unusable.

    Returns ``None`` rather than raising: one malformed entry must degrade that entry,
    never the whole search.
    """
    job_id = clean_text(entry.get("id"))
    title = clean_text(entry.get("title"))
    if not job_id or not title:
        logger.debug("hirebase.entry_skipped", board=board, reason="missing id or title")
        return None

    self._board_by_job_id[job_id] = board
    description = html_to_text(entry.get("description_html") or "")
    location = clean_text((entry.get("location") or {}).get("name"))
    comp = entry.get("compensation") or {}

    return RawPosting(
        provider=self.name,
        external_id=job_id,
        url=JOB_URL_TEMPLATE.format(board=board, job_id=job_id),
        title=title,
        company_name=clean_text(entry.get("company_name")) or board,
        description=description,
        location=location,
        # infer_* return UNKNOWN rather than guessing — that is correct and wanted.
        work_arrangement=infer_arrangement(location, description),
        employment_type=infer_employment_type(title, description),
        salary_min=comp.get("min"),
        salary_max=comp.get("max"),
        salary_currency=comp.get("currency"),
        posted_at=parse_date(entry.get("published_at")),
        closes_at=parse_date(entry.get("closes_at")),
        apply_url=entry.get("apply_url"),
        raw=dict(entry),  # <- keep the untouched payload
    )
```

**`RawPosting.__post_init__` normalises for you.** Enum-shaped strings become enum members, dates
of every shape become timezone-aware UTC, a reversed salary range is put back in order, and the
currency is upper-cased and truncated to three characters. Passing raw feed values straight in is
correct — do not pre-clean them.

**`raw` is not optional.** It stores the provider's untouched payload so a parser bug can be fixed
and re-run without re-crawling every board. It has already paid for itself twice in this codebase.

**`WorkArrangement.UNKNOWN` is honest.** `infer_arrangement` returns it when the posting did not
say. Do not default to `ONSITE` to fill the field — the remote-only preference gate explicitly
does not fire on `unknown`, precisely so a guess here cannot silently discard good postings.

---

## 5. `fetch_posting()` — one posting by id or URL

```python
    async def fetch_posting(self, id_or_url: str) -> RawPosting | None:
        """Fetch one posting by Hirebase job id or by any Hirebase URL.

        Args:
            id_or_url: A bare job id, or any URL matching :attr:`URL_PATTERNS`.

        Returns:
            The posting, or ``None`` when it no longer exists.
        """
        board, job_id = self._split(id_or_url)
        if job_id is None:
            return None
        if board is None:
            board = self._board_by_job_id.get(job_id)
        if board is None:
            logger.debug("hirebase.board_unknown", job_id=job_id)
            return None

        try:
            entry = await self._cached_json(
                f"{API_ROOT}/boards/{board}/jobs/{job_id}",
                key_parts=("job", board, job_id),
                ttl=FEED_TTL_SECONDS,
            )
        except PostingUnavailableError:
            return None
        return self._to_posting(board, entry) if isinstance(entry, Mapping) else None
```

A closed posting is `None`, not an exception. The pipeline treats "gone" as an expiry, and a
posting the user saved yesterday closing overnight is normal.

---

## 6. `apply()` — submission, delegated

**Never hand-roll form filling in a provider.** Every safety guard — the kill switch, the
confidence floor, the essay ceiling, blocker detection, the before/after screenshots, the
submit-control whitelist — lives in `AutoFiller`. A provider that fills forms itself has routed
around all of them.

```python
async def apply(self, ctx: ApplyContext) -> ApplyResult:
    """Submit one application through the browser layer.

    Args:
        ctx: The application context, including the rendered resume path and the
            caller's ``dry_run`` flag.

    Returns:
        The result. ``needs_review`` whenever anything could not be answered
        confidently; ``ok=False`` on a genuine failure.
    """
    return await run_browser_apply(ctx, provider=self.name)


async def healthcheck(self) -> bool:
    """Report whether discovery — and, honestly, submission — can work right now."""
    if not browser_available():
        logger.info("hirebase.browser_unavailable")
        # Discovery still works; report the truth rather than a flat False.
    try:
        await self._feed(HEALTHCHECK_BOARD, None)
    except Exception as exc:
        logger.warning("hirebase.healthcheck_failed", error=str(exc))
        return False
    return True
```

`run_browser_apply` resolves `app.browser` **by name at call time**, so importing `app.jobs` never
imports Playwright. It also applies the kill switch independently of anything the provider does:
if `settings.auto_apply_enabled` is off or `settings.dry_run` is on, `AutoFiller.submit` returns
`False` without touching the button.

---

## 7. The selector pack

`app/browser/selectors.py`. This is what tells `AutoFiller` where the form is and — critically —
**which control is allowed to be clicked**.

```python
#: Hirebase — ``hirebase.io/b/<board>/jobs/<id>``. Hirebase annotates its controls with
#: ``data-hb`` attributes, which survive redesigns that break class names, so every selector
#: leads with the annotated form. Submission is supported (``docs/CONTRACTS.md`` §9).
HIREBASE: Final[SelectorPack] = SelectorPack(
    name=ATSProviderName.HIREBASE.value,
    form_root="[data-hb='application-form'], form[action*='/applications']",
    field_container="[data-hb='field'], .hb-field",
    label="[data-hb='label'], label",
    input=_FILLABLE_CONTROLS,
    file_input="input[type='file'][data-hb='resume'], input[type='file']",
    submit="[data-hb='submit'], button[type='submit']",
    success_markers=(
        "[data-hb='confirmation']",
        "Thanks for applying",
        "Your application has been received",
    ),
    error_markers=(
        "[data-hb='field-error']",
        "[aria-invalid='true']",
        "This field is required",
    ),
    next_step="",
    cookie_banner=_COMMON_COOKIE_BANNER,
    captcha_markers=_COMMON_CAPTCHA_MARKERS,
    supports_auto_apply=True,
)
```

Then register it in the pack table so `pack_for(ATSProviderName.HIREBASE)` resolves.

**The `submit` selector is a whitelist, not a hint.** Only a control located through this pack (or
an exact accessible-name match) may ever be clicked. There is no heuristic "first button that
looks like submit" anywhere in this codebase, because that is how an automation clicks "Delete
Account".

Prefer stable attributes over class names, in this order: a purpose-built `data-*` attribute, an
ARIA role or accessible name, an id, a class. Lever's pack leads with `#btn-submit,
[data-qa='btn-submit']` for exactly this reason.

**Order your fallbacks defensively, then verify them against the real page.** A Playwright
locator resolves `.first` in *document* order, not selector order, so a broad fallback like
`button[type='submit']` can win over the specific alternative you listed first. Lever's pack
shipped for months resolving to `#hcaptchaSubmitBtn.hidden` — a zero-size helper button earlier
in the DOM — because of exactly that; it is why the fallback now reads
`button[type='submit']:not(.hidden):not([hidden])`. Add your ATS to
`tests/integration/test_browser_live.py`, which opens a real posting and asserts that the pack's
submit selector resolves to one visible control whose text says "submit". It never clicks.

**`captcha_markers` name a challenge, not a vendor.** Every major ATS loads reCAPTCHA or
hCaptcha in invisible, score-based mode, and matching that bookkeeping sends 100% of
applications to manual review while every unit test stays green. Reuse
`_COMMON_CAPTCHA_MARKERS`; only add a marker that identifies something a human would have to
solve. `BrowserSession._probe_captcha` additionally requires the match to be rendered.

---

## 8. Seed boards

`app/jobs/seeds.py` — a starting point so a fresh install finds something on its first run.

```python
_HIREBASE_BOARDS: Final[tuple[str, ...]] = (
    "acme-robotics",
    "northwind-systems",
    "globex-embedded",
)

DEFAULT_BOARDS: Final[dict[str, list[str]]] = {
    ...
    ATSProviderName.HIREBASE.value: list(_HIREBASE_BOARDS),
}
```

A user who names their own boards in `SearchQuery.extra` gets exactly those and none of these. A
stale token 404s, which `search()` already handles.

**Verify every token against the live API before you commit it, and never guess one.** Board
tokens are not employer names — Anduril publishes as `andurilindustries`, DoorDash as
`doordashusa`, NVIDIA's Workday career site is `NVIDIAExternalCareerSite` — and they go stale
constantly: a 2026-08-09 sweep of the four shipped lists found 46 of 107 tokens returning
nothing, including 28 of 33 on Lever. Add your provider to `scripts/validate_boards.py` and run
it:

```bash
python -m scripts.validate_boards --provider hirebase --tokens acme-robotics,globex-embedded
python -m scripts.validate_boards --provider hirebase        # the whole shipped list
```

It exits non-zero when any token discovers nothing, and `.github/workflows/integration.yml`
runs it nightly. Two traps it exists to catch: a board that answers `200` with an empty array is
indistinguishable from an employer who is not hiring, and on some providers tokens are
case-sensitive (`api.lever.co/v0/postings/Osmind` works, `.../osmind` 404s).

---

## 9. Registration

Built-in providers need nothing beyond living in `app/jobs/` — `app/jobs/__init__.py` imports
every provider module, and `BUILTIN_PLUGIN_MODULES` in `app/plugins/loader.py` imports
`app.jobs`, so the `@plugin` decorator runs.

**A provider shipped as a separate package** declares an entry point instead:

```toml
# in the third-party distribution's pyproject.toml
[project.entry-points."applicantos.providers"]
hirebase = "applicantos_hirebase.provider:HirebaseProvider"
```

`load_all()` reads that group after the built-ins. A third-party plugin that fails to import is
logged and skipped — one broken package must never stop the application from starting.

---

## 10. Tests

Record a real payload once, commit it, and never touch the network in a test.

```python
# tests/fixtures/hirebase_board.json  — captured from the live feed, trimmed to 3 postings


def test_parses_a_real_payload(hirebase_provider, hirebase_fixture):
    postings = [
        hirebase_provider._to_posting("acme-robotics", entry) for entry in hirebase_fixture["jobs"]
    ]
    assert all(p is not None for p in postings)
    first = postings[0]
    assert first.provider is ATSProviderName.HIREBASE
    assert first.external_id and first.title and first.url.startswith("https://hirebase.io/b/")
    assert first.posted_at.tzinfo is not None  # normalised to aware UTC
    assert first.raw  # untouched payload retained


def test_one_bad_entry_does_not_kill_the_batch(hirebase_provider):
    assert hirebase_provider._to_posting("acme-robotics", {"title": "no id"}) is None


def test_url_routing(hirebase_provider):
    assert HirebaseProvider.matches_url("https://hirebase.io/b/acme/jobs/42")
    assert HirebaseProvider.matches_url("https://jobs.example.com/x?hb_jid=42")
    assert not HirebaseProvider.matches_url("https://boards.greenhouse.io/acme/jobs/42")


def test_posture_is_declared():
    import app.jobs.hirebase as module

    assert HirebaseProvider.supports_auto_apply is True
    assert "posture" in (module.__doc__ or "").lower()
```

Add the parsing case to `tests/test_providers.py` and the ToS declaration to
`tests/test_golden_tos.py`.

---

## 11. Verify it

```bash
export SQLITE_MODE=true LLM_PROVIDER=null EMBEDDING_PROVIDER=hashing VECTOR_STORE=memory

# 1. It compiles and imports without Playwright
python -m compileall app/jobs
python -c "import app.jobs; print('ok')"

# 2. The registry resolves it
python -c "
from app.plugins.loader import load_all
from app.jobs.registry import all_providers, get_provider
load_all()
print([p.name.value for p in all_providers()])
p = get_provider('hirebase')
print(p.name, 'auto_apply=', p.supports_auto_apply, 'login=', p.requires_login)"

# 3. URL routing picks the right owner. `provider_for_url` is a coroutine — it may probe the
#    URL — so it has to be awaited. `matches_url` is the synchronous pattern-only test.
python -c "
import asyncio
from app.plugins.loader import load_all
from app.jobs.registry import provider_for_url

async def main():
    load_all()
    for u in ('https://hirebase.io/b/acme/jobs/42',
              'https://boards.greenhouse.io/acme/jobs/123',
              'https://example.com/careers'):
        print(u, '->', await provider_for_url(u))
asyncio.run(main())"

# 4. The selector pack exists and whitelists a submit control
python -c "
from app.browser.selectors import pack_for, pack_names
print(pack_names())
pack = pack_for('hirebase')
assert pack.submit, 'no submit selector — every apply would end in SUBMIT_NOT_FOUND'
print(pack.name, pack.submit)"

# 5. No concrete provider import leaked outside app/jobs/
grep -rn "from app.jobs.hirebase" --include=*.py app/ | grep -v "^app/jobs/" \
  && echo "ISOLATION VIOLATED" || echo "isolation ok"

# 6. Tests and gates
pytest tests/test_providers.py tests/test_dedupe.py tests/test_golden_tos.py -v
ruff check app/jobs app/browser && mypy app
```

### Trying it against the live feed

```bash
python -c "
import asyncio
from app.plugins.loader import load_all
from app.jobs.base import SearchQuery
from app.jobs.registry import get_provider

async def main():
    load_all()
    p = get_provider('hirebase')
    q = SearchQuery(keywords=['embedded'], limit=5)
    async for posting in p.search(q):
        print(f'{posting.title[:60]:60} | {posting.company_name[:20]:20} | {posting.location}')
    await p.aclose()

asyncio.run(main())"
```

### Watching it fill without submitting

The safe way to test the apply path against a real posting is to drive the pipeline with both
switches closed and the browser headed:

```bash
export SQLITE_MODE=true PLAYWRIGHT_HEADLESS=false PLAYWRIGHT_SLOW_MO_MS=400
export DRY_RUN=true AUTO_APPLY_ENABLED=false LOG_LEVEL=DEBUG LOG_JSON=false

python -c "
import asyncio, uuid
from app.config.settings import get_settings
from app.database.session import session_scope
from app.services.pipeline import Pipeline

POSTING_ID = uuid.UUID('...')   # from GET /api/v1/postings
USER_ID    = uuid.UUID('...')

async def main():
    async with session_scope() as s:
        p = Pipeline(s, get_settings())
        print(await p.prepare(POSTING_ID, USER_ID))
        print(await p.submit((await p.prepare(POSTING_ID, USER_ID)).id))
asyncio.run(main())"
```

With both switches closed, `submit` stops at guard rung 5 and returns
`needs_review` / `POLICY_BLOCK` — and if you temporarily set `AUTO_APPLY_ENABLED=true` while
keeping `DRY_RUN=true`, it fills the whole form and `AutoFiller.submit` returns `False`
**without clicking**. You watch it work and nothing is sent.

Never set `DRY_RUN=false` against a real posting unless you actually intend to apply to that job.

---

## Checklist

- [ ] The name is in `ATSProviderName`, mirrored in `types.ts`, and has a migration
- [ ] The module docstring states the ToS posture plainly, and `supports_auto_apply` matches it
- [ ] `search()` yields lazily, honours `q.limit`, and skips a bad posting rather than aborting
- [ ] `fetch_posting()` returns `None` for a closed posting
- [ ] `raw` carries the untouched payload
- [ ] `apply()` delegates to `run_browser_apply` — no hand-rolled form filling
- [ ] A `SelectorPack` exists with a real `submit` whitelist and success markers
- [ ] Seed boards added; a 404 board is skipped, not fatal
- [ ] Errors are typed: 429 → `ProviderRateLimitError`, 401/403 → `ProviderAuthError`,
      404 → `PostingUnavailableError`, forbidden flow → `UnsupportedFlowError`
- [ ] No concrete provider import outside `app/jobs/`
- [ ] The provider never touches the ORM — DTOs and `app.models.enums` only
- [ ] A recorded fixture and a parsing test are committed
- [ ] `ruff check .` and `mypy app` pass

---

## See also

- [`CONTRACTS.md`](CONTRACTS.md) §9 — the binding provider interface
- [`PIPELINE.md`](PIPELINE.md) — where discovery and submission sit
- [`ARCHITECTURE.md`](ARCHITECTURE.md) §5–6 — why DTOs and why the registry
- `.claude/agents/ats-provider-engineer.md` — the agent brief for this work
