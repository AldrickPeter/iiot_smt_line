"""Base station: a single-board server with a small state machine.

Each tick a station does at most these things, in order:
  1. If it is stopped (DOWN, STARVED_MATERIAL, ...), count down the stop
     time and do nothing else.
  2. If it holds a finished board, push it to the output buffer
     (or go BLOCKED if the buffer is full).
  3. Before taking a new board, give the station a chance to stop
     (`_before_next_board`: e.g. stencil wipe, paste low).
  4. If it is empty, pull the next board from the input buffer
     (or go STARVED if there is none). If that board is a different product
     than the station is set up for, change over first (SETUP).
  5. Prepare the board (`_prepare_board`: e.g. print paste, pick parts).
     This may stop the station part-way (e.g. a reel runs empty); the board
     stays in the station and preparation resumes after the stop.
  6. Work on the current board for one tick (RUNNING).
  7. If it ran this tick, age its failure modes; a failure takes it DOWN
     from the next tick for a random repair time.

Steps 3 and 5 are hooks that do nothing here; the printer and pick-and-place
override them. `_setup_for` is the changeover hook (swap stencil, feeders...).

Changeovers happen per station, when the first board of the new product
reaches it. So a product change ripples down the line: the printer changes
over first while downstream stations finish the old product.

Timing note: a board's cycle starts on the tick it is pulled and finishes once
`dt` has been subtracted enough times, so cycle times are effectively rounded
up to a whole number of ticks. With the default 0.5 s tick this error is small.
"""

from collections import Counter
from typing import Any, Protocol

import numpy as np

from smt_sim.board import Board
from smt_sim.components import Operator
from smt_sim.events import Event, EventLog, ReasonCode, StationState
from smt_sim.failures import FailureMode


class BoardInput(Protocol):
    """Anything a station can pull boards from (a Buffer or the BoardSource)."""

    def is_empty(self) -> bool: ...
    def peek(self) -> Board: ...
    def pop(self) -> Board: ...


class Station:
    # True for stations that hold many boards at once (the reflow conveyor).
    # Metrics treat these differently; see metrics.py.
    is_pipeline = False

    def __init__(
        self,
        name: str,
        params: dict[str, Any],
        input_buffer: BoardInput,
        output_buffer: Any,          # a Buffer
        event_log: EventLog,
        rng: np.random.Generator,
        operator: Operator | None = None,
    ) -> None:
        self.name = name
        self.params = params
        self.input_buffer = input_buffer
        self.output_buffer = output_buffer
        self.event_log = event_log
        self.rng = rng   # the run's single shared generator
        self.operator = operator
        self.failure_modes = [FailureMode(f, rng) for f in params.get("failures", [])]
        self.speed_pct = 100.0
        self.set_speed(params.get("speed_pct", 100.0))   # validates the configured value

        self.state = StationState.STARVED
        self.reason_code: str | None = None
        self.part_count = 0          # boards completed (passed on or rejected)
        self.reject_count = 0        # boards pulled off the line (inspection stations)
        self.parts_by_product: Counter[str] = Counter()
        self.current_product: str | None = None   # what the station is set up for

        self.current_board: Board | None = None
        self.remaining_s = 0.0       # work left on current_board
        self._board_prepared = False

        # An active stop: which state to show, why, and for how much longer.
        self._stop_state = StationState.DOWN
        self._stop_reason: str | None = None
        self._stop_remaining_s = 0.0

        # Clock of the tick being processed, so _set_state can timestamp events.
        self._tick = 0
        self._time_s = 0.0

        self.event_log.record(Event(0, 0.0, name, None, self.state, None))

    # ---- public API -------------------------------------------------------

    def cycle_time_for(self, board: Board) -> float:
        """Ideal (rated, 100 % speed) processing time for one board, in seconds."""
        return float(self.params["cycle_time_s"])

    @property
    def speed_fraction(self) -> float:
        return self.speed_pct / 100.0

    def set_speed(self, speed_pct: float) -> None:
        """Run the machine at `speed_pct` of its rated speed (0 < speed <= 100).

        Takes effect from the next board; a board in progress keeps its time.
        OEE still measures against the rated (100 %) cycle, so running slower
        shows up as a performance (speed) loss, as it would in a real plant.
        """
        if not 0 < speed_pct <= 100:
            raise ValueError(f"{self.name}: speed_pct must be in (0, 100], got {speed_pct}")
        self.speed_pct = float(speed_pct)

    def force_down(self, reason_code: str, duration_s: float) -> None:
        """Stop the station (DOWN) from the next tick. Used by failures, tests
        and scenario hooks. A board being worked on stays and resumes afterwards.
        """
        self._stop_state = StationState.DOWN
        self._stop_reason = reason_code
        self._stop_remaining_s = duration_s

    def _output_for(self, board: Board) -> Any:
        """Where a finished board goes. Inspection stations divert rejects."""
        return self.output_buffer

    def boards_inside(self) -> list[Board]:
        """Boards currently held by this station."""
        return [self.current_board] if self.current_board is not None else []

    def extra_tags(self) -> dict[str, Any]:
        """Station-specific tags for Simulator.get_tags(). Flat and primitive."""
        return {}

    def step(self, tick: int, time_s: float, dt_s: float) -> None:
        """Advance this station by one tick."""
        self._tick, self._time_s = tick, time_s

        if self._stop_remaining_s > 0:
            self._count_down_stop(dt_s)
            return

        self._process(dt_s)
        if self.state == StationState.RUNNING:
            self._age_failure_modes(dt_s)

    # ---- hooks for subclasses ---------------------------------------------

    def _before_next_board(self, dt_s: float) -> bool:
        """Called before pulling a board. Return True if a stop was started."""
        return False

    def _setup_for(self, board: Board) -> float:
        """Physically change over to `board`'s product; return how long it takes (s).

        Base station: a fixed time from config (`changeover.time_s`), e.g. a
        program load or fixture swap. Subclasses override to swap stencils,
        feeders, etc.
        """
        cfg = self.params.get("changeover")
        return float(cfg["time_s"]) if cfg else 0.0

    def _prepare_board(self, board: Board, dt_s: float) -> bool:
        """Do the station's per-board work. Return False if a stop was started
        and preparation must continue after it."""
        return True

    # ---- internals --------------------------------------------------------

    def _process(self, dt_s: float) -> None:
        """Single-board server logic: push finished, pull next, prepare, work."""
        # Hand off a finished board.
        if self.current_board is not None and self.remaining_s <= 0:
            destination = self._output_for(self.current_board)
            if not destination.has_space():
                self._set_state(StationState.BLOCKED)
                return
            self._finish_board(self.current_board, destination)
            self.current_board = None

        # Pull the next board.
        if self.current_board is None:
            if self._before_next_board(dt_s):
                return
            if self.input_buffer.is_empty():
                self._set_state(StationState.STARVED)
                return
            if self._change_over_if_needed(self.input_buffer.peek(), dt_s):
                return
            self.current_board = self.input_buffer.pop()
            self.current_board.time_in_s[self.name] = self._time_s
            self.remaining_s = self._sample_cycle_time(self.current_board)
            self._board_prepared = False

        # Station-specific per-board work (may stop part-way).
        if not self._board_prepared:
            if not self._prepare_board(self.current_board, dt_s):
                return
            self._board_prepared = True

        # Work on it.
        self._set_state(StationState.RUNNING)
        self.remaining_s -= dt_s

    def _change_over_if_needed(self, next_board: Board, dt_s: float) -> bool:
        """Set up for the next board's product. Returns True if that needs a SETUP stop.

        The line starts already set up for the first product it sees, so the
        very first setup is done instantly.
        """
        if next_board.product == self.current_product:
            return False
        first_setup = self.current_product is None
        self.current_product = next_board.product
        duration_s = self._setup_for(next_board)
        if first_setup or duration_s <= 0:
            return False
        reason = self.params.get("changeover", {}).get("reason", ReasonCode.CHANGEOVER)
        self._stop_now(StationState.SETUP, reason, duration_s, dt_s)
        return True

    def _stop_now(self, state: StationState, reason_code: str, duration_s: float, dt_s: float) -> None:
        """Start a stop from inside _process: it already counts this tick."""
        self._stop_state = state
        self._stop_reason = reason_code
        self._stop_remaining_s = duration_s
        self._count_down_stop(dt_s)

    def _count_down_stop(self, dt_s: float) -> None:
        self._set_state(self._stop_state, self._stop_reason)
        self._stop_remaining_s -= dt_s

    def _sample_cycle_time(self, board: Board) -> float:
        """Actual time for this board: ideal at the set speed, plus normal jitter.

        Clipped at that time, because it is the fastest the machine can go at
        this speed; disturbances only slow it down.
        """
        nominal_s = self.cycle_time_for(board) / self.speed_fraction
        sd_s = self.params.get("cycle_time_sd_s", 0.0)
        if sd_s <= 0:
            return nominal_s
        return max(nominal_s, nominal_s + self.rng.normal(0.0, sd_s))

    def _age_failure_modes(self, dt_s: float) -> None:
        for mode in self.failure_modes:
            if mode.advance(dt_s):
                self.force_down(mode.reason_code, mode.sample_repair_s())
                return   # one failure at a time

    def _finish_board(self, board: Board, destination: Any = None) -> None:
        board.time_out_s[self.name] = self._time_s
        if destination is None:   # not `or`: an empty Buffer has len 0 and counts as False
            destination = self.output_buffer
        destination.push(board)
        self.part_count += 1
        self.parts_by_product[board.product] += 1

    def _set_state(self, new_state: StationState, reason_code: str | None = None) -> None:
        """Change state and log it. Repeated calls with the same state are ignored."""
        if new_state == self.state and reason_code == self.reason_code:
            return
        self.event_log.record(
            Event(self._tick, self._time_s, self.name, self.state, new_state, reason_code)
        )
        self.state = new_state
        self.reason_code = reason_code
