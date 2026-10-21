"""Customer orders and per-order tracking on a line.

An order asks for a number of GOOD boards of one product by a due time.
Because inspection rejects some boards, a line has to release more boards
than the order quantity. OrderTracker keeps the counts that decide how many:

    released   boards started for the order
    good       boards that reached the line exit
    rejected   boards pulled off the line by an inspection station
    outstanding = released - rejected   (good + still in process)

The production plan keeps releasing for an order until `outstanding` covers
the quantity (see ProductionPlan). The order is complete when `good`
reaches the quantity; that moment is its completion time.

The tracker is updated by callbacks: the line exit and the reject bins call
it when a board arrives (Buffer.on_arrival), the plan calls it on release.
"""

import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from smt_sim.board import Board


@dataclass(frozen=True)
class Order:
    id: str
    product: str
    quantity: int        # good boards required
    due_s: float         # due time, seconds from the start of the run
    weight: float = 1.0  # importance in weighted tardiness


def orders_from_config(items: list[dict[str, Any]]) -> list[Order]:
    """Orders from config dicts: {id, product, quantity, due_h, weight?}."""
    orders = [Order(str(o["id"]), o["product"], int(o["quantity"]), float(o["due_h"]) * 3600.0,
                    float(o.get("weight", 1.0))) for o in items]
    ids = [o.id for o in orders]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate order ids in {ids}")
    for o in orders:
        if o.quantity < 1:
            raise ValueError(f"Order {o.id}: quantity must be >= 1")
    return orders


def load_orders(path: str | Path) -> list[Order]:
    """Read an order file (see config/orders/)."""
    with open(path, encoding="utf-8") as f:
        return orders_from_config(yaml.safe_load(f)["orders"])


def orders_to_config(orders: list[Order]) -> list[dict[str, Any]]:
    """The inverse of orders_from_config, for writing orders into a line config."""
    return [{"id": o.id, "product": o.product, "quantity": o.quantity, "due_h": o.due_s / 3600.0,
             "weight": o.weight} for o in orders]


class OrderTracker:
    """Released / good / rejected counts and completion time per order, on one line."""

    def __init__(self, orders: list[Order]) -> None:
        self.orders: dict[str, Order] = {o.id: o for o in orders}
        self.released: Counter[str] = Counter()
        self.good: Counter[str] = Counter()
        self.rejected: Counter[str] = Counter()
        self.completed_s: dict[str, float] = {}   # order id -> time its last needed good board left

    # ---- callbacks ------------------------------------------------------------

    def on_released(self, board: Board) -> None:
        if board.order_id is not None:
            self.released[board.order_id] += 1

    def on_good(self, board: Board) -> None:
        order_id = board.order_id
        if order_id is None:
            return
        self.good[order_id] += 1
        if self.good[order_id] == self.orders[order_id].quantity:
            self.completed_s[order_id] = max(board.time_out_s.values())

    def on_reject(self, board: Board) -> None:
        if board.order_id is not None:
            self.rejected[board.order_id] += 1

    # ---- questions ------------------------------------------------------------

    def outstanding(self, order_id: str) -> int:
        """Boards that are good or may still become good."""
        return self.released[order_id] - self.rejected[order_id]

    def in_process(self, order_id: str) -> int:
        """Released boards that have not yet passed or failed inspection."""
        return self.released[order_id] - self.good[order_id] - self.rejected[order_id]

    def shortfall(self, order_id: str, planned_yield: float = 1.0) -> int:
        """How many more boards to release so the order is expected to be covered.

        Boards still in process count as `planned_yield` good boards each
        (a planned scrap allowance). With planned_yield = 1 every board in
        process is assumed good, so rejects found late need a top-up.
        """
        expected_good = self.good[order_id] + planned_yield * self.in_process(order_id)
        missing = self.orders[order_id].quantity - expected_good
        return max(0, math.ceil(missing / planned_yield - 1e-9))

    def all_done(self) -> bool:
        return len(self.completed_s) == len(self.orders)
