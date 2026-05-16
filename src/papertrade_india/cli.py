"""Optional CLI for inspecting account state.

Usage::

    papertrade-india account
    papertrade-india positions
    papertrade-india orders --status pending
    papertrade-india cancel-all
    papertrade-india reset --capital 1000000

The CLI is opt-in (``pip install 'papertrade-india[cli]'``); if Typer is
not installed, importing this module raises a friendly error.

Common flags
------------
``--db PATH``: SQLite file path (default: ``data/india_paper.db``).
``--account ID``: account id (default: ``default``).
"""

from __future__ import annotations

import sys

try:
    import typer
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "papertrade-india CLI requires extras: "
        "pip install 'papertrade-india[cli]'\n"
    )
    raise

from .broker import IndiaPaperBroker
from .exceptions import AccountNotFoundError
from .models import OrderStatus

app = typer.Typer(
    name="papertrade-india",
    help="Inspect and control a local India paper trading broker.",
    no_args_is_help=True,
)
console = Console()


def _broker(db: str, account: str, strict_open: bool = True) -> IndiaPaperBroker:
    """Open a broker against an EXISTING account.

    By default ``strict_open=True``: inspection commands fail fast rather
    than silently spawning a fresh ₹1M account when the user mistypes
    ``--account`` or points at the wrong DB.

    Pass ``strict_open=False`` for the rare commands that legitimately
    create accounts (none of the inspection commands do).
    """
    try:
        return IndiaPaperBroker(
            db_path=db, account_id=account, strict_open=strict_open,
        )
    except AccountNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from e


@app.command()
def account(
    db: str = typer.Option("data/india_paper.db", help="SQLite DB path"),
    account_id: str = typer.Option("default", "--account", help="Account ID"),
) -> None:
    """Show the account summary."""
    b = _broker(db, account_id)
    a = b.get_account()
    table = Table(title=f"Account {a.account_id}")
    table.add_column("Field", style="cyan")
    table.add_column("Value", justify="right")
    rows = [
        ("Equity", f"₹{a.equity:,.2f}"),
        ("Cash", f"₹{a.cash:,.2f}"),
        ("Portfolio value", f"₹{a.portfolio_value:,.2f}"),
        ("Buying power", f"₹{a.buying_power:,.2f}"),
        ("Realized P&L", f"₹{a.realized_pl_total:,.2f}"),
        ("Unrealized P&L", f"₹{a.unrealized_pl_total:,.2f}"),
        ("Currency", a.currency),
    ]
    for k, v in rows:
        table.add_row(k, v)
    console.print(table)


@app.command()
def positions(
    db: str = typer.Option("data/india_paper.db", help="SQLite DB path"),
    account_id: str = typer.Option("default", "--account", help="Account ID"),
) -> None:
    """List all open positions."""
    b = _broker(db, account_id)
    pos = b.get_positions()
    if not pos:
        console.print("[yellow]No open positions.[/yellow]")
        return
    table = Table(title=f"Positions ({account_id})")
    for col in ("Symbol", "Exch", "Qty", "Avg Cost", "Mkt Price",
                "Mkt Value", "Unrealized P&L", "P&L %"):
        table.add_column(col)
    for p in pos:
        table.add_row(
            p.symbol,
            p.exchange.value,
            f"{p.qty:g}",
            f"₹{p.avg_cost:,.2f}",
            f"₹{p.current_price:,.2f}",
            f"₹{p.market_value:,.2f}",
            f"₹{p.unrealized_pl:,.2f}",
            f"{p.unrealized_pl_percent:+.2f}%",
        )
    console.print(table)


@app.command()
def orders(
    db: str = typer.Option("data/india_paper.db", help="SQLite DB path"),
    account_id: str = typer.Option("default", "--account", help="Account ID"),
    status: str | None = typer.Option(
        None, help="Filter by status (pending/filled/cancelled/...)"
    ),
    limit: int = typer.Option(50, help="Max rows to show"),
) -> None:
    """List orders, most recent first."""
    b = _broker(db, account_id)
    s = OrderStatus(status) if status else None
    rows = b.get_orders(status=s, limit=limit)
    if not rows:
        console.print("[yellow]No orders.[/yellow]")
        return
    table = Table(title=f"Orders ({account_id})")
    for col in ("Created", "ID", "Sym", "Side", "Qty", "Type",
                "Status", "Fill Px", "Limit", "Fees", "P&L"):
        table.add_column(col)
    for o in rows:
        table.add_row(
            o.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            o.id,
            o.symbol,
            o.side.value,
            f"{o.qty:g}",
            o.order_type.value,
            o.status.value,
            f"₹{o.filled_avg_price:,.2f}" if o.filled_avg_price else "-",
            f"₹{o.limit_price:,.2f}" if o.limit_price else "-",
            f"₹{o.fees_paid:,.2f}",
            f"₹{o.realized_pl:,.2f}",
        )
    console.print(table)


@app.command("cancel-all")
def cancel_all(
    db: str = typer.Option("data/india_paper.db", help="SQLite DB path"),
    account_id: str = typer.Option("default", "--account", help="Account ID"),
) -> None:
    """Cancel all pending orders."""
    b = _broker(db, account_id)
    n = b.cancel_all_orders()
    console.print(f"[green]Cancelled {n} pending order(s).[/green]")


@app.command()
def reset(
    db: str = typer.Option("data/india_paper.db", help="SQLite DB path"),
    account_id: str = typer.Option("default", "--account", help="Account ID"),
    capital: float | None = typer.Option(
        None, help="Reset cash to this amount (otherwise leave cash as-is)"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip the confirmation prompt",
    ),
) -> None:
    """Reset the account: clear positions, orders, trades."""
    if not yes:
        typer.confirm(
            f"This will delete all positions, orders and trades for "
            f"account '{account_id}'. Continue?",
            abort=True,
        )
    b = _broker(db, account_id)
    b.reset(initial_capital=capital)
    console.print(f"[green]Account '{account_id}' reset.[/green]")


@app.command("expire-day-orders")
def expire_day_orders(
    db: str = typer.Option("data/india_paper.db", help="SQLite DB path"),
    account_id: str = typer.Option("default", "--account", help="Account ID"),
) -> None:
    """Expire all PENDING DAY-tif limit orders. Run at session close."""
    b = _broker(db, account_id)
    n = b.expire_stale_day_orders()
    console.print(f"[green]Expired {n} DAY order(s).[/green]")


@app.command("create-account")
def create_account(
    db: str = typer.Option("data/india_paper.db", help="SQLite DB path"),
    account_id: str = typer.Option(..., "--account", help="Account ID"),
    capital: float = typer.Option(
        1_000_000.0, help="Initial cash for the new account",
    ),
) -> None:
    """Create a new paper-trading account."""
    # strict_open=False here is the *one* command that legitimately
    # creates accounts.
    b = _broker(db, account_id, strict_open=False)
    a = b.get_account()
    console.print(
        f"[green]Account '{a.account_id}' ready with "
        f"₹{a.cash:,.2f} cash.[/green]"
    )


if __name__ == "__main__":  # pragma: no cover
    app()
