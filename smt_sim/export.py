"""Export board records to CSV (one row per board).

Lists of defects are packed into one cell each, separated by "; ", e.g.
true_defects = "MISPLACEMENT@pnp (R0402-10K nozzle 2 offset 212 um); TOMBSTONE@reflow (...)".
The event log has its own exporter: EventLog.to_csv().
"""

import csv
from pathlib import Path

from smt_sim.board import Board

BOARD_COLUMNS = [
    "serial", "product", "disposition", "rejected_at",
    "printer_in_s", "line_out_s",
    "paste_volume_mean_pct", "paste_volume_min_pct", "paste_volume_max_pct", "prints_since_wipe",
    "max_abs_offset_um", "mispicks",
    "peak_temp_c", "peak_deviation_c",
    "true_defects", "detected_defects", "false_calls",
]


def board_row(board: Board) -> dict:
    printer = board.measurements.get("printer", {})
    pnp = board.measurements.get("pnp", {})
    reflow = board.measurements.get("reflow", {})
    last_station_out = max(board.time_out_s.values()) if board.time_out_s else ""
    return {
        "serial": board.serial,
        "product": board.product,
        "disposition": board.disposition or "",
        "rejected_at": board.rejected_at or "",
        "printer_in_s": board.time_in_s.get("printer", ""),
        "line_out_s": last_station_out,
        "paste_volume_mean_pct": printer.get("paste_volume_mean_pct", ""),
        "paste_volume_min_pct": printer.get("paste_volume_min_pct", ""),
        "paste_volume_max_pct": printer.get("paste_volume_max_pct", ""),
        "prints_since_wipe": printer.get("prints_since_wipe", ""),
        "max_abs_offset_um": pnp.get("max_abs_offset_um", ""),
        "mispicks": pnp.get("mispicks", ""),
        "peak_temp_c": reflow.get("peak_temp_c", ""),
        "peak_deviation_c": reflow.get("peak_deviation_c", ""),
        "true_defects": "; ".join(f"{d.type}@{d.origin_station} ({d.detail})" for d in board.true_defects),
        "detected_defects": "; ".join(f"{d.type}@{d.detected_at}->{d.attributed_origin}"
                                      for d in board.detected_defects),
        "false_calls": "; ".join(f"{d.type}@{d.detected_at}" for d in board.false_calls),
    }


def boards_to_csv(boards: list[Board], path: str | Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=BOARD_COLUMNS)
        writer.writeheader()
        for board in sorted(boards, key=lambda b: b.serial):
            writer.writerow(board_row(board))
