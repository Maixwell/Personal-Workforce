"""Package C tests for boundary-aware exclusive maintenance."""

import builtins
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.core.management.base import CommandError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from max_smart_db import maintenance  # noqa: E402
from max_smart_db.management.commands.exclusive_maintenance import (  # noqa: E402
    Command,
)
from max_smart_db.sql_policy import (  # noqa: E402
    MigrationPolicyError,
    scan_migration_package,
)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = None

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        return False

    def execute(self, statement, parameters=()):
        self.connection.statements.append((statement, parameters))
        self.rows = self.connection.rows_for(statement, parameters)
        return self

    def fetchall(self):
        return self.rows


class FakeConnection:
    alias = "default"
    in_atomic_block = False

    def __init__(self, boundary, inventory):
        self.boundary = boundary
        self.inventory = inventory
        self.statements = []
        self.closed = False
        self.extra_objects = {}
        self.recorder_override = None
        self.role_override = None
        self.slot_override = None
        self.settings_dict = {
            "ATOMIC_REQUESTS": False,
            "CONN_MAX_AGE": 0,
            "OPTIONS": {"transaction_mode": "IMMEDIATE"},
        }

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True

    def _boundary(self):
        return next(
            value
            for value in maintenance._BOUNDARIES
            if value.name == self.boundary
        )

    def _objects(self):
        objects = dict(
            maintenance._expected_objects(
                self._boundary(),
                self.inventory,
            )
        )
        objects.update(self.extra_objects)
        return objects

    def rows_for(self, statement, parameters):
        if statement == "PRAGMA integrity_check;":
            return [("ok",)]
        if statement == "PRAGMA foreign_key_check;":
            return []
        if statement.startswith("SELECT type, name, tbl_name, sql"):
            rows = [
                ("table", "django_migrations", "django_migrations", "approved")
            ]
            rows.extend(
                (object_type, name, table, sql)
                for name, (object_type, table, sql) in self._objects().items()
            )
            return sorted(rows)
        if statement.startswith("SELECT name FROM django_migrations"):
            names = (
                self._boundary().migrations
                if self.recorder_override is None
                else self.recorder_override
            )
            return [(name,) for name in names]
        if statement.startswith("SELECT slug, display_name, active"):
            if self.role_override is not None:
                return self.role_override
            if self.boundary == "B3":
                return [("max-smart", "Max Smart", 1)]
            return []
        if statement.startswith("SELECT id, operation_id"):
            if self.slot_override is not None:
                return self.slot_override
            if self.boundary == "B3":
                return [(1, None, None, None, None, None)]
            return []
        if "SELECT EXISTS (SELECT 1 FROM role" in statement:
            return [(1 if self.role_override else 0,)]
        if "SELECT EXISTS (SELECT 1 FROM global_model_slot" in statement:
            return [(1 if self.slot_override else 0,)]
        raise AssertionError(f"Unexpected statement: {statement!r}")


class FakeMigration:
    app_label = "max_smart_db"

    def __init__(self, name):
        self.name = name


class FakeExecutor:
    def __init__(self, connection):
        self.connection = connection

    def migration_plan(self, targets):
        target_name = targets[0][1]
        if self.connection.boundary == "B0":
            name, reverse = "0001_v1_schema", False
        elif self.connection.boundary == "B1" and target_name is None:
            name, reverse = "0001_v1_schema", True
        elif self.connection.boundary == "B1":
            name, reverse = "0002_v1_integrity_triggers", False
        elif self.connection.boundary == "B2" and target_name == "0001_v1_schema":
            name, reverse = "0002_v1_integrity_triggers", True
        elif self.connection.boundary == "B2":
            name, reverse = "0003_v1_seeds_and_retention", False
        else:
            name, reverse = "0003_v1_seeds_and_retention", True
        return [(FakeMigration(name), reverse)]

    def migrate(self, targets, plan):
        transitions = {
            ("B0", False): "B1",
            ("B1", False): "B2",
            ("B2", False): "B3",
            ("B1", True): "B0",
            ("B2", True): "B1",
            ("B3", True): "B2",
        }
        self.connection.boundary = transitions[
            (self.connection.boundary, plan[0][1])
        ]


class MaintenanceBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = scan_migration_package()

    def test_all_four_exact_boundaries_are_recognized(self):
        for name in ("B0", "B1", "B2", "B3"):
            with self.subTest(boundary=name):
                connection = FakeConnection(name, self.inventory)
                result = maintenance.classify_boundary(
                    connection,
                    self.inventory,
                )
                self.assertEqual(result.name, name)

    def test_recorder_or_object_crash_gap_fails_without_repair(self):
        connection = FakeConnection("B2", self.inventory)
        connection.recorder_override = (
            "0001_v1_schema",
            "0003_v1_seeds_and_retention",
        )
        with self.assertRaises(maintenance.MaintenanceError) as recorder:
            maintenance.classify_boundary(connection, self.inventory)
        self.assertEqual(recorder.exception.code, "unrecognized_boundary")
        self.assertEqual(connection.boundary, "B2")

        connection = FakeConnection("B2", self.inventory)
        connection.extra_objects["msv1_unapproved_trg"] = (
            "trigger",
            "role",
            "CREATE TRIGGER msv1_unapproved_trg",
        )
        with self.assertRaises(maintenance.MaintenanceError) as physical:
            maintenance.classify_boundary(connection, self.inventory)
        self.assertEqual(physical.exception.code, "unrecognized_boundary")
        self.assertEqual(connection.boundary, "B2")

    def test_one_forward_step_reaches_only_the_next_boundary(self):
        connection = FakeConnection("B2", self.inventory)

        result = maintenance._perform_one_step(
            connection,
            "forward",
            self.inventory,
            FakeExecutor,
        )

        self.assertEqual(
            (result.before, result.after, result.migration),
            ("B2", "B3", "0003_v1_seeds_and_retention"),
        )

    def test_forward_0003_refuses_nonempty_seed_tables(self):
        connection = FakeConnection("B2", self.inventory)
        connection.role_override = [("unexpected", "Unexpected", 1)]

        with self.assertRaises(maintenance.MaintenanceError) as raised:
            maintenance._require_forward_0003_empty(connection)

        self.assertEqual(raised.exception.code, "forward_0003_not_empty")


class MaintenanceCommandBoundaryTests(unittest.TestCase):
    def test_scanner_failure_happens_before_executor_import(self):
        imported = []
        original_import = builtins.__import__

        def tracked_import(name, *args, **kwargs):
            if name == "django.db.migrations.executor":
                imported.append(name)
            return original_import(name, *args, **kwargs)

        with mock.patch.object(
            maintenance,
            "scan_migration_package",
            side_effect=MigrationPolicyError("rejected"),
        ), mock.patch("builtins.__import__", side_effect=tracked_import):
            with self.assertRaises(MigrationPolicyError):
                maintenance.run_exclusive_maintenance("forward")

        self.assertEqual(imported, [])

    def test_public_allowlist_keeps_migrate_and_sqlmigrate_disabled(self):
        specification = importlib.util.spec_from_file_location(
            "project_manage",
            PROJECT_ROOT / "manage.py",
        )
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)

        self.assertIn("exclusive_maintenance", module._ALLOWED_COMMANDS)
        self.assertNotIn("migrate", module._ALLOWED_COMMANDS)
        self.assertNotIn("sqlmigrate", module._ALLOWED_COMMANDS)

    def test_command_parser_accepts_only_fixed_direction(self):
        parser = Command().create_parser("manage.py", "exclusive_maintenance")
        self.assertEqual(parser.parse_args(["forward"]).direction, "forward")
        self.assertEqual(parser.parse_args(["reverse"]).direction, "reverse")

        rejected = (
            ["max_smart_db", "forward"],
            ["forward", "0002_v1_integrity_triggers"],
            ["forward", "--database", "other"],
            ["forward", "--fake"],
            ["forward", "--plan"],
        )
        for arguments in rejected:
            with self.subTest(arguments=arguments), self.assertRaises(CommandError):
                parser.parse_args(arguments)

    def test_exclusive_command_requires_production_settings(self):
        settings = SimpleNamespace(
            MAX_SMART_DATABASE_KIND="test",
            DATABASES={
                "default": {
                    "NAME": "/tmp/disposable.sqlite3",
                }
            },
        )
        with mock.patch.dict(
            os.environ,
            {"DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings"},
        ):
            with self.assertRaises(maintenance.MaintenanceError) as raised:
                maintenance._require_production_settings(settings)
        self.assertEqual(
            raised.exception.code,
            "production_settings_required",
        )


if __name__ == "__main__":
    unittest.main()
