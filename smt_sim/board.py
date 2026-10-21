"""The board record that travels down the line, and the plan that releases boards.

A board carries its product spec (BOM, reflow profile, ...) so every station
can see what it is working on. `measurements` holds what each station
physically did to the board plus the cause values behind it.

Quality fields (see defects.py): ground truth (`true_defects`) is kept apart
from what inspection observed (`detected_defects`, `false_calls`).
"""

from dataclasses import dataclass, field
from typing import Any

from smt_sim.defects import DetectedDefect, FalseCall, TrueDefect


@dataclass
class Board:
    serial: str
    product: str
    spec: dict[str, Any]   # this product's config (shared, not copied)
    time_in_s: dict[str, float] = field(default_factory=dict)   # station name -> sim time entered
    time_out_s: dict[str, float] = field(default_factory=dict)  # station name -> sim time left
    measurements: dict[str, dict[str, Any]] = field(default_factory=dict)  # station name -> values
    true_defects: list[TrueDefect] = field(default_factory=list)
    detected_defects: list[DetectedDefect] = field(default_factory=list)
    false_calls: list[FalseCall] = field(default_factory=list)
    disposition: str | None = None     # Disposition.*, set when the board leaves the line
    order_id: str | None = None        # customer order this board is made for, if any
    rejected_at: str | None = None     # inspection station that pulled it off the line

    @property
    def bom(self) -> dict[str, int]:
        """Part number -> quantity per board."""
        return self.spec["bom"]

    @property
    def component_count(self) -> int:
        return sum(self.bom.values())


class ProductionPlan:
    """Releases bare boards into the line, job by job.

    plan config:
        jobs:   [{product: NAME, quantity: N, order: ID?}, ...]   quantity omitted = run forever
        repeat: true -> start again at the first job after the last one

    It has the same `is_empty()` / `peek()` / `pop()` interface as a Buffer,
    so the printer treats it like any other input. When a non-repeating plan
    is finished it reports empty and the printer goes STARVED.

    Jobs for a customer order (`order: ID`) also cover rejects, using the
    line's OrderTracker (set by the Simulator):
      - while the order's last job is running, it keeps releasing until the
        boards expected to turn out good cover the order quantity. Boards
        still in process count as `planned_yield` good boards each (job key,
        default 1.0): a planned scrap allowance, so a few extra boards are
        released up front instead of remaking them later;
      - rejects found after that lead to a TOP-UP job for the shortfall,
        appended after the last planned job (it usually costs a changeover).
    """

    def __init__(self, plan_cfg: dict[str, Any], products: dict[str, dict[str, Any]]) -> None:
        self.jobs: list[dict[str, Any]] = list(plan_cfg["jobs"])   # copy: top-ups get appended
        self.repeat: bool = plan_cfg.get("repeat", False)
        self.products = products
        for job in self.jobs:
            if job["product"] not in products:
                raise ValueError(f"Production plan names unknown product {job['product']!r}")
            if job.get("quantity") is not None and job["quantity"] < 1:
                raise ValueError("Job quantity must be >= 1 (or omitted for 'forever')")
            if not 0 < job.get("planned_yield", 1.0) <= 1:
                raise ValueError("planned_yield must be in (0, 1]")
        # Planned yield per order, for reject cover and top-ups.
        self._planned_yield = {j["order"]: j.get("planned_yield", 1.0) for j in self.jobs if "order" in j}

        self.tracker = None        # OrderTracker, set by the Simulator when the line has orders
        self.top_up_count = 0
        self.boards_created = 0
        self._job_index = 0
        self._released_in_job = 0
        self._next: Board | None = None   # created on first peek, handed out on pop

    def is_empty(self) -> bool:
        return self._upcoming() is None

    def peek(self) -> Board:
        board = self._upcoming()
        if board is None:
            raise IndexError("Production plan is finished")
        return board

    def pop(self) -> Board:
        board = self.peek()
        self._next = None
        return board

    def _upcoming(self) -> Board | None:
        if self._next is None:
            self._next = self._make_next_board()
        return self._next

    def _make_next_board(self) -> Board | None:
        while True:
            if self._job_index >= len(self.jobs):
                top_up = self._top_up_job()
                if top_up is not None:
                    self.jobs.append(top_up)
                elif self.repeat:
                    self._job_index = 0
                else:
                    return None
            job = self.jobs[self._job_index]
            if self._job_wants_another_board(job):
                self._released_in_job += 1
                self.boards_created += 1
                product = job["product"]
                board = Board(f"SN{self.boards_created:06d}", product, self.products[product],
                              order_id=job.get("order"))
                if self.tracker is not None:
                    self.tracker.on_released(board)
                return board
            self._job_index += 1
            self._released_in_job = 0

    def _job_wants_another_board(self, job: dict[str, Any]) -> bool:
        quantity = job.get("quantity")
        if quantity is None or self._released_in_job < quantity:
            return True
        # Planned quantity released: keep replacing known rejects while this
        # is the order's last job (top-ups handle rejects found later).
        order = job.get("order")
        if order is None or self.tracker is None:
            return False
        later_jobs_for_order = any(j.get("order") == order for j in self.jobs[self._job_index + 1:])
        return not later_jobs_for_order and self.tracker.shortfall(order, self._planned_yield[order]) > 0

    def _top_up_job(self) -> dict[str, Any] | None:
        """After the last job: a job for the first order still short of boards."""
        if self.tracker is None:
            return None
        for order_id in self.tracker.orders:
            planned_yield = self._planned_yield.get(order_id, 1.0)
            missing = self.tracker.shortfall(order_id, planned_yield)
            if missing > 0:
                self.top_up_count += 1
                order = self.tracker.orders[order_id]
                return {"product": order.product, "quantity": missing, "order": order_id,
                        "planned_yield": planned_yield, "top_up": True}
        return None
