"""Equipment speed: run a station below its rated speed (speed_pct)."""

import numpy as np
import pytest

from smt_sim import Simulator
from smt_sim.components import Nozzle
from smt_sim.config import load_config, make_deterministic
from smt_sim.defects import DefectType
from smt_sim.metrics import station_oee
from smt_sim.scenarios import set_speed

from helpers import default_single_product, single_product

PNP_RATED_CYCLE_S = 38.0   # product A at 100 %


def station_cfg(cfg: dict, name: str) -> dict:
    return next(st for st in cfg["line"]["stations"] if st["name"] == name)


def deterministic_with_pnp_speed(speed_pct: float) -> Simulator:
    cfg = single_product(make_deterministic(load_config()))
    station_cfg(cfg, "pnp")["speed_pct"] = speed_pct
    return Simulator(cfg)


def boards_out_between(sim: Simulator, start_s: float, end_s: float) -> int:
    sim.run(start_s - sim.time_s)
    before = sim.boards_completed
    sim.run(end_s - start_s)
    return sim.boards_completed - before


def test_half_speed_doubles_the_bottleneck_cycle():
    sim = deterministic_with_pnp_speed(50)
    out = boards_out_between(sim, 3600, 3 * 3600)
    assert abs(out - 2 * 3600 / (2 * PNP_RATED_CYCLE_S)) <= 1


def test_running_slow_shows_as_a_performance_loss_not_downtime():
    sim = deterministic_with_pnp_speed(80)
    sim.run(8 * 3600)
    m = station_oee(sim)["pnp"]
    assert m.performance == pytest.approx(0.80, abs=0.01)   # measured against RATED speed
    assert m.availability == pytest.approx(1.0)


def test_speed_must_be_between_0_and_100():
    sim = Simulator()
    for bad in (0, -10, 120):
        with pytest.raises(ValueError):
            sim.station("pnp").set_speed(bad)
    cfg = load_config()
    station_cfg(cfg, "printer")["speed_pct"] = 150
    with pytest.raises(ValueError):
        Simulator(cfg)


def test_oven_speed_is_fixed_by_its_profile():
    sim = Simulator()
    with pytest.raises(ValueError, match="fixed"):
        sim.station("reflow").set_speed(80)


def test_speed_change_applies_from_the_next_board():
    sim = deterministic_with_pnp_speed(100)
    sim.run(3600)
    set_speed(sim, "pnp", 50)          # the scenario action
    sim.run(3600)
    times = sorted((b.time_in_s["pnp"], b.time_out_s["pnp"] - b.time_in_s["pnp"])
                   for b in sim.all_boards() if "pnp" in b.time_out_s)
    before = [d for t_in, d in times if t_in < 3600 - 40]
    after = [d for t_in, d in times if t_in > 3600]
    assert set(before) == {38.0} and set(after) == {76.0}
    assert sim.get_tags()["pnp"]["speed_pct"] == 50.0


def test_slower_head_places_more_accurately_but_not_when_clogged():
    cfg = {"clog_shape": 3.0, "clog_scale_picks": 25000, "base_offset_sd_um": 30, "wear_offset_sd_um": 0,
           "clog_offset_sd_um": 60, "base_pick_fail_prob": 0.05, "clog_pick_fail_prob": 0.30}
    nz = Nozzle(0, cfg, np.random.default_rng(1))
    nz.eta = 1e12   # no clogging during the test
    fast = [nz.pick(1.0, 1.0) for _ in range(20_000)]
    slow = [nz.pick(0.5, 0.5) for _ in range(20_000)]
    assert np.std([o for o, _ in slow]) == pytest.approx(0.5 * np.std([o for o, _ in fast]), rel=0.05)
    assert np.mean([f for _, f in slow]) == pytest.approx(0.5 * 0.05, abs=0.005)

    nz.clogged = True
    clogged_slow = [nz.pick(0.5, 0.5) for _ in range(20_000)]
    assert np.mean([f for _, f in clogged_slow]) == pytest.approx(0.30, abs=0.015)   # speed doesn't unclog


def test_on_an_inaccurate_machine_slowing_down_cuts_misplacements():
    results = {}
    for speed in (100, 80):
        cfg = default_single_product()
        pnp = station_cfg(cfg, "pnp")
        pnp["speed_pct"] = speed
        pnp["nozzles"]["base_offset_sd_um"] = 45
        sim = Simulator(cfg, seed=3)
        sim.run(8 * 3600)
        misplaced = sum(1 for b in sim.all_boards() for d in b.true_defects if d.type == DefectType.MISPLACEMENT)
        results[speed] = (misplaced, sim.boards_completed)
    assert results[80][0] < results[100][0] / 3      # far fewer misplacements
    assert results[80][1] > results[100][1]           # and more good boards out
