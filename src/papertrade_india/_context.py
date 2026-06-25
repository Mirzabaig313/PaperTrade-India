"""Shared collaborator bundle handed to every subsystem function.

Subsystems take this as their first argument rather than a back-reference
to ``IndiaPaperBroker``. This breaks circular imports (subsystems can
import the context dataclass without importing the broker module) and
keeps subsystem-level tests cheap (build a fake context, no full broker
boot).

The context is mostly immutable: every field except ``pending_events``
is set once at broker construction and read by subsystems. Event
emission appends to ``pending_events`` inside a transaction; the broker
drains it post-commit via :meth:`drain_pending_events`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .domain.models import Exchange
    from .domain.rules.risk import RiskEngine
    from .domain.rules.tick_lot_band import MicrostructureConfig
    from .execution.book import OrderBookSimulator
    from .execution.fees import FeeSchedule, IndianFeeEngine
    from .execution.settlement import SettlementEngine
    from .execution.simulation import LatencySimulator, RejectionSimulator
    from .execution.slippage import SlippageConfig
    from .infrastructure.clock import Clock
    from .infrastructure.market_hours import NSECalendar
    from .infrastructure.observability import BrokerEvent, EventBus
    from .infrastructure.persistence import Persistence
    from .infrastructure.symbols import SymbolMaster
    from .orders.partial_fills import PartialFillConfig
    from .price_feed import PriceFeed


@dataclass
class BrokerContext:
    """Bundle of collaborators passed to subsystem functions.

    Mutability rule
    ---------------
    Treat every field except ``pending_events`` as read-only. The list is
    appended to during a transaction (via :meth:`emit`) and drained after
    the transaction commits (via :meth:`drain_pending_events`).
    """

    account_id: str
    default_exchange: "Exchange"

    persistence: "Persistence"
    price_feed: "PriceFeed"
    calendar: "NSECalendar"

    fee_schedule: "FeeSchedule"
    slippage_config: "SlippageConfig"
    risk_engine: "RiskEngine"
    symbol_master: "SymbolMaster"

    microstructure_config: "MicrostructureConfig"
    book_sim: "OrderBookSimulator"
    settlement: "SettlementEngine"
    latency_sim: "LatencySimulator"
    reject_sim: "RejectionSimulator"
    partial_fill_config: "PartialFillConfig"

    events: "EventBus"
    clock: "Clock"

    enforce_market_hours: bool
    enforce_fresh_prices: bool
    mark_to_bid: bool

    pending_events: list["BrokerEvent"] = field(default_factory=list)

    # ── Helpers used by subsystems ────────────────────────────────────

    def now_iso(self) -> str:
        """ISO timestamp of the broker's current 'now' (clock-aware)."""
        return self.clock.now().isoformat()

    def fee_engine_for(self, when_iso: str) -> "IndianFeeEngine":
        """Build the fee engine for an order's trade date."""
        from .execution.fees import IndianFeeEngine

        d = datetime.fromisoformat(when_iso).date()
        return IndianFeeEngine(self.fee_schedule.config_on(d))

    def emit(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        order_id: str | None = None,
        payload: dict | None = None,
    ) -> None:
        """Persist an event row AND queue it for post-commit publication.

        The persistence write is part of the caller's transaction, so
        the event log can never disagree with what was committed.
        Subscribers see the event only after the caller invokes
        :meth:`drain_pending_events` — typically right after
        ``with persistence.transaction()`` exits.
        """
        from .infrastructure import events as _events
        from .infrastructure.observability import BrokerEvent

        recorded_at = self.now_iso()
        _events.emit(
            conn,
            event_type=event_type,
            account_id=self.account_id,
            order_id=order_id,
            payload=payload or {},
            recorded_at_iso=recorded_at,
        )
        self.pending_events.append(
            BrokerEvent(
                event_type=event_type,
                account_id=self.account_id,
                order_id=order_id,
                payload=dict(payload or {}),
                recorded_at=datetime.fromisoformat(recorded_at),
            )
        )

    def drain_pending_events(self) -> None:
        """Publish queued events to the bus. Call after each transaction
        commits, so subscribers don't see uncommitted state."""
        if not self.pending_events:
            return
        events_to_send = self.pending_events
        self.pending_events = []
        for ev in events_to_send:
            self.events.publish(ev)


__all__ = ["BrokerContext"]
