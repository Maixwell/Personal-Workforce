"""Read-only production preflight. The account conflict fails closed."""

import os
import pwd
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from max_smart_db.config import PRODUCTION_DATABASE_PATH  # noqa: E402
from max_smart_db.safety import DatabaseSafetyError  # noqa: E402
from production_preflight import run_production_preflight  # noqa: E402


_UNTOUCHED = (
    PRODUCTION_DATABASE_PATH,
    PRODUCTION_DATABASE_PATH.parent,
    Path(f"{PRODUCTION_DATABASE_PATH}.initialization-state"),
    Path(f"{PRODUCTION_DATABASE_PATH}.initialization-state.tmp"),
    PROJECT_ROOT / "data" / "provider.fence",
    PROJECT_ROOT / "data" / "current_session.json",
    Path("/Users/smart/Library/Application Support/MaxSmartTrust"),
)


class ProductionPreflightTests(unittest.TestCase):
    def test_account_path_mismatch_fails_closed_without_mutation(self):
        before = {path: _metadata(path) for path in _UNTOUCHED}
        identity = pwd.getpwuid(os.geteuid())
        opened = []
        real_open = os.open

        def spy(path, flags, mode=0o777, **kwargs):
            opened.append(os.fspath(path))
            return real_open(path, flags, mode, **kwargs)

        with mock.patch("os.open", side_effect=spy):
            report = run_production_preflight()
        after = {path: _metadata(path) for path in _UNTOUCHED}
        self.assertEqual(before, after)
        self.assertFalse(
            any(_sensitive(path) for path in opened),
            opened,
        )
        self.assertEqual(report.effective_uid, identity.pw_uid)
        self.assertEqual(report.effective_user, identity.pw_name)
        self.assertEqual(report.process_home, identity.pw_dir)
        self.assertEqual(report.project_root, str(PROJECT_ROOT))
        self.assertEqual(report.project_home, "/Users/Max")
        self.assertEqual(
            report.pin_directory,
            "/Users/smart/Library/Application Support/MaxSmartTrust",
        )
        self.assertEqual(report.pin_home, "/Users/smart")
        self.assertTrue(report.account_path_conflict)
        self.assertFalse(report.passed)
        self.assertEqual(report.filesystem_code, "local_apfs")
        self.assertEqual(report.git_code, "ignored")
        self.assertIn(
            report.data_directory_state,
            {"absent", "private_directory", "unsafe_directory_permissions", "unsafe_not_directory"},
        )
        self.assertIn(report.database_state, {"absent", "private_file", "unsafe_file_permissions", "unsafe_not_file"})
        self.assertIn(report.marker_state, {"absent", "private_file", "unsafe_file_permissions", "unsafe_not_file"})
        self.assertIn(
            report.temporary_marker_state,
            {"absent", "private_file", "unsafe_file_permissions", "unsafe_not_file"},
        )
        repository = PROJECT_ROOT.lstat()
        self.assertEqual(report.repository_owner, repository.st_uid)
        self.assertEqual(report.repository_mode, stat.S_IMODE(repository.st_mode))
        self.assertFalse(report.repository_symlink)
        self.assertFalse(stat.S_ISLNK(repository.st_mode))
        self.assertEqual(report.path_identity, "established")

    def test_wrong_owner_mode_symlink_and_hard_link_fail_closed(self):
        root = _private_project()
        self.addCleanup(shutil.rmtree, root, True)
        data = root / "data"
        data.mkdir(mode=0o700)
        os.chmod(data, 0o755)
        report = _aligned_preflight(root, data / "max_smart.sqlite3")
        self.assertEqual(report.data_directory_state, "unsafe_directory_permissions")
        self.assertFalse(report.account_path_conflict)
        self.assertFalse(report.passed)
        os.chmod(data, 0o700)

        with mock.patch("production_preflight.os.geteuid", return_value=1):
            owned = _aligned_preflight(root, data / "max_smart.sqlite3")
        self.assertEqual(owned.path_identity, "repository_owner")
        self.assertEqual(owned.data_directory_state, "unsafe_directory_permissions")
        self.assertFalse(owned.passed)

        target = root / "target"
        target.mkdir(mode=0o700)
        linked = root / "linked-data"
        linked.symlink_to(target, target_is_directory=True)
        symlink_report = _aligned_preflight(root, linked / "max_smart.sqlite3")
        self.assertEqual(symlink_report.data_directory_state, "unsafe_not_directory")
        self.assertFalse(symlink_report.passed)

        database = data / "max_smart.sqlite3"
        _private_file(database)
        os.link(database, data / "max_smart-link.sqlite3")
        self.assertGreater(database.stat().st_nlink, 1)
        linked_file = _aligned_preflight(root, database)
        self.assertEqual(linked_file.database_state, "unsafe_file_permissions")
        self.assertFalse(linked_file.passed)

        repository_link = root / "repository-link"
        repository_link.symlink_to(root, target_is_directory=True)
        repository = _aligned_preflight(repository_link, database)
        self.assertTrue(repository.repository_symlink)
        self.assertEqual(repository.path_identity, "path_symlink")
        self.assertFalse(repository.passed)

    def test_filesystem_git_and_identity_failures_fail_closed(self):
        root = _private_project()
        self.addCleanup(shutil.rmtree, root, True)
        database = root / "data" / "max_smart.sqlite3"
        filesystem = _aligned_preflight(
            root,
            database,
            filesystem_error=DatabaseSafetyError(
                "filesystem_not_local_apfs",
                "The production database requires a local APFS filesystem.",
            ),
        )
        self.assertEqual(filesystem.filesystem_code, "filesystem_not_local_apfs")
        self.assertFalse(filesystem.passed)
        unsupported = _aligned_preflight(
            root,
            database,
            filesystem_error=DatabaseSafetyError(
                "unsupported_filesystem_platform",
                "Production database filesystem verification requires macOS.",
            ),
        )
        self.assertEqual(
            unsupported.filesystem_code,
            "unsupported_filesystem_platform",
        )
        self.assertFalse(unsupported.passed)
        ignored = _aligned_preflight(
            root,
            database,
            git_error=DatabaseSafetyError(
                "git_path_not_ignored",
                "A database artifact path is not safely ignored by Git.",
            ),
        )
        self.assertEqual(ignored.git_code, "git_path_not_ignored")
        self.assertFalse(ignored.passed)
        unavailable = _aligned_preflight(
            root,
            database,
            git_error=DatabaseSafetyError(
                "git_ignore_check_failed",
                "Database Git protection could not be verified.",
            ),
        )
        self.assertEqual(unavailable.git_code, "git_ignore_check_failed")
        self.assertFalse(unavailable.passed)
        missing = _aligned_preflight(root / "missing", database)
        self.assertIsNone(missing.repository_owner)
        self.assertIsNone(missing.repository_mode)
        self.assertFalse(missing.repository_symlink)
        self.assertEqual(missing.path_identity, "filesystem_identity_unavailable")
        self.assertFalse(missing.passed)


def _private_project():
    root = Path(tempfile.mkdtemp(prefix="max-smart-preflight-")).resolve()
    os.chmod(root, 0o700)
    return root


def _private_file(path):
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
        0o600,
    )
    os.close(descriptor)
    os.chmod(path, 0o600)


def _aligned_preflight(project, database, filesystem_error=None, git_error=None):
    identity = pwd.getpwuid(os.geteuid())

    def home(_path):
        return identity.pw_dir

    def filesystem(_path=PROJECT_ROOT):
        if filesystem_error is not None:
            raise filesystem_error

    def git(_path=PROJECT_ROOT):
        if git_error is not None:
            raise git_error

    with (
        mock.patch("production_preflight._account_home", side_effect=home),
        mock.patch("production_preflight.verify_local_apfs", side_effect=filesystem),
        mock.patch("production_preflight.verify_git_protection", side_effect=git),
    ):
        return run_production_preflight(
            project_root=project,
            database_path=database,
            pin_directory=Path(identity.pw_dir),
        )


def _metadata(path):
    try:
        status = Path(path).lstat()
    except FileNotFoundError:
        return None
    return (status.st_dev, status.st_ino, status.st_mode, status.st_uid, status.st_size)


def _sensitive(path):
    text = os.fspath(path)
    return any(
        marker in text
        for marker in (
            "max_smart.sqlite3",
            "provider.fence",
            "provider-fence.pin",
            "current_session.json",
            "MaxSmartTrust",
            "Keychains",
        )
    )


if __name__ == "__main__":
    unittest.main()
