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


@app.command("ledger")
def ledger(
    db: str = typer.Option("data/india_paper.db", help="SQLite DB path"),
    account_id: str = typer.Option("default", "--account", help="Account ID"),
    limit: int = typer.Option(50, help="Max rows to show, newest first"),
) -> None:
    """Show recent rows from the immutable cash-movement ledger."""
    b = _broker(db, account_id)
    movements = b.get_cash_movements(limit=limit)
    if not movements:
        console.print("[yellow]No cash movements.[/yellow]")
        return
    table = Table(title=f"Cash ledger ({account_id}, latest {len(movements)})")
    for col in ("Recorded", "Reason", "Amount", "Symbol", "Order", "Notes"):
        table.add_column(col)
    for m in movements:
        sign = "[green]" if m.amount >= 0 else "[red]"
        table.add_row(
            m.recorded_at.strftime("%Y-%m-%d %H:%M:%S"),
            m.reason,
            f"{sign}₹{m.amount:,.2f}[/]",
            m.symbol or "-",
            (m.order_id or "")[:8] or "-",
            (m.notes or "")[:40],
        )
    console.print(table)


@app.command("verify-invariant")
def verify_invariant(
    db: str = typer.Option("data/india_paper.db", help="SQLite DB path"),
    account_id: str = typer.Option("default", "--account", help="Account ID"),
) -> None:
    """Assert ``account.cash == sum(cash_movements)``. Exit 0 if OK, 3 if drift."""
    b = _broker(db, account_id)
    if b.verify_cash_invariant():
        a = b.get_account()
        console.print(
            f"[green]✓[/green] Cash invariant holds. "
            f"Cash = ₹{a.cash:,.2f}."
        )
        raise typer.Exit(code=0)
    a = b.get_account()
    movements_sum = sum(m.amount for m in b.get_cash_movements(limit=10**9))
    console.print(
        f"[red]✗ Cash invariant broken![/red]\n"
        f"  account.cash      = ₹{a.cash:,.2f}\n"
        f"  sum(movements)    = ₹{movements_sum:,.2f}\n"
        f"  drift             = ₹{a.cash - movements_sum:,.2f}\n"
        f"This indicates a bug — file an issue."
    )
    raise typer.Exit(code=3)


@app.command("events")
def events(
    db: str = typer.Option("data/india_paper.db", help="SQLite DB path"),
    account_id: str = typer.Option("default", "--account", help="Account ID"),
    limit: int = typer.Option(50, help="Max rows, newest first"),
    event_type: str | None = typer.Option(
        None, "--type",
        help="Filter by event type, e.g. 'order_filled'",
    ),
) -> None:
    """Recent rows from the persisted event log."""
    b = _broker(db, account_id)
    types = (event_type,) if event_type else None
    rows = b.get_events(limit=limit, event_types=types)
    if not rows:
        console.print("[yellow]No events.[/yellow]")
        return
    table = Table(title=f"Events ({account_id}, latest {len(rows)})")
    for col in ("Recorded", "Type", "Order", "Payload"):
        table.add_column(col)
    for e in rows:
        # Compact payload — full JSON is too noisy for a terminal table.
        payload_summary = ", ".join(
            f"{k}={v}" for k, v in list(e.payload.items())[:3]
        )
        if len(e.payload) > 3:
            payload_summary += " …"
        table.add_row(
            e.recorded_at.strftime("%Y-%m-%d %H:%M:%S"),
            e.event_type,
            (e.order_id or "")[:8] or "-",
            payload_summary or "-",
        )
    console.print(table)


@app.command("phase")
def phase(
    db: str = typer.Option("data/india_paper.db", help="SQLite DB path"),
    account_id: str = typer.Option("default", "--account", help="Account ID"),
) -> None:
    """Show the current NSE session phase (live time, IST)."""
    b = _broker(db, account_id)
    p = b.current_session_phase()
    next_open = b.calendar.next_open()
    console.print(
        f"Current phase: [cyan]{p.value}[/cyan]\n"
        f"Next REGULAR open: {next_open.isoformat()}"
    )


@app.command("status")
def status(
    db: str = typer.Option("data/india_paper.db", help="SQLite DB path"),
    account_id: str = typer.Option("default", "--account", help="Account ID"),
    ledger_rows: int = typer.Option(5, help="Recent ledger rows to show"),
    event_rows: int = typer.Option(5, help="Recent events to show"),
) -> None:
    """Consolidated audit panel: account + positions + ledger + events + invariant.

    One command instead of five. Use this as the first thing you run
    when something looks wrong; everything you'd want to inspect at a
    glance is here.

    Exit code is 0 normally, 3 if the cash invariant is broken — same
    contract as ``verify-invariant`` so this command can also be used
    in a cron / health-check.
    """
    b = _broker(db, account_id)

    # Account summary
    a = b.get_account()
    acct = Table(title=f"Account {a.account_id}", show_header=False)
    acct.add_column("k", style="cyan")
    acct.add_column("v", justify="right")
    for k, v in (
        ("Equity", f"₹{a.equity:,.2f}"),
        ("Cash", f"₹{a.cash:,.2f}"),
        ("Portfolio value", f"₹{a.portfolio_value:,.2f}"),
        ("Buying power", f"₹{a.buying_power:,.2f}"),
        ("Realized P&L", f"₹{a.realized_pl_total:,.2f}"),
        ("Unrealized P&L", f"₹{a.unrealized_pl_total:,.2f}"),
        ("Phase", b.current_session_phase().value),
    ):
        acct.add_row(k, v)
    console.print(acct)

    # Positions
    pos_list = b.get_positions()
    if pos_list:
        pt = Table(title=f"Open positions ({len(pos_list)})")
        for col in ("Symbol", "Qty", "Avg cost", "Mkt", "Unrealized P&L", "Stale?"):
            pt.add_column(col)
        for p in pos_list:
            pt.add_row(
                p.symbol,
                f"{p.qty:g}",
                f"₹{p.avg_cost:,.2f}",
                f"₹{p.current_price:,.2f}",
                f"₹{p.unrealized_pl:,.2f} ({p.unrealized_pl_percent:+.2f}%)",
                "yes" if p.current_price_stale else "no",
            )
        console.print(pt)
    else:
        console.print("[yellow]No open positions.[/yellow]")

    # Ledger tail
    movements = b.get_cash_movements(limit=ledger_rows)
    if movements:
        lt = Table(title=f"Ledger (latest {len(movements)})")
        for col in ("Recorded", "Reason", "Amount", "Symbol"):
            lt.add_column(col)
        for m in movements:
            sign = "[green]" if m.amount >= 0 else "[red]"
            lt.add_row(
                m.recorded_at.strftime("%Y-%m-%d %H:%M:%S"),
                m.reason,
                f"{sign}₹{m.amount:,.2f}[/]",
                m.symbol or "-",
            )
        console.print(lt)

    # Events tail
    evs = b.get_events(limit=event_rows)
    if evs:
        et = Table(title=f"Events (latest {len(evs)})")
        for col in ("Recorded", "Type", "Order"):
            et.add_column(col)
        for e in evs:
            et.add_row(
                e.recorded_at.strftime("%Y-%m-%d %H:%M:%S"),
                e.event_type,
                (e.order_id or "")[:8] or "-",
            )
        console.print(et)

    # Invariant check
    if b.verify_cash_invariant():
        console.print("[green]✓ Cash invariant holds.[/green]")
    else:
        movements_sum = sum(m.amount for m in b.get_cash_movements(limit=10**9))
        console.print(
            f"[red]✗ Cash invariant broken![/red] "
            f"cash=₹{a.cash:,.2f}  sum(movements)=₹{movements_sum:,.2f}  "
            f"drift=₹{a.cash - movements_sum:,.2f}"
        )
        raise typer.Exit(code=3)


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
