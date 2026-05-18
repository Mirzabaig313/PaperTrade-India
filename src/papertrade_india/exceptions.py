"""Domain exceptions for the India paper broker.

Every exception is a subclass of ``IndiaPaperBrokerError`` so callers can
catch broadly when they don't care about the specific failure, or narrowly
when they do (e.g. retry on ``PriceUnavailableError`` but reject on
``InsufficientFundsError``).
"""

from __future__ import annotations


class IndiaPaperBrokerError(Exception):
    """Base for all paper broker errors."""


class InsufficientFundsError(IndiaPaperBrokerError):
    """Tried to buy more than available cash allows (incl. fees)."""


class InsufficientSharesError(IndiaPaperBrokerError):
    """Tried to sell more shares than held."""


class MarketClosedError(IndiaPaperBrokerError):
    """Tried to submit a market order outside NSE trading hours."""


class InvalidOrderError(IndiaPaperBrokerError):
    """Order parameters violate constraints.

    Examples: non-positive qty, missing limit price on a limit order.
    """


class PriceUnavailableError(IndiaPaperBrokerError):
    """All price providers failed and no cached price is available."""


class StalePriceRejected(IndiaPaperBrokerError):  # noqa: N818
    """``enforce_fresh_prices=True`` mode rejected a fill because the
    underlying price came from the long-lived cached fallback rather
    than a live provider.

    Use this mode for autonomous-agent deployments where you'd rather
    halt than execute against a price that may be stale by minutes or
    hours.

    Naming: keeps ``raise StalePriceRejected`` readable; the base class
    already carries the ``Error`` suffix.
    """


class OrderNoLongerPending(IndiaPaperBrokerError):  # noqa: N818
    """Internal: a limit order moved out of PENDING between selection
    and fill (e.g. user cancelled while the watcher was working).

    The watcher catches this and skips the order; callers shouldn't
    normally see it.

    Naming: keeps the conditional read (``raise OrderNoLongerPending``)
    rather than a verbose ``OrderNoLongerPendingError``. The base class
    already carries the ``Error`` suffix.
    """


class AccountNotFoundError(IndiaPaperBrokerError):
    """Strict-open mode: tried to attach to an account that doesn't exist."""


class RiskViolation(IndiaPaperBrokerError):  # noqa: N818
    """A pre-trade risk control rejected the order.

    Examples: order notional exceeds ``max_order_notional``, post-fill
    position exceeds ``max_position_notional`` or its share-of-equity cap,
    symbol not in whitelist.

    Naming: keeps ``raise RiskViolation`` readable; the base class
    already carries the ``Error`` suffix.
    """


class KillSwitchActive(RiskViolation):
    """The broker's kill switch is engaged.

    Either ``RiskConfig.kill_switch=True`` or the env var
    ``PAPERTRADE_INDIA_KILL_SWITCH=1``. All orders are rejected until
    cleared.
    """


class IdempotencyConflict(IndiaPaperBrokerError):  # noqa: N818
    """An idempotency key was reused with different request parameters.

    Replaying with the same key + same params is fine (returns the
    stored order). Replaying with the same key + different params is a
    client bug — almost always a key generated too coarsely for its scope.

    Naming: keeps ``raise IdempotencyConflict`` readable; the base class
    already carries the ``Error`` suffix.
    """


class SymbolNotFound(IndiaPaperBrokerError):  # noqa: N818
    """Symbol master is in strict mode and the symbol isn't registered."""


class SymbolDelisted(IndiaPaperBrokerError):  # noqa: N818
    """Symbol exists in the master but has been marked delisted."""
