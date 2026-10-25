"""Regression tests for Max Smart's standing-context assembly."""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from context import build_standing_context  # noqa: E402


class StandingContextTests(unittest.TestCase):
    def test_builds_context_in_original_order_and_format(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            docs = project_root / "docs"
            docs.mkdir()

            instructions = "\n  INSTRUCTIONS\nsecond line  \n"
            goals = "  GOALS\nsecond line\n\n"
            memory = "\tMEMORY\nsecond line \n"

            (docs / "max-smart-instructions.md").write_text(
                instructions, encoding="utf-8"
            )
            (docs / "goals.md").write_text(goals, encoding="utf-8")
            memory_path = docs / "memory.md"
            memory_path.write_text(memory, encoding="utf-8")
            memory_before = memory_path.read_bytes()

            expected = (
                "=== Max Smart's standing instructions ===\n\n"
                + instructions
                + "\n\n---\n\n"
                + "=== Adam's goals ===\n\n"
                + goals
                + "\n\n---\n\n"
                + "=== Max Smart's long-term memory ===\n\n"
                + "This memory is file-based, transparent, and edited only by Adam. "
                + "You may use it as context. You must not modify docs/memory.md "
                + "or treat yourself as able to write memory.\n\n"
                + memory
            )

            self.assertEqual(build_standing_context(project_root), expected)
            self.assertEqual(memory_path.read_bytes(), memory_before)


if __name__ == "__main__":
    unittest.main()
