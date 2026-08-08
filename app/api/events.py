"""The event bus behind ``GET /ws`` (``docs/CONTRACTS.md`` §14).

The desktop app is built on the premise that live updates never produce a loading state:
the WebSocket feeds ``queryClient.setQueryData`` directly, so **payloads are the same
pydantic schemas the REST endpoints return**. An ``application.status_changed`` frame carries
an :class:`~app.schemas.application.ApplicationRead`, byte-for-byte what
``GET /applications/{id}`` would have returned, which is what lets the client update its
cache without a refetch. :meth:`EventBus.publish_model` is the call site that enforces this.

Three properties are load-bearing, and each exists because of a specific failure:

**A slow client must never grow the server's memory.** Each subscription owns a bounded
:class:`~collections.deque`. When it is full the *oldest* event is discarded and
:attr:`Subscription.dropped` is incremented. Dropping the oldest rather than the newest is
deliberate: for a UI fed by ``setQueryData``, the freshest state is the correct state and a
stale frame is worthless.

**A dead client is disconnected, not awaited.** :meth:`EventBus.publish` is synchronous and
never blocks — it appends to a deque and schedules a wakeup. A consumer that has stopped
draining simply accumulates drops, and once it passes
:data:`MAX_DROPPED_BEFORE_DISCONNECT` it is marked overflowed; the socket handler closes it.
The publisher never waits for a reader, so one hung WebSocket cannot stall a pipeline run.

**Publishing never raises into the caller.** A broadcast happens inside
``Pipeline.submit``. If the bus could raise, a WebSocket bug would fail a real job
application. :meth:`EventBus.publish` therefore swallows and logs everything, and returns the
number of subscribers reached so a caller that cares can tell.

Event names are exactly the ones §14 lists; :data:`EVENT_NAMES` is the closed set, and
publishing an unknown name is logged and refused rather than silently accepted — a typo in
an event name is invisible at the publisher and fatal at the consumer.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import structlog
from pydantic import BaseModel

__all__ = [
    "DEFAULT_QUEUE_SIZE",
    "EVENT_APPLICATION_CREATED",
    "EVENT_APPLICATION_NEEDS_REVIEW",
    "EVENT_APPLICATION_STATUS_CHANGED",
    "EVENT_APPLICATION_SUBMITTED",
    "EVENT_KNOWLEDGE_INDEX_FINISHED",
    "EVENT_KNOWLEDGE_INDEX_PROGRESS",
    "EVENT_KNOWLEDGE_INDEX_STARTED",
    "EVENT_LOG_ENTRY",
    "EVENT_NAMES",
    "EVENT_POSTING_DISCOVERED",
    "EVENT_POSTING_SCORED",
    "EVENT_SESSION_FINISHED",
    "EVENT_SESSION_STARTED",
    "EVENT_SESSION_UPDATED",
    "MAX_DROPPED_BEFORE_DISCONNECT",
    "Event",
    "EventBus",
    "Subscription",
    "bus",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Event vocabulary (docs/CONTRACTS.md §14 — frozen)
# ======================================================================================

EVENT_SESSION_STARTED: Final[str] = "session.started"
EVENT_SESSION_UPDATED: Final[str] = "session.updated"
EVENT_SESSION_FINISHED: Final[str] = "session.finished"
EVENT_POSTING_DISCOVERED: Final[str] = "posting.discovered"
EVENT_POSTING_SCORED: Final[str] = "posting.scored"
EVENT_APPLICATION_CREATED: Final[str] = "application.created"
EVENT_APPLICATION_STATUS_CHANGED: Final[str] = "application.status_changed"
EVENT_APPLICATION_SUBMITTED: Final[str] = "application.submitted"
EVENT_APPLICATION_NEEDS_REVIEW: Final[str] = "application.needs_review"
EVENT_KNOWLEDGE_INDEX_STARTED: Final[str] = "knowledge.index_started"
EVENT_KNOWLEDGE_INDEX_PROGRESS: Final[str] = "knowledge.index_progress"
EVENT_KNOWLEDGE_INDEX_FINISHED: Final[str] = "knowledge.index_finished"
EVENT_LOG_ENTRY: Final[str] = "log.entry"

#: Every event name a publisher may use. Closed by contract: the desktop app switches on
#: these strings, so an unlisted name would arrive as an unhandled frame.
EVENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        EVENT_SESSION_STARTED,
        EVENT_SESSION_UPDATED,
        EVENT_SESSION_FINISHED,
        EVENT_POSTING_DISCOVERED,
        EVENT_POSTING_SCORED,
        EVENT_APPLICATION_CREATED,
        EVENT_APPLICATION_STATUS_CHANGED,
        EVENT_APPLICATION_SUBMITTED,
        EVENT_APPLICATION_NEEDS_REVIEW,
        EVENT_KNOWLEDGE_INDEX_STARTED,
        EVENT_KNOWLEDGE_INDEX_PROGRESS,
        EVENT_KNOWLEDGE_INDEX_FINISHED,
        EVENT_LOG_ENTRY,
    }
)

#: Events buffered per connection before the oldest starts being discarded. Sized for a
#: burst: a discovery run publishes one ``posting.discovered`` per posting, and a 200-posting
#: poll must not cost a healthy client a single frame.
DEFAULT_QUEUE_SIZE: Final[int] = 512

#: Total drops tolerated on one connection before it is disconnected. A client this far
#: behind is not going to catch up, and its queue is pure overhead — the honest response is
#: to close the socket so the client reconnects and refetches.
MAX_DROPPED_BEFORE_DISCONNECT: Final[int] = 2_000

#: Field carrying the event name on the wire.
WIRE_EVENT_KEY: Final[str] = "event"

#: Field carrying the schema payload on the wire.
WIRE_PAYLOAD_KEY: Final[str] = "payload"


# ======================================================================================
# Events
# ======================================================================================


@dataclass(slots=True, frozen=True)
class Event:
    """One broadcast frame.

    Attributes:
        name: The event name, one of :data:`EVENT_NAMES`.
        payload: The serialised schema the REST endpoints return for this resource.
        sequence: Monotonic per-process counter. Lets a client detect that it missed frames
            without the server tracking per-connection acknowledgements.
        at: When the event was published, UTC.
    """

    name: str
    payload: dict[str, Any]
    sequence: int
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_wire(self) -> dict[str, Any]:
        """Render the frame as the JSON object the desktop client consumes.

        Returns:
            ``{"event": …, "payload": …, "sequence": …, "at": …}``.
        """
        return {
            WIRE_EVENT_KEY: self.name,
            WIRE_PAYLOAD_KEY: self.payload,
            "sequence": self.sequence,
            "at": self.at.isoformat(),
        }


# ======================================================================================
# Subscriptions
# ======================================================================================


class Subscription:
    """One live consumer's bounded mailbox.

    Created by :meth:`EventBus.subscribe` and drained by the WebSocket handler. Not
    constructed directly.

    Attributes:
        id: Identifier used in log lines to follow one connection.
    """

    __slots__ = (
        "_capacity",
        "_closed",
        "_dropped",
        "_loop",
        "_names",
        "_overflowed",
        "_queue",
        "_wakeup",
        "id",
    )

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_QUEUE_SIZE,
        names: frozenset[str] | None = None,
    ) -> None:
        """Create an empty mailbox bound to the running event loop.

        Args:
            capacity: Maximum buffered events before the oldest is discarded.
            names: Event names this consumer wants, or ``None`` for all of them.

        Raises:
            RuntimeError: If constructed outside a running event loop, which would leave
                the wakeup unschedulable.
        """
        self.id = uuid.uuid4().hex
        self._capacity = max(1, capacity)
        self._queue: deque[Event] = deque(maxlen=self._capacity)
        self._wakeup = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        self._names = names
        self._dropped = 0
        self._closed = False
        self._overflowed = False

    # -- state ---------------------------------------------------------------------

    @property
    def dropped(self) -> int:
        """Events discarded because this consumer was not draining fast enough."""
        return self._dropped

    @property
    def closed(self) -> bool:
        """Whether this subscription has been closed."""
        return self._closed

    @property
    def overflowed(self) -> bool:
        """Whether this consumer fell so far behind that it must be disconnected."""
        return self._overflowed

    @property
    def pending(self) -> int:
        """Events currently buffered."""
        return len(self._queue)

    def wants(self, name: str) -> bool:
        """Whether this consumer asked for *name*.

        Args:
            name: The event name.

        Returns:
            ``True`` when the subscription is unfiltered or lists this name.
        """
        return self._names is None or name in self._names

    # -- producer side (synchronous, never blocks) ---------------------------------

    def offer(self, event: Event) -> bool:
        """Enqueue *event*, discarding the oldest frame when the mailbox is full.

        Called from :meth:`EventBus.publish`, which may run on any thread. The wakeup is
        therefore scheduled onto the subscription's own loop rather than set directly.

        Args:
            event: The frame to deliver.

        Returns:
            ``True`` when the frame was buffered, ``False`` when the subscription is closed
            or filtered this event out.
        """
        if self._closed or not self.wants(event.name):
            return False
        if len(self._queue) == self._capacity:
            self._dropped += 1
            if self._dropped >= MAX_DROPPED_BEFORE_DISCONNECT and not self._overflowed:
                self._overflowed = True
                logger.warning(
                    "events.subscriber_overflowed",
                    subscription=self.id,
                    dropped=self._dropped,
                )
        self._queue.append(event)
        self._notify()
        return True

    def _notify(self) -> None:
        """Wake the draining coroutine, from whichever thread the publisher is on."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._wakeup.set()
            return
        try:
            self._loop.call_soon_threadsafe(self._wakeup.set)
        except RuntimeError:
            # The consumer's loop has closed; the socket handler will notice on its next
            # pass and unsubscribe. Nothing to do and nothing to report.
            self._closed = True

    # -- consumer side --------------------------------------------------------------

    async def drain(self) -> list[Event]:
        """Wait for at least one event and return everything buffered.

        Returning a batch rather than a single frame is what keeps a burst cheap: a
        discovery run that publishes 200 postings becomes a handful of WebSocket sends
        instead of 200.

        Returns:
            The buffered events in publication order, or an empty list once the
            subscription is closed.
        """
        while True:
            if self._queue:
                batch = list(self._queue)
                self._queue.clear()
                return batch
            if self._closed:
                return []
            self._wakeup.clear()
            # Re-check after clearing: a publisher that appended between the emptiness
            # check and the clear would otherwise have its wakeup erased.
            if self._queue or self._closed:
                continue
            await self._wakeup.wait()

    def close(self) -> None:
        """Mark the subscription closed and wake any coroutine blocked in :meth:`drain`."""
        if self._closed:
            return
        self._closed = True
        self._notify()

    def __repr__(self) -> str:
        """Return a diagnostic summary of this consumer's backlog."""
        return (
            f"<Subscription {self.id} pending={len(self._queue)} "
            f"dropped={self._dropped} closed={self._closed}>"
        )


# ======================================================================================
# The bus
# ======================================================================================


class EventBus:
    """Fan-out of typed events to every live WebSocket subscriber.

    A module-level singleton, :data:`bus`, is what the application uses. Constructing a
    private bus is useful in tests that want isolation.
    """

    __slots__ = ("_sequence", "_subscribers")

    def __init__(self) -> None:
        """Create an empty bus."""
        self._subscribers: dict[str, Subscription] = {}
        self._sequence = 0

    # -- membership --------------------------------------------------------------------

    def subscribe(
        self,
        *,
        events: Iterable[str] | None = None,
        capacity: int = DEFAULT_QUEUE_SIZE,
    ) -> Subscription:
        """Register a consumer and return its mailbox.

        Args:
            events: Event names to receive, or ``None`` for all of them. Unknown names are
                dropped from the filter rather than rejected, so a newer desktop build
                asking for an event this server does not publish still connects.
            capacity: Mailbox size before the oldest event starts being discarded.

        Returns:
            The subscription, already registered.

        Raises:
            RuntimeError: If called outside a running event loop.
        """
        names: frozenset[str] | None = None
        if events is not None:
            requested = {name.strip() for name in events if name and name.strip()}
            known = requested & EVENT_NAMES
            unknown = sorted(requested - EVENT_NAMES)
            if unknown:
                logger.debug("events.unknown_filter_ignored", names=unknown)
            names = frozenset(known) if known else None

        subscription = Subscription(capacity=capacity, names=names)
        self._subscribers[subscription.id] = subscription
        logger.debug(
            "events.subscribed",
            subscription=subscription.id,
            subscribers=len(self._subscribers),
            filtered=names is not None,
        )
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """Remove a consumer and close its mailbox.

        Idempotent, because the WebSocket handler calls it from a ``finally`` that may run
        after an error path already did.

        Args:
            subscription: The subscription to remove.
        """
        removed = self._subscribers.pop(subscription.id, None)
        subscription.close()
        if removed is not None:
            logger.debug(
                "events.unsubscribed",
                subscription=subscription.id,
                dropped=subscription.dropped,
                subscribers=len(self._subscribers),
            )

    @property
    def subscriber_count(self) -> int:
        """Number of live consumers."""
        return len(self._subscribers)

    # -- publishing ---------------------------------------------------------------------

    def publish(self, event: str, payload: dict[str, Any]) -> int:
        """Broadcast one event to every live subscriber.

        Synchronous and non-blocking: it appends to each mailbox and schedules a wakeup.
        Never raises — a failed broadcast must not fail the application submission that
        triggered it.

        Args:
            event: One of :data:`EVENT_NAMES`.
            payload: The serialised schema for this resource, JSON-ready.

        Returns:
            The number of subscribers the frame was buffered for. ``0`` when nobody is
            listening, which is the normal case with the desktop app closed.
        """
        try:
            if event not in EVENT_NAMES:
                logger.warning("events.unknown_event_refused", requested=event)
                return 0
            if not self._subscribers:
                return 0

            self._sequence += 1
            frame = Event(name=event, payload=payload, sequence=self._sequence)

            delivered = 0
            for subscription in list(self._subscribers.values()):
                try:
                    if subscription.offer(frame):
                        delivered += 1
                except Exception as exc:
                    logger.debug(
                        "events.offer_failed",
                        subscription=subscription.id,
                        error=str(exc),
                    )
            return delivered
        except Exception as exc:
            logger.warning("events.publish_failed", requested=event, error=str(exc))
            return 0

    def publish_model(self, event: str, model: BaseModel | None) -> int:
        """Broadcast a pydantic schema as the event payload.

        The preferred publisher. Serialising with ``mode="json"`` here — rather than leaving
        it to the socket handler — is what guarantees the frame is exactly the body the
        matching REST endpoint returns, which is the premise the desktop's ``setQueryData``
        path rests on.

        Args:
            event: One of :data:`EVENT_NAMES`.
            model: The schema instance to send. ``None`` is a no-op, so a caller need not
                branch on "was there anything to publish".

        Returns:
            The number of subscribers reached.
        """
        if model is None:
            return 0
        try:
            payload = model.model_dump(mode="json")
        except Exception as exc:
            logger.warning(
                "events.serialize_failed",
                requested=event,
                model=type(model).__name__,
                error=str(exc),
            )
            return 0
        return self.publish(event, payload)

    async def wait_for_delivery(self, timeout: float = 0.1) -> None:
        """Yield control so pending wakeups run. Test and shutdown helper.

        Args:
            timeout: Upper bound in seconds on how long to wait for mailboxes to drain.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(subscription.pending == 0 for subscription in self._subscribers.values()):
                return
            await asyncio.sleep(0)

    def close(self) -> None:
        """Close every subscription. Called from the application shutdown hook."""
        for subscription in list(self._subscribers.values()):
            self.unsubscribe(subscription)

    def __repr__(self) -> str:
        """Return a diagnostic summary of the bus."""
        return f"<EventBus subscribers={len(self._subscribers)} published={self._sequence}>"


#: The process-wide bus. Import this rather than constructing your own, or a publisher and
#: a subscriber will end up on different buses and the desktop app will show nothing.
bus: Final[EventBus] = EventBus()
