"""Regression tests for local conversation-session storage."""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from session import (  # noqa: E402
    SessionLoadError,
    load_session_messages,
    save_session_messages,
    session_path,
)


class SessionStorageTests(unittest.TestCase):
    def test_session_path_uses_expected_data_filename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)

            self.assertEqual(
                session_path(project_root),
                project_root / "data" / "current_session.json",
            )

    def test_missing_session_returns_none(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            data_directory = project_root / "data"
            expected_session_path = data_directory / "current_session.json"

            self.assertFalse(data_directory.exists())
            self.assertFalse(expected_session_path.exists())
            self.assertIsNone(load_session_messages(project_root))
            self.assertFalse(data_directory.exists())
            self.assertFalse(expected_session_path.exists())

    def test_read_error_raises_and_preserves_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "current_session.json"
            path.mkdir(parents=True)

            with self.assertRaises(SessionLoadError) as raised:
                load_session_messages(temp_dir)

            self.assertIn("Could not read", str(raised.exception))
            self.assertIn("The file was not changed.", str(raised.exception))
            self.assertTrue(path.is_dir())

    def test_save_and_load_round_trip_with_unicode(self):
        messages = [
            {"role": "user", "content": "Hello, Max 👋"},
            {"role": "assistant", "content": "こんにちは — I received it."},
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            save_session_messages(temp_dir, messages)

            self.assertEqual(load_session_messages(temp_dir), messages)
            self.assertIn(
                "こんにちは",
                session_path(temp_dir).read_text(encoding="utf-8"),
            )

    def assert_failed_load_preserves_file(self, file_bytes, error_fragment):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = session_path(temp_dir)
            path.parent.mkdir(parents=True)
            path.write_bytes(file_bytes)

            with self.assertRaises(SessionLoadError) as raised:
                load_session_messages(temp_dir)

            self.assertIn(error_fragment, str(raised.exception))
            self.assertIn("The file was not changed.", str(raised.exception))
            self.assertEqual(path.read_bytes(), file_bytes)

    def test_malformed_json_raises_and_preserves_file(self):
        self.assert_failed_load_preserves_file(
            b'{"messages": [}',
            "is not valid JSON",
        )

    def test_invalid_utf8_raises_and_preserves_file(self):
        self.assert_failed_load_preserves_file(
            b"\xff\xfe",
            "cannot be decoded as UTF-8",
        )

    def test_invalid_session_structures_raise_and_preserve_file(self):
        invalid_sessions = {
            "top level is not an object": (
                b"[]",
                "is missing a messages list",
            ),
            "messages is missing": (
                b"{}",
                "is missing a messages list",
            ),
            "messages is not a list": (
                b'{"messages": {}}',
                "is missing a messages list",
            ),
            "message is not an object": (
                b'{"messages": ["hello"]}',
                "has an invalid message",
            ),
            "role is invalid": (
                b'{"messages": [{"role": "system", "content": "x"}]}',
                "has an invalid message",
            ),
            "content is not text": (
                b'{"messages": [{"role": "user", "content": 1}]}',
                "has an invalid message",
            ),
        }

        for description, (file_bytes, error_fragment) in invalid_sessions.items():
            with self.subTest(description=description):
                self.assert_failed_load_preserves_file(file_bytes, error_fragment)


if __name__ == "__main__":
    unittest.main()
