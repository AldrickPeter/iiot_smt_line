"""Milestone 6: pygame view. Rendered off-screen (no window) with SDL's dummy driver."""

import os
import subprocess
import sys

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
pygame = pytest.importorskip("pygame")

from smt_sim import Simulator  # noqa: E402
from smt_sim.factory import Factory  # noqa: E402
from viz.pygame_view import (  # noqa: E402
    WIDTH,
    FactoryView,
    adjustable_stations,
    advance,
    change_station_speed,
    window_height,
)


@pytest.fixture(scope="module", autouse=True)
def pygame_session():
    pygame.init()
    yield
    pygame.quit()


def snapshot(sim: Simulator) -> tuple:
    return (sim.tick, len(sim.event_log), sim.boards_completed,
            [(st.state, st.part_count) for st in sim.stations], [len(b) for b in sim.buffers])


@pytest.mark.parametrize("factory_builder", [
    lambda: Factory.single(Simulator(seed=1)),
    lambda: Factory.from_config(seed=1),
], ids=["one line", "factory"])
def test_view_draws_at_many_moments_without_changing_the_sim(factory_builder):
    factory = factory_builder()
    view = FactoryView(factory)
    surface = pygame.Surface((WIDTH, window_height(len(factory.lines))))
    for _ in range(8):             # 0 h .. 5.25 h, includes changeovers and stops
        before = [snapshot(sim) for sim in factory.lines.values()]
        view._metrics_at_wall = -1e9   # force a metrics refresh every frame
        view.draw(surface, speed=60, paused=False)
        view.draw(surface, speed=60, paused=True)
        assert [snapshot(sim) for sim in factory.lines.values()] == before   # drawing is read-only
        factory.run(2700)


def test_advance_keeps_exact_speed_with_fractional_ticks():
    sim = Simulator(seed=1)
    debt = 0.0
    for _ in range(300):   # 10 real seconds at 30 fps, speed 1x -> 1/60 tick per frame
        debt = advance(sim, speed=1, frame_s=1 / 30, tick_debt=debt)
    assert sim.time_s == pytest.approx(10.0, abs=sim.dt_s)

    start = sim.time_s
    for _ in range(30):    # 1 real second at 600x
        debt = advance(sim, speed=600, frame_s=1 / 30, tick_debt=debt)
    assert sim.time_s - start == pytest.approx(600.0, abs=sim.dt_s)


def test_station_speed_keys_skip_the_oven_and_stay_in_range():
    factory = Factory.from_config()
    selectable = adjustable_stations(factory)
    assert ("line1", "pnp") in selectable and ("line1", "reflow") not in selectable
    for _ in range(30):
        change_station_speed(factory, ("line1", "pnp"), -5)
    assert factory.lines["line1"].station("pnp").speed_pct == 5
    for _ in range(30):
        change_station_speed(factory, ("line1", "pnp"), +5)
    assert factory.lines["line1"].station("pnp").speed_pct == 100


def test_simulation_core_never_imports_pygame():
    code = ("import sys; from smt_sim import Simulator; from smt_sim import metrics, export, factory, scenarios; "
            "Simulator().run(60); print('pygame' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert out.stdout.strip() == "False"
