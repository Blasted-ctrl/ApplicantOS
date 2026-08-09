"""GitHub analyzer (``analyzer`` plugin ``github``) — code the user already wrote is knowledge.

This is the analyzer that makes the product's central claim true: *"the next tailored resume
automatically knows about the work you added yesterday."* It reads a GitHub account (or a
single repository) through the REST v3 API and turns it into the five-part
:class:`~app.knowledge.analyzers.base.AnalysisResult` every analyzer returns — one document
per repository, a ``project`` node per repository, ``technology``/``skill`` nodes for every
language and declared dependency, the edges between them, and facts recovered from each
README.

**Three things make it cheap enough to run on a schedule.**

*Fingerprinting.* :meth:`GitHubAnalyzer.fingerprint` digests the profile's ``updated_at``
together with every selected repository's ``pushed_at``. Pushing one commit changes exactly
one ``pushed_at``, so the digest changes and the source is re-indexed; touching nothing
leaves the digest identical and the whole run is skipped. :meth:`GitHubAnalyzer.analyze`
composes the *same* digest from the *same* inputs through :func:`_compose_fingerprint`, so
the probe and the analysis can never disagree — a disagreement would silently re-index the
entire corpus on every pass.

*Conditional requests.* Every API response is cached under
:data:`~app.cache.keys.NAMESPACES.GITHUB` together with its ``ETag``, and every request
replays that ``ETag`` in ``If-None-Match``. GitHub answers ``304 Not Modified`` — which it
does **not** charge against the rate limit — and the cached body is used. An unchanged
account therefore costs almost no quota at all.

*Degradation.* One repository that 500s, or whose README has been deleted, or whose manifest
does not parse, is recorded in :attr:`~app.knowledge.analyzers.base.AnalysisResult.errors`
and the run continues. Only a rate-limit wall, a 404 on the account itself, or a rejected
credential ends the run, and each maps to the specific error the operator can act on.

**Credentials are optional.** With no ``GITHUB_TOKEN`` the analyzer works unauthenticated at
GitHub's 60 requests/hour, which is exactly why the ``ETag`` cache above is not a nicety.
With a token the same code path gets 5 000/hour and can see private repositories.
"""

from __future__ import annotations

import asyncio
import base64
import configparser
import datetime as dt
import json
import math
import re
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Final
from urllib.parse import quote, urlsplit

import structlog

from app.cache.keys import NAMESPACES, make_key
from app.knowledge.analyzers._text import html_to_text
from app.knowledge.analyzers.base import (
    MAX_IMPACT_SCORE,
    AnalysisResult,
    Analyzer,
    AnalyzerError,
    ExtractedDocument,
    ExtractedEdge,
    ExtractedEntity,
    ExtractedFact,
    SourceAccessDenied,
    SourceRef,
    SourceUnavailableError,
    compute_fingerprint,
    http_client,
)
from app.knowledge.extractors import (
    KnowledgeExtractor,
    canonical_skill,
    skill_entity_kind,
)
from app.models.enums import EntityKind, FactKind, PluginKind, RelationKind, SourceKind
from app.plugins import PluginMeta, plugin

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from collections.abc import Callable

    import httpx

    from app.cache.base import Cache
    from app.config.settings import Settings

__all__ = [
    "API_ROOT",
    "MANIFEST_FILES",
    "STAR_IMPACT_MAX_BONUS",
    "STAR_IMPACT_SCALE",
    "GitHubAnalyzer",
    "star_impact_bonus",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: Base URL of the GitHub REST v3 API.
API_ROOT: Final[str] = "https://api.github.com"

#: Media type requesting the documented JSON representation. GitHub's versioning policy
#: makes this the stable choice; ``application/json`` is an undocumented alias.
JSON_ACCEPT: Final[str] = "application/vnd.github+json"

#: Media type requesting a file's bytes rather than a JSON envelope around base64. Used for
#: READMEs and dependency manifests, which are text and are wanted as text.
RAW_ACCEPT: Final[str] = "application/vnd.github.raw"

#: REST API version header. Pinning it means a future default-version bump cannot silently
#: change the field names this module reads.
API_VERSION: Final[str] = "2022-11-28"

#: Page size for the repository listing. 100 is GitHub's maximum, so this is the fewest
#: requests the listing can possibly take.
REPOS_PER_PAGE: Final[int] = 100

#: Hard ceiling on listing pages, independent of ``github_max_repos``. An account with three
#: thousand forks must not be able to turn one index run into thirty requests of nothing;
#: the selection is ``pushed_at``-descending, so the newest work is on the first pages
#: anyway.
MAX_REPO_PAGES: Final[int] = 20

#: Attempts per request, including the first. Only transport failures and 5xx responses are
#: retried: a 4xx is an answer, not a hiccup.
MAX_ATTEMPTS: Final[int] = 3

#: Backoff before retry *n*, in seconds.
RETRY_BACKOFF_SECONDS: Final[tuple[float, ...]] = (0.5, 1.5)

#: Lifetime of a cached API response. Long, because the entry's job is to remember an
#: ``ETag``: the request is still made, GitHub still decides whether the body changed, and
#: a stale body can therefore never be served.
CACHE_TTL_SECONDS: Final[int] = 7 * 24 * 60 * 60

#: Cache-key discriminator, so a future change to the stored envelope's shape can be rolled
#: out by bumping one constant instead of clearing the whole namespace.
CACHE_TAG: Final[str] = "github.rest.v1"

#: Domain-separation tag for this analyzer's fingerprints.
FINGERPRINT_TAG: Final[str] = "analyzer.github.v1"

#: Dependency manifests, in the order they are tried. At most **one** is fetched per
#: repository: the root directory listing says which exist, and the first match wins. The
#: order runs from the most declarative (a lock-free manifest naming direct dependencies) to
#: the least (a build script, from which only linked package names can be recovered).
MANIFEST_FILES: Final[tuple[str, ...]] = (
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "Cargo.toml",
    "go.mod",
    "platformio.ini",
    "CMakeLists.txt",
)

#: Largest manifest fetched. Anything bigger is generated, and a generated manifest lists
#: transitive dependencies that say nothing about what the author chose.
MAX_MANIFEST_BYTES: Final[int] = 256_000

#: Largest README kept. Beyond this a README is a book, and the chunker upstream of the
#: embedder is the component that should be seeing it.
MAX_README_CHARS: Final[int] = 200_000

#: Most dependency names carried into the document header and metadata.
MAX_DEPENDENCIES: Final[int] = 60

#: Points added to a fact's impact per decade of stars, and the ceiling on that bonus.
#:
#: ``bonus = min(STAR_IMPACT_MAX_BONUS, round(STAR_IMPACT_SCALE * log10(1 + stars)))``
#:
#: Logarithmic because stars are: the gap between 0 and 10 stars is the gap between "nobody
#: saw it" and "people found it", while the gap between 1 000 and 1 010 is noise. Concretely
#: 0 stars adds 0, 9 adds 10, 99 adds 20, and 999 or more adds the capped 30 — a third of
#: the scale, enough to lift a well-known project's bullets above an unknown project's
#: without ever letting popularity outrank a quantified outcome.
STAR_IMPACT_SCALE: Final[int] = 10
STAR_IMPACT_MAX_BONUS: Final[int] = 30

#: Edge weights, which rank graph expansion during retrieval. A repository's primary
#: language is the strongest statement about what it is built with; a name that merely
#: appears in the README is the weakest.
EDGE_WEIGHT_PRIMARY_LANGUAGE: Final[float] = 2.0
EDGE_WEIGHT_LANGUAGE: Final[float] = 1.5
EDGE_WEIGHT_DEPENDENCY: Final[float] = 1.0
EDGE_WEIGHT_README: Final[float] = 1.0

#: Confidence for a node whose existence the API stated outright.
API_CONFIDENCE: Final[float] = 0.9

#: Confidence for a technology inferred from a declared dependency. Lower than
#: :data:`API_CONFIDENCE`: a dependency in a manifest may be one line of a tutorial the
#: author never returned to.
DEPENDENCY_CONFIDENCE: Final[float] = 0.7

#: A GitHub login: alphanumeric with single interior hyphens, at most 39 characters.
LOGIN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$"
)

#: A GitHub repository name.
REPO_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

#: Hosts whose URLs this analyzer understands.
GITHUB_HOSTS: Final[frozenset[str]] = frozenset(
    {"github.com", "www.github.com", "api.github.com", "raw.githubusercontent.com"}
)

#: URL path segments that introduce a sub-resource of a repository rather than another
#: repository, so ``/owner/repo/tree/main/src`` still resolves to ``owner/repo``.
REPO_SUBPATHS: Final[frozenset[str]] = frozenset(
    {
        "tree",
        "blob",
        "commits",
        "commit",
        "releases",
        "issues",
        "pull",
        "pulls",
        "actions",
        "wiki",
        "settings",
        "branches",
        "tags",
        "graphs",
        "network",
        "pkgs",
    }
)

#: Path prefixes used by the API form of a profile or repository URL.
API_PATH_PREFIXES: Final[frozenset[str]] = frozenset({"users", "repos", "orgs"})

#: Bytes per kilobyte and per megabyte, for the human-readable repository size.
BYTES_PER_KB: Final[int] = 1024

#: Header names read off every response.
RATE_LIMIT_REMAINING_HEADER: Final[str] = "x-ratelimit-remaining"
RATE_LIMIT_RESET_HEADER: Final[str] = "x-ratelimit-reset"
ETAG_HEADER: Final[str] = "etag"

#: Response headers worth remembering alongside a cached body.
CACHED_HEADERS: Final[tuple[str, ...]] = ("content-type", "last-modified", "link")

#: Markers that identify a README written in HTML rather than markdown.
_HTML_MARKERS: Final[tuple[str, ...]] = ("<!doctype html", "<html", "<body", "<div", "<p>")


# ======================================================================================
# Errors
# ======================================================================================


class _RateLimited(SourceUnavailableError):
    """The GitHub API rate limit is exhausted.

    A private subclass of :class:`~app.knowledge.analyzers.base.SourceUnavailableError` so
    that callers outside this module see exactly the contract-defined error, while the
    per-repository loop inside it can re-raise this one case instead of degrading it: once
    the quota is gone, continuing would produce a hundred identical failures and hammer an
    endpoint that has already said no.
    """


# ======================================================================================
# Source parsing
# ======================================================================================


@dataclass(frozen=True, slots=True)
class _Target:
    """What a source URI resolves to.

    Attributes:
        owner: The account login.
        repo: The repository name, or ``None`` when the URI names only an account.
    """

    owner: str
    repo: str | None = None

    @property
    def full_name(self) -> str:
        """``owner/repo`` when a repository is named, otherwise just the owner."""
        return f"{self.owner}/{self.repo}" if self.repo else self.owner


def _clean_repo_name(value: str) -> str | None:
    """Strip the decoration a repository name picks up in URLs.

    Args:
        value: A path segment that should be a repository name.

    Returns:
        The bare name, or ``None`` when it is not a legal repository name.
    """
    name = value.strip()
    name = name.removesuffix(".git")
    return name if REPO_PATTERN.match(name) else None


def _target_from_segments(segments: list[str], *, allow_api_prefix: bool) -> _Target | None:
    """Resolve a URL path's segments to an account and possibly a repository.

    Args:
        segments: Non-empty path segments, in order.
        allow_api_prefix: Whether a leading ``users``/``repos``/``orgs`` segment should be
            treated as API routing rather than as the account name. True only for a real
            URL, so that the shorthand ``users/thing`` is still read as an ``owner/repo``
            pair — ``users`` is itself a legal login.

    Returns:
        The target, or ``None`` when the first segment is not a legal login.
    """
    if not segments:
        return None
    if allow_api_prefix and segments[0] in API_PATH_PREFIXES:
        segments = segments[1:]
        if not segments:
            return None
    owner = segments[0].strip()
    if not LOGIN_PATTERN.match(owner):
        return None
    if len(segments) < 2:
        return _Target(owner=owner)
    if segments[1] in REPO_SUBPATHS:
        return _Target(owner=owner)
    return _Target(owner=owner, repo=_clean_repo_name(segments[1]))


def parse_target(uri: str) -> _Target | None:
    """Resolve any of the forms a user might paste into an account and repository.

    Accepts a bare login (``octocat``, ``@octocat``), the ``owner/repo`` shorthand, a
    profile URL, a repository URL with or without ``.git``, a deep link into a repository
    (``/owner/repo/tree/main/src``), and the API forms
    (``api.github.com/users/octocat``, ``api.github.com/repos/octocat/Hello-World``).

    Args:
        uri: The source URI exactly as configured.

    Returns:
        The parsed target, or ``None`` when nothing legal can be read out of *uri*.
    """
    candidate = (uri or "").strip().strip("<>").rstrip("/")
    if not candidate:
        return None
    candidate = candidate.removeprefix("@")
    if candidate.lower().startswith("git@github.com:"):
        candidate = candidate.split(":", 1)[1]

    head = candidate.split("/", 1)[0].lower()
    if "://" in candidate or head in GITHUB_HOSTS:
        url = candidate if "://" in candidate else f"https://{candidate}"
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        if (parts.hostname or "").lower() not in GITHUB_HOSTS:
            return None
        segments = [segment for segment in parts.path.split("/") if segment]
        return _target_from_segments(segments, allow_api_prefix=True)

    segments = [segment for segment in candidate.split("/") if segment]
    if len(segments) > 2:
        return None
    return _target_from_segments(segments, allow_api_prefix=False)


# ======================================================================================
# Manifest parsing
# ======================================================================================

#: Splits a PEP 508 requirement at the first character that ends the distribution name.
_REQUIREMENT_NAME: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")

#: ``require`` lines and blocks in a ``go.mod``.
_GO_REQUIRE_BLOCK: Final[re.Pattern[str]] = re.compile(
    r"^\s*require\s*\(([^)]*)\)", re.MULTILINE | re.DOTALL
)
#: Only horizontal whitespace between the tokens, so the ``require (`` that opens a block
#: cannot be mistaken for a single-line ``require <module> <version>``.
_GO_REQUIRE_LINE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*require[ \t]+(\S+)[ \t]+\S+", re.MULTILINE
)

#: A Go module path's trailing major-version segment, which is not part of its name.
_GO_MAJOR_SUFFIX: Final[re.Pattern[str]] = re.compile(r"^v\d+$")

#: CMake calls that name an external package.
_CMAKE_PACKAGE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:find_package|FetchContent_Declare|pkg_check_modules)\s*\(\s*([A-Za-z0-9_.:+-]+)",
    re.IGNORECASE,
)

#: The ``LANGUAGES`` clause of a CMake ``project()`` call.
_CMAKE_LANGUAGES: Final[re.Pattern[str]] = re.compile(
    r"\bproject\s*\([^)]*?\bLANGUAGES\s+([A-Za-z0-9 \t]+)", re.IGNORECASE | re.DOTALL
)

#: CMake language tokens that have a different name in the skill vocabulary.
_CMAKE_LANGUAGE_ALIASES: Final[dict[str, str]] = {"CXX": "C++", "ASM": "Assembly"}

#: ``platformio.ini`` keys that name a technology.
_PLATFORMIO_KEYS: Final[tuple[str, ...]] = ("platform", "framework", "board", "lib_deps")


def _requirement_name(specifier: str) -> str | None:
    """Return the distribution name at the head of a PEP 508 requirement.

    Args:
        specifier: A requirement such as ``"pydantic[email]>=2,<3 ; python_version>='3.11'"``.

    Returns:
        ``"pydantic"``, or ``None`` when the line names nothing.
    """
    match = _REQUIREMENT_NAME.match(specifier.strip())
    return match.group(0) if match else None


def _parse_package_json(text: str) -> list[str]:
    """Return the direct dependencies declared by a ``package.json``.

    Args:
        text: The manifest's contents.

    Returns:
        Dependency names from ``dependencies``, ``devDependencies`` and
        ``peerDependencies``.
    """
    payload = json.loads(text)
    if not isinstance(payload, dict):
        return []
    names: list[str] = []
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        block = payload.get(section)
        if isinstance(block, dict):
            names.extend(str(name) for name in block)
    return names


def _parse_pyproject(text: str) -> list[str]:
    """Return the direct dependencies declared by a ``pyproject.toml``.

    Covers PEP 621 (``project.dependencies`` and ``project.optional-dependencies``), PEP 735
    (``dependency-groups``) and Poetry (``tool.poetry.dependencies``).

    Args:
        text: The manifest's contents.

    Returns:
        Distribution names, with version specifiers and extras removed.
    """
    payload = tomllib.loads(text)
    specifiers: list[str] = []

    project = payload.get("project")
    if isinstance(project, dict):
        declared = project.get("dependencies")
        if isinstance(declared, list):
            specifiers.extend(str(item) for item in declared)
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group in optional.values():
                if isinstance(group, list):
                    specifiers.extend(str(item) for item in group)

    groups = payload.get("dependency-groups")
    if isinstance(groups, dict):
        for group in groups.values():
            if isinstance(group, list):
                specifiers.extend(str(item) for item in group if isinstance(item, str))

    poetry = payload.get("tool", {})
    poetry = poetry.get("poetry", {}) if isinstance(poetry, dict) else {}
    if isinstance(poetry, dict):
        declared = poetry.get("dependencies")
        if isinstance(declared, dict):
            specifiers.extend(name for name in declared if name != "python")

    names = [_requirement_name(specifier) for specifier in specifiers]
    return [name for name in names if name]


def _parse_requirements(text: str) -> list[str]:
    """Return the distributions named by a ``requirements.txt``.

    Args:
        text: The file's contents.

    Returns:
        Distribution names. Options (``-r``, ``-e``, ``--index-url``), comments, blank lines
        and bare URLs are skipped.
    """
    names: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-") or "://" in line:
            continue
        name = _requirement_name(line)
        if name:
            names.append(name)
    return names


def _parse_cargo(text: str) -> list[str]:
    """Return the crates a ``Cargo.toml`` depends on.

    Args:
        text: The manifest's contents.

    Returns:
        Crate names from ``dependencies``, ``dev-dependencies`` and ``build-dependencies``.
    """
    payload = tomllib.loads(text)
    names: list[str] = []
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        block = payload.get(section)
        if isinstance(block, dict):
            names.extend(str(name) for name in block)
    return names


def _parse_go_mod(text: str) -> list[str]:
    """Return the modules a ``go.mod`` requires.

    Args:
        text: The file's contents.

    Returns:
        The final path segment of each required module (``github.com/gin-gonic/gin`` becomes
        ``gin``), which is the form that has any chance of matching a technology name.
    """
    paths: list[str] = []
    for block in _GO_REQUIRE_BLOCK.findall(text):
        for line in block.splitlines():
            cleaned = line.split("//", 1)[0].strip()
            if cleaned:
                paths.append(cleaned.split()[0])
    paths.extend(_GO_REQUIRE_LINE.findall(text))

    names: list[str] = []
    for path in paths:
        segments = [segment for segment in path.strip('"').split("/") if segment]
        while segments and _GO_MAJOR_SUFFIX.match(segments[-1]):
            segments.pop()
        if segments:
            names.append(segments[-1])
    return names


def _parse_platformio(text: str) -> list[str]:
    """Return the platforms, frameworks, boards and libraries a ``platformio.ini`` declares.

    Args:
        text: The file's contents.

    Returns:
        The declared names, one per entry of a multi-line ``lib_deps`` list.
    """
    # ``interpolation=None`` because a PlatformIO value legitimately contains ``%`` and
    # ``${}``, which the default interpolation would reject as a syntax error.
    parser = configparser.ConfigParser(
        strict=False, inline_comment_prefixes=(";", "#"), interpolation=None
    )
    parser.read_string(text)
    names: list[str] = []
    for section in parser.sections():
        for key in _PLATFORMIO_KEYS:
            value = parser.get(section, key, fallback="")
            for entry in value.replace(",", "\n").splitlines():
                cleaned = entry.strip()
                if not cleaned or "://" in cleaned:
                    continue
                # "bblanchon/ArduinoJson@^6.21" -> "ArduinoJson"
                cleaned = cleaned.split("@", 1)[0].split("/")[-1].strip()
                if cleaned:
                    names.append(cleaned)
    return names


def _parse_cmake(text: str) -> list[str]:
    """Return the packages and languages a ``CMakeLists.txt`` names.

    Args:
        text: The file's contents.

    Returns:
        Every ``find_package``/``FetchContent_Declare``/``pkg_check_modules`` target, plus
        the languages of the ``project()`` call with CMake's spellings mapped onto the
        vocabulary's (``CXX`` becomes ``C++``).
    """
    names = [match.split("::")[0] for match in _CMAKE_PACKAGE.findall(text)]
    for clause in _CMAKE_LANGUAGES.findall(text):
        for token in clause.split():
            names.append(_CMAKE_LANGUAGE_ALIASES.get(token.upper(), token))
    return names


#: Manifest filename to the function that reads it. Keyed by the names in
#: :data:`MANIFEST_FILES`, which is also the priority order.
MANIFEST_PARSERS: Final[dict[str, Callable[[str], list[str]]]] = {
    "package.json": _parse_package_json,
    "pyproject.toml": _parse_pyproject,
    "requirements.txt": _parse_requirements,
    "Cargo.toml": _parse_cargo,
    "go.mod": _parse_go_mod,
    "platformio.ini": _parse_platformio,
    "CMakeLists.txt": _parse_cmake,
}


def parse_manifest(filename: str, text: str) -> list[str]:
    """Read the dependencies out of one manifest, never raising.

    Args:
        filename: One of :data:`MANIFEST_FILES`.
        text: The manifest's contents.

    Returns:
        Declared dependency names, deduplicated case-insensitively and order-stable. Empty
        when the file is unknown, malformed, or declares nothing — a broken manifest is one
        repository's missing detail, never a failed index run.
    """
    parser = MANIFEST_PARSERS.get(filename)
    if parser is None or not text.strip():
        return []
    try:
        raw = parser(text)
    except Exception as exc:
        logger.debug("github.manifest_unparsed", manifest=filename, error=str(exc))
        return []

    names: list[str] = []
    seen: set[str] = set()
    for name in raw:
        cleaned = str(name).strip().strip("\"'")
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        names.append(cleaned)
    return names[:MAX_DEPENDENCIES]


# ======================================================================================
# Small helpers
# ======================================================================================


def star_impact_bonus(stars: int) -> int:
    """Return the impact points a repository's star count is worth.

    See :data:`STAR_IMPACT_SCALE` for the formula and the reasoning behind its shape.

    Args:
        stars: The repository's stargazer count.

    Returns:
        An integer in ``[0, STAR_IMPACT_MAX_BONUS]``; zero for a repository with no stars
        and for any negative or unusable input.
    """
    try:
        count = int(stars)
    except (TypeError, ValueError):
        return 0
    if count <= 0:
        return 0
    return min(STAR_IMPACT_MAX_BONUS, round(STAR_IMPACT_SCALE * math.log10(1 + count)))


def _looks_like_html(text: str) -> bool:
    """Return whether a README is HTML rather than markdown or plain text.

    Args:
        text: The README's first characters onwards.

    Returns:
        ``True`` when the document opens with markup. A markdown README containing a stray
        ``<img>`` badge does not qualify, because the check is anchored at the start.
    """
    head = text.lstrip()[:200].casefold()
    return head.startswith(_HTML_MARKERS)


def _format_size(size_kb: Any) -> str:
    """Render a repository's size, which GitHub reports in kilobytes.

    Args:
        size_kb: The ``size`` field of a repository payload.

    Returns:
        A human-readable size such as ``"4.2 MB"``, or ``"unknown"``.
    """
    try:
        kilobytes = int(size_kb)
    except (TypeError, ValueError):
        return "unknown"
    if kilobytes < BYTES_PER_KB:
        return f"{kilobytes} KB"
    return f"{kilobytes / BYTES_PER_KB:.1f} MB"


def _date_only(timestamp: Any) -> str:
    """Reduce an ISO-8601 API timestamp to its date.

    Args:
        timestamp: A value such as ``"2024-05-05T09:12:44Z"``.

    Returns:
        ``"2024-05-05"``, or the empty string when *timestamp* is not a string.
    """
    return timestamp[:10] if isinstance(timestamp, str) and len(timestamp) >= 10 else ""


def _license_name(repo: dict[str, Any]) -> str:
    """Return a repository's licence, in the most recognisable form available.

    Args:
        repo: A repository payload.

    Returns:
        The SPDX identifier, else the licence's display name, else the empty string.
    """
    licence = repo.get("license")
    if not isinstance(licence, dict):
        return ""
    identifier = licence.get("spdx_id")
    if isinstance(identifier, str) and identifier and identifier != "NOASSERTION":
        return identifier
    name = licence.get("name")
    return name if isinstance(name, str) else ""


def _language_shares(languages: dict[str, Any]) -> list[tuple[str, int, float]]:
    """Turn the byte histogram into ranked shares.

    Args:
        languages: The ``/languages`` payload — language name to bytes.

    Returns:
        ``(language, bytes, percentage)`` triples, largest first.
    """
    counted = [
        (str(name), int(count))
        for name, count in languages.items()
        if isinstance(count, (int, float)) and count > 0
    ]
    total = sum(count for _, count in counted)
    if total <= 0:
        return []
    ranked = sorted(counted, key=lambda item: (-item[1], item[0]))
    return [(name, count, 100.0 * count / total) for name, count in ranked]


def _entity_for_name(name: str) -> tuple[EntityKind, str]:
    """Map a raw technology name onto a graph node identity.

    Args:
        name: A language, dependency or free-text technology name.

    Returns:
        ``(kind, canonical_name)``. A name the vocabulary recognises is canonicalised and
        classified as a discipline or a technology; anything else — ``"Jupyter Notebook"``,
        ``"ArduinoJson"`` — is kept verbatim as a technology, because a language GitHub
        detected is a fact about the repository whether or not the vocabulary knows it.
    """
    canonical = canonical_skill(name)
    if canonical is None:
        return (EntityKind.TECHNOLOGY, name.strip())
    return (skill_entity_kind(canonical), canonical)


# ======================================================================================
# HTTP plumbing
# ======================================================================================


@dataclass(slots=True)
class _ApiResponse:
    """One answer from the GitHub API, whether it came from the network or the cache.

    Attributes:
        status: The HTTP status. A cache hit reports ``200``, not ``304``: the caller cares
            that it has a body, not how it got one.
        text: The response body as text.
        headers: The response headers worth keeping (see :data:`CACHED_HEADERS`).
        from_cache: Whether the body came from a cached entry revalidated with ``ETag``.
    """

    status: int
    text: str
    headers: dict[str, str]
    from_cache: bool = False

    def json(self) -> Any:
        """Parse the body as JSON.

        Returns:
            The decoded payload, or ``None`` when the body is not JSON — which is the normal
            case for a raw README.
        """
        if not self.text.strip():
            return None
        try:
            return json.loads(self.text)
        except (ValueError, TypeError):
            return None


def _reset_time(headers: Any) -> str:
    """Render the moment the rate limit refills.

    Args:
        headers: Response headers.

    Returns:
        An ISO-8601 UTC timestamp, or ``"an unknown time"`` when GitHub sent no reset
        header — the message must stay useful either way, because it is what the operator
        reads on ``KnowledgeSource.last_error``.
    """
    raw = headers.get(RATE_LIMIT_RESET_HEADER)
    try:
        moment = dt.datetime.fromtimestamp(int(raw), tz=dt.UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return "an unknown time"
    return moment.isoformat(timespec="seconds")


# ======================================================================================
# The analyzer
# ======================================================================================


@plugin
class GitHubAnalyzer(Analyzer):
    """Turns a GitHub account or repository into documents, facts, entities and edges.

    Handles :attr:`~app.models.enums.SourceKind.GITHUB_PROFILE` — every repository the
    account owns, filtered and capped — and :attr:`~app.models.enums.SourceKind.GITHUB_REPO`
    — exactly one. Both accept a bare login, a profile URL, an ``owner/repo`` shorthand, or
    any repository URL; see :func:`parse_target`.

    Per-source options, read from ``KnowledgeSource.config``:

    ``max_repos``
        Overrides ``settings.github_max_repos`` for this source.
    ``include_forks``
        Overrides ``settings.github_include_forks``.
    ``include_archived``
        Include archived repositories, which are skipped by default.
    ``fetch_readme`` / ``fetch_manifest``
        Turn off the two optional per-repository requests. Useful when running
        unauthenticated against a large account, where the 60 requests/hour budget is the
        binding constraint.

    Attributes:
        source_kinds: ``github_profile`` and ``github_repo``.
    """

    meta: ClassVar[PluginMeta] = PluginMeta(
        kind=PluginKind.ANALYZER,
        name="github",
        display_name="GitHub",
        description=(
            "Indexes a GitHub account or repository: languages, topics, stars, READMEs and "
            "declared dependencies."
        ),
        capabilities=frozenset({"http", "fingerprint", "etag_cache", "optional_credential"}),
    )
    source_kinds: ClassVar[frozenset[SourceKind]] = frozenset(
        {SourceKind.GITHUB_PROFILE, SourceKind.GITHUB_REPO}
    )

    def __init__(self, settings: Settings, **kw: Any) -> None:
        """Construct the analyzer.

        Args:
            settings: Application settings; ``github_token``, ``github_max_repos`` and
                ``github_include_forks`` are read from it.
            **kw: Extra construction options, kept on :attr:`~app.plugins.base.BasePlugin.options`.
        """
        super().__init__(settings, **kw)
        self._cache: Cache | None = None
        self._cache_resolved = False
        self._extractor: KnowledgeExtractor | None = None

    # -- collaborators -------------------------------------------------------------------

    def _get_cache(self) -> Cache | None:
        """Return the process cache, resolving it once.

        Returns:
            The shared cache, or ``None`` when one cannot be built — in which case every
            request simply goes to the network.
        """
        if self._cache_resolved:
            return self._cache
        self._cache_resolved = True
        try:
            from app.cache import get_cache

            self._cache = get_cache()
        except Exception as exc:
            logger.info("github.cache_unavailable", error=str(exc))
            self._cache = None
        return self._cache

    def _get_extractor(self) -> KnowledgeExtractor:
        """Return the fact extractor, building it once.

        Returns:
            A :class:`~app.knowledge.extractors.KnowledgeExtractor` sharing this analyzer's
            cache. It resolves a model lazily and falls back to the deterministic rules when
            there is none, so this works with zero API keys.
        """
        if self._extractor is None:
            self._extractor = KnowledgeExtractor(cache=self._get_cache())
        return self._extractor

    # -- plugin surface -------------------------------------------------------------------

    def supports(self, source: SourceRef) -> bool:
        """Return whether this analyzer can handle *source*.

        Args:
            source: The candidate source.

        Returns:
            ``True`` when the kind matches and the URI names a GitHub account. A source with
            an empty URI is accepted so that :func:`~app.knowledge.analyzers.base.get_analyzer`
            — which probes with a bare kind and no URI — can still resolve this analyzer.
        """
        if not super().supports(source):
            return False
        return not source.uri or parse_target(source.uri) is not None

    async def healthcheck(self) -> bool:
        """Report whether the GitHub API is reachable.

        Uses ``/rate_limit``, the one endpoint that does not itself consume quota.

        Returns:
            ``True`` when the API answered. Never raises.
        """
        try:
            client = http_client()
            response = await client.get(
                f"{API_ROOT}/rate_limit", headers=self._headers(JSON_ACCEPT)
            )
        except Exception as exc:
            logger.info("github.healthcheck_failed", error=str(exc))
            return False
        return response.status_code < 400

    # -- request layer ----------------------------------------------------------------------

    def _headers(self, accept: str) -> dict[str, str]:
        """Build the request headers, including the credential when one is configured.

        Args:
            accept: The media type to request.

        Returns:
            The headers. ``Authorization`` is present only when ``settings.github_token`` is
            set; without it the request is unauthenticated and rate limited to 60/hour.
        """
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": API_VERSION,
        }
        token = getattr(self.settings, "github_token", None)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _cache_key(self, url: str, accept: str, params: dict[str, Any] | None) -> str:
        """Build the cache key for one request.

        The credential is deliberately *not* part of the key by value, only by presence: two
        installations sharing a Redis cache must not be able to read each other's private
        repository bodies through a key that happens to collide, and a rotated token must
        not throw away a warm cache.

        Args:
            url: The absolute request URL.
            accept: The requested media type, which changes the body's shape.
            params: The query parameters.

        Returns:
            The cache key.
        """
        return make_key(
            NAMESPACES.GITHUB,
            CACHE_TAG,
            url,
            accept,
            sorted((str(key), str(value)) for key, value in (params or {}).items()),
            bool(getattr(self.settings, "github_token", None)),
        )

    async def _read_cached(self, key: str) -> dict[str, Any] | None:
        """Return a cached response envelope.

        Args:
            key: The cache key.

        Returns:
            The stored envelope, or ``None`` on a miss or a cache failure.
        """
        cache = self._get_cache()
        if cache is None:
            return None
        try:
            entry = await cache.get(key)
        except Exception as exc:
            logger.debug("github.cache_read_failed", error=str(exc))
            return None
        return entry if isinstance(entry, dict) and "text" in entry else None

    async def _write_cached(self, key: str, response: httpx.Response) -> None:
        """Store a response envelope for later ``ETag`` revalidation.

        Args:
            key: The cache key.
            response: The successful response to remember.
        """
        cache = self._get_cache()
        if cache is None:
            return
        envelope = {
            "etag": response.headers.get(ETAG_HEADER, ""),
            "text": response.text,
            "headers": {
                name: response.headers[name] for name in CACHED_HEADERS if name in response.headers
            },
        }
        try:
            await cache.set(key, envelope, ttl=CACHE_TTL_SECONDS)
        except Exception as exc:
            logger.debug("github.cache_write_failed", error=str(exc))

    def _raise_for_status(self, response: httpx.Response, url: str) -> None:
        """Translate a failing response into the analyzer error hierarchy.

        Args:
            response: The response to inspect.
            url: The request URL, for the message.

        Raises:
            _RateLimited: On 403/429 with ``X-RateLimit-Remaining: 0``.
            SourceAccessDenied: On 401, and on a 403/429 that is not a rate limit.
            SourceUnavailableError: On 404.
            AnalyzerError: On any other status at or above 400.
        """
        status = response.status_code
        if status < 400:
            return

        remaining = response.headers.get(RATE_LIMIT_REMAINING_HEADER)
        if status in (403, 429) and remaining == "0":
            raise _RateLimited(
                f"GitHub API rate limit exhausted; it resets at {_reset_time(response.headers)}. "
                "Set GITHUB_TOKEN to raise the limit from 60 to 5000 requests per hour."
            )
        if status == 401:
            raise SourceAccessDenied(
                "GitHub rejected the credential (401). Check that GITHUB_TOKEN is valid and "
                "has not expired."
            )
        if status in (403, 429):
            raise SourceAccessDenied(
                f"GitHub refused {url} ({status}). A token with access to this resource is "
                "required; set GITHUB_TOKEN."
            )
        if status == 404:
            raise SourceUnavailableError(
                f"GitHub has nothing at {url} (404). The account or repository may have been "
                "renamed, deleted, or made private."
            )
        raise AnalyzerError(f"GitHub returned {status} for {url}")

    async def _request(
        self,
        url: str,
        *,
        accept: str = JSON_ACCEPT,
        params: dict[str, Any] | None = None,
        allow_missing: bool = False,
    ) -> _ApiResponse | None:
        """Perform one cached, conditional, retried GET.

        Args:
            url: Absolute request URL.
            accept: Media type to request.
            params: Query parameters.
            allow_missing: When ``True`` a 404 returns ``None`` instead of raising, which is
                how "this repository has no README" is expressed.

        Returns:
            The response, or ``None`` for a tolerated 404.

        Raises:
            _RateLimited: When the quota is gone.
            SourceAccessDenied: When the credential is missing, invalid, or insufficient.
            SourceUnavailableError: On a 404 that is not tolerated, or when the host could
                not be reached after :data:`MAX_ATTEMPTS` attempts.
            AnalyzerError: On a persistent server error.
        """
        # Lazy, like every third-party import in this package: the application must import
        # cleanly on a machine where no HTTP-backed analyzer will ever run.
        import httpx

        key = self._cache_key(url, accept, params)
        cached = await self._read_cached(key)
        headers = self._headers(accept)
        if cached and cached.get("etag"):
            headers["If-None-Match"] = str(cached["etag"])

        client = http_client()
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await client.get(url, headers=headers, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                logger.debug("github.request_failed", url=url, attempt=attempt, error=str(exc))
            else:
                if response.status_code == 304 and cached is not None:
                    logger.debug("github.not_modified", url=url)
                    return _ApiResponse(
                        status=200,
                        text=str(cached.get("text", "")),
                        headers=dict(cached.get("headers", {})),
                        from_cache=True,
                    )
                if response.status_code < 400:
                    await self._write_cached(key, response)
                    return _ApiResponse(
                        status=response.status_code,
                        text=response.text,
                        headers={name.lower(): value for name, value in response.headers.items()},
                    )
                if response.status_code == 404 and allow_missing:
                    return None
                if response.status_code < 500:
                    self._raise_for_status(response, url)
                last_error = AnalyzerError(f"GitHub returned {response.status_code} for {url}")

            if attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(
                    RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
                )

        if isinstance(last_error, AnalyzerError):
            raise last_error
        raise SourceUnavailableError(
            f"could not reach {url} after {MAX_ATTEMPTS} attempts: {last_error}"
        )

    async def _get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        allow_missing: bool = False,
    ) -> Any:
        """GET an API path and decode its JSON body.

        Args:
            path: API path beginning with ``/``.
            params: Query parameters.
            allow_missing: Whether a 404 yields ``None`` instead of raising.

        Returns:
            The decoded payload, or ``None`` for a tolerated 404.
        """
        response = await self._request(
            f"{API_ROOT}{path}", accept=JSON_ACCEPT, params=params, allow_missing=allow_missing
        )
        return None if response is None else response.json()

    # -- selection ---------------------------------------------------------------------------

    def _option_int(self, source: SourceRef, name: str, default: int) -> int:
        """Read a positive integer option off the source's config.

        Args:
            source: The source being analyzed.
            name: The option key.
            default: Value used when the option is absent or unusable.

        Returns:
            The configured value, or *default*.
        """
        try:
            value = int(source.option(name, default))
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    def _option_bool(self, source: SourceRef, name: str, default: bool) -> bool:
        """Read a boolean option off the source's config.

        Args:
            source: The source being analyzed.
            name: The option key.
            default: Value used when the option is absent.

        Returns:
            The configured value coerced to ``bool``, or *default*.
        """
        value = source.option(name, None)
        return default if value is None else bool(value)

    def _select_repos(self, repos: list[dict[str, Any]], source: SourceRef) -> list[dict[str, Any]]:
        """Filter and rank a repository listing.

        Forks are dropped unless ``github_include_forks`` (or the per-source override) says
        otherwise, archived repositories are dropped unless ``include_archived`` is set, and
        the survivors are sorted by ``pushed_at`` descending so the cap keeps the *newest*
        work rather than an arbitrary page of it.

        Args:
            repos: Raw repository payloads.
            source: The source being analyzed, for its config overrides.

        Returns:
            The selected repositories, most recently pushed first, at most
            ``max_repos`` long.
        """
        include_forks = self._option_bool(
            source, "include_forks", bool(getattr(self.settings, "github_include_forks", False))
        )
        include_archived = self._option_bool(source, "include_archived", False)
        limit = self._option_int(
            source, "max_repos", int(getattr(self.settings, "github_max_repos", 200))
        )

        selected = [
            repo
            for repo in repos
            if isinstance(repo, dict)
            and (include_forks or not repo.get("fork"))
            and (include_archived or not repo.get("archived"))
        ]
        selected.sort(
            key=lambda repo: (str(repo.get("pushed_at") or ""), str(repo.get("name") or "")),
            reverse=True,
        )
        return selected[:limit]

    async def _list_repos(self, owner: str, source: SourceRef) -> list[dict[str, Any]]:
        """Page through an account's repositories.

        Args:
            owner: The account login.
            source: The source being analyzed, for its config overrides.

        Returns:
            The selected repositories. Paging stops as soon as the selection is full, a
            short page arrives, or :data:`MAX_REPO_PAGES` is reached.
        """
        limit = self._option_int(
            source, "max_repos", int(getattr(self.settings, "github_max_repos", 200))
        )
        collected: list[dict[str, Any]] = []
        for page in range(1, MAX_REPO_PAGES + 1):
            payload = await self._get_json(
                f"/users/{quote(owner, safe='')}/repos",
                params={
                    "per_page": REPOS_PER_PAGE,
                    "page": page,
                    "sort": "pushed",
                    "direction": "desc",
                    "type": "owner",
                },
            )
            if not isinstance(payload, list) or not payload:
                break
            collected.extend(item for item in payload if isinstance(item, dict))
            if len(payload) < REPOS_PER_PAGE:
                break
            if len(self._select_repos(collected, source)) >= limit:
                break
        return self._select_repos(collected, source)

    async def _gather(
        self, source: SourceRef
    ) -> tuple[_Target, dict[str, Any] | None, list[dict[str, Any]]]:
        """Resolve *source* and fetch everything the fingerprint depends on.

        The single entry point shared by :meth:`fingerprint` and :meth:`analyze`, which is
        what guarantees the two compute the same digest from the same facts. It is also the
        cheap half of an index run: one profile request plus one or two listing requests,
        all ``ETag``-revalidated.

        Args:
            source: The source to resolve.

        Returns:
            ``(target, profile, repositories)``. *profile* is ``None`` for a single-repo
            source; *repositories* is already filtered, sorted and capped.

        Raises:
            AnalyzerError: If the URI names nothing usable.
            SourceUnavailableError: If the account or repository does not exist.
            SourceAccessDenied: If reading it needs a credential that is not configured.
        """
        target = parse_target(source.uri)
        if target is None:
            raise AnalyzerError(
                f"{source.uri!r} is not a GitHub account, repository, or URL. Expected a "
                "login (octocat), an owner/repo pair, or a github.com URL.",
                source=source,
            )

        if source.kind is SourceKind.GITHUB_REPO:
            if target.repo is None:
                raise AnalyzerError(
                    f"source kind 'github_repo' needs a repository, but {source.uri!r} names "
                    f"only the account {target.owner!r}",
                    source=source,
                )
            repo = await self._get_json(
                f"/repos/{quote(target.owner, safe='')}/{quote(target.repo, safe='')}"
            )
            if not isinstance(repo, dict):
                raise SourceUnavailableError(
                    f"GitHub returned no repository payload for {target.full_name}",
                    source=source,
                )
            return target, None, [repo]

        profile = await self._get_json(f"/users/{quote(target.owner, safe='')}")
        if not isinstance(profile, dict):
            raise SourceUnavailableError(
                f"GitHub returned no account payload for {target.owner!r}", source=source
            )
        repos = await self._list_repos(target.owner, source)
        return target, profile, repos

    # -- fingerprinting -------------------------------------------------------------------------

    def _compose_fingerprint(
        self,
        source: SourceRef,
        target: _Target,
        profile: dict[str, Any] | None,
        repos: list[dict[str, Any]],
    ) -> str:
        """Digest the profile's and every repository's last-modified stamps.

        This is the change probe the whole "re-index after you push a commit" promise rests
        on. ``pushed_at`` moves on every push to any branch, and the account's ``updated_at``
        moves when the profile itself changes, so together they cover everything this
        analyzer reads that a user can change. The repository list is the *selected* one, so
        changing ``max_repos`` or ``include_forks`` correctly invalidates the digest too.

        Args:
            source: The source being probed.
            target: The parsed account/repository.
            profile: The account payload, or ``None`` for a single-repo source.
            repos: The selected repositories.

        Returns:
            A 64-character hex digest, comparable across processes and runs.
        """
        stamps = sorted(
            (str(repo.get("full_name") or repo.get("name") or ""), str(repo.get("pushed_at") or ""))
            for repo in repos
        )
        return compute_fingerprint(
            FINGERPRINT_TAG,
            source.kind.value,
            target.full_name,
            str((profile or {}).get("updated_at") or ""),
            stamps,
        )

    async def fingerprint(self, source: SourceRef) -> str:
        """Cheaply probe whether the account or repository has changed.

        Costs one request for the account plus one per hundred repositories, all answered
        with ``304 Not Modified`` when nothing moved — and GitHub does not charge a 304
        against the rate limit, so a scheduled re-index of an idle account is effectively
        free.

        Args:
            source: The source to probe.

        Returns:
            The digest described in :meth:`_compose_fingerprint`, identical to the one
            :meth:`analyze` will store. Falls back to the base class's identity digest — one
            that can never match, and therefore always forces a re-analysis — when the probe
            itself fails, so a transient outage can never be mistaken for "unchanged".
        """
        try:
            target, profile, repos = await self._gather(source)
        except AnalyzerError as exc:
            logger.info("github.fingerprint_unavailable", uri=source.uri, error=str(exc))
            return await super().fingerprint(source)
        return self._compose_fingerprint(source, target, profile, repos)

    # -- analysis ---------------------------------------------------------------------------------

    async def analyze(self, source: SourceRef) -> AnalysisResult:
        """Extract everything knowable from a GitHub account or repository.

        Args:
            source: The source to analyze.

        Returns:
            One document per repository, a ``project`` node per repository with its
            technology edges, and the facts recovered from each README — plus one line in
            :attr:`~app.knowledge.analyzers.base.AnalysisResult.errors` per repository that
            failed, since a single broken repository must never cost the user the other
            hundred.

        Raises:
            AnalyzerError: If the URI names nothing usable.
            SourceUnavailableError: If the account or repository is gone, unreachable, or the
                API rate limit is exhausted.
            SourceAccessDenied: If a credential is required and none is configured.
        """
        # Only the *kind* is checked through the base guard. A URI that is not a GitHub one
        # is left to _gather(), whose message names every accepted form — far more
        # actionable than "unsupported source kind" for a source that has the right kind and
        # the wrong address.
        if source.kind not in type(self).source_kinds:
            self.require_supported(source)
        result = AnalysisResult()

        target, profile, repos = await self._gather(source)
        result.fingerprint = self._compose_fingerprint(source, target, profile, repos)

        if not repos:
            result.record_error(
                f"{target.full_name} has no repositories matching the current filters "
                "(forks and archived repositories are skipped by default)."
            )
            return result

        fetch_readme = self._option_bool(source, "fetch_readme", True)
        fetch_manifest = self._option_bool(source, "fetch_manifest", True)

        for repo in repos:
            full_name = str(repo.get("full_name") or repo.get("name") or "?")
            try:
                result.merge(
                    await self._analyze_repo(
                        repo,
                        source=source,
                        fetch_readme=fetch_readme,
                        fetch_manifest=fetch_manifest,
                    )
                )
            except _RateLimited:
                # The quota is gone: every remaining repository would fail identically and
                # hammering an endpoint that has said no is not polite. Keep what was
                # extracted and surface the wall to the operator.
                raise
            except AnalyzerError as exc:
                logger.warning("github.repo_failed", repo=full_name, error=str(exc))
                result.record_error(f"{full_name}: {exc}")
            except Exception as exc:
                logger.warning(
                    "github.repo_unexpected_error",
                    repo=full_name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                result.record_error(f"{full_name}: unexpected {type(exc).__name__}: {exc}")

        result.deduplicate()
        logger.info(
            "github.analyzed",
            target=target.full_name,
            repos=len(repos),
            **result.counts(),
        )
        return result

    async def _analyze_repo(
        self,
        repo: dict[str, Any],
        *,
        source: SourceRef,
        fetch_readme: bool,
        fetch_manifest: bool,
    ) -> AnalysisResult:
        """Extract one repository.

        Args:
            repo: The repository payload from the listing or the single-repo fetch.
            source: The source being analyzed, for provenance.
            fetch_readme: Whether to fetch the README.
            fetch_manifest: Whether to look for a dependency manifest.

        Returns:
            This repository's document, project node, technology nodes, edges and facts.
        """
        owner = str((repo.get("owner") or {}).get("login") or "")
        name = str(repo.get("name") or "")
        full_name = str(repo.get("full_name") or (f"{owner}/{name}" if owner else name))
        url = str(repo.get("html_url") or f"https://github.com/{full_name}")
        description = str(repo.get("description") or "").strip()

        languages = await self._fetch_languages(owner, name)
        readme = await self._fetch_readme(owner, name) if fetch_readme else ""
        manifest_name, dependencies = (
            await self._fetch_manifest(owner, name) if fetch_manifest else ("", [])
        )

        shares = _language_shares(languages)
        text = _repo_document_text(repo, shares, dependencies, manifest_name, readme)
        document = ExtractedDocument(
            uri=url,
            title=full_name,
            text=text,
            kind=SourceKind.GITHUB_REPO,
            metadata={
                "full_name": full_name,
                "owner": owner,
                "url": url,
                "description": description,
                "homepage": str(repo.get("homepage") or ""),
                "primary_language": str(repo.get("language") or ""),
                "languages": {language: count for language, count, _ in shares},
                "topics": [str(topic) for topic in (repo.get("topics") or [])],
                "stars": int(repo.get("stargazers_count") or 0),
                "forks": int(repo.get("forks_count") or 0),
                "size_kb": int(repo.get("size") or 0),
                "created_at": str(repo.get("created_at") or ""),
                "pushed_at": str(repo.get("pushed_at") or ""),
                "default_branch": str(repo.get("default_branch") or ""),
                "license": _license_name(repo),
                "is_fork": bool(repo.get("fork")),
                "is_archived": bool(repo.get("archived")),
                "is_private": bool(repo.get("private")),
                "manifest": manifest_name,
                "dependencies": dependencies,
                "readme_chars": len(readme),
                "source_kind": source.kind.value,
            },
        )

        result = AnalysisResult(documents=[document])
        project = ExtractedEntity(
            kind=EntityKind.PROJECT,
            name=name or full_name,
            summary=description or None,
            aliases=[full_name] if full_name and full_name != name else [],
            attributes={
                "url": url,
                "full_name": full_name,
                "owner": owner,
                "stars": int(repo.get("stargazers_count") or 0),
                "forks": int(repo.get("forks_count") or 0),
                "primary_language": str(repo.get("language") or ""),
                "topics": [str(topic) for topic in (repo.get("topics") or [])],
                "created_at": str(repo.get("created_at") or ""),
                "pushed_at": str(repo.get("pushed_at") or ""),
                "license": _license_name(repo),
                "homepage": str(repo.get("homepage") or ""),
            },
            confidence=API_CONFIDENCE,
        )
        result.entities.append(project)

        primary = str(repo.get("language") or "")
        for language, _count, _share in shares:
            weight = EDGE_WEIGHT_PRIMARY_LANGUAGE if language == primary else EDGE_WEIGHT_LANGUAGE
            self._link_technology(
                result, project, language, weight=weight, origin="languages", repo=full_name
            )
        for dependency in dependencies:
            if canonical_skill(dependency) is None:
                # An unrecognised package name ("left-pad", "bblanchon/ArduinoJson") is a
                # fact about the build, not a technology worth a graph node of its own; it
                # stays in the document metadata where retrieval can still find it.
                continue
            self._link_technology(
                result,
                project,
                dependency,
                weight=EDGE_WEIGHT_DEPENDENCY,
                origin="manifest",
                repo=full_name,
                confidence=DEPENDENCY_CONFIDENCE,
            )

        prose = "\n\n".join(part for part in (description, readme) if part)
        if prose.strip():
            extracted = await self._get_extractor().extract(
                prose,
                kind=FactKind.ACCOMPLISHMENT,
                context={"organization": owner or None, "source_uri": url},
            )
            bonus = star_impact_bonus(repo.get("stargazers_count") or 0)
            for fact in extracted.facts:
                result.facts.append(_boost_fact(fact, bonus))
            for entity in extracted.entities:
                if entity.kind not in (EntityKind.SKILL, EntityKind.TECHNOLOGY):
                    continue
                result.entities.append(entity)
                result.edges.append(
                    _used_in_edge(
                        entity.identity,
                        project.identity,
                        weight=EDGE_WEIGHT_README,
                        evidence={"source": "github", "repo": full_name, "origin": "readme"},
                    )
                )

        return result

    def _link_technology(
        self,
        result: AnalysisResult,
        project: ExtractedEntity,
        raw_name: str,
        *,
        weight: float,
        origin: str,
        repo: str,
        confidence: float = API_CONFIDENCE,
    ) -> None:
        """Add one technology node and its edge to the project.

        Args:
            result: The accumulator to append to.
            project: The repository's project node.
            raw_name: A language or dependency name as the API or manifest wrote it.
            weight: Edge weight; see :data:`EDGE_WEIGHT_PRIMARY_LANGUAGE`.
            origin: ``"languages"`` or ``"manifest"``, recorded as evidence.
            repo: The repository's full name, recorded as evidence.
            confidence: Confidence for the technology node.
        """
        if not raw_name.strip():
            return
        kind, name = _entity_for_name(raw_name)
        if not name:
            return
        result.entities.append(
            ExtractedEntity(
                kind=kind,
                name=name,
                attributes={"origin": origin},
                confidence=confidence,
            )
        )
        result.edges.append(
            _used_in_edge(
                (kind, name),
                project.identity,
                weight=weight,
                evidence={"source": "github", "repo": repo, "origin": origin},
            )
        )

    # -- per-repository fetches --------------------------------------------------------------------

    async def _fetch_languages(self, owner: str, name: str) -> dict[str, Any]:
        """Fetch a repository's language byte histogram.

        Args:
            owner: Account login.
            name: Repository name.

        Returns:
            Language name to byte count; empty when the repository has no detected
            languages.
        """
        payload = await self._get_json(
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/languages",
            allow_missing=True,
        )
        return payload if isinstance(payload, dict) else {}

    async def _fetch_readme(self, owner: str, name: str) -> str:
        """Fetch a repository's README as text.

        Requests the raw media type, but still handles the JSON envelope with base64
        ``content`` that the API returns when a proxy or a future default renegotiates the
        media type. An HTML README is run through
        :func:`~app.knowledge.analyzers._text.html_to_text`.

        Args:
            owner: Account login.
            name: Repository name.

        Returns:
            The README's text, truncated to :data:`MAX_README_CHARS`; empty when the
            repository has none.
        """
        response = await self._request(
            f"{API_ROOT}/repos/{quote(owner, safe='')}/{quote(name, safe='')}/readme",
            accept=RAW_ACCEPT,
            allow_missing=True,
        )
        if response is None:
            return ""

        text = _file_text(response)
        if _looks_like_html(text):
            text = html_to_text(text)[0]
        return text[:MAX_README_CHARS]

    async def _fetch_manifest(self, owner: str, name: str) -> tuple[str, list[str]]:
        """Find and read one dependency manifest.

        Lists the repository root once and fetches at most one file: the first entry of
        :data:`MANIFEST_FILES` that exists and is under :data:`MAX_MANIFEST_BYTES`. Two
        requests per repository is the whole budget this feature gets, because at 60
        requests/hour unauthenticated it would otherwise be the reason an index run stops.

        Args:
            owner: Account login.
            name: Repository name.

        Returns:
            ``(manifest_filename, dependency_names)``; ``("", [])`` when the repository has
            no recognised manifest.
        """
        prefix = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/contents"
        listing = await self._get_json(prefix, allow_missing=True)
        if not isinstance(listing, list):
            return "", []

        by_name = {
            str(entry.get("name")): entry
            for entry in listing
            if isinstance(entry, dict) and entry.get("type") == "file"
        }
        for filename in MANIFEST_FILES:
            entry = by_name.get(filename)
            if entry is None:
                continue
            size = entry.get("size")
            if isinstance(size, int) and size > MAX_MANIFEST_BYTES:
                logger.debug("github.manifest_too_large", repo=f"{owner}/{name}", manifest=filename)
                continue
            response = await self._request(
                f"{API_ROOT}{prefix}/{quote(filename, safe='')}",
                accept=RAW_ACCEPT,
                allow_missing=True,
            )
            if response is None:
                continue
            return filename, parse_manifest(filename, _file_text(response))
        return "", []


# ======================================================================================
# Result construction helpers
# ======================================================================================


def _file_text(response: _ApiResponse) -> str:
    """Return a file response's text, decoding a base64 envelope when there is one.

    The raw media type asks GitHub for the file's bytes, and that is what normally arrives.
    A corporate proxy, a GitHub Enterprise instance, or a future default renegotiation can
    still answer with the JSON envelope the contents API defines, so both shapes are
    handled. The envelope is only recognised when *both* ``content`` and ``encoding`` are
    present, so a file that happens to be JSON with a ``content`` key is left alone.

    Args:
        response: The response to a file request.

    Returns:
        The file's text; the empty string when a declared base64 body does not decode.
    """
    payload = response.json()
    if not isinstance(payload, dict) or "content" not in payload or "encoding" not in payload:
        return response.text
    content = payload.get("content")
    if not isinstance(content, str):
        return ""
    if payload.get("encoding") != "base64":
        return content
    try:
        return base64.b64decode(content).decode("utf-8", errors="replace")
    except (ValueError, TypeError) as exc:
        logger.debug("github.content_decode_failed", error=str(exc))
        return ""


def _used_in_edge(
    technology: tuple[EntityKind, str],
    project: tuple[EntityKind, str],
    *,
    weight: float,
    evidence: dict[str, Any],
) -> ExtractedEdge:
    """Build a ``used_in`` edge from a technology to a project.

    **Direction.** :class:`~app.models.enums.RelationKind` names the relation ``used_in``,
    and :class:`~app.knowledge.analyzers.base.ExtractedEdge` documents that a relation reads
    *subject-first* — its own example being "PyTorch *used_in* PoseNet". The subject is
    therefore the technology and the object is the project, which is also exactly what
    :func:`~app.knowledge.extractors.extract_entities_rule_based` emits for the same
    relationship from README text.

    That agreement is the reason this direction wins over the intuitive
    ``project -> technology`` reading. ``KnowledgeEdge`` is unique on
    ``(source_entity_id, target_entity_id, relation)``, so emitting the reverse here would
    not overwrite anything — it would create a *second*, parallel edge for every
    project/technology pair the rule-based extractor also saw, doubling the graph and
    breaking every ``neighbors()`` traversal that assumes one direction. One direction,
    chosen to match the contract's own example, is worth more than either reading in
    isolation. Recorded in ``docs/OPEN_QUESTIONS.md``.

    Args:
        technology: The ``(kind, name)`` of the technology or skill.
        project: The ``(kind, name)`` of the project it was used in.
        weight: Edge strength, used to rank graph expansion during retrieval.
        evidence: Provenance recorded on the edge.

    Returns:
        The edge.
    """
    return ExtractedEdge(
        source=technology,
        target=project,
        relation=RelationKind.USED_IN,
        weight=weight,
        evidence=evidence,
    )


def _boost_fact(fact: ExtractedFact, bonus: int) -> ExtractedFact:
    """Raise a fact's impact score by a repository's star bonus, in place.

    Args:
        fact: The fact extracted from the repository's prose.
        bonus: Points from :func:`star_impact_bonus`.

    Returns:
        The same fact, with its impact score raised and clamped to
        :data:`~app.knowledge.analyzers.base.MAX_IMPACT_SCORE`.
    """
    if bonus:
        fact.impact_score = min(MAX_IMPACT_SCORE, fact.impact_score + bonus)
    return fact


def _repo_document_text(
    repo: dict[str, Any],
    shares: list[tuple[str, int, float]],
    dependencies: list[str],
    manifest_name: str,
    readme: str,
) -> str:
    """Render a repository as the document text that gets chunked and embedded.

    A structured header followed by the README verbatim. The header exists so that a
    retrieval query like "what has this person written in Rust?" can match on the language
    histogram even when the README never says the word, and it is written as markdown so the
    chunker sees the same shape it sees everywhere else.

    Args:
        repo: The repository payload.
        shares: Ranked ``(language, bytes, percentage)`` triples.
        dependencies: Declared dependency names.
        manifest_name: The manifest they came from, for attribution.
        readme: The README text.

    Returns:
        The document text.
    """
    full_name = str(repo.get("full_name") or repo.get("name") or "")
    lines: list[str] = [f"# {full_name}", ""]

    description = str(repo.get("description") or "").strip()
    if description:
        lines.extend([description, ""])

    primary = str(repo.get("language") or "")
    if primary:
        lines.append(f"- Primary language: {primary}")
    if shares:
        breakdown = ", ".join(f"{language} {share:.0f}%" for language, _, share in shares[:8])
        lines.append(f"- Language breakdown: {breakdown}")
    topics = [str(topic) for topic in (repo.get("topics") or []) if str(topic).strip()]
    if topics:
        lines.append(f"- Topics: {', '.join(topics)}")
    lines.append(
        f"- Stars: {int(repo.get('stargazers_count') or 0)} · "
        f"Forks: {int(repo.get('forks_count') or 0)} · "
        f"Repository size: {_format_size(repo.get('size'))}"
    )
    created = _date_only(repo.get("created_at"))
    pushed = _date_only(repo.get("pushed_at"))
    if created or pushed:
        lines.append(f"- Created {created or 'unknown'}, last pushed {pushed or 'unknown'}")
    licence = _license_name(repo)
    if licence:
        lines.append(f"- License: {licence}")
    homepage = str(repo.get("homepage") or "").strip()
    if homepage:
        lines.append(f"- Homepage: {homepage}")
    if dependencies:
        listed = ", ".join(dependencies[:MAX_DEPENDENCIES])
        lines.append(f"- Declared dependencies ({manifest_name}): {listed}")

    if readme.strip():
        lines.extend(["", "## README", "", readme.strip()])
    return "\n".join(lines).strip()
