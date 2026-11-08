"""Disposable-database tests for restart classification and explicit recovery."""

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
PRODUCTION_DATABASE = PROJECT_ROOT / "data" / "max_smart.sqlite3"
SERVICE_SOURCE = SRC_ROOT / "conversation_service.py"

_PREAMBLE = """\
import base64
import json
import os
import sqlite3
import uuid
from pathlib import Path

database = Path(os.environ["MAX_SMART_TEST_DATABASE_PATH"])
if database.name == "max_smart.sqlite3":
    raise SystemExit("production database requested")
from max_smart_db.safety import initialize_sqlite_file
initialize_sqlite_file(database)

import django
django.setup()
from django.contrib.auth.models import User
from django.db import OperationalError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from conversation_service import ConversationService, TurnRequest
from max_smart_db.creation import SqliteConversationRepository
from max_smart_db.models import GlobalModelSlot, IdempotencyKey, Message, Operation
from max_smart_db.repository import PrepareCommand

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

OWNER = User.objects.create(username="owner")
OTHER = User.objects.create(username="other")
CONVERSATION = uuid.UUID("00112233445566778899aabbccddeeff")
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
NEW = "BBBBBBBBBBBBBBBBBBBBBQ"
PREPARED_AT = 1_000_000

from max_smart_db.models import Conversation
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


def key_for(value):
    return base64.urlsafe_b64encode(bytes([value]) * 16).rstrip(b"=").decode()


def repository(now=PREPARED_AT, runner=RUNNER):
    clock = Clock(now)
    return SqliteConversationRepository(clock, runner), clock


def turn(key, runner=RUNNER, adopt=None, content="hello", expected=0):
    return TurnRequest(
        actor_id=OWNER.id,
        conversation_id=CONVERSATION,
        request_content=content,
        idempotency_key=key_for(key),
        runner_id=runner,
        expected_version=expected,
        provider_id="openai",
        model_id="gpt-5.6-sol",
        adopt_operation_id=adopt,
    )


def watched(repo, loader=None):
    counts = {"provider": 0, "claim": 0, "adopt": 0, "recover": 0}
    real_claim = repo.claim_running
    real_adopt = repo.adopt_queued
    real_recover = repo.recover_slot

    def claim_running(*args, **kwargs):
        counts["claim"] += 1
        return real_claim(*args, **kwargs)

    def adopt_queued(*args, **kwargs):
        counts["adopt"] += 1
        result = real_adopt(*args, **kwargs)
        reservation = getattr(result, "reservation", None)
        if reservation is not None:
            counts["reservation"] = reservation.reservation_expires_at
            counts["adopted_runner"] = reservation.runner_id
        return result

    def recover_slot():
        counts["recover"] += 1
        return real_recover()

    repo.claim_running = claim_running
    repo.adopt_queued = adopt_queued
    repo.recover_slot = recover_slot

    def provide(**kwargs):
        counts["provider"] += 1
        counts["provider_operation"] = str(kwargs["operation_id"])
        return "model-output"

    def load_context(**kwargs):
        if loader is not None:
            return loader(**kwargs)
        return "standing"

    return ConversationService(repo, load_context, provide), counts


def prepare_queued(repo, key, content="hello", expected=0):
    prepared = repo.prepare(PrepareCommand(
        actor_id=OWNER.id,
        conversation_id=CONVERSATION,
        expected_version=expected,
        request_content=content,
        idempotency_key=key_for(key),
        provider_id="openai",
        model_id="gpt-5.6-sol",
    ))
    if type(prepared).__name__ != "Prepared":
        raise RuntimeError(type(prepared).__name__)
    return prepared.reservation


def facts(operation_id):
    operation = Operation.objects.get(pk=operation_id)
    slot = GlobalModelSlot.objects.get(pk=1)
    return {
        "status": operation.status,
        "runner": operation.runner_id,
        "reservation": operation.reservation_expires_at_us,
        "started": operation.provider_started_at_us,
        "deadline": operation.execution_deadline_us,
        "lease": operation.lease_expires_at_us,
        "blocks": operation.blocks_conversation,
        "acknowledged": operation.acknowledged_at_us is not None,
        "slot_operation": None if slot.operation_id is None else str(slot.operation_id),
        "slot_runner": slot.runner_id,
        "messages": Message.objects.filter(operation_id=operation_id).count(),
        "operations": Operation.objects.count(),
    }


def described(outcome):
    operation = getattr(outcome, "operation", None)
    described_outcome = {"type": type(outcome).__name__}
    if operation is not None:
        described_outcome.update({
            "status": operation.status.value,
            "blocks": operation.blocks_conversation,
            "acknowledged": operation.acknowledged,
            "runner": operation.runner_id,
            "started": operation.provider_started_at,
        })
    failure = getattr(outcome, "failure_category", None)
    if failure is not None:
        described_outcome["category"] = failure.value
    return described_outcome
"""


def _run(body, *, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "m3s4-service.sqlite3"
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
    return json.loads(result.stdout)


class RestartServiceTests(unittest.TestCase):
    def test_unexpired_queued_adoption_preserves_reservation(self):
        payload = _payload(self, _run(
            """\
            repo, clock = repository()
            reservation = prepare_queued(repo, 1)
            expiry = reservation.reservation_expires_at
            restarted, counts = watched(
                SqliteConversationRepository(Clock(PREPARED_AT), NEW)
            )
            classified = restarted.classify_restart(OWNER.id, reservation.operation_id)
            adopted = restarted.adopt_persisted(OWNER.id, reservation.operation_id)
            print(json.dumps({
                "classified": described(classified),
                "adopted": type(adopted).__name__,
                "counts": counts,
                "expiry": expiry,
                "facts": facts(reservation.operation_id),
                "clock": clock.calls,
            }))
            """
        ))
        self.assertEqual(payload["classified"]["type"], "RestartQueued")
        self.assertEqual(payload["classified"]["status"], "QUEUED")
        self.assertEqual(payload["classified"]["runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertTrue(payload["classified"]["blocks"])
        self.assertFalse(payload["classified"]["acknowledged"])
        self.assertIsNone(payload["classified"]["started"])
        self.assertEqual(payload["adopted"], "Adopted")
        self.assertEqual(payload["counts"]["reservation"], payload["expiry"])
        self.assertEqual(payload["counts"]["adopted_runner"], "BBBBBBBBBBBBBBBBBBBBBQ")
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["counts"]["claim"], 0)
        self.assertEqual(payload["counts"]["recover"], 0)
        self.assertEqual(payload["counts"]["adopt"], 1)
        self.assertEqual(payload["facts"]["reservation"], payload["expiry"])
        self.assertEqual(payload["facts"]["runner"], "BBBBBBBBBBBBBBBBBBBBBQ")
        self.assertEqual(payload["facts"]["slot_runner"], "BBBBBBBBBBBBBBBBBBBBBQ")
        self.assertEqual(payload["facts"]["status"], "QUEUED")
        self.assertEqual(payload["clock"], 1)

    def test_original_runner_is_fenced_after_adoption(self):
        payload = _payload(self, _run(
            """\
            repo, clock = repository()
            reservation = prepare_queued(repo, 2)
            restarted, counts = watched(
                SqliteConversationRepository(Clock(PREPARED_AT), NEW)
            )
            restarted.adopt_persisted(OWNER.id, reservation.operation_id)
            original, original_counts = watched(repo)
            replay = original.submit(turn(2))
            fenced = SqliteConversationRepository(
                Clock(PREPARED_AT),
                RUNNER,
            ).claim_running(OWNER.id, reservation.operation_id)
            print(json.dumps({
                "replay": type(replay).__name__,
                "fenced": type(fenced).__name__,
                "counts": counts,
                "original_counts": original_counts,
                "facts": facts(reservation.operation_id),
            }))
            """
        ))
        self.assertEqual(payload["replay"], "Existing")
        self.assertEqual(payload["fenced"], "OwnershipLost")
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["original_counts"]["provider"], 0)
        self.assertEqual(payload["original_counts"]["claim"], 0)
        self.assertEqual(payload["original_counts"]["adopt"], 0)
        self.assertEqual(payload["facts"]["status"], "QUEUED")
        self.assertEqual(payload["facts"]["runner"], "BBBBBBBBBBBBBBBBBBBBBQ")
        self.assertEqual(payload["facts"]["slot_runner"], "BBBBBBBBBBBBBBBBBBBBBQ")

    def test_adopted_queued_operation_claims_once(self):
        payload = _payload(self, _run(
            """\
            repo, clock = repository()
            reservation = prepare_queued(repo, 3)
            expiry = reservation.reservation_expires_at
            restarted_repo = SqliteConversationRepository(Clock(PREPARED_AT), NEW)
            restarted, counts = watched(restarted_repo)
            outcome = restarted.submit(turn(3, runner=NEW, adopt=reservation.operation_id))
            replay_repo = SqliteConversationRepository(Clock(PREPARED_AT), NEW)
            replay_service, replay_counts = watched(replay_repo)
            replay = replay_service.submit(turn(3, runner=NEW))
            print(json.dumps({
                "outcome": described(outcome),
                "operation_id": str(reservation.operation_id),
                "replay": type(replay).__name__,
                "counts": counts,
                "replay_counts": replay_counts,
                "expiry": expiry,
                "facts": facts(reservation.operation_id),
            }))
            """
        ))
        self.assertEqual(payload["outcome"]["type"], "DurableTurnCompleted")
        self.assertEqual(payload["counts"]["provider"], 1)
        self.assertEqual(payload["counts"]["claim"], 1)
        self.assertEqual(payload["counts"]["adopt"], 1)
        self.assertEqual(payload["counts"]["recover"], 0)
        self.assertEqual(payload["counts"]["reservation"], payload["expiry"])
        self.assertEqual(
            payload["counts"]["provider_operation"],
            payload["operation_id"],
        )
        self.assertEqual(payload["replay"], "Existing")
        self.assertEqual(payload["replay_counts"]["provider"], 0)
        self.assertEqual(payload["replay_counts"]["claim"], 0)
        self.assertEqual(payload["facts"]["status"], "SUCCEEDED")
        self.assertEqual(payload["facts"]["messages"], 2)
        self.assertIsNone(payload["facts"]["reservation"])

    def test_expired_queued_reservation_is_not_adopted(self):
        payload = _payload(self, _run(
            """\
            repo, clock = repository()
            reservation = prepare_queued(repo, 4)
            clock.value = reservation.reservation_expires_at
            restarted, counts = watched(
                SqliteConversationRepository(clock, NEW)
            )
            adopted = restarted.adopt_persisted(OWNER.id, reservation.operation_id)
            print(json.dumps({
                "adopted": type(adopted).__name__,
                "counts": counts,
                "facts": facts(reservation.operation_id),
            }))
            """
        ))
        self.assertEqual(payload["adopted"], "ExpiredResubmit")
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["counts"]["claim"], 0)
        self.assertEqual(payload["counts"]["adopt"], 1)
        self.assertEqual(payload["facts"]["status"], "CANCELLED")
        self.assertIsNone(payload["facts"]["runner"])
        self.assertIsNone(payload["facts"]["slot_operation"])

    def test_explicit_recovery_cancels_expired_queued(self):
        payload = _payload(self, _run(
            """\
            repo, clock = repository()
            reservation = prepare_queued(repo, 5)
            clock.value = reservation.reservation_expires_at
            handler, counts = watched(repo)
            recovered = handler.recover()
            print(json.dumps({
                "recovered": type(recovered).__name__,
                "counts": counts,
                "facts": facts(reservation.operation_id),
            }))
            """
        ))
        self.assertEqual(payload["recovered"], "CancelledExpiredQueued")
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["counts"]["claim"], 0)
        self.assertEqual(payload["counts"]["adopt"], 0)
        self.assertEqual(payload["counts"]["recover"], 1)
        self.assertEqual(payload["facts"]["status"], "CANCELLED")
        self.assertIsNone(payload["facts"]["slot_operation"])
        self.assertEqual(payload["facts"]["messages"], 0)

    def test_live_running_claim_is_not_replayed(self):
        payload = _payload(self, _run(
            """\
            repo, clock = repository()
            reservation = prepare_queued(repo, 6)
            claimed = repo.claim_running(OWNER.id, reservation.operation_id)
            restarted, counts = watched(
                SqliteConversationRepository(Clock(PREPARED_AT), NEW)
            )
            classified = restarted.classify_restart(OWNER.id, reservation.operation_id)
            adopted = restarted.adopt_persisted(OWNER.id, reservation.operation_id)
            replay = restarted.submit(turn(6, runner=NEW))
            print(json.dumps({
                "operation_id": str(reservation.operation_id),
                "claimed": type(claimed).__name__,
                "classified": described(classified),
                "adopted": type(adopted).__name__,
                "replay": type(replay).__name__,
                "counts": counts,
                "facts": facts(reservation.operation_id),
            }))
            """
        ))
        self.assertEqual(payload["claimed"], "Claimed")
        self.assertEqual(payload["classified"]["type"], "RestartRunning")
        self.assertEqual(payload["classified"]["status"], "RUNNING")
        self.assertEqual(payload["classified"]["runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertTrue(payload["classified"]["blocks"])
        self.assertFalse(payload["classified"]["acknowledged"])
        self.assertEqual(payload["classified"]["started"], 1_000_000)
        self.assertEqual(payload["adopted"], "NotQueued")
        self.assertEqual(payload["replay"], "Existing")
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["counts"]["claim"], 0)
        self.assertEqual(payload["counts"]["recover"], 0)
        self.assertEqual(payload["facts"]["status"], "RUNNING")
        self.assertEqual(payload["facts"]["runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(payload["facts"]["slot_operation"], payload["operation_id"])
        self.assertEqual(payload["facts"]["messages"], 0)

    def test_recovery_uses_repository_deadline_and_lease(self):
        payload = _payload(self, _run(
            """\
            repo, clock = repository()
            reservation = prepare_queued(repo, 7)
            claimed = repo.claim_running(OWNER.id, reservation.operation_id)
            claimed_operation = claimed.claimed_operation
            handler, counts = watched(repo)
            clock.value = claimed_operation.execution_deadline
            during_deadline = handler.recover()
            during_facts = facts(reservation.operation_id)
            clock.value = claimed_operation.lease_expires_at
            after_lease = handler.recover()
            print(json.dumps({
                "deadline": claimed_operation.execution_deadline,
                "lease": claimed_operation.lease_expires_at,
                "during_deadline": type(during_deadline).__name__,
                "during_facts": during_facts,
                "after_lease": type(after_lease).__name__,
                "after_facts": facts(reservation.operation_id),
                "counts": counts,
            }))
            """
        ))
        self.assertGreater(payload["lease"], payload["deadline"])
        self.assertEqual(payload["during_deadline"], "LiveRunning")
        self.assertEqual(payload["during_facts"]["status"], "RUNNING")
        self.assertEqual(payload["during_facts"]["deadline"], payload["deadline"])
        self.assertEqual(payload["during_facts"]["lease"], payload["lease"])
        self.assertIsNotNone(payload["during_facts"]["slot_operation"])
        self.assertEqual(payload["after_lease"], "MarkedOutcomeUnknown")
        self.assertEqual(payload["after_facts"]["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(payload["after_facts"]["blocks"], 1)
        self.assertFalse(payload["after_facts"]["acknowledged"])
        self.assertIsNone(payload["after_facts"]["slot_operation"])
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["counts"]["recover"], 2)
        self.assertEqual(payload["counts"]["claim"], 0)

    def test_unknown_outcomes_are_not_replayed(self):
        payload = _payload(self, _run(
            """\
            repo, clock = repository()
            reservation = prepare_queued(repo, 8)
            claimed = repo.claim_running(OWNER.id, reservation.operation_id)
            clock.value = claimed.claimed_operation.lease_expires_at
            handler, counts = watched(repo)
            handler.recover()
            blocking = handler.classify_restart(OWNER.id, reservation.operation_id)
            blocking_replay = handler.submit(turn(8))
            acknowledged = repo.acknowledge_unknown(OWNER.id, reservation.operation_id)
            historical = handler.classify_restart(OWNER.id, reservation.operation_id)
            historical_replay = handler.submit(turn(8))
            print(json.dumps({
                "blocking": described(blocking),
                "blocking_replay": type(blocking_replay).__name__,
                "acknowledged": type(acknowledged).__name__,
                "historical": described(historical),
                "historical_replay": type(historical_replay).__name__,
                "counts": counts,
                "facts": facts(reservation.operation_id),
            }))
            """
        ))
        self.assertEqual(payload["blocking"]["type"], "RestartUnknownBlocking")
        self.assertTrue(payload["blocking"]["blocks"])
        self.assertFalse(payload["blocking"]["acknowledged"])
        self.assertEqual(payload["blocking_replay"], "Existing")
        self.assertEqual(payload["acknowledged"], "Acknowledged")
        self.assertEqual(payload["historical"]["type"], "RestartUnknownAcknowledged")
        self.assertFalse(payload["historical"]["blocks"])
        self.assertTrue(payload["historical"]["acknowledged"])
        self.assertEqual(payload["historical_replay"], "Existing")
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["counts"]["claim"], 0)
        self.assertEqual(payload["facts"]["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(payload["facts"]["messages"], 0)
        self.assertTrue(payload["facts"]["acknowledged"])
        self.assertEqual(payload["facts"]["blocks"], 0)

    def test_completed_histories_are_not_reexecuted(self):
        payload = _payload(self, _run(
            """\
            def scenario_success():
                repo, clock = repository()
                handler, counts = watched(repo)
                completed = handler.submit(turn(9))
                operation_id = Operation.objects.get(owner_id=OWNER.id, status="SUCCEEDED").id
                restarted, replay_counts = watched(
                    SqliteConversationRepository(Clock(PREPARED_AT), NEW)
                )
                replay = restarted.submit(turn(9, runner=NEW))
                classified = restarted.classify_restart(OWNER.id, operation_id)
                return {
                    "completed": described(completed),
                    "replay": type(replay).__name__,
                    "classified": described(classified),
                    "provider": counts["provider"],
                    "replay_provider": replay_counts["provider"],
                    "replay_claim": replay_counts["claim"],
                }

            def scenario_failure():
                repo, clock = repository(now=2_000_000)
                def fail(**kwargs):
                    raise RuntimeError("standing context unavailable")
                handler, counts = watched(repo, fail)
                failed = handler.submit(turn(10, expected=1))
                operation_id = Operation.objects.get(status="FAILED").id
                restarted, replay_counts = watched(
                    SqliteConversationRepository(Clock(2_000_000), NEW)
                )
                replay = restarted.submit(turn(10, runner=NEW, expected=1))
                classified = restarted.classify_restart(OWNER.id, operation_id)
                return {
                    "failed": described(failed),
                    "replay": type(replay).__name__,
                    "classified": described(classified),
                    "provider": counts["provider"],
                    "replay_provider": replay_counts["provider"],
                }

            def scenario_cancelled():
                repo, clock = repository(now=3_000_000)
                reservation = prepare_queued(repo, 11, expected=1)
                clock.value = reservation.reservation_expires_at
                handler, counts = watched(repo)
                recovered = handler.recover()
                restarted, replay_counts = watched(
                    SqliteConversationRepository(clock, NEW)
                )
                replay = restarted.submit(turn(11, runner=NEW, expected=1))
                classified = restarted.classify_restart(
                    OWNER.id,
                    reservation.operation_id,
                )
                return {
                    "recovered": type(recovered).__name__,
                    "replay": type(replay).__name__,
                    "classified": described(classified),
                    "provider": counts["provider"],
                    "replay_provider": replay_counts["provider"],
                    "operations": Operation.objects.count(),
                }

            print(json.dumps({
                "success": scenario_success(),
                "failure": scenario_failure(),
                "cancelled": scenario_cancelled(),
            }))
            """
        ))
        success = payload["success"]
        self.assertEqual(success["completed"]["type"], "DurableTurnCompleted")
        self.assertEqual(success["provider"], 1)
        self.assertEqual(success["replay"], "Existing")
        self.assertEqual(success["replay_provider"], 0)
        self.assertEqual(success["replay_claim"], 0)
        self.assertEqual(success["classified"]["type"], "RestartCompleted")
        self.assertEqual(success["classified"]["status"], "SUCCEEDED")
        failure = payload["failure"]
        self.assertEqual(failure["failed"]["type"], "DurablePreProviderFailure")
        self.assertEqual(failure["provider"], 0)
        self.assertEqual(failure["replay"], "Existing")
        self.assertEqual(failure["replay_provider"], 0)
        self.assertEqual(failure["classified"]["status"], "FAILED")
        cancelled = payload["cancelled"]
        self.assertEqual(cancelled["recovered"], "CancelledExpiredQueued")
        self.assertEqual(cancelled["replay"], "Existing")
        self.assertEqual(cancelled["replay_provider"], 0)
        self.assertEqual(cancelled["classified"]["status"], "CANCELLED")
        self.assertEqual(cancelled["provider"], 0)
        self.assertEqual(cancelled["operations"], 3)

    def test_expired_idempotency_binding_does_not_execute(self):
        payload = _payload(self, _run(
            """\
            repo, clock = repository()
            reservation = prepare_queued(repo, 12)
            stored_expiry = IdempotencyKey.objects.get(
                operation_id=reservation.operation_id
            ).expires_at_us
            clock.value = stored_expiry
            restarted, counts = watched(SqliteConversationRepository(clock, NEW))
            replay = restarted.submit(turn(12, runner=NEW))
            print(json.dumps({
                "replay": type(replay).__name__,
                "same": str(replay.operation.operation_id) == str(reservation.operation_id),
                "stored_expiry": stored_expiry,
                "counts": counts,
                "facts": facts(reservation.operation_id),
            }))
            """
        ))
        self.assertEqual(payload["replay"], "Existing")
        self.assertTrue(payload["same"])
        self.assertGreater(payload["stored_expiry"], 1_000_000)
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["counts"]["claim"], 0)
        self.assertEqual(payload["counts"]["adopt"], 0)
        self.assertEqual(payload["facts"]["operations"], 1)
        self.assertEqual(payload["facts"]["status"], "QUEUED")

    def test_cross_owner_and_missing_operation_fail_closed(self):
        payload = _payload(self, _run(
            """\
            repo, clock = repository()
            reservation = prepare_queued(repo, 13, content="hello")
            handler, counts = watched(repo)
            foreign = handler.classify_restart(OTHER.id, reservation.operation_id)
            missing = handler.classify_restart(OWNER.id, uuid.uuid4())
            print(json.dumps({
                "foreign": type(foreign).__name__,
                "foreign_text": repr(foreign),
                "missing": type(missing).__name__,
                "missing_text": repr(missing),
                "counts": counts,
                "facts": facts(reservation.operation_id),
            }))
            """
        ))
        self.assertEqual(payload["foreign"], "NotFound")
        self.assertEqual(payload["missing"], "NotFound")
        self.assertNotIn("hello", payload["foreign_text"])
        self.assertNotIn("hello", payload["missing_text"])
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["counts"]["adopt"], 0)
        self.assertEqual(payload["counts"]["recover"], 0)
        self.assertEqual(payload["facts"]["status"], "QUEUED")

    def test_recovery_leaves_live_adoption_and_claim_unchanged(self):
        payload = _payload(self, _run(
            """\
            repo, clock = repository()
            reservation = prepare_queued(repo, 14)
            adopter, adopt_counts = watched(
                SqliteConversationRepository(Clock(PREPARED_AT), NEW)
            )
            adopter.adopt_persisted(OWNER.id, reservation.operation_id)
            handler, counts = watched(repo)
            while_queued = handler.recover()
            queued_facts = facts(reservation.operation_id)
            claimed = SqliteConversationRepository(
                Clock(PREPARED_AT),
                NEW,
            ).claim_running(OWNER.id, reservation.operation_id)
            clock.value = claimed.claimed_operation.execution_deadline
            while_running = handler.recover()
            print(json.dumps({
                "while_queued": type(while_queued).__name__,
                "queued_facts": queued_facts,
                "while_running": type(while_running).__name__,
                "running_facts": facts(reservation.operation_id),
                "counts": counts,
                "adopt_counts": adopt_counts,
            }))
            """
        ))
        self.assertEqual(payload["while_queued"], "LiveQueued")
        self.assertEqual(payload["queued_facts"]["status"], "QUEUED")
        self.assertEqual(payload["queued_facts"]["runner"], "BBBBBBBBBBBBBBBBBBBBBQ")
        self.assertEqual(
            payload["queued_facts"]["slot_runner"],
            "BBBBBBBBBBBBBBBBBBBBBQ",
        )
        self.assertEqual(payload["while_running"], "LiveRunning")
        self.assertEqual(payload["running_facts"]["status"], "RUNNING")
        self.assertEqual(payload["running_facts"]["runner"], "BBBBBBBBBBBBBBBBBBBBBQ")
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["counts"]["recover"], 2)
        self.assertEqual(payload["adopt_counts"]["provider"], 0)

    def test_ambiguous_recovery_commit_is_not_retried(self):
        payload = self._ambiguous(True)
        self.assertEqual(payload["recovered"], "RecoveryUnconfirmed")
        self.assertEqual(payload["counts"]["recover"], 1)
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["facts"]["status"], "CANCELLED")
        self.assertIsNone(payload["facts"]["slot_operation"])

    def test_ambiguous_recovery_rollback_is_not_retried(self):
        payload = self._ambiguous(False)
        self.assertEqual(payload["recovered"], "RecoveryUnconfirmed")
        self.assertEqual(payload["counts"]["recover"], 1)
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["facts"]["status"], "QUEUED")
        self.assertEqual(payload["facts"]["runner"], "AAAAAAAAAAAAAAAAAAAAAA")

    def test_busy_recovery_does_not_retry_or_call_provider(self):
        payload = _payload(self, _run(
            """\
            repo, clock = repository()
            reservation = prepare_queued(repo, 16)
            clock.value = reservation.reservation_expires_at
            handler, counts = watched(repo)
            holder = sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True)
            try:
                holder.execute("BEGIN IMMEDIATE")
                recovered = handler.recover()
            finally:
                holder.rollback()
                holder.close()
            print(json.dumps({
                "recovered": type(recovered).__name__,
                "counts": counts,
                "facts": facts(reservation.operation_id),
            }))
            """
        ))
        self.assertEqual(payload["recovered"], "DatabaseBusy")
        self.assertEqual(payload["counts"]["recover"], 1)
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["facts"]["status"], "QUEUED")
        self.assertEqual(payload["facts"]["runner"], "AAAAAAAAAAAAAAAAAAAAAA")

    def test_construction_and_classification_do_not_recover(self):
        payload = _payload(self, _run(
            """\
            repo, clock = repository()
            reservation = prepare_queued(repo, 17)
            handler, counts = watched(repo)
            before = dict(counts)
            classified = handler.classify_restart(OWNER.id, reservation.operation_id)
            print(json.dumps({
                "before": before,
                "after": counts,
                "classified": described(classified),
            }))
            """
        ))
        self.assertEqual(payload["before"], {
            "provider": 0,
            "claim": 0,
            "adopt": 0,
            "recover": 0,
        })
        self.assertEqual(payload["after"], payload["before"])
        self.assertEqual(payload["classified"]["type"], "RestartQueued")

    def test_caller_transaction_is_left_open(self):
        payload = _payload(self, _run(
            """\
            repo, clock = repository()
            reservation = prepare_queued(repo, 18)
            handler, counts = watched(repo)
            with transaction.atomic():
                classified = handler.classify_restart(OWNER.id, reservation.operation_id)
                adopted = handler.adopt_persisted(OWNER.id, reservation.operation_id)
                recovered = handler.recover()
                open_during = connection.in_atomic_block
            print(json.dumps({
                "classified": type(classified).__name__,
                "adopted": type(adopted).__name__,
                "recovered": type(recovered).__name__,
                "open_during": open_during,
                "autocommit": connection.get_autocommit(),
                "counts": counts,
                "facts": facts(reservation.operation_id),
            }))
            """
        ))
        self.assertEqual(payload["classified"], "TransactionHeld")
        self.assertEqual(payload["adopted"], "TransactionHeld")
        self.assertEqual(payload["recovered"], "TransactionHeld")
        self.assertTrue(payload["open_during"])
        self.assertTrue(payload["autocommit"])
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["counts"]["adopt"], 0)
        self.assertEqual(payload["counts"]["recover"], 0)
        self.assertEqual(payload["facts"]["status"], "QUEUED")
        self.assertEqual(payload["facts"]["runner"], "AAAAAAAAAAAAAAAAAAAAAA")

    def test_service_source_and_production_database(self):
        source = SERVICE_SOURCE.read_text()
        initializer = source.split("def __init__", 1)[1].split("def submit", 1)[0]
        self.assertNotIn("recover_slot", initializer)
        self.assertEqual(source.count("recover_slot"), 1)
        for banned in (
            "acknowledge_unknown",
            "delete_expired_idempotency",
            "GlobalModelSlot",
            "terminal",
            "session",
            "model_client",
            "openai",
            "sqlite3",
            "getpass",
            "getlogin",
            "max_smart.sqlite3",
            "setup_database",
            "connection.close",
        ):
            self.assertNotIn(banned, source, banned)
        self.assertFalse(PRODUCTION_DATABASE.exists())

    def _ambiguous(self, durable):
        return _payload(self, _run(
            f"""\
            repo, clock = repository()
            reservation = prepare_queued(repo, 15)
            clock.value = reservation.reservation_expires_at
            real_recover = repo.recover_slot
            counts = {{"recover": 0, "provider": 0}}

            def recover_slot():
                counts["recover"] += 1
                if counts["recover"] > 1:
                    return real_recover()
                real_commit = connection.commit

                def commit(*args, **kwargs):
                    if {durable!r}:
                        real_commit(*args, **kwargs)
                    raise OperationalError("simulated recovery commit")

                connection.commit = commit
                try:
                    return real_recover()
                finally:
                    connection.commit = real_commit

            repo.recover_slot = recover_slot

            def provide(**kwargs):
                counts["provider"] += 1
                return "model-output"

            handler = ConversationService(repo, lambda **kwargs: "standing", provide)
            recovered = handler.recover()
            print(json.dumps({{
                "recovered": type(recovered).__name__,
                "counts": counts,
                "facts": facts(reservation.operation_id),
            }}))
            """
        ))
