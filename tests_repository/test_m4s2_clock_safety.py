"""Injected-clock tests for the fail-closed clock-safety probe."""

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
PRODUCTION_DATABASE = PROJECT_ROOT / "data" / "max_smart.sqlite3"
SESSION_FILE = PROJECT_ROOT / "data" / "current_session.json"
sys.path.insert(0, str(SRC_ROOT))

from max_smart_db.application_validation import SIGNED_INT64_MAX  # noqa: E402
from max_smart_db.clock_safety import (  # noqa: E402
    ClockSampleAccepted,
    ClockSampleRejected,
    MonotonicNanosecondClock,
    UtcMicrosecondClock,
    evaluate_clock_sample,
    sample_clock_safety,
)
from max_smart_db.config import (  # noqa: E402
    EXECUTION_DEADLINE_US,
    FIXED_LEASE_US,
    QUEUED_RESERVATION_US,
    RECOVERY_CLOCK_SKEW_MARGIN_US,
)


MARGIN = RECOVERY_CLOCK_SKEW_MARGIN_US


class ScriptedClock:
    def __init__(self, values, method):
        self.values = list(values)
        self.method = method
        self.calls = 0

    def _next(self):
        self.calls += 1
        if not self.values:
            raise RuntimeError("clock exhausted")
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def now_us(self):
        if self.method != "wall":
            raise RuntimeError("wrong clock")
        return self._next()

    def now_ns(self):
        if self.method != "monotonic":
            raise RuntimeError("wrong clock")
        return self._next()


class ClockSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session_stat = SESSION_FILE.stat()

    @classmethod
    def tearDownClass(cls):
        current = SESSION_FILE.stat()
        if (
            current.st_ino != cls.session_stat.st_ino
            or current.st_size != cls.session_stat.st_size
            or current.st_mtime_ns != cls.session_stat.st_mtime_ns
        ):
            raise AssertionError("terminal session file changed")
        if PRODUCTION_DATABASE.exists():
            raise AssertionError("production database created")

    def test_wall_clock_converts_nanoseconds_to_microseconds(self):
        clock = UtcMicrosecondClock(time_ns=lambda: 1_500_000)
        self.assertEqual(clock.now_us(), 1_500)

    def test_monotonic_clock_returns_the_injected_reading(self):
        clock = MonotonicNanosecondClock(monotonic_ns=lambda: 4_000)
        self.assertEqual(clock.now_ns(), 4_000)

    def test_normal_progression_passes(self):
        result = sample_clock_safety(
            ScriptedClock((1_000_000, 2_000_000), "wall"),
            ScriptedClock((5_000_000_000, 6_000_000_000), "monotonic"),
        )
        self.assertEqual(
            result,
            ClockSampleAccepted(
                wall_elapsed_us=1_000_000,
                monotonic_elapsed_us=1_000_000,
            ),
        )

    def test_backward_wall_step_fails_closed(self):
        result = evaluate_clock_sample(2_000_000, 5_000, 1_999_999, 6_000)
        self.assertEqual(result.code, "wall_regressed")
        self.assertEqual(result.wall_elapsed_us, -1)
        self.assertNotIsInstance(result, ClockSampleAccepted)

    def test_forward_step_at_the_margin_fails_closed(self):
        result = evaluate_clock_sample(0, 0, MARGIN, 0)
        self.assertEqual(result.code, "disagreement")
        self.assertEqual(result.wall_elapsed_us, MARGIN)
        self.assertEqual(result.monotonic_elapsed_us, 0)

    def test_forward_step_inside_the_margin_passes(self):
        result = evaluate_clock_sample(0, 0, MARGIN - 1, 0)
        self.assertEqual(
            result,
            ClockSampleAccepted(
                wall_elapsed_us=MARGIN - 1,
                monotonic_elapsed_us=0,
            ),
        )

    def test_monotonic_lead_at_the_margin_fails_closed(self):
        result = evaluate_clock_sample(0, 0, 0, MARGIN * 1_000)
        self.assertEqual(result.code, "disagreement")
        self.assertEqual(result.monotonic_elapsed_us, MARGIN)

    def test_monotonic_regression_fails_closed(self):
        result = evaluate_clock_sample(1_000, 5_000, 1_001, 4_999)
        self.assertEqual(result.code, "monotonic_regressed")
        self.assertLess(result.monotonic_elapsed_us, 0)

    def test_invalid_timestamps_fail_closed(self):
        cases = (
            ("wall_invalid", None, 0, 1, 1),
            ("wall_invalid", 1.5, 0, 2, 1),
            ("wall_invalid", True, 0, 1, 1),
            ("wall_invalid", -1, 0, 0, 1),
            ("wall_invalid", SIGNED_INT64_MAX + 1, 0, SIGNED_INT64_MAX + 2, 1),
            ("monotonic_invalid", 0, None, 1, 1),
            ("monotonic_invalid", 0, False, 1, 1),
            ("monotonic_invalid", 0, -1, 1, 0),
            ("monotonic_invalid", 0, SIGNED_INT64_MAX + 1, 1, SIGNED_INT64_MAX + 1),
        )
        for code, wall_start, mono_start, wall_end, mono_end in cases:
            with self.subTest(code=code, wall_start=wall_start, mono_start=mono_start):
                result = evaluate_clock_sample(
                    wall_start,
                    mono_start,
                    wall_end,
                    mono_end,
                )
                self.assertIsInstance(result, ClockSampleRejected)
                self.assertEqual(result.code, code)
                self.assertIsNone(result.wall_elapsed_us)

    def test_injected_clock_failure_has_no_exception_text(self):
        result = sample_clock_safety(
            ScriptedClock((RuntimeError("secret path /tmp/key"),), "wall"),
            ScriptedClock((0, 1), "monotonic"),
        )
        self.assertEqual(result, ClockSampleRejected("clock_failed"))
        self.assertNotIn("secret", repr(result))

    def test_second_reading_failure_fails_closed(self):
        result = sample_clock_safety(
            ScriptedClock((10, RuntimeError("later")), "wall"),
            ScriptedClock((0, 1), "monotonic"),
        )
        self.assertEqual(result.code, "clock_failed")

    def test_deadline_equality_is_expired_and_lease_remains_live(self):
        claimed_at = 1_000_000
        deadline = claimed_at + EXECUTION_DEADLINE_US
        lease = claimed_at + FIXED_LEASE_US
        now = deadline
        self.assertEqual(lease - deadline, RECOVERY_CLOCK_SKEW_MARGIN_US)
        self.assertEqual(QUEUED_RESERVATION_US, 60_000_000)
        self.assertFalse(deadline > now)
        self.assertTrue(lease > now)
        self.assertEqual(EXECUTION_DEADLINE_US, 600_000_000)
        self.assertEqual(FIXED_LEASE_US, 720_000_000)


if __name__ == "__main__":
    unittest.main()
