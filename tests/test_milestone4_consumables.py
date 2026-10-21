"""Milestone 4: consumables — paste, stencil wipe/wear, feeders/reels/splicing, nozzles."""

import numpy as np
import pytest

from smt_sim import Simulator
from smt_sim.components import Nozzle, SolderPaste, Stencil
from smt_sim.config import load_config
from smt_sim.events import ReasonCode, StationState

from helpers import default_single_product


def stops(sim: Simulator, station: str, reason: str) -> int:
    """How many times a station entered a stop with this reason."""
    return sum(1 for e in sim.event_log.for_station(station) if e.reason_code == reason)


def station_cfg(cfg: dict, name: str) -> dict:
    return next(st for st in cfg["line"]["stations"] if st["name"] == name)


def printer_measurements(sim: Simulator) -> list[dict]:
    return [b.measurements["printer"] for b in sim.sink.boards()]


# ---- components on their own --------------------------------------------


def test_stencil_loss_grows_with_prints_since_wipe_and_resets():
    st = Stencil({"wipe_every_prints": 5, "wipe_time_s": 20, "clog_loss_pct_per_print": 1.0,
                  "wear_loss_pct_per_1k_cycles": 2.0, "initial_cycles": 1000})
    assert st.transfer_loss_pct() == pytest.approx(2.0)          # wear only
    for _ in range(5):
        st.record_print()
    assert st.wipe_due()
    assert st.transfer_loss_pct() == pytest.approx(5 * 1.0 + 2.0 * 1005 / 1000)
    st.wipe()
    assert st.transfer_loss_pct() == pytest.approx(2.0 * 1005 / 1000)   # clog gone, wear stays


def test_paste_dries_only_after_grace_period():
    paste = SolderPaste({"initial_g": 100, "per_print_mean_g": 1, "per_print_sd_g": 0, "low_limit_g": 10,
                         "refill_g": 50, "refill_time_s": 60, "open_time_grace_h": 4,
                         "open_time_loss_pct_per_h": 3.0}, np.random.default_rng(0))
    assert paste.drying_loss_pct(3 * 3600) == 0.0
    assert paste.drying_loss_pct(6 * 3600) == pytest.approx(6.0)
    paste.refill(added_at_s=6 * 3600)
    assert paste.drying_loss_pct(7 * 3600) == 0.0


NOZZLE_CFG = {"clog_shape": 3.0, "clog_scale_picks": 25000, "base_offset_sd_um": 15,
              "wear_offset_sd_um": 25, "clog_offset_sd_um": 60, "base_pick_fail_prob": 0.0005,
              "clog_pick_fail_prob": 0.30}


def test_nozzle_hazard_rises_and_clog_widens_offsets():
    nz = Nozzle(0, NOZZLE_CFG, np.random.default_rng(1))
    assert nz.hazard() == 0.0
    nz.picks_since_clean = 10_000
    h1 = nz.hazard()
    nz.picks_since_clean = 20_000
    assert nz.hazard() > h1

    nz.clean()
    healthy = [nz.pick() for _ in range(2000)]
    nz.clean()
    nz.clogged = True
    clogged = [nz.pick() for _ in range(2000)]
    assert np.std([o for o, _ in clogged]) > 3 * np.std([o for o, _ in healthy])
    assert np.mean([f for _, f in clogged]) == pytest.approx(0.30, abs=0.04)
    assert np.mean([f for _, f in healthy]) < 0.01


# ---- printer on the line --------------------------------------------------


def test_paste_volume_falls_with_prints_since_wipe():
    sim = Simulator(default_single_product(), seed=2)
    sim.run(8 * 3600)
    m = printer_measurements(sim)
    x = np.array([r["prints_since_wipe"] for r in m])
    y = np.array([r["paste_volume_mean_pct"] for r in m])
    slope = np.polyfit(x, y, 1)[0]
    assert slope == pytest.approx(-0.8, abs=0.15)   # config: 0.8 % per print


def test_wipe_happens_every_n_prints():
    sim = Simulator(default_single_product(), seed=2)
    sim.run(8 * 3600)
    printer = sim.station("printer")
    prints = printer.stencil.cycle_count - 20000
    wipes = stops(sim, "printer", ReasonCode.STENCIL_WIPE)
    # The wipe after the latest 10th print may not have happened yet.
    assert (prints - 1) // 10 <= wipes <= prints // 10
    assert all(r["prints_since_wipe"] < 10 for r in printer_measurements(sim))


def test_paste_mass_balance_and_paste_low_stops():
    sim = Simulator(default_single_product(), seed=4)
    sim.run(24 * 3600)
    printer = sim.station("printer")
    prints = printer.stencil.cycle_count - 20000
    refills = stops(sim, "printer", ReasonCode.PASTE_LOW)
    assert refills > 0
    expected_g = 400 + 300 * refills - 1.2 * prints
    assert printer.paste.grams == pytest.approx(expected_g, abs=20)   # ±4 sd of summed noise
    # PASTE_LOW is a material stop, not a fault.
    low = [e for e in sim.event_log.for_station("printer") if e.reason_code == ReasonCode.PASTE_LOW]
    assert all(e.new_state == StationState.STARVED_MATERIAL for e in low)


def test_stencil_wear_drifts_volume_down():
    cfg = default_single_product()
    stencil = station_cfg(cfg, "printer")["stencil"]
    stencil["wear_loss_pct_per_1k_cycles"] = 20.0   # accelerated wear
    stencil["initial_cycles"] = 0
    sim = Simulator(cfg, seed=3)
    sim.run(8 * 3600)
    m = printer_measurements(sim)
    x = np.array([r["stencil_cycles"] for r in m])
    y = np.array([r["paste_volume_mean_pct"] for r in m])
    assert np.polyfit(x, y, 1)[0] == pytest.approx(-0.02, abs=0.005)   # 20 % per 1000 cycles


# ---- feeders and splicing -------------------------------------------------


def parts_placed(pnp) -> int:
    in_progress = len(pnp._placements) if pnp.current_board is not None and pnp._picking_board is pnp.current_board else 0
    return 120 * pnp.part_count + in_progress


def test_every_part_taken_is_placed_or_mispicked():
    sim = Simulator(default_single_product(), seed=5)
    sim.run(24 * 3600)
    pnp = sim.station("pnp")
    parts_cfg = station_cfg(load_config(), "pnp")["feeders"]["parts"]
    taken = 0
    for part, f in pnp.feeders.items():
        initial = parts_cfg[part].get("initial_qty", f.reel_qty)
        taken += initial + f.reels_loaded * f.reel_qty - f.qty
    assert taken == parts_placed(pnp) + pnp.mispick_count


def test_fast_operator_splices_without_stopping():
    cfg = default_single_product()
    cfg["line"]["operator"] = {"response_median_s": 5, "response_sigma": 0.1}
    station_cfg(cfg, "pnp")["feeders"]["splice_miss_probability"] = 0.0
    sim = Simulator(cfg, seed=6)
    sim.run(24 * 3600)
    assert stops(sim, "pnp", ReasonCode.FEEDER_EMPTY) == 0
    assert sum(f.reels_loaded for f in sim.station("pnp").feeders.values()) > 5


def test_missed_splice_calls_cause_feeder_empty_stops():
    cfg = default_single_product()
    station_cfg(cfg, "pnp")["feeders"]["splice_miss_probability"] = 1.0
    sim = Simulator(cfg, seed=6)
    sim.run(24 * 3600)
    pnp = sim.station("pnp")
    reels = sum(f.reels_loaded for f in pnp.feeders.values())
    assert reels > 5
    assert stops(sim, "pnp", ReasonCode.FEEDER_EMPTY) == reels   # every new reel came after running empty
    empty = [e for e in sim.event_log.for_station("pnp") if e.reason_code == ReasonCode.FEEDER_EMPTY]
    assert all(e.new_state == StationState.STARVED_MATERIAL for e in empty)


# ---- nozzle clog on the line ---------------------------------------------


def test_injected_nozzle_clog_is_caught_and_leaves_traceable_offsets():
    sim = Simulator(default_single_product(), seed=8)
    sim.run(3600)
    pnp = sim.station("pnp")
    pnp.nozzles[2].clogged = True
    clogs_before = stops(sim, "pnp", ReasonCode.NOZZLE_CLOG)
    sim.run(600)

    assert stops(sim, "pnp", ReasonCode.NOZZLE_CLOG) == clogs_before + 1
    assert not pnp.nozzles[2].clogged   # cleaned

    # Boards picked between the clog and its detection carry nozzle 2's large offsets.
    detected_s = [e.sim_time_s for e in sim.event_log.for_station("pnp")
                  if e.reason_code == ReasonCode.NOZZLE_CLOG][-1]
    boards = [b for b in sim.all_boards()
              if "pnp" in b.measurements and 3600 <= b.time_in_s["pnp"] <= detected_s]
    clogged_offsets, other_offsets = [], []
    for b in boards:
        for _part, nozzle_id, offset in b.measurements["pnp"]["placements"]:
            (clogged_offsets if nozzle_id == 2 else other_offsets).append(offset)
    assert clogged_offsets
    assert np.std(clogged_offsets) > 2 * np.std(other_offsets)


# ---- tags -----------------------------------------------------------------


def test_consumable_tags_are_flat_and_primitive():
    sim = Simulator(default_single_product())
    sim.run(600)
    tags = sim.get_tags()
    assert {"paste_g", "prints_since_wipe", "stencil_cycles"} <= tags["printer"].keys()
    assert "slot1_qty" in tags["pnp"] and "mispick_count" in tags["pnp"]
    for entity in tags.values():
        for value in entity.values():
            assert isinstance(value, (str, int, float))
