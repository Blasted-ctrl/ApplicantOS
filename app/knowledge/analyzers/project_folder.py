"""Indexing a project folder on disk — how "I added a class project" enters the graph.

This is the analyzer that makes the product's central claim true. The user drops a folder
into ApplicantOS once; every time they add a subsystem, write a README paragraph, or land a
commit, the next tailored resume knows about it. Nothing is retyped and nothing is
maintained by hand.

What one scan produces:

* a **PROJECT** entity, named by the project's own manifest when it declares a name and by
  the folder otherwise, summarised from the README's opening paragraph;
* **TECHNOLOGY**/**SKILL** entities for every language the source tree actually contains and
  every dependency the manifests actually declare, each linked to the project with
  ``used_in`` — which is what later answers "what did they build with Rust?";
* a **document** for the README and one per file under ``docs/``, so the prose is chunked,
  embedded and retrievable;
* **facts** from the README's bullets, dated from the repository's own git history, so a
  project built in the summer of 2024 says so on the resume.

**Safety.** The root is resolved once and every entry is checked against it: a symlink or a
junction pointing outside the project is rejected and recorded, never followed. Linked
directories are never descended into at all, which also makes cycles impossible. Scanning
is capped by ``settings.project_scan_max_files`` and ``settings.project_scan_max_file_bytes``,
and binaries, images, archives, lockfiles and the usual dependency directories are skipped
before they are ever opened.

**The ``.gitignore`` subset supported** (implemented here rather than taken from a
dependency, so a folder scans identically on a machine with no git installed):

============================  =========================================================
``build/``                    trailing ``/`` — matches directories only
``*.log``                     ``*`` matches any run of characters except ``/``
``file?.txt``                 ``?`` matches one character except ``/``
``[abc].txt``, ``[!a-z].txt`` character classes, with ``!`` or ``^`` negating
``/root-only.txt``            a leading ``/`` anchors to the ``.gitignore``'s directory
``docs/build``                any other ``/`` also anchors it
``**/generated``              leading ``**/`` matches at any depth
``a/**/b``                    ``/**/`` matches zero or more directories
``a/**``                      trailing ``/**`` matches everything below ``a``
``!keep.txt``                 ``!`` re-includes; the last matching rule wins
``\\#literal``, ``\\!literal``  backslash escapes a leading ``#`` or ``!``
``trailing spaces``           stripped unless escaped with a backslash
============================  =========================================================

Nested ``.gitignore`` files are honoured and scoped to their own subtree, and are consulted
after the shallower ones so a deeper rule wins. Not supported, because git resolves them
against repository state this analyzer never loads: ``.git/info/exclude``, the global
``core.excludesFile``, and ``.gitattributes``-driven exclusions. Recorded in
``docs/OPEN_QUESTIONS.md``.
"""

from __future__ import annotations

import asyncio
import json
import re
import tomllib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final

import structlog

from app.knowledge.analyzers.base import (
    AnalysisResult,
    Analyzer,
    ExtractedDocument,
    ExtractedEdge,
    ExtractedEntity,
    SourceAccessDenied,
    SourceRef,
    SourceUnavailableError,
    compute_fingerprint,
)
from app.knowledge.analyzers.document import (
    DEFAULT_FACT_KIND,
    decode_bytes,
    knowledge_extractor,
    local_path_for,
)
from app.knowledge.extractors import canonical_skill, skill_entity_kind
from app.models.enums import EntityKind, PluginKind, RelationKind, SourceKind
from app.plugins.base import PluginMeta
from app.plugins.registry import plugin

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.config.settings import Settings
    from app.knowledge.extractors import KnowledgeExtractor

__all__ = [
    "ALWAYS_SKIPPED_DIRECTORIES",
    "GIT_TIMEOUT_SECONDS",
    "LANGUAGE_BY_FILENAME",
    "LANGUAGE_BY_SUFFIX",
    "MANIFEST_FILENAMES",
    "NON_CODE_LANGUAGES",
    "SKIPPED_FILENAMES",
    "SKIPPED_SUFFIXES",
    "GitignoreMatcher",
    "ProjectFolderAnalyzer",
    "ProjectProfile",
    "ProjectScan",
    "ScannedFile",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# What is never worth scanning
# ======================================================================================

#: Directory names skipped unconditionally, before ``.gitignore`` is even consulted. These
#: are dependency trees, build outputs and tool caches: thousands of files that belong to
#: somebody else's project and would drown the user's own work in the graph.
ALWAYS_SKIPPED_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".bzr",
        "node_modules",
        "bower_components",
        "jspm_packages",
        "venv",
        ".venv",
        "virtualenv",
        "site-packages",
        ".conda",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "dist",
        "build",
        "target",
        "out",
        "cmake-build-debug",
        "cmake-build-release",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".turbo",
        ".parcel-cache",
        ".cache",
        "vendor",
        "Pods",
        "Carthage",
        ".gradle",
        ".m2",
        ".cargo",
        ".stack-work",
        ".idea",
        ".vscode",
        ".vs",
        ".terraform",
        ".serverless",
        ".pio",
        ".platformio",
        "DerivedData",
        "Debug",
        "Release",
        "obj",
        "coverage",
        "htmlcov",
        ".coverage",
        ".sass-cache",
        "bin",
    }
)

#: File suffixes never read: compiled artifacts, images, archives, media, fonts and
#: databases. Their bytes carry no prose and no signal about what the user can do.
SKIPPED_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        # compiled / linked artifacts
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".o",
        ".obj",
        ".a",
        ".lib",
        ".bin",
        ".elf",
        ".hex",
        ".bit",
        ".pyc",
        ".pyo",
        ".pyd",
        ".class",
        ".jar",
        ".war",
        ".wasm",
        ".node",
        ".nupkg",
        ".pdb",
        ".ilk",
        ".d",
        ".map",
        ".lock",
        # images
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".tif",
        ".tiff",
        ".svg",
        ".psd",
        ".ai",
        ".eps",
        ".heic",
        ".raw",
        ".dng",
        # archives
        ".zip",
        ".tar",
        ".gz",
        ".tgz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".whl",
        ".deb",
        ".rpm",
        ".dmg",
        ".iso",
        ".pkg",
        ".msi",
        ".cab",
        # media
        ".mp3",
        ".wav",
        ".ogg",
        ".flac",
        ".m4a",
        ".mp4",
        ".mov",
        ".avi",
        ".mkv",
        ".webm",
        ".wmv",
        ".flv",
        # fonts and documents that are not source
        ".ttf",
        ".otf",
        ".woff",
        ".woff2",
        ".eot",
        ".pdf",
        ".doc",
        ".ppt",
        ".pptx",
        ".xls",
        ".xlsx",
        # data stores and models
        ".db",
        ".sqlite",
        ".sqlite3",
        ".mdb",
        ".dat",
        ".pkl",
        ".pickle",
        ".npy",
        ".npz",
        ".h5",
        ".hdf5",
        ".pt",
        ".pth",
        ".onnx",
        ".tflite",
        ".pb",
        ".safetensors",
        # 3D / CAD / EDA binaries
        ".stl",
        ".step",
        ".stp",
        ".3mf",
        ".fbx",
        ".blend",
        ".obj3d",
        ".brd",
    }
)

#: Exact filenames skipped: dependency lockfiles are machine-generated transitive closures,
#: thousands of lines long, that say nothing about what the user built.
SKIPPED_FILENAMES: Final[frozenset[str]] = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "npm-shrinkwrap.json",
        "bun.lockb",
        "poetry.lock",
        "pdm.lock",
        "uv.lock",
        "pipfile.lock",
        "cargo.lock",
        "gemfile.lock",
        "composer.lock",
        "go.sum",
        "packages.lock.json",
        "flake.lock",
        "mix.lock",
        "podfile.lock",
        "conan.lock",
        ".ds_store",
        "thumbs.db",
    }
)


# ======================================================================================
# Languages
# ======================================================================================

#: Extension → language. Curated rather than exhaustive: every entry here becomes a graph
#: node the user may be judged on, so a wrong mapping is worse than a missing one.
LANGUAGE_BY_SUFFIX: Final[dict[str, str]] = {
    ".py": "Python",
    ".pyi": "Python",
    ".pyx": "Cython",
    ".ipynb": "Jupyter Notebook",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".mts": "TypeScript",
    ".cts": "TypeScript",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".c++": "C++",
    ".hpp": "C++",
    ".hh": "C++",
    ".hxx": "C++",
    ".ino": "Arduino",
    ".cu": "CUDA",
    ".cuh": "CUDA",
    ".cs": "C#",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".swift": "Swift",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".lua": "Lua",
    ".luau": "Luau",
    ".m": "Objective-C",
    ".mm": "Objective-C++",
    ".r": "R",
    ".jl": "Julia",
    ".scala": "Scala",
    ".hs": "Haskell",
    ".ml": "OCaml",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".erl": "Erlang",
    ".dart": "Dart",
    ".zig": "Zig",
    ".nim": "Nim",
    ".pl": "Perl",
    ".pm": "Perl",
    ".clj": "Clojure",
    ".groovy": "Groovy",
    ".gradle": "Gradle",
    ".f90": "Fortran",
    ".f": "Fortran",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".fish": "Shell",
    ".ps1": "PowerShell",
    ".psm1": "PowerShell",
    ".bat": "Batchfile",
    ".cmd": "Batchfile",
    ".sql": "SQL",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".sass": "Sass",
    ".less": "Less",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".v": "Verilog",
    ".sv": "SystemVerilog",
    ".svh": "SystemVerilog",
    ".vhd": "VHDL",
    ".vhdl": "VHDL",
    ".asm": "Assembly",
    ".s": "Assembly",
    ".gd": "GDScript",
    ".glsl": "GLSL",
    ".vert": "GLSL",
    ".frag": "GLSL",
    ".comp": "GLSL",
    ".hlsl": "HLSL",
    ".shader": "ShaderLab",
    ".tf": "Terraform",
    ".proto": "Protocol Buffers",
    ".cmake": "CMake",
    ".mk": "Makefile",
    ".tex": "TeX",
    ".md": "Markdown",
    ".markdown": "Markdown",
    ".mdx": "Markdown",
    ".rst": "reStructuredText",
    ".adoc": "AsciiDoc",
    ".txt": "Text",
    ".json": "JSON",
    ".yml": "YAML",
    ".yaml": "YAML",
    ".toml": "TOML",
    ".xml": "XML",
    ".ini": "INI",
    ".cfg": "INI",
    ".csv": "CSV",
}

#: Extensionless files whose *name* identifies the language.
LANGUAGE_BY_FILENAME: Final[dict[str, str]] = {
    "makefile": "Makefile",
    "gnumakefile": "Makefile",
    "dockerfile": "Dockerfile",
    "containerfile": "Dockerfile",
    "cmakelists.txt": "CMake",
    "rakefile": "Ruby",
    "gemfile": "Ruby",
    "vagrantfile": "Ruby",
    "justfile": "Just",
    "kconfig": "Kconfig",
}

#: Languages counted in the histogram but *not* promoted to graph entities. Markdown and
#: YAML are real file formats and useless as evidence of engineering skill; listing "JSON"
#: among a candidate's technologies would make the whole graph less credible.
NON_CODE_LANGUAGES: Final[frozenset[str]] = frozenset(
    {
        "Markdown",
        "reStructuredText",
        "AsciiDoc",
        "Text",
        "JSON",
        "YAML",
        "TOML",
        "XML",
        "INI",
        "CSV",
        "TeX",
    }
)


# ======================================================================================
# Documents and manifests
# ======================================================================================

#: Suffixes treated as project prose under ``docs/``.
DOC_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc", ".asciidoc"}
)

#: Directory names (case-insensitive, at the project root) holding long-form documentation.
DOC_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset({"docs", "doc", "documentation"})

#: Matches ``README``, ``README.md``, ``readme.rst`` and friends.
_README_PATTERN: Final[re.Pattern[str]] = re.compile(r"^readme(\.[^.]+)?$", re.IGNORECASE)

#: Manifest filenames parsed for a project name and declared dependencies. Lowercased keys;
#: lookup is case-insensitive because ``Cargo.toml`` and ``cargo.toml`` both occur.
MANIFEST_FILENAMES: Final[frozenset[str]] = frozenset(
    {
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "cmakelists.txt",
        "platformio.ini",
        "gemfile",
        "setup.py",
        "composer.json",
        "pubspec.yaml",
    }
)

#: Most documentation files emitted as separate documents. Beyond this a docs tree is a
#: whole website, and the README plus the first N pages already carry the story.
MAX_DOC_FILES: Final[int] = 40

#: Most manifest-declared dependencies promoted to entities across the whole project. A
#: JavaScript project can declare hundreds; past this point they are transitive noise.
MAX_MANIFEST_DEPENDENCIES: Final[int] = 80

#: Longest project summary taken from the README's opening paragraph.
MAX_SUMMARY_CHARS: Final[int] = 400

#: How long a ``git`` invocation may take before it is killed. Generous enough for a large
#: repository's ``rev-list``, short enough that a hung git never stalls an index run.
GIT_TIMEOUT_SECONDS: Final[float] = 15.0

#: Domain-separation tag for the scan fingerprint.
_SCAN_FINGERPRINT_TAG: Final[str] = "analyzer.project_folder.v1"

#: Package-name prefixes dropped from dependency entities: TypeScript stub packages are
#: type shims for a library, not a library the user chose.
_IGNORED_DEPENDENCY_PREFIXES: Final[tuple[str, ...]] = ("@types/",)

#: Splits a PEP 508 / npm / cargo requirement into its name and the rest.
_REQUIREMENT_NAME: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._@/+-]+")

#: Confidence assigned to a language or dependency read directly out of the file tree.
#: High, because nothing was inferred: the files are there and the manifest says so.
_OBSERVED_CONFIDENCE: Final[float] = 0.85

#: Confidence assigned to the project entity itself.
_PROJECT_CONFIDENCE: Final[float] = 0.9


# ======================================================================================
# .gitignore
# ======================================================================================


@dataclass(frozen=True, slots=True)
class _IgnoreRule:
    """One compiled ``.gitignore`` line.

    Attributes:
        pattern: Regex matching a root-relative POSIX path. Its optional ``tail`` group
            captures the part of the path *below* the matched name, which is how a
            directory-only rule still excludes everything inside the directory.
        negated: Whether the rule re-includes rather than excludes.
        directory_only: Whether the source line ended in ``/``.
        source: ``"<gitignore path>:<line number>"``, for diagnostics.
    """

    pattern: re.Pattern[str]
    negated: bool
    directory_only: bool
    source: str

    def matches(self, relative_path: str, *, is_dir: bool) -> bool:
        """Return whether this rule applies to *relative_path*.

        Args:
            relative_path: Root-relative POSIX path, with no trailing slash.
            is_dir: Whether the path names a directory.

        Returns:
            ``True`` when the rule matches. A directory-only rule matches a non-directory
            only when the path lies *inside* a matching directory.
        """
        match = self.pattern.match(relative_path)
        if match is None:
            return False
        if self.directory_only and not is_dir:
            return match.group("tail") is not None
        return True


def _strip_unescaped_trailing_space(pattern: str) -> str:
    """Remove trailing spaces from a ``.gitignore`` line unless they are escaped.

    Args:
        pattern: The raw line, already stripped of its newline.

    Returns:
        The line with meaningless trailing whitespace removed.
    """
    end = len(pattern)
    while end > 0 and pattern[end - 1] in " \t":
        backslashes = 0
        index = end - 2
        while index >= 0 and pattern[index] == "\\":
            backslashes += 1
            index -= 1
        if backslashes % 2 == 1:
            break
        end -= 1
    return pattern[:end]


def _glob_to_regex(pattern: str) -> str:
    """Translate one ``.gitignore`` glob into a regex fragment.

    ``*`` and ``?`` never cross a ``/``; ``**`` does, in the three positions git defines
    (leading ``**/``, trailing ``/**``, and ``/**/`` in the middle).

    Args:
        pattern: The glob, with any ``!``, leading ``/`` and trailing ``/`` already removed.

    Returns:
        A regex fragment, unanchored.
    """
    out: list[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        character = pattern[index]
        if character == "*":
            run_end = index
            while run_end < length and pattern[run_end] == "*":
                run_end += 1
            is_double = run_end - index >= 2
            if is_double and run_end < length and pattern[run_end] == "/":
                out.append("(?:.*/)?")
                index = run_end + 1
                continue
            if is_double and run_end >= length:
                out.append(".*")
                index = run_end
                continue
            out.append("[^/]*")
            index = run_end
            continue
        if character == "?":
            out.append("[^/]")
            index += 1
            continue
        if character == "[":
            close = index + 1
            if close < length and pattern[close] in "!^":
                close += 1
            if close < length and pattern[close] == "]":
                close += 1
            while close < length and pattern[close] != "]":
                close += 2 if pattern[close] == "\\" else 1
            if close >= length:
                out.append(re.escape("["))
                index += 1
                continue
            body = pattern[index + 1 : close]
            if body.startswith("!"):
                body = f"^{body[1:]}"
            out.append(f"[{body}]")
            index = close + 1
            continue
        if character == "\\" and index + 1 < length:
            out.append(re.escape(pattern[index + 1]))
            index += 2
            continue
        out.append(re.escape(character))
        index += 1
    return "".join(out)


def _compile_ignore_rule(line: str, *, base: str, source: str) -> _IgnoreRule | None:
    """Compile one ``.gitignore`` line into a rule, or reject it.

    Args:
        line: The raw line.
        base: POSIX path of the directory containing the ``.gitignore``, relative to the
            project root; ``""`` at the root. Scopes the rule to that subtree.
        source: Diagnostic label recorded on the rule.

    Returns:
        The compiled rule, or ``None`` for a blank line or a comment.
    """
    pattern = _strip_unescaped_trailing_space(line.rstrip("\n\r"))
    if not pattern or pattern.startswith("#"):
        return None

    negated = pattern.startswith("!")
    if negated or pattern.startswith(("\\#", "\\!")):
        pattern = pattern[1:]
    if not pattern:
        return None

    directory_only = pattern.endswith("/")
    if directory_only:
        pattern = pattern[:-1]
    if not pattern:
        return None

    if pattern.startswith("/"):
        anchored = True
        pattern = pattern.lstrip("/")
    else:
        anchored = "/" in pattern
    if not pattern:
        return None

    body = _glob_to_regex(pattern)
    prefix = "" if anchored else "(?:.*/)?"
    scope = f"{re.escape(base)}/" if base else ""
    try:
        compiled = re.compile(f"^{scope}{prefix}{body}(?P<tail>/.*)?$")
    except re.error as exc:
        logger.debug("project_folder.bad_ignore_pattern", pattern=line, error=str(exc))
        return None
    return _IgnoreRule(
        pattern=compiled,
        negated=negated,
        directory_only=directory_only,
        source=source,
    )


class GitignoreMatcher:
    """Accumulates ``.gitignore`` rules and answers "is this path ignored?".

    Rules are appended in walk order — the root's file first, then each nested one as the
    walk descends — and evaluated in that order with the *last* match winning, which is
    exactly git's precedence: a deeper ``.gitignore``, and a later line within one file,
    override what came before. Each rule is scoped by regex to the subtree its file governs,
    so rules may safely accumulate for the whole scan instead of being pushed and popped.

    Attributes:
        rule_count: How many rules are currently loaded.
    """

    def __init__(self) -> None:
        """Create an empty matcher that ignores nothing."""
        self._rules: list[_IgnoreRule] = []

    @property
    def rule_count(self) -> int:
        """Number of compiled rules currently loaded."""
        return len(self._rules)

    def add_file(self, path: Path, *, base: str) -> int:
        """Load the rules in one ``.gitignore`` file.

        Args:
            path: The ``.gitignore`` to read. A missing or unreadable file is not an error;
                it simply contributes nothing.
            base: POSIX path of its directory relative to the project root, ``""`` at the
                root.

        Returns:
            The number of rules added.
        """
        try:
            content = decode_bytes(path.read_bytes())
        except OSError as exc:
            logger.debug("project_folder.gitignore_unreadable", path=str(path), error=str(exc))
            return 0

        added = 0
        for number, line in enumerate(content.splitlines(), start=1):
            rule = _compile_ignore_rule(line, base=base, source=f"{path.name}:{number}")
            if rule is not None:
                self._rules.append(rule)
                added += 1
        return added

    def is_ignored(self, relative_path: str, *, is_dir: bool) -> bool:
        """Return whether *relative_path* is excluded by the loaded rules.

        Args:
            relative_path: Root-relative POSIX path, without a trailing slash.
            is_dir: Whether the path names a directory.

        Returns:
            ``True`` when the last matching rule excludes it. ``False`` when no rule
            matches, or when the last match was a ``!`` re-inclusion.
        """
        ignored = False
        for rule in self._rules:
            if rule.matches(relative_path, is_dir=is_dir):
                ignored = not rule.negated
        return ignored


# ======================================================================================
# Scan results
# ======================================================================================


@dataclass(slots=True)
class ScannedFile:
    """One file the walk accepted.

    Attributes:
        path: Absolute path.
        relative: POSIX path relative to the project root — the identity used in
            fingerprints and document uris, so a moved project still fingerprints equal.
        size: Size in bytes.
        modified_ns: Modification time in nanoseconds.
        oversized: Whether the file exceeds ``settings.project_scan_max_file_bytes`` and was
            therefore counted but not read.
    """

    path: Path
    relative: str
    size: int
    modified_ns: int
    oversized: bool = False

    @property
    def name(self) -> str:
        """The file's base name."""
        return self.path.name

    @property
    def suffix(self) -> str:
        """The file's lowercased suffix, including the dot."""
        return self.path.suffix.lower()


@dataclass(slots=True)
class ProjectScan:
    """Everything one filesystem walk observed.

    Attributes:
        root: The resolved project root.
        files: The accepted files, in deterministic (breadth-first, name-sorted) order.
        directories: How many directories were descended into.
        ignored: How many entries ``.gitignore`` or the skip lists excluded.
        oversized: How many files exceeded the per-file byte cap.
        truncated: Whether ``settings.project_scan_max_files`` was reached, meaning the
            scan is a prefix of the project rather than all of it.
        escapes: Root-relative paths of links that pointed outside the project and were
            refused by the traversal guard.
        errors: Recoverable problems, forwarded to the analysis result.
    """

    root: Path
    files: list[ScannedFile] = field(default_factory=list)
    directories: int = 0
    ignored: int = 0
    oversized: int = 0
    truncated: bool = False
    escapes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        """Return the change digest for this scan.

        Covers the relative path, size and nanosecond mtime of every scanned file — enough
        to notice any edit, addition, deletion or rename without reading a single byte, and
        independent of where the folder happens to live so that moving it does not force a
        full re-index.

        Returns:
            A 64-character lowercase SHA-256 hex digest.
        """
        return compute_fingerprint(
            _SCAN_FINGERPRINT_TAG,
            [(item.relative, item.size, item.modified_ns) for item in self.files],
        )


@dataclass(slots=True)
class Dependency:
    """One dependency a manifest declares.

    Attributes:
        name: The dependency as the manifest spelled it.
        version: Version constraint as written, when the manifest gave one.
        manifest: Root-relative path of the manifest that declared it.
        scope: ``"runtime"``, ``"dev"``, ``"build"`` or ``"platform"``.
    """

    name: str
    version: str | None
    manifest: str
    scope: str = "runtime"


@dataclass(slots=True)
class ProjectProfile:
    """The complete picture of a project folder, assembled off the event loop.

    Attributes:
        scan: The raw walk.
        languages: Language → ``{"files", "lines", "bytes"}``, descending by line count.
        total_lines: Sum of non-blank lines across every readable source file.
        dependencies: Every dependency the manifests declared.
        manifests: Root-relative paths of the manifests that were parsed.
        declared_name: The project name a manifest declared, if any.
        readme: ``(relative path, text)`` of the README, if there is one.
        docs: ``(relative path, text)`` for each documentation file, in path order.
    """

    scan: ProjectScan
    languages: dict[str, dict[str, int]] = field(default_factory=dict)
    total_lines: int = 0
    dependencies: list[Dependency] = field(default_factory=list)
    manifests: list[str] = field(default_factory=list)
    declared_name: str | None = None
    readme: tuple[str, str] | None = None
    docs: list[tuple[str, str]] = field(default_factory=list)


# ======================================================================================
# The walk
# ======================================================================================


def _is_link(path: Path) -> bool:
    """Return whether *path* is a symlink or a Windows junction.

    Args:
        path: The entry to test.

    Returns:
        ``True`` for any reparse point the traversal guard must inspect.
    """
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction()) if callable(is_junction) else False
    except OSError:
        return False


def _scan_project(root: Path, *, max_files: int, max_file_bytes: int) -> ProjectScan:
    """Walk *root*, honouring ``.gitignore`` and the skip lists, and return what it holds.

    Breadth-first with name-sorted entries, so two scans of an unchanged tree produce an
    identical file list and therefore an identical fingerprint. A directory's own
    ``.gitignore`` is loaded before its children are tested, which is what gives nested
    ignore files their correct scope and precedence.

    Args:
        root: The resolved project root.
        max_files: Hard cap on accepted files (``settings.project_scan_max_files``).
        max_file_bytes: Per-file read cap (``settings.project_scan_max_file_bytes``).

    Returns:
        The scan. Blocking; call it through :func:`asyncio.to_thread`.
    """
    scan = ProjectScan(root=root)
    matcher = GitignoreMatcher()
    queue: deque[tuple[Path, str]] = deque([(root, "")])

    while queue:
        if len(scan.files) >= max_files:
            scan.truncated = True
            break
        directory, relative_dir = queue.popleft()
        scan.directories += 1

        gitignore = directory / ".gitignore"
        if gitignore.is_file():
            matcher.add_file(gitignore, base=relative_dir)

        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            scan.errors.append(f"{directory} could not be listed ({exc.strerror or exc}).")
            continue

        for entry in entries:
            if len(scan.files) >= max_files:
                scan.truncated = True
                break

            name = entry.name
            relative = f"{relative_dir}/{name}" if relative_dir else name

            try:
                is_directory = entry.is_dir()
            except OSError:
                continue

            if _is_link(entry):
                try:
                    target = entry.resolve()
                except OSError:
                    scan.ignored += 1
                    continue
                if not target.is_relative_to(root):
                    scan.escapes.append(relative)
                    logger.debug(
                        "project_folder.link_escaped_root", link=relative, target=str(target)
                    )
                    continue
                if is_directory:
                    # Inside the root, but descending would visit the same files twice and
                    # can form a cycle. The real directory is walked on its own path.
                    scan.ignored += 1
                    continue

            if is_directory:
                if name in ALWAYS_SKIPPED_DIRECTORIES:
                    scan.ignored += 1
                    continue
                if matcher.is_ignored(relative, is_dir=True):
                    scan.ignored += 1
                    continue
                queue.append((entry, relative))
                continue

            lowered = name.lower()
            if lowered in SKIPPED_FILENAMES or entry.suffix.lower() in SKIPPED_SUFFIXES:
                scan.ignored += 1
                continue
            if matcher.is_ignored(relative, is_dir=False):
                scan.ignored += 1
                continue

            try:
                stat = entry.stat()
            except OSError:
                scan.ignored += 1
                continue

            oversized = stat.st_size > max_file_bytes
            if oversized:
                scan.oversized += 1
            scan.files.append(
                ScannedFile(
                    path=entry,
                    relative=relative,
                    size=stat.st_size,
                    modified_ns=stat.st_mtime_ns,
                    oversized=oversized,
                )
            )

    logger.debug(
        "project_folder.scanned",
        root=str(root),
        files=len(scan.files),
        directories=scan.directories,
        ignored=scan.ignored,
        rules=matcher.rule_count,
        truncated=scan.truncated,
    )
    return scan


# ======================================================================================
# Manifests
# ======================================================================================


def _requirement_name(requirement: str) -> str | None:
    """Return the package name at the head of a requirement string.

    Handles the shapes ``requirements.txt``, npm, cargo and gradle actually contain:
    ``numpy>=1.26``, ``pytest[testing]``, ``requests ; python_version < "3.12"``,
    ``@scope/pkg``.

    Args:
        requirement: One requirement line or specifier.

    Returns:
        The bare name, or ``None`` when the line holds no name (a pip option, a comment).
    """
    candidate = requirement.strip().strip("\"'")
    if not candidate or candidate.startswith(("#", "-", "http://", "https://", "git+")):
        return None
    match = _REQUIREMENT_NAME.match(candidate)
    if match is None:
        return None
    name = match.group(0).strip(".-_/")
    return name or None


def _version_of(requirement: str, name: str) -> str | None:
    """Return the version constraint trailing a requirement, if it states one.

    Args:
        requirement: The full requirement string.
        name: The name already extracted from it.

    Returns:
        The remainder after the name, trimmed, or ``None`` when there is none.
    """
    remainder = requirement.strip().strip("\"'")[len(name) :].strip()
    return remainder or None


def _parse_package_json(text: str, manifest: str) -> tuple[str | None, list[Dependency]]:
    """Parse an npm ``package.json``.

    Args:
        text: File contents.
        manifest: Root-relative path, recorded on each dependency.

    Returns:
        ``(project name, dependencies)``.
    """
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return (None, [])
    if not isinstance(payload, dict):
        return (None, [])

    dependencies: list[Dependency] = []
    for section, scope in (
        ("dependencies", "runtime"),
        ("devDependencies", "dev"),
        ("peerDependencies", "runtime"),
        ("optionalDependencies", "runtime"),
    ):
        block = payload.get(section)
        if not isinstance(block, dict):
            continue
        for name, version in block.items():
            if isinstance(name, str) and name.strip():
                dependencies.append(
                    Dependency(
                        name=name.strip(),
                        version=str(version) if isinstance(version, str) else None,
                        manifest=manifest,
                        scope=scope,
                    )
                )
    name = payload.get("name")
    return (name.strip() if isinstance(name, str) and name.strip() else None, dependencies)


def _parse_pyproject(text: str, manifest: str) -> tuple[str | None, list[Dependency]]:
    """Parse a PEP 621 / Poetry ``pyproject.toml`` with the stdlib ``tomllib``.

    Args:
        text: File contents.
        manifest: Root-relative path, recorded on each dependency.

    Returns:
        ``(project name, dependencies)``.
    """
    try:
        payload = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return (None, [])

    dependencies: list[Dependency] = []
    project = payload.get("project") if isinstance(payload.get("project"), dict) else {}
    for requirement in project.get("dependencies", []) or []:
        if not isinstance(requirement, str):
            continue
        name = _requirement_name(requirement)
        if name:
            dependencies.append(
                Dependency(name, _version_of(requirement, name), manifest, "runtime")
            )
    optional = project.get("optional-dependencies")
    if isinstance(optional, dict):
        for group in optional.values():
            for requirement in group or []:
                if not isinstance(requirement, str):
                    continue
                name = _requirement_name(requirement)
                if name:
                    dependencies.append(
                        Dependency(name, _version_of(requirement, name), manifest, "dev")
                    )

    poetry = (
        payload.get("tool", {}).get("poetry", {}) if isinstance(payload.get("tool"), dict) else {}
    )
    if isinstance(poetry, dict):
        for section, scope in (("dependencies", "runtime"), ("dev-dependencies", "dev")):
            block = poetry.get(section)
            if not isinstance(block, dict):
                continue
            for name, spec in block.items():
                if isinstance(name, str) and name.strip().lower() != "python":
                    dependencies.append(
                        Dependency(
                            name.strip(),
                            spec if isinstance(spec, str) else None,
                            manifest,
                            scope,
                        )
                    )

    declared = project.get("name") or (poetry.get("name") if isinstance(poetry, dict) else None)
    return (
        declared.strip() if isinstance(declared, str) and declared.strip() else None,
        dependencies,
    )


def _parse_requirements(text: str, manifest: str) -> tuple[str | None, list[Dependency]]:
    """Parse a pip ``requirements.txt``.

    Args:
        text: File contents.
        manifest: Root-relative path, recorded on each dependency.

    Returns:
        ``(None, dependencies)`` — the file declares no project name.
    """
    dependencies: list[Dependency] = []
    for line in text.splitlines():
        requirement = line.split("#", 1)[0].strip()
        name = _requirement_name(requirement)
        if name:
            dependencies.append(
                Dependency(name, _version_of(requirement, name), manifest, "runtime")
            )
    return (None, dependencies)


def _parse_cargo(text: str, manifest: str) -> tuple[str | None, list[Dependency]]:
    """Parse a Rust ``Cargo.toml``.

    Args:
        text: File contents.
        manifest: Root-relative path, recorded on each dependency.

    Returns:
        ``(package name, dependencies)``.
    """
    try:
        payload = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return (None, [])

    dependencies: list[Dependency] = []
    for section, scope in (
        ("dependencies", "runtime"),
        ("dev-dependencies", "dev"),
        ("build-dependencies", "build"),
    ):
        block = payload.get(section)
        if not isinstance(block, dict):
            continue
        for name, spec in block.items():
            version = (
                spec
                if isinstance(spec, str)
                else (spec.get("version") if isinstance(spec, dict) else None)
            )
            dependencies.append(
                Dependency(str(name), str(version) if version else None, manifest, scope)
            )

    package = payload.get("package")
    declared = package.get("name") if isinstance(package, dict) else None
    return (str(declared) if declared else None, dependencies)


def _parse_go_mod(text: str, manifest: str) -> tuple[str | None, list[Dependency]]:
    """Parse a Go ``go.mod``, including its ``require ( ... )`` block.

    Args:
        text: File contents.
        manifest: Root-relative path, recorded on each dependency.

    Returns:
        ``(module name, dependencies)``.
    """
    dependencies: list[Dependency] = []
    declared: str | None = None
    in_require = False
    for raw_line in text.splitlines():
        line = raw_line.split("//", 1)[0].strip()
        if not line:
            continue
        if line.startswith("module "):
            declared = line[len("module ") :].strip() or None
            continue
        if line.startswith("require (") or line == "require(":
            in_require = True
            continue
        if in_require and line.startswith(")"):
            in_require = False
            continue
        if in_require or line.startswith("require "):
            spec = line[len("require ") :].strip() if line.startswith("require ") else line
            parts = spec.split()
            if parts:
                dependencies.append(
                    Dependency(parts[0], parts[1] if len(parts) > 1 else None, manifest)
                )
    if declared:
        declared = declared.rstrip("/").rsplit("/", 1)[-1]
    return (declared, dependencies)


def _parse_pom(text: str, manifest: str) -> tuple[str | None, list[Dependency]]:
    """Parse a Maven ``pom.xml``.

    Args:
        text: File contents.
        manifest: Root-relative path, recorded on each dependency.

    Returns:
        ``(artifactId, dependencies)``.
    """
    from xml.etree import ElementTree

    try:
        tree = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return (None, [])

    def local(tag: str) -> str:
        """Return an XML tag without its namespace."""
        return tag.rsplit("}", 1)[-1]

    dependencies: list[Dependency] = []
    for element in tree.iter():
        if local(element.tag) != "dependency":
            continue
        fields = {local(child.tag): (child.text or "").strip() for child in element}
        artifact = fields.get("artifactId")
        if artifact:
            dependencies.append(Dependency(artifact, fields.get("version") or None, manifest))

    declared: str | None = None
    for child in tree:
        if local(child.tag) == "artifactId":
            declared = (child.text or "").strip() or None
            break
    return (declared, dependencies)


#: ``implementation "group:artifact:version"`` and friends in a Gradle build script.
_GRADLE_DEPENDENCY: Final[re.Pattern[str]] = re.compile(
    r"\b(?:implementation|api|compileOnly|runtimeOnly|testImplementation|annotationProcessor|kapt)"
    r"\s*\(?\s*[\"'](?P<spec>[^\"']+)[\"']"
)


def _parse_gradle(text: str, manifest: str) -> tuple[str | None, list[Dependency]]:
    """Parse a Gradle build script's declared dependencies.

    Args:
        text: File contents.
        manifest: Root-relative path, recorded on each dependency.

    Returns:
        ``(None, dependencies)`` — the artifact name lives in ``settings.gradle``.
    """
    dependencies: list[Dependency] = []
    for match in _GRADLE_DEPENDENCY.finditer(text):
        parts = match.group("spec").split(":")
        if len(parts) >= 2 and parts[1]:
            dependencies.append(
                Dependency(parts[1], parts[2] if len(parts) > 2 else None, manifest)
            )
    return (None, dependencies)


#: ``project(name ...)`` in a CMake script.
_CMAKE_PROJECT: Final[re.Pattern[str]] = re.compile(
    r"\bproject\s*\(\s*(?P<name>[A-Za-z0-9_.+-]+)", re.IGNORECASE
)

#: ``find_package(Qt6 REQUIRED)`` — the closest CMake has to a dependency declaration.
_CMAKE_PACKAGE: Final[re.Pattern[str]] = re.compile(
    r"\bfind_package\s*\(\s*(?P<name>[A-Za-z0-9_.+-]+)", re.IGNORECASE
)


def _parse_cmake(text: str, manifest: str) -> tuple[str | None, list[Dependency]]:
    """Parse a ``CMakeLists.txt`` for its project name and ``find_package`` calls.

    Args:
        text: File contents.
        manifest: Root-relative path, recorded on each dependency.

    Returns:
        ``(project name, dependencies)``.
    """
    dependencies = [
        Dependency(match.group("name"), None, manifest) for match in _CMAKE_PACKAGE.finditer(text)
    ]
    project = _CMAKE_PROJECT.search(text)
    return (project.group("name") if project else None, dependencies)


def _parse_platformio(text: str, manifest: str) -> tuple[str | None, list[Dependency]]:
    """Parse a ``platformio.ini`` for its platform, framework, board and ``lib_deps``.

    Args:
        text: File contents.
        manifest: Root-relative path, recorded on each dependency.

    Returns:
        ``(None, dependencies)``. Platform, framework and board are recorded with the
        ``platform`` scope: on an embedded project they are the most informative thing the
        manifest contains.
    """
    import configparser

    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return (None, [])

    dependencies: list[Dependency] = []
    for section in parser.sections():
        for key, scope in (
            ("lib_deps", "runtime"),
            ("platform", "platform"),
            ("framework", "platform"),
            ("board", "platform"),
        ):
            raw = parser.get(section, key, fallback="")
            for entry in re.split(r"[\n,]+", raw):
                name = _requirement_name(entry)
                if name:
                    dependencies.append(Dependency(name, _version_of(entry, name), manifest, scope))
    return (None, dependencies)


#: ``gem "rails", "~> 7.0"`` in a Gemfile.
_GEMFILE_ENTRY: Final[re.Pattern[str]] = re.compile(
    r"^\s*gem\s+[\"'](?P<name>[^\"']+)[\"'](?:\s*,\s*[\"'](?P<version>[^\"']+)[\"'])?",
    re.MULTILINE,
)


def _parse_gemfile(text: str, manifest: str) -> tuple[str | None, list[Dependency]]:
    """Parse a Ruby ``Gemfile``.

    Args:
        text: File contents.
        manifest: Root-relative path, recorded on each dependency.

    Returns:
        ``(None, dependencies)``.
    """
    return (
        None,
        [
            Dependency(match.group("name"), match.group("version"), manifest)
            for match in _GEMFILE_ENTRY.finditer(text)
        ],
    )


#: ``install_requires=[...]`` / ``name="x"`` in a legacy ``setup.py``.
_SETUP_NAME: Final[re.Pattern[str]] = re.compile(r"name\s*=\s*[\"'](?P<name>[^\"']+)[\"']")
_SETUP_REQUIRES: Final[re.Pattern[str]] = re.compile(
    r"install_requires\s*=\s*\[(?P<body>[^\]]*)\]", re.DOTALL
)


def _parse_setup_py(text: str, manifest: str) -> tuple[str | None, list[Dependency]]:
    """Parse a legacy ``setup.py`` without executing it.

    Args:
        text: File contents.
        manifest: Root-relative path, recorded on each dependency.

    Returns:
        ``(project name, dependencies)``. Static regex only — running a ``setup.py`` to
        learn what it declares would be executing arbitrary code from the user's disk.
    """
    dependencies: list[Dependency] = []
    body = _SETUP_REQUIRES.search(text)
    if body:
        for entry in re.findall(r"[\"']([^\"']+)[\"']", body.group("body")):
            name = _requirement_name(entry)
            if name:
                dependencies.append(Dependency(name, _version_of(entry, name), manifest))
    declared = _SETUP_NAME.search(text)
    return (declared.group("name") if declared else None, dependencies)


def _parse_pubspec(text: str, manifest: str) -> tuple[str | None, list[Dependency]]:
    """Parse a Dart/Flutter ``pubspec.yaml`` without a YAML dependency.

    Only the two top-level shapes that matter are read — ``name:`` and the indented keys
    under ``dependencies:``/``dev_dependencies:`` — because pulling in a YAML parser for one
    manifest would violate the "imports cleanly with nothing installed" rule.

    Args:
        text: File contents.
        manifest: Root-relative path, recorded on each dependency.

    Returns:
        ``(package name, dependencies)``.
    """
    dependencies: list[Dependency] = []
    declared: str | None = None
    scope: str | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indented = raw_line[0] in " \t"
        stripped = raw_line.strip()
        if not indented:
            key = stripped.split(":", 1)[0].strip()
            if key == "name":
                declared = stripped.split(":", 1)[-1].strip() or None
            scope = (
                "runtime" if key == "dependencies" else "dev" if key == "dev_dependencies" else None
            )
            continue
        if scope and raw_line[: len(raw_line) - len(raw_line.lstrip())].count(" ") <= 2:
            name, _, version = stripped.partition(":")
            cleaned = _requirement_name(name)
            if cleaned:
                dependencies.append(Dependency(cleaned, version.strip() or None, manifest, scope))
    return (declared, dependencies)


#: Manifest filename (lowercased) → its parser. One entry per supported ecosystem; adding a
#: sixth build system is one function and one line.
_MANIFEST_PARSERS: Final[dict[str, Any]] = {
    "package.json": _parse_package_json,
    "composer.json": _parse_package_json,
    "pyproject.toml": _parse_pyproject,
    "requirements.txt": _parse_requirements,
    "cargo.toml": _parse_cargo,
    "go.mod": _parse_go_mod,
    "pom.xml": _parse_pom,
    "build.gradle": _parse_gradle,
    "build.gradle.kts": _parse_gradle,
    "cmakelists.txt": _parse_cmake,
    "platformio.ini": _parse_platformio,
    "gemfile": _parse_gemfile,
    "setup.py": _parse_setup_py,
    "pubspec.yaml": _parse_pubspec,
}


# ======================================================================================
# Profiling — everything blocking, in one place
# ======================================================================================


def _language_of(item: ScannedFile) -> str | None:
    """Return the language a scanned file is written in.

    Args:
        item: The scanned file.

    Returns:
        The language name, or ``None`` when the extension is unknown.
    """
    by_name = LANGUAGE_BY_FILENAME.get(item.name.lower())
    if by_name:
        return by_name
    return LANGUAGE_BY_SUFFIX.get(item.suffix)


def _count_lines(data: bytes) -> int:
    """Count non-blank lines in raw file bytes.

    Counting on bytes avoids decoding source files in a dozen encodings just to learn how
    long they are.

    Args:
        data: The file's bytes.

    Returns:
        The number of lines with at least one non-whitespace byte.
    """
    return sum(1 for line in data.splitlines() if line.strip())


def _lead_paragraph(text: str) -> str | None:
    """Return a project's opening prose paragraph, for use as a summary.

    Args:
        text: README text.

    Returns:
        The first paragraph that is neither a heading nor a badge line, truncated to
        :data:`MAX_SUMMARY_CHARS`; ``None`` when there is none.
    """
    paragraph: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            if paragraph:
                break
            continue
        if line.startswith("#") or line.startswith("---") or line.startswith("==="):
            if paragraph:
                break
            continue
        if line.startswith(("[![", "![", "<img", "<p", "<div", "<h", "|", "```")):
            continue
        paragraph.append(line)
    joined = " ".join(paragraph).strip()
    if not joined:
        return None
    return joined[:MAX_SUMMARY_CHARS]


def _read_text(item: ScannedFile) -> str | None:
    """Read one scanned file as text, or return ``None``.

    Args:
        item: The file to read.

    Returns:
        The decoded contents, or ``None`` when it is oversized or unreadable.
    """
    if item.oversized:
        return None
    try:
        return decode_bytes(item.path.read_bytes())
    except OSError as exc:
        logger.debug("project_folder.read_failed", path=item.relative, error=str(exc))
        return None


def _build_profile(root: Path, *, max_files: int, max_file_bytes: int) -> ProjectProfile:
    """Scan *root* and derive everything knowable from its files.

    All of the blocking work — the walk, the line counting, the manifest parsing, reading
    the README and the docs tree — happens here so that :meth:`ProjectFolderAnalyzer.analyze`
    can do it in a single :func:`asyncio.to_thread` hop.

    Args:
        root: The resolved project root.
        max_files: Hard cap on scanned files.
        max_file_bytes: Per-file read cap.

    Returns:
        The assembled profile.
    """
    scan = _scan_project(root, max_files=max_files, max_file_bytes=max_file_bytes)
    profile = ProjectProfile(scan=scan)

    readme_candidates: list[ScannedFile] = []
    doc_candidates: list[ScannedFile] = []

    for item in scan.files:
        language = _language_of(item)
        if language:
            bucket = profile.languages.setdefault(language, {"files": 0, "lines": 0, "bytes": 0})
            bucket["files"] += 1
            bucket["bytes"] += item.size

        head, _, _ = item.relative.partition("/")
        if "/" not in item.relative and _README_PATTERN.match(item.name):
            readme_candidates.append(item)
        elif head.lower() in DOC_DIRECTORY_NAMES and item.suffix in DOC_SUFFIXES:
            doc_candidates.append(item)

        lowered = item.name.lower()
        if lowered in _MANIFEST_PARSERS and not item.oversized:
            text = _read_text(item)
            if text is not None:
                declared, dependencies = _MANIFEST_PARSERS[lowered](text, item.relative)
                profile.manifests.append(item.relative)
                profile.dependencies.extend(dependencies)
                if declared and (profile.declared_name is None or "/" not in item.relative):
                    profile.declared_name = declared

        if language and not item.oversized:
            try:
                lines = _count_lines(item.path.read_bytes())
            except OSError:
                continue
            profile.languages[language]["lines"] += lines
            if language not in NON_CODE_LANGUAGES:
                # `total_lines` is "how much code is here", so prose and config are counted
                # in the histogram — which is descriptive — but not in the headline number.
                profile.total_lines += lines

    profile.languages = dict(
        sorted(
            profile.languages.items(),
            key=lambda entry: (-entry[1]["lines"], -entry[1]["files"], entry[0]),
        )
    )

    if readme_candidates:
        chosen = max(readme_candidates, key=lambda item: item.size)
        text = _read_text(chosen)
        if text and text.strip():
            profile.readme = (chosen.relative, text)

    for item in sorted(doc_candidates, key=lambda item: item.relative)[:MAX_DOC_FILES]:
        text = _read_text(item)
        if text and text.strip():
            profile.docs.append((item.relative, text))

    return profile


# ======================================================================================
# git history
# ======================================================================================


async def _run_git(root: Path, arguments: list[str]) -> str | None:
    """Run one ``git`` command inside *root*, with a timeout, degrading to ``None``.

    Args:
        root: The repository working tree.
        arguments: Arguments after ``git -C <root>``.

    Returns:
        Trimmed stdout on success; ``None`` when git is not installed, the command failed,
        it timed out, or the platform cannot spawn subprocesses on this event loop.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(root),
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, NotImplementedError, OSError, ValueError) as exc:
        logger.debug("project_folder.git_unavailable", error=str(exc))
        return None

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=GIT_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.debug("project_folder.git_timeout", arguments=arguments)
        try:
            process.kill()
            await process.wait()
        except (ProcessLookupError, OSError):  # pragma: no cover - already exited
            pass
        return None
    except (OSError, ValueError) as exc:  # pragma: no cover - transport-level failure
        logger.debug("project_folder.git_failed", error=str(exc))
        return None

    if process.returncode != 0:
        return None
    return stdout.decode("utf-8", errors="replace").strip()


async def _git_history(root: Path) -> dict[str, Any]:
    """Read the repository's commit count and first/last commit dates.

    Degrades silently and completely: no ``.git``, no ``git`` binary, a shallow clone, a
    repository with no commits, or a hung invocation all yield ``{}`` and the project is
    simply dated by nothing rather than dated wrongly.

    Args:
        root: The project root.

    Returns:
        A mapping with ``commits``, ``first_commit``/``last_commit`` (``YYYY-MM``) and
        ``first_commit_at``/``last_commit_at`` (ISO 8601), or ``{}``.
    """
    if not (root / ".git").exists():
        return {}

    history: dict[str, Any] = {}
    count = await _run_git(root, ["rev-list", "--count", "HEAD"])
    if count and count.isdigit():
        history["commits"] = int(count)

    last = await _run_git(root, ["log", "-1", "--format=%cI"])
    if last:
        history["last_commit_at"] = last
        history["last_commit"] = last[:7]

    first = await _run_git(root, ["log", "--max-parents=0", "--format=%cI"])
    if first:
        # A repository with merged histories has several root commits; the oldest is last.
        oldest = first.splitlines()[-1].strip()
        if oldest:
            history["first_commit_at"] = oldest
            history["first_commit"] = oldest[:7]

    branch = await _run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if branch and branch != "HEAD":
        history["branch"] = branch
    return history


# ======================================================================================
# The analyzer
# ======================================================================================


@plugin
class ProjectFolderAnalyzer(Analyzer):
    """Turns a directory of source code into a project, its technologies and its story.

    The path from "the user made a folder" to "the resume mentions it" runs entirely
    through this class. It reads what is actually on disk — languages by extension, lines of
    code, dependencies the manifests declare, prose in the README and ``docs/``, dates from
    git — and never guesses at anything it cannot see.

    Configuration recognised on :attr:`~app.knowledge.analyzers.base.SourceRef.config`:

    ``name``
        Overrides the project name, which otherwise comes from the manifest, then the
        folder.
    ``role``
        A title to attach to the extracted facts ("Firmware Lead").
    """

    meta: ClassVar[PluginMeta] = PluginMeta(
        kind=PluginKind.ANALYZER,
        name="project_folder",
        version="1.0.0",
        display_name="Project folder",
        description=(
            "Scans a local project directory: README and docs, manifests, language "
            "histogram, lines of code and git history."
        ),
        capabilities=frozenset({"local", "gitignore", "manifests", "git_history", "offline"}),
    )

    source_kinds: ClassVar[frozenset[SourceKind]] = frozenset({SourceKind.PROJECT_FOLDER})

    def __init__(self, settings: Settings, **kw: Any) -> None:
        """Construct the analyzer.

        Args:
            settings: Application settings, supplied by the plugin registry.
            **kw: Extra construction options, kept on :attr:`options`.
        """
        super().__init__(settings, **kw)
        self._extractor: KnowledgeExtractor | None = None

    # -- root resolution ----------------------------------------------------------------

    def _resolve_root(self, source: SourceRef) -> Path:
        """Resolve the project root, following links exactly once and no further.

        Resolving up front is what makes the traversal guard meaningful: every entry the
        walk later sees is compared against *this* path, so a symlink pointing at
        ``C:/Users`` is refused rather than silently indexed.

        Args:
            source: The source being analyzed.

        Returns:
            The absolute, symlink-free project root.

        Raises:
            SourceUnavailableError: If the uri is empty, missing, or not a directory.
            SourceAccessDenied: If the directory exists but cannot be read.
        """
        uri = source.uri.strip()
        if not uri:
            raise SourceUnavailableError("project folder source has no path", source=source)

        path = local_path_for(uri)
        try:
            root = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise SourceUnavailableError(
                f"{path} does not exist; the folder may have been moved or deleted",
                source=source,
            ) from exc
        except PermissionError as exc:
            raise SourceAccessDenied(
                f"{path} cannot be read; grant this application access to the folder",
                source=source,
            ) from exc
        except OSError as exc:
            raise SourceUnavailableError(
                f"{path} could not be resolved ({exc.strerror or exc})", source=source
            ) from exc

        if not root.is_dir():
            raise SourceUnavailableError(f"{root} is not a directory", source=source)
        return root

    # -- change detection ---------------------------------------------------------------

    async def fingerprint(self, source: SourceRef) -> str:
        """Probe the folder for changes by walking its metadata only.

        Reads no file contents: the digest covers each scanned file's relative path, size
        and nanosecond mtime, which is exactly what
        :meth:`ProjectScan.fingerprint` computes during a full analysis. The two therefore
        agree by construction, so an untouched project is skipped and a single edited file
        is not.

        Args:
            source: The folder to probe.

        Returns:
            The scan digest, or the never-matching identity digest when the folder cannot
            currently be walked.
        """
        try:
            root = self._resolve_root(source)
        except (SourceUnavailableError, SourceAccessDenied) as exc:
            logger.debug("project_folder.probe_unavailable", uri=source.uri, error=str(exc))
            return await super().fingerprint(source)

        scan = await asyncio.to_thread(
            _scan_project,
            root,
            max_files=self.settings.project_scan_max_files,
            max_file_bytes=self.settings.project_scan_max_file_bytes,
        )
        return scan.fingerprint()

    # -- analysis -----------------------------------------------------------------------

    async def analyze(self, source: SourceRef) -> AnalysisResult:
        """Scan the folder and return everything it says about the user.

        Args:
            source: A ``project_folder`` source whose uri is a local directory.

        Returns:
            One document for the README plus one per documentation file; a ``project``
            entity; ``technology``/``skill`` entities for every language and declared
            dependency, each joined to the project by ``used_in``; and facts extracted from
            the README, dated from git history when the prose itself gives no dates.

        Raises:
            SourceUnavailableError: If the folder does not exist or is not a directory.
            SourceAccessDenied: If the folder cannot be read.
        """
        self.require_supported(source)
        root = self._resolve_root(source)

        profile, history = await asyncio.gather(
            asyncio.to_thread(
                _build_profile,
                root,
                max_files=self.settings.project_scan_max_files,
                max_file_bytes=self.settings.project_scan_max_file_bytes,
            ),
            _git_history(root),
        )

        result = AnalysisResult()
        for message in profile.scan.errors:
            result.record_error(message)
        if profile.scan.truncated:
            result.record_error(
                f"scan stopped at settings.project_scan_max_files "
                f"({self.settings.project_scan_max_files}); the project was indexed only "
                "in part."
            )
        for escape in profile.scan.escapes:
            result.record_error(f"skipped {escape}: it links outside the project folder.")
        if profile.scan.oversized:
            result.record_error(
                f"{profile.scan.oversized} file(s) exceeded "
                f"settings.project_scan_max_file_bytes "
                f"({self.settings.project_scan_max_file_bytes:,} bytes) and were not read."
            )

        project_name = str(source.option("name") or profile.declared_name or root.name).strip()
        readme_uri = self._document_uri(root, profile.readme[0]) if profile.readme else None

        self._emit_documents(result, root, profile, project_name, history)
        aliases = await self._emit_facts(result, source, profile, project_name, history, readme_uri)
        self._emit_project(result, root, profile, project_name, history, aliases)
        self._emit_languages(result, profile, project_name)
        self._emit_dependencies(result, profile, project_name)

        result.deduplicate()
        result.fingerprint = profile.scan.fingerprint()
        logger.info(
            "project_folder.analyzed",
            root=str(root),
            project=project_name,
            files=len(profile.scan.files),
            languages=len(profile.languages),
            **result.counts(),
        )
        return result

    # -- emission -----------------------------------------------------------------------

    @staticmethod
    def _document_uri(root: Path, relative: str) -> str:
        """Return the stable document uri for a file inside the project.

        Args:
            root: The project root.
            relative: The file's root-relative POSIX path.

        Returns:
            An absolute POSIX path. Stable across runs, which is what makes
            ``UNIQUE(source_id, uri)`` update a row instead of accumulating duplicates.
        """
        return (root / relative).as_posix()

    def _emit_documents(
        self,
        result: AnalysisResult,
        root: Path,
        profile: ProjectProfile,
        project_name: str,
        history: dict[str, Any],
    ) -> None:
        """Append the README and documentation documents to *result*.

        Args:
            result: The result being assembled.
            root: The project root.
            profile: The scanned profile.
            project_name: The authoritative project name.
            history: Git history, attached as document metadata.
        """
        shared = {
            "analyzer": self.name,
            "project": project_name,
            "project_root": root.as_posix(),
        }
        if profile.readme is not None:
            relative, text = profile.readme
            result.documents.append(
                ExtractedDocument(
                    uri=self._document_uri(root, relative),
                    title=f"{project_name} README",
                    text=text,
                    kind=SourceKind.README,
                    metadata={
                        **shared,
                        "relative_path": relative,
                        "languages": profile.languages,
                        "lines_of_code": profile.total_lines,
                        "file_count": len(profile.scan.files),
                        "manifests": profile.manifests,
                        **history,
                    },
                )
            )
        for relative, text in profile.docs:
            result.documents.append(
                ExtractedDocument(
                    uri=self._document_uri(root, relative),
                    title=f"{project_name} — {Path(relative).stem}",
                    text=text,
                    kind=SourceKind.DOCUMENTATION,
                    metadata={**shared, "relative_path": relative},
                )
            )

    async def _emit_facts(
        self,
        result: AnalysisResult,
        source: SourceRef,
        profile: ProjectProfile,
        project_name: str,
        history: dict[str, Any],
        readme_uri: str | None,
    ) -> list[str]:
        """Extract facts from the README and merge them into *result*.

        A fact whose own wording carries no date inherits the repository's — the first
        commit is when the work started and the last is when it stopped, which is the
        honest reading of a project folder and is never invented by a model.

        Args:
            result: The result being assembled.
            source: The source, for its ``role`` option.
            profile: The scanned profile.
            project_name: Attached to every fact as its organization.
            history: Git history supplying the fallback dates.
            readme_uri: Provenance uri for the extracted facts.

        Returns:
            Alternative project names the README used, to be kept as entity aliases.
        """
        if profile.readme is None or readme_uri is None:
            return []

        extracted = await self._knowledge().extract(
            profile.readme[1],
            kind=DEFAULT_FACT_KIND,
            context={
                "organization": project_name,
                "role": source.option("role"),
                "source_uri": readme_uri,
            },
        )
        first = history.get("first_commit")
        last = history.get("last_commit")
        for fact in extracted.facts:
            if fact.date_start is None and first:
                fact.date_start = first
                fact.date_end = last
        result.merge(extracted)

        aliases = _retarget_project_entities(result, project_name)
        if aliases:
            logger.debug("project_folder.project_alias", project=project_name, aliases=aliases)
        return aliases

    def _emit_project(
        self,
        result: AnalysisResult,
        root: Path,
        profile: ProjectProfile,
        project_name: str,
        history: dict[str, Any],
        aliases: list[str],
    ) -> None:
        """Append the project entity itself.

        Args:
            result: The result being assembled.
            root: The project root.
            profile: The scanned profile.
            project_name: The authoritative project name.
            history: Git history, recorded as entity attributes.
            aliases: Other names the project is known by, from the README and the manifest.
        """
        summary = _lead_paragraph(profile.readme[1]) if profile.readme else None
        result.entities.append(
            ExtractedEntity(
                kind=EntityKind.PROJECT,
                name=project_name,
                summary=summary,
                aliases=[
                    alias
                    for alias in (root.name, profile.declared_name, *aliases)
                    if alias and alias != project_name
                ],
                attributes={
                    "path": root.as_posix(),
                    "languages": list(profile.languages),
                    "lines_of_code": profile.total_lines,
                    "files": len(profile.scan.files),
                    "manifests": profile.manifests,
                    **history,
                },
                confidence=_PROJECT_CONFIDENCE,
            )
        )

    def _emit_languages(
        self, result: AnalysisResult, profile: ProjectProfile, project_name: str
    ) -> None:
        """Append an entity and a ``used_in`` edge for every language present in the tree.

        Args:
            result: The result being assembled.
            profile: The scanned profile.
            project_name: The project the languages are linked to.
        """
        for language, stats in profile.languages.items():
            if language in NON_CODE_LANGUAGES or not stats["lines"]:
                continue
            canonical = canonical_skill(language)
            name = canonical or language
            kind = skill_entity_kind(canonical) if canonical else EntityKind.TECHNOLOGY
            result.entities.append(
                ExtractedEntity(
                    kind=kind,
                    name=name,
                    aliases=[language] if language != name else [],
                    attributes={
                        "files": stats["files"],
                        "lines_of_code": stats["lines"],
                        "evidence": "source files",
                    },
                    confidence=_OBSERVED_CONFIDENCE,
                )
            )
            result.edges.append(
                ExtractedEdge(
                    source=(kind, name),
                    target=(EntityKind.PROJECT, project_name),
                    relation=RelationKind.USED_IN,
                    weight=float(stats["files"]),
                    evidence={
                        "analyzer": self.name,
                        "source": "language histogram",
                        "lines_of_code": stats["lines"],
                    },
                )
            )

    def _emit_dependencies(
        self, result: AnalysisResult, profile: ProjectProfile, project_name: str
    ) -> None:
        """Append an entity and a ``used_in`` edge for every declared dependency.

        Names are canonicalised through the shared vocabulary when it recognises them, so
        ``"torch"`` from a ``requirements.txt`` and ``"PyTorch"`` from a README become the
        same graph node. Unrecognised names are kept verbatim — a niche embedded library is
        exactly the kind of specific evidence a resume benefits from.

        Args:
            result: The result being assembled.
            profile: The scanned profile.
            project_name: The project the dependencies are linked to.
        """
        # A package that declares an extra of itself ("applicantos[sqlite]" under
        # optional-dependencies) is not a technology the user chose; it is the project.
        own_names = {name.casefold() for name in (project_name, profile.declared_name) if name}
        seen: set[str] = set()
        emitted = 0
        for dependency in profile.dependencies:
            if emitted >= MAX_MANIFEST_DEPENDENCIES:
                break
            raw = dependency.name.strip()
            if not raw or raw.lower().startswith(_IGNORED_DEPENDENCY_PREFIXES):
                continue
            if raw.casefold() in own_names:
                continue
            canonical = canonical_skill(raw)
            name = canonical or raw
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            emitted += 1

            kind = skill_entity_kind(canonical) if canonical else EntityKind.TECHNOLOGY
            result.entities.append(
                ExtractedEntity(
                    kind=kind,
                    name=name,
                    aliases=[raw] if raw != name else [],
                    attributes={
                        "declared_in": dependency.manifest,
                        "version": dependency.version,
                        "scope": dependency.scope,
                        "evidence": "manifest dependency",
                    },
                    confidence=_OBSERVED_CONFIDENCE,
                )
            )
            result.edges.append(
                ExtractedEdge(
                    source=(kind, name),
                    target=(EntityKind.PROJECT, project_name),
                    relation=RelationKind.USED_IN,
                    evidence={
                        "analyzer": self.name,
                        "source": dependency.manifest,
                        "scope": dependency.scope,
                    },
                )
            )

    # -- internals ----------------------------------------------------------------------

    def _knowledge(self) -> KnowledgeExtractor:
        """Return this instance's extractor, building it once.

        Returns:
            The shared-cache-backed :class:`~app.knowledge.extractors.KnowledgeExtractor`.
        """
        if self._extractor is None:
            self._extractor = knowledge_extractor()
        return self._extractor


def _retarget_project_entities(result: AnalysisResult, project_name: str) -> list[str]:
    """Fold any project node the text extractor invented into the authoritative one.

    The README's ``# Heading`` and the folder's manifest often name the same project two
    different ways. Left alone that produces two ``project`` nodes with half the evidence
    each, so the extractor's node is removed, its edges are re-pointed at the real project,
    and its name is returned to be kept as an alias — which preserves recall without
    splitting the graph.

    Args:
        result: The result being assembled, modified in place.
        project_name: The authoritative project name.

    Returns:
        The alternative names that were folded in.
    """
    aliases: list[str] = []
    kept: list[ExtractedEntity] = []
    for entity in result.entities:
        if entity.kind is EntityKind.PROJECT and entity.name != project_name:
            aliases.append(entity.name)
            continue
        kept.append(entity)
    result.entities = kept

    if not aliases:
        return []

    retargeted: list[ExtractedEdge] = []
    for edge in result.edges:
        if edge.source[0] is EntityKind.PROJECT and edge.source[1] in aliases:
            edge.source = (EntityKind.PROJECT, project_name)
        if edge.target[0] is EntityKind.PROJECT and edge.target[1] in aliases:
            edge.target = (EntityKind.PROJECT, project_name)
        if edge.source != edge.target:
            retargeted.append(edge)
    result.edges = retargeted
    return aliases
