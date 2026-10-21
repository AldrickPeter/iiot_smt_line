"""Milestone 3: stochastic faults and repairs, cycle-time jitter, determinism."""

import math

import numpy as np
import pytest

from smt_sim import Simulator
from smt_sim.config import load_config, make_deterministic
from smt_sim.events import ReasonCode, StationState
from smt_sim.failures import FailureMode
from smt_sim.metrics import STOP_STATES, downtime_pareto, find_bottleneck, state_intervals, station_oee, time_in_state

from helpers import single_product

N_SAMPLES = 20_000


def mode(cfg_overrides: dict, seed: int = 0) -> FailureMode:
    cfg = {"reason": ReasonCode.CONVEYOR_JAM, "repair_median_s": 100.0, "repair_sigma": 0.5}
    cfg.update(cfg_overrides)
    return FailureMode(cfg, np.random.default_rng(seed))


def sample_failure_ages(fm: FailureMode, n: int) -> list[float]:
    """Running age at each failure: jump straight to each sampled failure age."""
    ages = []
    for _ in range(n):
        ttf = fm.time_to_failure_s
        assert fm.advance(ttf)   # jumping exactly to the failure age triggers it
        ages.append(ttf)
    return ages


# ---- the distributions themselves ---------------------------------------


def test_exponential_mean_time_to_failure():
    fm = mode({"distribution": "exponential", "mtbf_s": 3600.0})
    ages = sample_failure_ages(fm, N_SAMPLES)
    assert np.mean(ages) == pytest.approx(3600.0, rel=0.03)
    assert fm.hazard(0) == fm.hazard(10_000) == pytest.approx(1 / 3600)   # constant


def test_weibull_mean_and_rising_hazard():
    shape, scale_s = 2.5, 36000.0
    fm = mode({"distribution": "weibull", "shape": shape, "scale_s": scale_s})
    ages = sample_failure_ages(fm, N_SAMPLES)
    expected_mean = scale_s * math.gamma(1 + 1 / shape)
    assert np.mean(ages) == pytest.approx(expected_mean, rel=0.03)
    assert fm.hazard(0) == 0.0
    assert fm.hazard(10_000) < fm.hazard(20_000) < fm.hazard(40_000)   # wear-out


def test_lognormal_repair_median_and_skew():
    fm = mode({"distribution": "exponential", "mtbf_s": 1.0, "repair_median_s": 300.0, "repair_sigma": 0.6})
    repairs = np.array([fm.sample_repair_s() for _ in range(N_SAMPLES)])
    assert np.median(repairs) == pytest.approx(300.0, rel=0.03)
    assert repairs.min() > 0
    assert repairs.mean() > np.median(repairs)   # long right tail


def test_failure_mode_resets_age_after_failure():
    fm = mode({"distribution": "weibull", "shape": 3.0, "scale_s": 100.0})
    fm.advance(fm.time_to_failure_s)
    assert fm.age_s == 0.0 and fm.failure_count == 1


def test_bad_config_is_rejected():
    with pytest.raises(ValueError):
        mode({"distribution": "gaussian"})
    with pytest.raises(ValueError):
        mode({"distribution": "exponential", "mtbf_s": 1.0, "reason": "TYPO_JAM"})


# ---- on the line ---------------------------------------------------------


def test_same_seed_gives_identical_run():
    a, b = Simulator(seed=7), Simulator(seed=7)
    a.run(24 * 3600)
    b.run(24 * 3600)
    assert a.event_log.events == b.event_log.events
    assert a.boards_completed == b.boards_completed


def test_different_seed_gives_different_run():
    a, b = Simulator(seed=7), Simulator(seed=8)
    a.run(8 * 3600)
    b.run(8 * 3600)
    assert a.event_log.events != b.event_log.events


def test_faults_appear_with_their_reason_codes():
    sim = Simulator(seed=3)
    sim.run(3 * 24 * 3600)
    pareto = downtime_pareto(state_intervals(sim.event_log, sim.time_s))
    seen = {(r.station, r.reason_code) for r in pareto}
    assert ("pnp", ReasonCode.CONVEYOR_JAM) in seen
    assert ("pnp", ReasonCode.HEAD_SERVO_FAULT) in seen
    for row in pareto:
        assert row.state in STOP_STATES and row.total_s > 0


def test_observed_mtbf_matches_config():
    """Give pnp one frequent jam mode; running time / jams ≈ configured MTBF."""
    cfg = single_product(make_deterministic(load_config()))
    pnp_cfg = next(st for st in cfg["line"]["stations"] if st["name"] == "pnp")
    pnp_cfg["failures"] = [{"reason": "CONVEYOR_JAM", "distribution": "exponential",
                            "mtbf_s": 600.0, "repair_median_s": 60.0, "repair_sigma": 0.5}]
    sim = Simulator(cfg, seed=11)
    sim.run(24 * 3600)
    intervals = state_intervals(sim.event_log, sim.time_s)
    running_s = time_in_state(intervals)["pnp"][StationState.RUNNING]
    jams = sum(1 for e in sim.event_log.for_station("pnp")
               if e.new_state == StationState.DOWN and e.reason_code == ReasonCode.CONVEYOR_JAM)
    # ~130 jams expected, so the Poisson spread is about ±9 %; allow 25 %.
    assert running_s / jams == pytest.approx(600.0, rel=0.25)


def test_a_station_that_never_runs_never_wears():
    cfg = {
        "sim": {"tick_s": 0.5, "seed": 1},
        "products": {"T": {"bom": {}}},
        "line": {"buffer_capacity": 1, "stations": [{
            "name": "A", "type": "generic", "cycle_time_s": 10.0,
            "failures": [{"reason": "CONVEYOR_JAM", "distribution": "exponential",
                          "mtbf_s": 1.0, "repair_median_s": 1.0, "repair_sigma": 0.1}],
        }]},
    }
    sim = Simulator(cfg)
    sim.station("A").force_down(ReasonCode.MANUAL_STOP, duration_s=1000)
    sim.run(900)
    assert sim.station("A").failure_modes[0].failure_count == 0


def test_jitter_never_beats_ideal_cycle():
    sim = Simulator(seed=5)
    sim.run(24 * 3600)
    for name, m in station_oee(sim).items():
        assert m.performance <= 1.0 + 1e-9, name


def test_stochastic_line_still_finds_pnp_as_bottleneck():
    sim = Simulator(seed=42)
    sim.run(24 * 3600)
    bn = find_bottleneck(sim)
    assert bn.station == "pnp"
    assert bn.confirmed


# ---- reflow oven metrics (judged by utilization and its entrance) --------


def test_oven_performance_is_one_and_utilization_shows_fill():
    sim = Simulator(single_product(make_deterministic(load_config())))
    sim.run(8 * 3600)
    m = station_oee(sim)["reflow"]
    assert m.performance == 1.0
    # Gets a board every 38 s but could take one every 18.75 s.
    assert m.utilization == pytest.approx(18.75 / 38.0, abs=0.02)


def test_oven_active_periods_follow_its_entrance():
    sim = Simulator(single_product(make_deterministic(load_config())))
    sim.run(8 * 3600)
    bn = find_bottleneck(sim)
    # One entry window per board, not one 8-hour "always running" period.
    assert bn.mean_active_period_s["reflow"] == pytest.approx(18.75, abs=0.5)
    assert bn.mean_active_period_s["pnp"] > 100 * bn.mean_active_period_s["reflow"]
