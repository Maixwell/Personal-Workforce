"""Disposable-database tests for definite and uncertain provider failure."""

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
from max_smart_db.provider_failure import ProviderFailureInputError
from max_smart_db.repository import (
    ClaimedOperation,
    DefiniteProviderCategory,
    FailureClassification,
    PreProviderFailureCategory,
    StoredFailureCategory,
    UncertainProviderCategory,
)

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
        request_content="hello",
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


def claimed(identifier, *, actor=None, runner=RUNNER):
    return ClaimedOperation(
        operation_id=identifier,
        actor_id=OWNER.id if actor is None else actor,
        conversation_id=CONVERSATION,
        request_hash=HASH,
        fingerprint_version=1,
        expected_version=4,
        runner_id=runner,
        claimed_at=STARTED,
        execution_deadline=DEADLINE,
        lease_expires_at=LEASE,
    )


def state(identifier):
    operation = Operation.objects.get(pk=identifier)
    slot = GlobalModelSlot.objects.get(pk=1)
    conversation = Conversation.objects.get(pk=CONVERSATION)
    return {
        "status": operation.status,
        "blocks": operation.blocks_conversation,
        "runner": operation.runner_id,
        "started": operation.provider_started_at_us,
        "deadline": operation.execution_deadline_us,
        "lease": operation.lease_expires_at_us,
        "finished": operation.finished_at_us,
        "category": operation.failure_category,
        "request": operation.request_content,
        "expected": operation.expected_version,
        "version": conversation.version,
        "sequence": conversation.next_sequence,
        "messages": Message.objects.count(),
        "slot_operation": (
            None if slot.operation_id is None else str(slot.operation_id)
        ),
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
                Path(temporary) / "r3c6.sqlite3"
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


class ProviderFailureTests(unittest.TestCase):
    def test_definite_failure_unblocks_and_releases_without_history_changes(self):
        result = _run(
            """\
            stored = []
            categories = (
                DefiniteProviderCategory.PROVIDER_DEFINITE_FAILURE,
                DefiniteProviderCategory.PROVIDER_OUTPUT_INVALID,
            )
            for index, category in enumerate(categories):
                identifier = uuid.UUID(f"{index + 1:032x}")
                running(identifier)
                clock = Clock(50_000 + index)
                outcome = SqliteConversationRepository(
                    clock,
                    RUNNER,
                ).apply_failure(claimed(identifier), category)
                current = state(identifier)
                stored.append({
                    "type": type(outcome).__name__,
                    "category": outcome.operation.failure_category.value,
                    "status": outcome.operation.status.value,
                    "blocks": outcome.operation.blocks_conversation,
                    "calls": clock.calls,
                    "state": current,
                })
            print(json.dumps(stored))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(
            [item["category"] for item in payload],
            ["provider_definite_failure", "provider_output_invalid"],
        )
        for item, category in zip(payload, (
            "provider_definite_failure",
            "provider_output_invalid",
        )):
            self.assertEqual(item["type"], "DefinitelyFailed")
            self.assertEqual(item["status"], "FAILED")
            self.assertIs(item["blocks"], False)
            self.assertEqual(item["calls"], 1)
            current = item["state"]
            self.assertEqual(current["category"], category)
            self.assertEqual(current["finished"], 50_000 + payload.index(item))
            self.assertEqual(current["started"], 1_000)
            self.assertEqual(current["deadline"], 600_001_000)
            self.assertEqual(current["lease"], 720_001_000)
            self.assertIsNone(current["runner"])
            self.assertIsNone(current["slot_operation"])
            self.assertEqual(current["version"], 0)
            self.assertEqual(current["sequence"], 1)
            self.assertEqual(current["messages"], 0)
            self.assertEqual(current["request"], "hello")

    def test_uncertainty_retains_running_claim_then_definite_failure_can_finish(self):
        result = _run(
            """\
            identifier = uuid.UUID("33333333333333333333333333333333")
            running(identifier)
            clock = Clock(80)
            repository = SqliteConversationRepository(clock, RUNNER)
            category = UncertainProviderCategory.PROVIDER_OUTCOME_UNCERTAIN
            outcome = repository.apply_failure(claimed(identifier), category)
            real = QuerySet.update
            calls = {"update": 0}
            def counting(self, **kwargs):
                calls["update"] += 1
                return real(self, **kwargs)
            QuerySet.update = counting
            try:
                replay = repository.apply_failure(claimed(identifier), category)
            finally:
                QuerySet.update = real
            retained = state(identifier)
            definite = repository.apply_failure(
                claimed(identifier),
                DefiniteProviderCategory.PROVIDER_DEFINITE_FAILURE,
            )
            print(json.dumps({
                "type": type(outcome).__name__,
                "status": outcome.operation.status.value,
                "blocks": outcome.operation.blocks_conversation,
                "category": outcome.operation.failure_category.value,
                "recovery_at": outcome.recovery_at,
                "calls": clock.calls,
                "replay": type(replay).__name__,
                "updates": calls["update"],
                "retained": retained,
                "definite": type(definite).__name__,
                "final": state(identifier),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "UncertainRecorded")
        self.assertEqual(payload["status"], "RUNNING")
        self.assertIs(payload["blocks"], True)
        self.assertEqual(payload["category"], "provider_outcome_uncertain")
        self.assertEqual(payload["recovery_at"], 720_001_000)
        self.assertEqual(payload["replay"], "UncertainRecorded")
        self.assertEqual(payload["updates"], 0)
        retained = payload["retained"]
        self.assertIsNone(retained["finished"])
        self.assertEqual(retained["runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(retained["slot_lease"], 720_001_000)
        self.assertEqual(retained["slot_acquired"], 7)
        self.assertEqual(retained["version"], 0)
        self.assertEqual(retained["sequence"], 1)
        self.assertEqual(payload["definite"], "DefinitelyFailed")
        self.assertEqual(payload["final"]["status"], "FAILED")
        self.assertIsNone(payload["final"]["slot_operation"])
        self.assertEqual(payload["final"]["started"], 1_000)

    def test_wrong_owner_stale_runner_mismatch_and_nonrunning_do_not_mutate(self):
        result = _run(
            """\
            identifier = uuid.UUID("44444444444444444444444444444444")
            Operation.objects.create(
                id=identifier,
                conversation_id=CONVERSATION,
                owner_id=OWNER.id,
                operation_type="message",
                provider_id="openai",
                model_id="gpt-5.6-sol",
                status="QUEUED",
                blocks_conversation=1,
                expected_version=4,
                request_content="hello",
                fingerprint_version=1,
                request_hash=HASH,
                retry_of=None,
                runner_id=RUNNER,
                reservation_expires_at_us=LEASE,
                provider_started_at_us=None,
                execution_deadline_us=None,
                lease_expires_at_us=None,
                created_at_us=1,
                finished_at_us=None,
                acknowledged_at_us=None,
                failure_category=None,
            )
            GlobalModelSlot.objects.filter(pk=1).update(
                operation_id=identifier,
                runner_id=RUNNER,
                reservation_expires_at_us=LEASE,
                lease_expires_at_us=None,
                acquired_at_us=7,
            )
            repository = SqliteConversationRepository(Clock(10), RUNNER)
            failure = DefiniteProviderCategory.PROVIDER_DEFINITE_FAILURE
            queued = repository.apply_failure(claimed(identifier), failure)
            queued_status = state(identifier)["status"]
            Operation.objects.filter(pk=identifier).update(
                status="RUNNING",
                reservation_expires_at_us=None,
                provider_started_at_us=STARTED,
                execution_deadline_us=DEADLINE,
                lease_expires_at_us=LEASE,
            )
            GlobalModelSlot.objects.filter(pk=1).update(
                reservation_expires_at_us=None,
                lease_expires_at_us=LEASE,
            )
            missing = repository.apply_failure(claimed(uuid.uuid4()), failure)
            cross = repository.apply_failure(
                claimed(identifier, actor=OTHER.id),
                failure,
            )
            Operation.objects.filter(pk=identifier).update(runner_id=STALE)
            stale = repository.apply_failure(claimed(identifier), failure)
            stale_runner = state(identifier)["runner"]
            Operation.objects.filter(pk=identifier).update(runner_id=RUNNER)
            GlobalModelSlot.objects.filter(pk=1).update(
                operation_id=None,
                runner_id=None,
                lease_expires_at_us=None,
                acquired_at_us=None,
            )
            mismatch = repository.apply_failure(claimed(identifier), failure)
            mismatch_status = state(identifier)["status"]
            Operation.objects.filter(pk=identifier).update(
                status="SUCCEEDED",
                blocks_conversation=0,
                runner_id=None,
                finished_at_us=3,
            )
            terminal = repository.apply_failure(claimed(identifier), failure)
            unknown_id = uuid.UUID("55555555555555555555555555555555")
            Operation.objects.create(
                id=unknown_id,
                conversation_id=CONVERSATION,
                owner_id=OWNER.id,
                operation_type="message",
                provider_id="openai",
                model_id="gpt-5.6-sol",
                status="OUTCOME_UNKNOWN",
                blocks_conversation=1,
                expected_version=4,
                request_content="hello",
                fingerprint_version=1,
                request_hash=HASH,
                retry_of=None,
                runner_id=RUNNER,
                reservation_expires_at_us=None,
                provider_started_at_us=STARTED,
                execution_deadline_us=DEADLINE,
                lease_expires_at_us=LEASE,
                created_at_us=1,
                finished_at_us=9,
                acknowledged_at_us=None,
                failure_category=None,
            )
            before = state(unknown_id)
            late_definite = repository.apply_failure(claimed(unknown_id), failure)
            late_uncertain = repository.apply_failure(
                claimed(unknown_id),
                UncertainProviderCategory.PROVIDER_OUTCOME_UNCERTAIN,
            )
            print(json.dumps({
                "queued": [type(queued).__name__, queued.state.value, queued_status],
                "missing": type(missing).__name__,
                "cross": type(cross).__name__,
                "same": missing == cross,
                "stale": type(stale).__name__,
                "stale_runner": stale_runner,
                "mismatch": type(mismatch).__name__,
                "mismatch_status": mismatch_status,
                "terminal": [
                    type(terminal).__name__,
                    terminal.operation.status.value,
                ],
                "late_definite": [
                    type(late_definite).__name__,
                    late_definite.operation.status.value,
                ],
                "late_uncertain": type(late_uncertain).__name__,
                "unchanged": state(unknown_id) == before,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["queued"], ["InvalidState", "QUEUED", "QUEUED"])
        self.assertEqual(payload["missing"], "OwnershipLost")
        self.assertEqual(payload["cross"], "OwnershipLost")
        self.assertTrue(payload["same"])
        self.assertEqual(payload["stale"], "OwnershipLost")
        self.assertEqual(payload["stale_runner"], "BBBBBBBBBBBBBBBBBBBBBQ")
        self.assertEqual(payload["mismatch"], "SlotMismatch")
        self.assertEqual(payload["mismatch_status"], "RUNNING")
        self.assertEqual(payload["terminal"], ["AlreadyTerminal", "SUCCEEDED"])
        self.assertEqual(
            payload["late_definite"],
            ["AlreadyTerminal", "OUTCOME_UNKNOWN"],
        )
        self.assertEqual(payload["late_uncertain"], "AlreadyTerminal")
        self.assertTrue(payload["unchanged"])

    def test_invalid_classifications_fail_before_the_transaction(self):
        result = _run(
            """\
            identifier = uuid.UUID("66666666666666666666666666666666")
            running(identifier)
            clock = Clock(10)
            repository = SqliteConversationRepository(clock, RUNNER)
            rejected = []
            for failure in (
                "provider_definite_failure",
                PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
                StoredFailureCategory.PERSISTENCE_FAILED,
                StoredFailureCategory.PROVIDER_DEFINITE_FAILURE,
                StoredFailureCategory.PROVIDER_OUTCOME_UNCERTAIN,
                FailureClassification.DEFINITE_PROVIDER,
                FailureClassification.PERSISTENCE_FAILED,
                FailureClassification.UNCERTAIN_PROVIDER,
            ):
                try:
                    repository.apply_failure(claimed(identifier), failure)
                except ProviderFailureInputError as error:
                    rejected.append(error.code)
                else:
                    rejected.append("accepted")
            print(json.dumps({
                "rejected": rejected,
                "calls": clock.calls,
                "status": state(identifier)["status"],
                "category": state(identifier)["category"],
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(
            payload["rejected"],
            ["failure_classification_invalid"] * 8,
        )
        self.assertEqual(payload["calls"], 0)
        self.assertEqual(payload["status"], "RUNNING")
        self.assertIsNone(payload["category"])

    def test_zero_row_and_partial_write_roll_back(self):
        result = _run(
            """\
            identifier = uuid.UUID("77777777777777777777777777777777")
            running(identifier)
            repository = SqliteConversationRepository(Clock(10), RUNNER)
            supplied = claimed(identifier)
            failure = DefiniteProviderCategory.PROVIDER_OUTPUT_INVALID
            real = QuerySet.update
            def zero_operation(self, **kwargs):
                if self.model is Operation and kwargs.get("status") == "FAILED":
                    return 0
                return real(self, **kwargs)
            QuerySet.update = zero_operation
            try:
                zero = repository.apply_failure(supplied, failure)
            finally:
                QuerySet.update = real
            zero_state = state(identifier)

            def fail_slot(self, **kwargs):
                if self.model is GlobalModelSlot and kwargs.get("operation_id") is None:
                    raise RuntimeError("injected slot failure")
                return real(self, **kwargs)
            QuerySet.update = fail_slot
            try:
                try:
                    repository.apply_failure(supplied, failure)
                except RuntimeError as error:
                    injected = str(error)
                else:
                    injected = "accepted"
            finally:
                QuerySet.update = real
            print(json.dumps({
                "zero": type(zero).__name__,
                "zero_status": zero_state["status"],
                "zero_slot": zero_state["slot_operation"],
                "failure": injected,
                "final": state(identifier),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["zero"], "OwnershipLost")
        self.assertEqual(payload["zero_status"], "RUNNING")
        self.assertEqual(
            payload["zero_slot"],
            "77777777-7777-7777-7777-777777777777",
        )
        self.assertEqual(payload["failure"], "injected slot failure")
        self.assertEqual(payload["final"]["status"], "RUNNING")
        self.assertEqual(payload["final"]["slot_lease"], 720_001_000)
        self.assertIsNone(payload["final"]["finished"])

    def test_contention_and_commit_ambiguity_preserve_claim_identifiers(self):
        result = _run(
            """\
            identifier = uuid.UUID("88888888888888888888888888888888")
            running(identifier)
            clock = Clock(77)
            repository = SqliteConversationRepository(clock, RUNNER)
            failure = DefiniteProviderCategory.PROVIDER_DEFINITE_FAILURE
            holder = sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True)
            try:
                holder.execute("BEGIN IMMEDIATE")
                busy = repository.apply_failure(claimed(identifier), failure)
            finally:
                holder.rollback()
                holder.close()
            busy_calls = clock.calls
            real_commit = connection.commit
            def fail_commit(*args, **kwargs):
                raise OperationalError("injected commit uncertainty")
            connection.commit = fail_commit
            try:
                ambiguous = repository.apply_failure(claimed(identifier), failure)
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
                "status": state(identifier)["status"],
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["busy"], "DatabaseBusy")
        self.assertEqual(payload["busy_calls"], 0)
        self.assertEqual(payload["ambiguous"], "CommitAmbiguous")
        self.assertEqual(
            payload["operation_id"],
            "88888888-8888-8888-8888-888888888888",
        )
        self.assertEqual(payload["runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(payload["claimed_at"], 1_000)
        self.assertEqual(
            payload["conversation_id"],
            "00112233-4455-6677-8899-aabbccddeeff",
        )
        self.assertEqual(payload["status"], "RUNNING")

    def test_frozen_schema_inventory_and_no_provider_execution(self):
        sys.path.insert(0, str(SRC_ROOT))
        from max_smart_db.sql_policy import scan_migration_package

        inventory = scan_migration_package()
        self.assertEqual(sum(len(v) for v in inventory.forward_sql.values()), 40)
        self.assertEqual(sum(len(v) for v in inventory.reverse_sql.values()), 40)
        source = (SRC_ROOT / "max_smart_db" / "provider_failure.py").read_text()
        self.assertNotIn("responses.create", source)
        self.assertNotIn("openai", source)


if __name__ == "__main__":
    unittest.main()
