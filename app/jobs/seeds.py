"""Curated default board tokens, so discovery works on the very first run.

Greenhouse, Lever, Ashby and Workday are not job *search engines*. Each hosts a separate
careers page per employer, addressed by a short token in the URL — ``boards.greenhouse.io/
stripe``, ``jobs.lever.co/netflix``, ``jobs.ashbyhq.com/linear``,
``nvidia.wd5.myworkdayjobs.com``. There is no global "search all boards" endpoint, so a
provider with no tokens has nowhere to look and a brand-new install would discover exactly
nothing.

This module is the answer to that cold start. It ships a curated list of well-known employers
per provider, used whenever ``SearchQuery.extra`` names none:

    from app.jobs.seeds import boards_from_query

    for token in boards_from_query(self.provider_name, q.extra):
        ...

The lists are a *starting point*, not a product claim. A user who names their own targets
gets exactly those and none of these. Tokens are also not permanent: an employer can migrate
between ATSs or rename its board, at which point the board 404s. That is expected and must
never be an error — :meth:`~app.jobs.base.ATSProvider._request` raises
:class:`~app.jobs.base.PostingUnavailableError` for a missing board, and a provider skips it
and carries on with the rest. A stale token costs one wasted request per poll and nothing
else.

**Every token below was verified against its live API on 2026-08-09** by
``python -m scripts.validate_boards``, and each one returned at least one posting on that
date. Run that script before changing this file and again afterwards; it is the only thing
that can tell a curated list from a plausible one.

The sweep that produced the current lists replaced **11 of 40** Greenhouse tokens, **28 of
33** Lever tokens, **7 of 34** Ashby tokens and **12 of 37** Workday tenants. Lever is the
number worth staring at: five of the shipped tokens still worked, so discovery was not
*visibly* broken — it just spent 28 of every 33 requests on employers that had left, and the
feed a new user saw was a small fraction of what the list implied. Nothing anywhere reported
that, which is what :data:`app.jobs.lever.EVENT_BOARD_EMPTY` and
:attr:`app.services.discovery_service.DiscoveryReport.empty_providers` now exist for.

Two properties of these APIs make staleness hard to see, and are why the check has to be a
live one:

* **An empty board is not an error.** Lever answers ``200`` with ``[]`` for a real board with
  nothing published (``highspot``, ``plaid``), and Ashby does the same (``mercury``). Nothing
  distinguishes that from an employer who simply is not hiring this week, so
  :data:`app.jobs.lever.EVENT_BOARD_EMPTY` exists to at least make it *visible*.
* **Lever tokens are case-sensitive.** ``api.lever.co/v0/postings/Osmind`` answers ``200``
  and ``.../osmind`` answers ``404``. Tokens here are therefore written exactly as the
  employer publishes them, and must not be "tidied" to lowercase.

Workday is the odd one out. Its URLs are ``https://<tenant>.wd<N>.myworkdayjobs.com/<site>``,
where the shard number and the career-site name vary per tenant and cannot be derived from
the tenant token alone. :data:`DEFAULT_BOARDS` therefore carries plain tenant tokens, and the
Workday provider resolves the shard and site itself. Publishing guessed shard numbers here
would be worse than publishing none.
"""

from __future__ import annotations

from typing import Any, Final

import structlog

from app.models.enums import ATSProviderName

__all__ = [
    "DEFAULT_BOARDS",
    "EXTRA_BOARD_KEYS",
    "SEED_API_TEMPLATES",
    "SEED_URL_TEMPLATES",
    "boards_from_query",
    "default_boards",
    "has_defaults",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Board tokens
# ======================================================================================

#: Greenhouse board tokens. The public board lives at ``boards.greenhouse.io/<token>`` and
#: the JSON feed at ``boards-api.greenhouse.io/v1/boards/<token>/jobs``. Greenhouse is the
#: most widely used ATS among US technology employers, which is why this list is the longest.
#:
#: A board token is *not* the employer's name and cannot be derived from it: Anduril
#: publishes as ``andurilindustries``, DoorDash as ``doordashusa``, Sourcegraph as
#: ``sourcegraph91``. Every entry here was confirmed by request, never inferred.
_GREENHOUSE_BOARDS: Final[tuple[str, ...]] = (
    "affirm",
    "airbnb",
    "airtable",
    "amplitude",
    "andurilindustries",
    "anthropic",
    "asana",
    "betterment",
    "brex",
    "carta",
    "checkr",
    "chime",
    "clickhouse",
    "cloudflare",
    "cockroachlabs",
    "coinbase",
    "databricks",
    "datadog",
    "discord",
    "doordashusa",
    "dropbox",
    "duolingo",
    "elastic",
    "faire",
    "figma",
    "flexport",
    "gemini",
    "gitlab",
    "grafanalabs",
    "gusto",
    "instacart",
    "lyft",
    "mongodb",
    "nuro",
    "pinterest",
    "reddit",
    "robinhood",
    "roblox",
    "samsara",
    "scaleai",
    "sofi",
    "sourcegraph91",
    "squarespace",
    "stripe",
    "temporaltechnologies",
    "twilio",
    "vercel",
    "waymo",
    "webflow",
)

#: Lever company tokens. The public board lives at ``jobs.lever.co/<token>`` and the JSON
#: feed at ``api.lever.co/v0/postings/<token>``.
#:
#: **Case matters.** ``Osmind`` and ``SimbeRobotics`` are spelled exactly as their employers
#: publish them; the lowercase forms answer 404. See the module docstring.
#:
#: This is the shortest of the four lists and deliberately so. Lever's install base is much
#: smaller than Greenhouse's and turns over faster — of the 33 tokens shipped before
#: 2026-08-09, 27 had gone and one was empty. Nothing here is a guess; every token was
#: confirmed by request, and the list is short because that is how many survived.
_LEVER_COMPANIES: Final[tuple[str, ...]] = (
    "360learning",
    "aircall",
    "aledade",
    "anchorage",
    "binance",
    "contentsquare",
    "disqo",
    "duetti",
    "equativ",
    "gopuff",
    "greenlight",
    "hive",
    "kpler",
    "ledger",
    "matchgroup",
    "Osmind",
    "palantir",
    "pigment",
    "qonto",
    "scaleway",
    "shieldai",
    "SimbeRobotics",
    "sonatype",
    "spotify",
    "spreetail",
    "swissborg",
    "sysdig",
    "veeva",
    "xsolla",
    "zopa",
)

#: Ashby organisation tokens. The public board lives at ``jobs.ashbyhq.com/<token>`` and the
#: JSON feed at ``api.ashbyhq.com/posting-api/job-board/<token>``. Ashby skews heavily towards
#: recently founded AI and developer-tools companies, which is exactly where a lot of current
#: hiring is.
#:
#: As with Greenhouse, the token is the board's name and not the company's: Anysphere
#: publishes as ``cursor`` and Zed Industries as ``zed``.
_ASHBY_BOARDS: Final[tuple[str, ...]] = (
    "abridge",
    "attio",
    "baseten",
    "browserbase",
    "cartesia",
    "cerebras",
    "character",
    "cognition",
    "cohere",
    "crusoe",
    "cursor",
    "decagon",
    "elevenlabs",
    "gamma",
    "granola",
    "harvey",
    "hex",
    "langchain",
    "linear",
    "lovable",
    "mistral.ai",
    "modal",
    "neon",
    "notion",
    "openai",
    "openevidence",
    "pinecone",
    "posthog",
    "railway",
    "ramp",
    "replit",
    "runway",
    "sesame",
    "sierra",
    "suno",
    "supabase",
    "vanta",
    "warp",
    "writer",
    "zed",
)

#: Workday tenant tokens. See the module docstring: the shard (``wd1`` … ``wd12``) and the
#: career-site path vary per tenant and are resolved by the Workday provider, so only the
#: tenant appears here. Workday is where large enterprises hire, which makes it the right
#: complement to the startup-heavy lists above — even though it is discovery-only, since its
#: account-gated multi-step flow routes every application to manual review
#: (``docs/CONTRACTS.md`` §9).
#:
#: Two kinds of token were removed in the 2026-08-09 sweep and are worth telling apart.
#: ``walmart`` answers ``410 ERR_TENANT_MIGRATED`` — it has left this address entirely.
#: ``nike``'s ``robots.txt`` allows nothing and disallows its three real boards, so the
#: provider declines to poll it even though the CXS API would answer
#: (:func:`app.jobs.workday.career_sites_from_robots`, golden rule #10). The rest — ``amd``,
#: ``ford``, ``qualcomm`` and friends — simply are not on a ``myworkdayjobs.com`` or
#: ``myworkdaysite.com`` host under that tenant token any more.
_WORKDAY_TENANTS: Final[tuple[str, ...]] = (
    "3m",
    "accenture",
    "adobe",
    "amat",
    "analogdevices",
    "autodesk",
    "blackrock",
    "broadcom",
    "capitalone",
    "chevron",
    "cisco",
    "comcast",
    "cvshealth",
    "disney",
    "dow",
    "emerson",
    "gilead",
    "hpe",
    "humana",
    "intel",
    "marvell",
    "mastercard",
    "medtronic",
    "micron",
    "nvidia",
    "nxp",
    "paypal",
    "pfizer",
    "philips",
    "pnc",
    "rockwellautomation",
    "salesforce",
    "statestreet",
    "stryker",
    "target",
    "unilever",
    "workday",
)


#: Default board tokens per provider, keyed by :class:`~app.models.enums.ATSProviderName`
#: string value.
#:
#: Treat as read-only. :func:`default_boards` hands out copies precisely so that a provider
#: which filters or shuffles its working list cannot corrupt the defaults for the next
#: discovery run in the same process.
#:
#: LinkedIn and ``manual`` are absent on purpose. LinkedIn discovery is limited to a
#: user-supplied export or a public feed and has no notion of a board token (golden rule
#: #10); ``manual`` postings are entered by hand.
DEFAULT_BOARDS: Final[dict[str, list[str]]] = {
    ATSProviderName.GREENHOUSE.value: list(_GREENHOUSE_BOARDS),
    ATSProviderName.LEVER.value: list(_LEVER_COMPANIES),
    ATSProviderName.ASHBY.value: list(_ASHBY_BOARDS),
    ATSProviderName.WORKDAY.value: list(_WORKDAY_TENANTS),
}


# ======================================================================================
# URL shapes
# ======================================================================================

#: Human-facing board URL per provider, as a ``str.format`` template taking ``token``.
#: Documented here so that every provider derives its URLs from one place and a reader can
#: see what a seed token actually means.
SEED_URL_TEMPLATES: Final[dict[str, str]] = {
    ATSProviderName.GREENHOUSE.value: "https://boards.greenhouse.io/{token}",
    ATSProviderName.LEVER.value: "https://jobs.lever.co/{token}",
    ATSProviderName.ASHBY.value: "https://jobs.ashbyhq.com/{token}",
    # The shard and career-site path are tenant-specific; the provider completes this.
    ATSProviderName.WORKDAY.value: "https://{token}.myworkdayjobs.com",
}

#: Public JSON feed per provider, as a ``str.format`` template taking ``token``. Workday has
#: no shard-independent feed URL, so it is absent rather than approximated.
SEED_API_TEMPLATES: Final[dict[str, str]] = {
    ATSProviderName.GREENHOUSE.value: ("https://boards-api.greenhouse.io/v1/boards/{token}/jobs"),
    ATSProviderName.LEVER.value: "https://api.lever.co/v0/postings/{token}",
    ATSProviderName.ASHBY.value: ("https://api.ashbyhq.com/posting-api/job-board/{token}"),
}


# ======================================================================================
# Accessors
# ======================================================================================

#: Keys read from ``SearchQuery.extra`` when looking for caller-supplied tokens, in order.
#: Several spellings are accepted because the natural word differs per provider — Greenhouse
#: and Ashby have "boards", Lever has "companies", Workday has "tenants" — and a caller
#: should not have to remember which.
EXTRA_BOARD_KEYS: Final[tuple[str, ...]] = (
    "boards",
    "board_tokens",
    "companies",
    "tenants",
    "orgs",
)


def _provider_key(provider: ATSProviderName | str) -> str:
    """Reduce a provider identifier to the string :data:`DEFAULT_BOARDS` is keyed by.

    Args:
        provider: An :class:`~app.models.enums.ATSProviderName` or its string value.

    Returns:
        The lowercased provider name.
    """
    if isinstance(provider, ATSProviderName):
        return provider.value
    return str(provider).strip().lower()


def _clean_tokens(values: Any) -> list[str]:
    """Normalise a caller-supplied token collection.

    Args:
        values: A string (one token, or a comma-separated list of them), or any iterable of
            strings. Anything else yields ``[]``.

    Returns:
        Trimmed, de-duplicated, non-empty tokens in their original order.
    """
    if values is None:
        return []
    if isinstance(values, str):
        candidates: list[Any] = values.split(",")
    elif isinstance(values, (list, tuple, set, frozenset)):
        candidates = list(values)
    else:
        return []

    cleaned: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        token = candidate.strip().strip("/")
        if not token or token.lower() in seen:
            continue
        seen.add(token.lower())
        cleaned.append(token)
    return cleaned


def default_boards(provider: ATSProviderName | str) -> list[str]:
    """Return the curated default tokens for *provider*.

    Args:
        provider: The provider to look up.

    Returns:
        A fresh list, safe for the caller to filter, shuffle or extend. Empty for a provider
        that has no notion of a board — LinkedIn and ``manual``.
    """
    return list(DEFAULT_BOARDS.get(_provider_key(provider), ()))


def has_defaults(provider: ATSProviderName | str) -> bool:
    """Return whether *provider* ships curated default tokens.

    Args:
        provider: The provider to look up.

    Returns:
        ``True`` when :func:`default_boards` would return a non-empty list.
    """
    return bool(DEFAULT_BOARDS.get(_provider_key(provider)))


def boards_from_query(
    provider: ATSProviderName | str,
    extra: dict[str, Any] | None = None,
) -> list[str]:
    """Resolve which boards to poll for one discovery run.

    The precedence a provider should rely on: whatever the caller named in
    ``SearchQuery.extra`` wins outright, and the curated defaults apply only when the caller
    named nothing. A user who has listed their target employers must never also be polled
    against this module's opinions — that would spend their rate-limit budget on jobs they
    did not ask for.

    Both a per-provider key (``extra["greenhouse"]``) and a generic one
    (``extra["boards"]``, ``extra["companies"]``, …) are honoured, so a single query can
    carry different tokens for different providers::

        SearchQuery(extra={"greenhouse": ["acme"], "lever": ["globex"]})

    Args:
        provider: The provider about to run.
        extra: The query's ``extra`` mapping, or ``None``.

    Returns:
        The tokens to poll, de-duplicated and in order. Empty only when the caller named
        nothing and the provider ships no defaults.
    """
    key = _provider_key(provider)
    payload = extra if isinstance(extra, dict) else {}

    supplied = _clean_tokens(payload.get(key))
    if not supplied:
        for generic_key in EXTRA_BOARD_KEYS:
            supplied = _clean_tokens(payload.get(generic_key))
            if supplied:
                break

    if supplied:
        logger.debug("seeds.boards_from_query", provider=key, count=len(supplied), source="query")
        return supplied

    defaults = default_boards(key)
    logger.debug("seeds.boards_from_query", provider=key, count=len(defaults), source="defaults")
    return defaults
