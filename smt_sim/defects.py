"""Defect records: ground truth vs what inspection saw.

Three separate lists live on every board:

  true_defects      what is physically wrong, created by the station whose
                    physical quantity went out of spec (paste volume,
                    placement offset, peak temperature). Never visible to
                    the line; used only to validate conclusions.
  detected_defects  real defects an inspection station found, with the
                    origin it *attributes* the defect to (its best guess).
  false_calls       things an inspection station flagged that were not real.

Comparing these lets you measure escapes (defects nobody caught), false
rejects (good boards thrown out) and whether attribution was right.
"""

from dataclasses import dataclass


class DefectType:
    INSUFFICIENT_PASTE = "INSUFFICIENT_PASTE"   # pad paste volume below spec -> open / weak joint
    EXCESS_PASTE = "EXCESS_PASTE"               # pad paste volume above spec -> bridge / short
    MISPLACEMENT = "MISPLACEMENT"               # part offset beyond its tolerance
    TOMBSTONE = "TOMBSTONE"                     # small chip stands up during reflow
    COLD_JOINT = "COLD_JOINT"                   # solder never fully melted (peak too low)
    VOID = "VOID"                               # gas voids under a part (peak too high / too long)


class Disposition:
    GOOD = "good"       # passed every inspection
    REWORK = "rework"   # rejected, repairable
    SCRAP = "scrap"     # rejected with too many problems to be worth repairing


@dataclass(frozen=True)
class TrueDefect:
    type: str
    origin_station: str
    detail: str = ""     # the cause, e.g. "nozzle 2 offset 212 um"


@dataclass(frozen=True)
class DetectedDefect:
    type: str
    detected_at: str
    attributed_origin: str


@dataclass(frozen=True)
class FalseCall:
    type: str
    detected_at: str
