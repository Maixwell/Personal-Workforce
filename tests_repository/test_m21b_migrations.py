"""Disposable-database integration evidence for M2.1b Package D."""

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
from max_smart_db.maintenance import (
    MaintenanceError,
    _perform_one_step,
    classify_boundary,
)

def step(direction):
    return _perform_one_step(
        connection,
        direction,
        inventory,
        MigrationExecutor,
    )

def advance(boundary):
    order = {"B0": 0, "B1": 1, "B2": 2, "B3": 3}
    while order[classify_boundary(connection, inventory).name] < order[boundary]:
        step("forward")
"""


def _run_disposable(body, *, timeout=30):
    with tempfile.TemporaryDirectory() as temporary:
        database = Path(temporary) / "package-d.sqlite3"
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(database),
            "PYTHONPATH": str(SRC_ROOT),
        }
        result = subprocess.run(
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
            timeout=timeout,
        )
    return result


class MigrationBoundaryIntegrationTests(unittest.TestCase):
    def test_d1_all_legal_one_step_transitions(self):
        result = _run_disposable(
            """\
            observed = [classify_boundary(connection, inventory).name]
            for direction in (
                "forward", "forward", "forward",
                "reverse", "reverse", "reverse",
            ):
                observed.append(step(direction).after)
            print(json.dumps(observed))
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            ["B0", "B1", "B2", "B3", "B2", "B1", "B0"],
        )

    def test_d2_forward_0003_refuses_nonempty_seed_table(self):
        result = _run_disposable(
            """\
            advance("B2")
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO role (slug, display_name, active) "
                    "VALUES ('unexpected', 'Unexpected', 1);"
                )
            try:
                step("forward")
            except MaintenanceError as error:
                code = error.code
            else:
                code = None
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM django_migrations "
                    "WHERE app = 'max_smart_db' "
                    "AND name = '0003_v1_seeds_and_retention';"
                )
                count = cursor.fetchone()[0]
            print(json.dumps([code, count]))
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            ["unrecognized_boundary", 0],
        )

    def test_d5_outer_atomic_is_rejected_without_a_transition(self):
        result = _run_disposable(
            """\
            from django.db import transaction
            from django.db.utils import NotSupportedError
            try:
                with transaction.atomic():
                    step("forward")
            except NotSupportedError:
                rejected = True
            else:
                rejected = False
            print(json.dumps([
                rejected,
                classify_boundary(connection, inventory).name,
            ]))
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [True, "B0"])

    def test_d6_d7_begin_immediate_and_lock_timeouts(self):
        result = _run_disposable(
            """\
            import sqlite3
            import time
            from django.db import transaction

            connection.ensure_connection()
            trace = []
            connection.connection.set_trace_callback(trace.append)
            with transaction.atomic():
                pass

            holder = sqlite3.connect(
                f"file:{database}?mode=rw",
                uri=True,
                timeout=5,
                isolation_level=None,
            )
            holder.execute("BEGIN IMMEDIATE")
            outcomes = []
            for timeout in (0, 5):
                contender = sqlite3.connect(
                    f"file:{database}?mode=rw",
                    uri=True,
                    timeout=timeout,
                    isolation_level=None,
                )
                statements = []
                contender.set_trace_callback(statements.append)
                started = time.monotonic()
                try:
                    contender.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as error:
                    elapsed = time.monotonic() - started
                    outcomes.append([
                        "locked" in str(error).lower(),
                        elapsed,
                        statements.count("BEGIN IMMEDIATE"),
                    ])
                else:
                    outcomes.append([False, 0, 0])
                finally:
                    contender.close()
            holder.rollback()
            holder.close()
            print(json.dumps([
                trace.count("BEGIN IMMEDIATE"),
                outcomes,
            ]))
            """,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        begin_count, outcomes = json.loads(result.stdout)
        self.assertEqual(begin_count, 1)
        self.assertEqual(outcomes[0][0], True)
        self.assertLess(outcomes[0][1], 1)
        self.assertEqual(outcomes[0][2], 1)
        self.assertEqual(outcomes[1][0], True)
        self.assertGreaterEqual(outcomes[1][1], 4.5)
        self.assertLess(outcomes[1][1], 7)
        self.assertEqual(outcomes[1][2], 1)

    def test_d8_dangling_foreign_key_rolls_back_in_child(self):
        result = _run_disposable(
            """\
            advance("B1")
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                    "ORDER BY type, name;"
                )
                schema_before = cursor.fetchall()
                cursor.execute(
                    "SELECT app, name FROM django_migrations "
                    "ORDER BY app, name;"
                )
                recorder_before = cursor.fetchall()
            from django.db import IntegrityError
            try:
                with connection.schema_editor() as editor:
                    editor.execute(
                        "INSERT INTO authorized_identity "
                        "(id, user_id, tailscale_login, enabled, "
                        "created_at_us, last_seen_at_us) "
                        "VALUES (1, 999999, 'missing@example.test', 1, 0, NULL);"
                    )
            except IntegrityError:
                rejected = True
            else:
                rejected = False
            connection.close()

            import sqlite3
            fresh = sqlite3.connect(f"file:{database}?mode=rw", uri=True)
            schema_after = fresh.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                "ORDER BY type, name;"
            ).fetchall()
            recorder_after = fresh.execute(
                "SELECT app, name FROM django_migrations ORDER BY app, name;"
            ).fetchall()
            identity_count = fresh.execute(
                "SELECT COUNT(*) FROM authorized_identity;"
            ).fetchone()[0]
            fresh.close()
            print(json.dumps([
                rejected,
                schema_before == schema_after,
                recorder_before == recorder_after,
                identity_count,
            ]))
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [True, True, True, 0])

    def test_d9_backward_sql_snapshots_contain_only_frozen_drops(self):
        result = _run_disposable(
            """\
            from io import StringIO
            from django.core.management import call_command

            def statements(value):
                lines = [
                    line for line in value.splitlines()
                    if line.strip()
                    and line.strip() not in {"BEGIN;", "COMMIT;"}
                    and not line.lstrip().startswith("--")
                ]
                combined = "\\n".join(lines)
                return [
                    part.strip() + ";"
                    for part in combined.split(";")
                    if part.strip()
                ]

            actual = {}
            for name in (
                "0001_v1_schema",
                "0002_v1_integrity_triggers",
                "0003_v1_seeds_and_retention",
            ):
                output = StringIO()
                call_command(
                    "sqlmigrate",
                    "max_smart_db",
                    name,
                    backwards=True,
                    stdout=output,
                )
                actual[name] = statements(output.getvalue())
            expected = {
                "0001_v1_schema": list(reversed(
                    inventory.reverse_sql["0001_v1_schema.py"]
                )),
                "0002_v1_integrity_triggers": list(reversed(
                    inventory.reverse_sql["0002_v1_integrity_triggers.py"]
                )),
                "0003_v1_seeds_and_retention": list(reversed(
                    inventory.reverse_sql["0003_v1_seeds_and_retention.py"]
                )),
            }
            print(json.dumps([
                actual == expected,
                any(
                    statement.startswith("DELETE")
                    for statement in actual["0003_v1_seeds_and_retention"]
                ),
                {name: len(values) for name, values in actual.items()},
            ]))
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [
                True,
                False,
                {
                    "0001_v1_schema": 23,
                    "0002_v1_integrity_triggers": 11,
                    "0003_v1_seeds_and_retention": 6,
                },
            ],
        )

    def test_d12_d13_each_reverse_crash_gap_is_rejected_and_redacted(self):
        cases = (
            ("0001_v1_schema.py", "B1"),
            ("0002_v1_integrity_triggers.py", "B2"),
            ("0003_v1_seeds_and_retention.py", "B3"),
        )
        for filename, boundary in cases:
            with self.subTest(migration=filename):
                result = _run_disposable(
                    f"""\
                    advance({boundary!r})
                    from django.db import transaction
                    with transaction.atomic():
                        with connection.cursor() as cursor:
                            for statement in reversed(
                                inventory.reverse_sql[{filename!r}]
                            ):
                                cursor.execute(statement)
                            if {filename!r}.startswith("0003"):
                                cursor.execute(
                                    inventory.seed_sql["SLOT_DELETE_SQL"]
                                )
                                cursor.execute(
                                    inventory.seed_sql["ROLE_DELETE_SQL"]
                                )
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT type, name, tbl_name FROM sqlite_schema "
                            "ORDER BY type, name;"
                        )
                        schema_before = cursor.fetchall()
                        cursor.execute(
                            "SELECT app, name FROM django_migrations "
                            "ORDER BY app, name;"
                        )
                        recorder_before = cursor.fetchall()
                    try:
                        classify_boundary(connection, inventory)
                    except MaintenanceError as error:
                        code = error.code
                    else:
                        code = None
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT type, name, tbl_name FROM sqlite_schema "
                            "ORDER BY type, name;"
                        )
                        schema_after = cursor.fetchall()
                        cursor.execute(
                            "SELECT app, name FROM django_migrations "
                            "ORDER BY app, name;"
                        )
                        recorder_after = cursor.fetchall()
                    from max_smart_db.startup import boundary_diagnostics
                    diagnostic = boundary_diagnostics(connection)
                    value = repr(diagnostic)
                    print(json.dumps([
                        code,
                        schema_before == schema_after,
                        recorder_before == recorder_after,
                        diagnostic.migration_counts,
                        diagnostic.integrity_check,
                        diagnostic.foreign_key_check,
                        "request_content" in value,
                        "message content" in value,
                    ]))
                    """
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                evidence = json.loads(result.stdout)
                self.assertEqual(evidence[0], "unrecognized_boundary")
                self.assertEqual(evidence[1:3], [True, True])
                self.assertEqual(evidence[4:6], ["ok", "ok"])
                self.assertEqual(evidence[6:], [False, False])

    def test_d14_all_ten_nul_checks_accept_valid_and_reject_nul(self):
        result = _run_disposable(
            textwrap.dedent(
                r"""
            advance("B1")
            from django.db import IntegrityError, transaction

            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO auth_user "
                    "(id, password, last_login, is_superuser, username, "
                    "first_name, last_name, email, is_staff, is_active, date_joined) "
                    "VALUES (1, '', NULL, 0, 'owner', '', '', '', 0, 1, '2026-01-01');"
                )
                cursor.execute(
                    "INSERT INTO role (slug, display_name, active) "
                    "VALUES ('max-smart', 'Max Smart', 1);"
                )
                cursor.execute(
                    "INSERT INTO conversation "
                    "(id, owner_id, role_slug, version, next_sequence, title, "
                    "created_at_us, updated_at_us, archived_at_us) "
                    "VALUES ('11111111111111111111111111111111', 1, 'max-smart', "
                    "0, 1, NULL, 0, 0, NULL);"
                )

            operation_sql = (
                "INSERT INTO operation "
                "(id, conversation_id, owner_id, operation_type, provider_id, "
                "model_id, status, blocks_conversation, expected_version, "
                "request_content, fingerprint_version, request_hash, retry_of, "
                "runner_id, reservation_expires_at_us, provider_started_at_us, "
                "execution_deadline_us, lease_expires_at_us, created_at_us, "
                "finished_at_us, acknowledged_at_us, failure_category) "
                "VALUES (%s, '11111111111111111111111111111111', 1, 'message', "
                "'openai', 'gpt-5.6-sol', 'QUEUED', 1, 0, %s, 1, %s, NULL, "
                "%s, 1, NULL, NULL, NULL, 0, NULL, NULL, NULL);"
            )
            message_sql = (
                "INSERT INTO message "
                "(id, conversation_id, sequence, role, content, operation_id, "
                "created_at_us) VALUES (%s, "
                "'11111111111111111111111111111111', 1, 'user', %s, NULL, 0);"
            )
            idempotency_sql = (
                "INSERT INTO idempotency_key "
                "(id, owner_id, conversation_id, operation_type, expected_version, "
                "key_digest_version, key_digest, fingerprint_version, request_hash, "
                "operation_id, expires_at_us) VALUES (10, 1, "
                "'11111111111111111111111111111111', 'message', 0, 1, %s, 1, %s, "
                "'33333333333333333333333333333333', 0);"
            )
            slot_sql = (
                "INSERT INTO global_model_slot "
                "(id, operation_id, runner_id, reservation_expires_at_us, "
                "lease_expires_at_us, acquired_at_us) VALUES "
                "(1, '33333333333333333333333333333333', %s, 1, NULL, 0);"
            )
            cases = [
                (
                    "conversation_id",
                    "INSERT INTO conversation "
                    "(id, owner_id, role_slug, version, next_sequence, title, "
                    "created_at_us, updated_at_us, archived_at_us) "
                    "VALUES (%s, 1, 'max-smart', 0, 1, NULL, 0, 0, NULL);",
                    ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",),
                    ("a" * 31 + "\x00",),
                    "msv1_conv_id_fmt_ck",
                ),
                (
                    "operation_id",
                    operation_sql,
                    ("5" * 32, "request", "a" * 64, "A" * 22),
                    ("5" * 31 + "\x00", "request", "a" * 64, "A" * 22),
                    "msv1_op_id_fmt_ck",
                ),
                (
                    "operation_request",
                    operation_sql,
                    ("5" * 32, "request", "a" * 64, "A" * 22),
                    ("5" * 32, "re\x00quest", "a" * 64, "A" * 22),
                    "msv1_op_request_ck",
                ),
                (
                    "operation_hash",
                    operation_sql,
                    ("5" * 32, "request", "a" * 64, "A" * 22),
                    ("5" * 32, "request", "a" * 63 + "\x00", "A" * 22),
                    "msv1_op_hash_ck",
                ),
                (
                    "operation_runner",
                    operation_sql,
                    ("5" * 32, "request", "a" * 64, "A" * 22),
                    ("5" * 32, "request", "a" * 64, "A" * 21 + "\x00"),
                    "msv1_op_runner_ck",
                ),
                (
                    "message_id",
                    message_sql,
                    ("4" * 32, "content"),
                    ("4" * 31 + "\x00", "content"),
                    "msv1_msg_id_fmt_ck",
                ),
                (
                    "message_content",
                    message_sql,
                    ("4" * 32, "content"),
                    ("4" * 32, "con\x00tent"),
                    "msv1_msg_content_ck",
                ),
                (
                    "idempotency_key_digest",
                    idempotency_sql,
                    ("b" * 64, "c" * 64),
                    ("b" * 63 + "\x00", "c" * 64),
                    "msv1_idem_key_hash_ck",
                ),
                (
                    "idempotency_request_hash",
                    idempotency_sql,
                    ("b" * 64, "c" * 64),
                    ("b" * 64, "c" * 63 + "\x00"),
                    "msv1_idem_req_hash_ck",
                ),
                (
                    "slot_runner",
                    slot_sql,
                    ("A" * 22,),
                    ("A" * 21 + "\x00",),
                    "msv1_slot_runner_ck",
                ),
            ]

            results = []
            base_operation_inserted = False
            for name, statement, valid, invalid, constraint in cases:
                if name.startswith("idempotency_") and not base_operation_inserted:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            operation_sql,
                            ("3" * 32, "request", "a" * 64, "A" * 22),
                        )
                    base_operation_inserted = True
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(statement, valid)
                    transaction.set_rollback(True)
                try:
                    with transaction.atomic():
                        with connection.cursor() as cursor:
                            cursor.execute(statement, invalid)
                except IntegrityError as error:
                    results.append([name, constraint in str(error)])
                else:
                    results.append([name, False])
            print(json.dumps(results))
            """
            ).lstrip()
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        results = json.loads(result.stdout)
        self.assertEqual(len(results), 10)
        self.assertTrue(all(passed for _, passed in results), results)

    def test_d15_uuidfield_persists_32_lowercase_hex(self):
        result = _run_disposable(
            """\
            advance("B1")
            import uuid
            executor = MigrationExecutor(connection)
            apps = executor.loader.project_state([
                ("max_smart_db", "0001_v1_schema")
            ]).apps
            User = apps.get_model("auth", "User")
            Role = apps.get_model("max_smart_db", "Role")
            Conversation = apps.get_model("max_smart_db", "Conversation")
            user = User.objects.create(username="owner")
            Role.objects.create(slug="max-smart", display_name="Max Smart", active=1)
            value = uuid.UUID("ABCDEFAB-CDEF-ABCD-EFAB-CDEFABCDEFAB")
            Conversation.objects.create(
                id=value,
                owner_id=user.pk,
                role_id="max-smart",
                version=0,
                next_sequence=1,
                title=None,
                created_at_us=0,
                updated_at_us=0,
                archived_at_us=None,
            )
            with connection.cursor() as cursor:
                cursor.execute("SELECT id, typeof(id) FROM conversation;")
                row = cursor.fetchone()
            print(json.dumps(row))
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            ["abcdefabcdefabcdefabcdefabcdefab", "text"],
        )

    def test_d16_explicit_null_primary_key_physical_results(self):
        result = _run_disposable(
            """\
            advance("B1")
            from django.db import IntegrityError
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO auth_user "
                    "(id, password, last_login, is_superuser, username, "
                    "first_name, last_name, email, is_staff, is_active, date_joined) "
                    "VALUES (1, '', NULL, 0, 'owner', '', '', '', 0, 1, '2026-01-01');"
                )
                cursor.execute(
                    "INSERT INTO role (slug, display_name, active) "
                    "VALUES ('max-smart', 'Max Smart', 1);"
                )
                cursor.execute(
                    "INSERT INTO authorized_identity "
                    "(id, user_id, tailscale_login, enabled, created_at_us, "
                    "last_seen_at_us) VALUES "
                    "(NULL, 1, 'owner@example.test', 1, 0, NULL);"
                )
                ai_id = cursor.lastrowid
                cursor.execute(
                    "INSERT INTO conversation "
                    "(id, owner_id, role_slug, version, next_sequence, title, "
                    "created_at_us, updated_at_us, archived_at_us) VALUES "
                    "('1' || printf('%031d', 1), 1, 'max-smart', 0, 1, NULL, "
                    "0, 0, NULL);"
                )
                conversation_id = cursor.execute(
                    "SELECT id FROM conversation;"
                ).fetchone()[0]
                operation_id = "2" * 32
                cursor.execute(
                    "INSERT INTO operation "
                    "(id, conversation_id, owner_id, operation_type, provider_id, "
                    "model_id, status, blocks_conversation, expected_version, "
                    "request_content, fingerprint_version, request_hash, retry_of, "
                    "runner_id, reservation_expires_at_us, provider_started_at_us, "
                    "execution_deadline_us, lease_expires_at_us, created_at_us, "
                    "finished_at_us, acknowledged_at_us, failure_category) VALUES "
                    "(%s, %s, 1, 'message', 'openai', 'gpt-5.6-sol', 'CANCELLED', "
                    "0, 0, 'request', 1, %s, NULL, NULL, NULL, NULL, NULL, NULL, "
                    "0, 1, NULL, NULL);",
                    (operation_id, conversation_id, "a" * 64),
                )
                cursor.execute(
                    "INSERT INTO idempotency_key "
                    "(id, owner_id, conversation_id, operation_type, "
                    "expected_version, key_digest_version, key_digest, "
                    "fingerprint_version, request_hash, operation_id, expires_at_us) "
                    "VALUES (NULL, 1, %s, 'message', 0, 1, %s, 1, %s, %s, 0);",
                    (conversation_id, "b" * 64, "a" * 64, operation_id),
                )
                idem_id = cursor.lastrowid
                cursor.execute(
                    "INSERT INTO global_model_slot "
                    "(id, operation_id, runner_id, reservation_expires_at_us, "
                    "lease_expires_at_us, acquired_at_us) "
                    "VALUES (NULL, NULL, NULL, NULL, NULL, NULL);"
                )
                slot_id = cursor.lastrowid

            null_failures = {}
            for table, statement in {
                "conversation": (
                    "INSERT INTO conversation "
                    "(id, owner_id, role_slug, version, next_sequence, title, "
                    "created_at_us, updated_at_us, archived_at_us) "
                    "VALUES (NULL, 1, 'max-smart', 0, 1, NULL, 0, 0, NULL);"
                ),
                "operation": (
                    "INSERT INTO operation "
                    "(id, conversation_id, owner_id, operation_type, provider_id, "
                    "model_id, status, blocks_conversation, expected_version, "
                    "request_content, fingerprint_version, request_hash, retry_of, "
                    "runner_id, reservation_expires_at_us, provider_started_at_us, "
                    "execution_deadline_us, lease_expires_at_us, created_at_us, "
                    "finished_at_us, acknowledged_at_us, failure_category) VALUES "
                    "(NULL, '" + conversation_id + "', 1, 'message', 'openai', "
                    "'gpt-5.6-sol', 'CANCELLED', 0, 0, 'request', 1, '" +
                    ("c" * 64) + "', NULL, NULL, NULL, NULL, NULL, NULL, "
                    "0, 1, NULL, NULL);"
                ),
                "message": (
                    "INSERT INTO message "
                    "(id, conversation_id, sequence, role, content, operation_id, "
                    "created_at_us) VALUES (NULL, '" + conversation_id +
                    "', 1, 'user', 'content', NULL, 0);"
                ),
            }.items():
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(statement)
                except IntegrityError:
                    null_failures[table] = True
                else:
                    null_failures[table] = False
            print(json.dumps([
                ai_id,
                idem_id,
                slot_id,
                null_failures,
            ]))
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [
                1,
                1,
                1,
                {
                    "conversation": True,
                    "operation": True,
                    "message": True,
                },
            ],
        )
