"""Package A acceptance tests for the M2.1b SQLite connection boundary."""

import os
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

from max_smart_db import connections, safety  # noqa: E402
from max_smart_db.backend import base as backend  # noqa: E402
from max_smart_db.safety import (  # noqa: E402
    DatabaseSafetyError,
    close_owned_resource,
    initialize_sqlite_file,
    verify_sqlite_module_version,
)

_DEFAULT_VERSION_ROW = object()


class FakeConnection:
    def __init__(
        self,
        *,
        version=None,
        version_row=_DEFAULT_VERSION_ROW,
        responses=None,
        execute_errors=None,
        fetch_errors=None,
        close_error=None,
    ):
        self.close_error = close_error
        self.statements = []
        self.closed = False
        self.execute_errors = execute_errors or {}
        self.fetch_errors = fetch_errors or {}
        self.responses = {
            "SELECT sqlite_version()": (
                (version or sqlite3.sqlite_version,)
                if version_row is _DEFAULT_VERSION_ROW
                else version_row
            ),
            "PRAGMA foreign_keys": (1,),
            "PRAGMA journal_mode": ("wal",),
            "PRAGMA synchronous": (2,),
            "PRAGMA busy_timeout": (5000,),
            "PRAGMA recursive_triggers": (1,),
            **(responses or {}),
        }

    def execute(self, statement):
        self.statements.append(statement)
        if statement in self.execute_errors:
            raise self.execute_errors[statement]

        def fetchone():
            if statement in self.fetch_errors:
                raise self.fetch_errors[statement]
            return self.responses.get(statement)

        return SimpleNamespace(fetchone=fetchone)

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def _wrapper():
    wrapper = object.__new__(backend.DatabaseWrapper)
    wrapper.settings_dict = {"NAME": "/disposable/database.sqlite3"}
    wrapper.init_commands = [
        "PRAGMA synchronous=FULL",
        "PRAGMA busy_timeout=5000",
    ]
    return wrapper


class SQLiteVersionGateTests(unittest.TestCase):
    def test_module_version_requires_exact_non_boolean_three_integer_tuple(self):
        invalid_versions = (
            (3, 33),
            (3, 33, 0, 1),
            [3, 33, 0],
            (3, True, 0),
            (3, 33, "0"),
        )
        for version in invalid_versions:
            with self.subTest(version=version), mock.patch.object(
                safety.sqlite3,
                "sqlite_version_info",
                version,
            ):
                with self.assertRaises(DatabaseSafetyError) as raised:
                    verify_sqlite_module_version()
                self.assertEqual(
                    raised.exception.code,
                    "sqlite_module_version_invalid",
                )

    def test_old_module_version_fails_before_connect(self):
        wrapper = _wrapper()
        with mock.patch.object(
            safety.sqlite3,
            "sqlite_version_info",
            (3, 32, 9),
        ), mock.patch.object(
            backend.sqlite_base.Database,
            "connect",
        ) as connect:
            with self.assertRaises(DatabaseSafetyError) as raised:
                wrapper.get_new_connection({})
        self.assertEqual(raised.exception.code, "sqlite_version_unsupported")
        connect.assert_not_called()

    def test_connection_version_query_is_first_sql(self):
        wrapper = _wrapper()
        raw_connection = FakeConnection()
        with mock.patch.object(
            backend,
            "verify_database_files",
        ) as verify_files, mock.patch.object(
            backend.sqlite_base.Database,
            "connect",
            return_value=raw_connection,
        ), mock.patch.object(
            backend.sqlite_base,
            "register_functions",
        ) as register_functions:
            returned = wrapper.get_new_connection({"uri": True})

        self.assertIs(returned, raw_connection)
        self.assertEqual(raw_connection.statements[0], "SELECT sqlite_version()")
        self.assertEqual(verify_files.call_count, 2)
        register_functions.assert_called_once_with(raw_connection)
        self.assertEqual(
            raw_connection.statements[:7],
            [
                "SELECT sqlite_version()",
                "PRAGMA foreign_keys = ON",
                "PRAGMA legacy_alter_table = OFF",
                "PRAGMA synchronous=FULL",
                "PRAGMA busy_timeout=5000",
                "PRAGMA recursive_triggers = ON",
                "PRAGMA foreign_keys",
            ],
        )
        self.assertNotIn(
            "PRAGMA journal_mode=WAL",
            raw_connection.statements,
        )
        readbacks = [
            statement
            for statement in raw_connection.statements
            if statement
            in {
                "PRAGMA foreign_keys",
                "PRAGMA journal_mode",
                "PRAGMA synchronous",
                "PRAGMA busy_timeout",
                "PRAGMA recursive_triggers",
            }
        ]
        self.assertEqual(
            readbacks,
            [
                "PRAGMA foreign_keys",
                "PRAGMA journal_mode",
                "PRAGMA synchronous",
                "PRAGMA busy_timeout",
                "PRAGMA recursive_triggers",
            ],
        )

    def test_malformed_connection_version_closes_owned_handle(self):
        wrapper = _wrapper()
        close_failure = RuntimeError("close failed")
        raw_connection = FakeConnection(
            version="3.33",
            close_error=close_failure,
        )
        with mock.patch.object(
            backend,
            "verify_database_files",
        ), mock.patch.object(
            backend.sqlite_base.Database,
            "connect",
            return_value=raw_connection,
        ):
            with self.assertRaises(DatabaseSafetyError) as raised:
                wrapper.get_new_connection({})
        self.assertEqual(
            raised.exception.code,
            "sqlite_connection_version_invalid",
        )
        self.assertIs(raised.exception.__cause__, close_failure)
        self.assertTrue(raw_connection.closed)

    def test_connection_and_module_versions_must_match(self):
        wrapper = _wrapper()
        raw_connection = FakeConnection(version="3.33.0")
        with mock.patch.object(
            safety.sqlite3,
            "sqlite_version_info",
            (3, 34, 0),
        ), mock.patch.object(
            backend,
            "verify_database_files",
        ), mock.patch.object(
            backend.sqlite_base.Database,
            "connect",
            return_value=raw_connection,
        ):
            with self.assertRaises(DatabaseSafetyError) as raised:
                wrapper.get_new_connection({})
        self.assertEqual(raised.exception.code, "sqlite_version_mismatch")
        self.assertTrue(raw_connection.closed)

    def test_malformed_connection_version_results_fail_closed(self):
        malformed_rows = (
            None,
            (),
            ("3.33.0", "extra"),
            (None,),
            (True,),
            (3.33,),
            ("",),
            ("3.33",),
            ("3.33.0.1",),
            ("3.-33.0",),
        )
        for row in malformed_rows:
            with self.subTest(row=row):
                wrapper = _wrapper()
                raw_connection = FakeConnection(version_row=row)
                with mock.patch.object(
                    backend,
                    "verify_database_files",
                ), mock.patch.object(
                    backend.sqlite_base.Database,
                    "connect",
                    return_value=raw_connection,
                ):
                    with self.assertRaises(DatabaseSafetyError) as raised:
                        wrapper.get_new_connection({})
                self.assertEqual(
                    raised.exception.code,
                    "sqlite_connection_version_invalid",
                )
                self.assertTrue(raw_connection.closed)

    def test_old_connection_version_fails_closed(self):
        wrapper = _wrapper()
        raw_connection = FakeConnection(version="3.32.9")
        with mock.patch.object(
            backend,
            "verify_database_files",
        ), mock.patch.object(
            backend.sqlite_base.Database,
            "connect",
            return_value=raw_connection,
        ):
            with self.assertRaises(DatabaseSafetyError) as raised:
                wrapper.get_new_connection({})
        self.assertEqual(raised.exception.code, "sqlite_version_unsupported")
        self.assertTrue(raw_connection.closed)

    def test_connection_version_query_and_fetch_errors_remain_primary(self):
        failures = (
            (
                {"SELECT sqlite_version()": sqlite3.DatabaseError("query")},
                {},
            ),
            (
                {},
                {"SELECT sqlite_version()": sqlite3.DatabaseError("fetch")},
            ),
        )
        for execute_errors, fetch_errors in failures:
            with self.subTest(
                execute_errors=bool(execute_errors),
                fetch_errors=bool(fetch_errors),
            ):
                wrapper = _wrapper()
                raw_connection = FakeConnection(
                    execute_errors=execute_errors,
                    fetch_errors=fetch_errors,
                )
                with mock.patch.object(
                    backend,
                    "verify_database_files",
                ), mock.patch.object(
                    backend.sqlite_base.Database,
                    "connect",
                    return_value=raw_connection,
                ):
                    with self.assertRaises(sqlite3.DatabaseError):
                        wrapper.get_new_connection({})
                self.assertTrue(raw_connection.closed)


class PragmaValidationTests(unittest.TestCase):
    PRAGMAS = (
        ("foreign_keys", 0),
        ("journal_mode", "delete"),
        ("synchronous", 1),
        ("busy_timeout", 1000),
        ("recursive_triggers", 0),
    )

    def test_each_wrong_pragma_value_is_rejected(self):
        for pragma, wrong_value in self.PRAGMAS:
            with self.subTest(pragma=pragma):
                connection = FakeConnection(
                    responses={f"PRAGMA {pragma}": (wrong_value,)},
                )
                with self.assertRaises(DatabaseSafetyError) as raised:
                    safety.verify_required_sqlite_pragmas(connection)
                self.assertEqual(raised.exception.code, f"{pragma}_invalid")

    def test_each_malformed_pragma_result_is_rejected(self):
        for pragma, _ in self.PRAGMAS:
            for row in (None, (), (1, 2)):
                with self.subTest(pragma=pragma, row=row):
                    connection = FakeConnection(
                        responses={f"PRAGMA {pragma}": row},
                    )
                    with self.assertRaises(DatabaseSafetyError) as raised:
                        safety.verify_required_sqlite_pragmas(connection)
                    self.assertEqual(
                        raised.exception.code,
                        f"{pragma}_invalid",
                    )

    def test_each_pragma_query_error_propagates(self):
        for pragma, _ in self.PRAGMAS:
            with self.subTest(pragma=pragma):
                failure = sqlite3.DatabaseError("pragma query")
                connection = FakeConnection(
                    execute_errors={f"PRAGMA {pragma}": failure},
                )
                with self.assertRaises(sqlite3.DatabaseError) as raised:
                    safety.verify_required_sqlite_pragmas(connection)
                self.assertIs(raised.exception, failure)


class InitializerTests(unittest.TestCase):
    def test_old_module_version_creates_no_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory).resolve() / "database.sqlite3"
            with mock.patch.object(
                safety.sqlite3,
                "sqlite_version_info",
                (3, 32, 9),
            ):
                with self.assertRaises(DatabaseSafetyError) as raised:
                    initialize_sqlite_file(database_path)
            self.assertEqual(
                raised.exception.code,
                "sqlite_version_unsupported",
            )
            self.assertFalse(database_path.exists())

    def test_initializer_sets_and_verifies_all_five_pragmas(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory).resolve() / "database.sqlite3"
            initialize_sqlite_file(database_path)
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA recursive_triggers=ON")
                actual = [
                    connection.execute(f"PRAGMA {pragma}").fetchone()[0]
                    for pragma in (
                        "foreign_keys",
                        "journal_mode",
                        "synchronous",
                        "busy_timeout",
                        "recursive_triggers",
                    )
                ]
            finally:
                connection.close()
        self.assertEqual(actual, [1, "wal", 2, 5000, 1])

    def test_initializer_refuses_existing_database_without_modification(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory).resolve() / "database.sqlite3"
            database_path.write_bytes(b"existing")
            os.chmod(database_path, 0o600)
            with self.assertRaises(DatabaseSafetyError) as raised:
                initialize_sqlite_file(database_path)
            self.assertEqual(
                raised.exception.code,
                "database_initialization_artifact_exists",
            )
            self.assertEqual(database_path.read_bytes(), b"existing")

    def test_initializer_refuses_each_named_sidecar(self):
        for suffix in safety.SQLITE_SIDECAR_SUFFIXES:
            with self.subTest(
                suffix=suffix
            ), tempfile.TemporaryDirectory() as directory:
                database_path = Path(directory).resolve() / "database.sqlite3"
                sidecar = Path(f"{database_path}{suffix}")
                sidecar.write_bytes(b"existing-sidecar")
                os.chmod(sidecar, 0o600)
                with self.assertRaises(DatabaseSafetyError) as raised:
                    initialize_sqlite_file(database_path)
                self.assertEqual(
                    raised.exception.code,
                    "database_initialization_artifact_exists",
                )
                self.assertFalse(database_path.exists())
                self.assertEqual(sidecar.read_bytes(), b"existing-sidecar")

    def test_exclusive_creation_refuses_check_create_race(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory).resolve() / "database.sqlite3"

            def racer(path):
                Path(path).write_bytes(b"racer")
                os.chmod(path, 0o600)
                raise DatabaseSafetyError(
                    "database_initialization_artifact_exists",
                    "Database initialization requires unused artifact paths.",
                )

            with mock.patch.object(
                safety,
                "_create_sqlite_file_exclusively",
                side_effect=racer,
            ):
                with self.assertRaises(DatabaseSafetyError) as raised:
                    initialize_sqlite_file(database_path)
            self.assertEqual(
                raised.exception.code,
                "database_initialization_artifact_exists",
            )
            self.assertEqual(database_path.read_bytes(), b"racer")

    def test_initializer_preserves_primary_when_close_also_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory).resolve() / "database.sqlite3"
            raw_connection = FakeConnection(
                responses={
                    "PRAGMA journal_mode=WAL": ("delete",),
                },
                close_error=RuntimeError("close failed"),
            )
            with mock.patch.object(
                safety.sqlite3,
                "connect",
                return_value=raw_connection,
            ):
                with self.assertRaises(DatabaseSafetyError) as raised:
                    initialize_sqlite_file(database_path)
            self.assertEqual(raised.exception.code, "wal_not_enabled")
            self.assertIsInstance(raised.exception.__cause__, RuntimeError)


class CleanupPrecedenceTests(unittest.TestCase):
    def test_existing_cause_is_preserved_and_cleanup_is_noted(self):
        original_cause = ValueError("original")
        primary = DatabaseSafetyError("primary", "Primary failure.")
        primary.__cause__ = original_cause
        resource = mock.Mock()
        resource.close.side_effect = RuntimeError("sensitive cleanup detail")

        close_owned_resource(resource, primary)

        self.assertIs(primary.__cause__, original_cause)
        self.assertEqual(
            primary.__notes__,
            [
                "Owned-resource cleanup also failed "
                "(RuntimeError; details redacted)."
            ],
        )
        self.assertNotIn("sensitive cleanup detail", primary.__notes__[0])

    def test_unsupported_wrapper_is_detached_when_close_fails(self):
        raw_connection = mock.Mock()
        cleanup_failure = RuntimeError("close failed")
        raw_connection.close.side_effect = cleanup_failure
        connection = SimpleNamespace(connection=raw_connection)

        with self.assertRaises(DatabaseSafetyError) as raised:
            connections.require_safe_backend(
                sender=object(),
                connection=connection,
            )

        self.assertEqual(raised.exception.code, "unsupported_database")
        self.assertIs(raised.exception.__cause__, cleanup_failure)
        self.assertIsNone(connection.connection)


class InitializationMarkerTests(unittest.TestCase):
    def test_successful_initialization_commits_exact_ready_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory).resolve() / "database.sqlite3"
            initialize_sqlite_file(database_path)
            state_path = Path(
                f"{database_path}{safety.SQLITE_INITIALIZATION_STATE_SUFFIX}"
            )
            temporary_path = Path(
                f"{database_path}{safety.SQLITE_INITIALIZATION_TEMP_SUFFIX}"
            )
            self.assertEqual(
                state_path.read_bytes(),
                safety.SQLITE_INITIALIZATION_READY,
            )
            self.assertFalse(temporary_path.exists())
            safety.verify_test_database_before_connect(database_path)

    def test_missing_or_malformed_marker_blocks_ordinary_open(self):
        malformed = (
            None,
            b"",
            safety.SQLITE_INITIALIZATION_INCOMPLETE,
            b"max-smart/sqlite-init/v1 READY\nextra",
            b"not-a-marker",
        )
        for contents in malformed:
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as directory:
                database_path = Path(directory).resolve() / "database.sqlite3"
                initialize_sqlite_file(database_path)
                state_path = Path(
                    f"{database_path}"
                    f"{safety.SQLITE_INITIALIZATION_STATE_SUFFIX}"
                )
                if contents is None:
                    state_path.unlink()
                else:
                    state_path.write_bytes(contents)
                    os.chmod(state_path, 0o600)
                with self.assertRaises(DatabaseSafetyError) as raised:
                    safety.verify_test_database_before_connect(database_path)
                self.assertEqual(
                    raised.exception.code,
                    "database_initialization_incomplete",
                )

    def test_marker_permissions_and_hard_links_are_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory).resolve() / "database.sqlite3"
            initialize_sqlite_file(database_path)
            state_path = Path(
                f"{database_path}{safety.SQLITE_INITIALIZATION_STATE_SUFFIX}"
            )
            os.chmod(state_path, 0o644)
            with self.assertRaises(DatabaseSafetyError):
                safety.verify_test_database_before_connect(database_path)

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory).resolve() / "database.sqlite3"
            initialize_sqlite_file(database_path)
            state_path = Path(
                f"{database_path}{safety.SQLITE_INITIALIZATION_STATE_SUFFIX}"
            )
            os.chmod(state_path, 0o000)
            with self.assertRaises(DatabaseSafetyError):
                safety.verify_test_database_before_connect(database_path)

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory).resolve() / "database.sqlite3"
            initialize_sqlite_file(database_path)
            real_fstat = safety.os.fstat

            def foreign_marker(descriptor):
                status = real_fstat(descriptor)
                values = list(status)
                values[stat.ST_UID] = os.geteuid() + 1
                return os.stat_result(values)

            with mock.patch.object(
                safety.os,
                "fstat",
                side_effect=foreign_marker,
            ):
                with self.assertRaises(DatabaseSafetyError):
                    safety.verify_test_database_before_connect(database_path)

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory).resolve() / "database.sqlite3"
            initialize_sqlite_file(database_path)
            state_path = Path(
                f"{database_path}{safety.SQLITE_INITIALIZATION_STATE_SUFFIX}"
            )
            target = Path(directory) / "marker-target"
            state_path.rename(target)
            state_path.symlink_to(target)
            with self.assertRaises(DatabaseSafetyError):
                safety.verify_test_database_before_connect(database_path)

    def test_production_marker_paths_are_git_protected(self):
        self.assertIn(
            "data/max_smart.sqlite3.initialization-state",
            safety._PROTECTED_GIT_PATHS,
        )
        self.assertIn(
            "data/max_smart.sqlite3.initialization-state.tmp",
            safety._PROTECTED_GIT_PATHS,
        )

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory).resolve() / "database.sqlite3"
            initialize_sqlite_file(database_path)
            state_path = Path(
                f"{database_path}{safety.SQLITE_INITIALIZATION_STATE_SUFFIX}"
            )
            os.link(state_path, Path(directory) / "marker-link")
            with self.assertRaises(DatabaseSafetyError):
                safety.verify_test_database_before_connect(database_path)

    def test_residual_temporary_marker_blocks_ordinary_open(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory).resolve() / "database.sqlite3"
            initialize_sqlite_file(database_path)
            temporary_path = Path(
                f"{database_path}{safety.SQLITE_INITIALIZATION_TEMP_SUFFIX}"
            )
            temporary_path.write_bytes(safety.SQLITE_INITIALIZATION_READY)
            os.chmod(temporary_path, 0o600)
            with self.assertRaises(DatabaseSafetyError) as raised:
                safety.verify_test_database_before_connect(database_path)
            self.assertEqual(
                raised.exception.code,
                "database_initialization_incomplete",
            )

    def test_failed_setup_leaves_incomplete_marker_and_blocks_open(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory).resolve() / "database.sqlite3"
            failure = DatabaseSafetyError(
                "recursive_triggers_invalid",
                "A required SQLite connection safeguard is not active.",
            )
            with mock.patch.object(
                safety,
                "verify_required_sqlite_pragmas",
                side_effect=failure,
            ):
                with self.assertRaises(DatabaseSafetyError) as raised:
                    initialize_sqlite_file(database_path)
            self.assertIs(raised.exception, failure)
            state_path = Path(
                f"{database_path}{safety.SQLITE_INITIALIZATION_STATE_SUFFIX}"
            )
            self.assertEqual(
                state_path.read_bytes(),
                safety.SQLITE_INITIALIZATION_INCOMPLETE,
            )
            with self.assertRaises(DatabaseSafetyError) as ordinary:
                safety.verify_test_database_before_connect(database_path)
            self.assertEqual(
                ordinary.exception.code,
                "database_initialization_incomplete",
            )

    def test_close_failure_leaves_incomplete_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory).resolve() / "database.sqlite3"
            raw_connection = FakeConnection(
                close_error=RuntimeError("close failed"),
                responses={"PRAGMA journal_mode=WAL": ("wal",)},
            )
            with mock.patch.object(
                safety.sqlite3,
                "connect",
                return_value=raw_connection,
            ):
                with self.assertRaises(RuntimeError):
                    initialize_sqlite_file(database_path)
            state_path = Path(
                f"{database_path}{safety.SQLITE_INITIALIZATION_STATE_SUFFIX}"
            )
            self.assertEqual(
                state_path.read_bytes(),
                safety.SQLITE_INITIALIZATION_INCOMPLETE,
            )
            with self.assertRaises(DatabaseSafetyError):
                safety.verify_test_database_before_connect(database_path)

    def test_post_commit_fsync_failure_is_commit_uncertain(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory).resolve() / "database.sqlite3"
            real_fsync_directory = safety._fsync_directory
            calls = []

            def fail_final(directory_path):
                calls.append(Path(directory_path))
                if len(calls) == 3:
                    raise DatabaseSafetyError(
                        "database_initialization_failed",
                        "Injected directory synchronization failure.",
                    )
                return real_fsync_directory(directory_path)

            with mock.patch.object(
                safety,
                "_fsync_directory",
                side_effect=fail_final,
            ):
                with self.assertRaises(DatabaseSafetyError) as raised:
                    initialize_sqlite_file(database_path)
            self.assertEqual(
                raised.exception.code,
                "database_initialization_commit_uncertain",
            )
            state_path = Path(
                f"{database_path}{safety.SQLITE_INITIALIZATION_STATE_SUFFIX}"
            )
            self.assertEqual(
                state_path.read_bytes(),
                safety.SQLITE_INITIALIZATION_READY,
            )

    def test_marker_commit_operations_have_approved_order(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory).resolve() / "database.sqlite3"
            events = []
            real_create_marker = safety._create_marker_exclusively
            real_create_database = safety._create_sqlite_file_exclusively
            real_fsync_directory = safety._fsync_directory
            real_replace = safety.os.replace

            def create_marker(path, contents):
                state = (
                    "incomplete"
                    if contents == safety.SQLITE_INITIALIZATION_INCOMPLETE
                    else "ready"
                )
                events.append(f"marker:{state}")
                return real_create_marker(path, contents)

            def create_database(path):
                events.append("database")
                return real_create_database(path)

            def fsync_directory(path):
                events.append("directory-fsync")
                return real_fsync_directory(path)

            def replace(source, destination):
                events.append("replace")
                return real_replace(source, destination)

            with mock.patch.object(
                safety,
                "_create_marker_exclusively",
                side_effect=create_marker,
            ), mock.patch.object(
                safety,
                "_create_sqlite_file_exclusively",
                side_effect=create_database,
            ), mock.patch.object(
                safety,
                "_fsync_directory",
                side_effect=fsync_directory,
            ), mock.patch.object(
                safety.os,
                "replace",
                side_effect=replace,
            ):
                initialize_sqlite_file(database_path)

            self.assertEqual(
                events,
                [
                    "marker:incomplete",
                    "directory-fsync",
                    "database",
                    "marker:ready",
                    "directory-fsync",
                    "replace",
                    "directory-fsync",
                ],
            )


class LiveConnectionTraceTests(unittest.TestCase):
    def test_django_connection_issues_version_query_before_any_other_sql(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory).resolve() / "database.sqlite3"
            initialize_sqlite_file(database_path)
            environment = {
                **os.environ,
                "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
                "MAX_SMART_TEST_DATABASE_PATH": str(database_path),
                "PYTHONPATH": str(SRC_ROOT),
            }
            script = r"""
from django.db.backends.sqlite3 import base as sqlite_base

statements = []
real_connect = sqlite_base.Database.connect


def tracing_connect(*args, **kwargs):
    connection = real_connect(*args, **kwargs)
    connection.set_trace_callback(
        lambda sql: statements.append(str(sql))
    )
    return connection


sqlite_base.Database.connect = tracing_connect
import django
django.setup()
from django.db import connection
connection.ensure_connection()
normalized = [" ".join(statement.split()) for statement in statements]
print(normalized[0])
print(
    "WAL_CHANGED"
    if any(
        "journal_mode=WAL" in statement.replace(" ", "").upper()
        for statement in normalized
    )
    else "WAL_UNCHANGED"
)
readbacks = (
    "PRAGMA foreign_keys",
    "PRAGMA journal_mode",
    "PRAGMA synchronous",
    "PRAGMA busy_timeout",
    "PRAGMA recursive_triggers",
)
print(",".join(statement for statement in normalized if statement in readbacks))
"""
            result = subprocess.run(
                (sys.executable, "-B", "-c", script),
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = result.stdout.splitlines()
            self.assertEqual(lines[0], "SELECT sqlite_version()")
            self.assertEqual(lines[1], "WAL_UNCHANGED")
            self.assertEqual(
                lines[2],
                "PRAGMA foreign_keys,PRAGMA journal_mode,PRAGMA synchronous,"
                "PRAGMA busy_timeout,PRAGMA recursive_triggers",
            )


class AcceptedReplacementTests(unittest.TestCase):
    def test_same_constraint_replacement_passes_file_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            database_path = root / "database.sqlite3"
            replacement = root / "replacement.sqlite3"
            initialize_sqlite_file(database_path)
            initialize_sqlite_file(replacement)

            safety.verify_test_database_before_connect(database_path)
            os.replace(replacement, database_path)
            safety.verify_test_database_before_connect(database_path)


if __name__ == "__main__":
    unittest.main()
