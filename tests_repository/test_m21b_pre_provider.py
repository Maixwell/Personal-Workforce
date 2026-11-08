"""Disposable-database tests for definite pre-provider failure."""

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
from max_smart_db.pre_provider import PreProviderInputError
from max_smart_db.repository import (
    DefiniteProviderCategory,
    FailureClassification,
    PreProviderFailureCategory,
    QueuedReservation,
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


def queued(identifier):
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
        reservation_expires_at_us=1_000_000_000,
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
        reservation_expires_at_us=1_000_000_000,
        lease_expires_at_us=None,
        acquired_at_us=7,
    )


def token(identifier, *, actor=None, runner=RUNNER):
    return QueuedReservation(
        operation_id=identifier,
        actor_id=OWNER.id if actor is None else actor,
        conversation_id=CONVERSATION,
        request_hash=HASH,
        fingerprint_version=1,
        expected_version=4,
        runner_id=runner,
        reservation_expires_at=1_000_000_000,
    )


def state(identifier):
    operation = Operation.objects.get(pk=identifier)
    slot = GlobalModelSlot.objects.get(pk=1)
    conversation = Conversation.objects.get(pk=CONVERSATION)
    return {
        "status": operation.status,
        "blocks": operation.blocks_conversation,
        "runner": operation.runner_id,
        "reservation": operation.reservation_expires_at_us,
        "started": operation.provider_started_at_us,
        "deadline": operation.execution_deadline_us,
        "lease": operation.lease_expires_at_us,
        "finished": operation.finished_at_us,
        "category": operation.failure_category,
        "request": operation.request_content,
        "hash": operation.request_hash,
        "expected": operation.expected_version,
        "version": conversation.version,
        "sequence": conversation.next_sequence,
        "messages": Message.objects.count(),
        "slot_operation": (
            None if slot.operation_id is None else str(slot.operation_id)
        ),
        "slot_runner": slot.runner_id,
        "slot_reservation": slot.reservation_expires_at_us,
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
                Path(temporary) / "r3c5.sqlite3"
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


class PreProviderFailureTests(unittest.TestCase):
    def test_success_fails_unblocks_and_releases_without_history_changes(self):
        result = _run(
            """\
            identifier = uuid.UUID("11111111111111111111111111111111")
            queued(identifier)
            clock = Clock(654_321)
            repository = SqliteConversationRepository(clock, RUNNER)
            category = PreProviderFailureCategory.CONTEXT_UNAVAILABLE
            outcome = repository.fail_pre_provider(token(identifier), category)
            real = QuerySet.update
            calls = {"update": 0}
            def counting(self, **kwargs):
                calls["update"] += 1
                return real(self, **kwargs)
            QuerySet.update = counting
            try:
                replay = repository.fail_pre_provider(token(identifier), category)
            finally:
                QuerySet.update = real
            print(json.dumps({
                "type": type(outcome).__name__,
                "category": outcome.operation.failure_category.value,
                "status": outcome.operation.status.value,
                "blocks": outcome.operation.blocks_conversation,
                "calls": clock.calls,
                "replay": type(replay).__name__,
                "updates": calls["update"],
                "state": state(identifier),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "Failed")
        self.assertEqual(payload["category"], "context_unavailable")
        self.assertEqual(payload["status"], "FAILED")
        self.assertIs(payload["blocks"], False)
        self.assertEqual(payload["calls"], 2)
        self.assertEqual(payload["replay"], "AlreadyFailed")
        self.assertEqual(payload["updates"], 0)
        current = payload["state"]
        self.assertEqual(current["finished"], 654_321)
        self.assertEqual(current["category"], "context_unavailable")
        self.assertIsNone(current["runner"])
        self.assertIsNone(current["reservation"])
        self.assertIsNone(current["started"])
        self.assertIsNone(current["slot_operation"])
        self.assertEqual(current["version"], 0)
        self.assertEqual(current["sequence"], 1)
        self.assertEqual(current["messages"], 0)
        self.assertEqual(current["request"], "hello")
        self.assertEqual(current["expected"], 4)

    def test_remaining_approved_categories_are_stored_exactly(self):
        result = _run(
            """\
            stored = []
            for index, category in enumerate((
                PreProviderFailureCategory.CONTEXT_BUDGET_EXCEEDED,
                PreProviderFailureCategory.MODEL_CONFIGURATION_INVALID,
            )):
                identifier = uuid.UUID(f"{index + 2:032x}")
                queued(identifier)
                outcome = SqliteConversationRepository(
                    Clock(50 + index),
                    RUNNER,
                ).fail_pre_provider(token(identifier), category)
                stored.append([
                    type(outcome).__name__,
                    outcome.operation.failure_category.value,
                    state(identifier)["category"],
                    state(identifier)["slot_operation"],
                ])
            print(json.dumps(stored))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(
            payload,
            [
                [
                    "Failed",
                    "context_budget_exceeded",
                    "context_budget_exceeded",
                    None,
                ],
                [
                    "Failed",
                    "model_configuration_invalid",
                    "model_configuration_invalid",
                    None,
                ],
            ],
        )

    def test_wrong_owner_stale_runner_mismatch_and_nonqueued_do_not_mutate(self):
        result = _run(
            """\
            identifier = uuid.UUID("33333333333333333333333333333333")
            queued(identifier)
            repository = SqliteConversationRepository(Clock(10), RUNNER)
            category = PreProviderFailureCategory.CONTEXT_UNAVAILABLE
            missing = repository.fail_pre_provider(token(uuid.uuid4()), category)
            cross = repository.fail_pre_provider(
                token(identifier, actor=OTHER.id),
                category,
            )
            stale = repository.fail_pre_provider(
                token(identifier, runner=STALE),
                category,
            )
            after_stale = state(identifier)
            GlobalModelSlot.objects.filter(pk=1).update(
                operation_id=None,
                runner_id=None,
                reservation_expires_at_us=None,
                acquired_at_us=None,
            )
            mismatch = repository.fail_pre_provider(token(identifier), category)
            after_mismatch = state(identifier)
            GlobalModelSlot.objects.filter(pk=1).update(
                operation_id=identifier,
                runner_id=RUNNER,
                lease_expires_at_us=2_000_000_000,
                acquired_at_us=7,
            )
            Operation.objects.filter(pk=identifier).update(
                status="RUNNING",
                reservation_expires_at_us=None,
                provider_started_at_us=1,
                execution_deadline_us=1_000_000_000,
                lease_expires_at_us=2_000_000_000,
            )
            running = repository.fail_pre_provider(token(identifier), category)
            Operation.objects.filter(pk=identifier).update(
                status="SUCCEEDED",
                blocks_conversation=0,
                runner_id=None,
                finished_at_us=3,
            )
            terminal = repository.fail_pre_provider(token(identifier), category)
            print(json.dumps({
                "missing": type(missing).__name__,
                "cross": type(cross).__name__,
                "same": missing == cross,
                "stale": type(stale).__name__,
                "stale_status": after_stale["status"],
                "mismatch": type(mismatch).__name__,
                "mismatch_status": after_mismatch["status"],
                "running": type(running).__name__,
                "terminal": type(terminal).__name__,
                "terminal_status": state(identifier)["status"],
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["missing"], "OwnershipLost")
        self.assertEqual(payload["cross"], "OwnershipLost")
        self.assertTrue(payload["same"])
        self.assertEqual(payload["stale"], "OwnershipLost")
        self.assertEqual(payload["stale_status"], "QUEUED")
        self.assertEqual(payload["mismatch"], "SlotMismatch")
        self.assertEqual(payload["mismatch_status"], "QUEUED")
        self.assertEqual(payload["running"], "NotQueued")
        self.assertEqual(payload["terminal"], "NotQueued")
        self.assertEqual(payload["terminal_status"], "SUCCEEDED")

    def test_invalid_categories_fail_before_the_transaction(self):
        result = _run(
            """\
            identifier = uuid.UUID("44444444444444444444444444444444")
            queued(identifier)
            clock = Clock(10)
            repository = SqliteConversationRepository(clock, RUNNER)
            rejected = []
            for category in (
                "context_unavailable",
                StoredFailureCategory.CONTEXT_UNAVAILABLE,
                DefiniteProviderCategory.PROVIDER_DEFINITE_FAILURE,
                UncertainProviderCategory.PROVIDER_OUTCOME_UNCERTAIN,
                FailureClassification.PERSISTENCE_FAILED,
                FailureClassification.UNCERTAIN_PROVIDER,
            ):
                try:
                    repository.fail_pre_provider(token(identifier), category)
                except PreProviderInputError as error:
                    rejected.append(error.code)
                else:
                    rejected.append("accepted")
            print(json.dumps({
                "rejected": rejected,
                "calls": clock.calls,
                "status": state(identifier)["status"],
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["rejected"], ["failure_category_invalid"] * 6)
        self.assertEqual(payload["calls"], 0)
        self.assertEqual(payload["status"], "QUEUED")

    def test_zero_row_and_partial_write_roll_back(self):
        result = _run(
            """\
            identifier = uuid.UUID("55555555555555555555555555555555")
            queued(identifier)
            repository = SqliteConversationRepository(Clock(10), RUNNER)
            supplied = token(identifier)
            category = PreProviderFailureCategory.CONTEXT_UNAVAILABLE
            real = QuerySet.update
            def zero_operation(self, **kwargs):
                if self.model is Operation and kwargs.get("status") == "FAILED":
                    return 0
                return real(self, **kwargs)
            QuerySet.update = zero_operation
            try:
                zero = repository.fail_pre_provider(supplied, category)
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
                    repository.fail_pre_provider(supplied, category)
                except RuntimeError as error:
                    failure = str(error)
                else:
                    failure = "accepted"
            finally:
                QuerySet.update = real
            print(json.dumps({
                "zero": type(zero).__name__,
                "zero_status": zero_state["status"],
                "zero_slot": zero_state["slot_operation"],
                "failure": failure,
                "failure_state": state(identifier),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["zero"], "OwnershipLost")
        self.assertEqual(payload["zero_status"], "QUEUED")
        self.assertEqual(
            payload["zero_slot"],
            "55555555-5555-5555-5555-555555555555",
        )
        self.assertEqual(payload["failure"], "injected slot failure")
        self.assertEqual(payload["failure_state"]["status"], "QUEUED")
        self.assertEqual(
            payload["failure_state"]["slot_operation"],
            "55555555-5555-5555-5555-555555555555",
        )
        self.assertIsNone(payload["failure_state"]["finished"])

    def test_contention_and_commit_ambiguity_preserve_identifiers(self):
        result = _run(
            """\
            identifier = uuid.UUID("66666666666666666666666666666666")
            queued(identifier)
            clock = Clock(77)
            repository = SqliteConversationRepository(clock, RUNNER)
            holder = sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True)
            try:
                holder.execute("BEGIN IMMEDIATE")
                busy = repository.fail_pre_provider(
                    token(identifier),
                    PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
                )
            finally:
                holder.rollback()
                holder.close()
            busy_calls = clock.calls
            real_commit = connection.commit
            def fail_commit(*args, **kwargs):
                raise OperationalError("injected commit uncertainty")
            connection.commit = fail_commit
            try:
                ambiguous = repository.fail_pre_provider(
                    token(identifier),
                    PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
                )
            finally:
                connection.commit = real_commit
            connection.close()
            print(json.dumps({
                "busy": type(busy).__name__,
                "busy_calls": busy_calls,
                "ambiguous": type(ambiguous).__name__,
                "operation_id": str(ambiguous.operation_id),
                "runner": ambiguous.runner_id,
                "conversation_id": str(ambiguous.conversation_id),
                "claimed_at": ambiguous.claimed_at,
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
            "66666666-6666-6666-6666-666666666666",
        )
        self.assertEqual(payload["runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(
            payload["conversation_id"],
            "00112233-4455-6677-8899-aabbccddeeff",
        )
        self.assertIsNone(payload["claimed_at"])
        self.assertEqual(payload["status"], "QUEUED")

    def test_frozen_schema_inventory_and_no_provider_execution(self):
        sys.path.insert(0, str(SRC_ROOT))
        from max_smart_db.sql_policy import scan_migration_package

        inventory = scan_migration_package()
        self.assertEqual(sum(len(v) for v in inventory.forward_sql.values()), 40)
        self.assertEqual(sum(len(v) for v in inventory.reverse_sql.values()), 40)
        source = (SRC_ROOT / "max_smart_db" / "pre_provider.py").read_text()
        self.assertNotIn("responses.create", source)
        self.assertNotIn("openai", source)


if __name__ == "__main__":
    unittest.main()
