"""Reflow oven, modeled as a conveyor pipeline rather than a single-board server.

Many boards are inside the oven at once. Each one needs `transit_time_s` to
cross it. Boards must be spaced at least `board_pitch_mm` apart, so:

    capacity          = conveyor_length_mm / board_pitch_mm        (boards)
    entry_interval_s  = transit_time_s / capacity                  (seconds)

The entry interval is the oven's effective cycle time: it can accept one new
board every `entry_interval_s`, no matter how long each board stays inside.

Temperature: the peak zone drifts slowly around its setpoint (see
ZoneDrift). Each board's peak temperature = product setpoint + drift at the
moment it passes + a little board-to-board noise. The deviation from the
setpoint drives solder defects (origin: reflow):
  - COLD_JOINT: more likely the further the peak is *below* setpoint - margin
  - VOID:       small base rate, more likely when the peak is *above* setpoint + margin
  - TOMBSTONE:  per small chip (e.g. 0402), more likely with any deviation
The board stores its peak and deviation, so each defect is traceable.

Changeover: a new product needs a new temperature profile. The oven first
lets the old product's boards run out, then goes SETUP while the zones
settle at the new temperatures, then accepts the new product.

Simplification: if the board at the exit cannot leave (output buffer full), the
whole conveyor halts. A real oven cannot stop its belt without overbaking the
boards; that consequence is not modeled.
"""

import math
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

from smt_sim.board import Board
from smt_sim.defects import DefectType, TrueDefect
from smt_sim.events import StationState
from smt_sim.station import Station


@dataclass
class _BoardInOven:
    board: Board
    remaining_s: float   # time left until it reaches the exit


class ZoneDrift:
    """Slow random drift of the peak-zone temperature around its setpoint (°C).

    An Ornstein-Uhlenbeck process: a random walk that is pulled back toward
    zero. `sd_c` is how far it typically wanders, `time_constant_s` how slowly.
    The update below is the exact solution over one step, so the statistics
    don't depend on the tick size.
    """

    def __init__(self, cfg: dict[str, Any], rng: np.random.Generator) -> None:
        self.sd_c = float(cfg["drift_sd_c"])
        self.time_constant_s = float(cfg["drift_time_constant_s"])
        self.offset_c = 0.0
        self.rng = rng

    def advance(self, dt_s: float) -> None:
        keep = math.exp(-dt_s / self.time_constant_s)
        self.offset_c = self.offset_c * keep + self.sd_c * math.sqrt(1 - keep * keep) * self.rng.normal()


class ReflowOven(Station):
    is_pipeline = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.transit_time_s = float(self.params["transit_time_s"])
        self.capacity = int(self.params["conveyor_length_mm"] // self.params["board_pitch_mm"])
        self.entry_interval_s = self.transit_time_s / self.capacity
        self.conveyor: deque[_BoardInOven] = deque()   # leftmost = closest to exit
        self._since_last_entry_s = self.entry_interval_s  # first board may enter at once
        # Sim time each board entered, like a board-in sensor at the entrance.
        # Used to judge the oven by its entrance for bottleneck detection.
        self.entry_times_s: list[float] = []

        # Temperature and solder-defect models are optional (off in deterministic tests).
        temp_cfg = self.params.get("temperature")
        self.drift = ZoneDrift(temp_cfg, self.rng) if temp_cfg else None
        self.board_temp_sd_c = float(temp_cfg["board_sd_c"]) if temp_cfg else 0.0
        self.solder_cfg: dict[str, Any] | None = self.params.get("solder_defects")

    def cycle_time_for(self, board: Board) -> float:
        return self.entry_interval_s

    def set_speed(self, speed_pct: float) -> None:
        """The belt speed is set by the temperature profile, so it can't be slowed."""
        if speed_pct != 100:
            raise ValueError(f"{self.name}: oven speed is fixed by its profile (transit_time_s)")
        super().set_speed(speed_pct)

    def boards_inside(self) -> list[Board]:
        return [item.board for item in self.conveyor]

    def step(self, tick: int, time_s: float, dt_s: float) -> None:
        # The zones drift whatever the oven is doing, even while it is stopped.
        if self.drift is not None:
            self.drift.advance(dt_s)
        super().step(tick, time_s, dt_s)

    def extra_tags(self) -> dict[str, Any]:
        return {"peak_zone_offset_c": round(self.drift.offset_c, 2)} if self.drift else {}

    def _process(self, dt_s: float) -> None:
        # 1. Board at the exit leaves, or the conveyor halts.
        if self.conveyor and self.conveyor[0].remaining_s <= 0:
            if not self.output_buffer.has_space():
                self._set_state(StationState.BLOCKED)
                return
            board = self.conveyor.popleft().board
            self._solder(board)
            self._finish_board(board)

        # 2. Load a new board if there is room and enough spacing.
        next_board = None if self.input_buffer.is_empty() else self.input_buffer.peek()
        if next_board is not None and next_board.product != self.current_product:
            if self.current_product is None or not self.conveyor:
                if self._change_over_if_needed(next_board, dt_s):
                    return
            else:
                next_board = None   # new product waits until the old one has left the oven
        can_load = (
            next_board is not None
            and len(self.conveyor) < self.capacity
            and self._since_last_entry_s >= self.entry_interval_s
        )
        if can_load:
            board = self.input_buffer.pop()
            board.time_in_s[self.name] = self._time_s
            self.conveyor.append(_BoardInOven(board, self.transit_time_s))
            self._since_last_entry_s = 0.0
            self.entry_times_s.append(self._time_s)

        # 3. Move the belt.
        self._since_last_entry_s += dt_s
        if not self.conveyor:
            self._set_state(StationState.STARVED)
            return
        for item in self.conveyor:
            item.remaining_s -= dt_s
        self._set_state(StationState.RUNNING)

    def _solder(self, board: Board) -> None:
        """Record the board's peak temperature and the solder defects it causes."""
        if self.drift is None:
            return
        setpoint_c = float(board.spec["reflow_peak_c"])
        peak_c = setpoint_c + self.drift.offset_c + self.rng.normal(0.0, self.board_temp_sd_c)
        deviation_c = peak_c - setpoint_c
        board.measurements[self.name] = {"peak_temp_c": peak_c, "peak_deviation_c": deviation_c}

        cfg = self.solder_cfg
        if cfg is None:
            return
        detail = f"peak {peak_c:.1f} C ({deviation_c:+.1f})"

        too_cold_c = max(0.0, -deviation_c - cfg["cold_margin_c"])
        if self.rng.random() < min(1.0, cfg["cold_joint_per_c"] * too_cold_c):
            board.true_defects.append(TrueDefect(DefectType.COLD_JOINT, self.name, detail))

        too_hot_c = max(0.0, deviation_c - cfg["void_margin_c"])
        if self.rng.random() < min(1.0, cfg["void_base_prob"] + cfg["void_per_c"] * too_hot_c):
            board.true_defects.append(TrueDefect(DefectType.VOID, self.name, detail))

        small_chips = sum(board.bom.get(part, 0) for part in cfg["tombstone_parts"])
        p_chip = cfg["tombstone_base_prob"] + cfg["tombstone_per_c"] * abs(deviation_c)
        n_tombstones = int(self.rng.binomial(small_chips, min(1.0, p_chip))) if small_chips else 0
        if n_tombstones:
            board.true_defects.append(TrueDefect(DefectType.TOMBSTONE, self.name, f"{n_tombstones} chips, {detail}"))
