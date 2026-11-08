"""Disposable startup verification for Package D."""

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
from max_smart_db.maintenance import _perform_one_step, classify_boundary
from max_smart_db.startup import (
    StartupIntegrityError,
    verify_fresh_bootstrap,
    verify_ordinary_startup,
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
        database = Path(temporary) / "startup.sqlite3"
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


_BASE_RELATIONAL_ROWS = """\
with connection.cursor() as cursor:
    for user_id, username in ((1, 'owner'), (2, 'other')):
        cursor.execute(
            "INSERT INTO auth_user "
            "(id, password, last_login, is_superuser, username, "
            "first_name, last_name, email, is_staff, is_active, date_joined) "
            "VALUES (%s, '', NULL, 0, %s, '', '', '', 0, 1, '2026-01-01');",
            (user_id, username),
        )
    for conversation_id, owner_id in (('1' * 32, 1), ('9' * 32, 2)):
        cursor.execute(
            "INSERT INTO conversation "
            "(id, owner_id, role_slug, version, next_sequence, title, "
            "created_at_us, updated_at_us, archived_at_us) "
            "VALUES (%s, %s, 'max-smart', 0, 1, NULL, 0, 0, NULL);",
            (conversation_id, owner_id),
        )

def insert_cancelled(identifier, conversation, owner=1, retry_of=None):
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO operation "
            "(id, conversation_id, owner_id, operation_type, provider_id, "
            "model_id, status, blocks_conversation, expected_version, "
            "request_content, fingerprint_version, request_hash, retry_of, "
            "runner_id, reservation_expires_at_us, provider_started_at_us, "
            "execution_deadline_us, lease_expires_at_us, created_at_us, "
            "finished_at_us, acknowledged_at_us, failure_category) "
            "VALUES (%s, %s, %s, 'message', 'openai', 'gpt-5.6-sol', "
            "'CANCELLED', 0, 0, 'request', 1, %s, %s, NULL, NULL, NULL, "
            "NULL, NULL, 0, 1, NULL, NULL);",
            (identifier, conversation, owner, 'a' * 64, retry_of),
        )
"""


class StartupVerificationTests(unittest.TestCase):
    def test_d10_fresh_bootstrap_requires_free_seeds_and_runs_no_scans(self):
        result = _run_disposable(
            """\
            trace = []
            connection.connection.set_trace_callback(trace.append)
            verification = verify_fresh_bootstrap(connection, inventory)
            relational_terms = (
                "FROM operation AS operation_row",
                "FROM idempotency_key AS idempotency_row",
                "FROM global_model_slot AS slot",
            )
            print(json.dumps([
                verification.boundary,
                verification.scans,
                any(
                    any(term in statement for term in relational_terms)
                    for statement in trace
                ),
            ]))
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["B3", [], False])

    def test_d11_ordinary_startup_allows_matching_occupied_slot_and_runs_scans(self):
        result = _run_disposable(
            _BASE_RELATIONAL_ROWS
            + textwrap.dedent(
                """\
            runner = "A" * 22
            operation_id = "2" * 32
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO operation "
                    "(id, conversation_id, owner_id, operation_type, provider_id, "
                    "model_id, status, blocks_conversation, expected_version, "
                    "request_content, fingerprint_version, request_hash, retry_of, "
                    "runner_id, reservation_expires_at_us, provider_started_at_us, "
                    "execution_deadline_us, lease_expires_at_us, created_at_us, "
                    "finished_at_us, acknowledged_at_us, failure_category) "
                    "VALUES (%s, %s, 1, 'message', 'openai', 'gpt-5.6-sol', "
                    "'QUEUED', 1, 0, 'request', 1, %s, NULL, %s, 10, NULL, "
                    "NULL, NULL, 0, NULL, NULL, NULL);",
                    (operation_id, '1' * 32, 'a' * 64, runner),
                )
                cursor.execute(
                    "UPDATE global_model_slot SET operation_id = %s, "
                    "runner_id = %s, reservation_expires_at_us = 10, "
                    "lease_expires_at_us = NULL, acquired_at_us = 0 WHERE id = 1;",
                    (operation_id, runner),
                )
            ordinary = verify_ordinary_startup(connection, inventory)
            try:
                verify_fresh_bootstrap(connection, inventory)
            except StartupIntegrityError as error:
                fresh_code = error.code
            else:
                fresh_code = None
            print(json.dumps([
                ordinary.boundary,
                ordinary.scans,
                fresh_code,
            ]))
            """
            )
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        boundary, scans, fresh_code = json.loads(result.stdout)
        self.assertEqual(boundary, "B3")
        self.assertEqual(
            scans,
            [
                "slot_correspondence",
                "operation_owner",
                "idempotency_binding",
                "message_status",
                "retry_eligibility",
            ],
        )
        self.assertEqual(fresh_code, "slot_seed_invalid")

    def test_d11_each_relational_scan_fails_closed(self):
        corruptions = {
            "slot_correspondence_failed": """\
                runner = "A" * 22
                with connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO operation "
                        "(id, conversation_id, owner_id, operation_type, "
                        "provider_id, model_id, status, blocks_conversation, "
                        "expected_version, request_content, fingerprint_version, "
                        "request_hash, retry_of, runner_id, "
                        "reservation_expires_at_us, provider_started_at_us, "
                        "execution_deadline_us, lease_expires_at_us, created_at_us, "
                        "finished_at_us, acknowledged_at_us, failure_category) "
                        "VALUES (%s, %s, 1, 'message', 'openai', 'gpt-5.6-sol', "
                        "'QUEUED', 1, 0, 'request', 1, %s, NULL, %s, 10, NULL, "
                        "NULL, NULL, 0, NULL, NULL, NULL);",
                        ('2' * 32, '1' * 32, 'a' * 64, runner),
                    )
            """,
            "operation_owner_failed": """\
                with connection.cursor() as cursor:
                    cursor.execute("DROP TRIGGER msv1_op_bi_owner_match_trg;")
                insert_cancelled('3' * 32, '1' * 32, owner=2)
                with connection.cursor() as cursor:
                    cursor.execute(
                        inventory.object_sql["msv1_op_bi_owner_match_trg"]
                    )
            """,
            "idempotency_binding_failed": """\
                insert_cancelled('4' * 32, '1' * 32)
                with connection.cursor() as cursor:
                    cursor.execute("DROP TRIGGER msv1_idem_bi_binding_trg;")
                    cursor.execute(
                        "INSERT INTO idempotency_key "
                        "(id, owner_id, conversation_id, operation_type, "
                        "expected_version, key_digest_version, key_digest, "
                        "fingerprint_version, request_hash, operation_id, "
                        "expires_at_us) VALUES "
                        "(1, 2, %s, 'message', 0, 1, %s, 1, %s, %s, 0);",
                        ('1' * 32, 'b' * 64, 'a' * 64, '4' * 32),
                    )
                    cursor.execute(
                        inventory.object_sql["msv1_idem_bi_binding_trg"]
                    )
            """,
            "message_status_failed": """\
                insert_cancelled('5' * 32, '1' * 32)
                with connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO message "
                        "(id, conversation_id, sequence, role, content, "
                        "operation_id, created_at_us) VALUES "
                        "(%s, %s, 1, 'user', 'content', %s, 0);",
                        ('6' * 32, '1' * 32, '5' * 32),
                    )
            """,
            "retry_eligibility_failed": """\
                source = '7' * 32
                with connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO operation "
                        "(id, conversation_id, owner_id, operation_type, "
                        "provider_id, model_id, status, blocks_conversation, "
                        "expected_version, request_content, fingerprint_version, "
                        "request_hash, retry_of, runner_id, "
                        "reservation_expires_at_us, provider_started_at_us, "
                        "execution_deadline_us, lease_expires_at_us, created_at_us, "
                        "finished_at_us, acknowledged_at_us, failure_category) "
                        "VALUES (%s, %s, 1, 'message', 'openai', 'gpt-5.6-sol', "
                        "'SUCCEEDED', 0, 0, 'request', 1, %s, NULL, NULL, NULL, "
                        "1, 2, 3, 0, 4, NULL, NULL);",
                        (source, '1' * 32, 'a' * 64),
                    )
                    for message_id, sequence, role in (
                        ('8' * 32, 1, 'assistant'),
                        ('a' * 32, 2, 'user'),
                    ):
                        cursor.execute(
                            "INSERT INTO message "
                            "(id, conversation_id, sequence, role, content, "
                            "operation_id, created_at_us) VALUES "
                            "(%s, %s, %s, %s, 'content', %s, 0);",
                            (message_id, '1' * 32, sequence, role, source),
                        )
                    cursor.execute("DROP TRIGGER msv1_op_bi_retry_valid_trg;")
                insert_cancelled('b' * 32, '1' * 32, retry_of=source)
                with connection.cursor() as cursor:
                    cursor.execute(
                        inventory.object_sql["msv1_op_bi_retry_valid_trg"]
                    )
            """,
        }
        for expected_code, corruption in corruptions.items():
            with self.subTest(scan=expected_code):
                result = _run_disposable(
                    _BASE_RELATIONAL_ROWS
                    + textwrap.dedent(corruption)
                    + textwrap.dedent(
                        """\
                    try:
                        verify_ordinary_startup(connection, inventory)
                    except StartupIntegrityError as error:
                        code = error.code
                        message = str(error)
                    else:
                        code = None
                        message = None
                    print(json.dumps([
                        code,
                        message,
                        classify_boundary(connection, inventory).name,
                    ]))
                    """
                    )
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                code, message, boundary = json.loads(result.stdout)
                self.assertEqual(code, expected_code)
                self.assertEqual(
                    message,
                    "A startup relational integrity requirement failed.",
                )
                self.assertEqual(boundary, "B3")
