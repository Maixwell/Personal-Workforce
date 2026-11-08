"""Disposable-database tests for unknown-outcome acknowledgement."""

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
from max_smart_db.creation import SqliteConversationRepository
from max_smart_db.models import Conversation, GlobalModelSlot, Message, Operation
from max_smart_db.repository import (
    ClaimedOperation,
    RepositoryIntegrityError,
    RetainedResult,
)

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


def retained():
    return RetainedResult(
        claimed_operation=ClaimedOperation(
            operation_id=UNKNOWN,
            actor_id=OWNER.id,
            conversation_id=CONVERSATION,
            request_hash=HASH,
            fingerprint_version=1,
            expected_version=4,
            runner_id=RUNNER,
            claimed_at=STARTED,
            execution_deadline=DEADLINE,
            lease_expires_at=LEASE,
        ),
        output_text="late output",
    )


def snapshot():
    operation = Operation.objects.get(pk=UNKNOWN)
    conversation = Conversation.objects.get(pk=CONVERSATION)
    slot = GlobalModelSlot.objects.get(pk=1)
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
        "messages": Message.objects.filter(conversation_id=CONVERSATION).count(),
        "slot_operation": None if slot.operation_id is None else str(slot.operation_id),
        "slot_runner": slot.runner_id,
        "slot_lease": slot.lease_expires_at_us,
        "slot_acquired": slot.acquired_at_us,
    }
"""


def _run(body, *, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "r3c9.sqlite3"
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


class AcknowledgeUnknownTests(unittest.TestCase):
    def test_acknowledgement_preserves_history_and_occupied_slot(self):
        result = _run(
            """\
            unknown()
            occupy_slot()
            clock = Clock(90_000)
            repository = SqliteConversationRepository(clock, RUNNER)
            outcome = repository.acknowledge_unknown(OWNER.id, UNKNOWN)
            current = snapshot()
            print(json.dumps({
                "type": type(outcome).__name__,
                "status": outcome.operation.status.value,
                "blocks": outcome.operation.blocks_conversation,
                "acknowledged": outcome.operation.acknowledged,
                "category": outcome.operation.failure_category.value,
                "calls": clock.calls,
                "state": current,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "Acknowledged")
        self.assertEqual(payload["status"], "OUTCOME_UNKNOWN")
        self.assertIs(payload["blocks"], False)
        self.assertIs(payload["acknowledged"], True)
        self.assertEqual(payload["category"], "provider_outcome_uncertain")
        self.assertEqual(payload["calls"], 1)
        current = payload["state"]
        self.assertEqual(current["acknowledged"], 90_000)
        self.assertEqual(current["blocks"], 0)
        self.assertEqual(current["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(current["runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(current["started"], 1_000)
        self.assertEqual(current["deadline"], 600_001_000)
        self.assertEqual(current["lease"], 720_001_000)
        self.assertEqual(current["finished"], 4_242)
        self.assertEqual(current["category"], "provider_outcome_uncertain")
        self.assertEqual(current["version"], 4)
        self.assertEqual(current["next_sequence"], 5)
        self.assertEqual(current["updated"], 0)
        self.assertEqual(current["messages"], 0)
        self.assertEqual(
            current["slot_operation"],
            "22222222-2222-2222-2222-222222222222",
        )
        self.assertEqual(current["slot_runner"], "BBBBBBBBBBBBBBBBBBBBBQ")
        self.assertEqual(current["slot_lease"], 4)
        self.assertEqual(current["slot_acquired"], 8)

    def test_repeat_missing_owner_and_other_states_do_not_write(self):
        result = _run(
            """\
            unknown()
            occupy_slot()
            before = snapshot()
            repository = SqliteConversationRepository(Clock(90_000), RUNNER)
            repository.acknowledge_unknown(OWNER.id, UNKNOWN)
            real_update = QuerySet.update
            calls = {"update": 0}
            def counting_update(self, **kwargs):
                calls["update"] += 1
                return real_update(self, **kwargs)
            QuerySet.update = counting_update
            try:
                repeat = repository.acknowledge_unknown(OWNER.id, UNKNOWN)
                stored_at = Operation.objects.get(pk=UNKNOWN).acknowledged_at_us
            finally:
                QuerySet.update = real_update
            missing = repository.acknowledge_unknown(OWNER.id, uuid.uuid4())
            cross = repository.acknowledge_unknown(OTHER.id, UNKNOWN)
            states = {}
            shapes = {
                "FAILED": {
                    "status": "FAILED",
                    "blocks_conversation": 0,
                    "runner_id": None,
                    "failure_category": "provider_definite_failure",
                    "acknowledged_at_us": None,
                },
                "CANCELLED": {
                    "status": "CANCELLED",
                    "blocks_conversation": 0,
                    "runner_id": None,
                    "reservation_expires_at_us": None,
                    "provider_started_at_us": None,
                    "execution_deadline_us": None,
                    "lease_expires_at_us": None,
                    "finished_at_us": FINISHED,
                    "acknowledged_at_us": None,
                    "failure_category": None,
                },
                "SUCCEEDED": {
                    "status": "SUCCEEDED",
                    "blocks_conversation": 0,
                    "runner_id": None,
                    "reservation_expires_at_us": None,
                    "provider_started_at_us": STARTED,
                    "execution_deadline_us": DEADLINE,
                    "lease_expires_at_us": LEASE,
                    "finished_at_us": FINISHED,
                    "acknowledged_at_us": None,
                    "failure_category": None,
                },
            }
            for name, fields in shapes.items():
                Operation.objects.filter(pk=UNKNOWN).update(**fields)
                states[name] = repository.acknowledge_unknown(
                    OWNER.id,
                    UNKNOWN,
                ).current_state.value
            print(json.dumps({
                "repeat": type(repeat).__name__,
                "repeat_status": repeat.operation.status.value,
                "repeat_acknowledged": repeat.operation.acknowledged,
                "stored_at": stored_at,
                "updates": calls["update"],
                "missing": type(missing).__name__,
                "cross": type(cross).__name__,
                "equal": missing == cross,
                "states": states,
                "slot": snapshot()["slot_operation"] == before["slot_operation"],
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["repeat"], "AlreadyAcknowledged")
        self.assertEqual(payload["repeat_status"], "OUTCOME_UNKNOWN")
        self.assertIs(payload["repeat_acknowledged"], True)
        self.assertEqual(payload["stored_at"], 90_000)
        self.assertEqual(payload["updates"], 0)
        self.assertEqual(payload["missing"], "NotFound")
        self.assertEqual(payload["cross"], "NotFound")
        self.assertTrue(payload["equal"])
        self.assertEqual(
            payload["states"],
            {
                "FAILED": "FAILED",
                "CANCELLED": "CANCELLED",
                "SUCCEEDED": "SUCCEEDED",
            },
        )
        self.assertTrue(payload["slot"])

    def test_zero_row_and_partial_failure_roll_back(self):
        result = _run(
            """\
            unknown()
            occupy_slot()
            baseline = snapshot()
            repository = SqliteConversationRepository(Clock(10), RUNNER)
            real_update = QuerySet.update
            def zero_update(self, **kwargs):
                if self.model is Operation and "acknowledged_at_us" in kwargs:
                    return 0
                return real_update(self, **kwargs)
            QuerySet.update = zero_update
            try:
                repository.acknowledge_unknown(OWNER.id, UNKNOWN)
                zero = "accepted"
            except RepositoryIntegrityError:
                zero = "RepositoryIntegrityError"
            finally:
                QuerySet.update = real_update
            def excess_update(self, **kwargs):
                if self.model is Operation and "acknowledged_at_us" in kwargs:
                    return 2
                return real_update(self, **kwargs)
            QuerySet.update = excess_update
            try:
                repository.acknowledge_unknown(OWNER.id, UNKNOWN)
                excess = "accepted"
            except RepositoryIntegrityError:
                excess = "RepositoryIntegrityError"
            finally:
                QuerySet.update = real_update
            real_refresh = Operation.refresh_from_db
            def fail_refresh(self, *args, **kwargs):
                raise RuntimeError("after acknowledgement update")
            Operation.refresh_from_db = fail_refresh
            try:
                repository.acknowledge_unknown(OWNER.id, UNKNOWN)
                partial = "accepted"
            except RuntimeError as error:
                partial = str(error)
            finally:
                Operation.refresh_from_db = real_refresh
            print(json.dumps({
                "zero": zero,
                "excess": excess,
                "partial": partial,
                "unchanged": snapshot() == baseline,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["zero"], "RepositoryIntegrityError")
        self.assertEqual(payload["excess"], "RepositoryIntegrityError")
        self.assertEqual(payload["partial"], "after acknowledgement update")
        self.assertTrue(payload["unchanged"])

    def test_contention_ambiguity_and_acknowledgement_blocks_late_success(self):
        result = _run(
            """\
            unknown()
            occupy_slot()
            clock = Clock(70)
            repository = SqliteConversationRepository(clock, RUNNER)
            holder = sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True)
            try:
                holder.execute("BEGIN IMMEDIATE")
                busy = repository.acknowledge_unknown(OWNER.id, UNKNOWN)
                busy_calls = clock.calls
            finally:
                holder.rollback()
                holder.close()
            real_commit = connection.commit
            def fail_commit(*args, **kwargs):
                raise OperationalError("injected commit uncertainty")
            connection.commit = fail_commit
            try:
                ambiguous = repository.acknowledge_unknown(OWNER.id, UNKNOWN)
            finally:
                connection.commit = real_commit
            connection.close()
            after_ambiguity = snapshot()
            acknowledged = repository.acknowledge_unknown(OWNER.id, UNKNOWN)
            late = repository.reconcile_late_success(retained())
            print(json.dumps({
                "busy": type(busy).__name__,
                "busy_calls": busy_calls,
                "ambiguous": type(ambiguous).__name__,
                "operation_id": str(ambiguous.operation_id),
                "runner": ambiguous.runner_id,
                "status_after_ambiguity": after_ambiguity["status"],
                "blocks_after_ambiguity": after_ambiguity["blocks"],
                "slot_after_ambiguity": after_ambiguity["slot_operation"],
                "acknowledged": type(acknowledged).__name__,
                "late": type(late).__name__,
                "version": snapshot()["version"],
                "messages": snapshot()["messages"],
                "slot": snapshot()["slot_operation"],
                "final_status": snapshot()["status"],
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
        self.assertIsNone(payload["runner"])
        self.assertEqual(payload["status_after_ambiguity"], "OUTCOME_UNKNOWN")
        self.assertEqual(payload["blocks_after_ambiguity"], 1)
        self.assertEqual(
            payload["slot_after_ambiguity"],
            "22222222-2222-2222-2222-222222222222",
        )
        self.assertEqual(payload["acknowledged"], "Acknowledged")
        self.assertEqual(payload["late"], "AcknowledgedDiscard")
        self.assertEqual(payload["version"], 4)
        self.assertEqual(payload["messages"], 0)
        self.assertEqual(
            payload["slot"],
            "22222222-2222-2222-2222-222222222222",
        )
        self.assertEqual(payload["final_status"], "OUTCOME_UNKNOWN")
        source = (SRC_ROOT / "max_smart_db" / "acknowledgement.py").read_text()
        self.assertNotIn("GlobalModelSlot", source)
        self.assertNotIn("global_model_slot", source)


if __name__ == "__main__":
    unittest.main()
