"""Fail-closed comparison of one wall-clock sample and one monotonic sample.

The probe does not sleep, open a database, or authorize production startup.
The accepted safety margin is the maximum permitted disagreement, and an
equal disagreement is rejected.
"""

import time
from dataclasses import dataclass

from max_smart_db.application_validation import SIGNED_INT64_MAX
from max_smart_db.config import RECOVERY_CLOCK_SKEW_MARGIN_US


class UtcMicrosecondClock:
    """UTC wall clock in integer microseconds. It satisfies the repository Clock."""

    def __init__(self, time_ns=time.time_ns):
        self._time_ns = time_ns

    def now_us(self):
        return self._time_ns() // 1_000


class MonotonicNanosecondClock:
    """Independent monotonic clock in integer nanoseconds."""

    def __init__(self, monotonic_ns=time.monotonic_ns):
        self._monotonic_ns = monotonic_ns

    def now_ns(self):
        return self._monotonic_ns()


@dataclass(frozen=True)
class ClockSampleAccepted:
    """The two observations stayed inside the accepted margin."""

    wall_elapsed_us: int
    monotonic_elapsed_us: int


@dataclass(frozen=True)
class ClockSampleRejected:
    """The sample cannot authorize timing. It carries no exception text."""

    code: str
    wall_elapsed_us: int | None = None
    monotonic_elapsed_us: int | None = None


def sample_clock_safety(wall_clock, monotonic_clock):
    """Read each clock twice and apply the fail-closed predicates."""

    try:
        wall_start = wall_clock.now_us()
        monotonic_start = monotonic_clock.now_ns()
        wall_end = wall_clock.now_us()
        monotonic_end = monotonic_clock.now_ns()
    except Exception:
        return ClockSampleRejected("clock_failed")
    return evaluate_clock_sample(
        wall_start,
        monotonic_start,
        wall_end,
        monotonic_end,
    )


def evaluate_clock_sample(wall_start, monotonic_start, wall_end, monotonic_end):
    """Judge four already-read observations without reading a clock again."""

    if not _valid_timestamp(wall_start) or not _valid_timestamp(wall_end):
        return ClockSampleRejected("wall_invalid")
    if not _valid_timestamp(monotonic_start) or not _valid_timestamp(monotonic_end):
        return ClockSampleRejected("monotonic_invalid")
    wall_elapsed = wall_end - wall_start
    monotonic_elapsed = (monotonic_end - monotonic_start) // 1_000
    if wall_elapsed < 0:
        return ClockSampleRejected(
            "wall_regressed",
            wall_elapsed_us=wall_elapsed,
            monotonic_elapsed_us=monotonic_elapsed,
        )
    if monotonic_end < monotonic_start:
        return ClockSampleRejected(
            "monotonic_regressed",
            wall_elapsed_us=wall_elapsed,
            monotonic_elapsed_us=monotonic_elapsed,
        )
    disagreement = abs(wall_elapsed - monotonic_elapsed)
    if disagreement >= RECOVERY_CLOCK_SKEW_MARGIN_US:
        return ClockSampleRejected(
            "disagreement",
            wall_elapsed_us=wall_elapsed,
            monotonic_elapsed_us=monotonic_elapsed,
        )
    return ClockSampleAccepted(
        wall_elapsed_us=wall_elapsed,
        monotonic_elapsed_us=monotonic_elapsed,
    )


def _valid_timestamp(value):
    return type(value) is int and 0 <= value <= SIGNED_INT64_MAX
