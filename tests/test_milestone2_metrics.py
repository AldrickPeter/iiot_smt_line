"""Milestone 2: event log + metrics, checked against hand-computed small cases.

Tick = 0.5 s in all cases. Reminder of the tick rules (see station.py):
a board pulled on the tick at time t with cycle C finishes its work at the end
of tick t + C - 0.5, and is pushed on the tick at time t + C (if there is room).
"""

import csv
import math

import pytest

from smt_sim import Simulator
from smt_sim.events import ReasonCode, StationState
from smt_sim.metrics import (
    downtime_pareto,
    find_bottleneck,
    format_report,
    line_oee,
    state_intervals,
    station_oee,
    time_in_state,
)

from helpers import deterministic_sim, tiny_config


def test_single_station_with_one_stop():
    """One 10 s station, stopped for 20 s at t=30, observed for 100 s.

    Hand calculation:
      boards pushed at t = 10, 20            (normal)
      board 3 finishes work at t=30, but the station goes DOWN 30..50
      board 3 pushed at t = 50, then t = 60, 70, 80, 90  -> 7 boards out
      board 8 is fully worked by t=100 but not yet pushed -> not counted
      running = 100 - 20 = 80 s, down = 20 s
      availability = 80 / 100 = 0.80
      performance  = 10 s * 7 / 80 s = 0.875
    """
    sim = Simulator(tiny_config({"printer": 10.0}))
    sim.run(30)
    sim.station("printer").force_down(ReasonCode.MANUAL_STOP, duration_s=20)
    sim.run(70)

    tis = time_in_state(state_intervals(sim.event_log, sim.time_s))["printer"]
    assert tis[StationState.RUNNING] == 80.0
    assert tis[StationState.DOWN] == 20.0
    assert sum(tis.values()) == 100.0

    m = station_oee(sim)["printer"]
    assert m.boards_processed == 7
    assert m.availability == pytest.approx(0.80)
    assert m.performance == pytest.approx(0.875)
    assert m.oee == pytest.approx(0.70)

    pareto = downtime_pareto(state_intervals(sim.event_log, sim.time_s))
    assert len(pareto) == 1
    assert (pareto[0].station, pareto[0].reason_code, pareto[0].total_s, pareto[0].occurrences) == (
        "printer", ReasonCode.MANUAL_STOP, 20.0, 1)


def test_two_stations_starved_and_blocked():
    """A (10 s) -> buffer of 1 -> B (20 s), observed for 210.5 s.

    Hand calculation (B is updated before A each tick):
      t=10   A pushes board 1. B already updated this tick, so B sees it at 10.5.
      B: STARVED 0..10.5, then RUNNING to the end           -> starved 10.5, running 200
         pushes at 30.5, 50.5, ..., 190.5                    -> 9 boards
      A: RUNNING 0..30 (boards 1-3), buffer full at 30 -> BLOCKED 30..30.5
         then repeats: RUNNING 10 s, BLOCKED 10 s from 30.5 to 210.5 (9 rounds)
                                                            -> running 120, blocked 90.5
      availability: A = 120 / (210.5 - 90.5) = 1.0 ; B = 200 / (210.5 - 10.5) = 1.0
      B performance = 20 * 9 / 200 = 0.9
      line OEE = 9 boards * 20 s / 210.5 s
    """
    sim = Simulator(tiny_config({"A": 10.0, "B": 20.0}))
    sim.run(210.5)

    tis = time_in_state(state_intervals(sim.event_log, sim.time_s))
    assert tis["A"][StationState.RUNNING] == 120.0
    assert tis["A"][StationState.BLOCKED] == 90.5
    assert tis["A"][StationState.STARVED] == 0.0
    assert tis["B"][StationState.STARVED] == 10.5
    assert tis["B"][StationState.RUNNING] == 200.0

    m = station_oee(sim)
    assert m["A"].availability == pytest.approx(1.0)
    assert m["B"].availability == pytest.approx(1.0)
    assert m["B"].boards_processed == 9
    assert m["B"].performance == pytest.approx(0.9)

    line = line_oee(sim)
    assert line.bottleneck_ideal_cycle_s == {"TEST": 20.0}
    assert line.oee == pytest.approx(9 * 20 / 210.5)

    bn = find_bottleneck(sim)
    assert bn.station == "B"
    assert bn.confirmed
    # A's active periods are 30, then 10 s each; B has one 200 s period.
    assert bn.mean_active_period_s["B"] == 200.0
    assert bn.mean_active_period_s["A"] == pytest.approx((30 + 9 * 10) / 10)


def test_time_in_state_covers_every_second():
    sim = deterministic_sim()
    sim.run(3600)
    sim.station("pnp").force_down(ReasonCode.NOZZLE_CLOG, duration_s=300)
    sim.run(3600)
    for station, totals in time_in_state(state_intervals(sim.event_log, sim.time_s)).items():
        assert sum(totals.values()) == pytest.approx(sim.time_s), station


def test_default_line_bottleneck_is_pnp():
    sim = deterministic_sim()
    sim.run(8 * 3600)
    bn = find_bottleneck(sim)
    assert bn.station == "pnp"
    assert bn.confirmed


def test_pareto_ranks_largest_stop_first():
    sim = deterministic_sim()
    sim.run(1000)
    sim.station("printer").force_down(ReasonCode.PASTE_LOW, duration_s=60)
    sim.station("pnp").force_down(ReasonCode.NOZZLE_CLOG, duration_s=200)
    sim.run(1000)
    sim.station("pnp").force_down(ReasonCode.NOZZLE_CLOG, duration_s=100)
    sim.run(1000)

    pareto = downtime_pareto(state_intervals(sim.event_log, sim.time_s))
    assert [(r.station, r.reason_code, r.total_s, r.occurrences) for r in pareto] == [
        ("pnp", ReasonCode.NOZZLE_CLOG, 300.0, 2),
        ("printer", ReasonCode.PASTE_LOW, 60.0, 1),
    ]


def test_never_running_station_gives_nan_not_crash():
    sim = Simulator(tiny_config({"A": 10.0}))
    sim.station("A").force_down(ReasonCode.MANUAL_STOP, duration_s=100)
    sim.run(50)
    m = station_oee(sim)["A"]
    assert m.availability == 0.0
    assert math.isnan(m.performance)


def test_event_log_csv_export(tmp_path):
    sim = deterministic_sim()
    sim.run(600)
    path = tmp_path / "events.csv"
    sim.event_log.to_csv(path)
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == len(sim.event_log)
    assert rows[0]["old_state"] == ""            # initial state has no previous state
    assert rows[0]["new_state"] == "STARVED"


def test_report_renders():
    sim = deterministic_sim()
    sim.run(3600)
    text = format_report(sim)
    assert "Line OEE" in text and "Bottleneck: pnp" in text
