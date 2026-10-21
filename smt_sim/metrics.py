"""Metrics computed from the event log: time in state, OEE, Pareto, bottleneck.

Everything here is derived after the fact from the event log plus a few
counters, never from live station internals. That keeps the metrics honest:
a dashboard fed only by the event stream could compute exactly the same thing.

Step 1 turns the log into intervals: "station X was in state S, with reason R,
from t0 to t1". Every metric below is a sum or grouping over those intervals.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from smt_sim.defects import Disposition
from smt_sim.events import EventLog, StationState

if TYPE_CHECKING:   # only for type hints; avoids a circular import with sim.py
    from smt_sim.sim import Simulator

# States caused by *other* stations. Everything else is the station's own time.
WAITING_STATES = {StationState.STARVED, StationState.BLOCKED}

# States that are stops with a reason code and belong in the downtime Pareto.
STOP_STATES = {StationState.DOWN, StationState.STARVED_MATERIAL, StationState.SETUP, StationState.PM}


@dataclass(frozen=True)
class Interval:
    station: str
    state: StationState
    reason_code: str | None
    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def state_intervals(event_log: EventLog, end_time_s: float) -> list[Interval]:
    """Convert state-change events into closed time intervals.

    Each event starts an interval that lasts until that station's next event,
    or until `end_time_s` for the last one.
    """
    by_station: dict[str, list] = defaultdict(list)
    for event in event_log.events:
        by_station[event.station].append(event)

    intervals = []
    for station, events in by_station.items():
        for this, nxt in zip(events, events[1:] + [None]):
            end_s = nxt.sim_time_s if nxt is not None else end_time_s
            if end_s > this.sim_time_s:   # skip zero-length intervals
                intervals.append(Interval(station, this.new_state, this.reason_code, this.sim_time_s, end_s))
    return intervals


def time_in_state(intervals: list[Interval]) -> dict[str, dict[StationState, float]]:
    """Seconds spent in each state, per station. Every state appears (0.0 if never entered)."""
    totals: dict[str, dict[StationState, float]] = defaultdict(lambda: {s: 0.0 for s in StationState})
    for iv in intervals:
        totals[iv.station][iv.state] += iv.duration_s
    return dict(totals)


# ---- OEE ------------------------------------------------------------------


@dataclass(frozen=True)
class StationOEE:
    planned_s: float
    running_s: float
    starved_s: float
    blocked_s: float
    boards_processed: int
    ideal_work_s: float  # Σ boards × ideal cycle of their product
    utilization: float   # ideal work / planned: share of capacity actually used
    availability: float
    performance: float
    quality: float
    oee: float


def _ratio(numerator: float, denominator: float) -> float:
    """Division that returns NaN instead of crashing when nothing was measured."""
    return numerator / denominator if denominator > 0 else math.nan


def station_oee(sim: Simulator) -> dict[str, StationOEE]:
    """OEE per station, judging each station only on its own losses.

    planned time  = observed time − planned maintenance (PM)
    availability  = running / (planned − starved − blocked)
                    Starved/blocked are caused by neighbours, so they are
                    removed from the denominator and reported separately.
    performance   = ideal work / running
    quality       = 1 − (boards with a detected defect blamed on this station
                         / boards processed)
                    Uses what inspection *attributed*, not ground truth, just
                    like a real dashboard would.
    utilization   = ideal work / planned
                    How much of the station's capacity was used. Not part of
                    OEE; low utilization on a non-bottleneck is normal.

    ideal work = Σ over products of (boards of that product × its ideal
    cycle), because each product takes a different time (e.g. fewer parts
    to place).

    Pipeline stations (the reflow oven) are RUNNING whenever any board is
    inside. Their belt moves at a fixed speed, so they cannot lose speed:
    performance is 1.0 by definition. How full the oven runs (e.g. 8 of 16
    slots) shows up as utilization instead.
    """
    tis = time_in_state(state_intervals(sim.event_log, sim.time_s))
    blamed = boards_blamed_per_station(sim)
    result = {}
    for st in sim.stations:
        t = tis[st.name]
        planned_s = sim.time_s - t[StationState.PM]
        running_s = t[StationState.RUNNING]
        starved_s = t[StationState.STARVED]
        blocked_s = t[StationState.BLOCKED]
        ideal_work_s = sum(n * sim.ideal_cycle_time_s(st.name, product)
                           for product, n in st.parts_by_product.items())

        availability = _ratio(running_s, planned_s - starved_s - blocked_s)
        if st.is_pipeline:
            performance = 1.0
        else:
            performance = _ratio(ideal_work_s, running_s)
        quality = 1.0 - _ratio(blamed[st.name], st.part_count) if st.part_count else math.nan
        result[st.name] = StationOEE(
            planned_s=planned_s,
            running_s=running_s,
            starved_s=starved_s,
            blocked_s=blocked_s,
            boards_processed=st.part_count,
            ideal_work_s=ideal_work_s,
            utilization=_ratio(ideal_work_s, planned_s),
            availability=availability,
            performance=performance,
            quality=quality,
            oee=availability * performance * quality,
        )
    return result


def boards_blamed_per_station(sim: Simulator) -> Counter[str]:
    """For each station: how many boards had a detected defect attributed to it."""
    blamed: Counter[str] = Counter()
    for board in finished_boards(sim):
        for origin in {d.attributed_origin for d in board.detected_defects}:
            blamed[origin] += 1
    return blamed


@dataclass(frozen=True)
class LineOEE:
    planned_s: float
    boards_out: int
    bottleneck_ideal_cycle_s: dict[str, float]   # per product
    oee: float


def line_oee(sim: Simulator) -> LineOEE:
    """Line OEE measured at the end of the line.

    OEE = Σ (good boards of product p × bottleneck ideal cycle of p) / planned time

    i.e. actual good output, valued at the line's ideal pace for each product,
    divided by the time available. Only boards that passed every inspection
    reach the line exit, so rejects are automatically left out.
    """
    out_by_product = Counter(b.product for b in sim.sink.boards())
    bottleneck_s = {p: sim.bottleneck_ideal_cycle_s(p) for p in sim.products}
    ideal_output_s = sum(n * bottleneck_s[p] for p, n in out_by_product.items())
    return LineOEE(
        planned_s=sim.time_s,
        boards_out=sum(out_by_product.values()),
        bottleneck_ideal_cycle_s=bottleneck_s,
        oee=_ratio(ideal_output_s, sim.time_s),
    )


# ---- Quality ----------------------------------------------------------------


def finished_boards(sim: Simulator) -> list:
    """Boards that have left the line: passed (line exit) or rejected."""
    return sim.sink.boards() + sim.rejected_boards()


@dataclass(frozen=True)
class QualityReport:
    boards_finished: int
    good: int
    rework: int
    scrap: int
    first_pass_yield: float
    rejects_by_station: dict[str, int]
    detected_pareto: list[tuple[str, str, int]]   # (blamed station, defect type, count), largest first
    # Validation against ground truth (a real factory can't see these):
    true_defects_by_origin: dict[str, int]
    escapes: int             # boards shipped as good that carry a real defect
    false_rejects: int       # boards rejected that had no real defect at all
    attribution_accuracy: float   # share of detected defects blamed on their true origin


def quality_report(sim: Simulator) -> QualityReport:
    """First-pass yield, reject Pareto, and a ground-truth check of inspection.

    first-pass yield = boards that passed every inspection the first time
                       / boards that have left the line (passed or rejected)

    Boards still on the line are left out of both counts, so a short run
    isn't biased by boards that simply haven't been inspected yet. Rejected
    boards are not re-run, so "passed" here always means passed first time.
    """
    boards = finished_boards(sim)
    dispositions = Counter(b.disposition for b in boards)
    rejects = Counter(b.rejected_at for b in boards if b.rejected_at)
    detected = Counter((d.attributed_origin, d.type) for b in boards for d in b.detected_defects)
    true_by_origin = Counter(d.origin_station for b in boards for d in b.true_defects)

    correct = total = 0
    for b in boards:
        for d in b.detected_defects:
            true_origins = {t.origin_station for t in b.true_defects if t.type == d.type}
            correct += d.attributed_origin in true_origins
            total += 1

    return QualityReport(
        boards_finished=len(boards),
        good=dispositions[Disposition.GOOD],
        rework=dispositions[Disposition.REWORK],
        scrap=dispositions[Disposition.SCRAP],
        first_pass_yield=_ratio(dispositions[Disposition.GOOD], len(boards)),
        rejects_by_station=dict(rejects),
        detected_pareto=[(origin, typ, n) for (origin, typ), n in detected.most_common()],
        true_defects_by_origin=dict(true_by_origin),
        escapes=sum(1 for b in boards if b.disposition == Disposition.GOOD and b.true_defects),
        false_rejects=sum(1 for b in boards if b.rejected_at and not b.true_defects),
        attribution_accuracy=_ratio(correct, total),
    )


# ---- Downtime Pareto ------------------------------------------------------


@dataclass(frozen=True)
class ParetoRow:
    station: str
    reason_code: str
    state: StationState
    total_s: float
    occurrences: int


def downtime_pareto(intervals: list[Interval]) -> list[ParetoRow]:
    """Stop time grouped by (station, reason code), largest first.

    Only the station's own stops count (DOWN, STARVED_MATERIAL, SETUP, PM);
    starved/blocked waiting is a symptom, not a root cause.
    """
    totals: dict[tuple, float] = defaultdict(float)
    counts: dict[tuple, int] = defaultdict(int)
    for iv in intervals:
        if iv.state in STOP_STATES:
            key = (iv.station, iv.reason_code or "UNKNOWN", iv.state)
            totals[key] += iv.duration_s
            counts[key] += 1
    rows = []
    for (station, reason_code, state), total_s in totals.items():
        rows.append(ParetoRow(station, reason_code, state, total_s, counts[(station, reason_code, state)]))
    return sorted(rows, key=lambda r: r.total_s, reverse=True)


# ---- Bottleneck detection -------------------------------------------------


@dataclass(frozen=True)
class BottleneckResult:
    station: str
    mean_active_period_s: dict[str, float]   # per station
    confirmed: bool   # upstream mostly blocked AND downstream mostly starved


def active_spans(sim: Simulator, intervals: list[Interval], station_name: str) -> list[tuple[float, float]]:
    """(start, end) spans during which a station counts as "active".

    Normal stations: active = any state except waiting (STARVED or BLOCKED).
    That includes RUNNING and also DOWN, SETUP, etc.: the station is busy
    with its own business and the rest of the line has to wait for it.

    Pipeline stations (reflow oven) would look active almost all the time,
    because some board is nearly always inside. They are judged at their
    entrance instead: active for one entry interval after each board enters
    (the entrance is busy spacing boards), plus any own stops (DOWN, ...).
    Approximation: an entry window that overlaps a BLOCKED period still
    counts as active.
    """
    station = sim.station(station_name)
    own = [iv for iv in intervals if iv.station == station_name]
    if not station.is_pipeline:
        return [(iv.start_s, iv.end_s) for iv in own if iv.state not in WAITING_STATES]

    stops = [(iv.start_s, iv.end_s) for iv in own if iv.state in STOP_STATES]
    window_s = station.entry_interval_s
    entries = [(t, min(t + window_s, sim.time_s)) for t in station.entry_times_s]
    return stops + entries


def merge_spans(spans: list[tuple[float, float]]) -> list[float]:
    """Merge overlapping or touching spans; return the merged durations.

    E.g. RUNNING 0-30 then DOWN 30-50 is one uninterrupted 50 s active period.
    """
    periods = []
    cur_start = cur_end = None
    for start, end in sorted(spans):
        if cur_end is not None and start <= cur_end:
            cur_end = max(cur_end, end)
            continue
        if cur_end is not None:
            periods.append(cur_end - cur_start)
        cur_start, cur_end = start, end
    if cur_end is not None:
        periods.append(cur_end - cur_start)
    return periods


def find_bottleneck(sim: Simulator) -> BottleneckResult:
    """Active period method (Roser et al.).

    The bottleneck is the station with the longest average active period:
    it rarely has to wait, so everyone else waits for it. As a cross-check,
    stations upstream of it should spend more time blocked than starved, and
    stations downstream more time starved than blocked.
    """
    intervals = state_intervals(sim.event_log, sim.time_s)
    tis = time_in_state(intervals)
    names = [st.name for st in sim.stations]

    mean_active: dict[str, float] = {}
    starved: dict[str, float] = {}
    blocked: dict[str, float] = {}
    for name in names:
        periods = merge_spans(active_spans(sim, intervals, name))
        mean_active[name] = sum(periods) / len(periods) if periods else 0.0
        blocked[name] = tis[name][StationState.BLOCKED]
        if sim.station(name).is_pipeline:
            # Entrance idle and not blocked = waiting for boards from upstream.
            starved[name] = max(0.0, sim.time_s - sum(periods) - blocked[name])
        else:
            starved[name] = tis[name][StationState.STARVED]
    bottleneck = max(names, key=lambda n: mean_active[n])

    idx = names.index(bottleneck)
    upstream_ok = all(blocked[n] >= starved[n] for n in names[:idx])
    downstream_ok = all(starved[n] >= blocked[n] for n in names[idx + 1:])
    return BottleneckResult(bottleneck, mean_active, upstream_ok and downstream_ok)


# ---- Root-cause diagnosis -------------------------------------------------


@dataclass(frozen=True)
class Diagnosis:
    bottleneck: str
    top_bottleneck_stop: ParetoRow | None        # biggest own stop at the bottleneck
    top_defect: tuple[str, str, int] | None      # (blamed station, defect type, count)


def diagnose(sim: Simulator) -> Diagnosis:
    """Point at the most likely root causes, using only observable data.

    Throughput: lost time at the bottleneck is lost output for the whole line,
    while a stop at a non-bottleneck is often absorbed by buffers (the printer
    wipes its stencil while it would be blocked anyway). So the throughput
    root cause is the bottleneck's own largest stop, not the top of the
    line-wide Pareto. Changeovers (SETUP) are left out: they are planned,
    not a fault to fix.

    Quality: the most frequent detected defect and the station it is blamed on.
    """
    bottleneck = find_bottleneck(sim).station
    rows = downtime_pareto(state_intervals(sim.event_log, sim.time_s))
    stops = [r for r in rows if r.station == bottleneck and r.state != StationState.SETUP]
    pareto = quality_report(sim).detected_pareto
    return Diagnosis(bottleneck, stops[0] if stops else None, pareto[0] if pareto else None)


# ---- Text report ----------------------------------------------------------


def format_report(sim: Simulator) -> str:
    """Human-readable summary of all metrics (used by the headless script)."""
    lines = []
    line = line_oee(sim)
    cycles = ", ".join(f"{p} {s:g} s" for p, s in line.bottleneck_ideal_cycle_s.items())
    lines.append(f"Line OEE {line.oee:6.1%}   boards out {line.boards_out}   time {line.planned_s:g} s")
    lines.append(f"  bottleneck ideal cycle per product: {cycles}")
    lines.append("")
    lines.append(f"{'station':10s} {'running':>8s} {'starved':>8s} {'blocked':>8s}"
                 f" {'avail':>7s} {'perf':>7s} {'qual':>7s} {'OEE':>7s} {'util':>7s} {'boards':>7s}")
    for name, m in station_oee(sim).items():
        p = m.planned_s
        lines.append(
            f"{name:10s} {m.running_s / p:8.1%} {m.starved_s / p:8.1%} {m.blocked_s / p:8.1%}"
            f" {m.availability:7.1%} {m.performance:7.1%} {m.quality:7.1%} {m.oee:7.1%} {m.utilization:7.1%}"
            f" {m.boards_processed:7d}"
        )

    bn = find_bottleneck(sim)
    lines.append("")
    lines.append(f"Bottleneck: {bn.station} ({'confirmed' if bn.confirmed else 'NOT confirmed'} "
                 f"by upstream blocking / downstream starving)")
    lines.append("  mean active period: " + ", ".join(f"{n} {s:.0f} s" for n, s in bn.mean_active_period_s.items()))

    pareto = downtime_pareto(state_intervals(sim.event_log, sim.time_s))
    lines.append("")
    lines.append("Downtime Pareto:" + ("" if pareto else " (no stops)"))
    for row in pareto:
        lines.append(f"  {row.station:10s} {row.reason_code:16s} {row.total_s:8.1f} s  x{row.occurrences}")

    q = quality_report(sim)
    lines.append("")
    lines.append(f"Quality: first-pass yield {q.first_pass_yield:.1%}   good {q.good}   "
                 f"rework {q.rework}   scrap {q.scrap}   (of {q.boards_finished} finished)")
    lines.append("  rejects by station: " + ", ".join(f"{s} {n}" for s, n in q.rejects_by_station.items()))
    lines.append("  detected defects (blamed station, type):")
    for origin, typ, n in q.detected_pareto:
        lines.append(f"    {origin:10s} {typ:20s} {n:5d}")
    lines.append(f"  ground truth check: escapes {q.escapes}, false rejects {q.false_rejects}, "
                 f"attribution accuracy {q.attribution_accuracy:.1%}")
    lines.append("    true defects by origin: " + ", ".join(f"{s} {n}" for s, n in q.true_defects_by_origin.items()))

    d = diagnose(sim)
    lines.append("")
    lines.append(f"Diagnosis: bottleneck {d.bottleneck}")
    if d.top_bottleneck_stop is not None:
        r = d.top_bottleneck_stop
        lines.append(f"  biggest throughput loss: {r.station} {r.reason_code} ({r.total_s / 60:.0f} min, x{r.occurrences})")
    if d.top_defect is not None:
        origin, typ, n = d.top_defect
        lines.append(f"  biggest quality loss:    {typ} blamed on {origin} ({n} boards)")
    return "\n".join(lines)
