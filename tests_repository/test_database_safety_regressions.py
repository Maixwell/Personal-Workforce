"""Regression tests for the Milestone 2.1a security corrections."""

import os
import plistlib
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from max_smart_db import safety  # noqa: E402
from max_smart_db.config import PRODUCTION_DATABASE_PATH  # noqa: E402
from max_smart_db.safety import (  # noqa: E402
    DatabaseSafetyError,
    initialize_sqlite_file,
    secure_sqlite_artifacts,
    validate_production_database_path,
    validate_test_database_path,
    verify_git_protection,
    verify_local_apfs,
    verify_production_database_before_connect,
)


def _private_directory():
    return tempfile.TemporaryDirectory(prefix="max-smart-db-test-")


def _production_layout(root):
    """Create an initialized production-shaped tree under a temporary root."""
    database_path = root / "data" / "max_smart.sqlite3"
    initialize_sqlite_file(database_path)
    return database_path


def _legacy_database(database_path):
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("CREATE TABLE legacy (value INTEGER)")
        connection.commit()
    finally:
        connection.close()
    os.chmod(database_path, 0o600)
    marker = Path(
        f"{database_path}{safety.SQLITE_INITIALIZATION_STATE_SUFFIX}"
    )
    marker.write_bytes(safety.SQLITE_INITIALIZATION_READY)
    os.chmod(marker, 0o600)


def _completed(returncode=0, stdout=b""):
    return subprocess.CompletedProcess((), returncode, stdout=stdout, stderr=b"")


_RETRY_SCRIPT = """\
import sys
import django
django.setup()
from django.db import connection
from max_smart_db.safety import DatabaseSafetyError
if len(sys.argv) > 1:
    connection.settings_dict["OPTIONS"]["init_command"] = sys.argv[1]
results = []
for attempt in range(2):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM sqlite_master")
        results.append("query-succeeded")
    except DatabaseSafetyError as error:
        results.append(error.code)
    results.append("detached" if connection.connection is None else "attached")
print(",".join(results))
"""


def _run_test_settings(script, database_path, *arguments):
    environment = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
        "MAX_SMART_TEST_DATABASE_PATH": str(database_path),
        "PYTHONPATH": str(SRC_ROOT),
    }
    return subprocess.run(
        (sys.executable, "-B", "-c", script, *arguments),
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


def _production_environment(**overrides):
    environment = {**os.environ, "PYTHONPATH": str(SRC_ROOT), **overrides}
    environment.pop("DJANGO_SETTINGS_MODULE", None)
    return environment


def _run_manage(*arguments, environment=None):
    return subprocess.run(
        (sys.executable, "-B", str(PROJECT_ROOT / "manage.py"), *arguments),
        cwd=PROJECT_ROOT,
        env=environment or _production_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


class PreConnectFileTests(unittest.TestCase):
    def setUp(self):
        directory = _private_directory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.database_path = _production_layout(self.root)

    def assert_rejected(self, code):
        with self.assertRaises(DatabaseSafetyError) as raised:
            verify_production_database_before_connect(
                self.database_path,
                self.root,
            )
        self.assertEqual(raised.exception.code, code)

    def test_valid_layout_passes(self):
        verify_production_database_before_connect(self.database_path, self.root)

    def test_missing_database_is_rejected_and_not_created(self):
        self.database_path.unlink()
        self.assert_rejected("database_initialization_incomplete")
        self.assertFalse(self.database_path.exists())

    def test_symlinked_database_is_rejected(self):
        target = self.root / "elsewhere.sqlite3"
        self.database_path.rename(target)
        self.database_path.symlink_to(target)
        self.assert_rejected("path_symlink")

    def test_symlinked_data_directory_is_rejected(self):
        real_directory = self.root / "real-data"
        (self.root / "data").rename(real_directory)
        (self.root / "data").symlink_to(real_directory)
        self.assert_rejected("path_symlink")

    def test_symlinked_sidecar_is_rejected(self):
        target = self.root / "elsewhere-wal"
        target.write_bytes(b"")
        os.chmod(target, 0o600)
        Path(f"{self.database_path}-wal").symlink_to(target)
        self.assert_rejected("database_artifact_invalid")

    def test_existing_0644_database_is_rejected(self):
        os.chmod(self.database_path, 0o644)
        self.assert_rejected("database_permissions")

    def test_existing_0644_sidecar_is_rejected(self):
        sidecar = Path(f"{self.database_path}-shm")
        sidecar.write_bytes(b"")
        os.chmod(sidecar, 0o644)
        self.assert_rejected("database_permissions")

    def test_non_0700_data_directory_is_rejected(self):
        os.chmod(self.database_path.parent, 0o755)
        self.assert_rejected("data_directory_permissions")

    def test_hard_linked_database_is_rejected(self):
        os.link(self.database_path, self.root / "copy.sqlite3")
        self.assert_rejected("database_hard_link")

    def test_foreign_owned_database_is_rejected(self):
        real_uid = os.geteuid()
        directory_uid = self.database_path.parent.lstat().st_uid
        self.assertEqual(directory_uid, real_uid)
        original_lstat = Path.lstat

        def foreign_database(path):
            status = original_lstat(path)
            if path == self.database_path:
                values = list(status)
                values[stat.ST_UID] = real_uid + 1
                return os.stat_result(values)
            return status

        with mock.patch.object(Path, "lstat", foreign_database):
            self.assert_rejected("database_owner")

    def test_foreign_owned_data_directory_is_rejected(self):
        with mock.patch.object(
            safety.os,
            "geteuid",
            return_value=os.geteuid() + 1,
        ):
            self.assert_rejected("data_directory_owner")

    def test_path_traversal_is_rejected(self):
        traversal = self.root / "data" / ".." / "data" / "max_smart.sqlite3"
        with self.assertRaises(DatabaseSafetyError) as raised:
            validate_production_database_path(traversal, self.root)
        self.assertEqual(raised.exception.code, "production_path_mismatch")

    def test_secure_artifacts_never_follows_a_sidecar_symlink(self):
        target = self.root / "outside-target"
        target.write_bytes(b"")
        os.chmod(target, 0o644)
        Path(f"{self.database_path}-wal").symlink_to(target)
        with self.assertRaises(DatabaseSafetyError) as raised:
            secure_sqlite_artifacts(self.database_path)
        self.assertEqual(raised.exception.code, "database_artifact_invalid")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_chmod_without_no_follow_support_fails_closed(self):
        with mock.patch.object(safety.os, "supports_follow_symlinks", set()):
            with self.assertRaises(DatabaseSafetyError) as raised:
                secure_sqlite_artifacts(self.database_path)
        self.assertEqual(raised.exception.code, "chmod_nofollow_unsupported")


class TestDatabaseIdentityTests(unittest.TestCase):
    @unittest.skipUnless(
        sys.platform == "darwin",
        "macOS firmlink identity regression",
    )
    def test_system_volumes_data_alias_is_rejected(self):
        alias = Path("/System/Volumes/Data" + str(PROJECT_ROOT))
        if not alias.exists() or not os.path.samefile(alias, PROJECT_ROOT):
            self.skipTest("The standard macOS Data-volume alias is unavailable.")
        database_path = alias / "data" / "max_smart.sqlite3"
        with self.assertRaises(DatabaseSafetyError) as raised:
            validate_test_database_path(database_path)
        self.assertEqual(raised.exception.code, "test_path_in_repository")

    @unittest.skipUnless(
        sys.platform == "darwin",
        "case-insensitive APFS identity regression",
    )
    def test_case_changed_project_alias_is_rejected(self):
        alias = Path(str(PROJECT_ROOT).swapcase())
        if not alias.exists() or not os.path.samefile(alias, PROJECT_ROOT):
            self.skipTest("The project filesystem is case-sensitive.")
        database_path = alias / "data" / "max_smart.sqlite3"
        with self.assertRaises(DatabaseSafetyError) as raised:
            validate_test_database_path(database_path)
        self.assertEqual(raised.exception.code, "test_path_in_repository")

    @unittest.skipUnless(
        sys.platform == "darwin",
        "case-insensitive APFS symlink-alias regression",
    )
    def test_symlink_to_case_changed_data_alias_is_rejected(self):
        alias = Path(str(PROJECT_ROOT).swapcase()) / "data"
        if not alias.exists() or not os.path.samefile(alias, PROJECT_ROOT / "data"):
            self.skipTest("The project filesystem is case-sensitive.")
        with _private_directory() as directory:
            link = Path(directory).resolve() / "aliased-data"
            link.symlink_to(alias, target_is_directory=True)
            with self.assertRaises(DatabaseSafetyError) as raised:
                validate_test_database_path(link / "probe.sqlite3")
        self.assertEqual(raised.exception.code, "test_path_in_repository")

    @unittest.skipUnless(
        sys.platform == "darwin",
        "macOS firmlink symlink-alias regression",
    )
    def test_symlink_to_system_volumes_data_alias_is_rejected(self):
        alias = Path("/System/Volumes/Data" + str(PROJECT_ROOT)) / "data"
        if not alias.exists() or not os.path.samefile(alias, PROJECT_ROOT / "data"):
            self.skipTest("The standard macOS Data-volume alias is unavailable.")
        with _private_directory() as directory:
            link = Path(directory).resolve() / "aliased-data"
            link.symlink_to(alias, target_is_directory=True)
            with self.assertRaises(DatabaseSafetyError) as raised:
                validate_test_database_path(link / "probe.sqlite3")
        self.assertEqual(raised.exception.code, "test_path_in_repository")

    def test_existing_external_database_is_accepted(self):
        with _private_directory() as directory:
            database_path = Path(directory).resolve() / "external.sqlite3"
            initialize_sqlite_file(database_path)
            self.assertEqual(
                validate_test_database_path(database_path),
                database_path,
            )

    def test_production_file_identity_is_rejected(self):
        with _private_directory() as directory:
            production = Path(directory).resolve() / "production.sqlite3"
            initialize_sqlite_file(production)
            alias = production.parent / "alias.sqlite3"
            os.link(production, alias)
            with mock.patch.object(safety, "PRODUCTION_DATABASE_PATH", production):
                with self.assertRaises(DatabaseSafetyError) as raised:
                    validate_test_database_path(alias)
            self.assertEqual(raised.exception.code, "test_path_is_production")

    def test_filesystem_identity_error_fails_closed(self):
        original_stat = Path.stat

        def unavailable(path, *args, **kwargs):
            if path == PROJECT_ROOT:
                raise PermissionError("identity unavailable")
            return original_stat(path, *args, **kwargs)

        with _private_directory() as directory:
            database_path = Path(directory).resolve() / "external.sqlite3"
            with mock.patch.object(Path, "stat", unavailable):
                with self.assertRaises(DatabaseSafetyError) as raised:
                    validate_test_database_path(database_path)
        self.assertEqual(
            raised.exception.code,
            "filesystem_identity_unavailable",
        )


class NoCreateUriTests(unittest.TestCase):
    def test_no_create_uri_refuses_a_missing_file(self):
        from max_smart_db.backend.base import no_create_uri

        with _private_directory() as directory:
            missing = Path(directory).resolve() / "space in name.sqlite3"
            uri = no_create_uri(missing)
            self.assertTrue(uri.startswith("file:///"))
            self.assertTrue(uri.endswith("?mode=rw"))
            self.assertIn("space%20in%20name", uri)
            with self.assertRaises(sqlite3.OperationalError):
                sqlite3.connect(uri, uri=True)
            self.assertFalse(missing.exists())


class RawConnectionClosureTests(unittest.TestCase):
    def test_post_open_validation_failure_closes_raw_handle(self):
        from max_smart_db.backend import base as backend

        wrapper = object.__new__(backend.DatabaseWrapper)
        wrapper.settings_dict = {"NAME": "/external/test.sqlite3"}
        wrapper.init_commands = []
        raw_connection = mock.Mock()
        raw_connection.execute.return_value.fetchone.return_value = (
            sqlite3.sqlite_version,
        )
        failure = DatabaseSafetyError(
            "journal_mode_invalid",
            "SQLite WAL mode is required.",
        )

        with mock.patch.object(
            backend,
            "verify_database_files",
        ), mock.patch.object(
            backend.sqlite_base.Database,
            "connect",
            return_value=raw_connection,
        ), mock.patch.object(
            backend.sqlite_base,
            "register_functions",
        ), mock.patch.object(
            backend,
            "verify_sqlite_pragmas",
            side_effect=failure,
        ):
            with self.assertRaises(DatabaseSafetyError) as raised:
                wrapper.get_new_connection({})

        self.assertIs(raised.exception, failure)
        raw_connection.close.assert_called_once_with()


class DjangoConnectionRejectionTests(unittest.TestCase):
    def setUp(self):
        directory = _private_directory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name).resolve()

    def run_retry(self, database_path, *arguments):
        result = _run_test_settings(_RETRY_SCRIPT, database_path, *arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_rejected_non_wal_connection_is_never_reused(self):
        database_path = self.directory / "legacy.sqlite3"
        _legacy_database(database_path)
        self.assertEqual(
            self.run_retry(database_path),
            "journal_mode_invalid,detached,journal_mode_invalid,detached",
        )
        connection = sqlite3.connect(database_path)
        try:
            journal_mode = connection.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(journal_mode, "delete")

    def test_wrong_synchronous_pragma_is_rejected_on_every_attempt(self):
        database_path = self.directory / "synchronous.sqlite3"
        initialize_sqlite_file(database_path)
        self.assertEqual(
            self.run_retry(
                database_path,
                "PRAGMA synchronous=NORMAL;PRAGMA busy_timeout=5000",
            ),
            "synchronous_invalid,detached,synchronous_invalid,detached",
        )

    def test_wrong_busy_timeout_pragma_is_rejected_on_every_attempt(self):
        database_path = self.directory / "busy.sqlite3"
        initialize_sqlite_file(database_path)
        self.assertEqual(
            self.run_retry(
                database_path,
                "PRAGMA synchronous=FULL;PRAGMA busy_timeout=1000",
            ),
            "busy_timeout_invalid,detached,busy_timeout_invalid,detached",
        )

    def test_missing_test_database_is_never_created_by_django(self):
        database_path = self.directory / "missing.sqlite3"
        self.assertEqual(
            self.run_retry(database_path),
            "database_missing,detached,database_missing,detached",
        )
        self.assertFalse(database_path.exists())

    def test_valid_database_connects(self):
        database_path = self.directory / "valid.sqlite3"
        initialize_sqlite_file(database_path)
        self.assertEqual(
            self.run_retry(database_path),
            "query-succeeded,attached,query-succeeded,attached",
        )

    def test_plain_django_sqlite_backend_is_rejected_and_detached(self):
        database_path = self.directory / "plain.sqlite3"
        initialize_sqlite_file(database_path)
        script = """\
import django
django.setup()
from django.db import connection
from django.db.backends.sqlite3.base import DatabaseWrapper
from max_smart_db.safety import DatabaseSafetyError
plain = DatabaseWrapper(
    {**connection.settings_dict, "ENGINE": "django.db.backends.sqlite3"},
    alias="plain",
)
try:
    plain.ensure_connection()
    print("connected")
except DatabaseSafetyError as error:
    print(error.code, plain.connection is None)
"""
        result = _run_test_settings(script, database_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "unsupported_database True")


class MissingProductionDatabaseTests(unittest.TestCase):
    def setUp(self):
        if PRODUCTION_DATABASE_PATH.exists():
            self.skipTest("Production database exists; absence test not applicable.")

    def test_bootstrapped_connection_never_creates_production_database(self):
        script = """\
from max_smart_db.bootstrap import setup_database_django
setup_database_django()
from django.db import connection
from max_smart_db.safety import DatabaseSafetyError
for attempt in range(2):
    try:
        connection.ensure_connection()
        print("connected")
    except DatabaseSafetyError as error:
        print(error.code, connection.connection is None)
"""
        result = subprocess.run(
            (sys.executable, "-B", "-c", script),
            cwd=PROJECT_ROOT,
            env=_production_environment(),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            ["database_missing True", "database_missing True"],
        )
        self.assertFalse(PRODUCTION_DATABASE_PATH.exists())

    def test_management_command_never_creates_production_database(self):
        result = _run_manage(
            "shell",
            "-c",
            "from django.db import connection; connection.ensure_connection()",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("command_not_allowed", result.stderr)
        self.assertFalse(PRODUCTION_DATABASE_PATH.exists())


class ManagePreflightTests(unittest.TestCase):
    REFUSED_COMMANDS = (
        "dbshell",
        "flush",
        "sqlflush",
        "runserver",
        "testserver",
        "test",
        "migrate",
        "shell",
        "dbshel",
        "mig",
        "--no-color",
    )

    def test_check_passes_without_creating_production_database(self):
        before_exists = PRODUCTION_DATABASE_PATH.exists()
        result = _run_manage("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("System check identified no issues", result.stdout)
        self.assertEqual(before_exists, PRODUCTION_DATABASE_PATH.exists())

    def test_preflight_failure_stops_dispatch(self):
        result = _run_manage(
            "check",
            environment=_production_environment(PATH="/nonexistent"),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("git_unavailable", result.stderr)
        self.assertNotIn("System check", result.stdout)

    def test_settings_override_option_is_rejected(self):
        for argument in (
            "--settings=max_smart_db.test_settings",
            "--pythonpath=/tmp",
        ):
            with self.subTest(argument=argument):
                result = _run_manage("check", argument)
                self.assertEqual(result.returncode, 1)
                self.assertIn("settings_override_rejected", result.stderr)

    def test_unapproved_settings_module_is_rejected(self):
        result = _run_manage(
            "check",
            environment={
                **_production_environment(),
                "DJANGO_SETTINGS_MODULE": "some.other.settings",
            },
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("settings_module_conflict", result.stderr)

    def test_only_non_writing_introspection_commands_are_allowed(self):
        for command in ("help", "version", "--help", "-h", "--version"):
            with self.subTest(command=command):
                result = _run_manage(command)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_refused_commands_never_dispatch_or_touch_production_files(self):
        artifact_paths = (
            PRODUCTION_DATABASE_PATH,
            Path(f"{PRODUCTION_DATABASE_PATH}-wal"),
            Path(f"{PRODUCTION_DATABASE_PATH}-shm"),
            Path(f"{PRODUCTION_DATABASE_PATH}-journal"),
        )
        before = {
            path: (path.stat().st_mtime_ns, path.stat().st_size)
            if path.exists()
            else None
            for path in artifact_paths
        }

        for command in self.REFUSED_COMMANDS:
            with self.subTest(command=command):
                result = _run_manage(command)
                self.assertEqual(result.returncode, 1)
                self.assertIn("command_not_allowed", result.stderr)

        after = {
            path: (path.stat().st_mtime_ns, path.stat().st_size)
            if path.exists()
            else None
            for path in artifact_paths
        }
        self.assertEqual(after, before)


class BootstrapTests(unittest.TestCase):
    def test_settings_module_conflict_fails_before_preflight(self):
        from max_smart_db import bootstrap

        with mock.patch.dict(
            os.environ,
            {"DJANGO_SETTINGS_MODULE": "some.other.settings"},
        ), mock.patch.object(
            bootstrap,
            "verify_production_database_environment",
        ) as preflight, mock.patch.object(bootstrap.django, "setup") as setup:
            with self.assertRaises(DatabaseSafetyError) as raised:
                bootstrap.setup_database_django()
        self.assertEqual(raised.exception.code, "settings_module_conflict")
        preflight.assert_not_called()
        setup.assert_not_called()


class GitPreflightFailureTests(unittest.TestCase):
    def assert_git_failure(self, code, **run_behaviour):
        with mock.patch.object(safety.subprocess, "run", **run_behaviour):
            with self.assertRaises(DatabaseSafetyError) as raised:
                verify_git_protection(PROJECT_ROOT)
        self.assertEqual(raised.exception.code, code)

    def test_exit_code_128_fails_closed(self):
        with _private_directory() as directory:
            with self.assertRaises(DatabaseSafetyError) as raised:
                verify_git_protection(Path(directory).resolve())
        self.assertEqual(raised.exception.code, "git_ignore_check_failed")

    def test_mocked_exit_code_128_fails_closed(self):
        self.assert_git_failure(
            "git_ignore_check_failed",
            return_value=_completed(128),
        )

    def test_missing_git_fails_closed(self):
        self.assert_git_failure(
            "git_unavailable",
            side_effect=FileNotFoundError("git"),
        )

    def test_git_timeout_fails_closed(self):
        self.assert_git_failure(
            "git_unavailable",
            side_effect=subprocess.TimeoutExpired("git", 5),
        )

    def test_tracked_check_failure_fails_closed(self):
        def run(arguments, **kwargs):
            return _completed(128 if arguments[1] == "ls-files" else 0)

        self.assert_git_failure("git_tracked_check_failed", side_effect=run)

    def test_inherited_git_environment_is_cleared(self):
        captured = []

        def run(arguments, **kwargs):
            captured.append(kwargs["env"])
            return _completed(0)

        with mock.patch.dict(
            os.environ,
            {"GIT_DIR": "/nonexistent", "GIT_INDEX_FILE": "/nonexistent"},
        ), mock.patch.object(safety.subprocess, "run", side_effect=run):
            verify_git_protection(PROJECT_ROOT)
        self.assertTrue(captured)
        for environment in captured:
            self.assertFalse(
                [name for name in environment if name.startswith("GIT_")]
            )

    def test_real_git_ignores_inherited_git_dir(self):
        with mock.patch.dict(os.environ, {"GIT_DIR": "/nonexistent"}):
            verify_git_protection(PROJECT_ROOT)


class ApfsVerificationTests(unittest.TestCase):
    DEVICE = b"disk3s5\n"
    VALID_PLIST = plistlib.dumps(
        {"FilesystemType": "apfs", "DeviceNode": "/dev/disk3s5"}
    )

    def run_check(self, *results, path=PROJECT_ROOT):
        with mock.patch.object(
            safety.os,
            "uname",
            return_value=SimpleNamespace(sysname="Darwin"),
        ), mock.patch.object(safety.subprocess, "run", side_effect=results):
            verify_local_apfs(path)

    def assert_apfs_failure(self, code, *results, path=PROJECT_ROOT):
        with self.assertRaises(DatabaseSafetyError) as raised:
            self.run_check(*results, path=path)
        self.assertEqual(raised.exception.code, code)

    def test_valid_local_apfs_passes(self):
        self.run_check(_completed(stdout=self.DEVICE), _completed(stdout=self.VALID_PLIST))

    def test_device_command_errors_fail_closed(self):
        for result in (
            _completed(1),
            OSError("stat"),
            subprocess.TimeoutExpired("stat", 5),
            _completed(stdout=b"\xff\xfe"),
        ):
            with self.subTest(result=result):
                self.assert_apfs_failure("filesystem_check_failed", result)

    def test_network_device_is_rejected(self):
        self.assert_apfs_failure(
            "filesystem_not_local_apfs",
            _completed(stdout=b"//user@server/share\n"),
        )

    def test_diskutil_errors_fail_closed(self):
        for result in (
            _completed(1),
            OSError("diskutil"),
            subprocess.TimeoutExpired("diskutil", 5),
        ):
            with self.subTest(result=result):
                self.assert_apfs_failure(
                    "filesystem_check_failed",
                    _completed(stdout=self.DEVICE),
                    result,
                )

    def test_non_apfs_volume_is_rejected(self):
        self.assert_apfs_failure(
            "filesystem_not_local_apfs",
            _completed(stdout=self.DEVICE),
            _completed(
                stdout=plistlib.dumps(
                    {"FilesystemType": "hfs", "DeviceNode": "/dev/disk3s5"}
                )
            ),
        )

    def test_mismatched_device_node_is_rejected(self):
        self.assert_apfs_failure(
            "filesystem_not_local_apfs",
            _completed(stdout=self.DEVICE),
            _completed(
                stdout=plistlib.dumps(
                    {"FilesystemType": "apfs", "DeviceNode": "/dev/disk9s1"}
                )
            ),
        )

    def test_malformed_plist_output_fails_closed(self):
        for payload in (
            b"not a plist",
            plistlib.dumps(["apfs"]),
            plistlib.dumps({"FilesystemType": 7, "DeviceNode": "/dev/disk3s5"}),
            plistlib.dumps({"DeviceNode": "/dev/disk3s5"}),
            plistlib.dumps({"FilesystemType": "apfs"}),
        ):
            with self.subTest(payload=payload[:40]):
                self.assert_apfs_failure(
                    "filesystem_check_failed",
                    _completed(stdout=self.DEVICE),
                    _completed(stdout=payload),
                )

    def test_cloud_path_is_rejected_before_commands(self):
        self.assert_apfs_failure(
            "cloud_path",
            path=Path("/Users/example/Library/Mobile Documents/project"),
        )

    def test_non_macos_platform_is_rejected(self):
        with mock.patch.object(
            safety.os,
            "uname",
            return_value=SimpleNamespace(sysname="Linux"),
        ):
            with self.assertRaises(DatabaseSafetyError) as raised:
                verify_local_apfs(PROJECT_ROOT)
        self.assertEqual(
            raised.exception.code,
            "unsupported_filesystem_platform",
        )

    def test_real_data_directory_is_local_apfs(self):
        verify_local_apfs(PROJECT_ROOT / "data")


if __name__ == "__main__":
    unittest.main()
