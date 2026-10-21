"""Milestone 1: deterministic line — flow, bottleneck rate, update order, propagation."""

import pytest

from smt_sim import Simulator
from smt_sim.buffer import Buffer
from smt_sim.config import load_config, make_deterministic
from smt_sim.events import ReasonCode, StationState

from helpers import deterministic_sim, single_product

PNP_CYCLE_S = 8.0 + 0.25 * 120   # from config/default.yaml: 38 s, the bottleneck


def boards_out_between(sim: Simulator, start_s: float, end_s: float) -> int:
    """Run to start_s, then count boards leaving the line until end_s."""
    sim.run(start_s - sim.time_s)
    before = sim.boards_completed
    sim.run(end_s - start_s)
    return sim.boards_completed - before


def test_buffer_respects_capacity():
    buf = Buffer("b", capacity=2)
    buf.push("a")
    buf.push("b")
    assert not buf.has_space()
    with pytest.raises(RuntimeError):
        buf.push("c")
    assert buf.pop() == "a"   # FIFO
    assert buf.has_space()


def test_steady_state_throughput_equals_bottleneck_rate():
    sim = deterministic_sim()
    window_s = 2 * 3600
    # Skip the first hour so the line is full (warm-up), then measure 2 hours.
    out = boards_out_between(sim, 3600, 3600 + window_s)
    expected = window_s / PNP_CYCLE_S   # 189.5
    assert abs(out - expected) <= 1


def test_bottleneck_never_starved_or_blocked_in_steady_state():
    sim = deterministic_sim()
    sim.run(3600)
    n_events_before = len(sim.event_log.for_station("pnp"))
    sim.run(3600)
    # Once warmed up, the bottleneck should simply run nonstop.
    assert sim.station("pnp").state == StationState.RUNNING
    assert len(sim.event_log.for_station("pnp")) == n_events_before


def test_downstream_first_update_order_keeps_balanced_line_at_full_rate():
    """Every station at 10 s, buffers of size 1: no capacity should be lost.

    This only works if a slot freed downstream is reusable upstream in the
    same tick, which is what the downstream-to-upstream order guarantees.
    """
    cfg = single_product(make_deterministic(load_config()))
    cfg["line"]["buffer_capacity"] = 1
    for st in cfg["line"]["stations"]:
        if st["type"] == "pnp":
            st["board_handling_s"], st["time_per_component_s"] = 10.0, 0.0
        elif st["type"] == "reflow":
            st["transit_time_s"], st["conveyor_length_mm"], st["board_pitch_mm"] = 100.0, 1000.0, 100.0
        else:
            st["cycle_time_s"] = 10.0
    sim = Simulator(cfg)
    out = boards_out_between(sim, 1800, 1800 + 3600)
    assert abs(out - 360) <= 1


def test_reflow_is_a_pipeline_with_fixed_transit_time():
    sim = deterministic_sim()
    sim.run(3600)
    oven = sim.station("reflow")
    assert oven.capacity == 16
    assert len(oven.conveyor) > 1   # several boards inside at once
    finished = [b for b in sim.sink.boards() if "reflow" in b.time_out_s]
    transits = {b.time_out_s["reflow"] - b.time_in_s["reflow"] for b in finished}
    assert transits == {300.0}


def test_stoppage_propagates_starved_downstream_and_blocked_upstream():
    sim = deterministic_sim()
    sim.run(3600)   # reach steady state
    sim.station("pnp").force_down(ReasonCode.MANUAL_STOP, duration_s=900)
    sim.run(880)    # long enough for the oven (300 s transit) to empty

    states = {st.name: st.state for st in sim.stations}
    assert states["pnp"] == StationState.DOWN
    assert sim.station("pnp").reason_code == ReasonCode.MANUAL_STOP
    assert states["printer"] == StationState.BLOCKED
    assert states["spi"] == StationState.BLOCKED
    assert states["reflow"] == StationState.STARVED
    assert states["aoi"] == StationState.STARVED
    assert states["ict"] == StationState.STARVED

    # Upstream buffers full, downstream buffers empty.
    levels = {b.name: len(b) for b in sim.buffers}
    assert levels["printer->spi"] == 5 and levels["spi->pnp"] == 5
    assert levels["pnp->reflow"] == 0 and levels["reflow->aoi"] == 0 and levels["aoi->ict"] == 0

    # After the repair the line recovers.
    sim.run(600)
    assert sim.station("pnp").state == StationState.RUNNING
    assert sim.station("ict").state in (StationState.RUNNING, StationState.STARVED)


def test_event_log_records_the_down_event_with_reason():
    sim = deterministic_sim()
    sim.run(600)
    sim.station("pnp").force_down(ReasonCode.MANUAL_STOP, duration_s=60)
    sim.run(120)
    downs = [e for e in sim.event_log.for_station("pnp") if e.new_state == StationState.DOWN]
    assert len(downs) == 1
    assert downs[0].sim_time_s == 600.0
    assert downs[0].reason_code == ReasonCode.MANUAL_STOP
    # Every station logs its initial state at t=0.
    initial = [e for e in sim.event_log.events if e.old_state is None]
    assert len(initial) == 6


def test_get_tags_is_flat_and_primitive():
    sim = deterministic_sim()
    sim.run(600)
    tags = sim.get_tags()
    assert tags["pnp"]["state"] in {s.value for s in StationState}
    for entity in tags.values():
        for value in entity.values():
            assert isinstance(value, (str, int, float))
