"""Live pygame view of one SMT line or a whole factory of lines.

Usage:
    python viz/pygame_view.py [--config path.yaml] [--seed 42] [--speed 60]
    python viz/pygame_view.py --scenario nozzle_fault
    python viz/pygame_view.py --factory [config/factory.yaml]

Controls:
    Up / Down (or + / -)   faster / slower simulation
    Space                  pause / resume
    Tab                    select the next station (outlined in white)
    Left / Right           selected station's speed -5 % / +5 % (of rated speed)
    Esc or Q               quit

Design: this file is the only place that imports pygame. `FactoryView` only
*reads* the lines (tags, buffers, event logs, metrics) and draws them; it
never changes simulation state. A single line is shown as a factory with one
line. The `run()` loop is the controller: it decides how many ticks to advance
per frame to hit the chosen speed, handles the keys (including station speed,
like an operator panel would), then asks the view to draw. The simulation core
still runs headless without any of this.
"""

import argparse
import sys
import time
from pathlib import Path

import pygame

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # allow running without install

from smt_sim import Simulator  # noqa: E402
from smt_sim.events import StationState  # noqa: E402
from smt_sim.factory import DEFAULT_FACTORY_PATH, Factory, factory_summary  # noqa: E402
from smt_sim.metrics import line_oee, quality_report, station_oee  # noqa: E402
from smt_sim.scenarios import load_scenario  # noqa: E402

WIDTH = 1280
FPS = 30
SPEEDS = [1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600]   # sim seconds per real second
STATION_SPEED_STEP_PCT = 5
STATION_SPEED_MIN_PCT = 5
MAX_TICKS_PER_FRAME = 20_000   # if the PC can't keep up, run slower instead of freezing
METRICS_REFRESH_S = 0.5        # recomputing OEE scans the whole event log, so not every frame

STATE_COLORS = {
    StationState.RUNNING: (46, 160, 67),
    StationState.STARVED: (219, 180, 40),
    StationState.BLOCKED: (232, 120, 30),
    StationState.DOWN: (200, 45, 45),
    StationState.STARVED_MATERIAL: (140, 70, 170),
    StationState.SETUP: (50, 110, 200),
    StationState.PM: (120, 120, 120),
}
BACKGROUND = (24, 26, 30)
PANEL = (38, 41, 47)
TEXT = (225, 228, 232)
DIM_TEXT = (150, 155, 162)
SLOT_EMPTY = (60, 64, 72)
SLOT_FULL = (180, 190, 205)

STATION_W, STATION_H = 160, 170
BUFFER_W = 40
HEADER_H = 95             # factory title + totals
ROW_H = 24 + STATION_H + 18   # line label + station boxes + gap
BOTTOM_H = 250            # recent stops, defects, legend


def window_height(n_lines: int) -> int:
    return HEADER_H + n_lines * ROW_H + BOTTOM_H


class FactoryView:
    """Draws every line of a Factory onto a pygame surface, one row per line. Read-only."""

    def __init__(self, factory: Factory) -> None:
        self.factory = factory
        self.font = pygame.font.SysFont("consolas", 15)
        self.small = pygame.font.SysFont("consolas", 13)
        self.big = pygame.font.SysFont("consolas", 22, bold=True)
        self._metrics_at_wall = -1e9
        self._line_oee = {}       # line name -> LineOEE
        self._station_oee = {}    # line name -> {station: StationOEE}
        self._quality = {}        # line name -> QualityReport
        self._factory_totals = None
        self.selected: tuple[str, str] | None = None   # (line, station) chosen with Tab; set by run()

    # ---- public -------------------------------------------------------------

    def draw(self, surface: pygame.Surface, speed: float, paused: bool) -> None:
        self._refresh_metrics()
        surface.fill(BACKGROUND)
        self._draw_header(surface, speed, paused)
        for i, (name, sim) in enumerate(self.factory.lines.items()):
            self._draw_line_row(surface, name, sim, HEADER_H + i * ROW_H)
        self._draw_bottom(surface, HEADER_H + len(self.factory.lines) * ROW_H + 6)

    # ---- pieces ---------------------------------------------------------------

    def _refresh_metrics(self) -> None:
        now = time.perf_counter()
        if now - self._metrics_at_wall < METRICS_REFRESH_S or self.factory.time_s == 0:
            return
        self._metrics_at_wall = now
        for name, sim in self.factory.lines.items():
            self._line_oee[name] = line_oee(sim)
            self._station_oee[name] = station_oee(sim)
            self._quality[name] = quality_report(sim)
        if len(self.factory.lines) > 1:
            self._factory_totals = factory_summary(self.factory)

    def _draw_header(self, surface: pygame.Surface, speed: float, paused: bool) -> None:
        speed_txt = "PAUSED" if paused else f"{speed:g}x"
        title = "SMT factory" if len(self.factory.lines) > 1 else "SMT line"
        self._text(surface, f"{title}   sim time {_clock(self.factory.time_s)}   speed {speed_txt}", (20, 15), self.big)
        if self._factory_totals:
            _, oee, fpy = self._factory_totals
            self._text(surface, f"Factory OEE {oee:.1%}   first-pass yield {fpy:.1%}   good boards out "
                                f"{self.factory.boards_completed}", (20, 52), self.font)
        else:
            self._text(surface, "Line OEE, yield and counts are shown on each line's row.", (20, 52), self.font, DIM_TEXT)

    def _draw_line_row(self, surface: pygame.Surface, name: str, sim: Simulator, top: int) -> None:
        oee = self._line_oee.get(name)
        q = self._quality.get(name)
        fpy = f"{q.first_pass_yield:.1%}" if q and q.boards_finished else "-"
        rejects = (q.rework + q.scrap) if q else 0
        upcoming = "finished" if sim.source.is_empty() else sim.source.peek().product
        self._text(surface, f"{name.upper()}   OEE {f'{oee.oee:.1%}' if oee else '-'}   FPY {fpy}   "
                            f"good {sim.boards_completed}   rejected {rejects}   next: {upcoming}",
                   (20, top), self.font)

        tags = sim.get_tags()
        x, y = 20, top + 24
        for i, st in enumerate(sim.stations):
            self._draw_station(surface, name, st, tags[st.name], pygame.Rect(x, y, STATION_W, STATION_H))
            x += STATION_W
            if i < len(sim.buffers):
                buf = sim.buffers[i]
                self._draw_buffer(surface, len(buf), buf.capacity, x + 4, y + STATION_H // 2)
                x += BUFFER_W

    def _draw_station(self, surface: pygame.Surface, line: str, st, tags: dict, rect: pygame.Rect) -> None:
        pygame.draw.rect(surface, PANEL, rect, border_radius=8)
        pygame.draw.rect(surface, STATE_COLORS[st.state], (rect.x, rect.y, rect.w, 30),
                         border_top_left_radius=8, border_top_right_radius=8)
        is_selected = self.selected == (line, st.name)
        speed = f"   {st.speed_pct:.0f}%" if st.speed_pct < 100 or is_selected else ""
        self._text(surface, st.name.upper() + speed, (rect.x + 8, rect.y + 6), self.font)
        if is_selected:
            pygame.draw.rect(surface, TEXT, rect, width=2, border_radius=8)

        lines = [
            (st.state.value, TEXT),
            (tags["reason_code"] or "", DIM_TEXT),
            (st.current_product or "-", DIM_TEXT),
            (f"done {tags['part_count']}   rej {tags['reject_count']}", TEXT),
        ]
        m = self._station_oee.get(line, {}).get(st.name)
        if m is not None:
            lines.append((f"OEE {m.oee:.0%}  A {m.availability:.0%}", TEXT))
        lines.append((self._station_detail(st, tags), DIM_TEXT))
        y = rect.y + 36
        for text, col in lines:
            self._text(surface, text, (rect.x + 8, y), self.small, col)
            y += 21

    def _station_detail(self, st, tags: dict) -> str:
        """One station-specific line, from the station's own tags."""
        if "paste_g" in tags:
            return f"paste {tags['paste_g']:.0f} g  wipe {tags.get('prints_since_wipe', 0)}"
        if "mispick_count" in tags:
            low = min((v for k, v in tags.items() if k.endswith("_qty") and v > 0), default=0)
            return f"lowest reel {low}"
        if "peak_zone_offset_c" in tags:
            inside = len(st.boards_inside())
            return f"{inside}/{st.capacity} in  {tags['peak_zone_offset_c']:+.1f} C"
        return ""

    def _draw_buffer(self, surface: pygame.Surface, level: int, capacity: int, x: int, y_mid: int) -> None:
        slot_h, gap = 14, 4
        total_h = capacity * slot_h + (capacity - 1) * gap
        y = y_mid - total_h // 2
        for slot in range(capacity):
            filled = slot >= capacity - level   # fill from the bottom up
            pygame.draw.rect(surface, SLOT_FULL if filled else SLOT_EMPTY,
                             (x, y + slot * (slot_h + gap), BUFFER_W - 8, slot_h), border_radius=3)

    def _draw_bottom(self, surface: pygame.Surface, top: int) -> None:
        multi = len(self.factory.lines) > 1

        # Recent stops from every line, newest last.
        self._text(surface, "Recent stops and changeovers", (20, top), self.font)
        events = []
        for name, sim in self.factory.lines.items():
            events += [(e, name) for e in sim.event_log.events[-150:]
                       if e.new_state not in (StationState.RUNNING, StationState.STARVED, StationState.BLOCKED)]
        events.sort(key=lambda pair: pair[0].sim_time_s)
        for i, (e, name) in enumerate(events[-9:]):
            where = f"{name}/{e.station}" if multi else e.station
            self._text(surface, f"{_clock(e.sim_time_s)}  {where:14s} {e.new_state.value:17s} {e.reason_code or ''}",
                       (20, top + 22 + i * 18), self.small, STATE_COLORS[e.new_state])

        # Detected defects per line.
        x = 760
        self._text(surface, "Detected defects (blamed station, type)", (x, top), self.font)
        row = 0
        for name, q in self._quality.items():
            for origin, typ, n in q.detected_pareto[:4 if multi else 8]:
                label = f"{name}/{origin}" if multi else origin
                self._text(surface, f"{n:5d}  {label:14s} {typ}", (x, top + 22 + row * 18), self.small)
                row += 1

        # Legend.
        x, y = 20, surface.get_height() - 50
        for state, color in STATE_COLORS.items():
            pygame.draw.rect(surface, color, (x, y, 14, 14), border_radius=3)
            label = self.small.render(state.value, True, TEXT)
            surface.blit(label, (x + 20, y))
            x += 40 + label.get_width()
        self._text(surface, "Up/Down: sim speed   Space: pause   Tab: select station   "
                            "Left/Right: station speed   Esc: quit", (20, surface.get_height() - 26),
                   self.small, DIM_TEXT)

    # ---- helpers ---------------------------------------------------------------

    def _text(self, surface, text, pos, font, color=TEXT) -> None:
        surface.blit(font.render(text, True, color), pos)


def _clock(t_s: float) -> str:
    return f"{int(t_s // 3600):02d}:{int(t_s % 3600 // 60):02d}:{int(t_s % 60):02d}"


def advance(model, speed: float, frame_s: float, tick_debt: float) -> float:
    """Step a Simulator or Factory for one frame at `speed` (sim s per real s).

    Returns the leftover tick fraction. Carrying it over means e.g. 1x speed at
    30 fps (1/60 tick per frame) still advances exactly 1 sim second per real
    second.
    """
    tick_debt += speed * frame_s / model.dt_s
    n_ticks = int(tick_debt)
    tick_debt -= n_ticks
    for _ in range(min(n_ticks, MAX_TICKS_PER_FRAME)):
        model.step()
    return tick_debt


def adjustable_stations(factory: Factory) -> list[tuple[str, str]]:
    """(line, station) pairs whose speed can be changed (not the fixed-speed oven)."""
    return [(name, st.name) for name, sim in factory.lines.items()
            for st in sim.stations if not st.is_pipeline]


def change_station_speed(factory: Factory, selected: tuple[str, str], delta_pct: float) -> None:
    """Nudge one station's speed, kept between STATION_SPEED_MIN_PCT and 100 %."""
    line, station = selected
    st = factory.lines[line].station(station)
    st.set_speed(min(100.0, max(STATION_SPEED_MIN_PCT, st.speed_pct + delta_pct)))


def run(factory: Factory, speed: float = 60.0) -> None:
    """Open a window and run the factory live until the user quits."""
    pygame.init()
    screen = pygame.display.set_mode((WIDTH, window_height(len(factory.lines))))
    pygame.display.set_caption("SMT line simulator")
    clock = pygame.time.Clock()
    view = FactoryView(factory)
    speed_index = min(range(len(SPEEDS)), key=lambda i: abs(SPEEDS[i] - speed))
    paused = False
    tick_debt = 0.0   # fractional ticks carried to the next frame, so slow speeds stay exact
    selectable = adjustable_stations(factory)
    selected_index = -1   # nothing selected until Tab is pressed

    running = True
    while running:
        frame_s = clock.tick(FPS) / 1000.0
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_ESCAPE, pygame.K_q):
                    running = False
                elif event.key == pygame.K_SPACE:
                    paused = not paused
                elif event.key in (pygame.K_UP, pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS):
                    speed_index = min(speed_index + 1, len(SPEEDS) - 1)
                elif event.key in (pygame.K_DOWN, pygame.K_MINUS, pygame.K_KP_MINUS):
                    speed_index = max(speed_index - 1, 0)
                elif event.key == pygame.K_TAB:
                    selected_index = (selected_index + 1) % len(selectable)
                    view.selected = selectable[selected_index]
                elif event.key in (pygame.K_LEFT, pygame.K_RIGHT) and view.selected:
                    step = STATION_SPEED_STEP_PCT if event.key == pygame.K_RIGHT else -STATION_SPEED_STEP_PCT
                    change_station_speed(factory, view.selected, step)

        if not paused:
            tick_debt = advance(factory, SPEEDS[speed_index], frame_s, tick_debt)

        view.draw(screen, SPEEDS[speed_index], paused)
        pygame.display.flip()

    pygame.quit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--speed", type=float, default=60.0, help="sim seconds per real second")
    parser.add_argument("--scenario", default=None, help="name in config/scenarios/ or a path")
    parser.add_argument("--factory", nargs="?", const=str(DEFAULT_FACTORY_PATH), default=None,
                        help="show every line of a factory file (default config/factory.yaml)")
    args = parser.parse_args()
    if args.factory:
        factory = Factory.from_config(args.factory, seed=args.seed)
    elif args.scenario:
        factory = Factory.single(load_scenario(args.scenario).build_simulator(seed=args.seed))
    else:
        factory = Factory.single(Simulator(args.config, seed=args.seed))
    run(factory, speed=args.speed)


if __name__ == "__main__":
    main()
