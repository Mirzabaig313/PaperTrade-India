"""Corporate actions: stock splits, bonus issues, rights issues, dividends.

Five methods on the broker — ``apply_split``, ``apply_bonus``,
``apply_rights``, ``apply_dividend`` — each writes a row in
``corporate_actions`` and atomically updates the relevant position(s)
and account cash.

What we model
-------------
- **Splits**: multiply ``qty`` by ``ratio.numerator/denominator``,
  divide ``avg_cost`` by the same ratio. No cash impact. The position's
  total cost basis (``qty * avg_cost``) is preserved, just spread over
  more / fewer shares.
- **Bonus issues**: same arithmetic as a split, but recorded with
  ``action_type='bonus'`` so the audit trail stays distinct. A 1:1
  bonus = 2:1 split-equivalent.
- **Rights issues**: existing shareholders may subscribe to new shares
  at a fixed ``subscription_price`` per the entitlement ratio. The
  user opts in or lets the rights lapse — no automatic dilution.
- **Cash dividends**: credit ``per_share * qty`` to the holder's cash
  on the ex-date. Tax is *not* withheld in the simulator — Indian
  dividend TDS is recipient-specific and beyond scope.

What we don't model
-------------------
- **Demergers / spin-offs** (need a target-symbol mapping).
- **Special dividends with cash + shares**.
- **Mergers / acquisitions** (price discovery and ratio mapping).

Schema-wise, ``action_type`` accepts 'split', 'dividend', 'bonus',
'rights', plus 'merger' and 'spinoff' as forward-compatible
placeholders (no broker methods yet).
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction

SCHEMA = """
CREATE TABLE IF NOT EXISTS corporate_actions (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    -- 'split': stock split / reverse split.
    -- 'bonus': bonus issue (degenerate split with its own audit trail).
    -- 'rights': rights issue (entitlement ratio + subscription_price).
    -- 'dividend': cash dividend.
    -- 'merger', 'spinoff' reserved for future use.
    action_type TEXT NOT NULL CHECK(action_type IN (
        'split', 'dividend', 'bonus', 'rights', 'merger', 'spinoff'
    )),
    -- For splits / bonuses: numerator / denominator. Splits multiply
    -- qty by num/den; bonuses give num new shares per den held.
    -- For rights: entitlement ratio (num new shares per den held).
    -- For dividends: leave NULL; use ``amount_per_share``.
    ratio_num INTEGER,
    ratio_den INTEGER,
    -- For dividends: ₹ / share credit.
    -- For rights: ₹ / share subscription price.
    -- Otherwise NULL.
    amount_per_share REAL,
    ex_date TEXT NOT NULL,
    notes TEXT,
    applied_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_corporate_actions_symbol_date
    ON corporate_actions(symbol, ex_date);
"""


@dataclass(frozen=True)
class CorporateAction:
    id: str
    symbol: str
    exchange: str
    action_type: str         # 'split' | 'dividend'
    ratio_num: int | None
    ratio_den: int | None
    amount_per_share: float | None
    ex_date: str             # ISO date string
    notes: str | None
    applied_at: datetime


def record_split(
    conn: sqlite3.Connection,
    symbol: str,
    exchange: str,
    ratio: Fraction,
    ex_date: str,
    applied_at_iso: str,
    notes: str | None = None,
) -> str:
    return _record_ratio_action(
        conn, symbol=symbol, exchange=exchange, action_type="split",
        ratio=ratio, ex_date=ex_date, applied_at_iso=applied_at_iso,
        notes=notes,
    )


def record_bonus(
    conn: sqlite3.Connection,
    symbol: str,
    exchange: str,
    ratio: Fraction,
    ex_date: str,
    applied_at_iso: str,
    notes: str | None = None,
) -> str:
    """Record a bonus issue.

    A bonus issue gives existing shareholders ``num`` extra shares for
    every ``den`` shares held — economically equivalent to a stock
    split (``ratio_num+den : den``) but with its own audit trail.

    Note that the *split-equivalent* ratio is ``(num + den) / den`` —
    a 1:1 bonus doubles holdings (split-equivalent 2:1), 1:2 bonus =
    3:2 split-equivalent. We store the bonus's bonus-style ratio as
    given so the audit trail stays honest, and the broker computes
    the split equivalent at apply time.
    """
    return _record_ratio_action(
        conn, symbol=symbol, exchange=exchange, action_type="bonus",
        ratio=ratio, ex_date=ex_date, applied_at_iso=applied_at_iso,
        notes=notes,
    )


def record_rights(
    conn: sqlite3.Connection,
    symbol: str,
    exchange: str,
    ratio: Fraction,
    subscription_price: float,
    ex_date: str,
    applied_at_iso: str,
    notes: str | None = None,
) -> str:
    """Record a rights issue.

    Existing shareholders are entitled to subscribe to ``num`` new
    shares for every ``den`` held, at a fixed ``subscription_price``
    that's typically below market. We store the entitlement ratio in
    ``ratio_num`` / ``ratio_den`` and the price in ``amount_per_share``
    (overloading that column — semantics swap based on
    ``action_type``).

    The simulator's :meth:`IndiaPaperBroker.apply_rights` records the
    action and increments the position by the subscription if the
    user opts in. Skipping is the default — the rights then lapse
    (no position change).
    """
    action_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO corporate_actions "
        "(id, symbol, exchange, action_type, ratio_num, ratio_den, "
        "amount_per_share, ex_date, notes, applied_at) "
        "VALUES (?, ?, ?, 'rights', ?, ?, ?, ?, ?, ?)",
        (
            action_id, symbol, exchange,
            ratio.numerator, ratio.denominator,
            subscription_price, ex_date, notes, applied_at_iso,
        ),
    )
    return action_id


def _record_ratio_action(
    conn: sqlite3.Connection,
    symbol: str,
    exchange: str,
    action_type: str,
    ratio: Fraction,
    ex_date: str,
    applied_at_iso: str,
    notes: str | None,
) -> str:
    action_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO corporate_actions "
        "(id, symbol, exchange, action_type, ratio_num, ratio_den, "
        "amount_per_share, ex_date, notes, applied_at) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
        (
            action_id, symbol, exchange, action_type,
            ratio.numerator, ratio.denominator,
            ex_date, notes, applied_at_iso,
        ),
    )
    return action_id


def record_dividend(
    conn: sqlite3.Connection,
    symbol: str,
    exchange: str,
    amount_per_share: float,
    ex_date: str,
    applied_at_iso: str,
    notes: str | None = None,
) -> str:
    action_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO corporate_actions "
        "(id, symbol, exchange, action_type, ratio_num, ratio_den, "
        "amount_per_share, ex_date, notes, applied_at) "
        "VALUES (?, ?, ?, 'dividend', NULL, NULL, ?, ?, ?, ?)",
        (
            action_id, symbol, exchange, amount_per_share,
            ex_date, notes, applied_at_iso,
        ),
    )
    return action_id


def list_for_symbol(
    conn: sqlite3.Connection,
    symbol: str,
    limit: int = 100,
) -> list[CorporateAction]:
    rows = conn.execute(
        "SELECT id, symbol, exchange, action_type, ratio_num, ratio_den, "
        "amount_per_share, ex_date, notes, applied_at "
        "FROM corporate_actions WHERE symbol = ? "
        "ORDER BY ex_date DESC, applied_at DESC LIMIT ?",
        (symbol, limit),
    ).fetchall()
    return [
        CorporateAction(
            id=r["id"],
            symbol=r["symbol"],
            exchange=r["exchange"],
            action_type=r["action_type"],
            ratio_num=r["ratio_num"],
            ratio_den=r["ratio_den"],
            amount_per_share=r["amount_per_share"],
            ex_date=r["ex_date"],
            notes=r["notes"],
            applied_at=datetime.fromisoformat(r["applied_at"]),
        )
        for r in rows
    ]
