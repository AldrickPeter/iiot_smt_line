"""Pick-and-place machine: feeders, reels, splicing, nozzles.

Cycle time scales with the board's component count. On top of that, each
board's parts are picked one by one:

  - Parts come from the feeder holding that part number. Every pick, good or
    failed, uses up one part from the reel.
  - Nozzles take turns (round robin). A worn or clogged nozzle places parts
    with a larger offset (µm) and fails more picks. A failed pick is retried,
    which costs one extra component time.
  - The machine's vision sees failed picks. After K failures in a row on the
    same nozzle it stops: DOWN / NOZZLE_CLOG, and the nozzle is cleaned.
    Until then, a clogged nozzle keeps placing parts badly: those boards carry
    the large offsets, which become defects in milestone 5.

Reels and the operator:
  - When a reel drops below its low-reel warning (enough parts for N more
    boards) the operator is called. With some probability the call is missed.
  - If the operator arrives before the reel is empty, they splice a new reel
    on: no stop.
  - If the reel runs empty first: STARVED_MATERIAL / FEEDER_EMPTY until the
    operator arrives, plus the time to load a new reel.

True defects: a part placed further from its pad than its `tolerance_um`
is a MISPLACEMENT (origin: pnp), with the nozzle and offset as detail, so a
clogged or worn nozzle is traceable.

Head speed (`speed_pct`, see Station.set_speed): running below rated speed
makes every board take longer, but places more accurately and drops fewer
parts. Both effects follow a power law of the speed fraction s = speed/100:

    offset spread  x s ** offset_sd_exponent    (e.g. 0.75: at 80 % -> 0.85x)
    pick failures  x s ** pick_fail_exponent    (e.g. 1.0:  at 80 % -> 0.80x)

A power law keeps both factors positive at any speed and equal to 1 at
100 %, with one tunable number each (from `speed_effects` in the config).

Changeover: the machine has a fixed number of feeder slots. A new product's
parts that are not mounted yet are swapped in, replacing feeders the new
product does not use. Changeover time = fixed part + time per feeder swapped,
so two products that share many parts change over quickly.

Simplification: all picks for a board happen at the moment it starts, then
the cycle time runs. If a reel empties part-way, the board waits in the
machine and the remaining picks happen after the reel change.
"""

from typing import Any

from smt_sim.board import Board
from smt_sim.components import Feeder, Nozzle
from smt_sim.defects import DefectType, TrueDefect
from smt_sim.events import ReasonCode, StationState
from smt_sim.station import Station


class PickAndPlace(Station):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        nozzle_cfg = self.params.get("nozzles")
        self.nozzles = [Nozzle(i, nozzle_cfg, self.rng) for i in range(nozzle_cfg["count"])] if nozzle_cfg else []
        self.clog_detect_fails = int(nozzle_cfg["clog_detect_consecutive_fails"]) if nozzle_cfg else 0
        self.clean_time_s = float(nozzle_cfg["clean_time_s"]) if nozzle_cfg else 0.0

        feeder_cfg = self.params.get("feeders")
        # Every feeder this machine has used, mounted or parked: part number -> feeder.
        self.feeders: dict[str, Feeder] = {}
        self.parts_cfg: dict[str, dict[str, Any]] = {}
        self.slots: list[str | None] = []   # part number mounted in each slot
        if feeder_cfg:
            self.parts_cfg = feeder_cfg["parts"]
            self.slots = [None] * int(feeder_cfg["slot_count"])
            self.low_reel_warning_boards = int(feeder_cfg["low_reel_warning_boards"])
            self.splice_miss_probability = float(feeder_cfg["splice_miss_probability"])
            self.reel_change_time_s = float(feeder_cfg["reel_change_time_s"])

        effects = self.params.get("speed_effects", {})
        self.offset_sd_exponent = float(effects.get("offset_sd_exponent", 0.0))
        self.pick_fail_exponent = float(effects.get("pick_fail_exponent", 0.0))

        self.mispick_count = 0   # total failed picks (parts wasted)
        self._pick_counter = 0   # drives the nozzle round robin
        # Picking progress for the current board (kept across a mid-board stop).
        self._picking_board: Board | None = None
        self._picks_todo: list[str] = []         # part numbers still to place
        self._placements: list[tuple[str, int, float]] = []
        self._board_mispicks = 0

    def cycle_time_for(self, board: Board) -> float:
        handling_s = self.params["board_handling_s"]
        per_part_s = self.params["time_per_component_s"]
        return handling_s + per_part_s * board.component_count

    def speed_effect_scales(self) -> tuple[float, float]:
        """(offset spread scale, pick-failure scale) at the current speed."""
        s = self.speed_fraction
        return s ** self.offset_sd_exponent, s ** self.pick_fail_exponent

    # ---- changeover ---------------------------------------------------------

    def _setup_for(self, board: Board) -> float:
        """Mount the feeders this product needs. Time = fixed + per feeder swapped."""
        cfg = self.params.get("changeover", {})
        fixed_s = float(cfg.get("time_s", 0.0))
        if not self.slots:
            return fixed_s
        needed = list(board.bom)
        if len(needed) > len(self.slots):
            raise ValueError(f"{board.product} needs {len(needed)} feeders but {self.name} has {len(self.slots)} slots")

        missing = [part for part in needed if part not in self.slots]
        for part in missing:
            slot = self._slot_to_use(needed)
            if self.slots[slot] is not None:
                self.feeders[self.slots[slot]].unmount()
            feeder = self.feeders.get(part)
            if feeder is None:
                feeder = self.feeders[part] = Feeder(part, self.parts_cfg[part])
            feeder.slot = slot + 1   # slots are numbered from 1 on the machine
            self.slots[slot] = part
        return fixed_s + float(cfg.get("per_feeder_s", 0.0)) * len(missing)

    def _slot_to_use(self, needed: list[str]) -> int:
        """An empty slot if there is one, else the first slot holding an unneeded part."""
        for i, part in enumerate(self.slots):
            if part is None:
                return i
        for i, part in enumerate(self.slots):
            if part not in needed:
                return i
        raise RuntimeError("no free feeder slot")   # ruled out by the size check above

    # ---- per-board picking --------------------------------------------------

    def _prepare_board(self, board: Board, dt_s: float) -> bool:
        if not self.nozzles or not self.feeders:
            return True

        if self._picking_board is not board:   # a new board: list every part to place
            self._picking_board = board
            self._placements = []
            self._picks_todo = []
            for part, qty in board.bom.items():
                self._picks_todo.extend([part] * qty)
            self._picks_todo.reverse()   # pop() from the end keeps BOM order
            self._board_mispicks = 0

        offset_scale, fail_scale = self.speed_effect_scales()
        while self._picks_todo:
            part = self._picks_todo[-1]
            feeder = self._feeder_for(part)
            self._maybe_splice(feeder)
            if feeder.qty == 0:
                self._reel_empty_stop(feeder, dt_s)
                return False

            nozzle = self.nozzles[self._pick_counter % len(self.nozzles)]
            self._pick_counter += 1
            offset_um, nozzle_failed = nozzle.pick(offset_scale, fail_scale)
            feeder_failed = self.rng.random() < feeder.mispick_rate * fail_scale
            feeder.qty -= 1
            self._maybe_call_operator(feeder, board)

            if nozzle_failed or feeder_failed:
                self.mispick_count += 1
                self._board_mispicks += 1
                # A retry costs one more pick, at the current speed.
                self.remaining_s += self.params["time_per_component_s"] / self.speed_fraction
                nozzle.consecutive_fails += 1
                if nozzle.consecutive_fails >= self.clog_detect_fails:
                    nozzle.clean()
                    self._stop_now(StationState.DOWN, ReasonCode.NOZZLE_CLOG, self.clean_time_s, dt_s)
                    return False
                continue   # retry the same part

            nozzle.consecutive_fails = 0
            self._placements.append((part, nozzle.nozzle_id, offset_um))
            self._picks_todo.pop()
            if abs(offset_um) > self.parts_cfg[part]["tolerance_um"]:
                # Ground-truth detail, including whether the nozzle was clogged at that moment.
                state = "clogged" if nozzle.clogged else "ok"
                board.true_defects.append(TrueDefect(
                    DefectType.MISPLACEMENT, self.name,
                    f"{part} nozzle {nozzle.nozzle_id} ({state}) offset {offset_um:.0f} um"))

        board.measurements[self.name] = {
            "placements": self._placements,   # (part number, nozzle id, offset µm)
            "max_abs_offset_um": max(abs(p[2]) for p in self._placements),
            "mispicks": self._board_mispicks,
        }
        return True

    def _feeder_for(self, part: str) -> Feeder:
        feeder = self.feeders.get(part)
        if feeder is None or feeder.slot is None:
            raise KeyError(f"{self.name}: no feeder mounted with part {part!r}")
        return feeder

    # ---- reels and operator -------------------------------------------------

    def _maybe_call_operator(self, feeder: Feeder, board: Board) -> None:
        """Low-reel warning: call the operator once per reel."""
        warning_qty = board.bom[feeder.part_number] * self.low_reel_warning_boards
        if feeder.operator_called or feeder.qty > warning_qty:
            return
        feeder.operator_called = True
        if self.rng.random() >= self.splice_miss_probability:
            feeder.splice_at_s = self._time_s + self.operator.response_time_s()

    def _maybe_splice(self, feeder: Feeder) -> None:
        """The operator has arrived in time: splice without stopping."""
        if feeder.splice_at_s is not None and self._time_s >= feeder.splice_at_s and feeder.qty > 0:
            feeder.splice()

    def _reel_empty_stop(self, feeder: Feeder, dt_s: float) -> None:
        if feeder.splice_at_s is not None:   # operator already on the way
            wait_s = max(0.0, feeder.splice_at_s - self._time_s)
        else:                                # nobody coming: call now
            wait_s = self.operator.response_time_s()
        feeder.load_new_reel()   # done by the time the stop ends
        self._stop_now(StationState.STARVED_MATERIAL, ReasonCode.FEEDER_EMPTY,
                       wait_s + self.reel_change_time_s, dt_s)

    def extra_tags(self) -> dict[str, Any]:
        # Only what the real machine knows: reel counts and failed picks.
        # Whether a nozzle is actually clogged is hidden ground truth.
        tags: dict[str, Any] = {}
        for i, part in enumerate(self.slots, start=1):
            tags[f"slot{i}_part"] = part or ""
            tags[f"slot{i}_qty"] = self.feeders[part].qty if part else 0
        tags["mispick_count"] = self.mispick_count
        return tags
