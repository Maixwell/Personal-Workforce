"""Regression tests for local conversation-session storage."""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            saved_text = session_path(temp_dir).read_text(encoding="utf-8")

            self.assertEqual(load_session_messages(temp_dir), messages)
            self.assertIn("こんにちは", saved_text)
            self.assertIn('\n  "messages": [\n', saved_text)
            self.assertTrue(saved_text.endswith("\n"))

    @unittest.skipUnless(os.name == "posix", "POSIX permission modes required")
    def test_save_enforces_private_directory_and_file_permissions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            directory = project_root / "data"
            path = directory / "current_session.json"

            original_umask = os.umask(0o277)
            try:
                save_session_messages(project_root, [])
            finally:
                os.umask(original_umask)

            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

            directory.chmod(0o777)
            path.chmod(0o666)
            save_session_messages(project_root, [])

            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_save_atomically_replaces_existing_session(self):
        old_messages = [{"role": "user", "content": "old"}]
        new_messages = [{"role": "assistant", "content": "new"}]

        with tempfile.TemporaryDirectory() as temp_dir:
            path = session_path(temp_dir)
            save_session_messages(temp_dir, old_messages)

            with patch("session.os.replace", wraps=os.replace) as replace:
                save_session_messages(temp_dir, new_messages)

            replace.assert_called_once()
            temporary_path, target_path = replace.call_args.args
            self.assertEqual(Path(temporary_path).parent, path.parent)
            self.assertEqual(Path(target_path), path)
            self.assertEqual(load_session_messages(temp_dir), new_messages)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_failed_atomic_replace_preserves_session_and_cleans_up_temp_file(self):
        old_messages = [{"role": "user", "content": "keep me"}]
        new_messages = [{"role": "assistant", "content": "do not save"}]

        with tempfile.TemporaryDirectory() as temp_dir:
            path = session_path(temp_dir)
            save_session_messages(temp_dir, old_messages)
            original_bytes = path.read_bytes()

            with patch(
                "session.os.replace",
                side_effect=OSError("simulated replacement failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated replacement failure"):
                    save_session_messages(temp_dir, new_messages)

            self.assertEqual(path.read_bytes(), original_bytes)
            self.assertEqual(
                sorted(item.name for item in path.parent.iterdir()),
                ["current_session.json"],
            )

    def test_failed_fsync_preserves_session_and_cleans_up_temp_file(self):
        old_messages = [{"role": "user", "content": "keep me"}]
        new_messages = [{"role": "assistant", "content": "do not save"}]

        with tempfile.TemporaryDirectory() as temp_dir:
            path = session_path(temp_dir)
            save_session_messages(temp_dir, old_messages)
            original_bytes = path.read_bytes()

            with patch(
                "session.os.fsync",
                side_effect=OSError("simulated fsync failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated fsync failure"):
                    save_session_messages(temp_dir, new_messages)

            self.assertEqual(path.read_bytes(), original_bytes)
            self.assertEqual(
                sorted(item.name for item in path.parent.iterdir()),
                ["current_session.json"],
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "Symbolic links not supported")
    def test_save_refuses_session_symlink_without_modifying_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            path = session_path(project_root)
            path.parent.mkdir(parents=True)
            symlink_target = project_root / "outside.json"
            original_bytes = b'{"sentinel": true}\n'
            symlink_target.write_bytes(original_bytes)

            try:
                path.symlink_to(symlink_target)
            except OSError as error:
                self.skipTest(f"Cannot create symbolic links: {error}")

            with self.assertRaisesRegex(OSError, "symbolic link"):
                save_session_messages(project_root, [])

            self.assertTrue(path.is_symlink())
            self.assertEqual(symlink_target.read_bytes(), original_bytes)
            self.assertEqual(
                sorted(item.name for item in path.parent.iterdir()),
                ["current_session.json"],
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "Symbolic links not supported")
    def test_save_refuses_data_directory_symlink_without_modifying_target(self):
        with tempfile.TemporaryDirectory() as project_temp_dir:
            with tempfile.TemporaryDirectory() as target_temp_dir:
                project_root = Path(project_temp_dir)
                data_path = project_root / "data"
                symlink_target = Path(target_temp_dir)
                existing_file = symlink_target / "existing.txt"
                original_bytes = b"leave this unchanged\n"
                existing_file.write_bytes(original_bytes)
                symlink_target.chmod(0o755)
                original_mode = stat.S_IMODE(symlink_target.stat().st_mode)

                try:
                    data_path.symlink_to(symlink_target, target_is_directory=True)
                except OSError as error:
                    self.skipTest(f"Cannot create symbolic links: {error}")

                with self.assertRaisesRegex(OSError, "symbolic link"):
                    save_session_messages(project_root, [])

                self.assertTrue(data_path.is_symlink())
                self.assertEqual(
                    stat.S_IMODE(symlink_target.stat().st_mode),
                    original_mode,
                )
                self.assertEqual(existing_file.read_bytes(), original_bytes)
                self.assertEqual(
                    sorted(path.name for path in symlink_target.iterdir()),
                    ["existing.txt"],
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
