"""Disposable-database tests for conversation creation."""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import uuid
from pathlib import Path
from typing import get_args, get_type_hints


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))
PRODUCTION_DATABASE = PROJECT_ROOT / "data" / "max_smart.sqlite3"

from max_smart_db.repository import (  # noqa: E402
    CommitAmbiguous,
    ConversationConflict,
    ConversationRepository,
    ConversationView,
    DatabaseBusy,
)

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
from django.db import OperationalError, connection
from django.db.migrations.executor import MigrationExecutor
from max_smart_db.application_validation import ApplicationValidationError
from max_smart_db.creation import (
    CreationInputError,
    SqliteConversationRepository,
    _insert_conversation,
)
from max_smart_db.models import Role
from max_smart_db.repository import (
    CommitAmbiguous,
    ConversationConflict,
    ConversationView,
    DatabaseBusy,
    NotFound,
)

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)
connection.close()


class Clock:
    def __init__(self):
        self.calls = 0
        self.value = 123456

    def now_us(self):
        self.calls += 1
        return self.value


clock = Clock()
repository = SqliteConversationRepository(clock, "creator-under-test")
"""


def _run_disposable(body, *, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        database_path = Path(temporary) / "r3b.sqlite3"
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(database_path),
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
            timeout=timeout,
        )


def _payload(testcase, result):
    import json
    testcase.assertEqual(
        result.returncode,
        0,
        result.stdout + "\n" + result.stderr,
    )
    return json.loads(result.stdout)


class ConversationCreationTests(unittest.TestCase):
    def test_create_result_can_represent_conflict_and_reconciliation_id(self):
        returned = get_type_hints(
            ConversationRepository.create_conversation,
        )["return"]
        self.assertEqual(
            get_args(returned),
            (
                ConversationView,
                ConversationConflict,
                DatabaseBusy,
                CommitAmbiguous,
            ),
        )
        self.assertEqual(list(ConversationConflict.__dataclass_fields__), [])
        identifier = uuid.UUID("00112233445566778899aabbccddeeff")
        ambiguous = CommitAmbiguous(conversation_id=identifier)
        self.assertEqual(ambiguous.conversation_id, identifier)
        self.assertIsNone(ambiguous.operation_id)
        self.assertIsNone(CommitAmbiguous().conversation_id)

    def test_successful_creation_and_rejected_inputs(self):
        result = _run_disposable(
            """\
            import max_smart_db.creation as creation
            owner = User.objects.create(username="owner")
            Role.objects.create(slug="retired", display_name="Retired", active=0)
            fixed_id = uuid.UUID("00112233445566778899aabbccddeeff")
            original_uuid4 = uuid.uuid4
            uuid.uuid4 = lambda: fixed_id
            try:
                created = repository.create_conversation(owner.id, "max-smart", " Hello ")
                success_calls = clock.calls
                conflict = repository.create_conversation(owner.id, "max-smart", "other")
            finally:
                uuid.uuid4 = original_uuid4
            connection.close()
            reread = repository.get_conversation(owner.id, fixed_id)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT created_at_us, updated_at_us FROM conversation WHERE id = %s;",
                    (fixed_id.hex,),
                )
                stored_times = list(cursor.fetchone())
            before_invalid = connection.cursor()
            before_invalid.execute("SELECT COUNT(*) FROM conversation;")
            before_count = before_invalid.fetchone()[0]
            before_invalid.close()
            clock.calls = 0
            outcomes = {}
            for label, actor, role in (
                ("missing_owner", 999999, "max-smart"),
                ("missing_role", owner.id, "missing-role"),
                ("inactive_role", owner.id, "retired"),
            ):
                try:
                    repository.create_conversation(actor, role, None)
                except CreationInputError as error:
                    outcomes[label] = error.code
                else:
                    outcomes[label] = "accepted"
            invalid_calls = clock.calls
            type_codes = []
            trace = []
            connection.connection.set_trace_callback(trace.append)
            try:
                repository.create_conversation(True, "max-smart", None)
            except ApplicationValidationError as error:
                type_codes.append(error.code)
            else:
                type_codes.append("accepted")
            try:
                repository.create_conversation(owner.id, "max-smart", 1)
            except CreationInputError as error:
                type_codes.append(error.code)
            else:
                type_codes.append("accepted")
            began = any("BEGIN" in statement for statement in trace)
            real_insert = _insert_conversation

            def fail_after_insert(*args, **kwargs):
                real_insert(*args, **kwargs)
                raise RuntimeError("injected rollback")

            creation._insert_conversation = fail_after_insert
            uuid.uuid4 = lambda: uuid.UUID("abcdefabcdefabcdefabcdefabcdefab")
            try:
                try:
                    repository.create_conversation(owner.id, "max-smart", "rolled")
                except RuntimeError as error:
                    rolled = str(error)
                else:
                    rolled = "accepted"
            finally:
                creation._insert_conversation = real_insert
                uuid.uuid4 = original_uuid4
            connection.close()
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM conversation;")
                after_count = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT COUNT(*) FROM conversation WHERE id = %s;",
                    ("abcdefabcdefabcdefabcdefabcdefab",),
                )
                rolled_count = cursor.fetchone()[0]
            print(json.dumps({
                "created": [
                    str(created.conversation_id),
                    created.actor_id,
                    created.role_slug,
                    created.version,
                    created.next_sequence,
                    created.title,
                ],
                "stored": [
                    reread.version,
                    reread.next_sequence,
                    reread.title,
                    reread.role_slug,
                ],
                "calls_after_success": success_calls,
                "stored_times": stored_times,
                "conflict": type(conflict) is ConversationConflict,
                "conflict_fields": list(getattr(conflict, "__dataclass_fields__", {})),
                "invalid": outcomes,
                "type_codes": type_codes,
                "type_began": began,
                "clock_after_invalid": invalid_calls,
                "counts": [before_count, after_count, rolled_count],
                "rolled": rolled,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(
            payload["created"],
            [
                "00112233-4455-6677-8899-aabbccddeeff",
                payload["created"][1],
                "max-smart",
                0,
                1,
                " Hello ",
            ],
        )
        self.assertEqual(payload["stored"], [0, 1, " Hello ", "max-smart"])
        self.assertEqual(payload["stored_times"], [123456, 123456])
        self.assertEqual(payload["calls_after_success"], 1)
        self.assertTrue(payload["conflict"])
        self.assertEqual(payload["conflict_fields"], [])
        self.assertEqual(
            payload["invalid"],
            {
                "missing_owner": "owner_invalid",
                "missing_role": "role_invalid",
                "inactive_role": "role_invalid",
            },
        )
        self.assertEqual(
            payload["type_codes"],
            ["positive_integer_id_type", "title_type"],
        )
        self.assertFalse(payload["type_began"])
        self.assertEqual(payload["clock_after_invalid"], 3)
        self.assertEqual(payload["counts"], [1, 1, 0])
        self.assertEqual(payload["rolled"], "injected rollback")

    def test_lock_failure_before_mutation_returns_database_busy(self):
        result = _run_disposable(
            """\
            owner = User.objects.create(username="owner")
            holder = sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True)
            try:
                holder.execute("BEGIN IMMEDIATE")
                clock.calls = 0
                outcome = repository.create_conversation(owner.id, "max-smart", None)
            finally:
                holder.rollback()
                holder.close()
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM conversation;")
                count = cursor.fetchone()[0]
            print(json.dumps({
                "busy": type(outcome) is DatabaseBusy,
                "calls": clock.calls,
                "count": count,
            }))
            """,
            timeout=30,
        )
        payload = _payload(self, result)
        self.assertTrue(payload["busy"])
        self.assertEqual(payload["calls"], 0)
        self.assertEqual(payload["count"], 0)

    def test_commit_exception_returns_the_allocated_conversation_id(self):
        result = _run_disposable(
            """\
            owner = User.objects.create(username="owner")
            fixed_id = uuid.UUID("abcdefabcdefabcdefabcdefabcdefab")
            original_uuid4 = uuid.uuid4
            uuid.uuid4 = lambda: fixed_id
            real_commit = connection.commit

            def fail_commit(*args, **kwargs):
                raise OperationalError("injected commit failure")

            connection.commit = fail_commit
            try:
                outcome = repository.create_conversation(
                    owner.id,
                    "max-smart",
                    "pending",
                )
            finally:
                connection.commit = real_commit
                uuid.uuid4 = original_uuid4
            connection.close()
            reread = repository.get_conversation(owner.id, fixed_id)
            print(json.dumps({
                "ambiguous": type(outcome) is CommitAmbiguous,
                "conversation_id": str(outcome.conversation_id),
                "operation_id": outcome.operation_id,
                "calls": clock.calls,
                "reread_not_found": type(reread) is NotFound,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertTrue(payload["ambiguous"])
        self.assertEqual(
            payload["conversation_id"],
            "abcdefab-cdef-abcd-efab-cdefabcdefab",
        )
        self.assertIsNone(payload["operation_id"])
        self.assertEqual(payload["calls"], 1)
        self.assertTrue(payload["reread_not_found"])


if __name__ == "__main__":
    unittest.main()
