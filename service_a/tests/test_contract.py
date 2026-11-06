"""The API contract and the integer conversions everything else relies on.

| Test | Purpose | Method |
| --- | --- | --- |
| test_contract_lists_every_user_endpoint | The frontend's types cover the documented API | Compare OpenAPI paths with the architecture document's list |
| test_units_round_trip | Amounts survive conversion exactly | Decimal string -> units -> same string |
| test_units_refuse_rounding | No amount is silently rounded | Too many decimals, or not a number -> NotOnGrid |
| test_catalog_requires_exact_quote_units | price x qty must be exact in the quote asset | Inconsistent decimals -> ValueError |
"""
import json

import pytest

from app.config import NotOnGrid, from_units, load_catalog, to_units
from app.main import create_app


def test_contract_lists_every_user_endpoint():
    # Purpose: the OpenAPI contract is what the frontend's types are generated from.
    paths = set(create_app().openapi()["paths"])
    assert {
        "/api/auth/register", "/api/auth/login", "/api/auth/refresh", "/api/auth/logout", "/api/auth/me",
        "/api/accounts/me", "/api/accounts/deposits", "/api/orders", "/api/orders/{order_id}",
        "/api/fills", "/api/market/candles", "/api/market/summary", "/api/stream",
    } <= paths


@pytest.mark.parametrize("text,decimals,units", [("64250.25", 2, 6425025), ("0.001", 3, 1), ("3", 0, 3)])
def test_units_round_trip(text, decimals, units):
    # Purpose: conversion between the API's decimals and internal integers is exact.
    assert to_units(text, decimals) == units
    assert from_units(units, decimals) == text


@pytest.mark.parametrize("text", ["1.234", "abc", "NaN", "Infinity"])
def test_units_refuse_rounding(text):
    # Purpose: an amount off the grid is refused, never rounded.
    with pytest.raises(NotOnGrid):
        to_units(text, 2)


def test_catalog_requires_exact_quote_units(tmp_path):
    # Purpose: if the quote's decimals do not match, price x qty would need rounding.
    path = tmp_path / "symbols.json"
    path.write_text(json.dumps({
        "assets": {"U": {"decimals": 2, "max_deposit": "1"}, "B": {"decimals": 3, "max_deposit": "1"}},
        "symbols": [{"symbol": "B-U", "base": "B", "quote": "U", "tick": 1, "lot": 1, "price_decimals": 2,
                     "qty_decimals": 3, "min_qty": 1, "max_qty": 1, "max_notional": 1}],
    }))
    with pytest.raises(ValueError):
        load_catalog(str(path))
