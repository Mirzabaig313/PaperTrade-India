"""Live NSE/BSE trading-holiday source (Upstox market-holidays API).

Replaces hand-maintained ``nse_holidays_*.json`` with the exchange-
published calendar. Upstox's ``/v2/market/holidays`` is **public** (no
auth) and returns every holiday for the year with the list of
``closed_exchanges`` (NSE, BSE, NFO, ...), so one call covers both NSE
and BSE.

Design:
- **No network on import or per-query.** Fetched once, cached to disk
  with a TTL (holidays change ~yearly), and served from memory after.
- **Degrades gracefully.** API down → stale disk cache → empty set. The
  calendar keeps its bundled JSON as the ultimate floor, so a fetch
  failure never leaves the simulator with *no* holidays.
- Lives in ``infrastructure`` and depends on nothing outward (no
  ``providers`` import) to respect the layering rules.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import date
from pathlib import Path
from urllib.error import URLError

logger = logging.getLogger(__name__)

_URL = "https://api.upstox.com/v2/market/holidays"
_DEFAULT_CACHE = Path("data/upstox_holidays_cache.json")
# Cloudflare bans the default Python-urllib UA (Error 1010); use ours.
_UA = "papertrade-india/0.1 (+https://github.com/Mirzabaig313/papertrade-india)"
_MAX_BYTES = 4 * 1024 * 1024


class UpstoxHolidayProvider:
    """Fetches + caches exchange trading holidays from Upstox.

    Parameters
    ----------
    exchange:
        Which exchange's closures to report — ``"NSE"`` (default) or
        ``"BSE"``. A date counts as closed when this exchange is in the
        row's ``closed_exchanges``.
    cache_path:
        Where to cache the raw API rows. Default
        ``data/upstox_holidays_cache.json``.
    ttl_seconds:
        Re-fetch when the cache is older than this. Default 7 days.
    rows:
        Inject pre-parsed API rows (for tests) — skips all network/disk.
    """

    def __init__(
        self,
        exchange: str = "NSE",
        cache_path: Path | str | None = None,
        ttl_seconds: int = 7 * 86_400,
        rows: list[dict] | None = None,
    ) -> None:
        self._exchange = exchange.upper()
        self._cache_path = Path(cache_path) if cache_path else _DEFAULT_CACHE
        self._ttl = int(ttl_seconds)
        self._injected = rows

    def closed_dates(self) -> set[date]:
        """Set of dates on which ``exchange`` is closed for trading."""
        out: set[date] = set()
        for row in self._rows():
            if self._exchange not in (row.get("closed_exchanges") or []):
                continue
            raw = row.get("date")
            if not raw:
                continue
            try:
                out.add(date.fromisoformat(raw))
            except ValueError:
                logger.warning("Unparseable holiday date: %r", raw)
        return out

    # ── Internals ─────────────────────────────────────────────────────

    def _rows(self) -> list[dict]:
        if self._injected is not None:
            return self._injected
        cached = self._read_cache()
        if cached is not None:
            return cached
        try:
            rows = self._fetch()
            self._write_cache(rows)
            return rows
        except (URLError, TimeoutError, OSError, ValueError) as e:
            # Fetch failed — fall back to a stale cache if we have one,
            # else empty (the calendar's bundled JSON is the real floor).
            logger.warning("Holiday API fetch failed (%s); using fallback", e)
            stale = self._read_cache(ignore_ttl=True)
            return stale if stale is not None else []

    def _read_cache(self, ignore_ttl: bool = False) -> list[dict] | None:
        p = self._cache_path
        if not p.exists():
            return None
        if not ignore_ttl and (time.time() - p.stat().st_mtime) > self._ttl:
            return None
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return None

    def _write_cache(self, rows: list[dict]) -> None:
        import os

        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(rows))
            os.replace(tmp, self._cache_path)
        except OSError as e:  # cache is best-effort
            logger.warning("Could not write holiday cache: %s", e)

    def _fetch(self) -> list[dict]:
        req = urllib.request.Request(  # noqa: S310
            _URL, headers={"User-Agent": _UA, "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            body = resp.read(_MAX_BYTES).decode("utf-8", errors="replace")
        payload = json.loads(body)
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("unexpected holidays response shape")
        logger.info("Fetched %d exchange holidays from Upstox", len(rows))
        return rows


__all__ = ["UpstoxHolidayProvider"]
