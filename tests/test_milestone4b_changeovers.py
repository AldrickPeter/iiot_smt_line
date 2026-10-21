"""Milestone 4b: several products, a production plan, and per-station changeovers."""

import pytest

from smt_sim import Simulator
from smt_sim.board import Board
from smt_sim.config import load_config, make_deterministic
from smt_sim.events import ReasonCode, StationState
from smt_sim.metrics import downtime_pareto, state_intervals

A, B, C = "CTRL-BOARD-A", "SENSOR-BOARD-B", "POWER-BOARD-C"
LINE_ORDER = ["printer", "spi", "pnp", "reflow", "aoi", "ict"]


def plan(*jobs: tuple[str, int | None], repeat: bool = False) -> dict:
    return {"repeat": repeat, "jobs": [{"product": p, "quantity": q} for p, q in jobs]}


def deterministic_with_plan(*jobs, **kw) -> Simulator:
    cfg = make_deterministic(load_config())
    cfg["production_plan"] = plan(*jobs, **kw)
    return Simulator(cfg)


def setup_events(sim: Simulator) -> list:
    return [e for e in sim.event_log.events if e.new_state == StationState.SETUP]


def test_changeover_ripples_down_the_line_in_order():
    sim = deterministic_with_plan((A, 20), (B, None))
    sim.run(3 * 3600)
    setups = setup_events(sim)
    assert [e.station for e in setups] == LINE_ORDER   # one each, upstream first
    times = [e.sim_time_s for e in setups]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert [e.reason_code for e in setups] == [
        ReasonCode.STENCIL_CHANGE, ReasonCode.PROGRAM_LOAD, ReasonCode.FEEDER_CHANGE,
        ReasonCode.PROFILE_CHANGE, ReasonCode.PROGRAM_LOAD, ReasonCode.FIXTURE_CHANGE,
    ]


def test_no_changeover_for_the_first_product():
    sim = deterministic_with_plan((A, None))
    sim.run(2 * 3600)
    assert setup_events(sim) == []


def test_reflow_lets_old_product_out_before_changing_profile():
    sim = deterministic_with_plan((A, 20), (B, None))
    oven = sim.station("reflow")
    while oven.state != StationState.SETUP:
        sim.step()
    assert len(oven.conveyor) == 0
    sim.run(2 * 3600)
    a_exits = [b.time_out_s["reflow"] for b in sim.all_boards() if b.product == A]
    b_entries = [b.time_in_s["reflow"] for b in sim.all_boards() if b.product == B and "reflow" in b.time_in_s]
    assert max(a_exits) < min(b_entries)


def pnp_setup_duration_s(sim: Simulator) -> float:
    intervals = state_intervals(sim.event_log, sim.time_s)
    return sum(iv.duration_s for iv in intervals if iv.station == "pnp" and iv.state == StationState.SETUP)


def test_pnp_changeover_faster_when_products_share_parts():
    """A->B mounts 3 new feeders, A->C mounts 6: 300 s + 90 s per feeder."""
    durations = {}
    for target in (B, C):
        cfg = load_config()
        cfg["production_plan"] = plan((A, 20), (target, None))
        sim = Simulator(cfg, seed=1)
        sim.run(3 * 3600)
        durations[target] = pnp_setup_duration_s(sim)
    assert durations[B] == pytest.approx(300 + 90 * 3)
    assert durations[C] == pytest.approx(300 + 90 * 6)


def test_parked_feeder_keeps_its_reel():
    sim = Simulator()
    pnp = sim.station("pnp")
    board = {p: Board("REF", p, sim.products[p]) for p in (A, C)}
    pnp._setup_for(board[A])
    pnp.feeders["R0402-10K"].qty = 1234
    pnp._setup_for(board[C])                      # C does not use R0402-10K
    assert pnp.feeders["R0402-10K"].slot is None  # parked
    assert "R0402-10K" not in pnp.slots
    pnp._setup_for(board[A])                      # back to A
    assert pnp.feeders["R0402-10K"].slot is not None
    assert pnp.feeders["R0402-10K"].qty == 1234
    assert set(board[A].bom) <= set(pnp.slots)


def test_too_many_parts_for_the_slots_is_an_error():
    cfg = load_config()
    next(st for st in cfg["line"]["stations"] if st["name"] == "pnp")["feeders"]["slot_count"] = 6
    sim = Simulator(cfg)
    with pytest.raises(ValueError, match="slots"):
        sim.run(60)


def test_finite_plan_is_made_and_the_line_drains():
    sim = deterministic_with_plan((A, 10), (C, 10))
    sim.run(4 * 3600)
    assert sim.boards_completed == 20
    assert [b.product for b in sim.sink.boards()] == [A] * 10 + [C] * 10
    assert all(st.state == StationState.STARVED for st in sim.stations)


def test_plan_rejects_unknown_product():
    cfg = load_config()
    cfg["production_plan"] = plan(("NO-SUCH-BOARD", 5))
    with pytest.raises(ValueError, match="unknown product"):
        Simulator(cfg)


def test_repeating_plan_cycles_through_jobs():
    sim = deterministic_with_plan((A, 3), (B, 2), repeat=True)
    products = [sim.source.pop().product for _ in range(10)]
    assert products == [A, A, A, B, B, A, A, A, B, B]


def test_changeovers_show_as_setup_in_the_pareto():
    sim = Simulator(seed=3)
    sim.run(8 * 3600)
    rows = downtime_pareto(state_intervals(sim.event_log, sim.time_s))
    setup_reasons = {r.reason_code for r in rows if r.state == StationState.SETUP}
    assert {ReasonCode.STENCIL_CHANGE, ReasonCode.FEEDER_CHANGE, ReasonCode.FIXTURE_CHANGE} <= setup_reasons


def test_ideal_cycle_depends_on_product():
    sim = Simulator()
    assert sim.bottleneck_ideal_cycle_s(A) == 38.0
    assert sim.bottleneck_ideal_cycle_s(B) == 34.0
    assert sim.bottleneck_ideal_cycle_s(C) == 32.0
