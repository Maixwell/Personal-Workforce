"""Disposable-database tests for explicit operator commands."""

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
SESSION_FILE = PROJECT_ROOT / "data" / "current_session.json"
OPERATOR_SOURCE = SRC_ROOT / "operator_commands.py"
SERVICE_SOURCE = SRC_ROOT / "conversation_service.py"
INSPECTION_SOURCE = SRC_ROOT / "max_smart_db" / "acknowledgement_inspection.py"
_PROOF_COLUMNS = (
    "id",
    "owner_id",
    "conversation_id",
    "status",
    "runner_id",
    "request_hash",
    "fingerprint_version",
    "expected_version",
    "provider_started_at_us",
    "failure_category",
    "blocks_conversation",
    "acknowledged_at_us",
)

_PREAMBLE = """\
import base64
import json
import os
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
from max_smart_db.models import Conversation, GlobalModelSlot, IdempotencyKey, Message, Operation
from max_smart_db.repository import (
    PreProviderFailureCategory,
    PrepareCommand,
    RetainedResult,
)
from operator_commands import OperatorCommands

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

OWNER = User.objects.create(username="owner")
OTHER = User.objects.create(username="other")
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
PREPARED_AT = 1_000_000


class Clock:
    def __init__(self, value):
        self.value = value

    def now_us(self):
        return self.value


def key_for(value):
    return base64.urlsafe_b64encode(bytes([value]) * 16).rstrip(b"=").decode()


def repository(clock):
    return SqliteConversationRepository(clock, RUNNER)


def opened(clock):
    calls = {"provider": 0}
    repo = repository(clock)
    service = ConversationService(
        repo,
        lambda **_kwargs: "standing",
        lambda **_kwargs: calls.__setitem__("provider", calls["provider"] + 1),
    )
    before = Operation.objects.count()
    operator = OperatorCommands(service)
    return operator, service, repo, calls, before


def conversation(repo):
    created = repo.create_conversation(OWNER.id, "max-smart", None)
    if type(created).__name__ != "ConversationView":
        raise RuntimeError(type(created).__name__)
    return created.conversation_id


def prepare(repo, conversation_id, key, content="persisted original"):
    prepared = repo.prepare(PrepareCommand(
        actor_id=OWNER.id,
        conversation_id=conversation_id,
        expected_version=0,
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
        "owner_id": operation.owner_id,
        "content": operation.request_content,
        "blocks": operation.blocks_conversation,
        "acknowledged": operation.acknowledged_at_us is not None,
        "reservation": operation.reservation_expires_at_us,
        "deadline": operation.execution_deadline_us,
        "lease": operation.lease_expires_at_us,
        "retry_of": None if operation.retry_of_id is None else str(operation.retry_of_id),
        "version": Conversation.objects.get(pk=operation.conversation_id).version,
        "messages": Message.objects.filter(operation_id=operation_id).count(),
        "slot_operation": None if slot.operation_id is None else str(slot.operation_id),
    }


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
    operation = getattr(outcome, "operation", None)
    if operation is not None:
        payload["operation_id"] = str(operation.operation_id)
        payload["status"] = operation.status.value
    current = getattr(outcome, "current_state", None)
    if current is not None:
        payload["current_state"] = current.value
    return payload
"""


def _run(body, *, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "m4s5-operator.sqlite3"
            ),
            "PYTHONPATH": str(SRC_ROOT),
            "USER": "os-account",
            "LOGNAME": "os-account",
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


class OperatorCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session_stat = SESSION_FILE.stat()

    @classmethod
    def tearDownClass(cls):
        current = SESSION_FILE.stat()
        if (
            current.st_ino != cls.session_stat.st_ino
            or current.st_size != cls.session_stat.st_size
            or current.st_mtime_ns != cls.session_stat.st_mtime_ns
        ):
            raise AssertionError("terminal session file changed")
        if PRODUCTION_DATABASE.exists():
            raise AssertionError("production database created")

    def test_source_has_no_provider_or_startup_command(self):
        source = OPERATOR_SOURCE.read_text()
        for banned in (
            "submit",
            "claim_running",
            "supervise",
            "provider_worker",
            "build_service_provider",
            "list_messages",
            "getpass",
            "getlogin",
            "initialize_sqlite_file",
            "get_operation",
            "SELECT",
            "INSERT",
            "UPDATE",
            "BEGIN",
        ):
            self.assertNotIn(banned, source, banned)
        self.assertIn("inspect_acknowledged_unknown", source)
        inspection = INSPECTION_SOURCE.read_text()
        select = inspection.split("SELECT", 1)[1].split("FROM", 1)[0]
        columns = [
            part.strip().strip('"')
            for part in select.replace('"', " ").split(",")
        ]
        self.assertEqual(
            [column for column in columns if column],
            list(_PROOF_COLUMNS),
        )
        self.assertIn("BEGIN DEFERRED", inspection)
        self.assertIn("ROLLBACK", inspection)
        self.assertIn("_open_owned_connection", inspection)
        self.assertNotIn("acknowledge_unknown", inspection)
        self.assertNotIn("INSERT", inspection)
        self.assertNotIn("UPDATE", inspection)
        self.assertNotIn("acknowledge_unknown", SERVICE_SOURCE.read_text())
        self.assertNotIn("operator_commands", (SRC_ROOT / "terminal.py").read_text())
        self.assertNotIn("operator_commands", (SRC_ROOT / "max_smart.py").read_text())

    def test_classification_does_not_mutate_or_declare_liveness(self):
        payload = _payload(self, _run(
            """\
            clock = Clock(PREPARED_AT)
            operator, service, repo, calls, before = opened(clock)
            queued_id = conversation(repo)
            queued = prepare(repo, queued_id, 1)
            queued_class = operator.classify(OWNER.id, queued.operation_id)
            queued_facts = facts(queued.operation_id)
            clock.value = queued.reservation_expires_at
            expired_class = operator.classify(OWNER.id, queued.operation_id)
            expired_facts = facts(queued.operation_id)
            clock.value = PREPARED_AT
            claimed = repo.claim_running(OWNER.id, queued.operation_id)
            running_class = operator.classify(OWNER.id, queued.operation_id)
            applied = repo.apply_success(claimed.claimed_operation, "answer")
            succeeded_class = operator.classify(OWNER.id, queued.operation_id)

            failed_id = conversation(repo)
            failed = prepare(repo, failed_id, 2)
            failure = repo.fail_pre_provider(
                failed,
                PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
            )
            failed_class = operator.classify(OWNER.id, failed.operation_id)

            cancelled_id = conversation(repo)
            cancelled = prepare(repo, cancelled_id, 3)
            clock.value = cancelled.reservation_expires_at
            cancelled_recovery = operator.recover()
            cancelled_class = operator.classify(OWNER.id, cancelled.operation_id)

            unknown_id = conversation(repo)
            unknown = prepare(repo, unknown_id, 4)
            clock.value = PREPARED_AT
            unknown_claim = repo.claim_running(OWNER.id, unknown.operation_id)
            clock.value = unknown_claim.claimed_operation.lease_expires_at
            unknown_recovery = operator.recover()
            blocking_class = operator.classify(OWNER.id, unknown.operation_id)
            service._retained[(unknown.operation_id, RUNNER, 1)] = "kept answer"
            acknowledged = operator.acknowledge(OWNER.id, unknown.operation_id)
            acknowledged_class = operator.classify(OWNER.id, unknown.operation_id)
            print(json.dumps({
                "constructed": Operation.objects.count() == before + 4,
                "provider": calls["provider"],
                "queued": described(queued_class),
                "queued_facts": queued_facts,
                "expired": described(expired_class),
                "expired_facts": expired_facts,
                "running": described(running_class),
                "applied": type(applied).__name__,
                "succeeded": described(succeeded_class),
                "failure": type(failure).__name__,
                "failed": described(failed_class),
                "cancelled_recovery": type(cancelled_recovery).__name__,
                "cancelled": described(cancelled_class),
                "unknown_recovery": type(unknown_recovery).__name__,
                "blocking": described(blocking_class),
                "acknowledged": described(acknowledged),
                "acknowledged_class": described(acknowledged_class),
                "retained": list(service._retained),
            }))
            """
        ))
        self.assertTrue(payload["constructed"])
        self.assertEqual(payload["provider"], 0)
        self.assertEqual(payload["queued"]["type"], "RestartQueued")
        self.assertEqual(payload["queued"]["status"], "QUEUED")
        self.assertEqual(payload["queued_facts"]["status"], "QUEUED")
        self.assertEqual(payload["expired"]["type"], "RestartQueued")
        self.assertEqual(payload["expired_facts"]["status"], "QUEUED")
        self.assertEqual(
            payload["expired_facts"]["reservation"],
            payload["queued_facts"]["reservation"],
        )
        self.assertEqual(payload["running"]["type"], "RestartRunning")
        self.assertEqual(payload["running"]["status"], "RUNNING")
        self.assertEqual(payload["applied"], "Applied")
        self.assertEqual(payload["succeeded"]["type"], "RestartCompleted")
        self.assertEqual(payload["succeeded"]["status"], "SUCCEEDED")
        self.assertEqual(payload["failure"], "Failed")
        self.assertEqual(payload["failed"]["type"], "RestartCompleted")
        self.assertEqual(payload["failed"]["status"], "FAILED")
        self.assertEqual(payload["cancelled_recovery"], "CancelledExpiredQueued")
        self.assertEqual(payload["cancelled"]["type"], "RestartCompleted")
        self.assertEqual(payload["cancelled"]["status"], "CANCELLED")
        self.assertEqual(payload["unknown_recovery"], "MarkedOutcomeUnknown")
        self.assertEqual(payload["blocking"]["type"], "RestartUnknownBlocking")
        self.assertEqual(payload["blocking"]["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(payload["acknowledged"]["type"], "Acknowledged")
        self.assertEqual(payload["acknowledged_class"]["type"], "RestartUnknownAcknowledged")
        self.assertEqual(payload["acknowledged_class"]["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(payload["retained"], [])

    def test_explicit_recovery_follows_repository_deadlines(self):
        payload = _payload(self, _run(
            """\
            clock = Clock(PREPARED_AT)
            operator, service, repo, calls, _before = opened(clock)
            live_id = conversation(repo)
            live = prepare(repo, live_id, 1)
            live_recovery = operator.recover()
            live_facts = facts(live.operation_id)
            claimed = repo.claim_running(OWNER.id, live.operation_id)
            running_recovery = operator.recover()
            running_facts = facts(live.operation_id)
            clock.value = claimed.claimed_operation.execution_deadline
            deadline_recovery = operator.recover()
            deadline_facts = facts(live.operation_id)
            clock.value = claimed.claimed_operation.lease_expires_at
            expired_recovery = operator.recover()
            expired_facts = facts(live.operation_id)

            clock.value = PREPARED_AT
            expired_queue_id = conversation(repo)
            expired_queue = prepare(repo, expired_queue_id, 2)
            clock.value = expired_queue.reservation_expires_at
            cancelled = operator.recover()
            print(json.dumps({
                "provider": calls["provider"],
                "live": type(live_recovery).__name__,
                "live_facts": live_facts,
                "running": type(running_recovery).__name__,
                "running_facts": running_facts,
                "deadline": type(deadline_recovery).__name__,
                "deadline_facts": deadline_facts,
                "expired": type(expired_recovery).__name__,
                "expired_facts": expired_facts,
                "cancelled": type(cancelled).__name__,
                "cancelled_facts": facts(expired_queue.operation_id),
                "deadline_value": claimed.claimed_operation.execution_deadline,
                "lease_value": claimed.claimed_operation.lease_expires_at,
            }))
            """
        ))
        self.assertEqual(payload["provider"], 0)
        self.assertEqual(payload["live"], "LiveQueued")
        self.assertEqual(payload["live_facts"]["status"], "QUEUED")
        self.assertEqual(payload["running"], "LiveRunning")
        self.assertEqual(payload["running_facts"]["status"], "RUNNING")
        self.assertEqual(payload["deadline"], "LiveRunning")
        self.assertEqual(payload["deadline_facts"]["status"], "RUNNING")
        self.assertGreater(payload["lease_value"], payload["deadline_value"])
        self.assertEqual(payload["expired"], "MarkedOutcomeUnknown")
        self.assertEqual(payload["expired_facts"]["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(payload["expired_facts"]["lease"], payload["lease_value"])
        self.assertEqual(payload["cancelled"], "CancelledExpiredQueued")
        self.assertEqual(payload["cancelled_facts"]["status"], "CANCELLED")
        self.assertIsNone(payload["cancelled_facts"]["slot_operation"])

    def test_ambiguous_recovery_does_not_write_twice(self):
        payload = _payload(self, _run(
            """\
            def once(durable):
                clock = Clock(PREPARED_AT)
                operator, service, repo, calls, _before = opened(clock)
                conversation_id = conversation(repo)
                queued = prepare(repo, conversation_id, 1 if durable else 2)
                clock.value = queued.reservation_expires_at
                real_recover = repo.recover_slot
                writes = {"count": 0}

                def recover_slot():
                    writes["count"] += 1
                    real_commit = connection.commit

                    def commit(*args, **kwargs):
                        if durable:
                            real_commit(*args, **kwargs)
                        raise OperationalError("simulated recovery commit")

                    connection.commit = commit
                    try:
                        return real_recover()
                    finally:
                        connection.commit = real_commit

                repo.recover_slot = recover_slot
                outcome = operator.recover()
                return {
                    "type": type(outcome).__name__,
                    "writes": writes["count"],
                    "provider": calls["provider"],
                    "facts": facts(queued.operation_id),
                }

            print(json.dumps({"durable": once(True), "rolled": once(False)}))
            """
        ))
        self.assertEqual(payload["durable"]["type"], "RecoveryUnconfirmed")
        self.assertEqual(payload["durable"]["writes"], 1)
        self.assertEqual(payload["durable"]["provider"], 0)
        self.assertEqual(payload["durable"]["facts"]["status"], "CANCELLED")
        self.assertEqual(payload["rolled"]["type"], "RecoveryUnconfirmed")
        self.assertEqual(payload["rolled"]["writes"], 1)
        self.assertEqual(payload["rolled"]["provider"], 0)
        self.assertEqual(payload["rolled"]["facts"]["status"], "QUEUED")

    def test_acknowledgement_races_and_ineligible_operations(self):
        payload = _payload(self, _run(
            """\
            clock = Clock(PREPARED_AT)
            operator, service, repo, calls, _before = opened(clock)
            conversation_id = conversation(repo)
            queued = prepare(repo, conversation_id, 1, "persisted original")
            ineligible = operator.acknowledge(OWNER.id, queued.operation_id)
            claimed = repo.claim_running(OWNER.id, queued.operation_id)
            token = claimed.claimed_operation
            clock.value = token.lease_expires_at
            recovered = operator.recover()
            service._retained[(queued.operation_id, RUNNER, token.claimed_at)] = "kept answer"
            acknowledged = operator.acknowledge(OWNER.id, queued.operation_id)
            late = repo.reconcile_late_success(RetainedResult(token, "kept answer"))
            after_ack = facts(queued.operation_id)
            retained_after = (queued.operation_id, RUNNER, token.claimed_at) in service._retained

            clock.value = PREPARED_AT
            success_id = conversation(repo)
            success = prepare(repo, success_id, 2, "persisted original")
            success_claim = repo.claim_running(OWNER.id, success.operation_id)
            success_token = success_claim.claimed_operation
            clock.value = success_token.lease_expires_at
            operator.recover()
            service._retained[(
                success.operation_id,
                RUNNER,
                success_token.claimed_at,
            )] = "kept answer"
            reconciled = repo.reconcile_late_success(
                RetainedResult(success_token, "kept answer")
            )
            after_success = operator.acknowledge(OWNER.id, success.operation_id)
            success_facts = facts(success.operation_id)
            retained_success = (
                success.operation_id,
                RUNNER,
                success_token.claimed_at,
            ) in service._retained

            missing = operator.acknowledge(OWNER.id, uuid.uuid4())
            cross = operator.acknowledge(OTHER.id, queued.operation_id)
            with transaction.atomic():
                held = operator.acknowledge(OWNER.id, queued.operation_id)
                held_open = connection.in_atomic_block
            print(json.dumps({
                "provider": calls["provider"],
                "ineligible": described(ineligible),
                "recovered": type(recovered).__name__,
                "acknowledged": described(acknowledged),
                "late": type(late).__name__,
                "after_ack": after_ack,
                "retained_after": retained_after,
                "reconciled": type(reconciled).__name__,
                "after_success": described(after_success),
                "success_facts": success_facts,
                "retained_success": retained_success,
                "missing": type(missing).__name__,
                "cross": type(cross).__name__,
                "held": type(held).__name__,
                "held_open": held_open,
            }))
            """
        ))
        self.assertEqual(payload["provider"], 0)
        self.assertEqual(payload["ineligible"]["type"], "NotUnknown")
        self.assertEqual(payload["ineligible"]["current_state"], "QUEUED")
        self.assertEqual(payload["recovered"], "MarkedOutcomeUnknown")
        self.assertEqual(payload["acknowledged"]["type"], "Acknowledged")
        self.assertEqual(payload["late"], "AcknowledgedDiscard")
        self.assertEqual(payload["after_ack"]["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(payload["after_ack"]["blocks"], 0)
        self.assertTrue(payload["after_ack"]["acknowledged"])
        self.assertEqual(payload["after_ack"]["messages"], 0)
        self.assertEqual(payload["after_ack"]["content"], "persisted original")
        self.assertFalse(payload["retained_after"])
        self.assertEqual(payload["reconciled"], "Reconciled")
        self.assertEqual(payload["after_success"]["type"], "NotUnknown")
        self.assertEqual(payload["after_success"]["current_state"], "SUCCEEDED")
        self.assertEqual(payload["success_facts"]["status"], "SUCCEEDED")
        self.assertEqual(payload["success_facts"]["messages"], 2)
        self.assertTrue(payload["retained_success"])
        self.assertEqual(payload["missing"], "NotFound")
        self.assertEqual(payload["cross"], "NotFound")
        self.assertEqual(payload["held"], "TransactionHeld")
        self.assertTrue(payload["held_open"])

    def test_ambiguous_acknowledgement_does_not_write_again(self):
        payload = _payload(self, _run(
            """\
            def once(durable):
                clock = Clock(PREPARED_AT)
                operator, service, repo, calls, _before = opened(clock)
                conversation_id = conversation(repo)
                queued = prepare(repo, conversation_id, 1 if durable else 2)
                claimed = repo.claim_running(OWNER.id, queued.operation_id)
                clock.value = claimed.claimed_operation.lease_expires_at
                operator.recover()
                real_acknowledge = repo.acknowledge_unknown
                writes = {"count": 0}

                def acknowledge_unknown(*args, **kwargs):
                    writes["count"] += 1
                    real_commit = connection.commit

                    def commit(*commit_args, **commit_kwargs):
                        if durable:
                            real_commit(*commit_args, **commit_kwargs)
                        raise OperationalError("simulated acknowledgement commit")

                    connection.commit = commit
                    try:
                        return real_acknowledge(*args, **kwargs)
                    finally:
                        connection.commit = real_commit

                repo.acknowledge_unknown = acknowledge_unknown
                outcome = operator.acknowledge(OWNER.id, queued.operation_id)
                return {
                    "type": type(outcome).__name__,
                    "writes": writes["count"],
                    "provider": calls["provider"],
                    "facts": facts(queued.operation_id),
                }

            print(json.dumps({"durable": once(True), "rolled": once(False)}))
            """
        ))
        self.assertEqual(payload["durable"]["type"], "Acknowledged")
        self.assertEqual(payload["durable"]["writes"], 1)
        self.assertEqual(payload["durable"]["provider"], 0)
        self.assertEqual(payload["durable"]["facts"]["acknowledged"], True)
        self.assertEqual(payload["durable"]["facts"]["blocks"], 0)
        self.assertEqual(payload["rolled"]["type"], "AcknowledgementUnconfirmed")
        self.assertEqual(payload["rolled"]["writes"], 1)
        self.assertEqual(payload["rolled"]["provider"], 0)
        self.assertEqual(payload["rolled"]["facts"]["status"], "OUTCOME_UNKNOWN")
        self.assertFalse(payload["rolled"]["facts"]["acknowledged"])
        self.assertEqual(payload["rolled"]["facts"]["blocks"], 1)

    def test_retry_commands_preserve_source_text_and_warning(self):
        payload = _payload(self, _run(
            """\
            clock = Clock(PREPARED_AT)
            operator, service, repo, calls, _before = opened(clock)
            running_id = conversation(repo)
            running_row = prepare(repo, running_id, 1, "persisted original")
            running_claim = repo.claim_running(OWNER.id, running_row.operation_id)
            running = operator.create_retry(
                OWNER.id, running_row.operation_id, key_for(11), 0,
            )
            repo.apply_success(running_claim.claimed_operation, "answer")
            failed_id = conversation(repo)
            failed = prepare(repo, failed_id, 2, "persisted original")
            repo.fail_pre_provider(
                failed,
                PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
            )
            failed_retry = operator.create_retry(
                OWNER.id, failed.operation_id, key_for(12), 0,
            )
            repeated = operator.create_retry(
                OWNER.id, failed.operation_id, key_for(12), 0,
            )
            stored = IdempotencyKey.objects.get(
                operation_id=uuid.UUID(described(failed_retry)["operation_id"])
            )
            clock.value = stored.expires_at_us
            expired = operator.create_retry(
                OWNER.id, failed.operation_id, key_for(12), 0,
            )
            operator.recover()

            cancelled_id = conversation(repo)
            cancelled = prepare(repo, cancelled_id, 4, "persisted original")
            clock.value = cancelled.reservation_expires_at
            operator.recover()
            clock.value = PREPARED_AT
            cancelled_retry = operator.create_retry(
                OWNER.id, cancelled.operation_id, key_for(13), 0,
            )
            clock.value = cancelled_retry.reservation.reservation_expires_at
            operator.recover()

            unknown_id = conversation(repo)
            unknown = prepare(repo, unknown_id, 3, "persisted original")
            claimed = repo.claim_running(OWNER.id, unknown.operation_id)
            clock.value = claimed.claimed_operation.lease_expires_at
            operator.recover()
            blocking = operator.create_retry(
                OWNER.id, unknown.operation_id, key_for(14), 0,
            )
            operator.acknowledge(OWNER.id, unknown.operation_id)
            real_create = repo.create_retry
            writes = {"count": 0}

            def create_retry(command):
                writes["count"] += 1
                real_commit = connection.commit

                def commit(*args, **kwargs):
                    real_commit(*args, **kwargs)
                    raise OperationalError("simulated retry commit")

                connection.commit = commit
                try:
                    return real_create(command)
                finally:
                    connection.commit = real_commit

            repo.create_retry = create_retry
            warned = operator.create_retry(
                OWNER.id, unknown.operation_id, key_for(15), 0,
            )
            print(json.dumps({
                "provider": calls["provider"],
                "running_refused": described(running),
                "source_id": str(failed.operation_id),
                "failed_retry": described(failed_retry),
                "failed_facts": facts(uuid.UUID(described(failed_retry)["operation_id"])),
                "repeated": described(repeated),
                "expired": described(expired),
                "cancelled_retry": described(cancelled_retry),
                "blocking": described(blocking),
                "writes": writes["count"],
                "warned": described(warned),
                "children": Operation.objects.exclude(retry_of_id=None).count(),
            }))
            """
        ))
        self.assertEqual(payload["provider"], 0)
        self.assertEqual(payload["running_refused"]["type"], "RetrySourceNotEligible")
        self.assertEqual(payload["failed_retry"]["type"], "RetryPrepared")
        self.assertFalse(payload["failed_retry"]["warning"])
        self.assertEqual(payload["failed_facts"]["content"], "persisted original")
        self.assertEqual(payload["failed_facts"]["retry_of"], payload["source_id"])
        self.assertEqual(payload["repeated"]["type"], "RetryExisting")
        self.assertEqual(
            payload["repeated"]["operation_id"],
            payload["failed_retry"]["operation_id"],
        )
        self.assertEqual(payload["expired"]["type"], "RetryExisting")
        self.assertEqual(
            payload["expired"]["operation_id"],
            payload["failed_retry"]["operation_id"],
        )
        self.assertFalse(payload["expired"]["warning"])
        self.assertEqual(payload["cancelled_retry"]["type"], "RetryPrepared")
        self.assertFalse(payload["cancelled_retry"]["warning"])
        self.assertEqual(payload["blocking"]["type"], "RetrySourceNotEligible")
        self.assertEqual(payload["writes"], 1)
        self.assertEqual(payload["warned"]["type"], "RetryPrepared")
        self.assertTrue(payload["warned"]["warning"])
        self.assertEqual(payload["children"], 3)

    def test_concurrent_retry_creation_allows_one_child(self):
        payload = _payload(self, _run(
            """\
            clock = Clock(PREPARED_AT)
            operator, service, repo, calls, _before = opened(clock)
            conversation_id = conversation(repo)
            failed = prepare(repo, conversation_id, 1)
            repo.fail_pre_provider(
                failed,
                PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
            )
            barrier = threading.Barrier(2)
            outcomes = []

            def attempt(key):
                worker_clock = Clock(PREPARED_AT)
                worker, _worker_service, _worker_repo, worker_calls, _unused = opened(
                    worker_clock
                )
                barrier.wait()
                outcome = worker.create_retry(
                    OWNER.id,
                    failed.operation_id,
                    key_for(key),
                    0,
                )
                outcomes.append({
                    "type": type(outcome).__name__,
                    "provider": worker_calls["provider"],
                })

            threads = [
                threading.Thread(target=attempt, args=(20,)),
                threading.Thread(target=attempt, args=(21,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            print(json.dumps({
                "provider": calls["provider"],
                "outcomes": outcomes,
                "children": Operation.objects.exclude(retry_of_id=None).count(),
            }))
            """
        ))
        self.assertEqual(payload["provider"], 0)
        self.assertEqual(
            sorted(item["type"] for item in payload["outcomes"]),
            ["RetryPrepared", "RetrySourceNotEligible"],
        )
        self.assertEqual([item["provider"] for item in payload["outcomes"]], [0, 0])
        self.assertEqual(payload["children"], 1)


    def test_ambiguous_acknowledgement_is_proven_on_an_owned_connection(self):
        payload = _payload(self, _run(
            """\
            from django.db import IntegrityError, connections
            from max_smart_db.repository import CommitAmbiguous

            def unknown(clock, operator, repo, key):
                conversation_id = conversation(repo)
                queued = prepare(repo, conversation_id, key, "persisted original")
                claimed = repo.claim_running(OWNER.id, queued.operation_id)
                token = claimed.claimed_operation
                clock.value = token.lease_expires_at
                recovered = operator.recover()
                if type(recovered).__name__ != "MarkedOutcomeUnknown":
                    raise RuntimeError(type(recovered).__name__)
                clock.value = PREPARED_AT
                return queued.operation_id, token

            def remember(service, operation_id, token):
                service._retained[(
                    operation_id,
                    RUNNER,
                    token.claimed_at,
                )] = "kept answer"

            def retained(service, operation_id, token):
                return (
                    operation_id,
                    RUNNER,
                    token.claimed_at,
                ) in service._retained

            def proof_row(operation_id, **changes):
                operation = Operation.objects.get(pk=operation_id)
                values = {
                    "id": operation.id.hex,
                    "owner_id": operation.owner_id,
                    "conversation_id": operation.conversation_id.hex,
                    "status": operation.status,
                    "runner_id": operation.runner_id,
                    "request_hash": operation.request_hash,
                    "fingerprint_version": operation.fingerprint_version,
                    "expected_version": operation.expected_version,
                    "provider_started_at_us": operation.provider_started_at_us,
                    "failure_category": operation.failure_category,
                    "blocks_conversation": operation.blocks_conversation,
                    "acknowledged_at_us": operation.acknowledged_at_us,
                }
                values.update(changes)
                return tuple(values[name] for name in (
                    "id",
                    "owner_id",
                    "conversation_id",
                    "status",
                    "runner_id",
                    "request_hash",
                    "fingerprint_version",
                    "expected_version",
                    "provider_started_at_us",
                    "failure_category",
                    "blocks_conversation",
                    "acknowledged_at_us",
                ))

            def install_rows(rows):
                real_create = connections.create_connection

                def create_connection(alias):
                    owned = real_create(alias)
                    real_cursor = owned.cursor

                    def cursor(*args, **kwargs):
                        wrapped = real_cursor(*args, **kwargs)
                        real_execute = wrapped.execute
                        real_fetchall = wrapped.fetchall
                        state = {"fake": False}

                        def execute(sql, params=None):
                            if (
                                "SELECT" in str(sql)
                                and "acknowledged_at_us" in str(sql)
                            ):
                                state["fake"] = True
                                return None
                            if params is None:
                                return real_execute(sql)
                            return real_execute(sql, params)

                        def fetchall():
                            if state["fake"]:
                                return rows
                            return real_fetchall()

                        wrapped.execute = execute
                        wrapped.fetchall = fetchall
                        return wrapped

                    owned.cursor = cursor
                    return owned

                connections.create_connection = create_connection
                return real_create

            def install_failure(seen):
                real_create = connections.create_connection

                def create_connection(alias):
                    owned = real_create(alias)
                    seen.append(owned is not connection)
                    real_cursor = owned.cursor

                    def cursor(*args, **kwargs):
                        wrapped = real_cursor(*args, **kwargs)
                        real_execute = wrapped.execute

                        def execute(sql, params=None):
                            if (
                                "SELECT" in str(sql)
                                and "acknowledged_at_us" in str(sql)
                            ):
                                raise OperationalError(
                                    "simulated acknowledgement read"
                                )
                            if params is None:
                                return real_execute(sql)
                            return real_execute(sql, params)

                        wrapped.execute = execute
                        return wrapped

                    owned.cursor = cursor
                    return owned

                connections.create_connection = create_connection
                return real_create

            def commit_once(repo, durable, writes):
                real_acknowledge = repo.acknowledge_unknown

                def acknowledge_unknown(*args, **kwargs):
                    writes["count"] += 1
                    real_commit = connection.commit

                    def commit(*commit_args, **commit_kwargs):
                        if durable:
                            real_commit(*commit_args, **commit_kwargs)
                        raise OperationalError(
                            "simulated acknowledgement commit"
                        )

                    connection.commit = commit
                    try:
                        return real_acknowledge(*args, **kwargs)
                    finally:
                        connection.commit = real_commit

                repo.acknowledge_unknown = acknowledge_unknown
                return real_acknowledge

            clock = Clock(PREPARED_AT)
            operator, service, repo, calls, _before = opened(clock)
            connection.ensure_connection()
            writer_id = id(connection.connection)

            proven_id, proven_token = unknown(clock, operator, repo, 31)
            remember(service, proven_id, proven_token)
            proven_writes = {"count": 0}
            real_acknowledge = commit_once(repo, True, proven_writes)
            real_create = connections.create_connection
            seen = []

            def create_connection(alias):
                owned = real_create(alias)
                owned.ensure_connection()
                seen.append(owned.connection is not connection.connection)
                return owned

            connections.create_connection = create_connection
            try:
                proven = operator.acknowledge(OWNER.id, proven_id)
            finally:
                connections.create_connection = real_create
                repo.acknowledge_unknown = real_acknowledge
            proven_late = repo.reconcile_late_success(
                RetainedResult(proven_token, "kept answer")
            )

            rolled_id, rolled_token = unknown(clock, operator, repo, 32)
            remember(service, rolled_id, rolled_token)
            rolled_writes = {"count": 0}
            real_acknowledge = commit_once(repo, False, rolled_writes)
            try:
                rolled = operator.acknowledge(OWNER.id, rolled_id)
            finally:
                repo.acknowledge_unknown = real_acknowledge

            failed_id, failed_token = unknown(clock, operator, repo, 33)
            remember(service, failed_id, failed_token)
            failed_writes = {"count": 0}
            failed_seen = []
            real_acknowledge = commit_once(repo, True, failed_writes)
            real_create = install_failure(failed_seen)
            failed_before = id(connection.connection)
            try:
                failed = operator.acknowledge(OWNER.id, failed_id)
            finally:
                connections.create_connection = real_create
                repo.acknowledge_unknown = real_acknowledge
            failed_late = repo.reconcile_late_success(
                RetainedResult(failed_token, "kept answer")
            )

            hidden_id, _hidden_token = unknown(clock, operator, repo, 34)
            connection.set_autocommit(False)
            hidden_cursor = connection.cursor()
            hidden_cursor.execute("BEGIN IMMEDIATE")
            hidden_cursor.execute(
                "UPDATE operation SET acknowledged_at_us = ?, "
                "blocks_conversation = 0 WHERE id = ? AND owner_id = ? "
                "AND acknowledged_at_us IS NULL AND blocks_conversation = 1",
                (PREPARED_AT, hidden_id.hex, OWNER.id),
            )
            hidden_cursor.execute(
                "SELECT acknowledged_at_us, blocks_conversation "
                "FROM operation WHERE id = ?",
                (hidden_id.hex,),
            )
            hidden_visible = hidden_cursor.fetchone()
            hidden_identity = id(connection.connection)
            hidden = repo.inspect_acknowledged_unknown(OWNER.id, hidden_id)
            hidden_same = id(connection.connection) == hidden_identity
            hidden_open = connection.in_atomic_block or (
                connection.connection.in_transaction
            )
            connection.rollback()
            connection.set_autocommit(True)

            visible_id, _visible_token = unknown(clock, operator, repo, 35)
            connection.ensure_connection()
            visible_identity = id(connection.connection)
            visible_cursor = connection.cursor()
            visible_cursor.execute("BEGIN DEFERRED")
            visible_cursor.execute(
                "SELECT acknowledged_at_us, blocks_conversation "
                "FROM operation WHERE id = ?",
                (visible_id.hex,),
            )
            visible_pinned = visible_cursor.fetchone()
            side = connections.create_connection("default")
            try:
                with side.cursor() as side_cursor:
                    side_cursor.execute("BEGIN IMMEDIATE")
                    side_cursor.execute(
                        "UPDATE operation SET acknowledged_at_us = ?, "
                        "blocks_conversation = 0 WHERE id = ? AND owner_id = ? "
                        "AND acknowledged_at_us IS NULL AND blocks_conversation = 1",
                        (PREPARED_AT, visible_id.hex, OWNER.id),
                    )
                    side_cursor.execute(
                        "SELECT changes()"
                    )
                    side_changes = side_cursor.fetchone()[0]
                    side_cursor.execute("COMMIT")
            finally:
                side.close()
            visible = repo.inspect_acknowledged_unknown(OWNER.id, visible_id)
            visible_cursor.execute(
                "SELECT acknowledged_at_us, blocks_conversation "
                "FROM operation WHERE id = ?",
                (visible_id.hex,),
            )
            visible_still = visible_cursor.fetchone()
            visible_same = id(connection.connection) == visible_identity
            visible_cursor.execute("ROLLBACK")
            visible_cursor.close()

            cross = repo.inspect_acknowledged_unknown(OTHER.id, proven_id)
            cross_command = operator.acknowledge(OTHER.id, proven_id)

            shape_id, _shape_token = unknown(clock, operator, repo, 36)

            def schema_rejects(sql, params):
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(sql, params)
                except IntegrityError:
                    return True
                return False

            timestamp_rejected = schema_rejects(
                "UPDATE operation SET acknowledged_at_us = ? "
                "WHERE id = ? AND acknowledged_at_us IS NULL",
                (PREPARED_AT, shape_id.hex),
            )
            blocker_rejected = schema_rejects(
                "UPDATE operation SET blocks_conversation = 0 "
                "WHERE id = ? AND blocks_conversation = 1",
                (shape_id.hex,),
            )
            shaped = operator.acknowledge(OWNER.id, shape_id)
            blocker_rows = [proof_row(shape_id, blocks_conversation=1)]
            missing_rows = [proof_row(shape_id, acknowledged_at_us=None)]
            real_create = install_rows(blocker_rows)
            try:
                blocker_remains = repo.inspect_acknowledged_unknown(
                    OWNER.id,
                    shape_id,
                )
            finally:
                connections.create_connection = real_create
            real_create = install_rows(missing_rows)
            try:
                timestamp_missing = repo.inspect_acknowledged_unknown(
                    OWNER.id,
                    shape_id,
                )
            finally:
                connections.create_connection = real_create
            shape_writes = {"count": 0}

            def acknowledge_once(*_args, **_kwargs):
                shape_writes["count"] += 1
                return CommitAmbiguous(operation_id=shape_id)

            real_acknowledge = repo.acknowledge_unknown
            repo.acknowledge_unknown = acknowledge_once
            real_create = install_rows(blocker_rows)
            try:
                operator_blocker = operator.acknowledge(OWNER.id, shape_id)
            finally:
                connections.create_connection = real_create
                repo.acknowledge_unknown = real_acknowledge

            print(json.dumps({
                "provider": calls["provider"],
                "owner_id": OWNER.id,
                "proven_id": str(proven_id),
                "visible_id": str(visible_id),
                "writer_same": id(connection.connection) == writer_id,
                "autocommit": connection.get_autocommit(),
                "proven": described(proven),
                "proven_writes": proven_writes["count"],
                "proven_connections": seen,
                "proven_retained": retained(service, proven_id, proven_token),
                "proven_facts": facts(proven_id),
                "proven_late": type(proven_late).__name__,
                "rolled": type(rolled).__name__,
                "rolled_writes": rolled_writes["count"],
                "rolled_retained": retained(service, rolled_id, rolled_token),
                "rolled_facts": facts(rolled_id),
                "failed": type(failed).__name__,
                "failed_writes": failed_writes["count"],
                "failed_distinct": failed_seen,
                "failed_same": id(connection.connection) == failed_before,
                "failed_retained": retained(service, failed_id, failed_token),
                "failed_facts": facts(failed_id),
                "failed_late": type(failed_late).__name__,
                "failed_messages": facts(failed_id)["messages"],
                "hidden": type(hidden).__name__,
                "hidden_visible": list(hidden_visible),
                "hidden_same": hidden_same,
                "hidden_open": hidden_open,
                "hidden_facts": facts(hidden_id),
                "visible": type(visible).__name__,
                "visible_owner": visible.operation.actor_id,
                "visible_operation": str(visible.operation.operation_id),
                "visible_status": visible.operation.status.value,
                "visible_acknowledged": visible.operation.acknowledged,
                "visible_blocks": visible.operation.blocks_conversation,
                "visible_changes": side_changes,
                "visible_pinned": list(visible_pinned),
                "visible_still": list(visible_still),
                "visible_same": visible_same,
                "visible_facts": facts(visible_id),
                "cross": type(cross).__name__,
                "cross_fields": sorted(cross.__dataclass_fields__),
                "cross_command": type(cross_command).__name__,
                "cross_facts": facts(proven_id),
                "timestamp_rejected": timestamp_rejected,
                "blocker_rejected": blocker_rejected,
                "shaped": type(shaped).__name__,
                "shape_facts": facts(shape_id),
                "blocker_remains": type(blocker_remains).__name__,
                "timestamp_missing": type(timestamp_missing).__name__,
                "operator_blocker": type(operator_blocker).__name__,
                "shape_writes": shape_writes["count"],
            }))
            """
        ))
        self.assertEqual(payload["provider"], 0)
        self.assertTrue(payload["autocommit"])
        self.assertEqual(payload["proven"]["type"], "Acknowledged")
        self.assertEqual(payload["proven"]["operation_id"], payload["proven_id"])
        self.assertEqual(payload["proven"]["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(payload["proven_writes"], 1)
        self.assertEqual(payload["proven_connections"], [True])
        self.assertFalse(payload["proven_retained"])
        self.assertEqual(payload["proven_facts"]["status"], "OUTCOME_UNKNOWN")
        self.assertTrue(payload["proven_facts"]["acknowledged"])
        self.assertEqual(payload["proven_facts"]["blocks"], 0)
        self.assertEqual(payload["proven_facts"]["messages"], 0)
        self.assertEqual(payload["proven_facts"]["content"], "persisted original")
        self.assertEqual(payload["proven_late"], "AcknowledgedDiscard")
        self.assertEqual(payload["rolled"], "AcknowledgementUnconfirmed")
        self.assertEqual(payload["rolled_writes"], 1)
        self.assertTrue(payload["rolled_retained"])
        self.assertEqual(payload["rolled_facts"]["status"], "OUTCOME_UNKNOWN")
        self.assertFalse(payload["rolled_facts"]["acknowledged"])
        self.assertEqual(payload["rolled_facts"]["blocks"], 1)
        self.assertEqual(payload["failed"], "AcknowledgementUnconfirmed")
        self.assertEqual(payload["failed_writes"], 1)
        self.assertEqual(payload["failed_distinct"], [True])
        self.assertTrue(payload["failed_same"])
        self.assertTrue(payload["failed_retained"])
        self.assertTrue(payload["failed_facts"]["acknowledged"])
        self.assertEqual(payload["failed_facts"]["blocks"], 0)
        self.assertEqual(payload["failed_late"], "AcknowledgedDiscard")
        self.assertEqual(payload["failed_messages"], 0)
        self.assertEqual(payload["hidden"], "AcknowledgementNotProven")
        self.assertEqual(payload["hidden_visible"], [1_000_000, 0])
        self.assertTrue(payload["hidden_same"])
        self.assertTrue(payload["hidden_open"])
        self.assertFalse(payload["hidden_facts"]["acknowledged"])
        self.assertEqual(payload["hidden_facts"]["blocks"], 1)
        self.assertEqual(payload["visible"], "AcknowledgementObserved")
        self.assertEqual(payload["visible_owner"], payload["owner_id"])
        self.assertEqual(payload["visible_operation"], payload["visible_id"])
        self.assertEqual(payload["visible_changes"], 1)
        self.assertEqual(payload["visible_pinned"], [None, 1])
        self.assertEqual(payload["visible_still"], [None, 1])
        self.assertTrue(payload["visible_same"])
        self.assertTrue(payload["visible_acknowledged"])
        self.assertFalse(payload["visible_blocks"])
        self.assertEqual(payload["visible_status"], "OUTCOME_UNKNOWN")
        self.assertTrue(payload["visible_facts"]["acknowledged"])
        self.assertEqual(payload["visible_facts"]["blocks"], 0)
        self.assertEqual(payload["cross"], "AcknowledgementNotProven")
        self.assertEqual(payload["cross_fields"], [])
        self.assertEqual(payload["cross_command"], "NotFound")
        self.assertEqual(payload["cross_facts"]["content"], "persisted original")
        self.assertTrue(payload["timestamp_rejected"])
        self.assertTrue(payload["blocker_rejected"])
        self.assertEqual(payload["shaped"], "Acknowledged")
        self.assertTrue(payload["shape_facts"]["acknowledged"])
        self.assertEqual(payload["shape_facts"]["blocks"], 0)
        self.assertEqual(payload["blocker_remains"], "AcknowledgementNotProven")
        self.assertEqual(payload["timestamp_missing"], "AcknowledgementNotProven")
        self.assertEqual(payload["operator_blocker"], "AcknowledgementUnconfirmed")
        self.assertEqual(payload["shape_writes"], 1)

if __name__ == "__main__":
    unittest.main()
