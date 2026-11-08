"""Disposable-database tests for one claim-authorized provider attempt."""

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

_PREAMBLE = """\
import base64
import json
import os
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
from conversation_service import (
    AlreadyDispatched,
    ContextUnavailable,
    ConversationService,
    ProviderResponse,
    RunnerMismatch,
    TransactionHeld,
    TurnRequest,
)
from max_smart_db.creation import SqliteConversationRepository
from max_smart_db.models import Conversation, GlobalModelSlot, Message, Operation
from max_smart_db.repository import (
    Claimed,
    ClaimedOperation,
    ClaimNotConfirmed,
    CommitAmbiguous,
    ConversationBlocked,
    DatabaseBusy,
    DurableClaimConfirmed,
    Existing,
    ExpiredResubmit,
    PrepareCommand,
    KeyBindingMismatch,
    ModelBusy,
    NotFound,
    NotQueued,
    OperationStatus,
    OperationView,
    OwnershipLost,
    Prepared,
    QueuedReservation,
    SlotMismatch,
    StaleVersion,
)

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

OWNER = User.objects.create(username="owner")
OTHER = User.objects.create(username="other")
CONVERSATION = uuid.UUID("00112233445566778899aabbccddeeff")
OPERATION = uuid.UUID("11111111111111111111111111111111")
MISSING = uuid.UUID("33333333333333333333333333333333")
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
FORMER = "BBBBBBBBBBBBBBBBBBBBBQ"
HASH = "ab" * 32
CONTEXT = "standing-context:" + ("x" * 3000)

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


def turn(**overrides):
    values = {
        "actor_id": OWNER.id,
        "conversation_id": CONVERSATION,
        "request_content": "hello",
        "idempotency_key": key_for(1),
        "runner_id": RUNNER,
        "expected_version": 0,
        "provider_id": "openai",
        "model_id": "gpt-5.6-sol",
        "adopt_operation_id": None,
    }
    values.update(overrides)
    return TurnRequest(**values)


def queued(identifier, *, reservation=1_000_000_000, runner=FORMER):
    Operation.objects.create(
        id=identifier,
        conversation_id=CONVERSATION,
        owner_id=OWNER.id,
        operation_type="message",
        provider_id="openai",
        model_id="gpt-5.6-sol",
        status="QUEUED",
        blocks_conversation=1,
        expected_version=0,
        request_content="hello",
        fingerprint_version=1,
        request_hash=HASH,
        retry_of=None,
        runner_id=runner,
        reservation_expires_at_us=reservation,
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
        runner_id=runner,
        reservation_expires_at_us=reservation,
        lease_expires_at_us=None,
        acquired_at_us=7,
    )


def state(identifier):
    operation = Operation.objects.get(pk=identifier)
    slot = GlobalModelSlot.objects.get(pk=1)
    return {
        "status": operation.status,
        "runner": operation.runner_id,
        "reservation": operation.reservation_expires_at_us,
        "started": operation.provider_started_at_us,
        "deadline": operation.execution_deadline_us,
        "lease": operation.lease_expires_at_us,
        "slot_reservation": slot.reservation_expires_at_us,
        "slot_lease": slot.lease_expires_at_us,
        "messages": Message.objects.count(),
        "version": Conversation.objects.get(pk=operation.conversation_id).version,
    }


class Box:
    def __init__(self):
        self.provider = 0
        self.context = 0
        self.claims = 0
        self.confirms = 0
        self.context_atomic = None
        self.provider_atomic = None
        self.context_autocommit = None
        self.provider_autocommit = None
        self.standing_context = None
        self.claimed_at = None
        self.reservation_during_context = None
        self.runner_during_context = None


def observe(repository, box, loader=None, provider=None):
    real_claim = repository.claim_running
    real_confirm = repository.confirm_durable_claim

    def claim_running(actor_id, operation_id):
        box.claims += 1
        return real_claim(actor_id, operation_id)

    def confirm_durable_claim(*args):
        box.confirms += 1
        return real_confirm(*args)

    repository.claim_running = claim_running
    repository.confirm_durable_claim = confirm_durable_claim

    def load_context(**kwargs):
        box.context += 1
        box.context_atomic = connection.in_atomic_block
        box.context_autocommit = connection.get_autocommit()
        operation = Operation.objects.filter(pk=kwargs["operation_id"]).first()
        if operation is not None:
            box.reservation_during_context = operation.reservation_expires_at_us
            box.runner_during_context = operation.runner_id
        if loader is not None:
            return loader(box, **kwargs)
        return CONTEXT

    def provide(**kwargs):
        box.provider += 1
        box.provider_atomic = connection.in_atomic_block
        box.provider_autocommit = connection.get_autocommit()
        box.standing_context = kwargs["standing_context"]
        box.claimed_at = kwargs["claimed_at"]
        if provider is not None:
            return provider(box, **kwargs)
        return "model-output"

    return ConversationService(repository, load_context, provide)


def repository(now=123_456):
    clock = Clock(now)
    return SqliteConversationRepository(clock, RUNNER), clock


def public(outcome, box):
    return {
        "type": type(outcome).__name__,
        "provider": box.provider,
        "context": box.context,
        "claims": box.claims,
        "confirms": box.confirms,
        "context_atomic": box.context_atomic,
        "provider_atomic": box.provider_atomic,
        "context_autocommit": box.context_autocommit,
        "provider_autocommit": box.provider_autocommit,
        "context_exact": box.standing_context == CONTEXT,
        "claimed_at": box.claimed_at,
        "reservation_during_context": box.reservation_during_context,
        "runner_during_context": box.runner_during_context,
        "output": getattr(outcome, "output_text", None),
        "assistant": getattr(
            getattr(outcome, "assistant_message", None),
            "content",
            None,
        ),
        "messages": Message.objects.count(),
    }
"""


def _run(body, *, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "m3s1.sqlite3"
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


class ConversationServiceTests(unittest.TestCase):
    def test_prepare_context_claim_invokes_provider_once(self):
        result = _run(
            """\
            repo, clock = repository()
            box = Box()
            service = observe(repo, box)
            outcome = service.submit(turn())
            operation = Operation.objects.get(owner_id=OWNER.id)
            print(json.dumps({
                **public(outcome, box),
                "clock": clock.calls,
                "state": state(operation.id),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "DurableTurnCompleted")
        self.assertEqual(payload["assistant"], "model-output")
        self.assertEqual(payload["provider"], 1)
        self.assertEqual(payload["context"], 1)
        self.assertEqual(payload["claims"], 1)
        self.assertEqual(payload["confirms"], 0)
        self.assertEqual(payload["clock"], 3)
        self.assertFalse(payload["context_atomic"])
        self.assertFalse(payload["provider_atomic"])
        self.assertTrue(payload["context_autocommit"])
        self.assertTrue(payload["provider_autocommit"])
        self.assertTrue(payload["context_exact"])
        self.assertEqual(payload["claimed_at"], 123_456)
        self.assertEqual(payload["messages"], 2)
        self.assertEqual(payload["state"]["status"], "SUCCEEDED")
        self.assertEqual(payload["state"]["started"], 123_456)
        self.assertEqual(payload["state"]["deadline"], 600_123_456)
        self.assertEqual(payload["state"]["lease"], 720_123_456)
        self.assertEqual(payload["state"]["version"], 1)
        self.assertIsNone(payload["state"]["slot_reservation"])
        self.assertIsNone(payload["state"]["slot_lease"])

    def test_valid_queued_adoption_invokes_provider_once(self):
        result = _run(
            """\
            queued(OPERATION)
            repo, clock = repository()
            box = Box()
            service = observe(repo, box)
            outcome = service.submit(turn(
                adopt_operation_id=OPERATION,
                idempotency_key=key_for(2),
            ))
            print(json.dumps({
                **public(outcome, box),
                "clock": clock.calls,
                "state": state(OPERATION),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "DurableTurnCompleted")
        self.assertEqual(payload["provider"], 1)
        self.assertEqual(payload["claims"], 1)
        self.assertEqual(payload["confirms"], 0)
        self.assertEqual(
            payload["runner_during_context"],
            "AAAAAAAAAAAAAAAAAAAAAA",
        )
        self.assertEqual(payload["reservation_during_context"], 1_000_000_000)
        self.assertEqual(payload["state"]["started"], 123_456)
        self.assertEqual(payload["state"]["deadline"], 600_123_456)
        self.assertEqual(payload["state"]["lease"], 720_123_456)
        self.assertEqual(payload["messages"], 2)

    def test_commit_succeeded_ambiguity_confirms_then_invokes_once(self):
        result = _run(
            """\
            repo, clock = repository()
            box = Box()
            service = observe(repo, box)
            real_commit = connection.commit
            commits = {"count": 0}

            def commit(*args, **kwargs):
                commits["count"] += 1
                real_commit(*args, **kwargs)
                if commits["count"] >= 2:
                    raise OperationalError("simulated post-commit failure")

            connection.commit = commit
            try:
                outcome = service.submit(turn())
            finally:
                connection.commit = real_commit
            operation = Operation.objects.get(owner_id=OWNER.id)
            print(json.dumps({
                **public(outcome, box),
                "commits": commits["count"],
                "state": state(operation.id),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "DurableTurnCompleted")
        self.assertEqual(payload["provider"], 1)
        self.assertEqual(payload["claims"], 1)
        self.assertEqual(payload["confirms"], 1)
        self.assertEqual(payload["claimed_at"], 123_456)
        self.assertGreaterEqual(payload["commits"], 2)
        self.assertEqual(payload["state"]["status"], "SUCCEEDED")
        self.assertEqual(payload["state"]["started"], 123_456)
        self.assertEqual(payload["messages"], 2)

    def test_failed_ambiguous_claims_do_not_invoke_provider(self):
        rolled = _payload(self, _run(
            """\
            repo, clock = repository()
            box = Box()
            service = observe(repo, box)
            real_commit = connection.commit
            commits = {"count": 0}

            def wrapped(*args, **kwargs):
                commits["count"] += 1
                if commits["count"] == 1:
                    return real_commit(*args, **kwargs)
                raise OperationalError("simulated rollback before commit")

            connection.commit = wrapped
            try:
                outcome = service.submit(turn(idempotency_key=key_for(3)))
            finally:
                connection.commit = real_commit
            print(json.dumps({
                **public(outcome, box),
                "status": Operation.objects.get(owner_id=OWNER.id).status,
            }))
            """
        ))
        mismatched = _payload(self, _run(
            """\
            repo, clock = repository()
            box = Box()
            real_confirm = repo.confirm_durable_claim

            def mismatch(*args):
                proof = real_confirm(*args)
                return DurableClaimConfirmed(
                    operation_id=proof.operation_id,
                    runner_id=proof.runner_id,
                    claimed_at=proof.claimed_at + 1,
                )

            repo.confirm_durable_claim = mismatch
            service = observe(repo, box)
            real_commit = connection.commit
            commits = {"count": 0}

            def wrapped(*args, **kwargs):
                commits["count"] += 1
                real_commit(*args, **kwargs)
                if commits["count"] >= 2:
                    raise OperationalError("simulated post-commit failure")

            connection.commit = wrapped
            try:
                outcome = service.submit(turn(idempotency_key=key_for(4)))
            finally:
                connection.commit = real_commit
            print(json.dumps({
                **public(outcome, box),
                "status": Operation.objects.get(owner_id=OWNER.id).status,
            }))
            """
        ))
        inconclusive = _payload(self, _run(
            """\
            repo, clock = repository()
            box = Box()

            def explode(*args):
                raise OperationalError("inconclusive confirmation")

            repo.confirm_durable_claim = explode
            service = observe(repo, box)
            real_commit = connection.commit
            commits = {"count": 0}

            def wrapped(*args, **kwargs):
                commits["count"] += 1
                real_commit(*args, **kwargs)
                if commits["count"] >= 2:
                    raise OperationalError("simulated post-commit failure")

            connection.commit = wrapped
            try:
                outcome = service.submit(turn(idempotency_key=key_for(5)))
            finally:
                connection.commit = real_commit
            print(json.dumps(public(outcome, box)))
            """
        ))
        self.assertEqual(rolled["type"], "CommitAmbiguous")
        self.assertEqual(rolled["provider"], 0)
        self.assertEqual(rolled["confirms"], 1)
        self.assertEqual(rolled["status"], "QUEUED")
        self.assertEqual(rolled["messages"], 0)
        self.assertEqual(mismatched["type"], "CommitAmbiguous")
        self.assertEqual(mismatched["provider"], 0)
        self.assertEqual(mismatched["confirms"], 1)
        self.assertEqual(mismatched["status"], "RUNNING")
        self.assertEqual(mismatched["messages"], 0)
        self.assertEqual(inconclusive["type"], "CommitAmbiguous")
        self.assertEqual(inconclusive["provider"], 0)
        self.assertEqual(inconclusive["confirms"], 1)
        self.assertEqual(inconclusive["messages"], 0)

    def test_rejections_do_not_invoke_provider(self):
        result = _run(
            """\
            outcomes = []

            def once(label, request, now=123_456, before=None):
                repo, clock = repository(now)
                box = Box()
                if before is not None:
                    before()
                service = observe(repo, box)
                outcome = service.submit(request)
                outcomes.append({
                    "label": label,
                    "type": type(outcome).__name__,
                    "provider": box.provider,
                    "context": box.context,
                    "claims": box.claims,
                    "confirms": box.confirms,
                })

            once("stale", turn(expected_version=4, idempotency_key=key_for(6)))
            once(
                "missing_actor",
                turn(actor_id=OTHER.id, idempotency_key=key_for(7)),
            )
            once("missing_adopt", turn(
                adopt_operation_id=MISSING,
                idempotency_key=key_for(8),
            ))
            once(
                "runner",
                turn(runner_id=FORMER, idempotency_key=key_for(9)),
            )

            def occupy_blocker():
                queued(OPERATION, runner=RUNNER)

            once(
                "blocked",
                turn(idempotency_key=key_for(10)),
                before=occupy_blocker,
            )
            once(
                "expired_adopt",
                turn(adopt_operation_id=OPERATION, idempotency_key=key_for(11)),
                now=1_000_000_000,
            )

            repo, clock = repository()
            box = Box()
            service = observe(repo, box)
            real_commit = connection.commit

            def fail_prepare(*args, **kwargs):
                raise OperationalError("simulated prepare rollback")

            connection.commit = fail_prepare
            try:
                ambiguous_prepare = service.submit(
                    turn(idempotency_key=key_for(12))
                )
            finally:
                connection.commit = real_commit
            outcomes.append({
                "label": "prepare_ambiguous",
                "type": type(ambiguous_prepare).__name__,
                "provider": box.provider,
                "context": box.context,
                "claims": box.claims,
                "confirms": box.confirms,
            })

            repo, clock = repository()
            prepared = repo.prepare(PrepareCommand(
                actor_id=OWNER.id,
                conversation_id=CONVERSATION,
                expected_version=0,
                request_content="hello",
                idempotency_key=key_for(18),
                provider_id="openai",
                model_id="gpt-5.6-sol",
            ))
            once(
                "key_mismatch",
                turn(request_content="different", idempotency_key=key_for(18)),
            )
            print(json.dumps({
                "prepared": type(prepared).__name__,
                "outcomes": outcomes,
                "messages": Message.objects.count(),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["prepared"], "Prepared")
        self.assertEqual(payload["messages"], 0)
        by_label = {row["label"]: row for row in payload["outcomes"]}
        for label, expected in (
            ("stale", "StaleVersion"),
            ("missing_actor", "NotFound"),
            ("missing_adopt", "NotFound"),
            ("runner", "RunnerMismatch"),
            ("blocked", "ConversationBlocked"),
            ("expired_adopt", "ExpiredResubmit"),
            ("prepare_ambiguous", "CommitAmbiguous"),
            ("key_mismatch", "KeyBindingMismatch"),
        ):
            row = by_label[label]
            self.assertEqual(row["type"], expected, label)
            self.assertEqual(row["provider"], 0, label)
            self.assertEqual(row["confirms"], 0, label)
        self.assertEqual(by_label["runner"]["context"], 0)
        self.assertEqual(by_label["runner"]["claims"], 0)
        self.assertEqual(by_label["prepare_ambiguous"]["context"], 0)
        self.assertEqual(by_label["prepare_ambiguous"]["claims"], 0)
        self.assertEqual(by_label["expired_adopt"]["context"], 0)
        self.assertEqual(by_label["key_mismatch"]["claims"], 0)

    def test_context_failure_does_not_claim_or_invoke(self):
        result = _run(
            """\
            repo, clock = repository()
            box = Box()

            def fail(box, **kwargs):
                raise RuntimeError("standing context unavailable")

            service = observe(repo, box, loader=fail)
            outcome = service.submit(turn(idempotency_key=key_for(13)))
            operation = Operation.objects.get(owner_id=OWNER.id)
            print(json.dumps({
                **public(outcome, box),
                "category": outcome.failure_category.value,
                "state": state(operation.id),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "DurablePreProviderFailure")
        self.assertEqual(payload["category"], "context_unavailable")
        self.assertEqual(payload["provider"], 0)
        self.assertEqual(payload["claims"], 0)
        self.assertEqual(payload["confirms"], 0)
        self.assertEqual(payload["context"], 1)
        self.assertEqual(payload["state"]["status"], "FAILED")
        self.assertIsNone(payload["state"]["started"])
        self.assertIsNone(payload["state"]["slot_reservation"])
        self.assertEqual(payload["messages"], 0)

    def test_outer_transaction_skips_context_and_provider(self):
        result = _run(
            """\
            repo, clock = repository()
            box = Box()
            service = observe(repo, box)
            with transaction.atomic():
                outcome = service.submit(turn(idempotency_key=key_for(14)))
            print(json.dumps({
                **public(outcome, box),
                "operations": Operation.objects.count(),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "TransactionHeld")
        self.assertEqual(payload["provider"], 0)
        self.assertEqual(payload["context"], 0)
        self.assertEqual(payload["claims"], 0)
        self.assertEqual(payload["operations"], 0)

    def test_one_attempt_does_not_dispatch_twice(self):
        result = _run(
            """\
            repo, clock = repository()
            box = Box()
            holder = {}

            def provide(box, **kwargs):
                if box.provider == 1:
                    again = holder["service"].submit(
                        turn(idempotency_key=key_for(15))
                    )
                    box.again = type(again).__name__
                return "model-output"

            service = observe(repo, box, provider=provide)
            holder["service"] = service
            outcome = service.submit(turn(idempotency_key=key_for(15)))
            print(json.dumps({
                **public(outcome, box),
                "again": box.again,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "DurableTurnCompleted")
        self.assertEqual(payload["provider"], 1)
        self.assertEqual(payload["again"], "Existing")
        self.assertEqual(payload["claims"], 1)

    def test_restart_does_not_replay_provider(self):
        result = _run(
            """\
            repo, clock = repository()
            first_box = Box()
            first = observe(repo, first_box)
            original = first.submit(turn(idempotency_key=key_for(16)))
            second_box = Box()
            restarted = observe(repo, second_box)
            replay = restarted.submit(turn(idempotency_key=key_for(16)))
            operation = Operation.objects.get(owner_id=OWNER.id)
            print(json.dumps({
                "first": public(original, first_box),
                "replay_type": type(replay).__name__,
                "replay_provider": second_box.provider,
                "replay_claims": second_box.claims,
                "replay_confirms": second_box.confirms,
                "state": state(operation.id),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["first"]["type"], "DurableTurnCompleted")
        self.assertEqual(payload["first"]["provider"], 1)
        self.assertEqual(payload["replay_type"], "Existing")
        self.assertEqual(payload["replay_provider"], 0)
        self.assertEqual(payload["replay_claims"], 0)
        self.assertEqual(payload["replay_confirms"], 0)
        self.assertEqual(payload["state"]["status"], "SUCCEEDED")
        self.assertEqual(payload["state"]["messages"], 2)

    def test_authorization_matrix_fails_closed(self):
        result = _run(
            """\
            reservation = QueuedReservation(
                operation_id=OPERATION,
                actor_id=OWNER.id,
                conversation_id=CONVERSATION,
                request_hash=HASH,
                fingerprint_version=1,
                expected_version=0,
                runner_id=RUNNER,
                reservation_expires_at=1_000_000_000,
            )
            claimed = Claimed(ClaimedOperation(
                operation_id=OPERATION,
                actor_id=OWNER.id,
                conversation_id=CONVERSATION,
                request_hash=HASH,
                fingerprint_version=1,
                expected_version=0,
                runner_id=RUNNER,
                claimed_at=123_456,
                execution_deadline=600_123_456,
                lease_expires_at=720_123_456,
            ))
            view = OperationView(
                operation_id=OPERATION,
                actor_id=OWNER.id,
                conversation_id=CONVERSATION,
                status=OperationStatus.QUEUED,
                runner_id=RUNNER,
                request_hash=HASH,
                fingerprint_version=1,
                expected_version=0,
                provider_started_at=None,
                failure_category=None,
                blocks_conversation=True,
                acknowledged=False,
            )

            class Fake:
                def __init__(self, prepared, claim=None, proof=None, confirm_error=False):
                    self.runner_id = RUNNER
                    self.prepared = prepared
                    self.claim = claim
                    self.proof = proof
                    self.confirm_error = confirm_error
                    self.prepare_calls = 0
                    self.claim_calls = 0
                    self.confirm_calls = 0

                def prepare(self, command):
                    self.prepare_calls += 1
                    return self.prepared

                def adopt_queued(self, actor_id, operation_id):
                    return self.prepared

                def claim_running(self, actor_id, operation_id):
                    self.claim_calls += 1
                    return self.claim

                def confirm_durable_claim(self, actor_id, operation_id, runner_id, claimed_at):
                    self.confirm_calls += 1
                    if self.confirm_error:
                        raise OperationalError("inconclusive")
                    return self.proof

            rows = []

            def run(label, prepared, claim=None, proof=None, confirm_error=False, adopt=False):
                box = Box()
                fake = Fake(prepared, claim, proof, confirm_error)
                service = observe(fake, box)
                request = turn(
                    adopt_operation_id=OPERATION if adopt else None,
                    idempotency_key=key_for(17),
                )
                outcome = service.submit(request)
                rows.append({
                    "label": label,
                    "type": type(outcome).__name__,
                    "same": outcome is prepared or outcome is claim,
                    "provider": box.provider,
                    "claims": fake.claim_calls,
                    "confirms": fake.confirm_calls,
                })

            run("prepared_existing", Existing(view))
            run("prepared_stale", StaleVersion(actual=0))
            run("prepared_mismatch", KeyBindingMismatch())
            run("prepared_blocked", ConversationBlocked(
                operation_id=OPERATION,
                status=OperationStatus.QUEUED,
            ))
            run("prepared_busy", ModelBusy())
            run("prepared_missing", NotFound())
            run("prepared_database_busy", DatabaseBusy())
            run("prepared_ambiguous", CommitAmbiguous(
                operation_id=OPERATION,
                runner_id=RUNNER,
            ))
            run("claim_expired", Prepared(reservation), ExpiredResubmit())
            run("claim_lost", Prepared(reservation), OwnershipLost())
            run("claim_slot", Prepared(reservation), SlotMismatch())
            run("claim_busy", Prepared(reservation), DatabaseBusy())
            run("claim_missing", Prepared(reservation), NotFound())
            run("claim_not_queued", Prepared(reservation), NotQueued(view))
            run(
                "claim_ambiguous_rejected",
                Prepared(reservation),
                CommitAmbiguous(
                    operation_id=OPERATION,
                    runner_id=RUNNER,
                    claimed_at=123_456,
                ),
                ClaimNotConfirmed(),
            )
            run(
                "claim_ambiguous_mismatch",
                Prepared(reservation),
                CommitAmbiguous(
                    operation_id=OPERATION,
                    runner_id=RUNNER,
                    claimed_at=123_456,
                ),
                DurableClaimConfirmed(
                    operation_id=OPERATION,
                    runner_id=RUNNER,
                    claimed_at=123_457,
                ),
            )
            run(
                "claim_ambiguous_incomplete",
                Prepared(reservation),
                CommitAmbiguous(operation_id=OPERATION, runner_id=RUNNER),
            )
            run(
                "claim_ambiguous_error",
                Prepared(reservation),
                CommitAmbiguous(
                    operation_id=OPERATION,
                    runner_id=RUNNER,
                    claimed_at=123_456,
                ),
                confirm_error=True,
            )
            run("adopt_expired", ExpiredResubmit(), adopt=True)

            box = Box()
            fake = Fake(Prepared(reservation), claimed)
            holder = {}

            def provide(box, **kwargs):
                if box.provider == 1:
                    again = holder["service"].submit(turn())
                    box.again = type(again).__name__
                return "model-output"

            service = observe(fake, box, provider=provide)
            holder["service"] = service
            repeated = service.submit(turn())
            rows.append({
                "label": "reenter",
                "type": type(repeated).__name__,
                "same": False,
                "provider": box.provider,
                "claims": fake.claim_calls,
                "confirms": fake.confirm_calls,
                "again": box.again,
            })
            print(json.dumps(rows))
            """
        )
        payload = _payload(self, result)
        by_label = {row["label"]: row for row in payload}
        for label, expected in (
            ("prepared_existing", "Existing"),
            ("prepared_stale", "StaleVersion"),
            ("prepared_mismatch", "KeyBindingMismatch"),
            ("prepared_blocked", "ConversationBlocked"),
            ("prepared_busy", "ModelBusy"),
            ("prepared_missing", "NotFound"),
            ("prepared_database_busy", "DatabaseBusy"),
            ("prepared_ambiguous", "CommitAmbiguous"),
            ("claim_expired", "ExpiredResubmit"),
            ("claim_lost", "OwnershipLost"),
            ("claim_slot", "SlotMismatch"),
            ("claim_busy", "DatabaseBusy"),
            ("claim_missing", "NotFound"),
            ("claim_not_queued", "NotQueued"),
            ("claim_ambiguous_rejected", "CommitAmbiguous"),
            ("claim_ambiguous_mismatch", "CommitAmbiguous"),
            ("claim_ambiguous_incomplete", "CommitAmbiguous"),
            ("claim_ambiguous_error", "CommitAmbiguous"),
            ("adopt_expired", "ExpiredResubmit"),
        ):
            row = by_label[label]
            self.assertEqual(row["type"], expected)
            self.assertEqual(row["provider"], 0)
            self.assertTrue(row["same"])
        self.assertEqual(by_label["prepared_ambiguous"]["claims"], 0)
        self.assertEqual(by_label["prepared_ambiguous"]["confirms"], 0)
        self.assertEqual(by_label["claim_ambiguous_incomplete"]["confirms"], 0)
        self.assertEqual(by_label["claim_ambiguous_rejected"]["confirms"], 1)
        self.assertEqual(by_label["claim_ambiguous_error"]["confirms"], 1)
        self.assertEqual(by_label["reenter"]["provider"], 1)
        self.assertEqual(by_label["reenter"]["again"], "AlreadyDispatched")
        self.assertEqual(by_label["reenter"]["confirms"], 0)

    def test_service_does_not_reference_terminal_json_or_slot(self):
        source = (SRC_ROOT / "conversation_service.py").read_text()
        for banned in (
            "GlobalModelSlot",
            "recover_slot",
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
            self.assertNotIn(banned, source)
        self.assertFalse(PRODUCTION_DATABASE.exists())
