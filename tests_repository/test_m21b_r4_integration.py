"""Disposable cross-method verification of the accepted repository lifecycle."""

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
import base64
import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path

database = Path(os.environ["MAX_SMART_TEST_DATABASE_PATH"])
from max_smart_db.safety import initialize_sqlite_file
initialize_sqlite_file(database)

import django
django.setup()
from django.contrib.auth.models import User
from django.db import IntegrityError, OperationalError, connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models.query import QuerySet
from max_smart_db.config import (
    EXECUTION_DEADLINE_US,
    FIXED_LEASE_US,
    IDEMPOTENCY_EXPIRATION_US,
    QUEUED_RESERVATION_US,
    RECOVERY_CLOCK_SKEW_MARGIN_US,
)
from max_smart_db.creation import SqliteConversationRepository
from max_smart_db.models import (
    Conversation,
    GlobalModelSlot,
    IdempotencyKey,
    Message,
    Operation,
)
from max_smart_db.repository import (
    DefiniteProviderCategory,
    PreProviderFailureCategory,
    PrepareCommand,
    UncertainProviderCategory,
)
from max_smart_db.sql_policy import scan_migration_package
from max_smart_db.startup import StartupIntegrityError, verify_ordinary_startup

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

OWNER = User.objects.create(username="owner")
OTHER = User.objects.create(username="other")
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
FORMER = "BBBBBBBBBBBBBBBBBBBBBQ"


class Clock:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def now_us(self):
        self.calls += 1
        return self.value


def key_for(value):
    return base64.urlsafe_b64encode(bytes([value]) * 16).rstrip(b"=").decode()


def repository(clock, runner=RUNNER):
    return SqliteConversationRepository(clock, runner)


def command(actor, conversation_id, content, key, expected=0):
    return PrepareCommand(
        actor_id=actor,
        conversation_id=conversation_id,
        expected_version=expected,
        request_content=content,
        idempotency_key=key,
        provider_id="openai",
        model_id="gpt-5.6-sol",
    )


def open_conversation(repo):
    created = repo.create_conversation(OWNER.id, "max-smart", None)
    return created.conversation_id


def slot():
    row = GlobalModelSlot.objects.get(pk=1)
    return {
        "operation": None if row.operation_id is None else str(row.operation_id),
        "runner": row.runner_id,
        "reservation": row.reservation_expires_at_us,
        "lease": row.lease_expires_at_us,
    }


def operation_state(operation_id):
    row = Operation.objects.get(pk=operation_id)
    conversation = Conversation.objects.get(pk=row.conversation_id)
    messages = list(
        Message.objects.filter(operation_id=operation_id)
        .order_by("sequence")
        .values_list("sequence", "role", "content")
    )
    return {
        "status": row.status,
        "blocks": row.blocks_conversation,
        "runner": row.runner_id,
        "started": row.provider_started_at_us,
        "deadline": row.execution_deadline_us,
        "lease": row.lease_expires_at_us,
        "finished": row.finished_at_us,
        "category": row.failure_category,
        "acknowledged": row.acknowledged_at_us,
        "version": conversation.version,
        "next_sequence": conversation.next_sequence,
        "messages": messages,
    }
"""


def _run(body, *, timeout=90):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "r4.sqlite3"
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


class IntegrationTests(unittest.TestCase):
    def test_success_history_duplicate_timing_and_startup(self):
        result = _run(
            """\
            clock = Clock(1_000_000)
            repo = repository(clock)
            clock.value = 1_000_000
            conversation_id = open_conversation(repo)
            clock.value = 2_000_000
            prepared = repo.prepare(command(
                OWNER.id,
                conversation_id,
                "hello request",
                key_for(1),
            ))
            clock.value = 3_000_000
            claimed = repo.claim_running(OWNER.id, prepared.reservation.operation_id)
            updates = []
            real_update = QuerySet.update
            def recording_update(self, **kwargs):
                count = real_update(self, **kwargs)
                updates.append([self.model.__name__, count])
                return count
            QuerySet.update = recording_update
            clock.value = 4_000_000
            try:
                applied = repo.apply_success(
                    claimed.claimed_operation,
                    "hello assistant",
                )
            finally:
                QuerySet.update = real_update
            duplicate = repo.apply_success(
                claimed.claimed_operation,
                "replacement assistant",
            )
            snapshot = repo.load_snapshot(OWNER.id, conversation_id)
            hidden = repo.load_snapshot(OTHER.id, conversation_id)
            stale = repo.prepare(command(
                OWNER.id,
                conversation_id,
                "later request",
                key_for(2),
                expected=0,
            ))
            inventory = scan_migration_package()
            startup = verify_ordinary_startup(connection, inventory)
            orphan_id = uuid.uuid4()
            Operation.objects.create(
                id=orphan_id,
                conversation_id=conversation_id,
                owner_id=OWNER.id,
                operation_type="message",
                provider_id="openai",
                model_id="gpt-5.6-sol",
                status="SUCCEEDED",
                blocks_conversation=0,
                expected_version=0,
                request_content="orphan",
                fingerprint_version=1,
                request_hash="ab" * 32,
                retry_of=None,
                runner_id=None,
                reservation_expires_at_us=None,
                provider_started_at_us=1,
                execution_deadline_us=2,
                lease_expires_at_us=3,
                created_at_us=1,
                finished_at_us=4,
                acknowledged_at_us=None,
                failure_category=None,
            )
            try:
                verify_ordinary_startup(connection, inventory)
                scan = "accepted"
            except StartupIntegrityError as error:
                scan = error.code
            try:
                Operation.objects.filter(
                    pk=prepared.reservation.operation_id,
                ).update(runner_id=RUNNER)
                shape = "accepted"
            except IntegrityError:
                shape = "rejected"
            good = operation_state(prepared.reservation.operation_id)
            print(json.dumps({
                "prepared": type(prepared).__name__,
                "reservation": prepared.reservation.reservation_expires_at,
                "claimed": type(claimed).__name__,
                "deadline": claimed.claimed_operation.execution_deadline,
                "lease": claimed.claimed_operation.lease_expires_at,
                "applied": type(applied).__name__,
                "updates": updates,
                "duplicate": type(duplicate).__name__,
                "duplicate_user": duplicate.user_message.content,
                "duplicate_assistant": duplicate.assistant_message.content,
                "version": snapshot.conversation.version,
                "next_sequence": snapshot.conversation.next_sequence,
                "messages": [
                    [message.sequence, message.role.value, message.content]
                    for message in snapshot.messages
                ],
                "hidden": type(hidden).__name__,
                "stale": type(stale).__name__,
                "stale_actual": stale.actual,
                "operations": Operation.objects.count(),
                "idempotency_expires": IdempotencyKey.objects.get(
                    operation_id=prepared.reservation.operation_id,
                ).expires_at_us,
                "boundary": startup.boundary,
                "scans": list(startup.scans),
                "forward": sum(len(v) for v in inventory.forward_sql.values()),
                "reverse": sum(len(v) for v in inventory.reverse_sql.values()),
                "seeds": len(inventory.seed_sql),
                "scan": scan,
                "shape": shape,
                "orphan_messages": Message.objects.filter(
                    operation_id=orphan_id,
                ).count(),
                "orphan_status": Operation.objects.get(pk=orphan_id).status,
                "good": good,
                "slot": slot(),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["prepared"], "Prepared")
        self.assertEqual(payload["reservation"], 2_000_000 + 60_000_000)
        self.assertEqual(payload["claimed"], "Claimed")
        self.assertEqual(payload["deadline"], 3_000_000 + 600_000_000)
        self.assertEqual(payload["lease"], 3_000_000 + 720_000_000)
        self.assertEqual(payload["lease"] - payload["deadline"], 120_000_000)
        self.assertEqual(payload["applied"], "Applied")
        self.assertEqual(
            payload["updates"],
            [
                ["Conversation", 1],
                ["Operation", 1],
                ["GlobalModelSlot", 1],
            ],
        )
        self.assertEqual(payload["duplicate"], "AlreadySucceeded")
        self.assertEqual(payload["duplicate_user"], "hello request")
        self.assertEqual(payload["duplicate_assistant"], "hello assistant")
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["next_sequence"], 3)
        self.assertEqual(
            payload["messages"],
            [
                [1, "user", "hello request"],
                [2, "assistant", "hello assistant"],
            ],
        )
        self.assertEqual(payload["hidden"], "NotFound")
        self.assertEqual(payload["stale"], "StaleVersion")
        self.assertEqual(payload["stale_actual"], 1)
        self.assertEqual(payload["operations"], 2)
        self.assertEqual(
            payload["idempotency_expires"],
            2_000_000 + 2_592_000_000_000,
        )
        self.assertEqual(payload["boundary"], "B3")
        self.assertEqual(
            payload["scans"],
            [
                "slot_correspondence",
                "operation_owner",
                "idempotency_binding",
                "message_status",
                "retry_eligibility",
            ],
        )
        self.assertEqual(payload["forward"], 40)
        self.assertEqual(payload["reverse"], 40)
        self.assertEqual(payload["seeds"], 4)
        self.assertEqual(payload["scan"], "message_status_failed")
        self.assertEqual(payload["shape"], "rejected")
        self.assertEqual(payload["orphan_messages"], 0)
        self.assertEqual(payload["orphan_status"], "SUCCEEDED")
        self.assertEqual(payload["good"]["status"], "SUCCEEDED")
        self.assertEqual(payload["good"]["messages"], [
            [1, "user", "hello request"],
            [2, "assistant", "hello assistant"],
        ])
        self.assertEqual(payload["good"]["version"], 1)
        self.assertIsNone(payload["good"]["runner"])
        self.assertIsNone(payload["slot"]["operation"])

    def test_idempotency_adoption_expiry_and_pre_provider(self):
        result = _run(
            """\
            clock = Clock(1_000)
            original = repository(clock, RUNNER)
            adopted = repository(clock, FORMER)
            conversation_id = open_conversation(original)
            first = original.prepare(command(
                OWNER.id,
                conversation_id,
                "bound request",
                key_for(3),
            ))
            repeated = original.prepare(command(
                OWNER.id,
                conversation_id,
                "bound request",
                key_for(3),
            ))
            mismatch = original.prepare(command(
                OWNER.id,
                conversation_id,
                "different request",
                key_for(3),
            ))
            first_id = first.reservation.operation_id
            expires_at = IdempotencyKey.objects.get(
                operation_id=first_id,
            ).expires_at_us
            clock.value = expires_at
            repeated_after = original.prepare(command(
                OWNER.id,
                conversation_id,
                "bound request",
                key_for(3),
            ))
            mismatch_after = original.prepare(command(
                OWNER.id,
                conversation_id,
                "third request",
                key_for(3),
            ))
            recovered = original.recover_slot()
            keys_after_expiry = list(
                IdempotencyKey.objects.order_by("id").values_list(
                    "operation_id",
                    "expires_at_us",
                )
            )
            clock.value = expires_at + 10
            boundary_id = open_conversation(original)
            boundary = original.prepare(command(
                OWNER.id,
                boundary_id,
                "boundary request",
                key_for(4),
            ))
            reservation_at = boundary.reservation.reservation_expires_at
            clock.value = reservation_at - 1
            live = original.recover_slot()
            clock.value = reservation_at
            expired = original.recover_slot()
            clock.value = reservation_at + 50
            adopted_id = open_conversation(original)
            queued = original.prepare(command(
                OWNER.id,
                adopted_id,
                "adopt me",
                key_for(5),
            ))
            adoption = adopted.adopt_queued(
                OWNER.id,
                queued.reservation.operation_id,
            )
            former_claim = original.claim_running(
                OWNER.id,
                queued.reservation.operation_id,
            )
            former_failure = original.fail_pre_provider(
                queued.reservation,
                PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
            )
            adopted_state = operation_state(queued.reservation.operation_id)
            adopted_slot = slot()
            clock.value = queued.reservation.reservation_expires_at
            released = original.recover_slot()
            clock.value = queued.reservation.reservation_expires_at + 20
            failed_id = open_conversation(original)
            queued_failure = original.prepare(command(
                OWNER.id,
                failed_id,
                "fail before provider",
                key_for(6),
            ))
            failed = original.fail_pre_provider(
                queued_failure.reservation,
                PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
            )
            failure_state = operation_state(
                queued_failure.reservation.operation_id,
            )
            print(json.dumps({
                "repeated": type(repeated).__name__,
                "same_operation": repeated.operation.operation_id == first_id,
                "mismatch": type(mismatch).__name__,
                "repeated_after": type(repeated_after).__name__,
                "same_after": repeated_after.operation.operation_id == first_id,
                "mismatch_after": type(mismatch_after).__name__,
                "recovered": type(recovered).__name__,
                "keys": [
                    [str(operation_id), expires]
                    for operation_id, expires in keys_after_expiry
                ],
                "expires_at": expires_at,
                "live": type(live).__name__,
                "expired": type(expired).__name__,
                "expired_state": operation_state(boundary.reservation.operation_id),
                "expired_slot": slot(),
                "adoption": type(adoption).__name__,
                "adopted_runner": adoption.reservation.runner_id,
                "former_claim": type(former_claim).__name__,
                "former_failure": type(former_failure).__name__,
                "adopted_state": adopted_state,
                "adopted_slot": adopted_slot,
                "released": type(released).__name__,
                "failed": type(failed).__name__,
                "failure_state": failure_state,
                "failure_slot": slot(),
                "reservation_delta": reservation_at - (expires_at + 10),
                "key_count": IdempotencyKey.objects.count(),
                "cleanup_method": hasattr(
                    SqliteConversationRepository,
                    "delete_expired_idempotency",
                ),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["repeated"], "Existing")
        self.assertTrue(payload["same_operation"])
        self.assertEqual(payload["mismatch"], "KeyBindingMismatch")
        self.assertEqual(payload["repeated_after"], "Existing")
        self.assertTrue(payload["same_after"])
        self.assertEqual(payload["mismatch_after"], "KeyBindingMismatch")
        self.assertEqual(payload["recovered"], "CancelledExpiredQueued")
        self.assertEqual(len(payload["keys"]), 1)
        self.assertEqual(payload["keys"][0][1], payload["expires_at"])
        self.assertEqual(payload["live"], "LiveQueued")
        self.assertEqual(payload["expired"], "CancelledExpiredQueued")
        self.assertEqual(payload["expired_state"]["status"], "CANCELLED")
        self.assertEqual(payload["expired_state"]["blocks"], 0)
        self.assertIsNone(payload["expired_slot"]["operation"])
        self.assertEqual(payload["reservation_delta"], 60_000_000)
        self.assertEqual(payload["adoption"], "Adopted")
        self.assertEqual(payload["adopted_runner"], "BBBBBBBBBBBBBBBBBBBBBQ")
        self.assertEqual(payload["former_claim"], "OwnershipLost")
        self.assertEqual(payload["former_failure"], "OwnershipLost")
        self.assertEqual(payload["adopted_state"]["status"], "QUEUED")
        self.assertEqual(
            payload["adopted_state"]["runner"],
            "BBBBBBBBBBBBBBBBBBBBBQ",
        )
        self.assertEqual(
            payload["adopted_slot"]["runner"],
            "BBBBBBBBBBBBBBBBBBBBBQ",
        )
        self.assertEqual(payload["released"], "CancelledExpiredQueued")
        self.assertEqual(payload["failed"], "Failed")
        self.assertEqual(payload["failure_state"]["status"], "FAILED")
        self.assertEqual(payload["failure_state"]["blocks"], 0)
        self.assertIsNone(payload["failure_state"]["runner"])
        self.assertIsNone(payload["failure_state"]["started"])
        self.assertEqual(
            payload["failure_state"]["category"],
            "context_unavailable",
        )
        self.assertEqual(payload["failure_state"]["version"], 0)
        self.assertIsNone(payload["failure_slot"]["operation"])
        self.assertEqual(payload["key_count"], 4)
        self.assertFalse(payload["cleanup_method"])

    def test_provider_recovery_late_success_and_acknowledgement(self):
        result = _run(
            """\
            clock = Clock(10_000)
            repo = repository(clock)
            definite_conversation = open_conversation(repo)
            definite_prepared = repo.prepare(command(
                OWNER.id,
                definite_conversation,
                "definite request",
                key_for(7),
            ))
            definite_claimed = repo.claim_running(
                OWNER.id,
                definite_prepared.reservation.operation_id,
            )
            clock.value = definite_claimed.claimed_operation.claimed_at + 5
            definite = repo.apply_failure(
                definite_claimed.claimed_operation,
                DefiniteProviderCategory.PROVIDER_DEFINITE_FAILURE,
            )
            definite_state = operation_state(
                definite_claimed.claimed_operation.operation_id,
            )
            definite_slot = slot()
            clock.value = 20_000
            unknown_conversation = open_conversation(repo)
            unknown_prepared = repo.prepare(command(
                OWNER.id,
                unknown_conversation,
                "unknown request",
                key_for(8),
            ))
            unknown_claimed = repo.claim_running(
                OWNER.id,
                unknown_prepared.reservation.operation_id,
            )
            token = unknown_claimed.claimed_operation
            fingerprint = repo.apply_success(
                type(token)(
                    operation_id=token.operation_id,
                    actor_id=token.actor_id,
                    conversation_id=token.conversation_id,
                    request_hash="cd" * 32,
                    fingerprint_version=token.fingerprint_version,
                    expected_version=token.expected_version,
                    runner_id=token.runner_id,
                    claimed_at=token.claimed_at,
                    execution_deadline=token.execution_deadline,
                    lease_expires_at=token.lease_expires_at,
                ),
                "should not persist",
            )
            fingerprint_messages = Message.objects.filter(
                operation_id=token.operation_id,
            ).count()
            uncertain = repo.apply_failure(
                token,
                UncertainProviderCategory.PROVIDER_OUTCOME_UNCERTAIN,
            )
            clock.value = token.execution_deadline
            before_lease = repo.recover_slot()
            clock.value = token.lease_expires_at
            recovered = repo.recover_slot()
            recovered_state = operation_state(token.operation_id)
            recovered_slot = slot()
            clock.value = 30_000
            acknowledged_conversation = open_conversation(repo)
            acknowledged_prepared = repo.prepare(command(
                OWNER.id,
                acknowledged_conversation,
                "acknowledge request",
                key_for(9),
            ))
            acknowledged_claimed = repo.claim_running(
                OWNER.id,
                acknowledged_prepared.reservation.operation_id,
            )
            acknowledged_token = acknowledged_claimed.claimed_operation
            repo.apply_failure(
                acknowledged_token,
                UncertainProviderCategory.PROVIDER_OUTCOME_UNCERTAIN,
            )
            clock.value = acknowledged_token.lease_expires_at
            repo.recover_slot()
            acknowledged = repo.acknowledge_unknown(
                OWNER.id,
                acknowledged_token.operation_id,
            )
            from max_smart_db.repository import RetainedResult
            discarded = repo.reconcile_late_success(RetainedResult(
                claimed_operation=acknowledged_token,
                output_text="too late",
            ))
            acknowledged_state = operation_state(
                acknowledged_token.operation_id,
            )
            clock.value = 40_000
            occupant_conversation = open_conversation(repo)
            occupant_prepared = repo.prepare(command(
                OWNER.id,
                occupant_conversation,
                "occupant request",
                key_for(10),
            ))
            occupant_claimed = repo.claim_running(
                OWNER.id,
                occupant_prepared.reservation.operation_id,
            )
            occupant_before = slot()
            reconciled = repo.reconcile_late_success(RetainedResult(
                claimed_operation=token,
                output_text="retained assistant",
            ))
            print(json.dumps({
                "definite": type(definite).__name__,
                "definite_state": definite_state,
                "definite_slot": definite_slot,
                "definite_claimed_at": definite_claimed.claimed_operation.claimed_at,
                "fingerprint": type(fingerprint).__name__,
                "fingerprint_messages": fingerprint_messages,
                "uncertain": type(uncertain).__name__,
                "recovery_at": uncertain.recovery_at,
                "lease": token.lease_expires_at,
                "deadline": token.execution_deadline,
                "before_lease": type(before_lease).__name__,
                "recovered": type(recovered).__name__,
                "recovered_state": recovered_state,
                "recovered_slot": recovered_slot,
                "acknowledged": type(acknowledged).__name__,
                "discarded": type(discarded).__name__,
                "acknowledged_state": acknowledged_state,
                "occupant_before": occupant_before,
                "reconciled": type(reconciled).__name__,
                "late_state": operation_state(token.operation_id),
                "occupant_after": slot(),
                "occupant_status": Operation.objects.get(
                    pk=occupant_claimed.claimed_operation.operation_id,
                ).status,
            }))
            """
        )
        payload = _payload(self, result)
        definite = payload["definite_state"]
        self.assertEqual(payload["definite"], "DefinitelyFailed")
        self.assertEqual(definite["status"], "FAILED")
        self.assertEqual(definite["blocks"], 0)
        self.assertIsNone(definite["runner"])
        claimed_at = payload["definite_claimed_at"]
        self.assertEqual(definite["started"], claimed_at)
        self.assertEqual(definite["deadline"], claimed_at + 600_000_000)
        self.assertEqual(definite["lease"], claimed_at + 720_000_000)
        self.assertIsNone(payload["definite_slot"]["operation"])
        self.assertEqual(definite["category"], "provider_definite_failure")
        self.assertEqual(definite["version"], 0)
        self.assertEqual(definite["messages"], [])
        self.assertEqual(payload["fingerprint"], "OwnershipLost")
        self.assertEqual(payload["fingerprint_messages"], 0)
        self.assertEqual(payload["uncertain"], "UncertainRecorded")
        self.assertEqual(payload["recovery_at"], payload["lease"])
        self.assertEqual(payload["deadline"] < payload["lease"], True)
        self.assertEqual(payload["before_lease"], "LiveRunning")
        self.assertEqual(payload["recovered"], "MarkedOutcomeUnknown")
        self.assertIsNone(payload["recovered_slot"]["operation"])
        self.assertEqual(payload["recovered_state"]["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(payload["recovered_state"]["blocks"], 1)
        self.assertEqual(
            payload["recovered_state"]["category"],
            "provider_outcome_uncertain",
        )
        self.assertEqual(
            payload["recovered_state"]["finished"],
            payload["lease"],
        )
        self.assertEqual(payload["acknowledged"], "Acknowledged")
        self.assertEqual(payload["discarded"], "AcknowledgedDiscard")
        acknowledged = payload["acknowledged_state"]
        self.assertEqual(acknowledged["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(acknowledged["blocks"], 0)
        self.assertEqual(acknowledged["category"], "provider_outcome_uncertain")
        self.assertEqual(acknowledged["messages"], [])
        self.assertEqual(acknowledged["version"], 0)
        self.assertIsNotNone(acknowledged["acknowledged"])
        self.assertEqual(payload["reconciled"], "Reconciled")
        late = payload["late_state"]
        self.assertEqual(late["status"], "SUCCEEDED")
        self.assertEqual(late["version"], 1)
        self.assertEqual(late["next_sequence"], 3)
        self.assertEqual(
            late["messages"],
            [
                [1, "user", "unknown request"],
                [2, "assistant", "retained assistant"],
            ],
        )
        self.assertEqual(late["category"], "provider_outcome_uncertain")
        self.assertEqual(payload["occupant_after"], payload["occupant_before"])
        self.assertEqual(payload["occupant_status"], "RUNNING")

    def test_fences_rollback_contention_and_ambiguous_commit(self):
        result = _run(
            """\
            from django.db import close_old_connections

            clock = Clock(50_000)
            repo = repository(clock)
            conversation_id = open_conversation(repo)
            holder = sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True)
            try:
                holder.execute("BEGIN IMMEDIATE")
                busy = repo.prepare(command(
                    OWNER.id,
                    conversation_id,
                    "busy request",
                    key_for(11),
                ))
                busy_calls = clock.calls
            finally:
                holder.rollback()
                holder.close()
            prepared = repo.prepare(command(
                OWNER.id,
                conversation_id,
                "rollback request",
                key_for(12),
            ))
            claimed = repo.claim_running(
                OWNER.id,
                prepared.reservation.operation_id,
            )
            before_apply = operation_state(prepared.reservation.operation_id)
            before_slot = slot()
            real_create = Message.objects.create
            def fail_create(*args, **kwargs):
                raise RuntimeError("message boundary")
            Message.objects.create = fail_create
            try:
                repo.apply_success(claimed.claimed_operation, "partial")
                partial = "accepted"
            except RuntimeError as error:
                partial = str(error)
            finally:
                Message.objects.create = real_create
            after_partial = operation_state(prepared.reservation.operation_id)
            partial_slot = slot()
            real_commit = connection.commit
            def fail_commit(*args, **kwargs):
                raise OperationalError("injected commit uncertainty")
            connection.commit = fail_commit
            try:
                ambiguous = repo.apply_success(
                    claimed.claimed_operation,
                    "ambiguous",
                )
            finally:
                connection.commit = real_commit
            connection.close()
            after_ambiguous = operation_state(prepared.reservation.operation_id)
            resolved = repo.apply_success(
                claimed.claimed_operation,
                "resolved assistant",
            )
            resolved_state = operation_state(prepared.reservation.operation_id)
            wrong = repo.prepare(command(
                OTHER.id,
                conversation_id,
                "foreign request",
                key_for(13),
            ))
            queued_conversation = open_conversation(repo)
            queued = repo.prepare(command(
                OWNER.id,
                queued_conversation,
                "claim rollback",
                key_for(14),
            ))
            real_update = QuerySet.update
            def fail_slot(self, **kwargs):
                if self.model is GlobalModelSlot:
                    raise RuntimeError("slot boundary")
                return real_update(self, **kwargs)
            QuerySet.update = fail_slot
            try:
                repo.claim_running(OWNER.id, queued.reservation.operation_id)
                claim_failure = "accepted"
            except RuntimeError as error:
                claim_failure = str(error)
            finally:
                QuerySet.update = real_update
            claim_state = operation_state(queued.reservation.operation_id)
            released = repo.fail_pre_provider(
                queued.reservation,
                PreProviderFailureCategory.MODEL_CONFIGURATION_INVALID,
            )
            left = open_conversation(repo)
            right = open_conversation(repo)
            barrier = threading.Barrier(2)
            outcomes = []
            failures = []
            def worker(conversation, key_byte):
                close_old_connections()
                worker_clock = Clock(80_000)
                worker_repo = repository(worker_clock)
                barrier.wait()
                try:
                    outcome = worker_repo.prepare(command(
                        OWNER.id,
                        conversation,
                        "competitor",
                        key_for(key_byte),
                    ))
                    outcomes.append(type(outcome).__name__)
                except Exception as error:
                    failures.append(type(error).__name__)
                finally:
                    close_old_connections()
            threads = [
                threading.Thread(target=worker, args=(left, 15)),
                threading.Thread(target=worker, args=(right, 16)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            connection.close()
            print(json.dumps({
                "busy": type(busy).__name__,
                "busy_calls": busy_calls,
                "before_apply": before_apply["status"],
                "before_slot": before_slot["operation"],
                "partial": partial,
                "after_partial": after_partial,
                "partial_slot": partial_slot["operation"],
                "ambiguous": type(ambiguous).__name__,
                "ambiguous_operation": str(ambiguous.operation_id),
                "after_ambiguous": after_ambiguous["status"],
                "ambiguous_messages": after_ambiguous["messages"],
                "resolved": type(resolved).__name__,
                "resolved_version": resolved_state["version"],
                "resolved_messages": resolved_state["messages"],
                "wrong": type(wrong).__name__,
                "claim_failure": claim_failure,
                "claim_state": claim_state["status"],
                "claim_slot": slot()["operation"] if False else str(
                    GlobalModelSlot.objects.get(pk=1).operation_id
                ) if GlobalModelSlot.objects.get(pk=1).operation_id else None,
                "released": type(released).__name__,
                "outcomes": sorted(outcomes),
                "failures": failures,
                "live_operations": Operation.objects.filter(
                    status__in=["QUEUED", "RUNNING"],
                ).count(),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["busy"], "DatabaseBusy")
        self.assertEqual(payload["busy_calls"], 1)
        self.assertEqual(payload["before_apply"], "RUNNING")
        self.assertEqual(payload["partial"], "message boundary")
        self.assertEqual(payload["after_partial"]["status"], "RUNNING")
        self.assertEqual(payload["after_partial"]["messages"], [])
        self.assertEqual(payload["after_partial"]["version"], 0)
        self.assertEqual(
            payload["partial_slot"],
            payload["before_slot"],
        )
        self.assertEqual(payload["ambiguous"], "CommitAmbiguous")
        self.assertEqual(payload["after_ambiguous"], "RUNNING")
        self.assertEqual(payload["ambiguous_messages"], [])
        self.assertEqual(payload["resolved"], "Applied")
        self.assertEqual(payload["resolved_version"], 1)
        self.assertEqual(
            payload["resolved_messages"],
            [
                [1, "user", "rollback request"],
                [2, "assistant", "resolved assistant"],
            ],
        )
        self.assertEqual(payload["wrong"], "NotFound")
        self.assertEqual(payload["claim_failure"], "slot boundary")
        self.assertEqual(payload["claim_state"], "QUEUED")
        self.assertEqual(payload["released"], "Failed")
        self.assertEqual(payload["outcomes"], ["ModelBusy", "Prepared"])
        self.assertEqual(payload["failures"], [])
        self.assertEqual(payload["live_operations"], 1)


if __name__ == "__main__":
    unittest.main()
