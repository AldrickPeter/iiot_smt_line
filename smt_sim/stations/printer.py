"""Stencil printer: prints solder paste, consumes paste, clogs and wears the stencil.

Causal chain for paste volume (randomness lives in the physical quantity):

    prints since wipe  -> aperture clogging  ┐
    stencil cycles     -> aperture wear      ├─> transfer loss % ─> paste volume %
    paste open time    -> paste drying       ┘                       (+ board and pad noise)

The board stores the resulting volume AND the cause values behind it, so a
defect found later can be traced back to "printed 9 prints after a wipe with
5 h old paste".

Each product has its own stencil. A changeover swaps the stencil (each one
keeps its own wear and clogging); the paste stays on the machine.

True defects: a pad below `insufficient_below_pct` is INSUFFICIENT_PASTE, a
pad above `excess_above_pct` is EXCESS_PASTE (origin: printer). One defect
record per type per board, with the number of pads affected as detail.

Stops:
  - wipe due (every N prints): DOWN / STENCIL_WIPE. It is a planned, short
    stop, but it is logged as DOWN so it counts against availability and
    shows in the Pareto, which lets you trade wipe frequency against defects.
  - paste below its minimum: STARVED_MATERIAL / PASTE_LOW until the operator
    arrives and adds fresh paste.
"""

from typing import Any

from smt_sim.board import Board
from smt_sim.components import SolderPaste, Stencil
from smt_sim.defects import DefectType, TrueDefect
from smt_sim.events import ReasonCode, StationState
from smt_sim.station import Station


class StencilPrinter(Station):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Consumable models are optional so a deterministic test line can omit them.
        self.paste = SolderPaste(self.params["paste"], self.rng) if "paste" in self.params else None
        self.stencil_cfg = self.params.get("stencil")
        self.stencils: dict[str, Stencil] = {}   # product -> its stencil
        self.stencil: Stencil | None = None      # the one mounted now (set at first setup)
        quality = self.params.get("print_quality", {})
        self.nominal_volume_pct = float(quality.get("nominal_volume_pct", 100.0))
        self.board_volume_sd_pct = float(quality.get("board_volume_sd_pct", 0.0))
        self.pad_volume_sd_pct = float(quality.get("pad_volume_sd_pct", 0.0))
        self.pads_per_board = int(quality.get("pads_per_board", 1))
        self.insufficient_below_pct = float(quality.get("insufficient_below_pct", 70.0))
        self.excess_above_pct = float(quality.get("excess_above_pct", 130.0))

    def _setup_for(self, board: Board) -> float:
        if self.stencil_cfg is not None:
            if board.product not in self.stencils:
                self.stencils[board.product] = Stencil(self.stencil_cfg)
            self.stencil = self.stencils[board.product]
        return super()._setup_for(board)

    def _before_next_board(self, dt_s: float) -> bool:
        if self.stencil is not None and self.stencil.wipe_due():
            self.stencil.wipe()
            self._stop_now(StationState.DOWN, ReasonCode.STENCIL_WIPE, self.stencil.wipe_time_s, dt_s)
            return True
        if self.paste is not None and self.paste.is_low():
            wait_s = self.operator.response_time_s() + self.paste.refill_time_s
            self.paste.refill(added_at_s=self._time_s + wait_s)
            self._stop_now(StationState.STARVED_MATERIAL, ReasonCode.PASTE_LOW, wait_s, dt_s)
            return True
        return False

    def _prepare_board(self, board: Board, dt_s: float) -> bool:
        if self.paste is None or self.stencil is None:
            return True

        loss_pct = self.stencil.transfer_loss_pct() + self.paste.drying_loss_pct(self._time_s)
        board_mean_pct = self.nominal_volume_pct - loss_pct + self.rng.normal(0.0, self.board_volume_sd_pct)
        pads_pct = self.rng.normal(board_mean_pct, self.pad_volume_sd_pct, size=self.pads_per_board)

        board.measurements[self.name] = {
            "paste_volume_mean_pct": float(pads_pct.mean()),
            "paste_volume_min_pct": float(pads_pct.min()),
            "paste_volume_max_pct": float(pads_pct.max()),
            # Cause values, kept as ground truth for root-cause analysis:
            "prints_since_wipe": self.stencil.prints_since_wipe,
            "stencil_cycles": self.stencil.cycle_count,
            "paste_open_time_h": self.paste.open_time_h(self._time_s),
        }
        n_low = int((pads_pct < self.insufficient_below_pct).sum())
        n_high = int((pads_pct > self.excess_above_pct).sum())
        if n_low:
            board.true_defects.append(TrueDefect(DefectType.INSUFFICIENT_PASTE, self.name, f"{n_low} pads"))
        if n_high:
            board.true_defects.append(TrueDefect(DefectType.EXCESS_PASTE, self.name, f"{n_high} pads"))

        self.paste.consume_print()
        self.stencil.record_print()
        return True

    def extra_tags(self) -> dict[str, Any]:
        tags: dict[str, Any] = {}
        if self.paste is not None:
            tags["paste_g"] = round(self.paste.grams, 1)
        if self.stencil is not None:
            tags["prints_since_wipe"] = self.stencil.prints_since_wipe
            tags["stencil_cycles"] = self.stencil.cycle_count
        return tags
