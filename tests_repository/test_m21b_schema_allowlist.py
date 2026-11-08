"""Disposable D17 framework-schema allowlist verification."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
_APPROVED_FRAMEWORK_SCHEMA = (
    ("table", "auth_group", "auth_group"),
    ("table", "auth_group_permissions", "auth_group_permissions"),
    ("table", "auth_permission", "auth_permission"),
    ("table", "auth_user", "auth_user"),
    ("table", "auth_user_groups", "auth_user_groups"),
    ("table", "auth_user_user_permissions", "auth_user_user_permissions"),
    ("table", "django_content_type", "django_content_type"),
    ("table", "django_migrations", "django_migrations"),
    ("table", "sqlite_sequence", "sqlite_sequence"),
    ("index", "auth_group_permissions_group_id_b120cbf9", "auth_group_permissions"),
    (
        "index",
        "auth_group_permissions_group_id_permission_id_0cd325b0_uniq",
        "auth_group_permissions",
    ),
    ("index", "auth_group_permissions_permission_id_84c5c92e", "auth_group_permissions"),
    ("index", "auth_permission_content_type_id_2f476e4b", "auth_permission"),
    (
        "index",
        "auth_permission_content_type_id_codename_01ab375a_uniq",
        "auth_permission",
    ),
    ("index", "auth_user_groups_group_id_97559544", "auth_user_groups"),
    ("index", "auth_user_groups_user_id_6a12ed8b", "auth_user_groups"),
    (
        "index",
        "auth_user_groups_user_id_group_id_94350c0c_uniq",
        "auth_user_groups",
    ),
    (
        "index",
        "auth_user_user_permissions_permission_id_1fbb5f2c",
        "auth_user_user_permissions",
    ),
    (
        "index",
        "auth_user_user_permissions_user_id_a95ead1b",
        "auth_user_user_permissions",
    ),
    (
        "index",
        "auth_user_user_permissions_user_id_permission_id_14a6b632_uniq",
        "auth_user_user_permissions",
    ),
    (
        "index",
        "django_content_type_app_label_model_76bd3d3b_uniq",
        "django_content_type",
    ),
    ("index", "sqlite_autoindex_auth_group_1", "auth_group"),
    ("index", "sqlite_autoindex_auth_user_1", "auth_user"),
)

_PREAMBLE = """\
import json
import os
from pathlib import Path

database = Path(os.environ["MAX_SMART_TEST_DATABASE_PATH"])
from max_smart_db.safety import initialize_sqlite_file
from max_smart_db.sql_policy import scan_migration_package
initialize_sqlite_file(database)
inventory = scan_migration_package()

import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from max_smart_db.maintenance import _perform_one_step, classify_boundary
from max_smart_db.startup import (
    StartupIntegrityError,
    verify_approved_schema,
    verify_fresh_bootstrap,
)

def step():
    return _perform_one_step(
        connection,
        "forward",
        inventory,
        MigrationExecutor,
    )

while classify_boundary(connection, inventory).name != "B3":
    step()
"""


def _run_disposable(body):
    with tempfile.TemporaryDirectory() as temporary:
        database = Path(temporary) / "d17.sqlite3"
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(database),
            "PYTHONPATH": str(SRC_ROOT),
        }
        return subprocess.run(
            (
                sys.executable,
                "-B",
                "-c",
                _PREAMBLE + "\n" + textwrap.dedent(body),
            ),
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )


class FrameworkSchemaAllowlistTests(unittest.TestCase):
    def test_d17_exact_framework_and_separate_v1_inventory(self):
        approved = json.dumps(list(_APPROVED_FRAMEWORK_SCHEMA))
        result = _run_disposable(
            f"""\
            approved = {approved}
            verification = verify_approved_schema(connection, inventory)
            framework = [list(item) for item in verification.framework]
            index_name = "django_content_type_app_label_model_76bd3d3b_uniq"

            def stored_sql(name):
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT sql FROM sqlite_schema WHERE name = %s;",
                        (name,),
                    )
                    return cursor.fetchone()[0]

            def rejection(statement):
                with connection.cursor() as cursor:
                    cursor.execute(statement)
                try:
                    verify_approved_schema(connection, inventory)
                except StartupIntegrityError as error:
                    return error.code
                return None

            original_index = stored_sql(index_name)
            original_trigger = stored_sql("msv1_slot_bd_no_delete_trg")
            outcomes = {{
                "unexpected": rejection(
                    "CREATE TABLE unexpected_framework (id INTEGER);"
                ),
            }}
            with connection.cursor() as cursor:
                cursor.execute("DROP TABLE unexpected_framework;")
                outcomes["view"] = rejection(
                    "CREATE VIEW unexpected_framework_view AS SELECT 1 AS value;"
                )
                cursor.execute("DROP VIEW unexpected_framework_view;")
                outcomes["trigger"] = rejection(
                    "CREATE TRIGGER unexpected_framework_trg "
                    "BEFORE DELETE ON auth_user BEGIN SELECT 1; END;"
                )
                cursor.execute("DROP TRIGGER unexpected_framework_trg;")
                cursor.execute("DROP INDEX " + index_name + ";")
            try:
                verify_approved_schema(connection, inventory)
            except StartupIntegrityError as error:
                outcomes["missing"] = error.code
            else:
                outcomes["missing"] = None
            with connection.cursor() as cursor:
                cursor.execute(
                    "CREATE INDEX " + index_name + " ON auth_user(username);"
                )
            try:
                verify_approved_schema(connection, inventory)
            except StartupIntegrityError as error:
                outcomes["wrong_association"] = error.code
            else:
                outcomes["wrong_association"] = None
            with connection.cursor() as cursor:
                cursor.execute("DROP INDEX " + index_name + ";")
                cursor.execute(
                    "CREATE TABLE " + index_name + " (id INTEGER);"
                )
            try:
                verify_approved_schema(connection, inventory)
            except StartupIntegrityError as error:
                outcomes["wrong_type"] = error.code
            else:
                outcomes["wrong_type"] = None
            with connection.cursor() as cursor:
                cursor.execute("DROP TABLE " + index_name + ";")
                cursor.execute(original_index)
                cursor.execute(
                    "SELECT rootpage, sql FROM sqlite_schema "
                    "WHERE name = 'sqlite_sequence';"
                )
                sequence_rootpage, sequence_sql = cursor.fetchone()
                cursor.execute("PRAGMA writable_schema=ON;")
                cursor.execute(
                    "DELETE FROM sqlite_schema WHERE name = 'sqlite_sequence';"
                )
                cursor.execute("PRAGMA writable_schema=RESET;")
            connection.close()
            try:
                verify_approved_schema(connection, inventory)
            except StartupIntegrityError as error:
                outcomes["missing_sqlite_sequence"] = error.code
            else:
                outcomes["missing_sqlite_sequence"] = None
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA writable_schema=ON;")
                cursor.execute(
                    "INSERT INTO sqlite_schema "
                    "(type, name, tbl_name, rootpage, sql) "
                    "VALUES ('table', 'sqlite_sequence', 'sqlite_sequence', "
                    "%s, %s);",
                    (sequence_rootpage, sequence_sql),
                )
                cursor.execute("PRAGMA writable_schema=RESET;")
            connection.close()
            with connection.cursor() as cursor:
                cursor.execute("DROP TRIGGER msv1_slot_bd_no_delete_trg;")
            try:
                verify_approved_schema(connection, inventory)
            except StartupIntegrityError as error:
                outcomes["v1_missing"] = error.code
            else:
                outcomes["v1_missing"] = None
            with connection.cursor() as cursor:
                cursor.execute(original_trigger)
                outcomes["v1_unexpected"] = rejection(
                    "CREATE INDEX unexpected_v1_ix ON role(display_name);"
                )
                cursor.execute("DROP INDEX unexpected_v1_ix;")
                cursor.execute(
                    "CREATE TABLE unexpected_framework (id INTEGER);"
                )
            try:
                verify_fresh_bootstrap(connection, inventory)
            except StartupIntegrityError as error:
                outcomes["startup"] = error.code
            else:
                outcomes["startup"] = None
            with connection.cursor() as cursor:
                cursor.execute("DROP TABLE unexpected_framework;")
            restored = verify_approved_schema(connection, inventory)
            print(json.dumps([
                framework,
                [list(item) for item in restored.framework],
                sorted(verification.v1_names),
                outcomes,
            ]))
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        framework, restored, v1_names, outcomes = json.loads(result.stdout)
        approved = [
            list(item) for item in sorted(_APPROVED_FRAMEWORK_SCHEMA)
        ]
        self.assertEqual(framework, approved)
        self.assertEqual(restored, approved)
        self.assertEqual(
            [row[0] for row in framework].count("table"),
            9,
        )
        self.assertEqual(
            [row[0] for row in framework].count("index"),
            14,
        )
        self.assertTrue(
            set(v1_names).isdisjoint(row[1] for row in framework)
        )
        self.assertEqual(
            outcomes,
            {
                "unexpected": "framework_schema_unexpected",
                "view": "framework_schema_type_rejected",
                "trigger": "framework_schema_type_rejected",
                "missing": "framework_schema_missing",
                "wrong_association": "framework_schema_mismatch",
                "wrong_type": "framework_schema_mismatch",
                "missing_sqlite_sequence": "framework_schema_missing",
                "v1_missing": "v1_schema_invalid",
                "v1_unexpected": "v1_schema_invalid",
                "startup": "framework_schema_unexpected",
            },
        )
