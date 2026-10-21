"""The oracle: what every layer SHOULD produce for a given simulator run.

The simulator is deterministic (same seed + config = same run), so the expected
output of every interface can be computed straight from it instead of being
written by hand. Each layer's tests compare their real output with this.

    result = run_oracle(seed=1, scenario="nozzle_fault", hours=1)
    result.i2_events     # LastEvent JSON objects, per line, in order   (I2)
    result.i2_boards     # LastBoard JSON objects                       (I2)
    result.i3_messages   # (topic, payload) for every MQTT message      (I3)
    result.i4_expected   # metrics the L4 views must match, + master data (I4)

From the command line it writes the same thing as files:

    python tools/oracle.py --scenario nozzle_fault --seed 1 --hours 1 --out out/oracle

Timing rules (these are part of the contract, see docs/INTERFACES.md):
- An event's `product` is the station's product at the END of the tick in
  which the event happened.
- A board's `ts` is the sim time of the tick in which it reached the line
  exit or a reject bin.
- `counts` are snapshots every 60 sim-s, `buffers` every 10 sim-s, both
  taken after the tick that reaches that time.
- Only closed intervals are published. The interval still open at the end
  of the run is visible through the latest `state` message.
"""

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # allow running without install

from smt_sim import Simulator  # noqa: E402
from smt_sim.board import Board  # noqa: E402
from smt_sim.events import Event, StationState  # noqa: E402
from smt_sim.factory import Factory  # noqa: E402
from smt_sim.metrics import (downtime_pareto, line_oee, quality_report,  # noqa: E402
                             state_intervals, station_oee)
from smt_sim.scenarios import load_scenario  # noqa: E402

SITE = "lab"
COUNTS_EVERY_S = 60.0
BUFFERS_EVERY_S = 10.0

# INTERFACES.md §1.3: one classification of states, shared by every layer.
CATEGORY = {
    StationState.RUNNING: "productive",
    StationState.STARVED: "external",
    StationState.BLOCKED: "external",
    StationState.DOWN: "own_loss",
    StationState.STARVED_MATERIAL: "own_loss",
    StationState.SETUP: "own_loss",
    StationState.PM: "planned",
}

STANDARD_TAGS = {"state", "reason_code", "part_count", "reject_count", "speed_pct"}


def iso(epoch: datetime, sim_time_s: float) -> str:
    """Sim time -> ISO 8601 UTC with milliseconds, e.g. 2026-10-21T06:00:12.500Z."""
    t = epoch + timedelta(seconds=sim_time_s)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def buffer_id(sim_name: str) -> str:
    """'printer->spi' -> 'buf_printer_spi' (INTERFACES.md §1.1)."""
    upstream, downstream = sim_name.split("->")
    return f"buf_{upstream}_{downstream}"


def make_run_id(epoch: datetime, seed: int | None, scenario: str | None) -> str:
    return f"{epoch:%Y%m%dT%H%MZ}-s{seed if seed is not None else 'default'}-{scenario or 'base'}"


def default_epoch() -> datetime:
    """Today 06:00 UTC, so shift A starts at sim time 0."""
    return datetime.now(timezone.utc).replace(hour=6, minute=0, second=0, microsecond=0)


@dataclass
class OracleResult:
    run_id: str
    epoch: datetime
    factory: Factory
    i2_events: dict[str, list[dict]] = field(default_factory=dict)    # line -> LastEvent objects
    i2_boards: dict[str, list[dict]] = field(default_factory=dict)    # line -> LastBoard objects
    i3_messages: list[tuple[str, dict]] = field(default_factory=list)
    i4_expected: dict[str, Any] = field(default_factory=dict)

    def messages(self, kind: str) -> list[dict]:
        """Payloads of one I3 message kind ('state', 'interval', 'counts', 'board', ...)."""
        return [p for topic, p in self.i3_messages if topic.rsplit("/", 1)[-1] == kind]


class _LineRecorder:
    """Watches one line while it runs and turns what happens into I2/I3 objects."""

    def __init__(self, line: str, sim: Simulator, result: OracleResult) -> None:
        self.line, self.sim, self.result = line, sim, result
        self.events_seen = 0
        self.event_seq = 0
        self.board_seq = 0
        self.open: dict[str, tuple[int, dict]] = {}   # station -> (seq, LastEvent) that opened its interval
        self.arrived: list[Board] = []
        result.i2_events[line] = []
        result.i2_boards[line] = []
        # Listen on the line exit and every reject bin, keeping any listener already there.
        bins = [sim.sink] + [st.rejects for st in sim.stations if getattr(st, "rejects", None) is not None]
        for bin_ in bins:
            bin_.on_arrival = self._chain(bin_.on_arrival)

    def _chain(self, previous):
        def on_arrival(board: Board) -> None:
            if previous is not None:
                previous(board)
            self.arrived.append(board)
        return on_arrival

    def _envelope(self, kind_id: str, sim_time_s: float) -> dict:
        return {"v": 1, "msg_id": f"{self.result.run_id}/{self.line}/{kind_id}", "run_id": self.result.run_id,
                "ts": iso(self.result.epoch, sim_time_s), "sim_time_s": sim_time_s}

    def _publish(self, topic_tail: str, payload: dict) -> None:
        self.result.i3_messages.append((f"smt/v1/{SITE}/{self.line}/{topic_tail}", payload))

    def after_tick(self, tick_time_s: float, now_s: float) -> None:
        """Called after each tick. tick_time_s = time the tick ran at, now_s = time after it."""
        new_events = self.sim.event_log.events[self.events_seen:]
        self.events_seen += len(new_events)
        for event in new_events:
            self._on_event(event)
        for board in self.arrived:
            self._on_board(board, tick_time_s)
        self.arrived.clear()
        if now_s % COUNTS_EVERY_S == 0:
            self._counts(now_s)
        if now_s % BUFFERS_EVERY_S == 0:
            self._buffers(now_s)

    def _on_event(self, e: Event) -> None:
        self.event_seq += 1
        station = self.sim.station(e.station)
        last_event = {
            "seq": self.event_seq, "line": self.line, "station": e.station,
            "ts": iso(self.result.epoch, e.sim_time_s), "sim_time_s": e.sim_time_s,
            "old_state": e.old_state.value if e.old_state else None,
            "new_state": e.new_state.value, "reason_code": e.reason_code or None,
            "product": station.current_product,
        }
        self.result.i2_events[self.line].append(last_event)

        # Close the interval this station was in (I3 interval), then publish the new state.
        if e.station in self.open:
            opened_seq, opened = self.open[e.station]
            self._publish(f"{e.station}/interval", {
                **self._envelope(f"interval/{opened_seq}", e.sim_time_s),
                "line": self.line, "station": e.station, "state": opened["new_state"],
                "reason_code": opened["reason_code"], "category": CATEGORY[StationState(opened["new_state"])],
                "start": opened["ts"], "end": last_event["ts"],
                "duration_s": e.sim_time_s - opened["sim_time_s"], "product": opened["product"],
            })
        self.open[e.station] = (self.event_seq, last_event)
        self._publish(f"{e.station}/state", {
            **self._envelope(f"state/{self.event_seq}", e.sim_time_s),
            "line": self.line, "station": e.station, "state": e.new_state.value,
            "reason_code": last_event["reason_code"], "category": CATEGORY[e.new_state],
            "product": station.current_product,
        })

    def _on_board(self, board: Board, tick_time_s: float) -> None:
        self.board_seq += 1
        record = {
            "line": self.line, "serial": board.serial, "product": board.product,
            "disposition": board.disposition, "rejected_at": board.rejected_at,
            "detected_defects": [{"type": d.type, "detected_at": d.detected_at,
                                  "attributed_origin": d.attributed_origin} for d in board.detected_defects],
            "false_calls": [{"type": d.type, "detected_at": d.detected_at} for d in board.false_calls],
            "order_id": board.order_id,
        }
        ts = iso(self.result.epoch, tick_time_s)
        self.result.i2_boards[self.line].append(
            {"seq": self.board_seq, "ts": ts, "sim_time_s": tick_time_s, **record})
        self._publish("board", {**self._envelope(f"board/{board.serial}", tick_time_s), **record})

    def _counts(self, now_s: float) -> None:
        tags = self.sim.get_tags()
        for st in self.sim.stations:
            t = tags[st.name]
            self._publish(f"{st.name}/counts", {
                **self._envelope(f"counts/{st.name}/{int(now_s)}", now_s),
                "line": self.line, "station": st.name,
                "part_count": t["part_count"], "reject_count": t["reject_count"],
                "part_count_by_product": dict(st.parts_by_product), "speed_pct": t["speed_pct"],
                "extra": {k: v for k, v in t.items() if k not in STANDARD_TAGS},
            })

    def _buffers(self, now_s: float) -> None:
        self._publish("buffers", {
            **self._envelope(f"buffers/{int(now_s)}", now_s), "line": self.line,
            "levels": {buffer_id(b.name): len(b) for b in self.sim.buffers},
            "capacity": max(b.capacity for b in self.sim.buffers),
        })


def expected_metrics(sim: Simulator) -> dict[str, Any]:
    """What L4's views must show for one line over the whole run (INTERFACES.md §5)."""
    q = quality_report(sim)
    lo = line_oee(sim)
    return {
        "observed_s": sim.time_s,
        "station_oee": {name: {"planned_s": s.planned_s, "running_s": s.running_s, "starved_s": s.starved_s,
                               "blocked_s": s.blocked_s, "boards": s.boards_processed,
                               "ideal_work_s": s.ideal_work_s, "availability": s.availability,
                               "performance": s.performance, "quality": s.quality, "oee": s.oee}
                        for name, s in station_oee(sim).items()},
        "line_oee": {"planned_s": lo.planned_s, "good_out": lo.boards_out, "oee": lo.oee},
        "quality": {"finished": q.boards_finished, "good": q.good, "rework": q.rework, "scrap": q.scrap,
                    "fpy": q.first_pass_yield},
        "downtime_pareto": [{"station": r.station, "reason_code": r.reason_code, "state": r.state.value,
                             "total_s": r.total_s, "occurrences": r.occurrences}
                            for r in downtime_pareto(state_intervals(sim.event_log, sim.time_s))],
        "reject_blame": [{"blamed_station": s, "defect_type": t, "count": n} for s, t, n in q.detected_pareto],
    }


def ideal_cycles(sim: Simulator) -> list[dict]:
    """Master data for L4's ideal_cycle table."""
    return [{"station": st.name, "product": p, "ideal_cycle_s": sim.ideal_cycle_time_s(st.name, p)}
            for st in sim.stations for p in sim.products]


def run_oracle(seed: int | None = None, scenario: str | None = None, hours: float = 1.0,
               factory_config: str | None = None, epoch: datetime | None = None,
               speed_factor: float = 60.0) -> OracleResult:
    """Run the simulator once and record what every interface should carry.

    Use `scenario` for one line with a scripted fault, or `factory_config` for
    a multi-line factory (config/factory.yaml). Neither gives the default line.
    """
    if scenario and factory_config:
        raise ValueError("Pick a scenario (one line) or a factory config, not both")
    epoch = epoch or default_epoch()
    if scenario:
        factory = Factory.single(load_scenario(scenario).build_simulator(seed=seed))
    elif factory_config:
        factory = Factory.from_config(factory_config, seed=seed)
    else:
        factory = Factory.single(Simulator(seed=seed))

    result = OracleResult(make_run_id(epoch, seed, scenario), epoch, factory)
    result.i3_messages.append((f"smt/v1/{SITE}/run", {
        "v": 1, "msg_id": f"{result.run_id}/run", "run_id": result.run_id, "ts": iso(epoch, 0.0),
        "sim_time_s": 0.0, "epoch": iso(epoch, 0.0), "speed_factor": speed_factor, "seed": seed,
        "scenario": scenario, "lines": list(factory.lines)}))
    recorders = [_LineRecorder(name, sim, result) for name, sim in factory.lines.items()]

    end_s = hours * 3600
    while factory.time_s < end_s:
        tick_time_s = factory.time_s
        factory.step()
        for rec in recorders:
            rec.after_tick(tick_time_s, factory.time_s)

    result.i4_expected = {
        "run_id": result.run_id,
        "lines": {name: expected_metrics(sim) for name, sim in factory.lines.items()},
        "master_data": {"ideal_cycle": {name: ideal_cycles(sim) for name, sim in factory.lines.items()}},
    }
    return result


def write_files(result: OracleResult, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "i2_events.jsonl", "w", encoding="utf-8") as f:
        for events in result.i2_events.values():
            f.writelines(json.dumps(e) + "\n" for e in events)
    with open(out_dir / "i2_boards.jsonl", "w", encoding="utf-8") as f:
        for boards in result.i2_boards.values():
            f.writelines(json.dumps(b) + "\n" for b in boards)
    with open(out_dir / "i3_messages.jsonl", "w", encoding="utf-8") as f:
        f.writelines(json.dumps({"topic": t, "payload": p}) + "\n" for t, p in result.i3_messages)
    with open(out_dir / "i4_expected.json", "w", encoding="utf-8") as f:
        json.dump(result.i4_expected, f, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--scenario", default=None, help="name in config/scenarios/ or a path")
    parser.add_argument("--factory", nargs="?", const="config/factory.yaml", default=None,
                        help="run every line in a factory file")
    parser.add_argument("--hours", type=float, default=1.0)
    parser.add_argument("--epoch", default=None, help="ISO time of sim t=0, default today 06:00Z")
    parser.add_argument("--out", default="out/oracle")
    args = parser.parse_args()

    epoch = (datetime.fromisoformat(args.epoch.replace("Z", "+00:00")) if args.epoch else None)
    result = run_oracle(seed=args.seed, scenario=args.scenario, hours=args.hours,
                        factory_config=args.factory, epoch=epoch)
    write_files(result, Path(args.out))
    kinds = Counter(t.rsplit("/", 1)[-1] for t, _ in result.i3_messages)
    print(f"run_id {result.run_id}: "
          f"{sum(len(e) for e in result.i2_events.values())} events, "
          f"{sum(len(b) for b in result.i2_boards.values())} boards, "
          f"{len(result.i3_messages)} MQTT messages {dict(kinds)} -> {args.out}/")


if __name__ == "__main__":
    main()
