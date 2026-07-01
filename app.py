"""Streamlit console for the papertrade-india simulation.

A local trading-style UI: a dashboard, an order ticket, an editable
watchlist, and an order/position blotter — all driving the real broker.

Run it:

    pip install "papertrade-india[ui]"      # streamlit + the broker
    streamlit run app.py

Smoke-check the wiring without the UI (no streamlit needed):

    python app.py --check

Notes
-----
- State persists in a local SQLite file (``data/india_paper_ui.db``).
- Prices come from a resilient feed: Upstox (live, if
  ``UPSTOX_ACCESS_TOKEN`` is set — any NSE symbol via the instrument
  master) → yfinance → jugaad, each behind a circuit breaker.
- Market-hours / fresh-price gating are OFF so you can test fills any
  time. Flip them on in ``make_broker`` to simulate production.
- Paper-trading only. Not investment advice.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_DB_PATH = _ROOT / "data" / "india_paper_ui.db"
_WATCHLIST_PATH = _ROOT / "data" / "ui_watchlist.json"
_DEFAULT_WATCHLIST = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]


# ── Env + broker construction ─────────────────────────────────────────

def _load_env() -> None:
    """Load KEY=VALUE pairs from the local .env (no python-dotenv dep)."""
    env = _ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = val.strip().strip('"').strip("'")


def make_broker():
    """Broker with a resilient multi-provider feed (Upstox → yf → jugaad)."""
    _load_env()
    from papertrade_india import quickstart, resilient_feed
    from papertrade_india.price_feed import JugaadDataProvider, YFinanceProvider
    from papertrade_india.providers import UpstoxInstrumentMaster, UpstoxProvider

    providers = []
    if os.environ.get("UPSTOX_ACCESS_TOKEN"):
        # Auto-resolve any NSE symbol → instrument_key via the master.
        master = UpstoxInstrumentMaster()
        providers.append(UpstoxProvider(resolve=master.resolve))
    providers += [YFinanceProvider("NS"), JugaadDataProvider()]

    feed = resilient_feed(providers)
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return quickstart(
        db_path=str(_DB_PATH),
        symbol_master=None,          # allow any symbol
        enforce_market_hours=False,  # fill any time (incl. weekends)
        enforce_fresh_prices=False,
        price_feed=feed,
    )


# ── Watchlist persistence ─────────────────────────────────────────────

def load_watchlist() -> list[str]:
    if _WATCHLIST_PATH.exists():
        try:
            data = json.loads(_WATCHLIST_PATH.read_text())
            if isinstance(data, list):
                return [str(s).upper() for s in data if str(s).strip()]
        except (json.JSONDecodeError, OSError):
            pass
    return list(_DEFAULT_WATCHLIST)


def save_watchlist(symbols: list[str]) -> None:
    clean = []
    for s in symbols:
        s = str(s).strip().upper()
        if s and s not in clean:
            clean.append(s)
    _WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _WATCHLIST_PATH.write_text(json.dumps(clean, indent=2))


# ── Self-check (python app.py --check) — no streamlit ─────────────────

def _self_check() -> None:
    from papertrade_india import OrderStatus

    broker = make_broker()
    acct = broker.get_account()
    assert acct.equity > 0, "account should open with positive equity"
    order = broker.buy("RELIANCE", 1)
    assert order.status in (
        OrderStatus.FILLED, OrderStatus.PENDING, OrderStatus.REJECTED,
    ), f"unexpected status {order.status}"
    save_watchlist(_DEFAULT_WATCHLIST)
    assert load_watchlist(), "watchlist round-trip failed"
    print("self-check OK:")
    print(f"  equity={acct.equity:.2f} cash={acct.cash:.2f}")
    print(f"  order {order.id[:8]}: {order.status.value} "
          f"@{order.filled_avg_price} fees={order.fees_paid}")


if "--check" in sys.argv:
    _self_check()
    sys.exit(0)


# ── Streamlit UI ──────────────────────────────────────────────────────

import streamlit as st  # noqa: E402

from papertrade_india import OrderStatus, OrderType  # noqa: E402
from papertrade_india.domain.exceptions import IndiaPaperBrokerError  # noqa: E402

st.set_page_config(page_title="papertrade-india", page_icon="📈", layout="wide")


@st.cache_resource
def _broker():
    return make_broker()


broker = _broker()


def _quote(symbol: str):
    """Best-effort live quote; returns MarketQuote or None."""
    try:
        return broker.price_feed.get_market_quote(symbol)
    except Exception:  # noqa: BLE001
        return None


def _place(symbol: str, qty: float, side: str, otype: str, limit: float):
    fn = broker.buy if side == "BUY" else broker.sell
    kwargs = {"order_type": OrderType[otype]}
    if otype == "LIMIT":
        kwargs["limit_price"] = limit or None
    return fn(symbol, float(qty), **kwargs)


st.title("📈 papertrade-india")
st.caption("Paper-trading simulation. Not investment advice — personal use.")

acct = broker.get_account()
m = st.columns(6)
m[0].metric("Equity", f"₹{acct.equity:,.0f}")
m[1].metric("Cash", f"₹{acct.cash:,.0f}")
m[2].metric("Buying power", f"₹{acct.buying_power:,.0f}")
m[3].metric("Holdings", f"₹{acct.portfolio_value:,.0f}")
m[4].metric("Realized P&L", f"₹{acct.realized_pl_total:,.0f}")
m[5].metric("Unrealized P&L", f"₹{acct.unrealized_pl_total:,.0f}")

tab_dash, tab_trade, tab_watch, tab_orders = st.tabs(
    ["Dashboard", "Trade", "Watchlist", "Orders"]
)

# ── Dashboard ─────────────────────────────────────────────────────────
with tab_dash:
    st.subheader("Positions")
    positions = broker.get_positions()
    if positions:
        st.dataframe(
            [
                {
                    "Symbol": p.symbol, "Qty": p.qty,
                    "Avg cost": round(p.avg_cost, 2),
                    "Price": round(p.current_price, 2),
                    "Mkt value": round(p.market_value, 2),
                    "Unreal P&L": round(p.unrealized_pl, 2),
                    "P&L %": round(p.unrealized_pl_percent, 2),
                    "Basis": p.mark_basis,
                    "Stale": p.current_price_stale,
                }
                for p in positions
            ],
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("No open positions.")

    st.subheader("Watchlist prices")
    if st.button("Refresh prices"):
        st.rerun()
    rows = []
    for sym in load_watchlist():
        q = _quote(sym)
        rows.append({
            "Symbol": sym,
            "Last": round(q.last, 2) if q else None,
            "Bid": round(q.bid, 2) if q and q.bid else None,
            "Ask": round(q.ask, 2) if q and q.ask else None,
            "Source": q.source if q else "—",
            "Real-time": q.is_real_time if q else None,
        })
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)

# ── Trade ─────────────────────────────────────────────────────────────
with tab_trade:
    st.subheader("Order ticket")
    wl = load_watchlist()
    pick = st.selectbox("Symbol (from watchlist)", ["— type below —", *wl])
    typed = st.text_input("…or type any NSE symbol", value="").strip().upper()
    symbol = typed or (pick if pick != "— type below —" else "")

    if symbol:
        q = _quote(symbol)
        if q:
            st.caption(
                f"{symbol}: last ₹{q.last:,.2f} "
                f"(bid {q.bid} / ask {q.ask}) · {q.source} · "
                f"{'live' if q.is_real_time else 'delayed/cache'}"
            )
        else:
            st.caption(f"{symbol}: no quote available")

    with st.form("ticket"):
        c1, c2, c3, c4 = st.columns(4)
        qty = c1.number_input("Qty", min_value=1, value=1, step=1)
        side = c2.selectbox("Side", ["BUY", "SELL"])
        otype = c3.selectbox("Type", ["MARKET", "LIMIT"])
        limit = c4.number_input("Limit ₹", min_value=0.0, value=0.0, step=1.0)
        go = st.form_submit_button("Submit order", type="primary")

    if go:
        if not symbol:
            st.warning("Pick or type a symbol first.")
        else:
            try:
                o = _place(symbol, qty, side, otype, limit)
                if o.status == OrderStatus.REJECTED:
                    st.error(f"Rejected: {o.rejection_reason}")
                else:
                    st.success(
                        f"{side} {qty} {symbol} → {o.status.value} "
                        f"@ ₹{o.filled_avg_price or o.limit_price} "
                        f"(fees ₹{o.fees_paid:.2f})"
                    )
            except IndiaPaperBrokerError as e:
                st.error(f"{type(e).__name__}: {e}")
            except Exception as e:  # noqa: BLE001
                st.error(f"Unexpected: {type(e).__name__}: {e}")

# ── Watchlist (add / edit / remove) ───────────────────────────────────
with tab_watch:
    st.subheader("Edit watchlist")
    st.caption(
        "Add a row and type any NSE symbol. Saved symbols appear in the "
        "Dashboard price board and the Trade dropdown."
    )
    current = load_watchlist()
    edited = st.data_editor(
        [{"Symbol": s} for s in current],
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        key="wl_editor",
    )
    if st.button("Save watchlist", type="primary"):
        syms = [r.get("Symbol", "") for r in edited]
        save_watchlist(syms)
        st.success("Watchlist saved.")
        st.rerun()

# ── Orders ────────────────────────────────────────────────────────────
with tab_orders:
    top = st.columns([3, 1])
    top[0].subheader("Orders")
    if top[1].button("Cancel all pending"):
        n = broker.cancel_all_orders()
        st.success(f"Cancelled {n} order(s).")
        st.rerun()
    orders = broker.get_orders(limit=100)
    if orders:
        st.dataframe(
            [
                {
                    "ID": o.id[:8], "Symbol": o.symbol, "Side": o.side.value,
                    "Type": o.order_type.value, "Qty": o.qty,
                    "Status": o.status.value, "Filled@": o.filled_avg_price,
                    "Limit": o.limit_price, "Fees": round(o.fees_paid, 2),
                    "Realized P&L": round(o.realized_pl, 2),
                    "Created": o.created_at.strftime("%m-%d %H:%M:%S"),
                }
                for o in orders
            ],
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("No orders yet.")
