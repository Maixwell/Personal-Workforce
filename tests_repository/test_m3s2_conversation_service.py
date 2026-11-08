"""Disposable-database tests for one persisted provider outcome."""

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
from django.db import OperationalError, connection
from django.db.migrations.executor import MigrationExecutor
from conversation_service import (
    ConversationService,
    ProviderFailure,
    TurnRequest,
)
from max_smart_db.creation import SqliteConversationRepository
from max_smart_db.models import Conversation, GlobalModelSlot, Message, Operation
from max_smart_db.repository import (
    Claimed,
    ClaimedOperation,
    CommitAmbiguous,
    DefiniteProviderCategory,
    UncertainProviderCategory,
)

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

OWNER = User.objects.create(username="owner")
CONVERSATION = uuid.UUID("00112233445566778899aabbccddeeff")
OPERATION = uuid.UUID("11111111111111111111111111111111")
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
STALE = "BBBBBBBBBBBBBBBBBBBBBQ"
HASH = "ab" * 32
CLAIMED_AT = 123_456
LEASE = CLAIMED_AT + 720_000_000

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


def repository(now=CLAIMED_AT):
    return SqliteConversationRepository(Clock(now), RUNNER)


def key_for(value):
    return base64.urlsafe_b64encode(bytes([value]) * 16).rstrip(b"=").decode()


def request(key=1):
    return TurnRequest(
        actor_id=OWNER.id,
        conversation_id=CONVERSATION,
        request_content="hello",
        idempotency_key=key_for(key),
        runner_id=RUNNER,
        expected_version=0,
        provider_id="openai",
        model_id="gpt-5.6-sol",
    )


def queued():
    Operation.objects.create(
        id=OPERATION,
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
        operation_id=OPERATION,
        runner_id=RUNNER,
        reservation_expires_at_us=1_000_000_000,
        lease_expires_at_us=None,
        acquired_at_us=7,
    )


def facts(operation_id):
    operation = Operation.objects.get(pk=operation_id)
    slot = GlobalModelSlot.objects.get(pk=1)
    conversation = Conversation.objects.get(pk=CONVERSATION)
    return {
        "status": operation.status,
        "blocks": operation.blocks_conversation,
        "failure": operation.failure_category,
        "lease": operation.lease_expires_at_us,
        "slot_operation": None if slot.operation_id is None else str(slot.operation_id),
        "slot_lease": slot.lease_expires_at_us,
        "version": conversation.version,
        "messages": Message.objects.filter(operation_id=operation_id).count(),
    }


def shown(outcome):
    operation_id = getattr(outcome, "operation_id", None)
    if operation_id is None and getattr(outcome, "claimed_operation", None) is not None:
        operation_id = outcome.claimed_operation.operation_id
    if operation_id is None:
        operation_id = outcome.operation.operation_id
    return str(operation_id)


def release_running():
    Operation.objects.filter(status="RUNNING").update(
        status="CANCELLED",
        blocks_conversation=0,
        runner_id=None,
        reservation_expires_at_us=None,
        provider_started_at_us=None,
        execution_deadline_us=None,
        lease_expires_at_us=None,
        finished_at_us=1,
        failure_category=None,
    )
    GlobalModelSlot.objects.filter(pk=1).update(
        operation_id=None,
        runner_id=None,
        reservation_expires_at_us=None,
        lease_expires_at_us=None,
        acquired_at_us=None,
    )


def service(provider, repo=None, counts=None):
    if repo is None:
        repo = repository()
    box = counts if counts is not None else {"provider": 0, "apply": 0, "failure": 0, "reconcile": 0}
    real_apply = repo.apply_success
    real_failure = repo.apply_failure
    real_reconcile = repo.reconcile_late_success

    def apply_success(claimed, output_text):
        box["apply"] += 1
        box["apply_atomic"] = connection.in_atomic_block
        return real_apply(claimed, output_text)

    def apply_failure(claimed, failure):
        box["failure"] += 1
        box["failure_atomic"] = connection.in_atomic_block
        return real_failure(claimed, failure)

    def reconcile_late_success(retained):
        box["reconcile"] += 1
        box["reconcile_atomic"] = connection.in_atomic_block
        return real_reconcile(retained)

    repo.apply_success = apply_success
    repo.apply_failure = apply_failure
    repo.reconcile_late_success = reconcile_late_success

    def provide(**kwargs):
        box["provider"] += 1
        box["provider_atomic"] = connection.in_atomic_block
        box["provider_autocommit"] = connection.get_autocommit()
        return provider(box, **kwargs)

    def load_context(**kwargs):
        box["context_atomic"] = connection.in_atomic_block
        return "standing-context"

    return ConversationService(repo, load_context, provide), box
"""


def _run(body, *, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "m3s2-service.sqlite3"
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


class ConversationOutcomeTests(unittest.TestCase):
    def test_definite_failure_releases_the_slot(self):
        result = _run(
            """\
            def provide(box, **kwargs):
                return ProviderFailure(
                    DefiniteProviderCategory.PROVIDER_DEFINITE_FAILURE
                )

            handler, box = service(provide)
            outcome = handler.submit(request())
            print(json.dumps({
                "type": type(outcome).__name__,
                "category": getattr(outcome, "failure_category", None)
                    and outcome.failure_category.value,
                "provider": box["provider"],
                "failure_writes": box["failure"],
                "apply_writes": box["apply"],
                "facts": facts(shown(outcome)),
                "provider_atomic": box["provider_atomic"],
                "provider_autocommit": box["provider_autocommit"],
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "DurableFailureRecorded")
        self.assertEqual(payload["category"], "provider_definite_failure")
        self.assertEqual(payload["provider"], 1)
        self.assertEqual(payload["failure_writes"], 1)
        self.assertEqual(payload["apply_writes"], 0)
        self.assertEqual(payload["facts"]["status"], "FAILED")
        self.assertEqual(payload["facts"]["blocks"], 0)
        self.assertEqual(payload["facts"]["messages"], 0)
        self.assertEqual(payload["facts"]["version"], 0)
        self.assertIsNone(payload["facts"]["slot_operation"])
        self.assertFalse(payload["provider_atomic"])
        self.assertTrue(payload["provider_autocommit"])

    def test_blank_output_is_a_definite_output_failure(self):
        result = _run(
            """\
            def provide(box, **kwargs):
                return "   "

            handler, box = service(provide)
            outcome = handler.submit(request())
            print(json.dumps({
                "type": type(outcome).__name__,
                "category": outcome.failure_category.value,
                "provider": box["provider"],
                "facts": facts(shown(outcome)),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "DurableFailureRecorded")
        self.assertEqual(payload["category"], "provider_output_invalid")
        self.assertEqual(payload["provider"], 1)
        self.assertEqual(payload["facts"]["status"], "FAILED")
        self.assertEqual(payload["facts"]["messages"], 0)

    def test_uncertain_errors_leave_the_claim_running(self):
        result = _run(
            """\
            def once(label, error, key):
                def provide(box, **kwargs):
                    raise error
                handler, box = service(provide)
                outcome = handler.submit(request(key))
                print_row = {
                    "label": label,
                    "type": type(outcome).__name__,
                    "provider": box["provider"],
                    "failure_writes": box["failure"],
                    "facts": facts(shown(outcome)),
                }
                Operation.objects.filter(status="RUNNING").update(
                    status="CANCELLED",
                    blocks_conversation=0,
                    runner_id=None,
                    reservation_expires_at_us=None,
                    provider_started_at_us=None,
                    execution_deadline_us=None,
                    lease_expires_at_us=None,
                    finished_at_us=1,
                    failure_category=None,
                )
                GlobalModelSlot.objects.filter(pk=1).update(
                    operation_id=None,
                    runner_id=None,
                    reservation_expires_at_us=None,
                    lease_expires_at_us=None,
                    acquired_at_us=None,
                )
                return print_row

            rows = [
                once("timeout", TimeoutError("timed out"), 1),
                once("dropped", ConnectionError("dropped"), 2),
                once("generic", RuntimeError("request failed"), 3),
            ]
            print(json.dumps(rows))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(len(payload), 3)
        for row in payload:
            self.assertEqual(row["type"], "UncertainRecorded", row["label"])
            self.assertEqual(row["provider"], 1, row["label"])
            self.assertEqual(row["failure_writes"], 1, row["label"])
            self.assertEqual(row["facts"]["status"], "RUNNING", row["label"])
            self.assertEqual(row["facts"]["blocks"], 1, row["label"])
            self.assertEqual(
                row["facts"]["failure"],
                "provider_outcome_uncertain",
                row["label"],
            )
            self.assertEqual(row["facts"]["lease"], 720_123_456, row["label"])
            self.assertEqual(row["facts"]["slot_lease"], 720_123_456, row["label"])
            self.assertEqual(row["facts"]["messages"], 0, row["label"])
            self.assertEqual(row["facts"]["version"], 0, row["label"])

    def test_ambiguous_success_commit_does_not_write_twice_when_durable(self):
        payload = self._ambiguous(success=True, durable=True)
        self.assertEqual(payload["apply"], "CommitAmbiguous")
        self.assertEqual(payload["type"], "DurableTurnCompleted")
        self.assertEqual(payload["provider"], 1)
        self.assertEqual(payload["apply_writes"], 1)
        self.assertEqual(payload["facts"]["status"], "SUCCEEDED")
        self.assertEqual(payload["facts"]["messages"], 2)
        self.assertEqual(payload["facts"]["version"], 1)

    def test_ambiguous_success_rollback_retries_persistence_once(self):
        payload = self._ambiguous(success=True, durable=False)
        self.assertEqual(payload["apply"], "CommitAmbiguous")
        self.assertEqual(payload["type"], "DurableTurnCompleted")
        self.assertEqual(payload["provider"], 1)
        self.assertEqual(payload["apply_writes"], 2)
        self.assertEqual(payload["facts"]["messages"], 2)
        self.assertEqual(payload["facts"]["version"], 1)

    def test_ambiguous_failure_commit_and_rollback(self):
        durable = self._ambiguous(success=False, durable=True)
        rolled = self._ambiguous(success=False, durable=False)
        self.assertEqual(durable["type"], "DurableFailureRecorded")
        self.assertEqual(durable["provider"], 1)
        self.assertEqual(durable["failure_writes"], 1)
        self.assertEqual(durable["facts"]["status"], "FAILED")
        self.assertEqual(durable["facts"]["messages"], 0)
        self.assertEqual(rolled["apply"], "CommitAmbiguous")
        self.assertEqual(rolled["type"], "DurableFailureRecorded")
        self.assertEqual(rolled["provider"], 1)
        self.assertEqual(rolled["failure_writes"], 2)
        self.assertEqual(rolled["facts"]["status"], "FAILED")
        self.assertEqual(rolled["facts"]["messages"], 0)

    def test_recovery_reconciles_once_into_durable_success(self):
        result = _run(
            """\
            class StepClock:
                def __init__(self):
                    self.calls = 0

                def now_us(self):
                    self.calls += 1
                    if self.calls < 4:
                        return CLAIMED_AT
                    return LEASE + 5

            repo = SqliteConversationRepository(StepClock(), RUNNER)

            def provide(box, **kwargs):
                SqliteConversationRepository(Clock(LEASE), RUNNER).recover_slot()
                box["during_provider_atomic"] = connection.in_atomic_block
                return "kept answer"

            handler, box = service(provide, repo)
            outcome = handler.submit(request())
            operation = Operation.objects.get(owner_id=OWNER.id)
            created = list(
                Message.objects.filter(operation_id=operation.id)
                .order_by("sequence")
                .values_list("created_at_us", flat=True)
            )
            again, again_box = service(provide, repository())
            second = again.submit(request())
            print(json.dumps({
                "type": type(outcome).__name__,
                "assistant": outcome.assistant_message.content,
                "provider": box["provider"],
                "apply_writes": box["apply"],
                "reconcile": box["reconcile"],
                "reconcile_atomic": box.get("reconcile_atomic"),
                "facts": facts(operation.id),
                "finished": operation.finished_at_us,
                "created": created,
                "second_type": type(second).__name__,
                "second_provider": again_box["provider"],
                "messages_after": Message.objects.count(),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "DurableTurnCompleted")
        self.assertEqual(payload["assistant"], "kept answer")
        self.assertEqual(payload["provider"], 1)
        self.assertEqual(payload["apply_writes"], 1)
        self.assertEqual(payload["reconcile"], 1)
        self.assertFalse(payload["reconcile_atomic"])
        self.assertEqual(payload["facts"]["status"], "SUCCEEDED")
        self.assertEqual(payload["facts"]["messages"], 2)
        self.assertEqual(payload["facts"]["version"], 1)
        self.assertIsNone(payload["facts"]["slot_operation"])
        self.assertEqual(payload["finished"], 720_123_456)
        self.assertEqual(payload["created"], [720_123_461, 720_123_461])
        self.assertEqual(payload["second_type"], "Existing")
        self.assertEqual(payload["second_provider"], 0)
        self.assertEqual(payload["messages_after"], 2)

    def test_ambiguous_reconciliation_commit_does_not_write_again(self):
        result = _run(
            """\
            class StepClock:
                def __init__(self):
                    self.calls = 0

                def now_us(self):
                    self.calls += 1
                    if self.calls < 4:
                        return CLAIMED_AT
                    return LEASE + 5

            repo = SqliteConversationRepository(StepClock(), RUNNER)
            real_reconcile = repo.reconcile_late_success

            def reconcile_late_success(retained):
                real_commit = connection.commit

                def commit(*args, **kwargs):
                    real_commit(*args, **kwargs)
                    raise OperationalError("simulated post-commit failure")

                connection.commit = commit
                try:
                    return real_reconcile(retained)
                finally:
                    connection.commit = real_commit

            repo.reconcile_late_success = reconcile_late_success

            def provide(box, **kwargs):
                SqliteConversationRepository(Clock(LEASE), RUNNER).recover_slot()
                return "kept answer"

            handler, box = service(provide, repo)
            outcome = handler.submit(request())
            print(json.dumps({
                "type": type(outcome).__name__,
                "assistant": outcome.assistant_message.content,
                "provider": box["provider"],
                "reconcile": box["reconcile"],
                "facts": facts(shown(outcome)),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "DurableTurnCompleted")
        self.assertEqual(payload["assistant"], "kept answer")
        self.assertEqual(payload["provider"], 1)
        self.assertEqual(payload["reconcile"], 1)
        self.assertEqual(payload["facts"]["status"], "SUCCEEDED")
        self.assertEqual(payload["facts"]["messages"], 2)
        self.assertEqual(payload["facts"]["version"], 1)

    def test_observed_failure_does_not_consume_retained_success(self):
        result = _run(
            """\
            repo = repository()
            real_failure = repo.apply_failure

            def apply_success(claimed, output_text):
                real_failure(
                    claimed,
                    DefiniteProviderCategory.PROVIDER_DEFINITE_FAILURE,
                )
                return CommitAmbiguous(
                    operation_id=claimed.operation_id,
                    runner_id=claimed.runner_id,
                    claimed_at=claimed.claimed_at,
                    conversation_id=claimed.conversation_id,
                )

            repo.apply_success = apply_success

            def provide(box, **kwargs):
                return "kept answer"

            handler, box = service(provide, repo)
            outcome = handler.submit(request())
            print(json.dumps({
                "type": type(outcome).__name__,
                "retained": outcome.retained_output,
                "provider": box["provider"],
                "apply_writes": box["apply"],
                "failure_writes": box["failure"],
                "reconcile": box["reconcile"],
                "facts": facts(shown(outcome)),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "OutcomeUnconfirmed")
        self.assertEqual(payload["retained"], "kept answer")
        self.assertEqual(payload["provider"], 1)
        self.assertEqual(payload["apply_writes"], 1)
        self.assertEqual(payload["failure_writes"], 0)
        self.assertEqual(payload["reconcile"], 0)
        self.assertEqual(payload["facts"]["status"], "FAILED")
        self.assertEqual(payload["facts"]["messages"], 0)
        self.assertEqual(payload["facts"]["version"], 0)

    def test_acknowledgement_blocks_late_success(self):
        result = _run(
            """\
            def provide(box, **kwargs):
                operation_id = GlobalModelSlot.objects.get(pk=1).operation_id
                late = SqliteConversationRepository(Clock(LEASE), RUNNER)
                late.recover_slot()
                late.acknowledge_unknown(OWNER.id, operation_id)
                return "kept answer"

            handler, box = service(provide)
            outcome = handler.submit(request())
            print(json.dumps({
                "type": type(outcome).__name__,
                "retained": getattr(outcome, "retained_output", None),
                "provider": box["provider"],
                "reconcile": box["reconcile"],
                "facts": facts(shown(outcome)),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "OutcomeUnconfirmed")
        self.assertEqual(payload["retained"], "kept answer")
        self.assertEqual(payload["provider"], 1)
        self.assertEqual(payload["reconcile"], 0)
        self.assertEqual(payload["facts"]["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(payload["facts"]["messages"], 0)
        self.assertEqual(payload["facts"]["version"], 0)

    def test_persistence_failure_retains_output_without_replay(self):
        result = _run(
            """\
            repo = repository()
            real_apply = repo.apply_success

            def broken(claimed, output_text):
                raise OperationalError("persistence failed")

            repo.apply_success = broken

            def provide(box, **kwargs):
                return "kept answer"

            handler, box = service(provide, repo)
            outcome = handler.submit(request())
            repo.apply_success = real_apply
            again, again_box = service(provide, repo)
            second = again.submit(request())
            print(json.dumps({
                "type": type(outcome).__name__,
                "retained": outcome.retained_output,
                "provider": box["provider"],
                "second_type": type(second).__name__,
                "second_provider": again_box["provider"],
                "facts": facts(shown(outcome)),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "OutcomeUnconfirmed")
        self.assertEqual(payload["retained"], "kept answer")
        self.assertEqual(payload["provider"], 1)
        self.assertEqual(payload["second_type"], "Existing")
        self.assertEqual(payload["second_provider"], 0)
        self.assertEqual(payload["facts"]["status"], "RUNNING")
        self.assertEqual(payload["facts"]["messages"], 0)

    def test_mismatched_claim_does_not_write(self):
        result = _run(
            """\
            def replace(claimed, **changes):
                values = {
                    "operation_id": claimed.operation_id,
                    "actor_id": claimed.actor_id,
                    "conversation_id": claimed.conversation_id,
                    "request_hash": claimed.request_hash,
                    "fingerprint_version": claimed.fingerprint_version,
                    "expected_version": claimed.expected_version,
                    "runner_id": claimed.runner_id,
                    "claimed_at": claimed.claimed_at,
                    "execution_deadline": claimed.execution_deadline,
                    "lease_expires_at": claimed.lease_expires_at,
                }
                values.update(changes)
                return Claimed(ClaimedOperation(**values))

            def run(label, key, change):
                repo = repository()
                real = repo.claim_running

                def claim_running(actor_id, operation_id):
                    result = real(actor_id, operation_id)
                    return replace(result.claimed_operation, **change)

                repo.claim_running = claim_running

                def provide(box, **kwargs):
                    if label == "stale":
                        Conversation.objects.filter(pk=CONVERSATION).update(version=4)
                    return "kept answer"

                handler, box = service(provide, repo)
                outcome = handler.submit(request(key))
                operation = Operation.objects.exclude(status="CANCELLED").get()
                row = {
                    "label": label,
                    "type": type(outcome).__name__,
                    "provider": box["provider"],
                    "apply_writes": box["apply"],
                    "messages": Message.objects.filter(operation_id=operation.id).count(),
                    "status": operation.status,
                }
                release_running()
                return row

            print(json.dumps([
                run("hash", 4, {"request_hash": "cd" * 32}),
                run("fingerprint", 5, {"fingerprint_version": 2}),
                run("owner", 6, {"actor_id": OWNER.id + 1}),
                run(
                    "operation",
                    7,
                    {"operation_id": uuid.UUID("22222222222222222222222222222222")},
                ),
                run("claimed_at", 8, {"claimed_at": CLAIMED_AT + 1}),
                run("runner", 9, {"runner_id": STALE}),
                run("stale", 10, {}),
            ]))
            """
        )
        payload = _payload(self, result)
        by_label = {row["label"]: row for row in payload}
        self.assertEqual(by_label["hash"]["type"], "OwnershipLost")
        self.assertEqual(by_label["hash"]["provider"], 1)
        self.assertEqual(by_label["hash"]["messages"], 0)
        self.assertEqual(by_label["hash"]["status"], "RUNNING")
        self.assertEqual(by_label["fingerprint"]["type"], "OwnershipLost")
        self.assertEqual(by_label["fingerprint"]["provider"], 1)
        self.assertEqual(by_label["fingerprint"]["messages"], 0)
        self.assertEqual(by_label["fingerprint"]["status"], "RUNNING")
        self.assertEqual(by_label["owner"]["type"], "Claimed")
        self.assertEqual(by_label["owner"]["provider"], 0)
        self.assertEqual(by_label["owner"]["apply_writes"], 0)
        self.assertEqual(by_label["operation"]["type"], "Claimed")
        self.assertEqual(by_label["operation"]["provider"], 0)
        self.assertEqual(by_label["operation"]["messages"], 0)
        self.assertEqual(by_label["claimed_at"]["type"], "OwnershipLost")
        self.assertEqual(by_label["claimed_at"]["messages"], 0)
        self.assertEqual(by_label["runner"]["type"], "Claimed")
        self.assertEqual(by_label["runner"]["provider"], 0)
        self.assertEqual(by_label["runner"]["apply_writes"], 0)
        self.assertEqual(by_label["stale"]["type"], "StaleVersion")
        self.assertEqual(by_label["stale"]["provider"], 1)
        self.assertEqual(by_label["stale"]["messages"], 0)
        self.assertEqual(by_label["stale"]["status"], "RUNNING")

    def _ambiguous(self, *, success, durable):
        mode = "durable" if durable else "rollback"
        kind = "success" if success else "failure"
        return _payload(self, _run(
            f"""\
            repo = repository()
            state = {{"writes": 0}}
            target = repo.apply_success if {success!r} else repo.apply_failure

            def wrapped(*args, **kwargs):
                state["writes"] += 1
                if state["writes"] > 1:
                    return target(*args, **kwargs)
                real_commit = connection.commit
                def commit(*commit_args, **commit_kwargs):
                    if {durable!r}:
                        real_commit(*commit_args, **commit_kwargs)
                        raise OperationalError("simulated post-commit failure")
                    raise OperationalError("simulated rollback before commit")
                connection.commit = commit
                try:
                    return target(*args, **kwargs)
                finally:
                    connection.commit = real_commit

            if {success!r}:
                repo.apply_success = wrapped
            else:
                repo.apply_failure = wrapped

            def provide(box, **kwargs):
                if {success!r}:
                    return "kept answer"
                return ProviderFailure(
                    DefiniteProviderCategory.PROVIDER_DEFINITE_FAILURE
                )

            handler, box = service(provide, repo)
            outcome = handler.submit(request())
            print(json.dumps({{
                "mode": {mode!r},
                "kind": {kind!r},
                "apply": "CommitAmbiguous",
                "type": type(outcome).__name__,
                "provider": box["provider"],
                "apply_writes": box["apply"],
                "failure_writes": box["failure"],
                "facts": facts(shown(outcome)),
            }}))
            """
        ))

    def test_production_database_is_absent(self):
        self.assertFalse(PRODUCTION_DATABASE.exists())
