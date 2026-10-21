"""Station states, reason codes, and the event log.

Every station state change is appended to one shared EventLog. Later milestones
compute time-in-state, OEE and downtime Pareto from this log alone, so the log
is the single source of truth for "what happened when".
"""

import csv
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class StationState(str, Enum):
    """Possible states of a station.

    Subclassing `str` makes each value a plain string ("RUNNING"), which keeps
    CSV export and the future OPC UA tags simple.
    """

    RUNNING = "RUNNING"
    STARVED = "STARVED"                    # input buffer empty (upstream problem)
    BLOCKED = "BLOCKED"                    # finished board, output buffer full (downstream problem)
    DOWN = "DOWN"                          # fault; always has a reason code
    STARVED_MATERIAL = "STARVED_MATERIAL"  # out of a consumable (paste, reel)
    SETUP = "SETUP"                        # product changeover
    PM = "PM"                              # planned maintenance


class ReasonCode:
    """Short reason strings attached to DOWN / STARVED_MATERIAL / PM events.

    Plain string constants rather than an Enum so they can be written straight
    into logs and tags without conversion.
    """

    NOZZLE_CLOG = "NOZZLE_CLOG"
    FEEDER_EMPTY = "FEEDER_EMPTY"
    PASTE_LOW = "PASTE_LOW"
    STENCIL_WIPE = "STENCIL_WIPE"
    CONVEYOR_JAM = "CONVEYOR_JAM"
    VISION_FAULT = "VISION_FAULT"            # camera / lighting error (SPI, AOI)
    HEAD_SERVO_FAULT = "HEAD_SERVO_FAULT"    # pick-and-place head drive wear-out
    HEATER_FAULT = "HEATER_FAULT"            # reflow zone heater / thermocouple
    FIXTURE_FAULT = "FIXTURE_FAULT"          # ICT bed-of-nails contact problem
    # Changeovers (SETUP state)
    STENCIL_CHANGE = "STENCIL_CHANGE"
    PROGRAM_LOAD = "PROGRAM_LOAD"            # SPI / AOI inspection program
    FEEDER_CHANGE = "FEEDER_CHANGE"
    PROFILE_CHANGE = "PROFILE_CHANGE"        # reflow profile + thermal settling
    FIXTURE_CHANGE = "FIXTURE_CHANGE"        # ICT bed-of-nails fixture
    CHANGEOVER = "CHANGEOVER"                # generic, when config names none
    MANUAL_STOP = "MANUAL_STOP"    # injected by a test or scenario


# Every reason code string, so config typos are caught at startup.
ALL_REASON_CODES = {v for k, v in vars(ReasonCode).items() if not k.startswith("_")}


@dataclass(frozen=True)
class Event:
    """One state change of one station."""

    tick: int
    sim_time_s: float
    station: str
    old_state: StationState | None   # None for the initial state at t=0
    new_state: StationState
    reason_code: str | None


class EventLog:
    """Append-only list of state-change events, shared by all stations."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def record(self, event: Event) -> None:
        self.events.append(event)

    def for_station(self, station: str) -> list[Event]:
        return [e for e in self.events if e.station == station]

    def __len__(self) -> int:
        return len(self.events)

    def to_csv(self, path: str | Path) -> None:
        """Write the log as CSV, one row per event. Empty cells mean None."""
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_COLUMNS)
            for e in self.events:
                writer.writerow([
                    e.tick,
                    e.sim_time_s,
                    e.station,
                    e.old_state.value if e.old_state else "",
                    e.new_state.value,
                    e.reason_code or "",
                ])


CSV_COLUMNS = ["tick", "sim_time_s", "station", "old_state", "new_state", "reason_code"]
