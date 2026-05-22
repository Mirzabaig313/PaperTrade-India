"""Composite provider — fan-out across multiple sources.

Two strategies, both implemented as :class:`MarketDataProvider`s so they
slot anywhere a single provider would:

- :class:`CompositeProvider` (default: first-wins) — try each provider
  in order, return the first non-``None`` quote. This is what the legacy
  :class:`PriceFeed` already did, lifted into a provider for composability.
- :class:`CompositeProvider` with :class:`MedianAggregation` — fan out
  to every provider in parallel, return the median of the live quotes.
  More realistic than first-wins because no single provider sets the
  fill price; one outlier can't drag a fill price.

The aggregation is a strategy object so callers can write their own
(weighted-average, IQR-trim, "throw out the highest and lowest", etc.)
without subclassing :class:`CompositeProvider`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime

from .base import (
    MarketDataProvider,
    MarketQuote,
    ProviderCapability,
    ProviderInfo,
)

logger = logging.getLogger(__name__)


# An aggregation function takes the live quotes from N providers and
# returns the merged quote. Returning ``None`` means "couldn't form a
# consensus" — the composite then returns ``None`` to its caller.
AggregationFn = Callable[[list[MarketQuote]], MarketQuote | None]


def first_wins(quotes: list[MarketQuote]) -> MarketQuote | None:
    """Return the first available quote (legacy behavior)."""
    return quotes[0] if quotes else None


@dataclass(frozen=True)
class MedianAggregation:
    """Median of last-prices across providers.

    The merged quote uses:
      - ``last`` = median of inputs' ``last``
      - ``bid``/``ask`` = median of inputs that supplied them (or None)
      - ``volume`` = max across inputs (a sane proxy when sources
        disagree — prefer the one that actually saw the trade)
      - ``timestamp`` = max (most recent) across inputs
      - ``source`` = ``"composite-median:<n>"`` where n is the number
        of contributing providers
      - ``is_real_time`` = ``all(inputs.is_real_time)``

    Parameters
    ----------
    min_providers:
        Need at least this many live quotes to form a median; otherwise
        return ``None``. Default 1 (single-provider median == that
        provider's quote).
    max_disagreement_bps:
        If the spread between the lowest and highest input ``last`` is
        wider than this, log a warning and still return the median.
        Default 200 bps (2%). Set to ``None`` to disable the check.
    """

    min_providers: int = 1
    max_disagreement_bps: float | None = 200.0

    def __call__(self, quotes: list[MarketQuote]) -> MarketQuote | None:
        if len(quotes) < self.min_providers:
            return None
        prices = sorted(q.last for q in quotes)
        median_last = _median(prices)

        if self.max_disagreement_bps is not None and len(prices) >= 2:
            spread_bps = (prices[-1] - prices[0]) / median_last * 10000.0
            if spread_bps > self.max_disagreement_bps:
                logger.warning(
                    "Provider disagreement: %.0f bps (low=%.2f, high=%.2f, n=%d)",
                    spread_bps, prices[0], prices[-1], len(prices),
                )

        bids = [q.bid for q in quotes if q.bid is not None]
        asks = [q.ask for q in quotes if q.ask is not None]
        opens = [q.open for q in quotes if q.open is not None]
        highs = [q.high for q in quotes if q.high is not None]
        lows = [q.low for q in quotes if q.low is not None]
        prev_closes = [q.prev_close for q in quotes if q.prev_close is not None]
        volumes = [q.volume for q in quotes if q.volume is not None]
        timestamps = [q.timestamp for q in quotes]

        return MarketQuote(
            last=median_last,
            timestamp=max(timestamps),
            bid=_median(sorted(bids)) if bids else None,
            ask=_median(sorted(asks)) if asks else None,
            open=_median(sorted(opens)) if opens else None,
            high=max(highs) if highs else None,
            low=min(lows) if lows else None,
            prev_close=_median(sorted(prev_closes)) if prev_closes else None,
            volume=max(volumes) if volumes else None,
            currency=quotes[0].currency,
            source=f"composite-median:{len(quotes)}",
            is_real_time=all(q.is_real_time for q in quotes),
        )


def _median(sorted_values: list[float]) -> float:
    n = len(sorted_values)
    if n == 0:
        raise ValueError("median of empty list")
    mid = n // 2
    if n % 2 == 1:
        return sorted_values[mid]
    return (sorted_values[mid - 1] + sorted_values[mid]) / 2.0


class CompositeProvider(MarketDataProvider):
    """Fan-out across providers, then aggregate.

    Defaults to first-wins (legacy semantics). Pass
    ``aggregation=MedianAggregation()`` to fan out to *every* provider
    in parallel and return the median.

    Parameters
    ----------
    providers:
        Ordered list. With first-wins, order matters; with median, order
        only affects log/error reporting.
    aggregation:
        Aggregation strategy. Defaults to :func:`first_wins`.
    parallel:
        When True (default for non-first-wins aggregations), call all
        providers concurrently in a thread pool. When False, call serially
        (cheaper, easier to reason about, fine for first-wins).
    name:
        Optional override for ``info.name``.
    """

    def __init__(
        self,
        providers: Sequence[MarketDataProvider],
        aggregation: AggregationFn = first_wins,
        parallel: bool | None = None,
        name: str = "composite",
    ) -> None:
        self._providers: list[MarketDataProvider] = list(providers)
        self._aggregation = aggregation
        self._parallel = parallel if parallel is not None else (aggregation is not first_wins)
        self._name = name

    @property
    def info(self) -> ProviderInfo:
        # Union of capabilities — composite can deliver anything any of
        # its members can.
        caps = ProviderCapability.NONE
        for p in self._providers:
            caps |= p.capabilities
        return ProviderInfo(
            name=self._name,
            description=f"Composite over {len(self._providers)} providers.",
            capabilities=caps,
            requires_api_key=any(p.info.requires_api_key for p in self._providers),
            requires_network=any(p.info.requires_network for p in self._providers),
            notes=", ".join(p.name for p in self._providers),
        )

    @property
    def providers(self) -> list[MarketDataProvider]:
        return list(self._providers)

    def get_quote(self, symbol: str) -> MarketQuote | None:
        if not self._providers:
            return None

        # First-wins fast path: short-circuit on the first hit.
        if self._aggregation is first_wins and not self._parallel:
            for p in self._providers:
                try:
                    q = p.get_quote(symbol)
                except Exception as e:  # noqa: BLE001 — defensive
                    logger.warning("provider %s raised: %s", p.name, e)
                    continue
                if q is not None:
                    return q
            return None

        # Fan-out path: parallel.
        if self._parallel:
            quotes = self._fanout_parallel(symbol)
        else:
            quotes = self._fanout_serial(symbol)
        return self._aggregation(quotes)

    # ── Internals ─────────────────────────────────────────────────────

    def _fanout_serial(self, symbol: str) -> list[MarketQuote]:
        out: list[MarketQuote] = []
        for p in self._providers:
            q = self._safe_call(p, symbol)
            if q is not None:
                out.append(q)
        return out

    def _fanout_parallel(self, symbol: str) -> list[MarketQuote]:
        out: list[MarketQuote] = []
        with ThreadPoolExecutor(max_workers=len(self._providers)) as pool:
            futures = {
                pool.submit(self._safe_call, p, symbol): p
                for p in self._providers
            }
            for fut in as_completed(futures):
                try:
                    q = fut.result(timeout=15)
                except Exception as e:  # noqa: BLE001 — defensive
                    logger.warning(
                        "provider %s raised: %s",
                        futures[fut].name, e,
                    )
                    continue
                if q is not None:
                    out.append(q)
        return out

    @staticmethod
    def _safe_call(p: MarketDataProvider, symbol: str) -> MarketQuote | None:
        try:
            return p.get_quote(symbol)
        except Exception as e:  # noqa: BLE001 — defensive
            logger.warning("provider %s raised: %s", p.name, e)
            return None


# Convenience: tag now to avoid clock skew across composed quotes.
def _now_str() -> str:
    return datetime.now().isoformat(timespec="seconds")
