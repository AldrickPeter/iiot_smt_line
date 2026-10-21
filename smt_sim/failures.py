"""Random breakdowns: when a station fails, and how long the repair takes.

A FailureMode is one way a station can break (a conveyor jam, a servo fault).
It has two random parts:

Time to failure, measured in RUNNING time only (a starved machine doesn't wear):
  - "exponential": constant hazard 1/mtbf. Memoryless random faults, e.g. jams.
  - "weibull" with shape > 1: hazard rises with age, i.e. wear-out. After a
    repair the part is assumed as good as new, so age restarts at 0.

Repair time:
  - lognormal: always positive and right-skewed. Most repairs are quick, a
    few take much longer. Parameterised by its median and log-space sigma.

Why we sample a time-to-failure instead of rolling the dice every tick:
for a hazard h(t), the probability of surviving to age t is
S(t) = exp(-∫h). Drawing the failure age directly from that distribution
gives exactly the same behavior as a per-tick hazard check, without one
random draw per tick. `hazard()` is kept so the model can be inspected.
"""

import math
from typing import Any

import numpy as np

from smt_sim.events import ALL_REASON_CODES


class FailureMode:
    def __init__(self, cfg: dict[str, Any], rng: np.random.Generator) -> None:
        self.reason_code: str = cfg["reason"]
        if self.reason_code not in ALL_REASON_CODES:
            raise ValueError(f"Unknown reason code {self.reason_code!r}; add it to ReasonCode")

        self.distribution: str = cfg["distribution"]
        if self.distribution == "exponential":
            self.mtbf_s = float(cfg["mtbf_s"])
        elif self.distribution == "weibull":
            self.shape = float(cfg["shape"])      # beta; > 1 means wear-out
            self.scale_s = float(cfg["scale_s"])  # eta; ~63% fail before this age
        else:
            raise ValueError(f"Unknown failure distribution {self.distribution!r}")

        self.repair_median_s = float(cfg["repair_median_s"])
        self.repair_sigma = float(cfg["repair_sigma"])

        self.rng = rng
        self.age_s = 0.0   # running time since the last repair
        self.time_to_failure_s = self._sample_time_to_failure()
        self.failure_count = 0

    def hazard(self, age_s: float) -> float:
        """Instantaneous failure rate (per second) at a given running age."""
        if self.distribution == "exponential":
            return 1.0 / self.mtbf_s
        beta, eta = self.shape, self.scale_s
        return (beta / eta) * (age_s / eta) ** (beta - 1)

    def advance(self, dt_s: float) -> bool:
        """Add running time. Returns True if the station fails during this step."""
        self.age_s += dt_s
        if self.age_s < self.time_to_failure_s:
            return False
        self.failure_count += 1
        self.age_s = 0.0   # repair restores the part to as-good-as-new
        self.time_to_failure_s = self._sample_time_to_failure()
        return True

    def sample_repair_s(self) -> float:
        # numpy's lognormal takes the mean of log(X); log(median) is exactly that.
        return float(self.rng.lognormal(mean=math.log(self.repair_median_s), sigma=self.repair_sigma))

    def _sample_time_to_failure(self) -> float:
        if self.distribution == "exponential":
            return float(self.rng.exponential(self.mtbf_s))
        # numpy's weibull has scale 1, so stretch it by eta.
        return float(self.scale_s * self.rng.weibull(self.shape))
