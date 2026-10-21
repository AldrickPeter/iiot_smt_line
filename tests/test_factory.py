"""Factory: several lines on one clock, built from config/factory.yaml."""

import pytest
import yaml

from smt_sim import Simulator
from smt_sim.config import apply_station_overrides, load_config
from smt_sim.events import ReasonCode, StationState
from smt_sim.factory import DEFAULT_FACTORY_PATH, Factory, build_line_config, factory_summary, line_seeds
from smt_sim.metrics import find_bottleneck, line_oee


def test_factory_builds_both_lines_with_their_overrides():
    factory = Factory.from_config()
    assert list(factory.lines) == ["line1", "line2"]
    line1, line2 = factory.lines["line1"], factory.lines["line2"]
    assert line1.station("pnp").params["time_per_component_s"] == 0.25
    assert line2.station("pnp").params["time_per_component_s"] == 0.18
    assert [j["product"] for j in line2.config["production_plan"]["jobs"]] == ["POWER-BOARD-C", "SENSOR-BOARD-B"]
    assert len(line1.config["production_plan"]["jobs"]) == 3   # line 1 is the default config
    assert "faster placement" in factory.descriptions["line2"]


def test_lines_get_different_seeds_derived_from_one_factory_seed():
    seeds = line_seeds(42, ["line1", "line2"])
    assert seeds["line1"] != seeds["line2"]
    assert seeds == line_seeds(42, ["line1", "line2"])        # reproducible
    assert seeds != line_seeds(43, ["line1", "line2"])


def test_same_factory_seed_gives_identical_runs():
    a, b = Factory.from_config(seed=7), Factory.from_config(seed=7)
    a.run(4 * 3600)
    b.run(4 * 3600)
    for name in a.lines:
        assert a.lines[name].event_log.events == b.lines[name].event_log.events


def test_lines_are_independent():
    """Line 2 in a factory behaves exactly like line 2 run on its own, even if line 1 breaks."""
    factory = Factory.from_config(seed=5)
    factory.lines["line1"].station("pnp").force_down(ReasonCode.MANUAL_STOP, 3600)
    factory.run(3 * 3600)

    with open(DEFAULT_FACTORY_PATH, encoding="utf-8") as f:
        line2_cfg = yaml.safe_load(f)["lines"]["line2"]
    alone = Simulator(build_line_config(line2_cfg), seed=line_seeds(5, ["line1", "line2"])["line2"])
    alone.run(3 * 3600)
    assert alone.event_log.events == factory.lines["line2"].event_log.events


def test_clock_and_tags():
    factory = Factory.from_config()
    factory.run(600)
    assert all(sim.time_s == 600 for sim in factory.lines.values()) and factory.time_s == 600
    tags = factory.get_tags()
    assert tags["line2/pnp"]["state"] in {s.value for s in StationState}
    assert {"line1/ict", "line2/ict", "line1/printer->spi"} <= tags.keys()
    for entity in tags.values():
        for value in entity.values():
            assert isinstance(value, (str, int, float))


def test_faster_placement_moves_line2_bottleneck_to_ict():
    factory = Factory.from_config(seed=1)
    factory.run(12 * 3600)
    assert find_bottleneck(factory.lines["line1"]).station == "pnp"
    assert find_bottleneck(factory.lines["line2"]).station == "ict"


def test_factory_summary_combines_lines():
    factory = Factory.from_config(seed=1)
    factory.run(8 * 3600)
    per_line, oee, fpy = factory_summary(factory)
    assert oee == pytest.approx(sum(line_oee(s).oee for s in factory.lines.values()) / 2)
    good = sum(s.good_boards for s in per_line.values())
    assert good == factory.boards_completed
    assert fpy == pytest.approx(good / sum(s.finished_boards for s in per_line.values()))


def test_station_override_must_name_a_real_station():
    with pytest.raises(ValueError, match="no_such_station"):
        apply_station_overrides(load_config(), {"no_such_station": {"cycle_time_s": 1}})


def test_mismatched_tick_sizes_are_rejected():
    cfg = load_config()
    cfg["sim"]["tick_s"] = 1.0
    with pytest.raises(ValueError, match="tick size"):
        Factory({"a": Simulator(), "b": Simulator(cfg)})
