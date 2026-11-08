"""Disposable-database tests for guarded late success reconciliation."""

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
from max_smart_db.repository import ClaimedOperation, RetainedResult

late_module = importlib.import_module("max_smart_db.late_success")
MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

OWNER = User.objects.create(username="owner")
OTHER = User.objects.create(username="other")
CONVERSATION = uuid.UUID("00112233445566778899aabbccddeeff")
OTHER_CONVERSATION = uuid.UUID("ffeeddccbbaa99887766554433221100")
UNKNOWN = uuid.UUID("11111111111111111111111111111111")
OCCUPANT = uuid.UUID("22222222222222222222222222222222")
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
STALE = "BBBBBBBBBBBBBBBBBBBBBQ"
HASH = "ab" * 32
STARTED = 1_000
DEADLINE = STARTED + 600_000_000
LEASE = STARTED + 720_000_000
FINISHED = 4_242

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
Conversation.objects.create(
    id=OTHER_CONVERSATION,
    owner_id=OWNER.id,
    role_id="max-smart",
    version=0,
    next_sequence=1,
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


def unknown():
    Operation.objects.create(
        id=UNKNOWN,
        conversation_id=CONVERSATION,
        owner_id=OWNER.id,
        operation_type="message",
        provider_id="openai",
        model_id="gpt-5.6-sol",
        status="OUTCOME_UNKNOWN",
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
        finished_at_us=FINISHED,
        acknowledged_at_us=None,
        failure_category="provider_outcome_uncertain",
    )


def occupy_slot():
    Operation.objects.create(
        id=OCCUPANT,
        conversation_id=OTHER_CONVERSATION,
        owner_id=OWNER.id,
        operation_type="message",
        provider_id="openai",
        model_id="gpt-5.6-sol",
        status="RUNNING",
        blocks_conversation=1,
        expected_version=0,
        request_content="other",
        fingerprint_version=1,
        request_hash=HASH,
        retry_of=None,
        runner_id=STALE,
        reservation_expires_at_us=None,
        provider_started_at_us=2,
        execution_deadline_us=3,
        lease_expires_at_us=4,
        created_at_us=2,
        finished_at_us=None,
        acknowledged_at_us=None,
        failure_category=None,
    )
    GlobalModelSlot.objects.filter(pk=1).update(
        operation_id=OCCUPANT,
        runner_id=STALE,
        reservation_expires_at_us=None,
        lease_expires_at_us=4,
        acquired_at_us=8,
    )


def retained(output="  exact assistant\r\n", *, actor=None, runner=RUNNER,
             request_hash=HASH, expected_version=4, operation_id=UNKNOWN):
    return RetainedResult(
        claimed_operation=ClaimedOperation(
            operation_id=operation_id,
            actor_id=OWNER.id if actor is None else actor,
            conversation_id=CONVERSATION,
            request_hash=request_hash,
            fingerprint_version=1,
            expected_version=expected_version,
            runner_id=runner,
            claimed_at=STARTED,
            execution_deadline=DEADLINE,
            lease_expires_at=LEASE,
        ),
        output_text=output,
    )


def snapshot():
    operation = Operation.objects.get(pk=UNKNOWN)
    conversation = Conversation.objects.get(pk=CONVERSATION)
    slot = GlobalModelSlot.objects.get(pk=1)
    messages = list(
        Message.objects.filter(conversation_id=CONVERSATION)
        .order_by("sequence")
        .values_list("sequence", "role", "content", "operation_id", "created_at_us")
    )
    return {
        "status": operation.status,
        "blocks": operation.blocks_conversation,
        "runner": operation.runner_id,
        "started": operation.provider_started_at_us,
        "deadline": operation.execution_deadline_us,
        "lease": operation.lease_expires_at_us,
        "finished": operation.finished_at_us,
        "category": operation.failure_category,
        "acknowledged": operation.acknowledged_at_us,
        "version": conversation.version,
        "next_sequence": conversation.next_sequence,
        "updated": conversation.updated_at_us,
        "slot_operation": None if slot.operation_id is None else str(slot.operation_id),
        "slot_runner": slot.runner_id,
        "slot_lease": slot.lease_expires_at_us,
        "slot_acquired": slot.acquired_at_us,
        "messages": [
            [sequence, role, content, str(operation_id), created]
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
                Path(temporary) / "r3c8.sqlite3"
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


class LateSuccessTests(unittest.TestCase):
    def test_late_success_preserves_slot_and_historical_evidence(self):
        result = _run(
            """\
            unknown()
            occupy_slot()
            clock = Clock(90_000)
            outcome = SqliteConversationRepository(
                clock,
                RUNNER,
            ).reconcile_late_success(retained())
            current = snapshot()
            print(json.dumps({
                "type": type(outcome).__name__,
                "status": outcome.operation.status.value,
                "blocks": outcome.operation.blocks_conversation,
                "category": outcome.operation.failure_category.value,
                "calls": clock.calls,
                "state": current,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "Reconciled")
        self.assertEqual(payload["status"], "SUCCEEDED")
        self.assertIs(payload["blocks"], False)
        self.assertEqual(payload["category"], "provider_outcome_uncertain")
        self.assertEqual(payload["calls"], 1)
        current = payload["state"]
        self.assertEqual(current["version"], 5)
        self.assertEqual(current["next_sequence"], 7)
        self.assertEqual(current["updated"], 90_000)
        self.assertEqual(current["finished"], 4_242)
        self.assertEqual(current["started"], 1_000)
        self.assertEqual(current["deadline"], 600_001_000)
        self.assertEqual(current["lease"], 720_001_000)
        self.assertIsNone(current["runner"])
        self.assertEqual(
            current["slot_operation"],
            "22222222-2222-2222-2222-222222222222",
        )
        self.assertEqual(current["slot_runner"], "BBBBBBBBBBBBBBBBBBBBBQ")
        self.assertEqual(current["slot_lease"], 4)
        self.assertEqual(current["slot_acquired"], 8)
        self.assertEqual(
            current["messages"],
            [
                [
                    5,
                    "user",
                    "  exact user content\r\n",
                    "11111111-1111-1111-1111-111111111111",
                    90_000,
                ],
                [
                    6,
                    "assistant",
                    "  exact assistant\r\n",
                    "11111111-1111-1111-1111-111111111111",
                    90_000,
                ],
            ],
        )

    def test_duplicate_and_acknowledgement_do_not_mutate(self):
        result = _run(
            """\
            unknown()
            occupy_slot()
            repository = SqliteConversationRepository(Clock(90_000), RUNNER)
            Operation.objects.filter(pk=UNKNOWN).update(
                blocks_conversation=0,
                acknowledged_at_us=91_000,
            )
            acknowledged = repository.reconcile_late_success(retained("late"))
            acknowledged_messages = Message.objects.filter(
                operation_id=UNKNOWN
            ).count()
            Operation.objects.filter(pk=UNKNOWN).update(
                blocks_conversation=1,
                acknowledged_at_us=None,
            )
            first = repository.reconcile_late_success(retained("original"))
            before = snapshot()
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
                duplicate = repository.reconcile_late_success(
                    retained("ignored replacement")
                )
            finally:
                QuerySet.update = real_update
                Message.objects.create = real_create
            print(json.dumps({
                "first": type(first).__name__,
                "duplicate": type(duplicate).__name__,
                "assistant": duplicate.assistant_message.content,
                "updates": calls["update"],
                "creates": calls["create"],
                "unchanged": snapshot()["slot_operation"] == before["slot_operation"],
                "acknowledged": type(acknowledged).__name__,
                "messages": acknowledged_messages,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["first"], "Reconciled")
        self.assertEqual(payload["duplicate"], "AlreadySucceeded")
        self.assertEqual(payload["assistant"], "original")
        self.assertEqual(payload["updates"], 0)
        self.assertEqual(payload["creates"], 0)
        self.assertTrue(payload["unchanged"])
        self.assertEqual(payload["acknowledged"], "AcknowledgedDiscard")
        self.assertEqual(payload["messages"], 0)

    def test_identity_fingerprint_version_archive_and_state_discards(self):
        result = _run(
            """\
            unknown()
            occupy_slot()
            before = snapshot()
            repository = SqliteConversationRepository(Clock(10), RUNNER)
            missing = repository.reconcile_late_success(
                retained(operation_id=uuid.uuid4())
            )
            cross = repository.reconcile_late_success(retained(actor=OTHER.id))
            runner = repository.reconcile_late_success(retained(runner=STALE))
            fingerprint = repository.reconcile_late_success(
                retained(request_hash="cd" * 32)
            )
            stale = repository.reconcile_late_success(retained(expected_version=9))
            Conversation.objects.filter(pk=CONVERSATION).update(version=8)
            advanced = repository.reconcile_late_success(retained())
            Conversation.objects.filter(pk=CONVERSATION).update(
                version=4,
                archived_at_us=3,
            )
            archived = repository.reconcile_late_success(retained())
            Conversation.objects.filter(pk=CONVERSATION).update(archived_at_us=None)
            Operation.objects.filter(pk=UNKNOWN).update(
                status="FAILED",
                blocks_conversation=0,
                runner_id=None,
                failure_category="provider_definite_failure",
            )
            terminal = repository.reconcile_late_success(retained())
            print(json.dumps({
                "missing": type(missing).__name__,
                "cross": type(cross).__name__,
                "equal": missing == cross,
                "runner": type(runner).__name__,
                "fingerprint": type(fingerprint).__name__,
                "stale": type(stale).__name__,
                "advanced": type(advanced).__name__,
                "archived": type(archived).__name__,
                "terminal": type(terminal).__name__,
                "unchanged_slot": snapshot()["slot_operation"] == before["slot_operation"],
                "messages": Message.objects.filter(conversation_id=CONVERSATION).count(),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["missing"], "InvalidStateDiscard")
        self.assertEqual(payload["cross"], "InvalidStateDiscard")
        self.assertTrue(payload["equal"])
        self.assertEqual(payload["runner"], "RunnerMismatchDiscard")
        self.assertEqual(payload["fingerprint"], "FingerprintMismatch")
        self.assertEqual(payload["stale"], "StaleVersionDiscard")
        self.assertEqual(payload["advanced"], "StaleVersionDiscard")
        self.assertEqual(payload["archived"], "InvalidStateDiscard")
        self.assertEqual(payload["terminal"], "InvalidStateDiscard")
        self.assertTrue(payload["unchanged_slot"])
        self.assertEqual(payload["messages"], 0)

    def test_invalid_output_zero_row_and_partial_writes_roll_back(self):
        result = _run(
            """\
            unknown()
            occupy_slot()
            baseline = snapshot()
            repository = SqliteConversationRepository(Clock(10), RUNNER)
            rejected = []
            for output in (None, "", " \\t", "bad\\x00", "\\ud800"):
                try:
                    repository.reconcile_late_success(retained(output))
                except ApplySuccessInputError as error:
                    rejected.append(error.code)
                else:
                    rejected.append("accepted")
            real_update = QuerySet.update
            def zero_conversation(self, **kwargs):
                if self.model is Conversation and "next_sequence" in kwargs:
                    return 0
                return real_update(self, **kwargs)
            QuerySet.update = zero_conversation
            try:
                zero = repository.reconcile_late_success(retained("reply"))
            finally:
                QuerySet.update = real_update
            failures = {}
            real_insert = late_module._insert_message
            def fail_insert(*args, **kwargs):
                raise RuntimeError("after conversation update")
            late_module._insert_message = fail_insert
            try:
                repository.reconcile_late_success(retained("reply"))
            except RuntimeError as error:
                failures["insert"] = str(error)
            finally:
                late_module._insert_message = real_insert
            def zero_operation(self, **kwargs):
                if self.model is Operation and kwargs.get("status") == "SUCCEEDED":
                    return 0
                return real_update(self, **kwargs)
            QuerySet.update = zero_operation
            try:
                operation_race = repository.reconcile_late_success(retained("reply"))
            finally:
                QuerySet.update = real_update
            print(json.dumps({
                "rejected": rejected,
                "zero": type(zero).__name__,
                "insert": failures["insert"],
                "operation": type(operation_race).__name__,
                "unchanged": snapshot() == baseline,
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
        self.assertEqual(payload["zero"], "InvalidStateDiscard")
        self.assertEqual(payload["insert"], "after conversation update")
        self.assertEqual(payload["operation"], "InvalidStateDiscard")
        self.assertTrue(payload["unchanged"])

    def test_contention_ambiguity_and_no_slot_access(self):
        result = _run(
            """\
            unknown()
            occupy_slot()
            clock = Clock(70)
            repository = SqliteConversationRepository(clock, RUNNER)
            holder = sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True)
            try:
                holder.execute("BEGIN IMMEDIATE")
                busy = repository.reconcile_late_success(retained("reply"))
                busy_calls = clock.calls
            finally:
                holder.rollback()
                holder.close()
            real_commit = connection.commit
            def fail_commit(*args, **kwargs):
                raise OperationalError("injected commit uncertainty")
            connection.commit = fail_commit
            try:
                ambiguous = repository.reconcile_late_success(retained("reply"))
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
                "status": snapshot()["status"],
                "slot": snapshot()["slot_operation"],
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["busy"], "DatabaseBusy")
        self.assertEqual(payload["busy_calls"], 0)
        self.assertEqual(payload["ambiguous"], "CommitAmbiguous")
        self.assertEqual(
            payload["operation_id"],
            "11111111-1111-1111-1111-111111111111",
        )
        self.assertEqual(payload["runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(payload["claimed_at"], 1_000)
        self.assertEqual(
            payload["conversation_id"],
            "00112233-4455-6677-8899-aabbccddeeff",
        )
        self.assertEqual(payload["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(
            payload["slot"],
            "22222222-2222-2222-2222-222222222222",
        )
        source = (SRC_ROOT / "max_smart_db" / "late_success.py").read_text()
        self.assertNotIn("GlobalModelSlot", source)
        self.assertNotIn("global_model_slot", source)


if __name__ == "__main__":
    unittest.main()
