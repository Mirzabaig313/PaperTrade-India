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
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

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


# A subscriber is any callable that takes a BrokerEvent. Use a dataclass
# wrapper so we can hold metadata (a name, for log messages) without
# requiring subscribers to be a class.
Subscriber = Callable[[BrokerEvent], None]


@dataclass
class _NamedSub:
    name: str
    fn: Subscriber


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

    def subscribe(self, fn: Subscriber, name: str | None = None) -> None:
        """Register a subscriber. ``name`` is used in failure logs."""
        n = name or getattr(fn, "__name__", repr(fn))
        self.subscribers.append(_NamedSub(name=n, fn=fn))

    def unsubscribe(self, fn: Subscriber) -> bool:
        """Remove the first subscriber whose ``fn`` matches. Returns
        whether one was removed."""
        for i, sub in enumerate(self.subscribers):
            if sub.fn is fn:
                del self.subscribers[i]
                return True
        return False

    def publish(self, event: BrokerEvent) -> None:
        """Fan out to every subscriber. Failures are isolated."""
        if not self.subscribers:
            return
        for sub in self.subscribers:
            try:
                sub.fn(event)
            except Exception as e:  # noqa: BLE001 — defensive
                logger.exception(
                    "Subscriber %s failed handling %s: %s",
                    sub.name, event.event_type, e,
                )


# Convenience: a logging subscriber for quick smoke tests.
def stdlib_log_subscriber(event: BrokerEvent) -> None:
    """Log every event at INFO. Useful for ``broker.events.subscribe(...)``
    during development."""
    logger.info(
        "event=%s account=%s order=%s payload=%s",
        event.event_type, event.account_id, event.order_id, event.payload,
    )
