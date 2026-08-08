"""Is the worker tier alive? — a process-exit healthcheck for containers and supervisors.

Docker's ``HEALTHCHECK``, Kubernetes' ``exec`` probe and most process supervisors judge a
process by its exit code, not by what it prints. This module is that entry point:

.. code-block:: console

   $ python -m app.workers.healthcheck            # broker reachable?  0 / 1
   $ python -m app.workers.healthcheck --workers  # ...and is anybody consuming?

**The broker check is the real one.** A worker that cannot reach its broker consumes
nothing, silently, forever — no error, no crash, just a queue that grows. That is the failure
this probe exists to catch, and it is checked with a bounded ``ensure_connection`` and no
retries, because a probe that blocks is a probe that hangs the thing probing it.

**The worker ping is opt-in**, because it means something different depending on where the
probe runs. Inside a worker container, "no workers responded" means this container is broken.
Inside the API container it means only that the worker tier is down — which is a degraded
install, not a broken API, and ``app.api.tasks`` already reports that to the user as a 202
with ``degraded`` set rather than a 500. Failing the API's probe for it would take a
perfectly usable read-only install offline.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Final

import structlog

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "EXIT_FAILURE",
    "EXIT_SUCCESS",
    "celery_healthcheck",
    "main",
    "ping_workers",
]

logger = structlog.get_logger(__name__)

#: Seconds to wait for the broker (and, when asked, for a worker to answer). Short: a probe
#: runs on a timer and a slow answer is a failed answer.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 5.0

#: Process exit code meaning "healthy".
EXIT_SUCCESS: Final[int] = 0

#: Process exit code meaning "unhealthy". Any failure maps here — a probe that distinguished
#: failure modes by exit code would be misread by every supervisor that consumes it.
EXIT_FAILURE: Final[int] = 1


def celery_healthcheck(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> bool:
    """Return whether the configured Celery broker is reachable.

    Opens one connection with retries disabled and closes it. Never raises: an unreachable
    broker is an expected operational state that must be *reported*, not propagated.

    Args:
        timeout: Seconds to wait for the connection.

    Returns:
        ``True`` when the broker answered, ``False`` on any transport or configuration
        failure — including Celery not being installed, which for a worker probe is the same
        answer.
    """
    try:
        from app.workers.celery_app import celery_app
    except ImportError as exc:
        logger.error("workers.healthcheck_celery_missing", error=str(exc))
        return False

    connection: Any = None
    try:
        connection = celery_app.connection()
        connection.ensure_connection(max_retries=0, timeout=timeout)
    except Exception as exc:
        logger.error(
            "workers.healthcheck_broker_unreachable",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False
    else:
        logger.info("workers.healthcheck_broker_ok")
        return True
    finally:
        if connection is not None:
            try:
                connection.release()
            except Exception as exc:
                logger.debug("workers.healthcheck_release_failed", error=str(exc))


def ping_workers(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> int:
    """Return how many workers answered a control ping.

    Args:
        timeout: Seconds to wait for replies.

    Returns:
        The number of responding workers. ``0`` when none answered or the ping itself
        failed — the two are indistinguishable to a probe, and both mean "nothing is
        consuming".
    """
    try:
        from app.workers.celery_app import celery_app
    except ImportError as exc:
        logger.error("workers.healthcheck_celery_missing", error=str(exc))
        return 0

    try:
        replies = celery_app.control.ping(timeout=timeout) or []
    except Exception as exc:
        logger.error(
            "workers.healthcheck_ping_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return 0
    logger.info("workers.healthcheck_ping", workers=len(replies))
    return len(replies)


def main(argv: list[str] | None = None) -> int:
    """Run the probe and return a process exit code.

    Args:
        argv: Command-line arguments, or ``None`` to read :data:`sys.argv`.

    Returns:
        :data:`EXIT_SUCCESS` when every requested check passed, :data:`EXIT_FAILURE`
        otherwise.
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.workers.healthcheck",
        description="Exit 0 when the Celery broker is reachable, 1 otherwise.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Seconds to wait (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--workers",
        action="store_true",
        help="Also require at least one worker to answer a control ping.",
    )
    args = parser.parse_args(argv)

    if not celery_healthcheck(args.timeout):
        print("celery: broker unreachable", file=sys.stderr)
        return EXIT_FAILURE

    if args.workers:
        responded = ping_workers(args.timeout)
        if responded < 1:
            print("celery: broker reachable, but no workers responded", file=sys.stderr)
            return EXIT_FAILURE
        print(f"celery: ok ({responded} worker(s))")
        return EXIT_SUCCESS

    print("celery: ok")
    return EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
