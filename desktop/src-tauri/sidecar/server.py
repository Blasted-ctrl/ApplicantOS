"""Entry point for the packaged backend sidecar (``docs/CONTRACTS.md`` §18).

The Tauri shell launches one process and hands it a host and a port:

    applicantos-server --host 127.0.0.1 --port 51423

That is the entire contract between the shell and the backend, and it is deliberately the
same shape as the development fallback (``python -m uvicorn app.main:app --host … --port …``)
so `src/sidecar.rs` builds one argument list for both.

This file exists because a frozen binary cannot use uvicorn's command line. ``uvicorn`` resolves
``app.main:app`` by importing a module *by name at runtime*, which PyInstaller's static analysis
cannot see and therefore does not bundle. Importing the application object here makes the
dependency explicit, so the whole graph — FastAPI, SQLAlchemy, every plugin the registry loads —
is discoverable at freeze time.

Two behaviours differ from a server deployment and both are deliberate:

**Reload is impossible and not offered.** ``uvicorn``'s reloader re-executes the interpreter,
which does not exist inside a frozen binary.

**The bind address is not configurable to anything routable.** The backend has no
authentication because it is not reachable: it holds the user's entire knowledge graph and
answers whoever asks. ``--host`` exists so the shell can be explicit, not so the user can
publish the API, and a non-loopback value is refused.
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
from typing import Final

#: Exit status used when the arguments describe something we refuse to do. Distinct from
#: argparse's own status 2 so the shell's log tail can tell a bad flag from a refused bind.
EXIT_REFUSED: Final[int] = 3

#: Default bind address, matching ``Settings.api_host``.
DEFAULT_HOST: Final[str] = "127.0.0.1"

#: Default port. The shell always passes an explicit one; this is for a manual run.
DEFAULT_PORT: Final[int] = 8000


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the shell's command line.

    Args:
        argv: Arguments to parse, defaulting to ``sys.argv[1:]``.

    Returns:
        The parsed namespace.
    """
    parser = argparse.ArgumentParser(
        prog="applicantos-server",
        description="Run the ApplicantOS backend for the desktop application.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="Loopback address to bind.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="TCP port to bind.")
    parser.add_argument(
        "--log-level",
        default="info",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        help="uvicorn log level.",
    )
    return parser.parse_args(argv)


def _is_loopback(host: str) -> bool:
    """Whether the address refers only to this machine.

    Args:
        host: The requested bind address.

    Returns:
        ``True`` for a loopback address or ``localhost``.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    """Run the backend until the process is terminated.

    Args:
        argv: Arguments to parse, defaulting to ``sys.argv[1:]``.

    Returns:
        The process exit status.
    """
    args = _parse_args(argv)

    if not _is_loopback(args.host):
        print(
            f"applicantos-server: refusing to bind {args.host!r}. The desktop backend has no "
            "authentication because it is only reachable from this machine; binding a routable "
            "address would publish the user's knowledge graph.",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    # Imported here, not at module scope: an argument error should not pay for importing
    # FastAPI, SQLAlchemy and the plugin registry first.
    import uvicorn

    from app.main import app

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        # The shell terminates this process directly; access logs would duplicate the
        # structlog line the API middleware already emits for every request.
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
