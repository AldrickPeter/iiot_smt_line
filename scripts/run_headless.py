"""Run the line headless and print a metrics report.

Usage:
    python scripts/run_headless.py [--hours 8] [--config path.yaml] [--seed 42]
                                   [--events-csv out/events.csv] [--boards-csv out/boards.csv]
    python scripts/run_headless.py --scenario nozzle_fault [--baseline] [--seed 1]
    python scripts/run_headless.py --factory [config/factory.yaml] [--hours 24]

With --scenario the run length defaults to the scenario's duration_h, and the
report ends with the scenario's expected root cause. --baseline runs the same
scenario without its interventions, for comparison.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # allow running without install

from smt_sim import Simulator  # noqa: E402
from smt_sim.export import boards_to_csv  # noqa: E402
from smt_sim.metrics import finished_boards, format_report  # noqa: E402
from smt_sim.factory import DEFAULT_FACTORY_PATH, Factory, format_factory_summary  # noqa: E402
from smt_sim.scenarios import load_scenario  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=None, help="default 8, or the scenario's duration")
    parser.add_argument("--config", default=None)
    parser.add_argument("--scenario", default=None, help="name in config/scenarios/ or a path")
    parser.add_argument("--baseline", action="store_true", help="run the scenario without its interventions")
    parser.add_argument("--factory", nargs="?", const=str(DEFAULT_FACTORY_PATH), default=None,
                        help="run every line in a factory file (default config/factory.yaml)")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--events-csv", default=None, help="write the event log to this CSV file")
    parser.add_argument("--boards-csv", default=None, help="write finished board records to this CSV file")
    args = parser.parse_args()

    if args.factory:
        run_factory(args)
        return

    scenario = None
    if args.scenario:
        scenario = load_scenario(args.scenario)
        sim = scenario.build_simulator(seed=args.seed, with_interventions=not args.baseline)
        hours = args.hours or scenario.duration_h
        print(f"Scenario {scenario.name}{' (BASELINE, no interventions)' if args.baseline else ''}: "
              f"{scenario.description}\n")
    else:
        sim = Simulator(args.config, seed=args.seed)
        hours = args.hours or 8.0
    args.hours = hours
    sim_s = hours * 3600

    wall_start = time.perf_counter()
    sim.run(sim_s)
    wall_s = time.perf_counter() - wall_start

    print(f"Simulated {args.hours:g} h in {wall_s:.2f} s wall time "
          f"({sim_s / wall_s:,.0f}x real time)")
    print(f"Boards completed: {sim.boards_completed}  "
          f"({sim.boards_completed / args.hours:.1f} boards/h)")
    print(f"Event log entries: {len(sim.event_log)}\n")
    print(format_report(sim))
    if scenario is not None:
        print(f"\nScenario expects: {scenario.expected_root_cause}")
        for at_s, label in sim.intervention_log:
            print(f"  applied at {at_s:7.0f} s: {label}")

    if args.events_csv:
        Path(args.events_csv).parent.mkdir(parents=True, exist_ok=True)
        sim.event_log.to_csv(args.events_csv)
        print(f"\nEvent log written to {args.events_csv}")
    if args.boards_csv:
        Path(args.boards_csv).parent.mkdir(parents=True, exist_ok=True)
        boards_to_csv(finished_boards(sim), args.boards_csv)
        print(f"Board records written to {args.boards_csv}")


def run_factory(args: argparse.Namespace) -> None:
    """Run all lines of a factory; print a factory summary, then each line's full report.

    CSV files get the line name added: out/boards.csv -> out/boards_line1.csv, ...
    """
    factory = Factory.from_config(args.factory, seed=args.seed)
    hours = args.hours or 8.0
    wall_start = time.perf_counter()
    factory.run(hours * 3600)
    wall_s = time.perf_counter() - wall_start

    print(f"Simulated {hours:g} h of {len(factory.lines)} lines in {wall_s:.2f} s wall time\n")
    print(format_factory_summary(factory))
    for name, sim in factory.lines.items():
        print(f"\n{'=' * 30} {name} {'=' * 30}")
        if factory.descriptions.get(name):
            print(factory.descriptions[name] + "\n")
        print(format_report(sim))
        for flag, write in ((args.events_csv, lambda p, s=sim: s.event_log.to_csv(p)),
                            (args.boards_csv, lambda p, s=sim: boards_to_csv(finished_boards(s), p))):
            if flag:
                path = Path(flag).with_stem(f"{Path(flag).stem}_{name}")
                path.parent.mkdir(parents=True, exist_ok=True)
                write(path)
                print(f"Written: {path}")


if __name__ == "__main__":
    main()
