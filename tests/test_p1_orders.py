"""P1: customer orders in the simulator — order ids, reject replacement, top-ups, completion."""

import pytest

from smt_sim import Simulator
from smt_sim.config import load_config, make_deterministic
from smt_sim.factory import Factory
from smt_sim.orders import load_orders, orders_from_config

A, C = "CTRL-BOARD-A", "POWER-BOARD-C"


def config_with_orders(base: dict, orders: list[dict], jobs: list[dict]) -> dict:
    base["orders"] = orders
    base["production_plan"] = {"jobs": jobs}
    return base


def two_orders(base: dict) -> dict:
    return config_with_orders(
        base,
        [{"id": "SO-1", "product": A, "quantity": 60, "due_h": 3},
         {"id": "SO-2", "product": C, "quantity": 40, "due_h": 5}],
        [{"product": A, "quantity": 60, "order": "SO-1"},
         {"product": C, "quantity": 40, "order": "SO-2"}],
    )


def test_every_order_gets_exactly_its_good_boards():
    sim = Simulator(two_orders(load_config()), seed=2)
    assert sim.run_until_orders_done(24 * 3600)
    t = sim.order_tracker
    for order_id, qty in (("SO-1", 60), ("SO-2", 40)):
        assert t.good[order_id] >= qty
        assert t.released[order_id] == t.good[order_id] + t.rejected[order_id] + sum(
            1 for b in sim.all_boards() if b.order_id == order_id and b.disposition is None)
    assert all(b.order_id in ("SO-1", "SO-2") for b in sim.all_boards())


def test_completion_time_is_when_the_last_needed_good_board_left():
    sim = Simulator(two_orders(load_config()), seed=2)
    sim.run_until_orders_done(24 * 3600)
    for order in sim.orders:
        good = sorted(max(b.time_out_s.values()) for b in sim.sink.boards() if b.order_id == order.id)
        assert sim.order_tracker.completed_s[order.id] == good[order.quantity - 1]


def station_cfg(cfg: dict, name: str) -> dict:
    return next(st for st in cfg["line"]["stations"] if st["name"] == name)


def test_rejects_are_replaced_while_the_lot_runs():
    cfg = two_orders(make_deterministic(load_config()))
    aoi = station_cfg(cfg, "aoi")
    aoi["false_calls_per_board"] = 0.15        # rejects found early, while the lot is still releasing
    aoi["false_call_types"] = ["MISPLACEMENT"]
    sim = Simulator(cfg, seed=3)
    assert sim.run_until_orders_done(24 * 3600)
    t = sim.order_tracker
    assert t.rejected["SO-1"] > 0 and t.released["SO-1"] > 60
    assert t.good["SO-1"] >= 60 and t.good["SO-2"] >= 40


def test_late_rejects_lead_to_a_top_up_lot_at_the_end():
    cfg = two_orders(make_deterministic(load_config()))
    ict = station_cfg(cfg, "ict")
    ict["false_calls_per_board"] = 0.3         # rejects found at the END of the line
    ict["false_call_types"] = ["COLD_JOINT"]
    sim = Simulator(cfg, seed=4)
    assert sim.run_until_orders_done(48 * 3600)
    top_ups = [j for j in sim.source.jobs if j.get("top_up")]
    assert sim.source.top_up_count == len(top_ups) > 0
    assert {j["order"] for j in top_ups} <= {"SO-1", "SO-2"}
    assert sim.order_tracker.all_done()


def test_line_drains_after_the_orders():
    cfg = two_orders(make_deterministic(load_config()))
    sim = Simulator(cfg, seed=1)
    sim.run_until_orders_done(24 * 3600)
    assert sim.source.is_empty()
    assert sim.order_tracker.released["SO-1"] == 60 and sim.order_tracker.released["SO-2"] == 40   # no rejects


def test_order_validation():
    with pytest.raises(ValueError, match="Duplicate"):
        orders_from_config([{"id": "X", "product": A, "quantity": 1, "due_h": 1}] * 2)
    with pytest.raises(ValueError, match="unknown order"):
        Simulator(config_with_orders(load_config(), [{"id": "SO-1", "product": A, "quantity": 5, "due_h": 1}],
                                     [{"product": A, "quantity": 5, "order": "NOPE"}]))
    with pytest.raises(ValueError, match="wrong product"):
        Simulator(config_with_orders(load_config(), [{"id": "SO-1", "product": A, "quantity": 5, "due_h": 1}],
                                     [{"product": C, "quantity": 5, "order": "SO-1"}]))


def test_order_file_loads():
    orders = load_orders("config/orders/demo_order.yaml")
    assert len(orders) >= 5 and all(o.due_s > 0 and o.quantity > 0 for o in orders)


def test_factory_runs_until_every_line_is_done():
    factory = Factory.from_config(seed=1)
    for name, product, oid in (("line1", A, "SO-1"), ("line2", C, "SO-2")):
        sim = factory.lines[name]
        cfg = config_with_orders(sim.config, [{"id": oid, "product": product, "quantity": 30, "due_h": 4}],
                                 [{"product": product, "quantity": 30, "order": oid}])
        factory.lines[name] = Simulator(cfg, seed=sim.seed)
    assert factory.run_until_orders_done(24 * 3600)
    assert factory.lines["line1"].order_tracker.good["SO-1"] >= 30
    assert factory.lines["line2"].order_tracker.good["SO-2"] >= 30
