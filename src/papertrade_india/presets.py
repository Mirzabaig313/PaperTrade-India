"""Named broker fee presets.

Hand-curated ``FeeConfig`` instances that map to common Indian discount-
broker schedules. Use as drop-in fee configs::

    from papertrade_india import IndiaPaperBroker
    from papertrade_india.presets import ZERODHA_DELIVERY

    broker = IndiaPaperBroker(fee_config=ZERODHA_DELIVERY)

The defaults baked into ``FeeConfig()`` already match a Zerodha-style
delivery account, so ``ZERODHA_DELIVERY`` is just an alias. The other
presets exist so users don't have to reverse-engineer a competitor's
schedule from first principles.

Sources used (paraphrased for compliance with licensing): public Indian
broker fee structures (Zerodha, Upstox, Groww, Angel One, ICICIdirect)
published 2025–2026. Verify against your contract note before relying.

PRESETS dict at the bottom is for the CLI / configuration loaders.
"""

from __future__ import annotations

from .execution.fees import FeeConfig

# ── Zerodha ────────────────────────────────────────────────────────────


# Delivery: ₹0 brokerage, all statutory components apply.
ZERODHA_DELIVERY = FeeConfig()  # uses package defaults


# Intraday / equity F&O: ₹20 or 0.03% per executed order, whichever is
# lower. STT is asymmetric for intraday but we model delivery-equivalent
# rates here; override stt_pct_buy=0 when running an intraday-only test.
ZERODHA_INTRADAY = FeeConfig(
    brokerage_pct=0.0003,
    brokerage_max=20.0,
)


# ── Upstox ────────────────────────────────────────────────────────────


# Delivery: ₹20 flat per order or 2.5% of turnover, whichever is lower.
UPSTOX_DELIVERY = FeeConfig(
    brokerage_pct=0.025,
    brokerage_max=20.0,
)


UPSTOX_INTRADAY = FeeConfig(
    brokerage_pct=0.0005,    # 0.05%
    brokerage_max=20.0,
)


# ── Groww ─────────────────────────────────────────────────────────────


# Groww: ₹20 flat or 0.1% per executed order (delivery), whichever is lower.
GROWW_DELIVERY = FeeConfig(
    brokerage_pct=0.001,
    brokerage_max=20.0,
)


# ── Angel One ─────────────────────────────────────────────────────────


# Angel One: zero brokerage on delivery, ₹20/order on intraday.
ANGEL_ONE_DELIVERY = FeeConfig()


ANGEL_ONE_INTRADAY = FeeConfig(
    brokerage_flat=20.0,
)


# ── ICICIdirect (full-service) ────────────────────────────────────────


# Full-service brokers charge percentage-of-turnover with no cap. The
# rates below approximate the ICICIdirect "I-Secure" plan; their actual
# tier structure depends on volume and is broker-specific.
ICICIDIRECT_DELIVERY = FeeConfig(
    brokerage_pct=0.0055,    # 0.55%
)


# ── Lookup map ────────────────────────────────────────────────────────


PRESETS: dict[str, FeeConfig] = {
    "zerodha-delivery": ZERODHA_DELIVERY,
    "zerodha-intraday": ZERODHA_INTRADAY,
    "upstox-delivery": UPSTOX_DELIVERY,
    "upstox-intraday": UPSTOX_INTRADAY,
    "groww-delivery": GROWW_DELIVERY,
    "angel-one-delivery": ANGEL_ONE_DELIVERY,
    "angel-one-intraday": ANGEL_ONE_INTRADAY,
    "icicidirect-delivery": ICICIDIRECT_DELIVERY,
}


def get_preset(name: str) -> FeeConfig:
    """Look up a preset by name.

    Case-insensitive; underscores and hyphens treated as equivalent.
    Raises ``KeyError`` with a helpful list of valid names on miss.
    """
    norm = name.lower().replace("_", "-")
    cfg = PRESETS.get(norm)
    if cfg is None:
        raise KeyError(
            f"Unknown preset {name!r}. "
            f"Available: {sorted(PRESETS)}"
        )
    return cfg
