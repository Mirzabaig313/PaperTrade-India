"""Observability hooks.

The broker emits structured events as Python callbacks. The consumer
wires those into whatever observability stack they use (OpenTelemetry,
Prometheus, Datadog, plain logs). The broker itself doesn't pull in any
of those dependencies.

Two parallel mechanisms:

1. **Event log** (``events.py``): persisted to SQLite. Survives process
   restarts. Use for audits, replay, post-hoc analysis.
2. **Callback bus** (this module): in-process, ephemeral. Use for live
   metrics, real-time alerts, log shipping. Multiple subscribers
   supported; one bad subscriber doesn't poison the others.

Callbacks fire AFTER state has been committed. They cannot veto an
order; for that, you want ``risk.py`` (pre-trade) instead.

Failure handling
----------------
A subscriber that raises is logged and skipped. Other subscribers still
run. This is the right tradeoff: the broker's correctness must not
depend on whether a metrics shipper is healthy.

Per-subscription filtering
--------------------------
``EventBus.subscribe(fn, event_types=("order_filled", "order_cancelled"))``
delivers only the listed types to ``fn``. Saves subscribers from doing
their own filter dispatch.

Replay from the persisted log
-----------------------------
``EventBus.replay_from_broker(broker, since=..., event_types=...)``
fetches historical events from the SQLite log and dispatches them to
all current subscribers. Useful when a long-lived subscriber crashes
and needs to catch up, or when adding a subscriber to a process that
has already been running.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid runtime circular import
    from .broker import IndiaPaperBroker  # noqa: F401

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrokerEvent:
    """In-memory mirror of a persisted event, delivered to subscribers.

    Same shape as ``events.Event`` minus the SQLite id (subscribers
    rarely care about it).
    """

    event_type: str
    account_id: str | None
    order_id: str | None
    payload: dict[str, Any]
    # Wall-clock time the event was recorded. Set automatically by the
    # broker when emitting; replay-from-log preserves the original time.
    recorded_at: datetime | None = None


# A subscriber is any callable that takes a BrokerEvent. We wrap it in
# a private record so we can attach a name (for failure logs) and an
# optional filter set.
Subscriber = Callable[[BrokerEvent], None]


@dataclass
class _NamedSub:
    name: str
    fn: Subscriber
    # When non-None, only events whose type is in this set are delivered.
    # None means "deliver everything" (the default).
    event_types: frozenset[str] | None = None


@dataclass
class EventBus:
    """In-process pub/sub for ``BrokerEvent``.

    Thread-safety: subscriber lists are mutated rarely (subscribe at
    startup) and iterated frequently (publish per order). We don't
    bother with a lock — Python's GIL + append-only list iteration is
    safe enough for the typical "register at boot" usage. If you need
    dynamic subscribe/unsubscribe under load, wrap a lock externally.
    """

    subscribers: list[_NamedSub] = field(default_factory=list)

    def subscribe(
        self,
        fn: Subscriber,
        name: str | None = None,
        event_types: Iterable[str] | None = None,
    ) -> None:
        """Register a subscriber.

        Parameters
        ----------
        fn:
            The callable to invoke for each matching event.
        name:
            Used in failure logs. Defaults to ``fn.__name__``.
        event_types:
            When provided, only events whose ``event_type`` is in the
            set are delivered to this subscriber. ``None`` (default)
            delivers every event.
        """
        n = name or getattr(fn, "__name__", repr(fn))
        et = frozenset(event_types) if event_types is not None else None
        self.subscribers.append(_NamedSub(name=n, fn=fn, event_types=et))

    def unsubscribe(self, fn: Subscriber) -> bool:
        """Remove the first subscriber whose ``fn`` matches. Returns
        whether one was removed."""
        for i, sub in enumerate(self.subscribers):
            if sub.fn is fn:
                del self.subscribers[i]
                return True
        return False

    def publish(self, event: BrokerEvent) -> None:
        """Fan out to every matching subscriber. Failures are isolated."""
        if not self.subscribers:
            return
        for sub in self.subscribers:
            if sub.event_types is not None and event.event_type not in sub.event_types:
                continue
            try:
                sub.fn(event)
            except Exception as e:  # noqa: BLE001 — defensive
                logger.exception(
                    "Subscriber %s failed handling %s: %s",
                    sub.name, event.event_type, e,
                )

    def replay_from_broker(
        self,
        broker: IndiaPaperBroker,
        since: datetime | None = None,
        event_types: Iterable[str] | None = None,
        limit: int = 10_000,
    ) -> int:
        """Re-dispatch persisted events to current subscribers.

        Useful for:
          - Bootstrapping a new subscriber after the process has been running.
          - Catching a crashed subscriber up after a restart.

        Returns the count of events replayed (post-filter on subscribers).
        """
        types_tuple = tuple(event_types) if event_types is not None else None
        rows = broker.get_events(limit=limit, event_types=types_tuple)
        # ``get_events`` returns newest-first; replay should be chronological.
        rows = list(reversed(rows))
        if since is not None:
            # Persisted timestamps are tz-aware (IST); coerce a naive
            # ``since`` to the persisted rows' tzinfo so comparison
            # doesn't raise. We don't *change* the wall-clock value —
            # this is a best-effort assumption that callers passing a
            # naive datetime mean it in the broker's local zone.
            if since.tzinfo is None and rows and rows[0].recorded_at.tzinfo is not None:
                since = since.replace(tzinfo=rows[0].recorded_at.tzinfo)
            rows = [r for r in rows if r.recorded_at >= since]
        count = 0
        for row in rows:
            ev = BrokerEvent(
                event_type=row.event_type,
                account_id=row.account_id,
                order_id=row.order_id,
                payload=dict(row.payload),
                recorded_at=row.recorded_at,
            )
            self.publish(ev)
            count += 1
        return count


# Convenience: a logging subscriber for quick smoke tests.
def stdlib_log_subscriber(event: BrokerEvent) -> None:
    """Log every event at INFO. Useful for ``broker.events.subscribe(...)``
    during development."""
    logger.info(
        "event=%s account=%s order=%s payload=%s",
        event.event_type, event.account_id, event.order_id, event.payload,
    )
