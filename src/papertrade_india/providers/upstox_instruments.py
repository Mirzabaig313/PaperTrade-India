"""Upstox instrument-master resolver.

Turns a trading symbol (``"RELIANCE"``) into an Upstox
``instrument_key`` (``"NSE_EQ|INE002A01018"``) by downloading Upstox's
public NSE instrument file once and caching it locally. This is what
lets :class:`~papertrade_india.providers.UpstoxProvider` price *any* NSE
symbol without a hand-maintained map.

The master is a ~2 MB gzipped JSON of ~96k instruments; we keep only the
equity (``segment == "NSE_EQ"``) ``trading_symbol → instrument_key``
mapping in memory. The raw file is cached on disk with a TTL (default 1
day) since instruments change rarely.

Usage::

    master = UpstoxInstrumentMaster()
    provider = UpstoxProvider(resolve=master.resolve)   # any NSE symbol

No network happens until the first :meth:`resolve` call.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_NSE_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
_DEFAULT_CACHE = Path("data/upstox_nse_instruments.json.gz")
# Cloudflare bans the default Python-urllib UA (see UpstoxProvider).
_UA = "papertrade-india/0.1 (+https://github.com/your-org/papertrade-india)"


class UpstoxInstrumentMaster:
    """Lazy, cached symbol → instrument_key resolver for NSE equities.

    Parameters
    ----------
    cache_path:
        Where to cache the downloaded ``.json.gz``. Defaults to
        ``data/upstox_nse_instruments.json.gz``.
    ttl_seconds:
        Re-download when the cache is older than this. Default 1 day.
    records:
        Inject a pre-parsed records list (for tests) — skips all I/O.
    url:
        Override the source URL.
    """

    def __init__(
        self,
        cache_path: Path | str | None = None,
        ttl_seconds: int = 86_400,
        records: list[dict] | None = None,
        url: str = _NSE_URL,
    ) -> None:
        self._cache_path = Path(cache_path) if cache_path else _DEFAULT_CACHE
        self._ttl = int(ttl_seconds)
        self._url = url
        self._injected = records
        self._index: dict[str, str] | None = None
        if records is not None:
            self._index = self._build_index(records)

    # ── Public API ────────────────────────────────────────────────────

    def resolve(self, symbol: str, segment: str = "NSE_EQ") -> str | None:
        """Return the ``instrument_key`` for ``symbol`` (NSE equity)."""
        if segment != "NSE_EQ":
            return None  # this master only indexes NSE equities
        return self._get_index().get(symbol.upper())

    def symbols(self) -> list[str]:
        """All known NSE equity trading symbols (sorted)."""
        return sorted(self._get_index())

    # ── Internals ─────────────────────────────────────────────────────

    def _get_index(self) -> dict[str, str]:
        if self._index is None:
            self._index = self._build_index(self._load_records())
        return self._index

    @staticmethod
    def _build_index(records: list[dict]) -> dict[str, str]:
        index: dict[str, str] = {}
        for rec in records:
            if rec.get("segment") != "NSE_EQ":
                continue
            if rec.get("instrument_type") not in (None, "EQ"):
                continue
            sym = rec.get("trading_symbol") or rec.get("tradingsymbol")
            key = rec.get("instrument_key")
            if sym and key:
                index[sym.upper()] = key
        logger.info("Upstox instrument index: %d NSE equities", len(index))
        return index

    def _load_records(self) -> list[dict]:
        raw = self._read_cache()
        if raw is None:
            raw = self._download()
            self._write_cache(raw)
        return json.loads(gzip.decompress(raw))

    def _read_cache(self) -> bytes | None:
        p = self._cache_path
        if not p.exists():
            return None
        age = time.time() - p.stat().st_mtime
        if age > self._ttl:
            logger.info("Upstox instrument cache stale (%.0fs), refreshing", age)
            return None
        return p.read_bytes()

    def _write_cache(self, raw: bytes) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_bytes(raw)
        except OSError as e:  # cache is best-effort
            logger.warning("Could not write instrument cache: %s", e)

    def _download(self) -> bytes:
        logger.info("Downloading Upstox NSE instrument master from %s", self._url)
        req = urllib.request.Request(  # noqa: S310
            self._url, headers={"User-Agent": _UA, "Accept-Encoding": "gzip"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            return resp.read()


__all__ = ["UpstoxInstrumentMaster"]
