"""Load shapes. A shape drives the user count over time instead of ``--users``.

Why a shape rather than a longer ramp:

``--users 300 --spawn-rate 5`` reaches 300 users in a minute and holds there.
That is a *campus* — a large population arriving gradually and clicking
independently. It is not a *classroom*, which is the shape that actually
threatens this platform: an instructor says "ask the assistant about X" and 250
to 300 students submit inside about thirty seconds. The infrastructure sees a
near-vertical edge, and the things that break on an edge (ALB connection surge,
Fargate scaling from a cold 2 tasks with 60s cooldowns, Bedrock TPM measured
per minute) do not break on a ramp of the same height.

Locust ignores ``--users`` and ``--spawn-rate`` when a shape is present.
"""

from __future__ import annotations

import os

from locust import LoadTestShape


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value}")
    return value


class ClassroomBurstShape(LoadTestShape):
    """One lecture hall, twice.

    ======================  ==========================================
    phase                   what it represents
    ======================  ==========================================
    baseline                ambient campus traffic before class
    spike                   the instructor's instruction lands
    hold                    everyone working through their answer
    drain                   back to ambient
    second spike            the next class, on warmed infrastructure
    ======================  ==========================================

    The second spike is the point of the whole shape. The first one hits cold:
    two Fargate tasks, an empty prompt cache, no scaled-out capacity. If the
    platform only survives the second, then it survives a class *following*
    another class and not the 9am one — which is a materially different answer
    and one a single-spike test cannot distinguish.

    Tunable with ``AGENTCORE_LOAD_BURST_*`` env vars; defaults model a
    300-student section.
    """

    def __init__(self) -> None:
        super().__init__()
        self.baseline_users = _int_env("AGENTCORE_LOAD_BURST_BASELINE_USERS", 20)
        self.burst_users = _int_env("AGENTCORE_LOAD_BURST_USERS", 300)
        self.burst_seconds = _int_env("AGENTCORE_LOAD_BURST_SECONDS", 30)
        self.hold_seconds = _int_env("AGENTCORE_LOAD_BURST_HOLD_SECONDS", 240)
        self.drain_seconds = _int_env("AGENTCORE_LOAD_BURST_DRAIN_SECONDS", 120)
        self.bursts = _int_env("AGENTCORE_LOAD_BURST_COUNT", 2)
        self.warmup_seconds = _int_env("AGENTCORE_LOAD_BURST_WARMUP_SECONDS", 60)

        if self.burst_users < self.baseline_users:
            raise ValueError(
                f"AGENTCORE_LOAD_BURST_USERS ({self.burst_users}) must be >= "
                f"AGENTCORE_LOAD_BURST_BASELINE_USERS ({self.baseline_users})."
            )
        if self.burst_seconds < 1:
            raise ValueError("AGENTCORE_LOAD_BURST_SECONDS must be >= 1.")

        # Spawn fast enough to clear the gap inside the burst window. This is
        # the number that makes it a burst rather than a ramp; at the defaults
        # it is (300-20)/30 = ~10 users/second, each doing a full OAuth login.
        self._spawn_rate = max(
            1, round((self.burst_users - self.baseline_users) / self.burst_seconds)
        )
        self._cycle_seconds = self.burst_seconds + self.hold_seconds + self.drain_seconds

    @property
    def total_seconds(self) -> int:
        """Full run length, so an operator can sanity-check cost before starting."""
        return self.warmup_seconds + self.bursts * self._cycle_seconds

    def tick(self) -> tuple[int, float] | None:
        run_time = self.get_run_time()

        if run_time >= self.total_seconds:
            return None

        if run_time < self.warmup_seconds:
            # Baseline first: a cold ALB and an unwarmed prompt cache would
            # otherwise be attributed to the spike.
            return self.baseline_users, max(1, self.baseline_users // 10)

        offset = (run_time - self.warmup_seconds) % self._cycle_seconds

        if offset < self.burst_seconds:
            return self.burst_users, self._spawn_rate
        if offset < self.burst_seconds + self.hold_seconds:
            return self.burst_users, self._spawn_rate
        return self.baseline_users, self._spawn_rate
