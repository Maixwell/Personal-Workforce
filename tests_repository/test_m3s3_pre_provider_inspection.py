"""Disposable-database proof of one pre-provider reservation."""

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
from django.db import OperationalError, connection, connections
from django.db.migrations.executor import MigrationExecutor
from max_smart_db.creation import SqliteConversationRepository
from max_smart_db.models import Conversation, GlobalModelSlot, Message, Operation
from max_smart_db.repository import (
    DefiniteProviderCategory,
    PreProviderFailureCategory,
    QueuedReservation,
)

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

OWNER = User.objects.create(username="owner")
OTHER = User.objects.create(username="other")
CONVERSATION = uuid.UUID("00112233445566778899aabbccddeeff")
OPERATION = uuid.UUID("11111111111111111111111111111111")
ALT_OPERATION = uuid.UUID("22222222222222222222222222222222")
MISSING = uuid.UUID("33333333333333333333333333333333")
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
STALE = "BBBBBBBBBBBBBBBBBBBBBQ"
HASH = "ab" * 32
RESERVATION = 1_000_000_000

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


def repository(now=123_456, runner=RUNNER):
    return SqliteConversationRepository(Clock(now), runner)


def queued(identifier=OPERATION, runner=RUNNER):
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
        reservation_expires_at_us=RESERVATION,
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
        reservation_expires_at_us=RESERVATION,
        lease_expires_at_us=None,
        acquired_at_us=7,
    )


def token(identifier=OPERATION):
    return QueuedReservation(
        operation_id=identifier,
        actor_id=OWNER.id,
        conversation_id=CONVERSATION,
        request_hash=HASH,
        fingerprint_version=1,
        expected_version=0,
        runner_id=RUNNER,
        reservation_expires_at=RESERVATION,
    )


def described(outcome):
    payload = {"type": type(outcome).__name__}
    if payload["type"] == "PreProviderFailureObserved":
        payload["operation_id"] = str(outcome.operation_id)
        payload["failure_category"] = outcome.failure_category.value
    elif payload["type"] == "QueuedReservationObserved":
        payload["operation_id"] = str(outcome.operation_id)
        payload["runner_id"] = outcome.runner_id
        payload["reservation_expires_at"] = outcome.reservation_expires_at
    return payload


def inspect(repo, actor_id, operation_id, runner_id):
    before = repo.clock.calls
    outcome = repo.inspect_pre_provider_failure(
        actor_id,
        operation_id,
        runner_id,
    )
    return described(outcome), repo.clock.calls - before


def release_slot():
    GlobalModelSlot.objects.filter(pk=1).update(
        operation_id=None,
        runner_id=None,
        reservation_expires_at_us=None,
        lease_expires_at_us=None,
        acquired_at_us=None,
    )
"""


def _run(body, *, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "m3s3-pre-provider.sqlite3"
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


class PreProviderInspectionTests(unittest.TestCase):
    def test_committed_pre_provider_failure_is_proven(self):
        result = _run(
            """\
            repo = repository(50)
            proofs = []
            first = None
            for category in PreProviderFailureCategory:
                identifier = uuid.uuid4()
                if first is None:
                    first = identifier
                queued(identifier)
                failed = repo.fail_pre_provider(token(identifier), category)
                proof, delta = inspect(repo, OWNER.id, identifier, STALE)
                operation = Operation.objects.get(pk=identifier)
                slot = GlobalModelSlot.objects.get(pk=1)
                proofs.append({
                    "failed": type(failed).__name__,
                    "proof": proof,
                    "clock_delta": delta,
                    "status": operation.status,
                    "started": operation.provider_started_at_us,
                    "messages": Message.objects.filter(
                        operation_id=identifier
                    ).count(),
                    "slot_operation": (
                        None if slot.operation_id is None
                        else str(slot.operation_id)
                    ),
                })
            queued(ALT_OPERATION)
            still_proven, _delta = inspect(repo, OWNER.id, first, STALE)
            print(json.dumps({
                "proofs": proofs,
                "still_proven": still_proven,
            }))
            """
        )
        payload = _payload(self, result)
        proofs = payload["proofs"]
        self.assertEqual(
            [row["proof"]["failure_category"] for row in proofs],
            [
                "context_unavailable",
                "context_budget_exceeded",
                "model_configuration_invalid",
            ],
        )
        self.assertEqual(
            payload["still_proven"]["type"],
            "PreProviderFailureObserved",
        )
        self.assertEqual(
            payload["still_proven"]["failure_category"],
            "context_unavailable",
        )
        for row in proofs:
            self.assertEqual(row["failed"], "Failed")
            self.assertEqual(row["proof"]["type"], "PreProviderFailureObserved")
            self.assertEqual(row["clock_delta"], 0)
            self.assertEqual(row["status"], "FAILED")
            self.assertIsNone(row["started"])
            self.assertEqual(row["messages"], 0)
            self.assertIsNone(row["slot_operation"])

    def test_ambiguous_failure_commit_and_rollback(self):
        durable = self._ambiguous(True)
        rolled = self._ambiguous(False)
        self.assertEqual(durable["failed"], "CommitAmbiguous")
        self.assertEqual(durable["proof"]["type"], "PreProviderFailureObserved")
        self.assertEqual(
            durable["proof"]["failure_category"],
            "context_unavailable",
        )
        self.assertEqual(durable["clock_delta"], 0)
        self.assertEqual(durable["status"], "FAILED")
        self.assertIsNone(durable["slot_operation"])
        self.assertEqual(rolled["failed"], "CommitAmbiguous")
        self.assertEqual(rolled["proof"]["type"], "QueuedReservationObserved")
        self.assertEqual(rolled["proof"]["runner_id"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(rolled["proof"]["reservation_expires_at"], 1_000_000_000)
        self.assertEqual(rolled["clock_delta"], 0)
        self.assertEqual(rolled["status"], "QUEUED")
        self.assertEqual(rolled["slot_operation"], str(
            __import__("uuid").UUID("11111111111111111111111111111111")
        ))

    def test_queued_reservation_requires_exact_slot_correspondence(self):
        result = _run(
            """\
            queued()
            repo = repository()
            proven, delta = inspect(repo, OWNER.id, OPERATION, RUNNER)
            wrong_runner, _delta = inspect(repo, OWNER.id, OPERATION, STALE)
            release_slot()
            released, _delta = inspect(repo, OWNER.id, OPERATION, RUNNER)
            print(json.dumps({
                "proven": proven,
                "clock_delta": delta,
                "wrong_runner": wrong_runner,
                "released": released,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["proven"]["type"], "QueuedReservationObserved")
        self.assertEqual(payload["proven"]["runner_id"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(payload["proven"]["reservation_expires_at"], 1_000_000_000)
        self.assertEqual(payload["clock_delta"], 0)
        self.assertEqual(payload["wrong_runner"]["type"], "PreProviderStateNotProven")
        self.assertEqual(payload["released"]["type"], "PreProviderStateNotProven")

    def test_identity_category_timing_and_messages_fail_closed(self):
        result = _run(
            """\
            queued()
            repo = repository(50)
            wrong_actor, _delta = inspect(repo, OTHER.id, OPERATION, RUNNER)
            missing, _delta = inspect(repo, OWNER.id, MISSING, RUNNER)
            failed = repo.fail_pre_provider(
                token(),
                PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
            )
            Message.objects.create(
                id=uuid.UUID("55555555555555555555555555555555"),
                conversation_id=CONVERSATION,
                sequence=1,
                role="user",
                content="hello",
                operation_id=OPERATION,
                created_at_us=50,
            )
            linked, _delta = inspect(repo, OWNER.id, OPERATION, RUNNER)
            Operation.objects.create(
                id=ALT_OPERATION,
                conversation_id=CONVERSATION,
                owner_id=OWNER.id,
                operation_type="message",
                provider_id="openai",
                model_id="gpt-5.6-sol",
                status="FAILED",
                blocks_conversation=0,
                expected_version=0,
                request_content="hello",
                fingerprint_version=1,
                request_hash=HASH,
                retry_of=None,
                runner_id=None,
                reservation_expires_at_us=None,
                provider_started_at_us=None,
                execution_deadline_us=None,
                lease_expires_at_us=None,
                created_at_us=1,
                finished_at_us=50,
                acknowledged_at_us=None,
                failure_category="provider_definite_failure",
            )
            wrong_category, _delta = inspect(repo, OWNER.id, ALT_OPERATION, RUNNER)
            timed = uuid.UUID("44444444444444444444444444444444")
            Operation.objects.create(
                id=timed,
                conversation_id=CONVERSATION,
                owner_id=OWNER.id,
                operation_type="message",
                provider_id="openai",
                model_id="gpt-5.6-sol",
                status="FAILED",
                blocks_conversation=0,
                expected_version=0,
                request_content="hello",
                fingerprint_version=1,
                request_hash=HASH,
                retry_of=None,
                runner_id=None,
                reservation_expires_at_us=None,
                provider_started_at_us=123_456,
                execution_deadline_us=600_123_456,
                lease_expires_at_us=720_123_456,
                created_at_us=1,
                finished_at_us=123_456,
                acknowledged_at_us=None,
                failure_category="context_unavailable",
            )
            wrong_timing, _delta = inspect(repo, OWNER.id, timed, RUNNER)
            print(json.dumps({
                "failed": type(failed).__name__,
                "wrong_actor": wrong_actor,
                "missing": missing,
                "linked": linked,
                "wrong_category": wrong_category,
                "wrong_timing": wrong_timing,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["failed"], "Failed")
        for label in (
            "wrong_actor",
            "missing",
            "linked",
            "wrong_category",
            "wrong_timing",
        ):
            self.assertEqual(
                payload[label]["type"],
                "PreProviderStateNotProven",
                label,
            )
        self.assertNotIn("hello", json.dumps(payload["wrong_actor"]))

    def test_adoption_and_recovery_remove_the_original_reservation(self):
        result = _run(
            """\
            queued()
            adopted = repository(123_456, STALE).adopt_queued(OWNER.id, OPERATION)
            former, _delta = inspect(repository(), OWNER.id, OPERATION, RUNNER)
            current, _delta = inspect(repository(), OWNER.id, OPERATION, STALE)
            release_slot()
            Operation.objects.filter(pk=OPERATION).update(
                status="CANCELLED",
                blocks_conversation=0,
                runner_id=None,
                reservation_expires_at_us=None,
                finished_at_us=RESERVATION,
            )
            cancelled, _delta = inspect(repository(), OWNER.id, OPERATION, STALE)
            print(json.dumps({
                "adopted": type(adopted).__name__,
                "former": former,
                "current": current,
                "cancelled": cancelled,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["adopted"], "Adopted")
        self.assertEqual(payload["former"]["type"], "PreProviderStateNotProven")
        self.assertEqual(payload["current"]["type"], "QueuedReservationObserved")
        self.assertEqual(payload["current"]["runner_id"], "BBBBBBBBBBBBBBBBBBBBBQ")
        self.assertEqual(payload["cancelled"]["type"], "PreProviderStateNotProven")

    def test_recovery_cancels_the_expired_reservation(self):
        result = _run(
            """\
            queued()
            recovered = repository(RESERVATION).recover_slot()
            proof, delta = inspect(repository(), OWNER.id, OPERATION, RUNNER)
            operation = Operation.objects.get(pk=OPERATION)
            slot = GlobalModelSlot.objects.get(pk=1)
            print(json.dumps({
                "recovered": type(recovered).__name__,
                "proof": proof,
                "clock_delta": delta,
                "status": operation.status,
                "slot_operation": (
                    None if slot.operation_id is None else str(slot.operation_id)
                ),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["recovered"], "CancelledExpiredQueued")
        self.assertEqual(payload["proof"]["type"], "PreProviderStateNotProven")
        self.assertEqual(payload["clock_delta"], 0)
        self.assertEqual(payload["status"], "CANCELLED")
        self.assertIsNone(payload["slot_operation"])

    def test_provider_boundary_states_are_not_pre_provider_failures(self):
        result = _run(
            """\
            queued()
            repo = repository(123_456)
            claimed = repo.claim_running(OWNER.id, OPERATION)
            running, _delta = inspect(repo, OWNER.id, OPERATION, RUNNER)
            failed = repo.apply_failure(
                claimed.claimed_operation,
                DefiniteProviderCategory.PROVIDER_DEFINITE_FAILURE,
            )
            provider_failed, _delta = inspect(repo, OWNER.id, OPERATION, RUNNER)
            print(json.dumps({
                "claimed": type(claimed).__name__,
                "running": running,
                "failed": type(failed).__name__,
                "provider_failed": provider_failed,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["claimed"], "Claimed")
        self.assertEqual(payload["running"]["type"], "PreProviderStateNotProven")
        self.assertEqual(payload["failed"], "DefinitelyFailed")
        self.assertEqual(
            payload["provider_failed"]["type"],
            "PreProviderStateNotProven",
        )

    def test_caller_transactions_do_not_become_durable_evidence(self):
        result = _run(
            """\
            queued()
            repo = repository()
            real_create = connections.create_connection

            def failing_create(alias):
                owned = real_create(alias)
                real_cursor = owned.cursor

                def cursor(*args, **kwargs):
                    wrapped = real_cursor(*args, **kwargs)
                    real_execute = wrapped.execute

                    def execute(sql, params=None):
                        if "failure_category" in str(sql):
                            raise OperationalError("simulated incomplete read")
                        if params is None:
                            return real_execute(sql)
                        return real_execute(sql, params)

                    wrapped.execute = execute
                    return wrapped

                owned.cursor = cursor
                return owned

            connection.ensure_connection()
            before_failed = id(connection.connection)
            connections.create_connection = failing_create
            try:
                failed_outcome = repo.inspect_pre_provider_failure(
                    OWNER.id, OPERATION, RUNNER,
                )
            finally:
                connections.create_connection = real_create
            failed_same = id(connection.connection) == before_failed

            connection.set_autocommit(False)
            write_identity = id(connection.connection)
            write_cursor = connection.cursor()
            write_cursor.execute("BEGIN IMMEDIATE")
            write_cursor.execute(
                "UPDATE operation SET status = 'FAILED', "
                "blocks_conversation = 0, runner_id = NULL, "
                "reservation_expires_at_us = NULL, finished_at_us = 50, "
                "failure_category = 'context_unavailable' WHERE id = ?",
                (OPERATION.hex,),
            )
            write_cursor.execute(
                "SELECT status FROM operation WHERE id = ?",
                (OPERATION.hex,),
            )
            caller_status = write_cursor.fetchone()[0]
            false_proof, _delta = inspect(repo, OWNER.id, OPERATION, RUNNER)
            write_same = id(connection.connection) == write_identity
            write_open = connection.connection.in_transaction
            write_autocommit = connection.get_autocommit()
            connection.rollback()
            connection.set_autocommit(True)

            connection.ensure_connection()
            read_identity = id(connection.connection)
            read_cursor = connection.cursor()
            read_cursor.execute("BEGIN DEFERRED")
            read_cursor.execute(
                "SELECT status FROM operation WHERE id = ?",
                (OPERATION.hex,),
            )
            pinned = read_cursor.fetchone()[0]
            writer = connections.create_connection("default")
            try:
                with writer.cursor() as writer_cursor:
                    writer_cursor.execute("BEGIN IMMEDIATE")
                    writer_cursor.execute(
                        "UPDATE operation SET status = 'FAILED', "
                        "blocks_conversation = 0, runner_id = NULL, "
                        "reservation_expires_at_us = NULL, "
                        "finished_at_us = 50, "
                        "failure_category = 'context_unavailable' "
                        "WHERE id = ?",
                        (OPERATION.hex,),
                    )
                    writer_cursor.execute(
                        "UPDATE global_model_slot SET operation_id = NULL, "
                        "runner_id = NULL, reservation_expires_at_us = NULL, "
                        "lease_expires_at_us = NULL, acquired_at_us = NULL "
                        "WHERE id = 1",
                    )
                    writer_cursor.execute("COMMIT")
            finally:
                writer.close()
            committed_proof, _delta = inspect(repo, OWNER.id, OPERATION, RUNNER)
            read_cursor.execute(
                "SELECT status FROM operation WHERE id = ?",
                (OPERATION.hex,),
            )
            read_still = read_cursor.fetchone()[0]
            read_open = connection.connection.in_transaction
            read_same = id(connection.connection) == read_identity
            read_autocommit = connection.get_autocommit()
            read_cursor.execute("ROLLBACK")
            read_cursor.close()
            print(json.dumps({
                "failed_type": type(failed_outcome).__name__,
                "failed_same": failed_same,
                "caller_status": caller_status,
                "false_proof": false_proof,
                "write_same": write_same,
                "write_open": write_open,
                "write_autocommit": write_autocommit,
                "pinned": pinned,
                "read_still": read_still,
                "committed_proof": committed_proof,
                "read_open": read_open,
                "read_same": read_same,
                "read_autocommit": read_autocommit,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["failed_type"], "PreProviderStateNotProven")
        self.assertTrue(payload["failed_same"])
        self.assertEqual(payload["caller_status"], "FAILED")
        self.assertEqual(payload["false_proof"]["type"], "QueuedReservationObserved")
        self.assertTrue(payload["write_same"])
        self.assertTrue(payload["write_open"])
        self.assertFalse(payload["write_autocommit"])
        self.assertEqual(payload["pinned"], "QUEUED")
        self.assertEqual(payload["read_still"], "QUEUED")
        self.assertEqual(
            payload["committed_proof"]["type"],
            "PreProviderFailureObserved",
        )
        self.assertTrue(payload["read_open"])
        self.assertTrue(payload["read_same"])
        self.assertTrue(payload["read_autocommit"])

    def test_source_does_not_mutate_or_open_production(self):
        source = (
            SRC_ROOT / "max_smart_db" / "pre_provider_inspection.py"
        ).read_text()
        self.assertNotIn("now_us", source)
        self.assertNotIn("fail_pre_provider", source)
        self.assertNotIn("BEGIN IMMEDIATE", source)
        self.assertFalse(PRODUCTION_DATABASE.exists())

    def _ambiguous(self, durable):
        return _payload(self, _run(
            f"""\
            queued()
            repo = repository(50)
            real_commit = connection.commit

            def commit(*args, **kwargs):
                if {durable!r}:
                    real_commit(*args, **kwargs)
                    raise OperationalError("simulated post-commit failure")
                raise OperationalError("simulated rollback before commit")

            connection.commit = commit
            try:
                failed = repo.fail_pre_provider(
                    token(),
                    PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
                )
            finally:
                connection.commit = real_commit
            proof, delta = inspect(repo, OWNER.id, OPERATION, RUNNER)
            operation = Operation.objects.get(pk=OPERATION)
            slot = GlobalModelSlot.objects.get(pk=1)
            print(json.dumps({{
                "failed": type(failed).__name__,
                "proof": proof,
                "clock_delta": delta,
                "status": operation.status,
                "slot_operation": (
                    None if slot.operation_id is None else str(slot.operation_id)
                ),
            }}))
            """
        ))
