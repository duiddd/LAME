from d.rules import Opposite, decide

G = frozenset({1, 2})          # users 1 and 2 are linked
RULES = {"self_trade": True, "wash_trade": True}


def order(user=1, side="BUY", type_="LIMIT", price=100, qty=1):
    return {"user_id": user, "side": side, "type": type_, "price": price, "qty": qty}


def rest(user, price, remaining=1, seq=1, in_flight=False):
    return Opposite(user, price, remaining, None if in_flight else seq, in_flight)


def test_limit_crossing_own_order_is_self_trade():
    assert decide(order(price=100), [rest(1, 99)], G, RULES) == "SELF_TRADE"


def test_limit_not_crossing_own_order_is_allowed():
    assert decide(order(price=100), [rest(1, 101)], G, RULES) is None


def test_sell_side_crossing():
    assert decide(order(side="SELL", price=100), [rest(1, 100)], G, RULES) == "SELF_TRADE"


def test_crossing_linked_account_is_wash_trade():
    assert decide(order(price=100), [rest(2, 99)], G, RULES) == "WASH_TRADE"


def test_unlinked_user_never_wash_trades():
    assert decide(order(user=7, price=100), [rest(8, 99)], frozenset({7}), RULES) is None


def test_in_flight_order_counts_as_resting():
    assert decide(order(price=100), [rest(1, 99, in_flight=True)], G, RULES) == "SELF_TRADE"


def test_market_order_stops_before_reaching_own_order():
    book = [rest(5, 100, remaining=3, seq=1), rest(1, 101, remaining=1, seq=2)]
    assert decide(order(type_="MARKET", price=None, qty=2), book, G, RULES) is None


def test_market_order_reaching_own_order():
    book = [rest(5, 100, remaining=1, seq=1), rest(1, 101, remaining=1, seq=2)]
    assert decide(order(type_="MARKET", price=None, qty=2), book, G, RULES) == "SELF_TRADE"


def test_market_order_with_own_in_flight_order_is_rejected():
    assert decide(order(type_="MARKET", price=None), [rest(1, 500, in_flight=True)], G, RULES) == "SELF_TRADE"


def test_rules_can_be_switched_off():
    assert decide(order(price=100), [rest(2, 99)], G, {"self_trade": True, "wash_trade": False}) is None
