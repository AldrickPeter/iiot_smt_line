"""Scenarios: a run setup plus faults scheduled at sim times.

A scenario YAML file looks like:

    name: nozzle_fault
    description: ...
    duration_h: 4
    config_overrides:          # run setup, deep-merged into config/default.yaml
      production_plan: {jobs: [{product: CTRL-BOARD-A}]}
    interventions:             # what goes wrong; applied at the first tick at/after at_s
      - {at_s: 0, action: set_clog_detection, station: pnp, consecutive_fails: 6}
      - {at_s: 1800, action: clog_nozzle, station: pnp, nozzle: 1}
    expected_root_cause:       # what the metrics should point to (checked by tests)
      defect: {station: pnp, type: MISPLACEMENT}
      # or: bottleneck_stop: {station: pnp, reason_code: FEEDER_EMPTY}

The split matters: `config_overrides` describe a normal run, and every fault
is an intervention (at_s: 0 means "from the start"). So the same scenario
without its interventions is a fair baseline to compare against.

Interventions only change physical state or settings (a nozzle clogs, a reel
is nearly empty, the operator gets slow). They never write defects or stops
directly: those must still emerge from the normal cause -> effect chain, which
is exactly what the scenario tests check.
"""

import copy
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from smt_sim.config import deep_merge, load_config
from smt_sim.sim import Simulator

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "config" / "scenarios"


# ---- actions ----------------------------------------------------------------
# Each action takes the simulator plus the parameters from the YAML entry.


def clog_nozzle(sim: Simulator, station: str, nozzle: int) -> None:
    sim.station(station).nozzles[nozzle].clogged = True


def set_clog_detection(sim: Simulator, station: str, consecutive_fails: int) -> None:
    """How many failed picks in a row the vision check tolerates before stopping."""
    sim.station(station).clog_detect_fails = consecutive_fails


def set_reel_qty(sim: Simulator, station: str, part: str, qty: int) -> None:
    """Leave only `qty` parts on a mounted reel (as if it had been running a long time)."""
    feeder = sim.station(station).feeders.get(part)
    if feeder is None or feeder.slot is None:
        raise ValueError(f"set_reel_qty: {part} is not mounted on {station}")
    feeder.qty = qty


def set_splice_miss_probability(sim: Simulator, station: str, probability: float) -> None:
    """Chance that a low-reel call is missed (then the reel runs empty)."""
    sim.station(station).splice_miss_probability = probability


def set_operator_response(sim: Simulator, median_s: float, sigma: float) -> None:
    sim.operator.response_median_s = median_s
    sim.operator.response_sigma = sigma


def accelerate_stencil_wear(sim: Simulator, station: str, factor: float) -> None:
    """Make every stencil (mounted, stored and future) wear `factor` times faster."""
    printer = sim.station(station)
    printer.stencil_cfg["wear_loss_pct_per_1k_cycles"] *= factor
    for stencil in printer.stencils.values():
        stencil.wear_loss_pct_per_1k_cycles *= factor


def set_speed(sim: Simulator, station: str, speed_pct: float) -> None:
    """Run a station at `speed_pct` of its rated speed from its next board."""
    sim.station(station).set_speed(speed_pct)


def force_down(sim: Simulator, station: str, reason: str, duration_s: float) -> None:
    sim.station(station).force_down(reason, duration_s)


ACTIONS: dict[str, Callable[..., None]] = {
    "clog_nozzle": clog_nozzle,
    "set_clog_detection": set_clog_detection,
    "set_reel_qty": set_reel_qty,
    "set_splice_miss_probability": set_splice_miss_probability,
    "set_operator_response": set_operator_response,
    "accelerate_stencil_wear": accelerate_stencil_wear,
    "set_speed": set_speed,
    "force_down": force_down,
}


# ---- loading ----------------------------------------------------------------


@dataclass
class Scenario:
    name: str
    description: str
    duration_h: float
    config: dict[str, Any]                 # full config with the overrides applied
    interventions: list[dict[str, Any]]
    expected_root_cause: dict[str, Any]

    def build_simulator(self, seed: int | None = None, with_interventions: bool = True) -> Simulator:
        """A fresh simulator for this scenario. Without interventions it is the baseline."""
        sim = Simulator(copy.deepcopy(self.config), seed=seed)
        if with_interventions:
            for item in self.interventions:
                params = {k: v for k, v in item.items() if k not in ("at_s", "action")}
                action = ACTIONS[item["action"]]
                label = f"{item['action']} {params}"
                # Default arguments freeze this loop's action/params inside the lambda.
                sim.schedule(float(item["at_s"]), label, lambda s, a=action, p=params: a(s, **p))
        return sim


def load_scenario(path_or_name: str | Path, base_config: dict[str, Any] | None = None) -> Scenario:
    """Load a scenario file, by path or by name from config/scenarios/."""
    path = Path(path_or_name)
    if not path.suffix:
        path = SCENARIO_DIR / f"{path_or_name}.yaml"
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    config = copy.deepcopy(base_config) if base_config is not None else load_config()
    deep_merge(config, raw.get("config_overrides", {}))

    interventions = raw.get("interventions", [])
    for item in interventions:
        if item["action"] not in ACTIONS:
            raise ValueError(f"Scenario {raw['name']}: unknown action {item['action']!r}")

    return Scenario(
        name=raw["name"],
        description=raw.get("description", "").strip(),
        duration_h=float(raw.get("duration_h", 8)),
        config=config,
        interventions=interventions,
        expected_root_cause=raw.get("expected_root_cause", {}),
    )

