"""Simulator: builds the line from config and runs the tick loop."""

import bisect
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from smt_sim.board import Board, ProductionPlan
from smt_sim.buffer import Buffer, LineExit
from smt_sim.components import Operator
from smt_sim.config import load_config
from smt_sim.events import EventLog
from smt_sim.orders import OrderTracker, orders_from_config
from smt_sim.station import Station
from smt_sim.stations.inspection import InspectionStation, SolderPasteInspection
from smt_sim.stations.pnp import PickAndPlace
from smt_sim.stations.printer import StencilPrinter
from smt_sim.stations.reflow import ReflowOven

# Station `type` in the config -> class. Unknown types use the base Station
# (fixed cycle time, no other behavior).
STATION_CLASSES: dict[str, type[Station]] = {
    "printer": StencilPrinter,
    "spi": SolderPasteInspection,
    "pnp": PickAndPlace,
    "reflow": ReflowOven,
    "aoi": InspectionStation,
    "ict": InspectionStation,
}


@dataclass(order=True)
class ScheduledIntervention:
    """Something a scenario (or test) will do to the line at a given sim time."""

    at_s: float
    seq: int                                   # tie-breaker: same time -> scheduling order
    label: str = field(compare=False)
    action: Callable[["Simulator"], None] = field(compare=False)


class Simulator:
    """A six-station SMT line advanced in fixed time steps (ticks).

    Layout: source -> st1 -> buf -> st2 -> buf -> ... -> st6 -> sink
    """

    def __init__(self, config: dict[str, Any] | str | Path | None = None, seed: int | None = None) -> None:
        if config is None or isinstance(config, (str, Path)):
            config = load_config(config)
        self.config = config

        self.dt_s: float = float(config["sim"]["tick_s"])
        self.seed: int = config["sim"]["seed"] if seed is None else seed
        self.rng = np.random.default_rng(self.seed)   # the ONE generator for this run

        self.tick = 0
        self.time_s = 0.0
        self.event_log = EventLog()
        self._scheduled: list[ScheduledIntervention] = []   # kept sorted by time
        self._schedule_count = 0
        self.intervention_log: list[tuple[float, str]] = []   # (sim time, label) of applied ones

        self.products: dict[str, dict[str, Any]] = config["products"]
        # Without a plan, run the first product forever.
        first_product = next(iter(self.products))
        plan_cfg = config.get("production_plan") or {"jobs": [{"product": first_product}]}
        self.source = ProductionPlan(plan_cfg, self.products)
        self.sink = LineExit()   # good boards collect here

        self.stations: list[Station] = []
        self.buffers: list[Buffer] = []
        self._build_line(config["line"])
        self._connect_orders(config.get("orders", []))

    def _build_line(self, line_cfg: dict[str, Any]) -> None:
        operator = Operator(line_cfg["operator"], self.rng) if "operator" in line_cfg else None
        self.operator = operator   # one operator shared by all stations
        station_cfgs = line_cfg["stations"]
        upstream: Any = self.source
        for i, st_cfg in enumerate(station_cfgs):
            is_last = i == len(station_cfgs) - 1
            if is_last:
                downstream = self.sink
            else:
                next_name = station_cfgs[i + 1]["name"]
                downstream = Buffer(f"{st_cfg['name']}->{next_name}", line_cfg["buffer_capacity"])
                self.buffers.append(downstream)

            cls = STATION_CLASSES.get(st_cfg["type"], Station)
            station = cls(st_cfg["name"], st_cfg, upstream, downstream, self.event_log, self.rng, operator)
            self.stations.append(station)
            upstream = downstream

    def _connect_orders(self, order_cfgs: list[dict[str, Any]]) -> None:
        """If the config lists customer orders, track them on this line."""
        self.orders = orders_from_config(order_cfgs)
        self.order_tracker: OrderTracker | None = None
        if not self.orders:
            return
        tracker = OrderTracker(self.orders)
        for job in self.source.jobs:
            order_id = job.get("order")
            if order_id is None:
                continue
            if order_id not in tracker.orders:
                raise ValueError(f"Job refers to unknown order {order_id!r}")
            if tracker.orders[order_id].product != job["product"]:
                raise ValueError(f"Job for order {order_id} has the wrong product {job['product']!r}")
        self.order_tracker = tracker
        self.source.tracker = tracker
        self.sink.on_arrival = tracker.on_good
        for st in self.stations:
            rejects = getattr(st, "rejects", None)
            if rejects is not None:
                rejects.on_arrival = tracker.on_reject

    # ---- running ----------------------------------------------------------

    def step(self) -> None:
        """Advance the whole line by one tick.

        Stations update downstream -> upstream. That way, when a station pulls
        a board from its input buffer, the station before it sees the freed
        slot in the same tick instead of waiting an extra tick.
        """
        self._apply_due_interventions()
        for station in reversed(self.stations):
            station.step(self.tick, self.time_s, self.dt_s)
        self.tick += 1
        self.time_s = self.tick * self.dt_s   # multiply, don't accumulate, to avoid float drift

    def schedule(self, at_s: float, label: str, action: Callable[["Simulator"], None]) -> None:
        """Run `action(sim)` at the start of the first tick at or after `at_s`."""
        self._schedule_count += 1
        bisect.insort(self._scheduled, ScheduledIntervention(at_s, self._schedule_count, label, action))

    def _apply_due_interventions(self) -> None:
        while self._scheduled and self._scheduled[0].at_s <= self.time_s:
            item = self._scheduled.pop(0)
            item.action(self)
            self.intervention_log.append((self.time_s, item.label))

    def orders_done(self) -> bool:
        """True when every order on this line has its good boards (or there are no orders)."""
        return self.order_tracker is None or self.order_tracker.all_done()

    def run_until_orders_done(self, max_s: float, check_every_s: float = 60.0) -> bool:
        """Run until all orders are complete or `max_s` of sim time has passed.

        Returns True if the orders were finished. Checks every `check_every_s`
        (the check is cheap, but there is no need to do it every tick).
        """
        while not self.orders_done() and self.time_s < max_s:
            self.run(min(check_every_s, max_s - self.time_s))
        return self.orders_done()

    def run(self, seconds: float) -> None:
        """Run headless for `seconds` of simulated time."""
        n_ticks = round(seconds / self.dt_s)
        for _ in range(n_ticks):
            self.step()

    # ---- inspection -------------------------------------------------------

    def station(self, name: str) -> Station:
        for st in self.stations:
            if st.name == name:
                return st
        raise KeyError(name)

    def ideal_cycle_time_s(self, station_name: str, product: str) -> float:
        """Ideal cycle time of a station for one product.

        Uses a throwaway reference board so stations whose cycle time depends
        on the board (pick-and-place: component count) answer correctly.
        """
        reference = Board("REFERENCE", product, self.products[product])
        return self.station(station_name).cycle_time_for(reference)

    def bottleneck_ideal_cycle_s(self, product: str) -> float:
        """The slowest station's ideal cycle for this product = the line's ideal pace."""
        return max(self.ideal_cycle_time_s(st.name, product) for st in self.stations)

    def all_boards(self) -> list[Board]:
        """Every board started so far: finished, waiting in buffers, or inside a station."""
        boards = self.sink.boards()
        for buf in self.buffers:
            boards += buf.boards()
        for st in self.stations:
            boards += st.boards_inside()
        return boards + self.rejected_boards()

    def rejected_boards(self) -> list[Board]:
        """Boards pulled off the line by an inspection station."""
        boards: list[Board] = []
        for st in self.stations:
            rejects = getattr(st, "rejects", None)
            if rejects is not None:
                boards += rejects.boards()
        return boards

    @property
    def boards_completed(self) -> int:
        return len(self.sink)

    def get_tags(self) -> dict[str, dict[str, Any]]:
        """Flat, primitive-typed snapshot per station and buffer.

        Shaped so it can later be mapped 1:1 onto OPC UA tags.
        """
        tags: dict[str, dict[str, Any]] = {}
        for st in self.stations:
            tags[st.name] = {
                "state": st.state.value,
                "reason_code": st.reason_code or "",
                "part_count": st.part_count,
                "reject_count": st.reject_count,
                "speed_pct": st.speed_pct,     # a setpoint: later a writable OPC UA tag
                **st.extra_tags(),
            }
        for buf in self.buffers:
            tags[buf.name] = {"level": len(buf), "capacity": buf.capacity}
        return tags
