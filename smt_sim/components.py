"""Consumables and wear parts: solder paste, stencil, feeders, nozzles, operator.

Each class only tracks its own physical state and turns it into a measured
quantity (grams, % transfer loss, µm offset). The stations decide what that
means for the line (stop, slow down, record on the board).
"""

import math
from typing import Any

import numpy as np


class Operator:
    """The person who answers machine calls (add paste, splice a reel).

    Response time is lognormal: usually quick, occasionally long. For now
    every call gets an independent response; a single busy operator shared by
    the whole line could be modeled here later.
    """

    def __init__(self, cfg: dict[str, Any], rng: np.random.Generator) -> None:
        self.response_median_s = float(cfg["response_median_s"])
        self.response_sigma = float(cfg["response_sigma"])
        self.rng = rng

    def response_time_s(self) -> float:
        return float(self.rng.lognormal(math.log(self.response_median_s), self.response_sigma))


class SolderPaste:
    """Paste sitting on the stencil.

    Each print uses a normal-distributed amount. Paste also dries out once on
    the stencil: after `open_time_grace_h` its transfer efficiency drops by
    `open_time_loss_pct_per_h` every further hour. Adding fresh paste resets
    the open time (simplification: in reality old and new paste mix).
    """

    def __init__(self, cfg: dict[str, Any], rng: np.random.Generator) -> None:
        self.grams = float(cfg["initial_g"])
        self.per_print_mean_g = float(cfg["per_print_mean_g"])
        self.per_print_sd_g = float(cfg["per_print_sd_g"])
        self.low_limit_g = float(cfg["low_limit_g"])
        self.refill_g = float(cfg["refill_g"])
        self.refill_time_s = float(cfg["refill_time_s"])
        self.open_time_grace_h = float(cfg["open_time_grace_h"])
        self.open_time_loss_pct_per_h = float(cfg["open_time_loss_pct_per_h"])
        self.added_at_s = 0.0
        self.rng = rng

    def is_low(self) -> bool:
        return self.grams < self.low_limit_g

    def consume_print(self) -> float:
        used_g = max(0.0, self.rng.normal(self.per_print_mean_g, self.per_print_sd_g))
        self.grams -= used_g
        return used_g

    def open_time_h(self, now_s: float) -> float:
        return (now_s - self.added_at_s) / 3600.0

    def drying_loss_pct(self, now_s: float) -> float:
        overdue_h = max(0.0, self.open_time_h(now_s) - self.open_time_grace_h)
        return self.open_time_loss_pct_per_h * overdue_h

    def refill(self, added_at_s: float) -> None:
        self.grams += self.refill_g
        self.added_at_s = added_at_s


class Stencil:
    """The stencil: aperture clogging (recoverable) and wear (permanent).

    clogging: paste residue builds up under the stencil with every print,
              so less paste is released. Wiping removes it.
    wear:     apertures slowly deform over the stencil's life, a small
              permanent downward drift in transfer.

    Wear is accumulated print by print (not computed as rate × total cycles),
    so if the wear rate changes later (e.g. the stencil gets damaged), only
    the prints after the change wear faster.
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.wipe_every_prints = int(cfg["wipe_every_prints"])
        self.wipe_time_s = float(cfg["wipe_time_s"])
        self.clog_loss_pct_per_print = float(cfg["clog_loss_pct_per_print"])
        self.wear_loss_pct_per_1k_cycles = float(cfg["wear_loss_pct_per_1k_cycles"])
        self.cycle_count = int(cfg.get("initial_cycles", 0))
        self.wear_loss_pct = self.wear_loss_pct_per_1k_cycles * self.cycle_count / 1000.0
        self.prints_since_wipe = 0

    def wipe_due(self) -> bool:
        return self.prints_since_wipe >= self.wipe_every_prints

    def transfer_loss_pct(self) -> float:
        clog = self.clog_loss_pct_per_print * self.prints_since_wipe
        return clog + self.wear_loss_pct

    def record_print(self) -> None:
        self.prints_since_wipe += 1
        self.cycle_count += 1
        self.wear_loss_pct += self.wear_loss_pct_per_1k_cycles / 1000.0

    def wipe(self) -> None:
        self.prints_since_wipe = 0


class Feeder:
    """A feeder holding a reel of a single part number.

    It is mounted in a machine slot, or parked (slot None) after a changeover
    removed it. A parked feeder keeps its partly used reel, so mounting it
    again later brings back the same quantity.
    """

    def __init__(self, part_number: str, cfg: dict[str, Any]) -> None:
        self.slot: int | None = None
        self.part_number = part_number
        self.reel_qty = int(cfg["reel_qty"])
        self.qty = int(cfg.get("initial_qty", self.reel_qty))
        self.mispick_rate = float(cfg["mispick_rate"])
        self.reels_loaded = 0   # new reels added (spliced or after running empty)
        # Low-reel warning handling:
        self.operator_called = False
        self.splice_at_s: float | None = None   # when the operator will splice; None = not coming

    def splice(self) -> None:
        """Join a new reel onto the running one: no stop needed."""
        self.qty += self.reel_qty
        self.reels_loaded += 1
        self._clear_call()

    def load_new_reel(self) -> None:
        """Replace an empty reel (machine was stopped)."""
        self.qty = self.reel_qty
        self.reels_loaded += 1
        self._clear_call()

    def unmount(self) -> None:
        """Take the feeder off the machine. A pending splice call is cancelled."""
        self.slot = None
        self._clear_call()

    def _clear_call(self) -> None:
        self.operator_called = False
        self.splice_at_s = None


class Nozzle:
    """A pick-and-place nozzle that wears and can clog.

    Clogging is a discrete event, so it uses a hazard that rises with picks
    since the last cleaning (Weibull, shape > 1). Wear and clogs show up in
    two measurable ways: a wider spread of placement offsets (µm) and a
    higher chance that a pick fails (part dropped or not picked up).
    """

    def __init__(self, nozzle_id: int, cfg: dict[str, Any], rng: np.random.Generator) -> None:
        self.nozzle_id = nozzle_id
        self.beta = float(cfg["clog_shape"])
        self.eta = float(cfg["clog_scale_picks"])
        self.base_offset_sd_um = float(cfg["base_offset_sd_um"])
        self.wear_offset_sd_um = float(cfg["wear_offset_sd_um"])
        self.clog_offset_sd_um = float(cfg["clog_offset_sd_um"])
        self.base_pick_fail_prob = float(cfg["base_pick_fail_prob"])
        self.clog_pick_fail_prob = float(cfg["clog_pick_fail_prob"])
        self.rng = rng

        self.picks_since_clean = 0
        self.clogged = False
        self.consecutive_fails = 0   # what the machine's vision check counts

    def hazard(self) -> float:
        """Probability of clogging on the next pick."""
        n = self.picks_since_clean
        return (self.beta / self.eta) * (n / self.eta) ** (self.beta - 1)

    def pick(self, offset_scale: float = 1.0, fail_scale: float = 1.0) -> tuple[float, bool]:
        """One pick. Returns (placement offset in µm, pick failed?).

        offset_scale / fail_scale come from the head speed (1.0 at rated
        speed, smaller when slowed down). They scale the normal and wear-related
        spread and the normal failure rate, but not the clog's extra spread
        or failures: slowing the head down doesn't unclog a nozzle.
        """
        self.picks_since_clean += 1
        if not self.clogged and self.rng.random() < self.hazard():
            self.clogged = True
        wear = self.picks_since_clean / self.eta
        sigma_um = ((self.base_offset_sd_um + self.wear_offset_sd_um * wear) * offset_scale
                    + (self.clog_offset_sd_um if self.clogged else 0.0))
        offset_um = float(self.rng.normal(0.0, sigma_um))
        fail_prob = self.clog_pick_fail_prob if self.clogged else self.base_pick_fail_prob * fail_scale
        pick_failed = bool(self.rng.random() < fail_prob)
        return offset_um, pick_failed

    def clean(self) -> None:
        self.picks_since_clean = 0
        self.clogged = False
        self.consecutive_fails = 0
