"""Indian fee engine.

A real Indian paper broker must simulate fees properly. P&L without fees
is misleading and an agent will systematically over-estimate returns.

Default ``FeeConfig`` matches a typical Indian discount broker (Zerodha-style)
for equity *delivery* in 2026:

- Brokerage: ₹0 for delivery
- STT: 0.1% on both buy and sell
- Exchange charges: 0.00322% (NSE), 0.00375% (BSE)
- GST: 18% on (brokerage + exchange charges)
- SEBI turnover charges: ₹10 per crore
- Stamp duty: 0.015% on buy only
- DP charge: ₹13.5 per sell order

References used (paraphrased and rephrased for compliance with licensing):
public Indian broker fee structures from Zerodha, Upstox, Groww and Angel
One published in 2025–2026.

Pass a custom ``FeeConfig`` to model a different broker's schedule.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .models import Exchange, OrderSide


@dataclass(frozen=True)
class FeeBreakdown:
    """Per-order fee breakdown, all amounts in INR."""

    brokerage: float
    stt: float
    exchange_charge: float
    gst: float
    sebi_charges: float
    stamp_duty: float
    dp_charges: float
    total: float

    def __str__(self) -> str:
        return (
            f"Brokerage: ₹{self.brokerage:.2f}, STT: ₹{self.stt:.2f}, "
            f"Exchange: ₹{self.exchange_charge:.2f}, GST: ₹{self.gst:.2f}, "
            f"SEBI: ₹{self.sebi_charges:.2f}, Stamp: ₹{self.stamp_duty:.2f}, "
            f"DP: ₹{self.dp_charges:.2f}, Total: ₹{self.total:.2f}"
        )


@dataclass(frozen=True)
class FeeConfig:
    """Configurable fee schedule.

    Defaults model a discount-broker delivery account in 2026. Override
    fields to model intraday, full-service brokers, or custom contracts.
    """

    # Brokerage. Discount-broker delivery is ₹0. For intraday or full-service:
    #   - ``brokerage_flat`` charges a fixed amount per order
    #   - ``brokerage_pct`` charges a percentage of turnover
    #   - ``brokerage_max`` caps the percentage charge
    # If ``brokerage_max`` is set (>0), brokerage = min(turnover * pct, max).
    # Otherwise, brokerage = ``brokerage_flat``.
    brokerage_flat: float = 0.0
    brokerage_pct: float = 0.0
    brokerage_max: float = 0.0

    # Statutory taxes/charges (rarely change)
    stt_pct_buy: float = 0.001          # 0.1% on buy
    stt_pct_sell: float = 0.001         # 0.1% on sell
    exchange_charge_nse: float = 0.0000322
    exchange_charge_bse: float = 0.0000375
    gst_pct: float = 0.18                # 18% on brokerage + exchange charge
    sebi_charges_pct: float = 0.000001   # ₹10 per crore
    stamp_duty_pct: float = 0.00015      # 0.015% on buy only

    # DP charge: in reality ~₹13.5–20 per scrip per day on sells. We apply
    # it per sell order in the simulator (very slightly conservative if a
    # user sells the same symbol multiple times in one day).
    dp_charge_per_sell: float = 13.5


def _round_paise(x: float) -> float:
    """Round to paise (2 decimals) using half-up."""
    return float(
        Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


class IndianFeeEngine:
    """Computes Indian-specific brokerage and statutory fees."""

    def __init__(self, config: FeeConfig | None = None) -> None:
        self.config = config or FeeConfig()

    def calculate(
        self,
        side: OrderSide,
        qty: float,
        price: float,
        exchange: Exchange,
    ) -> FeeBreakdown:
        """Compute the full fee breakdown for an executed leg."""
        if qty <= 0 or price <= 0:
            # Zero-fee, zero-everything — keeps callers from special-casing.
            zero = _round_paise(0.0)
            return FeeBreakdown(
                brokerage=zero, stt=zero, exchange_charge=zero, gst=zero,
                sebi_charges=zero, stamp_duty=zero, dp_charges=zero,
                total=zero,
            )

        turnover = qty * price
        cfg = self.config

        # Brokerage: percentage with cap, or flat if no percentage configured.
        if cfg.brokerage_max > 0 and cfg.brokerage_pct > 0:
            brokerage = min(turnover * cfg.brokerage_pct, cfg.brokerage_max)
        elif cfg.brokerage_pct > 0:
            brokerage = turnover * cfg.brokerage_pct
        else:
            brokerage = cfg.brokerage_flat
        brokerage = max(0.0, brokerage)

        # STT — symmetric on buy/sell for delivery
        stt_pct = cfg.stt_pct_buy if side == OrderSide.BUY else cfg.stt_pct_sell
        stt = turnover * stt_pct

        # Exchange charges (depend on venue)
        exch_pct = (
            cfg.exchange_charge_nse if exchange == Exchange.NSE
            else cfg.exchange_charge_bse
        )
        exchange_charge = turnover * exch_pct

        # GST is on (brokerage + exchange charge), not on STT/stamp/SEBI.
        gst = (brokerage + exchange_charge) * cfg.gst_pct

        # SEBI turnover charges
        sebi_charges = turnover * cfg.sebi_charges_pct

        # Stamp duty applies to buys only (in delivery).
        stamp_duty = (
            turnover * cfg.stamp_duty_pct if side == OrderSide.BUY else 0.0
        )

        # DP charge applies to sells only.
        dp_charges = (
            cfg.dp_charge_per_sell if side == OrderSide.SELL else 0.0
        )

        total = (
            brokerage + stt + exchange_charge + gst
            + sebi_charges + stamp_duty + dp_charges
        )

        return FeeBreakdown(
            brokerage=_round_paise(brokerage),
            stt=_round_paise(stt),
            exchange_charge=_round_paise(exchange_charge),
            gst=_round_paise(gst),
            sebi_charges=_round_paise(sebi_charges),
            stamp_duty=_round_paise(stamp_duty),
            dp_charges=_round_paise(dp_charges),
            total=_round_paise(total),
        )
