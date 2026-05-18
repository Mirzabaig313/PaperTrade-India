# Indian fee model

How `IndianFeeEngine` computes fees on each order leg.

## Defaults at a glance

| Component | Default | Buy | Sell | Notes |
|:---|:---|:---:|:---:|:---|
| Brokerage | ₹0 | ✓ | ✓ | Discount-broker delivery; intraday clients should override |
| STT | 0.1% of turnover | ✓ | ✓ | Symmetric on both sides for delivery |
| Exchange charge | 0.00322% (NSE), 0.00375% (BSE) | ✓ | ✓ | Venue-specific |
| GST | 18% × (brokerage + exchange charge) | ✓ | ✓ | Not on STT/SEBI/stamp |
| SEBI charges | 0.0001% (₹10 per crore) | ✓ | ✓ | |
| Stamp duty | 0.015% of turnover | ✓ |   | Buy only (delivery) |
| DP charge | ₹13.5 flat | | ✓ | Per sell order in this simulator |

Sources for the rates: paraphrased from public Indian discount-broker
fee structures (Zerodha, Upstox, Groww, Angel One) published 2025–2026.
Content was rephrased for compliance with licensing.

## Formulas

Let:

- `T = qty × price` (turnover, INR)
- `cfg` = the active `FeeConfig`
- `side` ∈ {`BUY`, `SELL`}
- `exch` ∈ {`NSE`, `BSE`}

Then:

```
brokerage =
    min(T × cfg.brokerage_pct, cfg.brokerage_max)   if cfg.brokerage_max > 0 and cfg.brokerage_pct > 0
    T × cfg.brokerage_pct                           if cfg.brokerage_pct > 0
    cfg.brokerage_flat                              otherwise

stt = T × (cfg.stt_pct_buy if side == BUY else cfg.stt_pct_sell)

exchange_charge = T × (
    cfg.exchange_charge_nse if exch == NSE
    else cfg.exchange_charge_bse
)

gst = (brokerage + exchange_charge) × cfg.gst_pct

sebi_charges = T × cfg.sebi_charges_pct

stamp_duty = T × cfg.stamp_duty_pct  if side == BUY  else  0

dp_charges = cfg.dp_charge_per_sell  if side == SELL else  0

total = brokerage + stt + exchange_charge + gst
      + sebi_charges + stamp_duty + dp_charges
```

Each component is rounded to paise (2 decimals, half-up) independently;
`total` is rounded the same way. Tests pin `|total - sum(components)| ≤
₹0.05` to absorb the rounding drift.

## Worked example

Buy 10 shares of RELIANCE at ₹2500 on NSE, default `FeeConfig`:

- T = 10 × 2500 = ₹25,000
- brokerage = ₹0 (delivery default)
- stt = 25000 × 0.001 = ₹25.00
- exchange = 25000 × 0.0000322 ≈ ₹0.81 → ₹0.81
- gst = (0 + 0.81) × 0.18 ≈ ₹0.15 → ₹0.15
- sebi = 25000 × 0.000001 = ₹0.025 → ₹0.03
- stamp = 25000 × 0.00015 = ₹3.75
- dp = ₹0 (buy)
- **total ≈ ₹29.74**

For a matched sell of the same 10 shares at ₹2500:

- stt = ₹25.00
- exchange = ₹0.81
- gst = ₹0.15
- sebi = ₹0.03
- stamp = ₹0
- dp = ₹13.50
- **total ≈ ₹39.49**

Round-trip total fees ≈ **₹69.23**. That's the realistic cost the
simulator subtracts from your P&L.

## Customizing for your broker

Override fields on `FeeConfig`. Three common scenarios:

### Discount-broker intraday (₹20 or 0.03%, capped)

```python
from papertrade_india import FeeConfig
cfg = FeeConfig(
    brokerage_pct=0.0003,    # 0.03%
    brokerage_max=20.0,      # ₹20 cap
)
```

### Full-service broker (flat brokerage)

```python
cfg = FeeConfig(
    brokerage_flat=50.0,     # ₹50 per order
)
```

### BSE-primary trading

The default `FeeConfig` already supports BSE — pass
`exchange=Exchange.BSE` to `IndiaPaperBroker` and the engine picks the
BSE exchange-charge rate automatically.

## What the model does not include

- **Securities lending fees / interest** (not relevant for cash delivery)
- **DDPI / CDSL annual maintenance** (charged annually, not per trade)
- **GST on DP charge** (typically rolled into the stated DP rate)
- **Statement / contract-note fees** (broker-specific, often zero)
- **Pledging/unpledging charges** (not modeled — no margin)

These are either negligible per-trade or out of scope for an equity
delivery simulator. If your broker bills you differently, override the
relevant `FeeConfig` field or open an issue.

## Verifying against your broker's contract note

For each leg, the engine produces a `FeeBreakdown` with all seven
components broken out individually. To compare against a real contract
note:

```python
from papertrade_india import IndianFeeEngine, FeeConfig, OrderSide, Exchange

fb = IndianFeeEngine().calculate(
    side=OrderSide.BUY,
    qty=10,
    price=2500.0,
    exchange=Exchange.NSE,
)
print(fb)
# Brokerage: ₹0.00, STT: ₹25.00, Exchange: ₹0.81, GST: ₹0.15,
# SEBI: ₹0.03, Stamp: ₹3.75, DP: ₹0.00, Total: ₹29.74
```

Map line items 1:1 against your broker's PDF. If numbers diverge, the
issue is almost always one of:

1. Wrong fee schedule (intraday vs delivery vs F&O — you need a
   different `FeeConfig`).
2. Statutory rate change (when the government tweaks STT or GST mid-year).
3. Broker promotional rebate or tier-based pricing.

For (1), construct the right `FeeConfig`. For (2)/(3), open an issue or PR.
