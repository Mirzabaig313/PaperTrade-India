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
