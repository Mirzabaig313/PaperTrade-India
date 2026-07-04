"""Contract test: ``IndiaPaperBroker`` keeps an Alpaca-style shape.

The package advertises an Alpaca-style interface (same method
signatures), so downstream code can swap brokers without changes. This
test pins that claim by encoding Alpaca's public broker shape as a
fixture and checking the broker stays compatible with it.

What we check
-------------
For each method on the contract:

  - It exists on ``IndiaPaperBroker``.
  - Its required parameters (positional-or-keyword without a default)
    are a superset of Alpaca's required parameters.
  - The optional parameters Alpaca offers are also offered (so a
    caller passing them through doesn't break).

We deliberately don't check *exact* signatures — this package adds
params Alpaca doesn't have (``idempotency_key``, ``time_in_force``,
``product_type``, ...), and that's fine: compatibility holds *up to*
additive extensions.
"""

from __future__ import annotations

import inspect

import pytest

from papertrade_india import IndiaPaperBroker

# Encoded contract: the Alpaca-style broker methods callers rely on.
# Shape: { method_name: { "required": [params], "optional": [params] } }
ALPACA_CONTRACT: dict[str, dict[str, list[str]]] = {
    "buy": {
        "required": ["symbol", "qty"],
        "optional": ["order_type", "limit_price"],
    },
    "sell": {
        "required": ["symbol", "qty"],
        "optional": ["order_type", "limit_price"],
    },
    "get_account": {
        "required": [],
        "optional": [],
    },
    "get_positions": {
        "required": [],
        "optional": [],
    },
    "get_position": {
        "required": ["symbol"],
        "optional": [],
    },
    "get_orders": {
        "required": [],
        "optional": ["status"],
    },
    "cancel_order": {
        "required": ["order_id"],
        "optional": [],
    },
    "cancel_all_orders": {
        "required": [],
        "optional": [],
    },
}


def _params(method) -> dict[str, inspect.Parameter]:
    """Return the named parameters of ``method`` (excluding ``self``)."""
    sig = inspect.signature(method)
    return {
        name: p for name, p in sig.parameters.items() if name != "self"
    }


@pytest.mark.parametrize("method_name", sorted(ALPACA_CONTRACT))
def test_method_exists(method_name: str):
    assert hasattr(IndiaPaperBroker, method_name), (
        f"IndiaPaperBroker is missing the {method_name!r} method "
        f"that an Alpaca-style broker exposes"
    )


@pytest.mark.parametrize("method_name", sorted(ALPACA_CONTRACT))
def test_required_params_are_a_subset(method_name: str):
    """India broker must accept all of Alpaca's required params.

    "Subset" because the India broker may have *additional* required
    params (none today) — the test catches the regression where a
    caller's existing call site stops working because we tightened
    something. We never tighten requirements.
    """
    contract = ALPACA_CONTRACT[method_name]
    method = getattr(IndiaPaperBroker, method_name)
    params = _params(method)

    for required in contract["required"]:
        assert required in params, (
            f"{method_name}: required Alpaca param {required!r} "
            f"missing from IndiaPaperBroker.{method_name} signature: "
            f"{list(params)}"
        )
        # And it should still be passable positionally — callers
        # commonly use positional args.
        p = params[required]
        assert p.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        ), f"{method_name}.{required} is no longer positional"


@pytest.mark.parametrize("method_name", sorted(ALPACA_CONTRACT))
def test_optional_params_present(method_name: str):
    """India broker must offer all of Alpaca's optional params, with defaults."""
    contract = ALPACA_CONTRACT[method_name]
    method = getattr(IndiaPaperBroker, method_name)
    params = _params(method)

    for optional in contract["optional"]:
        assert optional in params, (
            f"{method_name}: optional Alpaca param {optional!r} "
            f"missing from IndiaPaperBroker.{method_name}"
        )
        p = params[optional]
        assert p.default is not inspect.Parameter.empty, (
            f"{method_name}.{optional} should have a default to match "
            f"Alpaca's optional contract"
        )


def test_buy_returns_order(broker, stub_provider):
    """Smoke check the runtime contract: buy returns an Order with the
    fields an Alpaca-style caller reads off order objects."""
    stub_provider.set("RELIANCE", 1000)
    order = broker.buy("RELIANCE", 1)
    # These are the attributes Alpaca-style order consumers use.
    for attr in ("id", "symbol", "qty", "status", "filled_qty",
                 "filled_avg_price", "created_at"):
        assert hasattr(order, attr), f"Order missing {attr!r}"


def test_account_has_alpaca_compatible_fields(broker):
    """Alpaca's Account exposes equity/cash/buying_power/portfolio_value."""
    a = broker.get_account()
    for attr in ("equity", "cash", "buying_power", "portfolio_value"):
        assert hasattr(a, attr), f"Account missing {attr!r}"
        assert isinstance(getattr(a, attr), (int, float))


def test_position_has_alpaca_compatible_fields(broker, stub_provider):
    """Alpaca's Position exposes
    symbol/qty/market_value/cost_basis/unrealized_pl/unrealized_pl_percent/current_price."""
    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 1)
    p = broker.get_position("RELIANCE")
    assert p is not None
    for attr in ("symbol", "qty", "market_value", "cost_basis",
                 "unrealized_pl", "unrealized_pl_percent", "current_price"):
        assert hasattr(p, attr), f"Position missing {attr!r}"
