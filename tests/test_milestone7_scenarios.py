"""Milestone 7: scenarios. The metrics must point to the injected root cause.

Each scenario is run with its interventions and as a baseline (same config,
same seed, no interventions). The diagnosis must name the expected cause, and
that cause must clearly be worse than in the baseline, so a test can't pass
just because the cause happens to be big on a normal day.
"""

import numpy as np
import pytest

from smt_sim.defects import DefectType
from smt_sim.metrics import diagnose, downtime_pareto, finished_boards, quality_report, state_intervals
from smt_sim.scenarios import ACTIONS, deep_merge, load_scenario
from smt_sim.sim import Simulator

SEEDS = [1, 2]


def run_scenario(name: str, seed: int, with_interventions: bool = True) -> Simulator:
    scenario = load_scenario(name)
    sim = scenario.build_simulator(seed=seed, with_interventions=with_interventions)
    sim.run(scenario.duration_h * 3600)
    return sim


def defect_count(sim: Simulator, station: str, defect_type: str) -> int:
    pareto = quality_report(sim).detected_pareto
    return next((n for origin, typ, n in pareto if (origin, typ) == (station, defect_type)), 0)


def stop_time_s(sim: Simulator, station: str, reason: str) -> float:
    rows = downtime_pareto(state_intervals(sim.event_log, sim.time_s))
    return sum(r.total_s for r in rows if (r.station, r.reason_code) == (station, reason))


# ---- the three scenarios -----------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
def test_stencil_wear_points_to_printer_paste(seed):
    expected = load_scenario("stencil_wear").expected_root_cause["defect"]
    sim = run_scenario("stencil_wear", seed)
    baseline = run_scenario("stencil_wear", seed, with_interventions=False)

    origin, defect_type, _ = diagnose(sim).top_defect
    assert (origin, defect_type) == (expected["station"], expected["type"])
    assert defect_count(sim, "printer", DefectType.INSUFFICIENT_PASTE) >= 2 * defect_count(
        baseline, "printer", DefectType.INSUFFICIENT_PASTE)

    # Ground truth: paste volume really drifts down after the damage at t = 2 h.
    boards = finished_boards(sim)
    early = [b.measurements["printer"]["paste_volume_mean_pct"] for b in boards if b.time_in_s["printer"] < 7200]
    late = [b.measurements["printer"]["paste_volume_mean_pct"] for b in boards if b.time_in_s["printer"] > 10 * 3600]
    assert np.mean(late) < np.mean(early) - 3


@pytest.mark.parametrize("seed", SEEDS)
def test_feeder_depletion_points_to_feeder_empty_at_bottleneck(seed):
    expected = load_scenario("feeder_depletion").expected_root_cause["bottleneck_stop"]
    sim = run_scenario("feeder_depletion", seed)
    baseline = run_scenario("feeder_depletion", seed, with_interventions=False)

    d = diagnose(sim)
    assert d.bottleneck == expected["station"]
    assert (d.top_bottleneck_stop.station, d.top_bottleneck_stop.reason_code) == (
        expected["station"], expected["reason_code"])
    assert stop_time_s(sim, "pnp", "FEEDER_EMPTY") >= 3 * stop_time_s(baseline, "pnp", "FEEDER_EMPTY")
    assert sim.boards_completed < baseline.boards_completed   # it really costs output


@pytest.mark.parametrize("seed", SEEDS)
def test_nozzle_fault_points_to_pnp_and_traces_to_nozzle_1(seed):
    expected = load_scenario("nozzle_fault").expected_root_cause["defect"]
    sim = run_scenario("nozzle_fault", seed)
    baseline = run_scenario("nozzle_fault", seed, with_interventions=False)

    origin, defect_type, _ = diagnose(sim).top_defect
    assert (origin, defect_type) == (expected["station"], expected["type"])
    assert defect_count(sim, "pnp", DefectType.MISPLACEMENT) >= 5 * max(
        1, defect_count(baseline, "pnp", DefectType.MISPLACEMENT))

    # Ground truth: most misplacements were made by a nozzle while it was clogged,
    # including the one we clogged. (The lenient vision check also lets any
    # natural clog run on; with seed 1 nozzle 3 clogs by itself.)
    details = [d.detail for b in finished_boards(sim) for d in b.true_defects if d.type == DefectType.MISPLACEMENT]
    assert sum("(clogged)" in text for text in details) > 0.7 * len(details)
    assert any("nozzle 1 (clogged)" in text for text in details)


# ---- scenario machinery ---------------------------------------------------------


def test_interventions_are_applied_once_at_their_time():
    sim = load_scenario("feeder_depletion").build_simulator(seed=1)
    sim.run(2 * 3600)
    times = [at_s for at_s, _ in sim.intervention_log]
    assert times == [0.0, 0.0, 1800.0, 5400.0]   # the one at 9000 s hasn't happened yet
    assert "set_reel_qty" in sim.intervention_log[2][1]


def test_baseline_has_no_interventions_and_same_seed_is_reproducible():
    a = run_scenario("nozzle_fault", 3)
    b = run_scenario("nozzle_fault", 3)
    assert a.event_log.events == b.event_log.events
    assert run_scenario("nozzle_fault", 3, with_interventions=False).intervention_log == []


def test_scheduled_interventions_keep_order_at_equal_times():
    sim = Simulator()
    order = []
    sim.schedule(10, "b", lambda s: order.append("b"))
    sim.schedule(5, "a", lambda s: order.append("a"))
    sim.schedule(10, "c", lambda s: order.append("c"))
    sim.run(20)
    assert order == ["a", "b", "c"]


def test_deep_merge_merges_nested_dicts_and_replaces_the_rest():
    target = {"line": {"buffer_capacity": 5, "operator": {"response_median_s": 240, "response_sigma": 0.7}},
              "production_plan": {"jobs": [1, 2, 3]}}
    deep_merge(target, {"line": {"operator": {"response_median_s": 600}}, "production_plan": {"jobs": [9]}})
    assert target["line"] == {"buffer_capacity": 5, "operator": {"response_median_s": 600, "response_sigma": 0.7}}
    assert target["production_plan"]["jobs"] == [9]


def test_unknown_action_is_rejected(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("name: bad\ninterventions:\n  - {at_s: 0, action: summon_gremlins}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown action"):
        load_scenario(path)
    assert "clog_nozzle" in ACTIONS
