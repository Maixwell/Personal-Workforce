"""Package C tests for the pre-import migration AST scanner."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from max_smart_db.sql_policy import (  # noqa: E402
    MigrationPolicyError,
    scan_migration_package,
)


MIGRATION_ROOT = SRC_ROOT / "max_smart_db" / "migrations"


class MigrationPolicyTests(unittest.TestCase):
    def _copy_migrations(self, directory):
        destination = Path(directory) / "migrations"
        shutil.copytree(MIGRATION_ROOT, destination)
        return destination

    def test_approved_package_has_closed_84_statement_inventory(self):
        inventory = scan_migration_package()

        self.assertEqual(
            [len(inventory.forward_sql[name]) for name in sorted(inventory.forward_sql)],
            [23, 11, 6],
        )
        self.assertEqual(
            [len(inventory.reverse_sql[name]) for name in sorted(inventory.reverse_sql)],
            [23, 11, 6],
        )
        self.assertEqual(len(inventory.seed_sql), 4)
        self.assertEqual(len(inventory.object_sql), 40)

    def test_sql_and_import_policy_rejects_every_unapproved_form(self):
        cases = {
            "dynamic_sql": (
                "cursor.execute(AI_EMPTY_SQL)",
                'cursor.execute("SELECT " + "1")',
            ),
            "imported_sql": (
                "from django.db import migrations, models",
                "from django.db import migrations, models\n"
                "from unapproved import SQL",
            ),
            "orm_access": (
                "def forward_noop(apps, schema_editor):\n    return None",
                "def forward_noop(apps, schema_editor):\n"
                "    apps.get_model('auth', 'User').objects.all()",
            ),
            "migration_recorder": (
                "def forward_noop(apps, schema_editor):\n    return None",
                "def forward_noop(apps, schema_editor):\n"
                "    MigrationRecorder(schema_editor.connection)",
            ),
            "raw_query": (
                "def forward_noop(apps, schema_editor):\n    return None",
                "def forward_noop(apps, schema_editor):\n"
                "    apps.get_model('auth', 'User').objects.raw('SELECT 1')",
            ),
            "extra_query": (
                "def forward_noop(apps, schema_editor):\n    return None",
                "def forward_noop(apps, schema_editor):\n"
                "    apps.get_model('auth', 'User').objects.extra()",
            ),
            "execute_alias": (
                "cursor.execute(AI_EMPTY_SQL)",
                "run_sql = cursor.execute\n        run_sql(AI_EMPTY_SQL)",
            ),
            "executemany": (
                "cursor.execute(AI_EMPTY_SQL)",
                "cursor.executemany(AI_EMPTY_SQL, ())",
            ),
            "executescript": (
                "cursor.execute(AI_EMPTY_SQL)",
                "cursor.executescript(AI_EMPTY_SQL)",
            ),
            "eval": (
                "cursor.execute(AI_EMPTY_SQL)",
                "eval(AI_EMPTY_SQL)",
            ),
            "exec": (
                "cursor.execute(AI_EMPTY_SQL)",
                "exec(AI_EMPTY_SQL)",
            ),
            "unresolved_call": (
                "cursor.execute(AI_EMPTY_SQL)",
                "unapproved_callable(AI_EMPTY_SQL)",
            ),
            "pragma": (
                "cursor.execute(AI_EMPTY_SQL)",
                'cursor.execute("PRAGMA foreign_keys")',
            ),
            "transaction_sql": (
                "cursor.execute(AI_EMPTY_SQL)",
                'cursor.execute("BEGIN IMMEDIATE")',
            ),
            "extra_module": (None, None),
        }
        for label, replacement in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                migration_root = self._copy_migrations(temporary)
                if label == "extra_module":
                    (migration_root / "0004_unapproved.py").write_text(
                        '"""Not approved."""\n',
                        encoding="utf-8",
                    )
                else:
                    path = migration_root / "0001_v1_schema.py"
                    source = path.read_text(encoding="utf-8")
                    old, new = replacement
                    self.assertIn(old, source)
                    path.write_text(
                        source.replace(old, new, 1),
                        encoding="utf-8",
                    )
                with self.assertRaises(MigrationPolicyError):
                    scan_migration_package(migration_root)

    def test_scanner_rejects_before_import_side_effect_can_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            migration_root = self._copy_migrations(temporary)
            marker = Path(temporary) / "imported"
            path = migration_root / "0002_v1_integrity_triggers.py"
            source = path.read_text(encoding="utf-8")
            path.write_text(
                f"open({str(marker)!r}, 'w').write('imported')\n" + source,
                encoding="utf-8",
            )

            with self.assertRaises(MigrationPolicyError):
                scan_migration_package(migration_root)

            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
