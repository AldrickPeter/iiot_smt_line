"""Shared helpers for the tests (pytest puts this folder on sys.path)."""

from typing import Any

from smt_sim import Simulator
from smt_sim.config import load_config, make_deterministic


def single_product(config: dict[str, Any], product: str = "CTRL-BOARD-A") -> dict[str, Any]:
    """Make the line run one product forever (no changeovers)."""
    config["production_plan"] = {"jobs": [{"product": product}]}
    return config


def default_single_product() -> dict[str, Any]:
    """The default line, randomness on, making only product A."""
    return single_product(load_config())


def deterministic_sim() -> Simulator:
    """The default line making only product A, with all randomness off."""
    return Simulator(single_product(make_deterministic(load_config())))


def tiny_config(cycle_times_s: dict[str, float], buffer_capacity: int = 1) -> dict[str, Any]:
    """A line of fixed-cycle stations, in the given order, making one empty product."""
    return {
        "sim": {"tick_s": 0.5, "seed": 1},
        "products": {"TEST": {"bom": {}}},
        "line": {
            "buffer_capacity": buffer_capacity,
            "stations": [
                {"name": name, "type": "generic", "cycle_time_s": c} for name, c in cycle_times_s.items()
            ],
        },
    }
