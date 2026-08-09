"""Fixtures for the live provider suite.

One job, and it is not optional. Providers are plugin-registry **singletons** and each one
builds its ``httpx.AsyncClient`` lazily and keeps it for the process lifetime — which is
exactly right in production, where there is one event loop for the life of the worker, and
exactly wrong under pytest-asyncio, where every test gets a fresh loop. The second test to
touch a provider inherits a client bound to the first test's closed loop and fails with
``RuntimeError: Event loop is closed`` somewhere unrelated to what it was asserting.

So every provider's transport is released after each test. :meth:`~app.jobs.base.ATSProvider.
aclose` is idempotent and costs nothing on a provider that never made a request, and the next
test rebuilds the client inside its own loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest


@pytest.fixture(autouse=True)
async def release_provider_transports() -> AsyncIterator[None]:
    """Close every provider's HTTP client when a test ends.

    Yields:
        Nothing; the teardown is the point.
    """
    yield

    from app.jobs.registry import all_providers

    for provider in all_providers():
        await provider.aclose()
