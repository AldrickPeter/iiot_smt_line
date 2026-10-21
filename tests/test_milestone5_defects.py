"""Milestone 5: measurements -> true defects -> imperfect inspection -> FPY and attribution."""

import csv

import numpy as np
import pytest

from smt_sim import Simulator
from smt_sim.config import load_config, make_deterministic
from smt_sim.defects import DefectType, Disposition
from smt_sim.export import BOARD_COLUMNS, boards_to_csv
from smt_sim.metrics import finished_boards, line_oee, quality_report, station_oee
from smt_sim.stations.reflow import ZoneDrift

from helpers import default_single_product, single_product


def station_cfg(cfg: dict, name: str) -> dict:
    return next(st for st in cfg["line"]["stations"] if st["name"] == name)


def run(cfg: dict, hours: float, seed: int = 1) -> Simulator:
    sim = Simulator(cfg, seed=seed)
    sim.run(hours * 3600)
    return sim


def defects_of(board, defect_type: str) -> list:
    return [d for d in board.true_defects if d.type == defect_type]


@pytest.fixture(scope="module")
def day_run() -> Simulator:
    """One shared 24 h run of the default single-product line."""
    return run(default_single_product(), 24, seed=4)


# ---- every true defect comes from an out-of-spec physical quantity ---------


def test_paste_defects_match_the_measured_pad_volumes(day_run):
    for b in finished_boards(day_run):
        m = b.measurements["printer"]
        assert bool(defects_of(b, DefectType.INSUFFICIENT_PASTE)) == (m["paste_volume_min_pct"] < 70)
        assert bool(defects_of(b, DefectType.EXCESS_PASTE)) == (m["paste_volume_max_pct"] > 130)
        for d in b.true_defects:
            if d.type in (DefectType.INSUFFICIENT_PASTE, DefectType.EXCESS_PASTE):
                assert d.origin_station == "printer"


def test_misplacements_match_offsets_beyond_tolerance(day_run):
    parts = station_cfg(day_run.config, "pnp")["feeders"]["parts"]
    for b in finished_boards(day_run):
        if "pnp" not in b.measurements:
            continue   # rejected at SPI before placement
        beyond = [p for p in b.measurements["pnp"]["placements"] if abs(p[2]) > parts[p[0]]["tolerance_um"]]
        assert len(defects_of(b, DefectType.MISPLACEMENT)) == len(beyond)


def test_cold_joints_only_when_peak_is_too_low():
    cfg = default_single_product()
    station_cfg(cfg, "reflow")["temperature"]["drift_sd_c"] = 8.0   # badly controlled oven
    sim = run(cfg, 24, seed=2)
    cold = [b for b in finished_boards(sim) if defects_of(b, DefectType.COLD_JOINT)]
    assert cold
    assert all(b.measurements["reflow"]["peak_deviation_c"] < -5 for b in cold)


def test_zone_drift_wanders_by_its_configured_spread():
    drift = ZoneDrift({"drift_sd_c": 3.0, "drift_time_constant_s": 600}, np.random.default_rng(0))
    samples = []
    for i in range(400_000):
        drift.advance(0.5)
        if i % 200 == 0:
            samples.append(drift.offset_c)
    assert np.std(samples) == pytest.approx(3.0, rel=0.1)


# ---- ground truth is kept apart from observation ----------------------------


def test_board_records_are_consistent(day_run):
    boards = finished_boards(day_run)
    for b in boards:
        assert b.disposition in (Disposition.GOOD, Disposition.REWORK, Disposition.SCRAP)
        if b.disposition == Disposition.GOOD:
            assert b.rejected_at is None and not b.detected_defects and not b.false_calls
        else:
            assert b.rejected_at is not None and (b.detected_defects or b.false_calls)
        true_types = {d.type for d in b.true_defects}
        assert all(d.type in true_types for d in b.detected_defects)   # can only detect what exists
    q = quality_report(day_run)
    assert q.good + q.rework + q.scrap == q.boards_finished == len(boards)
    assert q.first_pass_yield == pytest.approx(q.good / q.boards_finished)


def test_reject_counts_and_tags_agree(day_run):
    q = quality_report(day_run)
    tags = day_run.get_tags()
    for name in ("spi", "aoi", "ict"):
        assert tags[name]["reject_count"] == q.rejects_by_station.get(name, 0)
    assert tags["printer"]["reject_count"] == 0


def test_line_oee_counts_only_good_boards(day_run):
    q = quality_report(day_run)
    assert line_oee(day_run).boards_out == day_run.boards_completed == q.good


# ---- inspection is imperfect, in measurable ways ----------------------------


def test_spi_decides_from_its_noisy_measurement(day_run):
    for b in finished_boards(day_run):
        true_min = b.measurements["printer"]["paste_volume_min_pct"]
        flagged_at_spi = b.rejected_at == "spi"
        if true_min < 64:      # 4 sd of SPI noise below the limit: always caught
            assert flagged_at_spi
        if true_min > 76 and b.measurements["printer"]["paste_volume_max_pct"] < 124:
            assert not flagged_at_spi


def test_aoi_detection_rate_matches_config():
    cfg = default_single_product()
    station_cfg(cfg, "pnp")["nozzles"]["base_offset_sd_um"] = 60   # sloppy placement -> many misplacements
    station_cfg(cfg, "aoi")["false_calls_per_board"] = 0.0
    sim = run(cfg, 12, seed=3)
    seen = caught = 0
    for b in finished_boards(sim):
        if "aoi" not in b.time_in_s:
            continue
        seen += len(defects_of(b, DefectType.MISPLACEMENT))
        caught += sum(1 for d in b.detected_defects if d.detected_at == "aoi" and d.type == DefectType.MISPLACEMENT)
    assert seen > 300
    assert caught / seen == pytest.approx(0.95, abs=0.03)


def test_ict_catches_what_aoi_misses():
    cfg = default_single_product()
    station_cfg(cfg, "pnp")["nozzles"]["base_offset_sd_um"] = 60
    station_cfg(cfg, "aoi")["detection_rate"]["MISPLACEMENT"] = 0.0   # AOI blind to misplacement
    sim = run(cfg, 8, seed=3)
    ict_catches = [d for b in finished_boards(sim) for d in b.detected_defects
                   if d.detected_at == "ict" and d.type == DefectType.MISPLACEMENT]
    assert len(ict_catches) > 20
    assert quality_report(sim).escapes > 0   # ICT (40 %) still lets some through


def test_false_calls_reject_good_boards():
    cfg = single_product(make_deterministic(load_config()))   # no real defects at all
    station_cfg(cfg, "aoi")["false_calls_per_board"] = 0.2
    sim = run(cfg, 8, seed=5)
    q = quality_report(sim)
    assert q.false_rejects == q.rework + q.scrap > 0
    assert q.first_pass_yield == pytest.approx(np.exp(-0.2), abs=0.03)   # P(Poisson(0.2) == 0)
    assert q.detected_pareto == []


def test_many_calls_mean_scrap():
    cfg = single_product(make_deterministic(load_config()))
    station_cfg(cfg, "aoi")["false_calls_per_board"] = 5.0
    sim = run(cfg, 4, seed=6)
    q = quality_report(sim)
    assert q.scrap > q.rework   # Poisson(5) is usually >= 3 calls


# ---- metrics point to the true root cause ----------------------------------


def test_worn_stencil_shows_up_as_printer_paste_defects():
    cfg = default_single_product()
    station_cfg(cfg, "printer")["stencil"]["initial_cycles"] = 60_000   # -18 % transfer from wear
    sim = run(cfg, 8, seed=7)
    q = quality_report(sim)
    origin, defect_type, _ = q.detected_pareto[0]
    assert (origin, defect_type) == ("printer", DefectType.INSUFFICIENT_PASTE)
    assert station_oee(sim)["printer"].quality < station_oee(sim)["pnp"].quality


def test_clogged_nozzle_shows_up_as_pnp_misplacements():
    cfg = default_single_product()
    station_cfg(cfg, "pnp")["nozzles"]["clog_detect_consecutive_fails"] = 1000   # vision never notices
    sim = Simulator(cfg, seed=8)
    sim.run(1800)
    sim.station("pnp").nozzles[1].clogged = True
    sim.run(4 * 3600)
    q = quality_report(sim)
    origin, defect_type, _ = q.detected_pareto[0]
    assert (origin, defect_type) == ("pnp", DefectType.MISPLACEMENT)
    assert q.attribution_accuracy == 1.0


# ---- export -----------------------------------------------------------------


def test_board_csv_export(day_run, tmp_path):
    path = tmp_path / "boards.csv"
    boards = finished_boards(day_run)
    boards_to_csv(boards, path)
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == len(boards)
    assert list(rows[0].keys()) == BOARD_COLUMNS
    rejected = [r for r in rows if r["disposition"] != "good"]
    assert rejected and all(r["rejected_at"] for r in rejected)
