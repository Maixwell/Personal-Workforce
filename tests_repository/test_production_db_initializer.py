"""Disposable tests for the fixed production database initializer command."""

import errno
import hashlib
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

import max_smart_db.safety as safety  # noqa: E402
import production_db_initializer as command  # noqa: E402
from max_smart_db.config import PRODUCTION_DATABASE_PATH  # noqa: E402
from max_smart_db.safety import (  # noqa: E402
    SQLITE_INITIALIZATION_INCOMPLETE,
    SQLITE_INITIALIZATION_READY,
    SQLITE_INITIALIZATION_STATE_SUFFIX,
    SQLITE_INITIALIZATION_TEMP_SUFFIX,
    DatabaseSafetyError,
    _disposable_production_initialization_capability,
    _initialize_sqlite_capability,
)
from provider_fence import (  # noqa: E402
    PRODUCTION_FENCE_PATH,
    _APPROVED_PIN_DIRECTORY,
)


SESSION_FILE = PROJECT_ROOT / "data" / "current_session.json"
PRESERVED_MODULES = (
    "src/production_preflight.py",
    "src/provider_fence.py",
    "src/durable_composition.py",
    "src/terminal_durable_turn.py",
    "src/disposable_terminal.py",
    "src/max_smart.py",
    "src/terminal.py",
    "src/max_smart_db/maintenance.py",
)


def _metadata(path):
    try:
        status = Path(path).lstat()
    except FileNotFoundError:
        return None
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_uid,
        status.st_gid,
        status.st_nlink,
        status.st_size,
        status.st_mtime_ns,
    )


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class DisposableRoot:
    def __init__(self):
        self._temporary = tempfile.TemporaryDirectory(
            prefix="max-smart-production-initializer-"
        )
        self.root = Path(self._temporary.name).resolve()
        self.parent = self.root / "private"
        self.parent.mkdir(mode=0o700)
        self.database = self.parent / "production-shaped.sqlite3"

    def cleanup(self):
        self._temporary.cleanup()


class ProductionInitializerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protected_before = {
            path: _metadata(path)
            for path in (
                PRODUCTION_DATABASE_PATH,
                PRODUCTION_FENCE_PATH,
                _APPROVED_PIN_DIRECTORY,
                SESSION_FILE,
            )
        }
        cls.module_hashes = {
            relative: _digest(PROJECT_ROOT / relative)
            for relative in PRESERVED_MODULES
        }

    @classmethod
    def tearDownClass(cls):
        protected_after = {
            path: _metadata(path)
            for path in cls.protected_before
        }
        if protected_after != cls.protected_before:
            raise AssertionError("protected production or session metadata changed")
        module_hashes = {
            relative: _digest(PROJECT_ROOT / relative)
            for relative in PRESERVED_MODULES
        }
        if module_hashes != cls.module_hashes:
            raise AssertionError("accepted module changed during disposable tests")

    def setUp(self):
        self.fixture = DisposableRoot()
        self.addCleanup(self.fixture.cleanup)

    def initialize(self):
        capability = _disposable_production_initialization_capability(
            self.fixture.database
        )
        return _initialize_sqlite_capability(capability)

    def test_missing_authorization_refuses_before_initializer(self):
        with (
            mock.patch.object(command, "_verify_invocation_context"),
            mock.patch.object(command, "_new_authorization_challenge", return_value="nonce"),
            mock.patch.object(command.getpass, "getpass", side_effect=EOFError),
            mock.patch.object(command, "initialize_sqlite_file") as initialize,
            self.assertRaises(SystemExit) as raised,
        ):
            command.main([])
        self.assertEqual(raised.exception.code, 1)
        initialize.assert_not_called()

    def test_invalid_and_non_reusable_authorization_are_refused(self):
        self.assertFalse(command._authorization_valid("", "first"))
        self.assertFalse(command._authorization_valid("wrong", "first"))
        response = command._authorization_response("first")
        self.assertTrue(command._authorization_valid(response, "first"))
        self.assertFalse(command._authorization_valid(response, "second"))
        with (
            mock.patch.object(command, "_verify_invocation_context"),
            mock.patch.object(command, "_new_authorization_challenge", return_value="nonce"),
            mock.patch.object(command.getpass, "getpass", return_value="wrong"),
            mock.patch.object(command, "initialize_sqlite_file") as initialize,
            self.assertRaises(SystemExit) as raised,
        ):
            command.main([])
        self.assertEqual(raised.exception.code, 1)
        initialize.assert_not_called()

    def test_correct_authorization_can_call_only_the_fixed_target(self):
        challenge = "disposable-wiring-challenge"
        response = command._authorization_response(challenge)
        with (
            mock.patch.object(command, "_verify_invocation_context"),
            mock.patch.object(command, "initialize_sqlite_file") as initialize,
        ):
            command.initialize_approved_production_database(response, challenge)
        initialize.assert_called_once_with(
            PRODUCTION_DATABASE_PATH,
            production=True,
        )

    def test_command_has_no_path_argument_or_environment_override(self):
        source = (SRC_ROOT / "production_db_initializer.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("add_argument", source)
        self.assertNotIn("os.getenv", source)
        self.assertNotIn("os.environ", source)
        self.assertIn(
            "initialize_sqlite_file(PRODUCTION_DATABASE_PATH, production=True)",
            source,
        )
        with self.assertRaises(SystemExit) as raised:
            command.main(["--database", str(self.fixture.database)])
        self.assertEqual(raised.exception.code, 2)

    def test_wrong_effective_user_and_repository_are_refused(self):
        identity = type(
            "Identity",
            (),
            {"pw_name": "other", "pw_dir": "/Users/other"},
        )()
        with mock.patch.object(command.pwd, "getpwuid", return_value=identity):
            with self.assertRaises(command.ProductionInitializationRefused) as raised:
                command._verify_invocation_context()
        self.assertEqual(raised.exception.code, "account_identity_refused")
        with mock.patch.object(command, "PROJECT_ROOT", self.fixture.root):
            with self.assertRaises(command.ProductionInitializationRefused) as raised:
                command._verify_invocation_context()
        self.assertEqual(raised.exception.code, "repository_location_refused")

    def test_disposable_capability_rejects_wrong_mode_owner_and_symlink(self):
        os.chmod(self.fixture.parent, 0o755)
        with self.assertRaises(DatabaseSafetyError) as mode:
            _disposable_production_initialization_capability(self.fixture.database)
        self.assertEqual(mode.exception.code, "data_directory_permissions")
        os.chmod(self.fixture.parent, 0o700)
        with mock.patch.object(safety.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(DatabaseSafetyError) as owner:
                _disposable_production_initialization_capability(
                    self.fixture.database
                )
        self.assertEqual(owner.exception.code, "data_directory_owner")

        real = self.fixture.root / "real"
        real.mkdir(mode=0o700)
        alias = self.fixture.root / "alias"
        alias.symlink_to(real, target_is_directory=True)
        with self.assertRaises(DatabaseSafetyError) as linked:
            _disposable_production_initialization_capability(
                alias / self.fixture.database.name
            )
        self.assertEqual(linked.exception.code, "path_symlink")

    def test_disposable_capability_rejects_production_repository_and_aliases(self):
        for candidate in (
            PRODUCTION_DATABASE_PATH,
            PROJECT_ROOT / "data" / "other.sqlite3",
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(DatabaseSafetyError) as raised:
                    _disposable_production_initialization_capability(candidate)
                self.assertIn(
                    raised.exception.code,
                    {"test_path_in_repository", "data_directory_invalid"},
                )
        alias = self.fixture.root / "repository-alias"
        alias.symlink_to(PROJECT_ROOT, target_is_directory=True)
        with self.assertRaises(DatabaseSafetyError) as raised:
            _disposable_production_initialization_capability(
                alias / "data" / "alias.sqlite3"
            )
        self.assertEqual(raised.exception.code, "test_path_in_repository")

    def test_existing_database_marker_sidecar_and_partial_artifacts_refuse(self):
        suffixes = (
            "",
            SQLITE_INITIALIZATION_STATE_SUFFIX,
            SQLITE_INITIALIZATION_TEMP_SUFFIX,
            "-wal",
            "-shm",
            "-journal",
        )
        for suffix in suffixes:
            with self.subTest(suffix=suffix):
                fixture = DisposableRoot()
                self.addCleanup(fixture.cleanup)
                artifact = Path(f"{fixture.database}{suffix}")
                artifact.touch(mode=0o600)
                before = artifact.read_bytes()
                capability = _disposable_production_initialization_capability(
                    fixture.database
                )
                with self.assertRaises(DatabaseSafetyError) as raised:
                    _initialize_sqlite_capability(capability)
                self.assertEqual(
                    raised.exception.code,
                    "database_initialization_artifact_exists",
                )
                self.assertEqual(artifact.read_bytes(), before)

    def test_marker_order_ready_contents_wal_modes_and_no_schema(self):
        created = []
        real_open = safety.os.open

        def recording_open(path, flags, mode=0o777, **kwargs):
            if flags & os.O_CREAT:
                created.append(Path(path).name)
            return real_open(path, flags, mode, **kwargs)

        with mock.patch.object(safety.os, "open", side_effect=recording_open):
            self.initialize()
        marker = Path(
            f"{self.fixture.database}{SQLITE_INITIALIZATION_STATE_SUFFIX}"
        )
        self.assertLess(
            created.index(marker.name),
            created.index(self.fixture.database.name),
        )
        self.assertEqual(marker.read_bytes(), SQLITE_INITIALIZATION_READY)
        self.assertFalse(
            Path(
                f"{self.fixture.database}{SQLITE_INITIALIZATION_TEMP_SUFFIX}"
            ).exists()
        )
        self.assertEqual(stat.S_IMODE(self.fixture.parent.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE(self.fixture.database.stat().st_mode),
            0o600,
        )
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
        connection = sqlite3.connect(self.fixture.database)
        try:
            self.assertEqual(
                connection.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                "wal",
            )
            self.assertEqual(
                connection.execute("PRAGMA synchronous").fetchone()[0],
                2,
            )
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(tables, [])

    def test_interrupted_ready_commit_remains_incomplete_and_refuses_retry(self):
        real_replace = safety.os.replace

        def interrupted(source, destination):
            if str(source).endswith(SQLITE_INITIALIZATION_TEMP_SUFFIX):
                raise OSError(errno.EIO, "test interruption")
            return real_replace(source, destination)

        with mock.patch.object(safety.os, "replace", side_effect=interrupted):
            with self.assertRaises(DatabaseSafetyError) as raised:
                self.initialize()
        self.assertEqual(raised.exception.code, "database_initialization_failed")
        marker = Path(
            f"{self.fixture.database}{SQLITE_INITIALIZATION_STATE_SUFFIX}"
        )
        self.assertEqual(marker.read_bytes(), SQLITE_INITIALIZATION_INCOMPLETE)
        capability = _disposable_production_initialization_capability(
            self.fixture.database
        )
        with self.assertRaises(DatabaseSafetyError) as retry:
            _initialize_sqlite_capability(capability)
        self.assertEqual(
            retry.exception.code,
            "database_initialization_artifact_exists",
        )

    def test_parent_identity_replacement_is_refused_before_mutation(self):
        capability = _disposable_production_initialization_capability(
            self.fixture.database
        )
        retired = self.fixture.root / "retired"
        self.fixture.parent.rename(retired)
        self.fixture.parent.mkdir(mode=0o700)
        with self.assertRaises(DatabaseSafetyError) as raised:
            _initialize_sqlite_capability(capability)
        self.assertEqual(raised.exception.code, "initialization_identity_changed")
        self.assertFalse(self.fixture.database.exists())
        self.assertFalse((retired / self.fixture.database.name).exists())

    def test_parent_replacement_immediately_before_sqlite_connection(self):
        real_create = safety._create_sqlite_file_exclusively
        retired = self.fixture.root / "retired-before-connect"
        connections = {"count": 0}

        def create_and_replace(database_path):
            real_create(database_path)
            self.fixture.parent.rename(retired)
            self.fixture.parent.mkdir(mode=0o700)

        def count_connection(*args, **kwargs):
            connections["count"] += 1
            raise AssertionError("sqlite connection followed parent replacement")

        capability = _disposable_production_initialization_capability(
            self.fixture.database
        )
        with (
            mock.patch.object(
                safety,
                "_create_sqlite_file_exclusively",
                side_effect=create_and_replace,
            ),
            mock.patch.object(safety.sqlite3, "connect", side_effect=count_connection),
            self.assertRaises(DatabaseSafetyError) as raised,
        ):
            _initialize_sqlite_capability(capability)
        self.assertEqual(raised.exception.code, "initialization_identity_changed")
        self.assertEqual(connections["count"], 0)
        self.assertEqual(list(self.fixture.parent.iterdir()), [])
        original = retired / self.fixture.database.name
        self.assertTrue(original.is_file())
        self.assertFalse(Path(f"{original}-wal").exists())
        self.assertEqual(
            Path(f"{original}{SQLITE_INITIALIZATION_STATE_SUFFIX}").read_bytes(),
            SQLITE_INITIALIZATION_INCOMPLETE,
        )

    def test_parent_replacement_immediately_before_final_directory_fsync(self):
        real_replace = safety.os.replace
        retired = self.fixture.root / "retired-before-fsync"

        def replace_and_swap(source, destination):
            real_replace(source, destination)
            self.fixture.parent.rename(retired)
            self.fixture.parent.mkdir(mode=0o700)

        with (
            mock.patch.object(safety.os, "replace", side_effect=replace_and_swap),
            self.assertRaises(DatabaseSafetyError) as raised,
        ):
            self.initialize()
        self.assertEqual(
            raised.exception.code,
            "database_initialization_commit_uncertain",
        )
        self.assertEqual(list(self.fixture.parent.iterdir()), [])
        original = retired / self.fixture.database.name
        self.assertEqual(
            Path(f"{original}{SQLITE_INITIALIZATION_STATE_SUFFIX}").read_bytes(),
            SQLITE_INITIALIZATION_READY,
        )
        self.assertFalse((self.fixture.parent / original.name).exists())

    def test_confirmation_does_not_claim_independent_authentication(self):
        source = (SRC_ROOT / "production_db_initializer.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("not independent owner authentication", source)
        self.assertIn("getpass.getpass", source)
        self.assertNotIn("os.environ", source)

    def test_parent_identity_replacement_before_ready_is_refused(self):
        real_check = safety._check_initialized_artifacts
        retired = self.fixture.root / "retired-before-ready"

        def replace_parent(database_path):
            real_check(database_path)
            self.fixture.parent.rename(retired)
            self.fixture.parent.mkdir(mode=0o700)

        capability = _disposable_production_initialization_capability(
            self.fixture.database
        )
        with (
            mock.patch.object(
                safety,
                "_check_initialized_artifacts",
                side_effect=replace_parent,
            ),
            self.assertRaises(DatabaseSafetyError) as raised,
        ):
            _initialize_sqlite_capability(capability)
        self.assertEqual(raised.exception.code, "initialization_identity_changed")
        self.assertFalse(
            Path(
                f"{self.fixture.database}{SQLITE_INITIALIZATION_TEMP_SUFFIX}"
            ).exists()
        )
        old_marker = Path(
            f"{retired / self.fixture.database.name}"
            f"{SQLITE_INITIALIZATION_STATE_SUFFIX}"
        )
        self.assertEqual(old_marker.read_bytes(), SQLITE_INITIALIZATION_INCOMPLETE)

    def test_ordinary_test_initializer_public_contract_still_works(self):
        ordinary = self.fixture.parent / "ordinary.sqlite3"
        safety.initialize_sqlite_file(ordinary, production=False)
        marker = Path(f"{ordinary}{SQLITE_INITIALIZATION_STATE_SUFFIX}")
        self.assertEqual(marker.read_bytes(), SQLITE_INITIALIZATION_READY)

    def test_no_existing_entry_point_imports_the_command(self):
        for name in ("max_smart.py", "terminal.py", "disposable_terminal.py"):
            source = (SRC_ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("production_db_initializer", source)


if __name__ == "__main__":
    unittest.main()
