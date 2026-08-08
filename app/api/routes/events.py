"""``GET /ws`` — the live event stream (``docs/CONTRACTS.md`` §14).

The desktop app is built on the premise that live updates never produce a loading state:
frames from this socket feed ``queryClient.setQueryData`` directly, because
:meth:`app.api.events.EventBus.publish_model` guarantees each payload is byte-for-byte the
body the matching REST endpoint returns. An ``application.status_changed`` frame *is* an
:class:`~app.schemas.application.ApplicationRead`.

This handler is deliberately small; the hard parts live on the bus. What it owns is the
socket's lifecycle, and three properties of that lifecycle are load-bearing:

**Unsubscribing happens in a ``finally``, always.** A disconnect that skipped it would leave
a dead :class:`~app.api.events.Subscription` in the bus's dictionary — and since the desktop
app reconnects on every network blip, on every sleep/wake, and on every dev-server reload,
the leak is per-reconnect and unbounded. Every publish then walks a list of corpses. This is
the single most important line in the module, and it is why the drain loop is wrapped rather
than written as a bare ``while True``.

**A client that stops reading is disconnected, not waited for.**
:meth:`~app.api.events.EventBus.publish` never blocks; it appends to a bounded deque and
drops the *oldest* frame when full, which is correct for a UI fed by ``setQueryData`` because
the freshest state is the correct state. Once a connection has dropped
:data:`~app.api.events.MAX_DROPPED_BEFORE_DISCONNECT` frames it is marked overflowed, and
this handler closes it: a client that far behind will not catch up, and the honest response
is to make it reconnect and refetch.

**The receive side is polled purely to notice a disconnect.** A client that only listens is
the normal case, but without reading from the socket a browser closing the tab would go
unnoticed until the next publish — which, on an idle install, might be hours. The receive
task exists for that, and any frame a client does send is discarded: this stream is
one-directional by design, and accepting commands over it would be a second, unversioned API.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Final

import structlog
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.api.events import DEFAULT_QUEUE_SIZE, Subscription, bus
from app.api.routes._support import csv_list

__all__ = ["MOUNT_AT_ROOT", "PREFIX", "TAGS", "router"]

logger = structlog.get_logger(__name__)

#: ``/ws`` lives at the root, not under ``/api/v1`` (§14).
MOUNT_AT_ROOT: Final[bool] = True

#: No prefix — the path is absolute.
PREFIX: Final[str] = ""

#: OpenAPI tag for this group. WebSocket routes do not appear in the OpenAPI document, but
#: the aggregator reads this attribute from every module and a missing one would default to
#: the module name anyway.
TAGS: Final[list[str]] = ["events"]

#: Close code sent when a consumer fell too far behind to be worth feeding. 1011 ("internal
#: error") would misattribute the fault; 1013 ("try again later") says what actually
#: happened and tells the client to reconnect.
CLOSE_CODE_OVERLOADED: Final[int] = 1013

router = APIRouter()


async def _drain_client(websocket: WebSocket) -> None:
    """Read and discard anything the client sends, returning when it disconnects.

    The stream is one-directional. This coroutine exists only so a disconnect is noticed
    promptly on an idle connection, where no publish would otherwise fail and reveal it.

    Args:
        websocket: The live socket.
    """
    while True:
        try:
            await websocket.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            return
        except Exception as exc:
            logger.debug("events.receive_failed", error=str(exc))
            return


async def _send_batch(websocket: WebSocket, frames: list[Any]) -> bool:
    """Send one drained batch, reporting rather than raising when the peer has gone.

    Args:
        websocket: The live socket.
        frames: The events to deliver, in publication order.

    Returns:
        ``True`` when every frame was written, ``False`` when the socket is gone and the
        loop should stop.
    """
    for frame in frames:
        try:
            await websocket.send_json(frame.to_wire())
        except (WebSocketDisconnect, RuntimeError):
            return False
        except Exception as exc:
            logger.debug("events.send_failed", error=str(exc))
            return False
    return True


async def _pump(websocket: WebSocket, subscription: Subscription) -> None:
    """Forward events to the client until either side goes away.

    Waits on the mailbox and on the client's receive side at once, so a disconnect ends the
    loop immediately rather than at the next publish. Batching is what keeps a burst cheap:
    a discovery run publishing 200 postings becomes a handful of sends, not 200.

    Args:
        websocket: The live socket.
        subscription: This connection's mailbox.
    """
    receiver = asyncio.create_task(_drain_client(websocket), name="ws-receive")
    try:
        while True:
            drain = asyncio.create_task(subscription.drain(), name="ws-drain")
            done, _ = await asyncio.wait({drain, receiver}, return_when=asyncio.FIRST_COMPLETED)

            if receiver in done:
                drain.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await drain
                return

            frames = drain.result()
            if not frames:
                # An empty drain means the subscription was closed — by shutdown, or by
                # the bus tearing down. Nothing more will arrive.
                return
            if not await _send_batch(websocket, frames):
                return

            if subscription.overflowed:
                logger.warning(
                    "events.disconnecting_slow_client",
                    subscription=subscription.id,
                    dropped=subscription.dropped,
                )
                with contextlib.suppress(Exception):
                    await websocket.close(code=CLOSE_CODE_OVERLOADED)
                return
    finally:
        receiver.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await receiver


@router.websocket("/ws")
async def event_stream(
    websocket: WebSocket,
    events: str | None = Query(
        default=None,
        description=(
            "Comma-separated event names to receive; omit for all of them. Unknown names "
            "are ignored rather than refused, so a newer client still connects."
        ),
    ),
    capacity: int = Query(
        default=DEFAULT_QUEUE_SIZE,
        ge=1,
        le=DEFAULT_QUEUE_SIZE * 8,
        description="Frames buffered for this connection before the oldest is discarded.",
    ),
) -> None:
    """Stream typed events to one connected client.

    The subscription is created **after** the handshake is accepted, because
    :class:`~app.api.events.Subscription` binds to the running event loop and a mailbox
    created for a connection that never opened would be a leak with no owner.

    Args:
        websocket: The incoming connection.
        events: Comma-separated event names to filter on. An unrecognised name is dropped
            from the filter rather than rejecting the connection, so a desktop build asking
            for an event this server does not publish still gets the ones it does.
        capacity: Mailbox size before the oldest frame starts being discarded.
    """
    await websocket.accept()
    subscription = bus.subscribe(events=csv_list(events) or None, capacity=capacity)
    logger.info(
        "api.ws_connected",
        subscription=subscription.id,
        subscribers=bus.subscriber_count,
    )
    try:
        await _pump(websocket, subscription)
    finally:
        # The one line this module exists to get right: without it the bus accumulates one
        # dead subscriber per reconnect, and every publish walks them all.
        bus.unsubscribe(subscription)
        logger.info(
            "api.ws_disconnected",
            subscription=subscription.id,
            dropped=subscription.dropped,
            subscribers=bus.subscriber_count,
        )
