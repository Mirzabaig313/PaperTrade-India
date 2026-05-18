"""Expose the broker as an MCP server so any LLM agent can use it.

Run::

    pip install 'papertrade-india[mcp]'
    python examples/05_mcp_server.py

Then point your MCP client (Claude Desktop, Cursor, custom agent) at the
running server. The agent can now buy/sell/inspect via tool calls.

This validates the pattern shown by ``Open-Agent-Tools/open-paper-trading-mcp``:
paper trading platforms are an excellent fit for the MCP protocol because
LLMs need a sandboxed environment to act safely.
"""

from __future__ import annotations

try:
    from fastmcp import FastMCP
except ImportError as e:
    raise SystemExit(
        "Install MCP extras first: pip install 'papertrade-india[mcp]'"
    ) from e

from papertrade_india import IndiaPaperBroker, OrderType

broker = IndiaPaperBroker(
    initial_capital=1_000_000,
    db_path="examples/_local/mcp.db",
    account_id="mcp-agent",
)
mcp = FastMCP("papertrade-india")


@mcp.tool()
def buy(symbol: str, qty: float) -> dict:
    """Buy shares on NSE at current market price.

    Returns the filled order details.
    """
    order = broker.buy(symbol, qty)
    return {
        "order_id": order.id,
        "status": order.status.value,
        "fill_price": order.filled_avg_price,
        "fees": order.fees_paid,
    }


@mcp.tool()
def sell(symbol: str, qty: float) -> dict:
    """Sell shares on NSE at current market price."""
    order = broker.sell(symbol, qty)
    return {
        "order_id": order.id,
        "status": order.status.value,
        "fill_price": order.filled_avg_price,
        "fees": order.fees_paid,
        "realized_pl": order.realized_pl,
    }


@mcp.tool()
def buy_limit(symbol: str, qty: float, limit_price: float) -> dict:
    """Place a limit buy order. It will fill when the market reaches ``limit_price``."""
    order = broker.buy(
        symbol, qty,
        order_type=OrderType.LIMIT,
        limit_price=limit_price,
    )
    return {
        "order_id": order.id,
        "status": order.status.value,
        "limit_price": order.limit_price,
    }


@mcp.tool()
def get_positions() -> list[dict]:
    """List all current positions with P&L."""
    return [
        {
            "symbol": p.symbol,
            "qty": p.qty,
            "avg_cost": p.avg_cost,
            "current_price": p.current_price,
            "market_value": p.market_value,
            "unrealized_pl": p.unrealized_pl,
            "unrealized_pl_percent": p.unrealized_pl_percent,
        }
        for p in broker.get_positions()
    ]


@mcp.tool()
def get_account() -> dict:
    """Account summary: cash, equity, P&L."""
    a = broker.get_account()
    return {
        "account_id": a.account_id,
        "equity": a.equity,
        "cash": a.cash,
        "portfolio_value": a.portfolio_value,
        "buying_power": a.buying_power,
        "realized_pl_total": a.realized_pl_total,
        "unrealized_pl_total": a.unrealized_pl_total,
        "currency": a.currency,
    }


@mcp.tool()
def cancel_all_orders() -> dict:
    """Cancel all pending orders. Returns the count cancelled."""
    return {"cancelled": broker.cancel_all_orders()}


# ── Tier-3 surfaces ───────────────────────────────────────────────────


@mcp.tool()
def get_session_phase() -> dict:
    """Current NSE session phase (PRE_OPEN / REGULAR / POST_CLOSE / CLOSED).

    Useful as a guard before submitting market orders — only REGULAR
    accepts market fills; LIMIT orders queue in any phase.
    """
    return {"phase": broker.current_session_phase().value}


@mcp.tool()
def get_cash_ledger(limit: int = 50) -> list[dict]:
    """Recent immutable cash-movement rows, newest first.

    Each row is one of: initial_capital, buy_principal, buy_fees,
    sell_principal, sell_fees, dividend, adjustment.
    """
    return [
        {
            "recorded_at": m.recorded_at.isoformat(),
            "amount": m.amount,
            "reason": m.reason,
            "symbol": m.symbol,
            "order_id": m.order_id,
            "notes": m.notes,
        }
        for m in broker.get_cash_movements(limit=limit)
    ]


@mcp.tool()
def get_recent_events(limit: int = 50, event_type: str | None = None) -> list[dict]:
    """Recent broker events from the persisted log.

    Filter by ``event_type`` (e.g. ``"order_filled"``) when set.
    """
    types = (event_type,) if event_type else None
    return [
        {
            "recorded_at": e.recorded_at.isoformat(),
            "event_type": e.event_type,
            "order_id": e.order_id,
            "payload": e.payload,
        }
        for e in broker.get_events(limit=limit, event_types=types)
    ]


@mcp.tool()
def verify_cash_invariant() -> dict:
    """Audit hook: assert ``account.cash == sum(cash_movements)``.

    Returns a structured result. False here indicates a bug — the
    package's tests prevent this from happening, but exposing the
    check lets an autonomous agent self-audit.
    """
    return {"holds": broker.verify_cash_invariant()}


if __name__ == "__main__":  # pragma: no cover
    mcp.run()
