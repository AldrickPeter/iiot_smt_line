"""Contract tests: the schemas, the example messages and the oracle agree.

These guard docs/INTERFACES.md. If one of them fails, a contract and the code
that is supposed to follow it have drifted apart.
"""

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

from smt_sim.defects import DefectType
from smt_sim.events import ALL_REASON_CODES, StationState
from tools.oracle import CATEGORY, run_oracle

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "contracts" / "schemas"
EXAMPLES = ROOT / "contracts" / "examples"
EPOCH = datetime(2026, 10, 21, 6, 0, tzinfo=timezone.utc)


def validator(name: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json.loads((SCHEMAS / f"{name}.schema.json").read_text()))


def assert_valid(name: str, payload: dict) -> None:
    errors = [f"{list(e.path)}: {e.message}" for e in validator(name).iter_errors(payload)]
    assert not errors, f"{name}: {errors}"


@pytest.fixture(scope="module")
def nozzle_run():
    return run_oracle(seed=1, scenario="nozzle_fault", hours=1, epoch=EPOCH)


@pytest.fixture(scope="module")
def factory_run():
    return run_oracle(seed=3, factory_config=str(ROOT / "config" / "factory.yaml"), hours=0.5, epoch=EPOCH)


# ---- schemas and examples -----------------------------------------------------


@pytest.mark.parametrize("path", sorted(SCHEMAS.glob("*.schema.json")), ids=lambda p: p.stem)
def test_schema_is_valid_json_schema(path):
    jsonschema.Draft202012Validator.check_schema(json.loads(path.read_text()))


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.json")), ids=lambda p: p.stem)
def test_example_matches_its_schema(path):
    assert_valid(path.stem, json.loads(path.read_text()))


def test_every_schema_has_an_example():
    assert {p.name.removesuffix(".schema.json") for p in SCHEMAS.glob("*.schema.json")} == \
           {p.stem for p in EXAMPLES.glob("*.json")}


def test_schema_enums_match_the_simulator():
    """Adding a state, reason code or defect type in smt_sim must update the contract too."""
    interval = json.loads((SCHEMAS / "mqtt_interval.schema.json").read_text())["properties"]
    assert set(interval["state"]["enum"]) == {s.value for s in StationState}
    assert set(interval["reason_code"]["enum"]) - {None} == ALL_REASON_CODES
    assert set(interval["category"]["enum"]) == set(CATEGORY.values())
    assert set(CATEGORY) == set(StationState)
    board = json.loads((SCHEMAS / "mqtt_board.schema.json").read_text())["properties"]
    defect_types = {v for k, v in vars(DefectType).items() if not k.startswith("_")}
    assert set(board["detected_defects"]["items"]["properties"]["type"]["enum"]) == defect_types


# ---- the oracle follows the contract -------------------------------------------


@pytest.mark.parametrize("run", ["nozzle_run", "factory_run"])
def test_oracle_output_matches_the_schemas(run, request):
    result = request.getfixturevalue(run)
    for events in result.i2_events.values():
        for e in events:
            assert_valid("opcua_last_event", e)
    for boards in result.i2_boards.values():
        for b in boards:
            assert_valid("opcua_last_board", b)
    for topic, payload in result.i3_messages:
        assert topic.startswith("smt/v1/lab/")
        assert_valid(f"mqtt_{topic.rsplit('/', 1)[-1]}", payload)


@pytest.mark.parametrize("run", ["nozzle_run", "factory_run"])
def test_sequences_have_no_gaps_and_ids_are_unique(run, request):
    result = request.getfixturevalue(run)
    for line in result.i2_events:
        assert [e["seq"] for e in result.i2_events[line]] == list(range(1, len(result.i2_events[line]) + 1))
        assert [b["seq"] for b in result.i2_boards[line]] == list(range(1, len(result.i2_boards[line]) + 1))
    msg_ids = [p["msg_id"] for _, p in result.i3_messages]
    assert len(msg_ids) == len(set(msg_ids))


def test_intervals_tile_each_stations_timeline(nozzle_run):
    """Each station's intervals follow on without gaps or overlaps, starting at t=0."""
    by_station = defaultdict(list)
    for p in nozzle_run.messages("interval"):
        by_station[p["station"]].append(p)
    for station, intervals in by_station.items():
        assert intervals[0]["start"] == "2026-10-21T06:00:00.000Z", station
        for before, after in zip(intervals, intervals[1:]):
            assert before["end"] == after["start"], station


def test_oracle_is_deterministic():
    """Same seed, same messages: this is what lets L4 de-duplicate replays by msg_id."""
    a = run_oracle(seed=5, hours=0.2, epoch=EPOCH)
    b = run_oracle(seed=5, hours=0.2, epoch=EPOCH)
    assert a.i3_messages == b.i3_messages


# ---- I3 carries enough to compute I4 -------------------------------------------


def reference_l4(result, line: str, end_s: float) -> dict:
    """A minimal L4: rebuild the I4 metrics from MQTT messages only.

    If this matches the simulator's own metrics, the MQTT contract carries
    everything the cloud needs. It is also a worked example for the L4 owner.
    """
    msgs = [p for p in (q for _, q in result.i3_messages) if p.get("line") == line]
    ideal = {(r["station"], r["product"]): r["ideal_cycle_s"]
             for r in result.i4_expected["master_data"]["ideal_cycle"][line]}

    # Time in state: closed intervals + the open interval from each station's latest state message.
    time_in = defaultdict(Counter)
    stops = defaultdict(lambda: [0.0, 0])
    latest_state = {}
    spans = [(p["station"], p["state"], p["reason_code"], p["duration_s"]) for p in msgs
             if p["msg_id"].split("/")[-2] == "interval"]
    for p in msgs:
        if p["msg_id"].split("/")[-2] == "state":
            latest_state[p["station"]] = p
    spans += [(s, p["state"], p["reason_code"], end_s - p["sim_time_s"]) for s, p in latest_state.items()]
    for station, state, reason, duration in spans:
        time_in[station][state] += duration
        if CATEGORY[StationState(state)] in ("own_loss", "planned"):
            stops[(station, reason or "UNKNOWN", state)][0] += duration
            stops[(station, reason or "UNKNOWN", state)][1] += 1

    final_counts = {p["station"]: p for p in msgs
                    if p["msg_id"].split("/")[-3] == "counts" and p["sim_time_s"] == end_s}
    boards = [p for p in msgs if p["msg_id"].split("/")[-2] == "board"]
    blamed = Counter(origin for b in boards for origin in {d["attributed_origin"] for d in b["detected_defects"]})

    def ratio(a, b):
        return a / b if b > 0 else math.nan

    station_oee = {}
    for station, t in time_in.items():
        planned = end_s - t["PM"]
        running = t["RUNNING"]
        counts = final_counts[station]
        ideal_work = sum(n * ideal[(station, prod)] for prod, n in counts["part_count_by_product"].items())
        availability = ratio(running, planned - t["STARVED"] - t["BLOCKED"])
        performance = 1.0 if station == "reflow" else ratio(ideal_work, running)
        quality = 1 - ratio(blamed[station], counts["part_count"]) if counts["part_count"] else math.nan
        station_oee[station] = {"running_s": running, "availability": availability,
                                "performance": performance, "quality": quality,
                                "oee": availability * performance * quality}

    bottleneck = {prod: max(v for (s, p), v in ideal.items() if p == prod) for (_, prod) in ideal}
    good = Counter(b["product"] for b in boards if b["disposition"] == "good")
    dispositions = Counter(b["disposition"] for b in boards)
    return {
        "station_oee": station_oee,
        "line_oee": ratio(sum(n * bottleneck[p] for p, n in good.items()), end_s),
        "quality": {"finished": len(boards), "good": dispositions["good"], "rework": dispositions["rework"],
                    "scrap": dispositions["scrap"], "fpy": ratio(dispositions["good"], len(boards))},
        "pareto": sorted(((k[0], k[1], k[2], round(v[0], 6), v[1]) for k, v in stops.items()),
                         key=lambda r: -r[3]),
    }


def same(a: float, b: float) -> bool:
    return (math.isnan(a) and math.isnan(b)) or math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


@pytest.mark.parametrize("run", ["nozzle_run", "factory_run"])
def test_mqtt_messages_reproduce_the_simulator_metrics(run, request):
    result = request.getfixturevalue(run)
    for line, expected in result.i4_expected["lines"].items():
        got = reference_l4(result, line, expected["observed_s"])
        for station, exp in expected["station_oee"].items():
            for key in ("running_s", "availability", "performance", "quality", "oee"):
                assert same(got["station_oee"][station][key], exp[key]), (line, station, key)
        assert same(got["line_oee"], expected["line_oee"]["oee"])
        assert {k: v for k, v in got["quality"].items() if k != "fpy"} == \
               {k: v for k, v in expected["quality"].items() if k != "fpy"}
        assert same(got["quality"]["fpy"], expected["quality"]["fpy"])
        assert got["pareto"] == [(r["station"], r["reason_code"], r["state"], round(r["total_s"], 6),
                                  r["occurrences"]) for r in expected["downtime_pareto"]]
