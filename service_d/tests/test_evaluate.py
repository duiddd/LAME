"""Acceptance criterion 2: precision, recall and false positive rate from labels."""
from d.evaluate import evaluate
from tests.fakes import wait_until
from tests.test_flow import BUY, SELL, ready_engine, req


async def test_metrics_from_labels(make_engine, fake_a, tmp_path):
    e = await ready_engine(make_engine)
    e.submit(req("S1", 1024, SELL, price=100))
    await wait_until(lambda: fake_a.of("S1", "ACCEPTED"))
    e.submit(req("B1", 1024, BUY, price=100))   # SELF_TRADE, labelled SELF_TRADE  -> caught
    e.submit(req("B2", 1025, BUY, price=100))   # WASH_TRADE, labelled WASH_TRADE  -> caught
    e.submit(req("B3", 1025, BUY, price=101))   # WASH_TRADE, but labelled LEGIT   -> false positive
    e.submit(req("B4", 3001, BUY, price=99))    # approved,   labelled WASH_TRADE  -> missed
    await wait_until(lambda: len(e.store.all("SELECT * FROM decisions")) == 5)
    labels = [
        {"order_id": "S1", "user_id": 1024, "label": "LEGIT", "scenario_id": "s"},
        {"order_id": "B1", "user_id": 1024, "label": "SELF_TRADE", "scenario_id": "s"},
        {"order_id": "B2", "user_id": 1025, "label": "WASH_TRADE", "scenario_id": "w"},
        {"order_id": "B3", "user_id": 1025, "label": "LEGIT", "scenario_id": "w"},
        {"order_id": "B4", "user_id": 3001, "label": "WASH_TRADE", "scenario_id": "w"},
        {"order_id": "ZZ", "user_id": 9, "label": "LEGIT", "scenario_id": "w"},   # never reached D
    ]
    r = evaluate(str(tmp_path / "d.db"), labels)
    assert r["per_rule"]["SELF_TRADE"] == {"precision": 1.0, "recall": 1.0, "true_positive": 1,
                                           "rejected_with_reason": 1, "labelled": 1}
    assert r["per_rule"]["WASH_TRADE"]["precision"] == 0.5     # B2 right, B3 wrong
    assert r["per_rule"]["WASH_TRADE"]["recall"] == 0.5        # B2 caught, B4 missed
    assert r["overall"]["false_positive_rate"] == 0.5          # B3 of {S1, B3}
    assert r["not_decided"] == 1
    assert r["decision_latency_ms"]["count"] == 5
