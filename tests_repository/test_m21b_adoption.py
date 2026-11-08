"""Disposable-database tests for fenced QUEUED adoption."""

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

_PREAMBLE = """\
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
from max_smart_db.models import GlobalModelSlot, Operation
from max_smart_db.sql_policy import scan_migration_package

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

OWNER = User.objects.create(username="owner")
OTHER = User.objects.create(username="other")
CONVERSATION = uuid.UUID("00112233445566778899aabbccddeeff")
OLD = "AAAAAAAAAAAAAAAAAAAAAA"
NEW = "BBBBBBBBBBBBBBBBBBBBBQ"
HASH = "ab" * 32

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


def queued(identifier, *, reservation=200, runner=OLD, owner=None):
    Operation.objects.create(
        id=identifier,
        conversation_id=CONVERSATION,
        owner_id=OWNER.id if owner is None else owner,
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
        "blocks": operation.blocks_conversation,
        "runner": operation.runner_id,
        "reservation": operation.reservation_expires_at_us,
        "request": operation.request_content,
        "slot_operation": (
            None if slot.operation_id is None else str(slot.operation_id)
        ),
        "slot_runner": slot.runner_id,
        "slot_reservation": slot.reservation_expires_at_us,
        "slot_acquired": slot.acquired_at_us,
    }
"""


def _run(body, *, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(Path(temporary) / "r3c3.sqlite3"),
            "PYTHONPATH": str(SRC_ROOT),
        }
        return subprocess.run(
            (sys.executable, "-B", "-c", _PREAMBLE + "\n" + textwrap.dedent(body)),
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )


def _payload(testcase, result):
    testcase.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
    return json.loads(result.stdout)


class QueuedAdoptionTests(unittest.TestCase):
    def test_adoption_transfers_both_runners_without_extending_reservation(self):
        result = _run(
            """\
            identifier = uuid.UUID("11111111111111111111111111111111")
            queued(identifier, reservation=200)
            clock = Clock(199)
            outcome = SqliteConversationRepository(clock, NEW).adopt_queued(
                OWNER.id,
                identifier,
            )
            print(json.dumps({
                "type": type(outcome).__name__,
                "runner": outcome.reservation.runner_id,
                "reservation": outcome.reservation.reservation_expires_at,
                "calls": clock.calls,
                "state": state(identifier),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "Adopted")
        self.assertEqual(payload["runner"], "BBBBBBBBBBBBBBBBBBBBBQ")
        self.assertEqual(payload["reservation"], 200)
        self.assertEqual(payload["calls"], 1)
        self.assertEqual(payload["state"]["runner"], "BBBBBBBBBBBBBBBBBBBBBQ")
        self.assertEqual(payload["state"]["slot_runner"], "BBBBBBBBBBBBBBBBBBBBBQ")
        self.assertEqual(payload["state"]["reservation"], 200)
        self.assertEqual(payload["state"]["slot_reservation"], 200)
        self.assertEqual(payload["state"]["slot_acquired"], 7)
        self.assertEqual(payload["state"]["request"], "hello")

    def test_same_runner_is_already_owned_without_rewrite(self):
        result = _run(
            """\
            identifier = uuid.UUID("22222222222222222222222222222222")
            queued(identifier)
            calls = {"update": 0}
            real = QuerySet.update
            def counting(self, **kwargs):
                calls["update"] += 1
                return real(self, **kwargs)
            QuerySet.update = counting
            try:
                outcome = SqliteConversationRepository(Clock(100), OLD).adopt_queued(
                    OWNER.id,
                    identifier,
                )
            finally:
                QuerySet.update = real
            print(json.dumps({
                "type": type(outcome).__name__,
                "updates": calls["update"],
                "runner": outcome.reservation.runner_id,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "AlreadyOwned")
        self.assertEqual(payload["updates"], 0)
        self.assertEqual(payload["runner"], "AAAAAAAAAAAAAAAAAAAAAA")

    def test_exact_expiry_cancels_and_releases_without_transfer(self):
        result = _run(
            """\
            identifier = uuid.UUID("33333333333333333333333333333333")
            queued(identifier, reservation=100)
            outcome = SqliteConversationRepository(Clock(100), NEW).adopt_queued(
                OWNER.id,
                identifier,
            )
            print(json.dumps({
                "type": type(outcome).__name__,
                "state": state(identifier),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "ExpiredResubmit")
        self.assertEqual(payload["state"]["status"], "CANCELLED")
        self.assertEqual(payload["state"]["blocks"], 0)
        self.assertIsNone(payload["state"]["runner"])
        self.assertIsNone(payload["state"]["slot_operation"])
        self.assertIsNone(payload["state"]["slot_runner"])

    def test_wrong_owner_stale_runner_mismatch_and_nonqueued_do_not_steal(self):
        result = _run(
            """\
            identifier = uuid.UUID("44444444444444444444444444444444")
            queued(identifier, reservation=200)
            repository = SqliteConversationRepository(Clock(100), NEW)
            missing = repository.adopt_queued(OWNER.id, uuid.uuid4())
            cross = repository.adopt_queued(OTHER.id, identifier)
            Operation.objects.filter(pk=identifier).update(runner_id=NEW)
            stale = repository.adopt_queued(OWNER.id, identifier)
            stale_state = state(identifier)
            Operation.objects.filter(pk=identifier).update(runner_id=OLD)
            GlobalModelSlot.objects.filter(pk=1).update(operation_id=None, runner_id=None, reservation_expires_at_us=None, acquired_at_us=None)
            mismatch = repository.adopt_queued(OWNER.id, identifier)
            mismatch_state = state(identifier)
            Operation.objects.filter(pk=identifier).update(
                status="RUNNING",
                reservation_expires_at_us=None,
                provider_started_at_us=1,
                execution_deadline_us=2,
                lease_expires_at_us=2,
            )
            running = repository.adopt_queued(OWNER.id, identifier)
            Operation.objects.filter(pk=identifier).update(
                status="SUCCEEDED",
                blocks_conversation=0,
                runner_id=None,
                finished_at_us=3,
            )
            succeeded = repository.adopt_queued(OWNER.id, identifier)
            cancelled_id = uuid.UUID("44444444444444444444444444444445")
            Operation.objects.create(
                id=cancelled_id,
                conversation_id=CONVERSATION,
                owner_id=OWNER.id,
                operation_type="message",
                provider_id="openai",
                model_id="gpt-5.6-sol",
                status="CANCELLED",
                blocks_conversation=0,
                expected_version=4,
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
                finished_at_us=3,
                acknowledged_at_us=None,
                failure_category=None,
            )
            cancelled = repository.adopt_queued(OWNER.id, cancelled_id)
            print(json.dumps({
                "missing": type(missing).__name__,
                "cross": type(cross).__name__,
                "equal": missing == cross,
                "stale": type(stale).__name__,
                "stale_runner": stale_state["runner"],
                "mismatch": type(mismatch).__name__,
                "mismatch_runner": mismatch_state["runner"],
                "running": type(running).__name__,
                "succeeded": type(succeeded).__name__,
                "cancelled": type(cancelled).__name__,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["missing"], "NotFound")
        self.assertEqual(payload["cross"], "NotFound")
        self.assertTrue(payload["equal"])
        self.assertEqual(payload["stale"], "OwnershipLost")
        self.assertEqual(payload["stale_runner"], "BBBBBBBBBBBBBBBBBBBBBQ")
        self.assertEqual(payload["mismatch"], "SlotMismatch")
        self.assertEqual(payload["mismatch_runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(payload["running"], "NotQueued")
        self.assertEqual(payload["succeeded"], "NotQueued")
        self.assertEqual(payload["cancelled"], "ExpiredResubmit")

    def test_zero_row_and_injected_failure_roll_back_both_runners(self):
        result = _run(
            """\
            identifier = uuid.UUID("55555555555555555555555555555555")
            queued(identifier, reservation=200)
            repository = SqliteConversationRepository(Clock(100), NEW)
            real = QuerySet.update
            def zero_runner(self, **kwargs):
                if self.model is Operation and kwargs.get("runner_id") == NEW:
                    return 0
                return real(self, **kwargs)
            QuerySet.update = zero_runner
            try:
                zero = repository.adopt_queued(OWNER.id, identifier)
            finally:
                QuerySet.update = real
            zero_state = state(identifier)

            def fail_slot(self, **kwargs):
                if self.model is GlobalModelSlot and kwargs.get("runner_id") == NEW:
                    raise RuntimeError("injected slot failure")
                return real(self, **kwargs)
            QuerySet.update = fail_slot
            try:
                try:
                    repository.adopt_queued(OWNER.id, identifier)
                except RuntimeError as error:
                    failure = str(error)
                else:
                    failure = "accepted"
            finally:
                QuerySet.update = real
            print(json.dumps({
                "zero": type(zero).__name__,
                "failure": failure,
                "zero_state": zero_state,
                "failure_state": state(identifier),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["zero"], "OwnershipLost")
        self.assertEqual(payload["failure"], "injected slot failure")
        self.assertEqual(payload["zero_state"]["runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(payload["failure_state"]["runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(
            payload["failure_state"]["slot_runner"],
            "AAAAAAAAAAAAAAAAAAAAAA",
        )

    def test_writer_contention_and_commit_ambiguity(self):
        result = _run(
            """\
            identifier = uuid.UUID("66666666666666666666666666666666")
            queued(identifier, reservation=200)
            clock = Clock(100)
            repository = SqliteConversationRepository(clock, NEW)
            holder = sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True)
            try:
                holder.execute("BEGIN IMMEDIATE")
                busy = repository.adopt_queued(OWNER.id, identifier)
            finally:
                holder.rollback()
                holder.close()
            busy_calls = clock.calls
            real_commit = connection.commit
            def fail_commit(*args, **kwargs):
                raise OperationalError("injected commit failure")
            connection.commit = fail_commit
            try:
                ambiguous = repository.adopt_queued(OWNER.id, identifier)
            finally:
                connection.commit = real_commit
            connection.close()
            print(json.dumps({
                "busy": type(busy).__name__,
                "busy_calls": busy_calls,
                "ambiguous": type(ambiguous).__name__,
                "operation_id": str(ambiguous.operation_id),
                "runner": ambiguous.runner_id,
                "state": state(identifier),
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
        self.assertEqual(payload["runner"], "BBBBBBBBBBBBBBBBBBBBBQ")
        self.assertEqual(payload["state"]["runner"], "AAAAAAAAAAAAAAAAAAAAAA")

    def test_frozen_schema_inventory_remains_unchanged(self):
        sys.path.insert(0, str(SRC_ROOT))
        from max_smart_db.sql_policy import scan_migration_package

        inventory = scan_migration_package()
        self.assertEqual(sum(len(v) for v in inventory.forward_sql.values()), 40)
        self.assertEqual(sum(len(v) for v in inventory.reverse_sql.values()), 40)


if __name__ == "__main__":
    unittest.main()
