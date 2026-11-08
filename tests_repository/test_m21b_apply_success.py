"""Disposable-database tests for atomic ordinary APPLY."""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"

_PREAMBLE = r"""
import importlib
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
from django.db.models.query import QuerySet
from max_smart_db.apply_success import ApplySuccessInputError
from max_smart_db.creation import SqliteConversationRepository
from max_smart_db.models import Conversation, GlobalModelSlot, Message, Operation
from max_smart_db.repository import ClaimedOperation

success_module = importlib.import_module("max_smart_db.apply_success")
MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

OWNER = User.objects.create(username="owner")
OTHER = User.objects.create(username="other")
CONVERSATION = uuid.UUID("00112233445566778899aabbccddeeff")
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
STALE = "BBBBBBBBBBBBBBBBBBBBBQ"
HASH = "ab" * 32
STARTED = 1_000
DEADLINE = STARTED + 600_000_000
LEASE = STARTED + 720_000_000

Conversation.objects.create(
    id=CONVERSATION,
    owner_id=OWNER.id,
    role_id="max-smart",
    version=4,
    next_sequence=5,
    title=None,
    created_at_us=0,
    updated_at_us=0,
)


class Clock:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def now_us(self):
        self.calls += 1
        return self.value


def running(identifier):
    Operation.objects.create(
        id=identifier,
        conversation_id=CONVERSATION,
        owner_id=OWNER.id,
        operation_type="message",
        provider_id="openai",
        model_id="gpt-5.6-sol",
        status="RUNNING",
        blocks_conversation=1,
        expected_version=4,
        request_content="  exact user content\r\n",
        fingerprint_version=1,
        request_hash=HASH,
        retry_of=None,
        runner_id=RUNNER,
        reservation_expires_at_us=None,
        provider_started_at_us=STARTED,
        execution_deadline_us=DEADLINE,
        lease_expires_at_us=LEASE,
        created_at_us=1,
        finished_at_us=None,
        acknowledged_at_us=None,
        failure_category=None,
    )
    GlobalModelSlot.objects.filter(pk=1).update(
        operation_id=identifier,
        runner_id=RUNNER,
        reservation_expires_at_us=None,
        lease_expires_at_us=LEASE,
        acquired_at_us=7,
    )


def claimed(identifier, *, actor=None, runner=RUNNER, request_hash=HASH):
    return ClaimedOperation(
        operation_id=identifier,
        actor_id=OWNER.id if actor is None else actor,
        conversation_id=CONVERSATION,
        request_hash=request_hash,
        fingerprint_version=1,
        expected_version=4,
        runner_id=runner,
        claimed_at=STARTED,
        execution_deadline=DEADLINE,
        lease_expires_at=LEASE,
    )


def state(identifier):
    operation = Operation.objects.get(pk=identifier)
    conversation = Conversation.objects.get(pk=CONVERSATION)
    slot = GlobalModelSlot.objects.get(pk=1)
    messages = list(
        Message.objects.filter(conversation_id=CONVERSATION)
        .order_by("sequence")
        .values_list(
            "sequence",
            "role",
            "content",
            "operation_id",
            "created_at_us",
        )
    )
    return {
        "status": operation.status,
        "blocks": operation.blocks_conversation,
        "runner": operation.runner_id,
        "started": operation.provider_started_at_us,
        "deadline": operation.execution_deadline_us,
        "lease": operation.lease_expires_at_us,
        "finished": operation.finished_at_us,
        "version": conversation.version,
        "next_sequence": conversation.next_sequence,
        "updated": conversation.updated_at_us,
        "archived": conversation.archived_at_us,
        "slot_operation": (
            None if slot.operation_id is None else str(slot.operation_id)
        ),
        "slot_runner": slot.runner_id,
        "slot_lease": slot.lease_expires_at_us,
        "messages": [
            [
                sequence,
                role,
                content,
                None if operation_id is None else str(operation_id),
                created,
            ]
            for sequence, role, content, operation_id, created in messages
        ],
    }
"""


def _run(body, *, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "r3c7.sqlite3"
            ),
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
    testcase.assertEqual(
        result.returncode,
        0,
        result.stdout + "\n" + result.stderr,
    )
    return __import__("json").loads(result.stdout)


class ApplySuccessTests(unittest.TestCase):
    def test_success_atomically_commits_adjacent_exact_messages(self):
        result = _run(
            """\
            identifier = uuid.UUID("11111111111111111111111111111111")
            running(identifier)
            clock = Clock(88_000)
            outcome = SqliteConversationRepository(
                clock,
                RUNNER,
            ).apply_success(claimed(identifier), "  exact assistant\\r\\n")
            print(json.dumps({
                "type": type(outcome).__name__,
                "view": [
                    outcome.operation.status.value,
                    outcome.operation.blocks_conversation,
                    outcome.operation.runner_id,
                ],
                "calls": clock.calls,
                "state": state(identifier),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "Applied")
        self.assertEqual(payload["view"], ["SUCCEEDED", False, None])
        self.assertEqual(payload["calls"], 1)
        current = payload["state"]
        self.assertEqual(current["version"], 5)
        self.assertEqual(current["next_sequence"], 7)
        self.assertEqual(current["updated"], 88_000)
        self.assertEqual(current["status"], "SUCCEEDED")
        self.assertEqual(current["finished"], 88_000)
        self.assertIsNone(current["runner"])
        self.assertEqual(current["started"], 1_000)
        self.assertEqual(current["deadline"], 600_001_000)
        self.assertEqual(current["lease"], 720_001_000)
        self.assertIsNone(current["slot_operation"])
        self.assertEqual(
            current["messages"],
            [
                [
                    5,
                    "user",
                    "  exact user content\r\n",
                    "11111111-1111-1111-1111-111111111111",
                    88_000,
                ],
                [
                    6,
                    "assistant",
                    "  exact assistant\r\n",
                    "11111111-1111-1111-1111-111111111111",
                    88_000,
                ],
            ],
        )

    def test_duplicate_returns_original_messages_without_mutation(self):
        result = _run(
            """\
            identifier = uuid.UUID("22222222222222222222222222222222")
            running(identifier)
            repository = SqliteConversationRepository(Clock(90), RUNNER)
            first = repository.apply_success(claimed(identifier), "original")
            before = state(identifier)
            real_update = QuerySet.update
            real_create = Message.objects.create
            calls = {"update": 0, "create": 0}
            def counting_update(self, **kwargs):
                calls["update"] += 1
                return real_update(self, **kwargs)
            def counting_create(*args, **kwargs):
                calls["create"] += 1
                return real_create(*args, **kwargs)
            QuerySet.update = counting_update
            Message.objects.create = counting_create
            try:
                duplicate = repository.apply_success(
                    claimed(identifier),
                    "different ignored output",
                )
            finally:
                QuerySet.update = real_update
                Message.objects.create = real_create
            print(json.dumps({
                "first": type(first).__name__,
                "duplicate": type(duplicate).__name__,
                "user": duplicate.user_message.content,
                "assistant": duplicate.assistant_message.content,
                "sequences": [
                    duplicate.user_message.sequence,
                    duplicate.assistant_message.sequence,
                ],
                "updates": calls["update"],
                "creates": calls["create"],
                "unchanged": state(identifier) == before,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["first"], "Applied")
        self.assertEqual(payload["duplicate"], "AlreadySucceeded")
        self.assertEqual(payload["user"], "  exact user content\r\n")
        self.assertEqual(payload["assistant"], "original")
        self.assertEqual(payload["sequences"], [5, 6])
        self.assertEqual(payload["updates"], 0)
        self.assertEqual(payload["creates"], 0)
        self.assertTrue(payload["unchanged"])

    def test_ownership_fingerprint_slot_version_archive_and_state_results(self):
        result = _run(
            """\
            identifier = uuid.UUID("33333333333333333333333333333333")
            running(identifier)
            repository = SqliteConversationRepository(Clock(10), RUNNER)
            missing = repository.apply_success(claimed(uuid.uuid4()), "reply")
            cross = repository.apply_success(
                claimed(identifier, actor=OTHER.id),
                "reply",
            )
            stale_runner = repository.apply_success(
                claimed(identifier, runner=STALE),
                "reply",
            )
            fingerprint = repository.apply_success(
                claimed(identifier, request_hash="cd" * 32),
                "reply",
            )
            unchanged = state(identifier)
            GlobalModelSlot.objects.filter(pk=1).update(
                operation_id=None,
                runner_id=None,
                lease_expires_at_us=None,
                acquired_at_us=None,
            )
            slot = repository.apply_success(claimed(identifier), "reply")
            GlobalModelSlot.objects.filter(pk=1).update(
                operation_id=identifier,
                runner_id=RUNNER,
                lease_expires_at_us=LEASE,
                acquired_at_us=7,
            )
            Conversation.objects.filter(pk=CONVERSATION).update(version=5)
            stale_version = repository.apply_success(claimed(identifier), "reply")
            Conversation.objects.filter(pk=CONVERSATION).update(
                version=4,
                archived_at_us=9,
            )
            archived = repository.apply_success(claimed(identifier), "reply")
            Conversation.objects.filter(pk=CONVERSATION).update(archived_at_us=None)
            Operation.objects.filter(pk=identifier).update(
                status="FAILED",
                blocks_conversation=0,
                runner_id=None,
                finished_at_us=11,
                failure_category="provider_definite_failure",
            )
            terminal = repository.apply_success(claimed(identifier), "reply")
            print(json.dumps({
                "missing": type(missing).__name__,
                "cross": type(cross).__name__,
                "equal": missing == cross,
                "runner": type(stale_runner).__name__,
                "fingerprint": type(fingerprint).__name__,
                "unchanged": state(identifier)["messages"] == unchanged["messages"],
                "slot": type(slot).__name__,
                "stale": [type(stale_version).__name__, stale_version.actual],
                "archived": [type(archived).__name__, archived.state.value],
                "terminal": [type(terminal).__name__, terminal.state.value],
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["missing"], "OwnershipLost")
        self.assertEqual(payload["cross"], "OwnershipLost")
        self.assertTrue(payload["equal"])
        self.assertEqual(payload["runner"], "OwnershipLost")
        self.assertEqual(payload["fingerprint"], "OwnershipLost")
        self.assertTrue(payload["unchanged"])
        self.assertEqual(payload["slot"], "SlotMismatch")
        self.assertEqual(payload["stale"], ["StaleVersion", 5])
        self.assertEqual(payload["archived"], ["InvalidState", "RUNNING"])
        self.assertEqual(payload["terminal"], ["InvalidState", "FAILED"])

    def test_invalid_output_is_rejected_before_any_apply_mutation(self):
        result = _run(
            """\
            identifier = uuid.UUID("44444444444444444444444444444444")
            running(identifier)
            clock = Clock(10)
            repository = SqliteConversationRepository(clock, RUNNER)
            rejected = []
            for output in (
                None,
                "",
                " \\t\\r\\n",
                "bad\\x00output",
                "\\ud800",
            ):
                try:
                    repository.apply_success(claimed(identifier), output)
                except ApplySuccessInputError as error:
                    rejected.append(error.code)
                else:
                    rejected.append("accepted")
            print(json.dumps({
                "rejected": rejected,
                "calls": clock.calls,
                "state": state(identifier),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(
            payload["rejected"],
            [
                "output_type",
                "output_blank",
                "output_blank",
                "output_nul",
                "output_encoding",
            ],
        )
        self.assertEqual(payload["calls"], 5)
        self.assertEqual(payload["state"]["status"], "RUNNING")
        self.assertEqual(payload["state"]["version"], 4)
        self.assertEqual(payload["state"]["messages"], [])

    def test_recovery_first_returns_recovered_unknown_without_overwrite(self):
        result = _run(
            """\
            identifier = uuid.UUID("55555555555555555555555555555555")
            running(identifier)
            Operation.objects.filter(pk=identifier).update(
                status="OUTCOME_UNKNOWN",
                finished_at_us=LEASE,
            )
            GlobalModelSlot.objects.filter(pk=1).update(
                operation_id=None,
                runner_id=None,
                lease_expires_at_us=None,
                acquired_at_us=None,
            )
            before = state(identifier)
            outcome = SqliteConversationRepository(
                Clock(LEASE + 1),
                RUNNER,
            ).apply_success(claimed(identifier), "retained output")
            print(json.dumps({
                "type": type(outcome).__name__,
                "reconcile": outcome.reconcile_required,
                "unchanged": state(identifier) == before,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "RecoveredUnknown")
        self.assertTrue(payload["reconcile"])
        self.assertTrue(payload["unchanged"])

    def test_conditional_zero_and_each_partial_failure_roll_back_everything(self):
        result = _run(
            """\
            identifier = uuid.UUID("66666666666666666666666666666666")
            running(identifier)
            repository = SqliteConversationRepository(Clock(10), RUNNER)
            supplied = claimed(identifier)
            baseline = state(identifier)
            real_update = QuerySet.update
            def zero_conversation(self, **kwargs):
                if self.model is Conversation and "next_sequence" in kwargs:
                    return 0
                return real_update(self, **kwargs)
            QuerySet.update = zero_conversation
            try:
                zero = repository.apply_success(supplied, "reply")
            finally:
                QuerySet.update = real_update
            zero_unchanged = state(identifier) == baseline

            failures = {}
            real_insert = success_module._insert_message
            real_release = success_module._release_slot

            def fail_first(*args, **kwargs):
                raise RuntimeError("after conversation update")
            success_module._insert_message = fail_first
            try:
                repository.apply_success(supplied, "reply")
            except RuntimeError as error:
                failures["conversation"] = [str(error), state(identifier) == baseline]
            finally:
                success_module._insert_message = real_insert

            calls = {"value": 0}
            def fail_second(*args, **kwargs):
                calls["value"] += 1
                if calls["value"] == 2:
                    raise RuntimeError("after first message")
                return real_insert(*args, **kwargs)
            success_module._insert_message = fail_second
            try:
                repository.apply_success(supplied, "reply")
            except RuntimeError as error:
                failures["first"] = [str(error), state(identifier) == baseline]
            finally:
                success_module._insert_message = real_insert

            calls["value"] = 0
            def fail_after_second(*args, **kwargs):
                calls["value"] += 1
                result = real_insert(*args, **kwargs)
                if calls["value"] == 2:
                    raise RuntimeError("after second message")
                return result
            success_module._insert_message = fail_after_second
            try:
                repository.apply_success(supplied, "reply")
            except RuntimeError as error:
                failures["second"] = [str(error), state(identifier) == baseline]
            finally:
                success_module._insert_message = real_insert

            def fail_release(*args, **kwargs):
                raise RuntimeError("after operation update")
            success_module._release_slot = fail_release
            try:
                repository.apply_success(supplied, "reply")
            except RuntimeError as error:
                failures["operation"] = [str(error), state(identifier) == baseline]
            finally:
                success_module._release_slot = real_release

            print(json.dumps({
                "zero": type(zero).__name__,
                "zero_unchanged": zero_unchanged,
                "failures": failures,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["zero"], "OwnershipLost")
        self.assertTrue(payload["zero_unchanged"])
        self.assertEqual(
            payload["failures"],
            {
                "conversation": ["after conversation update", True],
                "first": ["after first message", True],
                "second": ["after second message", True],
                "operation": ["after operation update", True],
            },
        )

    def test_contention_ambiguity_and_frozen_inventory(self):
        result = _run(
            """\
            identifier = uuid.UUID("77777777777777777777777777777777")
            running(identifier)
            clock = Clock(99)
            repository = SqliteConversationRepository(clock, RUNNER)
            holder = sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True)
            try:
                holder.execute("BEGIN IMMEDIATE")
                busy = repository.apply_success(claimed(identifier), "reply")
            finally:
                holder.rollback()
                holder.close()
            busy_calls = clock.calls
            real_commit = connection.commit
            def fail_commit(*args, **kwargs):
                raise OperationalError("injected commit uncertainty")
            connection.commit = fail_commit
            try:
                ambiguous = repository.apply_success(claimed(identifier), "reply")
            finally:
                connection.commit = real_commit
            connection.close()
            print(json.dumps({
                "busy": type(busy).__name__,
                "busy_calls": busy_calls,
                "ambiguous": type(ambiguous).__name__,
                "operation_id": str(ambiguous.operation_id),
                "runner": ambiguous.runner_id,
                "claimed_at": ambiguous.claimed_at,
                "conversation_id": str(ambiguous.conversation_id),
                "state": state(identifier),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["busy"], "DatabaseBusy")
        self.assertEqual(payload["busy_calls"], 0)
        self.assertEqual(payload["ambiguous"], "CommitAmbiguous")
        self.assertEqual(
            payload["operation_id"],
            "77777777-7777-7777-7777-777777777777",
        )
        self.assertEqual(payload["runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(payload["claimed_at"], 1_000)
        self.assertEqual(
            payload["conversation_id"],
            "00112233-4455-6677-8899-aabbccddeeff",
        )
        self.assertEqual(payload["state"]["status"], "RUNNING")
        self.assertEqual(payload["state"]["messages"], [])

        sys.path.insert(0, str(SRC_ROOT))
        from max_smart_db.sql_policy import scan_migration_package

        inventory = scan_migration_package()
        self.assertEqual(sum(len(v) for v in inventory.forward_sql.values()), 40)
        self.assertEqual(sum(len(v) for v in inventory.reverse_sql.values()), 40)


if __name__ == "__main__":
    unittest.main()
