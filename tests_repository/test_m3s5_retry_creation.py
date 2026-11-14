"""Disposable-database tests for explicit retry creation."""

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
import threading
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
from conversation_service import ConversationService
from max_smart_db.creation import SqliteConversationRepository
from max_smart_db.models import Conversation, IdempotencyKey, Operation
from max_smart_db.repository import (
    PreProviderFailureCategory,
    PrepareCommand,
    RetryCommand,
)

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

OWNER = User.objects.create(username="owner")
OTHER = User.objects.create(username="other")
CONVERSATION = uuid.UUID("00112233445566778899aabbccddeeff")
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
PREPARED_AT = 1_000_000
CONTENT = "hello\\r\\n  exact  "

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


def repository_with(clock, runner=RUNNER):
    return SqliteConversationRepository(clock, runner)


def service(repo):
    counts = {"provider": 0, "create": 0, "inspect": 0}
    real_create = repo.create_retry
    real_inspect = repo.inspect_committed_retry

    def create_retry(command):
        counts["create"] += 1
        return real_create(command)

    def inspect_committed_retry(*args, **kwargs):
        counts["inspect"] += 1
        return real_inspect(*args, **kwargs)

    repo.create_retry = create_retry
    repo.inspect_committed_retry = inspect_committed_retry

    def provide(**kwargs):
        counts["provider"] += 1
        return "model-output"

    return ConversationService(repo, lambda **kwargs: "standing", provide), counts


def prepare(repo, key, conversation=CONVERSATION, content=CONTENT, expected=0):
    prepared = repo.prepare(PrepareCommand(
        actor_id=OWNER.id,
        conversation_id=conversation,
        expected_version=expected,
        request_content=content,
        idempotency_key=key_for(key),
        provider_id="openai",
        model_id="gpt-5.6-sol",
    ))
    if type(prepared).__name__ != "Prepared":
        raise RuntimeError(type(prepared).__name__)
    return prepared.reservation


def fail_source(key=1):
    repo, clock = repository()
    reservation = prepare(repo, key)
    failed = repo.fail_pre_provider(
        reservation,
        PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
    )
    if type(failed).__name__ != "Failed":
        raise RuntimeError(type(failed).__name__)
    return repo, clock, reservation


def described(outcome):
    payload = {"type": type(outcome).__name__}
    warning = getattr(outcome, "duplicate_charge_warning", None)
    if warning is not None:
        payload["warning"] = warning
    retry_of = getattr(outcome, "retry_of", None)
    if retry_of is not None:
        payload["retry_of"] = str(retry_of)
    reservation = getattr(outcome, "reservation", None)
    if reservation is not None:
        payload["operation_id"] = str(reservation.operation_id)
        payload["runner"] = reservation.runner_id
    operation = getattr(outcome, "operation", None)
    if operation is not None:
        payload["operation_id"] = str(operation.operation_id)
        payload["status"] = operation.status.value
    if type(outcome).__name__ == "StaleVersion":
        payload["actual"] = outcome.actual
    if type(outcome).__name__ == "ConversationBlocked":
        payload["blocked_status"] = outcome.status.value
    return payload


def row(operation_id):
    operation = Operation.objects.get(pk=operation_id)
    return {
        "status": operation.status,
        "content": operation.request_content,
        "retry_of": None if operation.retry_of_id is None else str(operation.retry_of_id),
        "blocks": operation.blocks_conversation,
        "runner": operation.runner_id,
    }
"""


def _run(body, *, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "m3s5-retry.sqlite3"
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


class RetryCreationTests(unittest.TestCase):
    def test_failed_and_cancelled_sources_create_linear_retries(self):
        payload = _payload(self, _run(
            """\
            repo, clock, failed = fail_source(1)
            handler, counts = service(repo)
            first = handler.create_retry(
                OWNER.id,
                failed.operation_id,
                key_for(21),
                0,
            )
            again = handler.create_retry(
                OWNER.id,
                failed.operation_id,
                key_for(22),
                0,
            )
            retry_id = uuid.UUID(described(first)["operation_id"])
            repo.fail_pre_provider(
                first.reservation,
                PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
            )
            second = handler.create_retry(OWNER.id, retry_id, key_for(23), 0)
            print(json.dumps({
                "first": described(first),
                "again": described(again),
                "second": described(second),
                "counts": counts,
                "source_id": str(failed.operation_id),
                "source": row(failed.operation_id),
                "retry": row(retry_id),
                "child": row(uuid.UUID(described(second)["operation_id"])),
                "keys": IdempotencyKey.objects.count(),
            }))
            """
        ))
        self.assertEqual(payload["first"]["type"], "RetryPrepared")
        self.assertFalse(payload["first"]["warning"])
        self.assertEqual(payload["first"]["retry_of"], payload["source_id"])
        self.assertNotEqual(payload["first"]["operation_id"], payload["source_id"])
        self.assertEqual(payload["again"]["type"], "RetrySourceNotEligible")
        self.assertEqual(payload["second"]["type"], "RetryPrepared")
        self.assertEqual(payload["second"]["retry_of"], payload["first"]["operation_id"])
        self.assertFalse(payload["second"]["warning"])
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["source"]["status"], "FAILED")
        self.assertEqual(payload["source"]["content"], "hello\r\n  exact  ")
        self.assertIsNone(payload["source"]["retry_of"])
        self.assertEqual(payload["retry"]["content"], "hello\r\n  exact  ")
        self.assertEqual(payload["retry"]["retry_of"], payload["first"]["retry_of"])
        self.assertEqual(payload["child"]["retry_of"], payload["first"]["operation_id"])
        self.assertEqual(payload["keys"], 3)

    def test_cancelled_source_is_eligible_without_a_warning(self):
        payload = _payload(self, _run(
            """\
            repo, clock = repository()
            reservation = prepare(repo, 2)
            clock.value = reservation.reservation_expires_at
            cancelled = repo.recover_slot()
            handler, counts = service(repository_with(clock))
            outcome = handler.create_retry(
                OWNER.id,
                reservation.operation_id,
                key_for(24),
                0,
            )
            print(json.dumps({
                "cancelled": type(cancelled).__name__,
                "outcome": described(outcome),
                "counts": counts,
                "source_id": str(reservation.operation_id),
                "source": row(reservation.operation_id),
                "retry": row(uuid.UUID(described(outcome)["operation_id"])),
            }))
            """
        ))
        self.assertEqual(payload["cancelled"], "CancelledExpiredQueued")
        self.assertEqual(payload["outcome"]["type"], "RetryPrepared")
        self.assertFalse(payload["outcome"]["warning"])
        self.assertEqual(payload["outcome"]["retry_of"], payload["source_id"])
        self.assertEqual(payload["source"]["status"], "CANCELLED")
        self.assertEqual(payload["retry"]["content"], payload["source"]["content"])
        self.assertEqual(payload["retry"]["status"], "QUEUED")
        self.assertEqual(payload["counts"]["provider"], 0)

    def test_acknowledged_unknown_warns_about_a_duplicate_charge(self):
        payload = _payload(self, _run(
            """\
            repo, clock = repository()
            reservation = prepare(repo, 3)
            claimed = repo.claim_running(OWNER.id, reservation.operation_id)
            clock.value = claimed.claimed_operation.lease_expires_at
            repo.recover_slot()
            acknowledged = repo.acknowledge_unknown(OWNER.id, reservation.operation_id)
            handler, counts = service(repository_with(clock))
            outcome = handler.create_retry(
                OWNER.id,
                reservation.operation_id,
                key_for(25),
                0,
            )
            replay = handler.create_retry(
                OWNER.id,
                reservation.operation_id,
                key_for(25),
                0,
            )
            print(json.dumps({
                "acknowledged": type(acknowledged).__name__,
                "outcome": described(outcome),
                "replay": described(replay),
                "counts": counts,
                "source": row(reservation.operation_id),
                "keys": IdempotencyKey.objects.count(),
            }))
            """
        ))
        self.assertEqual(payload["acknowledged"], "Acknowledged")
        self.assertEqual(payload["outcome"]["type"], "RetryPrepared")
        self.assertTrue(payload["outcome"]["warning"])
        self.assertEqual(payload["replay"]["type"], "RetryExisting")
        self.assertTrue(payload["replay"]["warning"])
        self.assertEqual(payload["replay"]["operation_id"], payload["outcome"]["operation_id"])
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["counts"]["create"], 2)
        self.assertEqual(payload["source"]["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(payload["keys"], 2)

    def test_ineligible_sources_are_rejected(self):
        queued = _payload(self, _run(
            """\
            repo, clock = repository()
            reservation = prepare(repo, 4)
            handler, counts = service(repo)
            outcome = handler.create_retry(
                OWNER.id, reservation.operation_id, key_for(26), 0,
            )
            print(json.dumps({
                "type": type(outcome).__name__,
                "provider": counts["provider"],
                "operations": Operation.objects.count(),
                "status": row(reservation.operation_id)["status"],
            }))
            """
        ))
        running = _payload(self, _run(
            """\
            repo, clock = repository()
            reservation = prepare(repo, 5)
            claimed = repo.claim_running(OWNER.id, reservation.operation_id)
            handler, counts = service(repo)
            outcome = handler.create_retry(
                OWNER.id, reservation.operation_id, key_for(27), 0,
            )
            print(json.dumps({
                "claimed": type(claimed).__name__,
                "type": type(outcome).__name__,
                "provider": counts["provider"],
                "status": row(reservation.operation_id)["status"],
            }))
            """
        ))
        unknown = _payload(self, _run(
            """\
            repo, clock = repository()
            reservation = prepare(repo, 6)
            claimed = repo.claim_running(OWNER.id, reservation.operation_id)
            clock.value = claimed.claimed_operation.lease_expires_at
            recovered = repo.recover_slot()
            handler, counts = service(repository_with(clock))
            outcome = handler.create_retry(
                OWNER.id, reservation.operation_id, key_for(28), 0,
            )
            print(json.dumps({
                "recovered": type(recovered).__name__,
                "type": type(outcome).__name__,
                "provider": counts["provider"],
                "status": row(reservation.operation_id)["status"],
                "acknowledged": Operation.objects.get(
                    pk=reservation.operation_id
                ).acknowledged_at_us is not None,
            }))
            """
        ))
        succeeded = _payload(self, _run(
            """\
            repo, clock = repository()
            reservation = prepare(repo, 7)
            claimed = repo.claim_running(OWNER.id, reservation.operation_id)
            applied = repo.apply_success(
                claimed.claimed_operation,
                "model-output",
            )
            handler, counts = service(repo)
            outcome = handler.create_retry(
                OWNER.id, reservation.operation_id, key_for(29), 1,
            )
            print(json.dumps({
                "applied": type(applied).__name__,
                "type": type(outcome).__name__,
                "provider": counts["provider"],
                "status": row(reservation.operation_id)["status"],
            }))
            """
        ))
        for name, payload in (
            ("queued", queued),
            ("running", running),
            ("unknown", unknown),
            ("succeeded", succeeded),
        ):
            self.assertEqual(payload["type"], "RetrySourceNotEligible", name)
            self.assertEqual(payload["provider"], 0, name)
        self.assertEqual(queued["operations"], 1)
        self.assertEqual(queued["status"], "QUEUED")
        self.assertEqual(running["claimed"], "Claimed")
        self.assertEqual(running["status"], "RUNNING")
        self.assertEqual(unknown["recovered"], "MarkedOutcomeUnknown")
        self.assertEqual(unknown["status"], "OUTCOME_UNKNOWN")
        self.assertFalse(unknown["acknowledged"])
        self.assertEqual(succeeded["applied"], "Applied")
        self.assertEqual(succeeded["status"], "SUCCEEDED")

    def test_same_key_expired_binding_and_mismatch_are_preserved(self):
        payload = _payload(self, _run(
            """\
            repo, clock, failed = fail_source(8)
            original_key = IdempotencyKey.objects.get()
            handler, counts = service(repository_with(clock))
            mismatch = handler.create_retry(
                OWNER.id,
                failed.operation_id,
                key_for(8),
                0,
            )
            created = handler.create_retry(
                OWNER.id,
                failed.operation_id,
                key_for(30),
                0,
            )
            stored = IdempotencyKey.objects.get(
                operation_id=uuid.UUID(described(created)["operation_id"])
            )
            clock.value = stored.expires_at_us
            expired = handler.create_retry(
                OWNER.id,
                failed.operation_id,
                key_for(30),
                0,
            )
            print(json.dumps({
                "mismatch": described(mismatch),
                "created": described(created),
                "expired": described(expired),
                "counts": counts,
                "keys": IdempotencyKey.objects.count(),
                "original_expires": original_key.expires_at_us,
                "operations": Operation.objects.count(),
            }))
            """
        ))
        self.assertEqual(payload["mismatch"]["type"], "KeyBindingMismatch")
        self.assertEqual(payload["created"]["type"], "RetryPrepared")
        self.assertEqual(payload["expired"]["type"], "RetryExisting")
        self.assertFalse(payload["expired"]["warning"])
        self.assertEqual(
            payload["expired"]["operation_id"],
            payload["created"]["operation_id"],
        )
        self.assertEqual(payload["keys"], 2)
        self.assertEqual(payload["operations"], 2)
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertGreater(payload["original_expires"], 1_000_000)

    def test_version_and_active_slot_conflicts_do_not_create_a_retry(self):
        payload = _payload(self, _run(
            """\
            repo, clock, failed = fail_source(9)
            handler, counts = service(repo)
            stale = handler.create_retry(
                OWNER.id, failed.operation_id, key_for(31), 4,
            )
            other = uuid.uuid4()
            Conversation.objects.create(
                id=other,
                owner_id=OWNER.id,
                role_id="max-smart",
                version=0,
                next_sequence=1,
                title=None,
                created_at_us=0,
                updated_at_us=0,
            )
            prepare(repo, 32, conversation=other)
            busy = handler.create_retry(
                OWNER.id, failed.operation_id, key_for(33), 0,
            )
            print(json.dumps({
                "stale": described(stale),
                "busy": described(busy),
                "counts": counts,
                "retries": Operation.objects.exclude(retry_of_id=None).count(),
            }))
            """
        ))
        self.assertEqual(payload["stale"]["type"], "StaleVersion")
        self.assertEqual(payload["stale"]["actual"], 0)
        self.assertEqual(payload["busy"]["type"], "ModelBusy")
        self.assertEqual(payload["retries"], 0)
        self.assertEqual(payload["counts"]["provider"], 0)

    def test_conversation_blocker_rejects_retry_creation(self):
        payload = _payload(self, _run(
            """\
            repo, clock, failed = fail_source(10)
            prepare(repo, 34)
            handler, counts = service(repo)
            blocked = handler.create_retry(
                OWNER.id, failed.operation_id, key_for(35), 0,
            )
            print(json.dumps({
                "blocked": described(blocked),
                "counts": counts,
                "retries": Operation.objects.exclude(retry_of_id=None).count(),
            }))
            """
        ))
        self.assertEqual(payload["blocked"]["type"], "ConversationBlocked")
        self.assertEqual(payload["blocked"]["blocked_status"], "QUEUED")
        self.assertEqual(payload["retries"], 0)
        self.assertEqual(payload["counts"]["provider"], 0)

    def test_concurrent_retry_creation_allows_one_child(self):
        payload = _payload(self, _run(
            """\
            repo, clock, failed = fail_source(11)
            barrier = threading.Barrier(2)
            outcomes = []

            def attempt(key):
                worker, worker_clock = repository()
                handler, counts = service(worker)
                barrier.wait()
                outcome = handler.create_retry(
                    OWNER.id,
                    failed.operation_id,
                    key_for(key),
                    0,
                )
                outcomes.append({
                    "type": type(outcome).__name__,
                    "provider": counts["provider"],
                })

            threads = [
                threading.Thread(target=attempt, args=(40,)),
                threading.Thread(target=attempt, args=(41,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            print(json.dumps({
                "outcomes": outcomes,
                "children": Operation.objects.exclude(retry_of_id=None).count(),
                "source": row(failed.operation_id)["status"],
            }))
            """
        ))
        kinds = sorted(item["type"] for item in payload["outcomes"])
        self.assertEqual(kinds.count("RetryPrepared"), 1)
        self.assertIn(
            kinds[0] if kinds[1] == "RetryPrepared" else kinds[1],
            ("ModelBusy", "RetrySourceNotEligible"),
        )
        self.assertEqual(
            [item["provider"] for item in payload["outcomes"]],
            [0, 0],
        )
        self.assertEqual(payload["children"], 1)
        self.assertEqual(payload["source"], "FAILED")

    def test_ambiguous_commit_is_reconciled_without_a_second_write(self):
        committed = self._ambiguous(True)
        self.assertEqual(committed["outcome"]["type"], "RetryPrepared")
        self.assertFalse(committed["outcome"]["warning"])
        self.assertEqual(committed["counts"]["create"], 1)
        self.assertEqual(committed["counts"]["inspect"], 1)
        self.assertEqual(committed["counts"]["provider"], 0)
        self.assertEqual(committed["children"], 1)
        self.assertEqual(committed["retry"]["content"], "hello\r\n  exact  ")
        self.assertTrue(committed["key_matches"])

    def test_ambiguous_rollback_is_not_retried(self):
        rolled = self._ambiguous(False)
        self.assertEqual(rolled["outcome"]["type"], "RetryUnconfirmed")
        self.assertEqual(rolled["counts"]["create"], 1)
        self.assertEqual(rolled["counts"]["inspect"], 1)
        self.assertEqual(rolled["counts"]["provider"], 0)
        self.assertEqual(rolled["children"], 0)
        self.assertEqual(rolled["source"]["status"], "FAILED")
        self.assertFalse(rolled["key_matches"])

    def test_busy_and_cross_owner_fail_closed(self):
        payload = _payload(self, _run(
            """\
            repo, clock, failed = fail_source(13)
            handler, counts = service(repo)
            holder = sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True)
            try:
                holder.execute("BEGIN IMMEDIATE")
                busy = handler.create_retry(
                    OWNER.id, failed.operation_id, key_for(42), 0,
                )
            finally:
                holder.rollback()
                holder.close()
            foreign = handler.create_retry(
                OTHER.id, failed.operation_id, key_for(43), 0,
            )
            missing = handler.create_retry(
                OWNER.id, uuid.uuid4(), key_for(44), 0,
            )
            print(json.dumps({
                "busy": type(busy).__name__,
                "foreign": type(foreign).__name__,
                "foreign_text": repr(foreign),
                "missing": type(missing).__name__,
                "missing_text": repr(missing),
                "counts": counts,
                "children": Operation.objects.exclude(retry_of_id=None).count(),
            }))
            """
        ))
        self.assertEqual(payload["busy"], "DatabaseBusy")
        self.assertEqual(payload["foreign"], "NotFound")
        self.assertEqual(payload["missing"], "NotFound")
        self.assertNotIn("exact", payload["foreign_text"])
        self.assertNotIn("exact", payload["missing_text"])
        self.assertEqual(payload["children"], 0)
        self.assertEqual(payload["counts"]["provider"], 0)

    def test_caller_transaction_is_left_open(self):
        payload = _payload(self, _run(
            """\
            repo, clock, failed = fail_source(14)
            handler, counts = service(repo)
            with transaction.atomic():
                outcome = handler.create_retry(
                    OWNER.id, failed.operation_id, key_for(45), 0,
                )
                open_during = connection.in_atomic_block
            print(json.dumps({
                "type": type(outcome).__name__,
                "open_during": open_during,
                "autocommit": connection.get_autocommit(),
                "counts": counts,
                "children": Operation.objects.exclude(retry_of_id=None).count(),
            }))
            """
        ))
        self.assertEqual(payload["type"], "TransactionHeld")
        self.assertTrue(payload["open_during"])
        self.assertTrue(payload["autocommit"])
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["counts"]["create"], 0)
        self.assertEqual(payload["children"], 0)

    def test_committed_proof_requires_the_exact_key_binding(self):
        payload = _payload(self, _run(
            """\
            repo, clock, failed = fail_source(90)
            handler, counts = service(repo)
            created = handler.create_retry(
                OWNER.id, failed.operation_id, key_for(91), 0,
            )
            retry_id = uuid.UUID(described(created)["operation_id"])
            before = Operation.objects.count()

            def proof(key, actor=OWNER.id):
                outcome = repo.inspect_committed_retry(
                    actor,
                    retry_id,
                    failed.operation_id,
                    RUNNER,
                    key,
                )
                return type(outcome).__name__

            exact = proof(key_for(91))
            other_operation = proof(key_for(90))
            wrong_digest = proof(key_for(92))
            malformed = proof("AB")
            short_key = proof("A" * 10)
            foreign_actor = proof(key_for(91), actor=OTHER.id)
            IdempotencyKey.objects.filter(operation_id=retry_id).delete()
            missing = proof(key_for(91))
            print(json.dumps({
                "created": described(created),
                "exact": exact,
                "other_operation": other_operation,
                "wrong_digest": wrong_digest,
                "malformed": malformed,
                "short_key": short_key,
                "foreign_actor": foreign_actor,
                "missing": missing,
                "counts": counts,
                "operations": Operation.objects.count(),
                "before": before,
                "content": row(retry_id)["content"],
                "retry_of": row(retry_id)["retry_of"],
            }))
            """
        ))
        self.assertEqual(payload["created"]["type"], "RetryPrepared")
        self.assertFalse(payload["created"]["warning"])
        self.assertEqual(payload["exact"], "RetryCreationObserved")
        self.assertEqual(payload["other_operation"], "RetryCreationNotProven")
        self.assertEqual(payload["wrong_digest"], "RetryCreationNotProven")
        self.assertEqual(payload["malformed"], "RetryCreationNotProven")
        self.assertEqual(payload["short_key"], "RetryCreationNotProven")
        self.assertEqual(payload["foreign_actor"], "RetryCreationNotProven")
        self.assertEqual(payload["missing"], "RetryCreationNotProven")
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["counts"]["create"], 1)
        self.assertEqual(payload["operations"], payload["before"])
        self.assertEqual(payload["content"], "hello\r\n  exact  ")
        self.assertEqual(payload["retry_of"], payload["created"]["retry_of"])

    def test_expired_binding_still_proves_the_original_retry(self):
        payload = _payload(self, _run(
            """\
            repo, clock, failed = fail_source(93)
            handler, counts = service(repository_with(clock))
            created = handler.create_retry(
                OWNER.id, failed.operation_id, key_for(94), 0,
            )
            retry_id = uuid.UUID(described(created)["operation_id"])
            stored = IdempotencyKey.objects.get(operation_id=retry_id)
            clock.value = stored.expires_at_us
            proof = repo.inspect_committed_retry(
                OWNER.id,
                retry_id,
                failed.operation_id,
                RUNNER,
                key_for(94),
            )
            replay = handler.create_retry(
                OWNER.id, failed.operation_id, key_for(94), 0,
            )
            print(json.dumps({
                "created_id": str(retry_id),
                "proof": type(proof).__name__,
                "warning": proof.duplicate_charge_warning,
                "replay": described(replay),
                "expired": stored.expires_at_us <= clock.value,
                "keys": IdempotencyKey.objects.count(),
                "children": Operation.objects.exclude(retry_of_id=None).count(),
                "counts": counts,
            }))
            """
        ))
        self.assertEqual(payload["proof"], "RetryCreationObserved")
        self.assertFalse(payload["warning"])
        self.assertTrue(payload["expired"])
        self.assertEqual(payload["replay"]["type"], "RetryExisting")
        self.assertFalse(payload["replay"]["warning"])
        self.assertEqual(payload["replay"]["operation_id"], payload["created_id"])
        self.assertEqual(payload["keys"], 2)
        self.assertEqual(payload["children"], 1)
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["counts"]["create"], 2)

    def test_missing_binding_after_an_ambiguous_commit_is_not_proof(self):
        payload = _payload(self, _run(
            """\
            repo, clock, failed = fail_source(95)
            real_create = repo.create_retry
            real_inspect = repo.inspect_committed_retry
            counts = {"create": 0, "inspect": 0, "provider": 0}

            def create_retry(command):
                counts["create"] += 1
                real_commit = connection.commit

                def commit(*args, **kwargs):
                    real_commit(*args, **kwargs)
                    raise OperationalError("simulated retry commit")

                connection.commit = commit
                try:
                    return real_create(command)
                finally:
                    connection.commit = real_commit

            def inspect_committed_retry(*args, **kwargs):
                counts["inspect"] += 1
                IdempotencyKey.objects.filter(operation_id=args[1]).delete()
                return real_inspect(*args, **kwargs)

            repo.create_retry = create_retry
            repo.inspect_committed_retry = inspect_committed_retry
            handler = ConversationService(
                repo,
                lambda **kwargs: "standing",
                lambda **kwargs: counts.__setitem__("provider", counts["provider"] + 1),
            )
            outcome = handler.create_retry(
                OWNER.id, failed.operation_id, key_for(96), 0,
            )
            print(json.dumps({
                "outcome": type(outcome).__name__,
                "counts": counts,
                "children": Operation.objects.exclude(retry_of_id=None).count(),
                "keys_for_retry": IdempotencyKey.objects.exclude(
                    operation_id=failed.operation_id,
                ).count(),
            }))
            """
        ))
        self.assertEqual(payload["outcome"], "RetryUnconfirmed")
        self.assertEqual(payload["counts"]["create"], 1)
        self.assertEqual(payload["counts"]["inspect"], 1)
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["children"], 1)
        self.assertEqual(payload["keys_for_retry"], 0)

    def test_ambiguous_unknown_preserves_the_duplicate_charge_warning(self):
        payload = _payload(self, _run(
            """\
            from max_smart_db.idempotency import idempotency_key_digest
            from max_smart_db.models import GlobalModelSlot
            repo, clock = repository()
            reservation = prepare(repo, 97)
            claimed = repo.claim_running(OWNER.id, reservation.operation_id)
            clock.value = claimed.claimed_operation.lease_expires_at
            recovered = repo.recover_slot()
            acknowledged = repo.acknowledge_unknown(
                OWNER.id, reservation.operation_id,
            )
            real_create = repo.create_retry
            counts = {"create": 0, "inspect": 0, "provider": 0}

            def create_retry(command):
                counts["create"] += 1
                if counts["create"] > 1:
                    return real_create(command)
                real_commit = connection.commit

                def commit(*args, **kwargs):
                    real_commit(*args, **kwargs)
                    raise OperationalError("simulated retry commit")

                connection.commit = commit
                try:
                    return real_create(command)
                finally:
                    connection.commit = real_commit

            real_inspect = repo.inspect_committed_retry

            def inspect_committed_retry(*args, **kwargs):
                counts["inspect"] += 1
                return real_inspect(*args, **kwargs)
            repo.create_retry = create_retry
            repo.inspect_committed_retry = inspect_committed_retry
            handler = ConversationService(
                repo,
                lambda **kwargs: "standing",
                lambda **kwargs: counts.__setitem__("provider", counts["provider"] + 1),
            )
            outcome = handler.create_retry(
                OWNER.id, reservation.operation_id, key_for(98), 0,
            )
            retry = Operation.objects.exclude(retry_of_id=None).get()
            stored = IdempotencyKey.objects.get(operation_id=retry.id)
            slot = GlobalModelSlot.objects.get(pk=1)
            print(json.dumps({
                "recovered": type(recovered).__name__,
                "acknowledged": type(acknowledged).__name__,
                "outcome": described(outcome),
                "counts": counts,
                "content_equal": retry.request_content == row(
                    reservation.operation_id
                )["content"],
                "retry_of": str(retry.retry_of_id),
                "source_id": str(reservation.operation_id),
                "key_matches": (
                    stored.key_digest == idempotency_key_digest(key_for(98))
                    and stored.owner_id == OWNER.id
                    and stored.operation_id == retry.id
                ),
                "slot_operation": str(slot.operation_id),
                "slot_runner": slot.runner_id,
                "children": Operation.objects.exclude(retry_of_id=None).count(),
            }))
            """
        ))
        self.assertEqual(payload["recovered"], "MarkedOutcomeUnknown")
        self.assertEqual(payload["acknowledged"], "Acknowledged")
        self.assertEqual(payload["outcome"]["type"], "RetryPrepared")
        self.assertTrue(payload["outcome"]["warning"])
        self.assertEqual(payload["outcome"]["retry_of"], payload["source_id"])
        self.assertEqual(payload["retry_of"], payload["source_id"])
        self.assertTrue(payload["content_equal"])
        self.assertTrue(payload["key_matches"])
        self.assertEqual(payload["slot_operation"], payload["outcome"]["operation_id"])
        self.assertEqual(payload["slot_runner"], payload["outcome"]["runner"])
        self.assertEqual(payload["counts"]["create"], 1)
        self.assertEqual(payload["counts"]["inspect"], 1)
        self.assertEqual(payload["counts"]["provider"], 0)
        self.assertEqual(payload["children"], 1)

    def test_source_and_production_database(self):
        source = SERVICE_SOURCE.read_text()
        method = source.split("def create_retry", 1)[1].split(
            "def _resolve_ambiguous_retry",
            1,
        )[0]
        self.assertNotIn("_provider", method)
        self.assertNotIn("acknowledge_unknown", source)
        self.assertNotIn("delete_expired_idempotency", source)
        self.assertEqual(source.count("recover_slot"), 1)
        self.assertFalse(PRODUCTION_DATABASE.exists())

    def _ambiguous(self, durable):
        return _payload(self, _run(
            f"""\
            repo, clock, failed = fail_source(12)
            real_create = repo.create_retry
            counts = {{"create": 0, "inspect": 0, "provider": 0}}

            def create_retry(command):
                counts["create"] += 1
                if counts["create"] > 1:
                    return real_create(command)
                real_commit = connection.commit

                def commit(*args, **kwargs):
                    if {durable!r}:
                        real_commit(*args, **kwargs)
                    raise OperationalError("simulated retry commit")

                connection.commit = commit
                try:
                    return real_create(command)
                finally:
                    connection.commit = real_commit

            def inspect_committed_retry(*args, **kwargs):
                counts["inspect"] += 1
                return repo_inspect(*args, **kwargs)

            repo_inspect = repo.inspect_committed_retry
            repo.create_retry = create_retry
            repo.inspect_committed_retry = inspect_committed_retry
            handler = ConversationService(
                repo,
                lambda **kwargs: "standing",
                lambda **kwargs: counts.__setitem__("provider", counts["provider"] + 1),
            )
            outcome = handler.create_retry(
                OWNER.id,
                failed.operation_id,
                key_for(46),
                0,
            )
            from max_smart_db.idempotency import idempotency_key_digest
            retry = Operation.objects.exclude(retry_of_id=None).first()
            stored = None if retry is None else IdempotencyKey.objects.filter(
                operation_id=retry.id,
            ).first()
            print(json.dumps({{
                "outcome": described(outcome),
                "counts": counts,
                "children": Operation.objects.exclude(retry_of_id=None).count(),
                "source": row(failed.operation_id),
                "retry": None if retry is None else row(retry.id),
                "key_matches": (
                    stored is not None
                    and stored.key_digest == idempotency_key_digest(key_for(46))
                    and stored.owner_id == OWNER.id
                    and stored.operation_id == retry.id
                ),
            }}))
            """
        ))
