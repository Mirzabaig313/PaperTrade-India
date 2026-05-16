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


if __name__ == "__main__":  # pragma: no cover
    mcp.run()
