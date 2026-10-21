"""Inspection stations: SPI, AOI, ICT.

Each one looks at a board, flags problems, and pulls flagged boards off the
line into its own reject bin. Detection is imperfect in two ways:

  - it can MISS a real defect (an escape travels on downstream), and
  - it can FLAG a problem that isn't there (a false call -> false reject).

Two detection styles:

  InspectionStation (AOI, ICT): for every true defect on the board, detect it
      with a configured probability for that defect type (AOI can't see voids
      under a QFN; ICT can't see a slightly shifted part that still
      conducts). False calls arrive at a configured average rate per board.

  SolderPasteInspection (SPI): measures the paste volume with a little noise
      and flags it if the *measured* value is out of its spec. Misses and
      false calls come out naturally for boards close to the limit.

For every flag, the station names the station it blames
(`attribution`: defect type -> origin station). That is the station's best
guess; validation compares it with the defect's true origin.

Bookkeeping note: when a flag matches a real defect it is stored as a
detected defect, otherwise as a false call. That split uses ground truth and
exists only for validation; the reject decision itself uses only the flags.
"""

from typing import Any

from smt_sim.board import Board
from smt_sim.buffer import Buffer
from smt_sim.defects import DefectType, DetectedDefect, Disposition, FalseCall, TrueDefect
from smt_sim.station import Station


class InspectionStation(Station):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.attribution: dict[str, str] = self.params.get("attribution", {})
        self.detection_rate: dict[str, float] = self.params.get("detection_rate", {})
        self.false_calls_per_board = float(self.params.get("false_calls_per_board", 0.0))
        self.false_call_types: list[str] = self.params.get("false_call_types", [])
        self.scrap_if_calls_at_least = int(self.params.get("scrap_if_calls_at_least", 10**9))
        self.rejects = Buffer(f"{self.name}.rejects", capacity=None)

    # ---- per board ----------------------------------------------------------

    def _prepare_board(self, board: Board, dt_s: float) -> bool:
        detected, false_call_types = self._inspect(board)
        for defect in detected:
            board.detected_defects.append(
                DetectedDefect(defect.type, self.name, self.attribution.get(defect.type, "UNKNOWN")))
        for defect_type in false_call_types:
            board.false_calls.append(FalseCall(defect_type, self.name))

        calls = len(detected) + len(false_call_types)
        if calls:
            board.rejected_at = self.name
            board.disposition = Disposition.SCRAP if calls >= self.scrap_if_calls_at_least else Disposition.REWORK
        return True

    def _inspect(self, board: Board) -> tuple[list[TrueDefect], list[str]]:
        """Return (real defects found, types of false calls)."""
        detected = [d for d in board.true_defects
                    if self.rng.random() < self.detection_rate.get(d.type, 0.0)]
        return detected, self._false_calls()

    def _false_calls(self) -> list[str]:
        if self.false_calls_per_board <= 0 or not self.false_call_types:
            return []
        n = int(self.rng.poisson(self.false_calls_per_board))
        return [self.false_call_types[int(self.rng.integers(len(self.false_call_types)))] for _ in range(n)]

    # ---- routing --------------------------------------------------------------

    def _output_for(self, board: Board) -> Any:
        return self.rejects if board.rejected_at == self.name else self.output_buffer

    def _finish_board(self, board: Board, destination: Any = None) -> None:
        if destination is self.rejects:
            self.reject_count += 1
        super()._finish_board(board, destination)


class SolderPasteInspection(InspectionStation):
    """SPI: 3D-measures paste volume; flags boards whose measurement is out of spec."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        spec = self.params.get("spec_pct", {"min": 70.0, "max": 130.0})
        self.spec_min_pct = float(spec["min"])
        self.spec_max_pct = float(spec["max"])
        self.noise_pct = float(self.params.get("measurement_noise_pct", 0.0))

    def _inspect(self, board: Board) -> tuple[list[TrueDefect], list[str]]:
        printed = board.measurements.get("printer")
        if printed is None:   # no paste model (deterministic test line)
            return [], []
        measured_min = printed["paste_volume_min_pct"] + self.rng.normal(0.0, self.noise_pct)
        measured_max = printed["paste_volume_max_pct"] + self.rng.normal(0.0, self.noise_pct)
        board.measurements[self.name] = {"measured_min_pct": measured_min, "measured_max_pct": measured_max}

        flagged = []
        if measured_min < self.spec_min_pct:
            flagged.append(DefectType.INSUFFICIENT_PASTE)
        if measured_max > self.spec_max_pct:
            flagged.append(DefectType.EXCESS_PASTE)

        detected, false_calls = [], []
        for defect_type in flagged:
            real = next((d for d in board.true_defects if d.type == defect_type), None)
            if real is not None:
                detected.append(real)
            else:
                false_calls.append(defect_type)
        return detected, false_calls
