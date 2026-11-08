"""Focused tests for the Milestone 2.1a database foundation."""

import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from max_smart_db.config import (  # noqa: E402
    PRODUCTION_DATABASE_PATH,
    SQLITE_BUSY_TIMEOUT_MS,
    database_configuration,
)
from max_smart_db.safety import (  # noqa: E402
    DatabaseSafetyError,
    initialize_sqlite_file,
    validate_production_database_path,
    validate_test_database_path,
    verify_git_protection,
)


APPROVED_GITIGNORE = """\
data/*.sqlite3*
data/*-wal
data/*-shm
data/backups/
data/.backup-*
data/.restore-*
data/.migration-rehearsal-*
"""


class DatabaseFoundationTests(unittest.TestCase):
    def test_production_configuration_is_fixed_and_does_not_connect(self):
        before_exists = PRODUCTION_DATABASE_PATH.exists()

        validate_production_database_path()
        configuration = database_configuration(PRODUCTION_DATABASE_PATH)

        self.assertEqual(
            configuration["ENGINE"],
            "max_smart_db.backend",
        )
        self.assertEqual(
            configuration["NAME"],
            str(PRODUCTION_DATABASE_PATH),
        )
        self.assertFalse(configuration["ATOMIC_REQUESTS"])
        self.assertEqual(configuration["CONN_MAX_AGE"], 0)
        self.assertEqual(configuration["OPTIONS"]["timeout"], 5)
        self.assertEqual(
            configuration["OPTIONS"]["transaction_mode"],
            "IMMEDIATE",
        )
        self.assertEqual(before_exists, PRODUCTION_DATABASE_PATH.exists())

    def test_test_database_path_must_be_absolute_and_outside_repository(self):
        with self.assertRaises(DatabaseSafetyError) as relative:
            validate_test_database_path("test.sqlite3")
        self.assertEqual(relative.exception.code, "test_path_not_absolute")

        with self.assertRaises(DatabaseSafetyError) as in_repository:
            validate_test_database_path(PROJECT_ROOT / "test.sqlite3")
        self.assertEqual(
            in_repository.exception.code,
            "test_path_in_repository",
        )

    def test_explicit_test_initialization_sets_pragmas_and_permissions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "foundation.sqlite3"

            initialize_sqlite_file(database_path)

            self.assertEqual(
                stat.S_IMODE(database_path.stat().st_mode),
                0o600,
            )
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                self.assertEqual(
                    connection.execute("PRAGMA journal_mode").fetchone()[0],
                    "wal",
                )
                self.assertEqual(
                    connection.execute("PRAGMA synchronous").fetchone()[0],
                    2,
                )
            finally:
                connection.close()

    def test_django_test_settings_verify_every_connection(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "django.sqlite3"
            initialize_sqlite_file(database_path)
            environment = {
                **os.environ,
                "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
                "MAX_SMART_TEST_DATABASE_PATH": str(database_path),
                "PYTHONPATH": str(SRC_ROOT),
            }
            script = f"""\
import django
django.setup()
from django.conf import settings
from django.db import connection
connection.ensure_connection()
with connection.cursor() as cursor:
    values = []
    for pragma in (
        "foreign_keys",
        "journal_mode",
        "synchronous",
        "busy_timeout",
        "recursive_triggers",
    ):
        cursor.execute("PRAGMA " + pragma)
        values.append(str(cursor.fetchone()[0]).lower())
print(",".join(values))
print(settings.DATABASES["default"]["OPTIONS"]["transaction_mode"])
print(connection.get_autocommit())
print(settings.is_overridden("SECRET_KEY"))
"""

            result = subprocess.run(
                (sys.executable, "-B", "-c", script),
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.splitlines(),
                [
                    f"1,wal,2,{SQLITE_BUSY_TIMEOUT_MS},1",
                    "IMMEDIATE",
                    "True",
                    "False",
                ],
            )

    def test_database_bootstrap_does_not_create_production_database(self):
        before_exists = PRODUCTION_DATABASE_PATH.exists()
        environment = {
            **os.environ,
            "PYTHONPATH": str(SRC_ROOT),
        }
        environment.pop("DJANGO_SETTINGS_MODULE", None)

        result = subprocess.run(
            (
                sys.executable,
                "-B",
                "-c",
                "from max_smart_db.bootstrap import "
                "setup_database_django; setup_database_django(); "
                "from django.db import connections; "
                "assert connections['default'].connection is None",
            ),
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before_exists, PRODUCTION_DATABASE_PATH.exists())

    def test_current_repository_git_protection_passes(self):
        verify_git_protection(PROJECT_ROOT)

    def test_git_preflight_rejects_unignored_and_tracked_paths(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            git_root = Path(temporary_directory)
            subprocess.run(
                ("git", "init", "--quiet"),
                cwd=git_root,
                check=True,
            )
            (git_root / ".gitignore").write_text(
                APPROVED_GITIGNORE,
                encoding="utf-8",
            )
            verify_git_protection(git_root)

            (git_root / ".gitignore").write_text(
                APPROVED_GITIGNORE.replace("data/.restore-*\n", ""),
                encoding="utf-8",
            )
            with self.assertRaises(DatabaseSafetyError) as unignored:
                verify_git_protection(git_root)
            self.assertEqual(
                unignored.exception.code,
                "git_path_not_ignored",
            )

            (git_root / ".gitignore").write_text(
                APPROVED_GITIGNORE,
                encoding="utf-8",
            )
            data_directory = git_root / "data"
            data_directory.mkdir()
            database_path = data_directory / "max_smart.sqlite3"
            database_path.touch()
            subprocess.run(
                ("git", "add", "--force", "data/max_smart.sqlite3"),
                cwd=git_root,
                check=True,
            )
            with self.assertRaises(DatabaseSafetyError) as tracked:
                verify_git_protection(git_root)
            self.assertEqual(tracked.exception.code, "git_path_tracked")


if __name__ == "__main__":
    unittest.main()
