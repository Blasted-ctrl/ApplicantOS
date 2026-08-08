"""Freeze the backend into the sidecar binary the Tauri bundle expects.

    python desktop/src-tauri/sidecar/build_sidecar.py

Produces ``desktop/src-tauri/binaries/applicantos-server-<target-triple>[.exe]``. The target
triple suffix is not decoration: ``bundle.externalBin`` in ``tauri.conf.json`` names the file
*without* it, and Tauri appends the triple of the target it is building for so one repository
can hold sidecars for several platforms at once. A binary without the suffix is invisible to
the bundler.

The triple is asked of ``rustc`` rather than guessed from ``platform``, because it is Rust's
notion of the target that has to match — ``x86_64-pc-windows-msvc`` and ``x86_64-pc-windows-gnu``
are the same machine and different triples.

Run this once before ``cargo check``, ``npm run app`` or ``npm run app:build``: Tauri's build
script copies external binaries on every build, and a missing one is a build error rather than
a warning. The development loop does not otherwise need it — ``npm run dev`` starts the backend
from the project's virtualenv (see ``scripts/dev-with-backend.mjs``) and the Rust shell attaches
to that instead of spawning a sidecar.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

#: Name the Tauri configuration expects, before the target triple is appended.
BINARY_STEM: Final[str] = "applicantos-server"

#: Directory `tauri.conf.json` points `externalBin` at.
OUTPUT_DIRNAME: Final[str] = "binaries"

#: Packages whose submodules are pulled in wholesale by PyInstaller's own helper.
#:
#: ``uvicorn`` picks its protocol, loop and lifespan implementations by string at startup, so
#: nothing in the source names them. Its modules all import cleanly, which is what makes
#: ``collect_submodules`` — a helper that discovers by *importing* — the right tool here and the
#: wrong one for ``app`` (see :func:`discover_app_modules`).
COLLECT_SUBMODULES: Final[tuple[str, ...]] = ("uvicorn",)

#: Modules reached only through a driver string or an optional dependency check, which is not
#: a package walk and so is not covered above.
HIDDEN_IMPORTS: Final[tuple[str, ...]] = (
    "aiosqlite",
    "sqlalchemy.dialects.sqlite.aiosqlite",
    "websockets",
)

#: Environment the analysis runs under.
#:
#: Anything PyInstaller imports during analysis — its own hooks, and ``collect_submodules`` —
#: imports `app` transitively, and `app/database/session.py` builds the engine at import time
#: from a ``DATABASE_URL`` that defaults to ``postgresql+asyncpg``. A desktop install has no
#: `asyncpg`, so the import raises. Freezing in the configuration the binary will actually run
#: in is the fix; this is the same switch the Tauri shell sets when it launches the sidecar
#: (`src/sidecar.rs`).
BUILD_ENVIRONMENT: Final[dict[str, str]] = {"SQLITE_MODE": "true"}


def discover_app_modules(repo_root: Path) -> list[str]:
    """Enumerate every module in the ``app`` package by reading the source tree.

    The backend discovers most of itself at runtime — ``app.api.routes`` walks its own package
    with :func:`pkgutil.iter_modules` and mounts whatever it finds (``docs/CONTRACTS.md`` §14),
    and the plugin registry does the same for providers, analyzers, models and templates. None
    of those imports appear in the source, so a freezer that follows imports sees none of them
    and the resulting binary starts, logs a healthy startup, and serves **zero routes**.

    PyInstaller's ``collect_submodules`` exists for exactly this, but it discovers by
    *importing*, and it swallows an import failure as "no such module". One missing optional
    dependency on the build machine therefore silently removes a whole subtree from the bundle
    and the result still builds and still runs. Walking the filesystem instead cannot fail
    quietly: a file either exists or it does not.

    Args:
        repo_root: The repository root, containing the ``app`` package.

    Returns:
        Dotted module names, sorted.

    Raises:
        SystemExit: If the package is not where it is expected to be.
    """
    package_root = repo_root / "app"
    if not (package_root / "__init__.py").is_file():
        raise SystemExit(f"No `app` package at {package_root}.")

    modules: list[str] = []
    for path in package_root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(repo_root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if parts:
            modules.append(".".join(parts))
    return sorted(set(modules))


def _target_triple() -> str:
    """Ask ``rustc`` for the triple Tauri will look for.

    Returns:
        The host target triple.

    Raises:
        SystemExit: If ``rustc`` is unavailable, since the output filename cannot be guessed
            without it.
    """
    rustc = shutil.which("rustc")
    if rustc is None:
        raise SystemExit(
            "rustc is not on PATH. It is needed to name the sidecar after the target triple "
            "Tauri will look for; install Rust from https://rustup.rs."
        )
    # Fixed argv, executable resolved through shutil.which: no shell, no user input.
    result = subprocess.run(
        [rustc, "-vV"],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise SystemExit("rustc -vV did not report a host triple.")


def main() -> int:
    """Freeze the backend and place the result where Tauri expects it.

    Returns:
        The process exit status.
    """
    for name, value in BUILD_ENVIRONMENT.items():
        os.environ[name] = value

    try:
        import PyInstaller.__main__ as pyinstaller
    except ImportError:
        raise SystemExit(
            "PyInstaller is not installed in this interpreter. Install it into the project's "
            "virtualenv with `pip install pyinstaller`, then run this script with that same "
            "interpreter."
        ) from None

    here = Path(__file__).resolve().parent
    repo_root = here.parents[2]
    output_dir = here.parent / OUTPUT_DIRNAME
    output_dir.mkdir(parents=True, exist_ok=True)

    triple = _target_triple()
    suffix = ".exe" if sys.platform == "win32" else ""
    final_name = f"{BINARY_STEM}-{triple}{suffix}"

    arguments = [
        str(here / "server.py"),
        "--name",
        BINARY_STEM,
        "--onefile",
        "--noconfirm",
        "--clean",
        "--console",
        "--distpath",
        str(output_dir),
        "--workpath",
        str(here / "build"),
        "--specpath",
        str(here / "build"),
        # `app/` is imported by name, so the project root has to be on the analysis path.
        "--paths",
        str(repo_root),
        # `app/config/*.yaml` is read at runtime (`scoring_rules.yaml`) and is not a module,
        # so it has to be carried as data.
        "--add-data",
        f"{repo_root / 'app' / 'config'}{';' if sys.platform == 'win32' else ':'}app/config",
    ]
    for package in COLLECT_SUBMODULES:
        arguments += ["--collect-submodules", package]

    app_modules = discover_app_modules(repo_root)
    for module in (*HIDDEN_IMPORTS, *app_modules):
        arguments += ["--hidden-import", module]
    print(f"including {len(app_modules)} modules from the app package")

    pyinstaller.run(arguments)

    built = output_dir / f"{BINARY_STEM}{suffix}"
    if not built.is_file():
        raise SystemExit(f"PyInstaller did not produce {built}.")

    destination = output_dir / final_name
    destination.unlink(missing_ok=True)
    built.rename(destination)
    print(f"sidecar ready: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
