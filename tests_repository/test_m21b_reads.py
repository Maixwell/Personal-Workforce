"""Disposable-database tests for owner-scoped repository reads."""

import ast
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from max_smart_db.repository import (
    MessageRole,
    OperationStatus,
    StoredFailureCategory,
)


SRC_ROOT = PROJECT_ROOT / "src"
PRODUCTION_DATABASE = PROJECT_ROOT / "data" / "max_smart.sqlite3"

_PREAMBLE = """\
import json
import os
import sqlite3
import uuid
from pathlib import Path

database = Path(os.environ["MAX_SMART_TEST_DATABASE_PATH"])
from max_smart_db.safety import initialize_sqlite_file
initialize_sqlite_file(database)

import django
django.setup()
from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from max_smart_db.models import Conversation, Message
from max_smart_db.reads import ReadInputError, SqliteConversationReader
from max_smart_db.repository import (
    MessageRole,
    NotFound,
    OperationStatus,
    Snapshot,
    StoredFailureCategory,
)

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)
connection.close()


class UnusedClock:
    def now_us(self):
        raise AssertionError("read path used the clock")


reader = SqliteConversationReader(UnusedClock(), "reader-under-test")
"""


def _run_disposable(body, *, initialize=True, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        database = Path(temporary) / "r3a.sqlite3"
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(database),
            "PYTHONPATH": str(SRC_ROOT),
        }
        source = textwrap.dedent(body)
        if initialize:
            source = _PREAMBLE + "\n" + source
        return subprocess.run(
            (sys.executable, "-B", "-c", source),
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )


def _require_ok(testcase, result):
    testcase.assertEqual(
        result.returncode,
        0,
        result.stdout + "\n" + result.stderr,
    )
    return json.loads(result.stdout)


class RepositoryReadTests(unittest.TestCase):
    def test_reads_are_owner_scoped_and_not_found_is_identical(self):
        result = _run_disposable(
            """\
            owner = User.objects.create(username="owner")
            other = User.objects.create(username="other")
            conversation_id = uuid.UUID("00112233445566778899aabbccddeeff")
            operation_id = uuid.UUID("abcdefabcdefabcdefabcdefabcdefab")
            Conversation.objects.create(
                id=conversation_id,
                owner_id=owner.id,
                role_id="max-smart",
                version=3,
                next_sequence=4,
                title="Visible",
                created_at_us=10,
                updated_at_us=11,
            )
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
                    "'FAILED', 0, 2, 'request', 1, %s, NULL, NULL, NULL, 50, "
                    "60, 70, 12, 13, NULL, 'provider_definite_failure');",
                    (
                        operation_id.hex,
                        conversation_id.hex,
                        owner.id,
                        "a" * 64,
                    ),
                )
            Message.objects.create(
                id=uuid.UUID("12345678123456781234567812345678"),
                conversation_id=conversation_id,
                sequence=2,
                role="assistant",
                content="second",
                operation_id=operation_id,
                created_at_us=2,
            )
            Message.objects.create(
                id=uuid.UUID("22345678123456781234567812345678"),
                conversation_id=conversation_id,
                sequence=1,
                role="user",
                content="first",
                operation_id=None,
                created_at_us=1,
            )
            owned = reader.get_conversation(owner.id, conversation_id)
            missing = reader.get_conversation(owner.id, uuid.uuid4())
            cross = reader.get_conversation(other.id, conversation_id)
            operation = reader.get_operation(owner.id, operation_id)
            missing_operation = reader.get_operation(owner.id, uuid.uuid4())
            cross_operation = reader.get_operation(other.id, operation_id)
            print(json.dumps({
                "conversation": [
                    str(owned.conversation_id),
                    owned.actor_id,
                    owned.role_slug,
                    owned.version,
                    owned.next_sequence,
                    owned.title,
                ],
                "not_found_equal": missing == cross == missing_operation == cross_operation,
                "not_found_type": [
                    type(missing) is NotFound,
                    type(cross) is NotFound,
                    type(missing_operation) is NotFound,
                    type(cross_operation) is NotFound,
                ],
                "operation": [
                    str(operation.operation_id),
                    operation.status.value,
                    operation.provider_started_at,
                    operation.failure_category.value,
                    operation.blocks_conversation,
                    operation.acknowledged,
                    operation.expected_version,
                ],
            }))
            """
        )
        payload = _require_ok(self, result)
        self.assertEqual(
            payload["conversation"],
            [
                "00112233-4455-6677-8899-aabbccddeeff",
                payload["conversation"][1],
                "max-smart",
                3,
                4,
                "Visible",
            ],
        )
        self.assertTrue(payload["not_found_equal"])
        self.assertEqual(payload["not_found_type"], [True, True, True, True])
        self.assertEqual(
            payload["operation"],
            [
                "abcdefab-cdef-abcd-efab-cdefabcdefab",
                OperationStatus.FAILED.value,
                50,
                StoredFailureCategory.PROVIDER_DEFINITE_FAILURE.value,
                False,
                False,
                2,
            ],
        )

    def test_messages_use_cursor_order_and_snapshot_isolation(self):
        result = _run_disposable(
            """\
            owner = User.objects.create(username="owner")
            other = User.objects.create(username="other")
            conversation_id = uuid.UUID("00112233445566778899aabbccddeeff")
            Conversation.objects.create(
                id=conversation_id,
                owner_id=owner.id,
                role_id="max-smart",
                version=0,
                next_sequence=3,
                title=None,
                created_at_us=0,
                updated_at_us=0,
            )
            for sequence, content in ((2, "b"), (1, "a")):
                Message.objects.create(
                    id=uuid.uuid4(),
                    conversation_id=conversation_id,
                    sequence=sequence,
                    role="user",
                    content=content,
                    operation_id=None,
                    created_at_us=sequence,
                )
            empty_id = uuid.uuid4()
            Conversation.objects.create(
                id=empty_id,
                owner_id=owner.id,
                role_id="max-smart",
                version=0,
                next_sequence=1,
                title=None,
                created_at_us=0,
                updated_at_us=0,
            )
            listed = reader.list_messages(owner.id, conversation_id)
            after_one = reader.list_messages(owner.id, conversation_id, 1)
            after_two = reader.list_messages(owner.id, conversation_id, 2)
            after_zero = reader.list_messages(owner.id, conversation_id, 0)
            empty = reader.list_messages(owner.id, empty_id)
            cross = reader.list_messages(other.id, conversation_id)
            trace = []
            connection.connection.set_trace_callback(trace.append)
            original_filter = Message.objects.filter

            def filter_after_competing_commit(*args, **kwargs):
                if not filter_after_competing_commit.done:
                    filter_after_competing_commit.done = True
                    other_connection = sqlite3.connect(
                        f"{database.as_uri()}?mode=rw",
                        uri=True,
                    )
                    try:
                        other_connection.execute("BEGIN IMMEDIATE")
                        other_connection.execute(
                            "INSERT INTO message "
                            "(id, conversation_id, sequence, role, content, "
                            "operation_id, created_at_us) "
                            "VALUES (?, ?, 99, 'assistant', 'late', NULL, 99)",
                            (
                                uuid.uuid4().hex,
                                conversation_id.hex,
                            ),
                        )
                        other_connection.commit()
                    finally:
                        other_connection.close()
                return original_filter(*args, **kwargs)

            filter_after_competing_commit.done = False
            Message.objects.filter = filter_after_competing_commit
            try:
                snapshot = reader.load_snapshot(owner.id, conversation_id)
            finally:
                Message.objects.filter = original_filter
                connection.connection.set_trace_callback(None)
            connection.close()
            connection.connect()
            visible_later = reader.list_messages(owner.id, conversation_id)
            joined = "\\n".join(trace)
            print(json.dumps({
                "listed": [(item.sequence, item.content, item.role.value) for item in listed],
                "after_one": [item.sequence for item in after_one],
                "after_two": [item.sequence for item in after_two],
                "after_zero": [item.sequence for item in after_zero],
                "empty": list(empty),
                "cross_not_found": type(cross) is NotFound,
                "snapshot_type": type(snapshot) is Snapshot,
                "snapshot": [
                    (item.sequence, item.content, item.role.value)
                    for item in snapshot.messages
                ],
                "snapshot_version": snapshot.conversation.version,
                "later": [item.sequence for item in visible_later],
                "began_deferred": "BEGIN DEFERRED" in joined,
                "began_immediate": "BEGIN IMMEDIATE" in joined,
                "rolled_back": "ROLLBACK" in joined,
                "reader_wrote": any(
                    statement.startswith(("INSERT", "UPDATE", "DELETE"))
                    for statement in trace
                ),
            }))
            """
        )
        payload = _require_ok(self, result)
        self.assertEqual(
            payload["listed"],
            [[1, "a", MessageRole.USER.value], [2, "b", MessageRole.USER.value]],
        )
        self.assertEqual(payload["after_one"], [2])
        self.assertEqual(payload["after_two"], [])
        self.assertEqual(payload["after_zero"], [1, 2])
        self.assertEqual(payload["empty"], [])
        self.assertTrue(payload["cross_not_found"])
        self.assertTrue(payload["snapshot_type"])
        self.assertEqual(
            payload["snapshot"],
            [[1, "a", "user"], [2, "b", "user"]],
        )
        self.assertEqual(payload["snapshot_version"], 0)
        self.assertEqual(payload["later"], [1, 2, 99])
        self.assertTrue(payload["began_deferred"])
        self.assertFalse(payload["began_immediate"])
        self.assertTrue(payload["rolled_back"])
        self.assertFalse(payload["reader_wrote"])

    def test_invalid_cursors_and_identifiers_are_rejected_before_lookup(self):
        result = _run_disposable(
            """\
            class IntSubclass(int):
                pass

            owner = User.objects.create(username="owner")
            conversation_id = uuid.uuid4()
            Conversation.objects.create(
                id=conversation_id,
                owner_id=owner.id,
                role_id="max-smart",
                version=0,
                next_sequence=1,
                title=None,
                created_at_us=0,
                updated_at_us=0,
            )
            rejected = []
            for value in (True, False, IntSubclass(0), -1, 1.0, "0"):
                try:
                    reader.list_messages(owner.id, conversation_id, value)
                except ReadInputError as error:
                    rejected.append(error.code)
                else:
                    rejected.append("accepted")
            identifier_codes = []
            for actor, identifier in (
                (True, conversation_id),
                (IntSubclass(owner.id), conversation_id),
                (owner.id, conversation_id.hex),
            ):
                try:
                    reader.get_conversation(actor, identifier)
                except ReadInputError as error:
                    identifier_codes.append(error.code)
                else:
                    identifier_codes.append("accepted")
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM message;")
                before = cursor.fetchone()[0]
            try:
                with connection.cursor() as cursor:
                    cursor.execute("BEGIN IMMEDIATE")
                from django.db import transaction
                connection.in_atomic_block = True
                try:
                    reader.load_snapshot(owner.id, conversation_id)
                except ReadInputError as error:
                    nested = error.code
                else:
                    nested = "accepted"
            finally:
                connection.in_atomic_block = False
                with connection.cursor() as cursor:
                    cursor.execute("ROLLBACK")
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM message;")
                after = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM django_migrations;")
                migrations = cursor.fetchone()[0]
            print(json.dumps({
                "rejected": rejected,
                "identifiers": identifier_codes,
                "nested": nested,
                "unchanged": [before, after, migrations],
            }))
            """
        )
        payload = _require_ok(self, result)
        self.assertEqual(
            payload["rejected"],
            [
                "cursor_type",
                "cursor_type",
                "cursor_type",
                "cursor_range",
                "cursor_type",
                "cursor_type",
            ],
        )
        self.assertEqual(
            payload["identifiers"],
            ["actor_type", "actor_type", "identifier_type"],
        )
        self.assertEqual(payload["nested"], "read_inside_write_transaction")
        self.assertEqual(payload["unchanged"][0], payload["unchanged"][1])
        self.assertGreater(payload["unchanged"][2], 0)

    def test_missing_database_is_not_created(self):
        result = _run_disposable(
            """\
            import uuid
            from pathlib import Path
            import os
            database = Path(os.environ["MAX_SMART_TEST_DATABASE_PATH"])
            import django
            django.setup()
            from max_smart_db.reads import SqliteConversationReader
            from max_smart_db.safety import DatabaseSafetyError

            class UnusedClock:
                def now_us(self):
                    raise AssertionError("clock")

            reader = SqliteConversationReader(UnusedClock(), "reader")
            try:
                reader.get_conversation(1, uuid.uuid4())
            except DatabaseSafetyError as error:
                code = error.code
            else:
                code = "opened"
            print(code)
            print(database.exists())
            """,
            initialize=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
        code, exists = result.stdout.splitlines()
        self.assertEqual(code, "database_missing")
        self.assertEqual(exists, "False")

    def test_reader_source_has_no_write_or_migration_operations(self):
        source = (SRC_ROOT / "max_smart_db" / "reads.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertTrue(called.isdisjoint({
            "save",
            "create",
            "update",
            "delete",
            "bulk_create",
            "migrate",
            "initialize_sqlite_file",
        }))
        self.assertNotIn("BEGIN IMMEDIATE", source)
        self.assertNotIn("now_us", source)


if __name__ == "__main__":
    unittest.main()
