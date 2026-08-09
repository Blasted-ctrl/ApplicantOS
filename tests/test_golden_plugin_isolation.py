"""Golden rule #5 — plugin isolation, enforced as a test rather than as a convention.

    Never import a concrete provider / analyzer / model / template module outside its own
    package. Go through ``app.plugins.registry``.

A convention decays silently. The first ``from app.jobs.greenhouse import GreenhouseProvider``
in a service looks harmless, works, and quietly welds the orchestration layer to one ATS —
after which "add a provider" stops being a drop-in and the registry becomes decorative.

So this is a **static** check over the source tree: every ``import`` statement in ``app/`` is
parsed with :mod:`ast` and matched against the set of concrete plugin modules. Parsing rather
than string-matching means a commented-out import, a module path inside a docstring, and the
loader's own ``importlib.import_module(name)`` (a string, not an import statement — which is
exactly why the loader is allowed to name every builtin) are all correctly ignored.

The registry indirection is asserted positively too: fetching a provider by name has to
actually work, or the rule would be satisfiable by having no way to reach a provider at all.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.models.enums import PluginKind

#: The repository's ``app`` package.
APP_ROOT = Path(__file__).resolve().parent.parent / "app"

#: Concrete plugin modules, grouped by the package that owns them. A module inside the owning
#: package may import its siblings freely; nothing outside it may.
CONCRETE_MODULES: dict[str, tuple[str, ...]] = {
    "app.jobs": (
        "app.jobs.greenhouse",
        "app.jobs.lever",
        "app.jobs.ashby",
        "app.jobs.workday",
        "app.jobs.linkedin",
    ),
    "app.knowledge.analyzers": (
        "app.knowledge.analyzers.github",
        "app.knowledge.analyzers.website",
        "app.knowledge.analyzers.project_folder",
        "app.knowledge.analyzers.resume_parser",
        "app.knowledge.analyzers.linkedin_export",
        "app.knowledge.analyzers.document",
    ),
    "app.ai.models": (
        "app.ai.models.anthropic",
        "app.ai.models.openai",
        "app.ai.models.local",
        "app.ai.models.null",
    ),
    "app.documents": (
        "app.documents.latex",
        "app.documents.docx",
        "app.documents.html",
        "app.documents.markdown",
    ),
}

#: Flat lookup: concrete module → the package permitted to import it.
OWNER_OF: dict[str, str] = {
    module: owner for owner, modules in CONCRETE_MODULES.items() for module in modules
}


def _module_name(path: Path) -> str:
    """Return the dotted module name for a file under ``app/``."""
    relative = path.relative_to(APP_ROOT.parent).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(path: Path) -> list[tuple[str, int]]:
    """Return every module imported by *path*, as ``(dotted name, line number)``.

    Both statement forms are resolved: ``import a.b`` yields ``a.b``, and ``from a.b import c``
    yields ``a.b`` **and** ``a.b.c`` — the latter because ``from app.jobs import greenhouse``
    is an import of ``app.jobs.greenhouse`` however it is spelled.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import can never reach another package
                continue
            base = node.module or ""
            found.append((base, node.lineno))
            for alias in node.names:
                found.append((f"{base}.{alias.name}", node.lineno))
    return found


def _source_files() -> list[Path]:
    """Every Python module in the ``app`` package."""
    return sorted(APP_ROOT.rglob("*.py"))


def test_the_source_tree_was_actually_found() -> None:
    """Guard against a silently empty scan making every check below vacuous."""
    files = _source_files()
    assert len(files) > 100, f"expected the full app package, found {len(files)} files"


@pytest.mark.parametrize("owner", sorted(CONCRETE_MODULES))
def test_no_concrete_plugin_is_imported_outside_its_package(owner: str) -> None:
    """The rule itself, one package at a time so a failure names the guilty layer."""
    violations: list[str] = []

    for path in _source_files():
        importer = _module_name(path)
        if importer == owner or importer.startswith(f"{owner}."):
            continue  # a module may import its own siblings
        for imported, line in _imports(path):
            if OWNER_OF.get(imported) == owner:
                violations.append(f"{path.relative_to(APP_ROOT.parent)}:{line} imports {imported}")

    assert not violations, (
        f"concrete {owner} plugins imported from outside their package — go through "
        "app.plugins.registry instead:\n  " + "\n  ".join(violations)
    )


def test_no_concrete_plugin_is_imported_from_outside_app_either() -> None:
    """The same rule across every owner at once, as a single readable failure."""
    violations: list[str] = []
    for path in _source_files():
        importer = _module_name(path)
        for imported, line in _imports(path):
            owner = OWNER_OF.get(imported)
            if owner is None:
                continue
            if importer == owner or importer.startswith(f"{owner}."):
                continue
            violations.append(f"{path.relative_to(APP_ROOT.parent)}:{line} -> {imported}")

    assert not violations, "plugin isolation violated:\n  " + "\n  ".join(violations)


def test_every_listed_concrete_module_exists() -> None:
    """A typo in :data:`CONCRETE_MODULES` would make this whole file pass vacuously."""
    for module in OWNER_OF:
        path = APP_ROOT.parent / Path(*module.split("."))
        assert path.with_suffix(".py").is_file(), f"{module} is not a real module"


# ======================================================================================
# The other half: the registry has to actually work
# ======================================================================================


@pytest.fixture
def loaded_registry(settings):
    """The plugin registry with every builtin loaded."""
    from app.plugins.loader import load_all
    from app.plugins.registry import registry

    registry.configure(settings)
    load_all()
    return registry


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        (PluginKind.PROVIDER, "greenhouse"),
        (PluginKind.PROVIDER, "lever"),
        (PluginKind.PROVIDER, "ashby"),
        (PluginKind.PROVIDER, "workday"),
        (PluginKind.PROVIDER, "linkedin"),
        (PluginKind.MODEL, "null"),
        (PluginKind.ANALYZER, "github"),
    ],
)
def test_the_registry_reaches_every_concrete_plugin(loaded_registry, kind, name) -> None:
    """Isolation is only virtuous if the indirection works; otherwise it is just absence."""
    instance = loaded_registry.get(kind, name)
    assert instance is not None
    assert type(instance).meta.name == name


def test_builtin_module_list_covers_every_concrete_module(loaded_registry) -> None:
    """``BUILTIN_PLUGIN_MODULES`` must name every package holding a concrete plugin.

    The loader imports builtins by *string*, which is what makes the isolation rule
    satisfiable at all. A concrete module missing from that list would never register, and its
    provider would silently vanish from discovery.
    """
    from app.plugins.loader import BUILTIN_PLUGIN_MODULES

    for module in OWNER_OF:
        assert any(
            module == entry or module.startswith(f"{entry}.") or entry.startswith(f"{module}")
            for entry in BUILTIN_PLUGIN_MODULES
        ), f"{module} is not reachable from BUILTIN_PLUGIN_MODULES"
