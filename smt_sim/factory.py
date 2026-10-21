"""A factory: several SMT lines (Simulators) advanced together on one clock.

Each line is a complete, independent Simulator. The Factory only:
  - builds the lines from config/factory.yaml (base config + per-line overrides),
  - gives each line its own random stream derived from ONE factory seed,
  - steps all lines in lockstep, and
  - combines their tags and metrics.

Why derived seeds: if both lines used the same seed they would break down at
exactly the same moments. numpy's SeedSequence.spawn() turns one seed into
independent child seeds, so the whole factory is still reproducible from a
single number (same seed + same config = identical run, as for one line).

Not modeled yet: anything the lines share (an operator walking between them,
one order book dispatched across lines). Those would live here.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from smt_sim.config import apply_station_overrides, deep_merge, load_config
from smt_sim.metrics import diagnose, line_oee, quality_report
from smt_sim.sim import Simulator

DEFAULT_FACTORY_PATH = Path(__file__).resolve().parent.parent / "config" / "factory.yaml"


def line_seeds(factory_seed: int, line_names: list[str]) -> dict[str, int]:
    """One independent seed per line, derived from the factory seed (in line order)."""
    children = np.random.SeedSequence(factory_seed).spawn(len(line_names))
    return {name: int(child.generate_state(1)[0]) for name, child in zip(line_names, children)}


def build_line_config(line_cfg: dict[str, Any]) -> dict[str, Any]:
    """Base config plus this line's overrides."""
    config = load_config(line_cfg.get("base_config"))
    deep_merge(config, line_cfg.get("config_overrides", {}))
    apply_station_overrides(config, line_cfg.get("station_overrides", {}))
    return config


class Factory:
    def __init__(self, lines: dict[str, Simulator], descriptions: dict[str, str] | None = None) -> None:
        if not lines:
            raise ValueError("A factory needs at least one line")
        tick_sizes = {sim.dt_s for sim in lines.values()}
        if len(tick_sizes) != 1:
            raise ValueError(f"All lines must use the same tick size, got {tick_sizes}")
        self.lines = lines
        self.descriptions = descriptions or {}

    @classmethod
    def from_config(cls, path: str | Path | None = None, seed: int | None = None) -> "Factory":
        """Build every line in config/factory.yaml (or `path`)."""
        with open(path or DEFAULT_FACTORY_PATH, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        line_cfgs: dict[str, dict[str, Any]] = raw["lines"]
        seeds = line_seeds(raw["seed"] if seed is None else seed, list(line_cfgs))
        lines = {name: Simulator(build_line_config(cfg or {}), seed=seeds[name])
                 for name, cfg in line_cfgs.items()}
        descriptions = {name: ((cfg or {}).get("description") or "").strip() for name, cfg in line_cfgs.items()}
        return cls(lines, descriptions)

    @classmethod
    def single(cls, sim: Simulator, name: str = "line1") -> "Factory":
        """Wrap one simulator, so tools built for factories also work for one line."""
        return cls({name: sim})

    # ---- running ------------------------------------------------------------

    @property
    def dt_s(self) -> float:
        return next(iter(self.lines.values())).dt_s

    @property
    def time_s(self) -> float:
        return next(iter(self.lines.values())).time_s

    def step(self) -> None:
        for sim in self.lines.values():
            sim.step()

    def run(self, seconds: float) -> None:
        for _ in range(round(seconds / self.dt_s)):
            self.step()

    def orders_done(self) -> bool:
        return all(sim.orders_done() for sim in self.lines.values())

    def run_until_orders_done(self, max_s: float, check_every_s: float = 60.0) -> bool:
        """Run all lines in lockstep until every order on every line is done (or max_s)."""
        while not self.orders_done() and self.time_s < max_s:
            self.run(min(check_every_s, max_s - self.time_s))
        return self.orders_done()

    # ---- inspection ---------------------------------------------------------

    @property
    def boards_completed(self) -> int:
        return sum(sim.boards_completed for sim in self.lines.values())

    def get_tags(self) -> dict[str, dict[str, Any]]:
        """Every line's tags, keyed "line/entity" (e.g. "line2/pnp"). Still flat and primitive."""
        return {f"{line}/{entity}": values
                for line, sim in self.lines.items()
                for entity, values in sim.get_tags().items()}


# ---- factory-level metrics ----------------------------------------------------


@dataclass(frozen=True)
class LineSummary:
    oee: float
    first_pass_yield: float
    good_boards: int
    finished_boards: int
    bottleneck: str


def factory_summary(factory: Factory) -> tuple[dict[str, LineSummary], float, float]:
    """Per-line summary, plus factory OEE and factory first-pass yield.

    factory OEE = mean of the line OEEs (every line had the same time
                  available, so this is total ideal output / total time).
    factory FPY = all good boards / all finished boards, across lines.
    """
    per_line = {}
    for name, sim in factory.lines.items():
        q = quality_report(sim)
        per_line[name] = LineSummary(line_oee(sim).oee, q.first_pass_yield, q.good,
                                     q.boards_finished, diagnose(sim).bottleneck)
    oee = sum(s.oee for s in per_line.values()) / len(per_line)
    finished = sum(s.finished_boards for s in per_line.values())
    fpy = sum(s.good_boards for s in per_line.values()) / finished if finished else float("nan")
    return per_line, oee, fpy


def format_factory_summary(factory: Factory) -> str:
    per_line, oee, fpy = factory_summary(factory)
    lines = [f"Factory: {len(per_line)} lines   OEE {oee:.1%}   first-pass yield {fpy:.1%}   "
             f"good boards {sum(s.good_boards for s in per_line.values())}", ""]
    lines.append(f"{'line':8s} {'OEE':>7s} {'FPY':>7s} {'good':>6s}  bottleneck")
    for name, s in per_line.items():
        lines.append(f"{name:8s} {s.oee:7.1%} {s.first_pass_yield:7.1%} {s.good_boards:6d}  {s.bottleneck}")
    return "\n".join(lines)
