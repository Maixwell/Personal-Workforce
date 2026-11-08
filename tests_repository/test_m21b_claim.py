"""Disposable-database tests for the fenced CLAIM transaction."""

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
from max_smart_db.models import Conversation, GlobalModelSlot, Operation
from max_smart_db.repository import RepositoryIntegrityError

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


def queued(identifier, *, reservation=1_000_000_000, runner=RUNNER):
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
        "started": operation.provider_started_at_us,
        "deadline": operation.execution_deadline_us,
        "lease": operation.lease_expires_at_us,
        "request": operation.request_content,
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
                Path(temporary) / "r3c4.sqlite3"
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


class ClaimRunningTests(unittest.TestCase):
    def test_success_uses_exact_claim_deadline_and_fixed_lease(self):
        result = _run(
            """\
            identifier = uuid.UUID("11111111111111111111111111111111")
            queued(identifier)
            clock = Clock(123_456)
            outcome = SqliteConversationRepository(
                clock,
                RUNNER,
            ).claim_running(OWNER.id, identifier)
            claimed = outcome.claimed_operation
            print(json.dumps({
                "type": type(outcome).__name__,
                "calls": clock.calls,
                "claimed": [
                    str(claimed.operation_id),
                    claimed.runner_id,
                    claimed.claimed_at,
                    claimed.execution_deadline,
                    claimed.lease_expires_at,
                ],
                "state": state(identifier),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "Claimed")
        self.assertEqual(payload["calls"], 1)
        self.assertEqual(
            payload["claimed"],
            [
                "11111111-1111-1111-1111-111111111111",
                "AAAAAAAAAAAAAAAAAAAAAA",
                123_456,
                600_123_456,
                720_123_456,
            ],
        )
        current = payload["state"]
        self.assertEqual(current["status"], "RUNNING")
        self.assertEqual(current["started"], 123_456)
        self.assertEqual(current["deadline"], 600_123_456)
        self.assertEqual(current["lease"], 720_123_456)
        self.assertIsNone(current["reservation"])
        self.assertEqual(current["slot_lease"], 720_123_456)
        self.assertIsNone(current["slot_reservation"])
        self.assertEqual(current["slot_acquired"], 7)
        self.assertEqual(current["request"], "hello")

    def test_exact_expiry_cancels_releases_and_never_claims(self):
        result = _run(
            """\
            identifier = uuid.UUID("22222222222222222222222222222222")
            queued(identifier, reservation=100)
            provider_calls = 0
            outcome = SqliteConversationRepository(
                Clock(100),
                RUNNER,
            ).claim_running(OWNER.id, identifier)
            if type(outcome).__name__ == "Claimed":
                provider_calls += 1
            print(json.dumps({
                "type": type(outcome).__name__,
                "provider_calls": provider_calls,
                "state": state(identifier),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "ExpiredResubmit")
        self.assertEqual(payload["provider_calls"], 0)
        current = payload["state"]
        self.assertEqual(current["status"], "CANCELLED")
        self.assertEqual(current["blocks"], 0)
        self.assertIsNone(current["started"])
        self.assertIsNone(current["slot_operation"])

    def test_wrong_owner_stale_runner_mismatch_and_nonqueued(self):
        result = _run(
            """\
            identifier = uuid.UUID("33333333333333333333333333333333")
            queued(identifier)
            repository = SqliteConversationRepository(Clock(100), RUNNER)
            missing = repository.claim_running(OWNER.id, uuid.uuid4())
            cross = repository.claim_running(OTHER.id, identifier)
            stale_repository = SqliteConversationRepository(Clock(100), STALE)
            stale = stale_repository.claim_running(OWNER.id, identifier)
            stale_state = state(identifier)
            GlobalModelSlot.objects.filter(pk=1).update(
                operation_id=None,
                runner_id=None,
                reservation_expires_at_us=None,
                acquired_at_us=None,
            )
            mismatch = repository.claim_running(OWNER.id, identifier)
            mismatch_state = state(identifier)
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
            running = repository.claim_running(OWNER.id, identifier)
            Operation.objects.filter(pk=identifier).update(
                status="SUCCEEDED",
                blocks_conversation=0,
                runner_id=None,
                finished_at_us=3,
            )
            terminal = repository.claim_running(OWNER.id, identifier)
            print(json.dumps({
                "missing": type(missing).__name__,
                "cross": type(cross).__name__,
                "same_not_found": missing == cross,
                "stale": type(stale).__name__,
                "stale_status": stale_state["status"],
                "mismatch": type(mismatch).__name__,
                "mismatch_status": mismatch_state["status"],
                "running": type(running).__name__,
                "terminal": type(terminal).__name__,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["missing"], "NotFound")
        self.assertEqual(payload["cross"], "NotFound")
        self.assertTrue(payload["same_not_found"])
        self.assertEqual(payload["stale"], "OwnershipLost")
        self.assertEqual(payload["stale_status"], "QUEUED")
        self.assertEqual(payload["mismatch"], "SlotMismatch")
        self.assertEqual(payload["mismatch_status"], "QUEUED")
        self.assertEqual(payload["running"], "NotQueued")
        self.assertEqual(payload["terminal"], "NotQueued")

    def test_signed_integer_overflow_fails_closed_and_rolls_back(self):
        result = _run(
            """\
            maximum = 9_223_372_036_854_775_807
            now = maximum - 720_000_000 + 1
            identifier = uuid.UUID("44444444444444444444444444444444")
            queued(identifier, reservation=maximum)
            clock = Clock(now)
            try:
                SqliteConversationRepository(
                    clock,
                    RUNNER,
                ).claim_running(OWNER.id, identifier)
            except RepositoryIntegrityError:
                outcome = "RepositoryIntegrityError"
            else:
                outcome = "accepted"
            print(json.dumps({
                "outcome": outcome,
                "calls": clock.calls,
                "state": state(identifier),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["outcome"], "RepositoryIntegrityError")
        self.assertEqual(payload["calls"], 1)
        self.assertEqual(payload["state"]["status"], "QUEUED")
        self.assertIsNone(payload["state"]["started"])
        self.assertEqual(
            payload["state"]["slot_reservation"],
            9_223_372_036_854_775_807,
        )

    def test_zero_row_and_partial_failure_roll_back_operation_and_slot(self):
        result = _run(
            """\
            identifier = uuid.UUID("55555555555555555555555555555555")
            queued(identifier)
            repository = SqliteConversationRepository(Clock(100), RUNNER)
            real = QuerySet.update
            def zero_operation(self, **kwargs):
                if self.model is Operation and kwargs.get("status") == "RUNNING":
                    return 0
                return real(self, **kwargs)
            QuerySet.update = zero_operation
            try:
                zero = repository.claim_running(OWNER.id, identifier)
            finally:
                QuerySet.update = real
            zero_state = state(identifier)

            def fail_slot(self, **kwargs):
                if (
                    self.model is GlobalModelSlot
                    and kwargs.get("lease_expires_at_us") is not None
                ):
                    raise RuntimeError("injected slot failure")
                return real(self, **kwargs)
            QuerySet.update = fail_slot
            try:
                try:
                    repository.claim_running(OWNER.id, identifier)
                except RuntimeError as error:
                    failure = str(error)
                else:
                    failure = "accepted"
            finally:
                QuerySet.update = real
            print(json.dumps({
                "zero": type(zero).__name__,
                "zero_state": zero_state,
                "failure": failure,
                "failure_state": state(identifier),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["zero"], "OwnershipLost")
        self.assertEqual(payload["zero_state"]["status"], "QUEUED")
        self.assertEqual(payload["failure"], "injected slot failure")
        self.assertEqual(payload["failure_state"]["status"], "QUEUED")
        self.assertIsNone(payload["failure_state"]["started"])
        self.assertIsNone(payload["failure_state"]["slot_lease"])

    def test_contention_and_commit_ambiguity_preserve_reconciliation_data(self):
        result = _run(
            """\
            identifier = uuid.UUID("66666666666666666666666666666666")
            queued(identifier)
            clock = Clock(321)
            repository = SqliteConversationRepository(clock, RUNNER)
            holder = sqlite3.connect(
                f"{database.as_uri()}?mode=rw",
                uri=True,
            )
            try:
                holder.execute("BEGIN IMMEDIATE")
                busy = repository.claim_running(OWNER.id, identifier)
            finally:
                holder.rollback()
                holder.close()
            busy_calls = clock.calls
            real_commit = connection.commit
            def fail_commit(*args, **kwargs):
                raise OperationalError("injected commit uncertainty")
            connection.commit = fail_commit
            try:
                ambiguous = repository.claim_running(OWNER.id, identifier)
            finally:
                connection.commit = real_commit
            connection.close()
            provider_calls = 0
            if type(ambiguous).__name__ == "Claimed":
                provider_calls += 1
            print(json.dumps({
                "busy": type(busy).__name__,
                "busy_calls": busy_calls,
                "ambiguous": type(ambiguous).__name__,
                "operation_id": str(ambiguous.operation_id),
                "runner": ambiguous.runner_id,
                "claimed_at": ambiguous.claimed_at,
                "provider_calls": provider_calls,
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
        self.assertEqual(payload["runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(payload["claimed_at"], 321)
        self.assertEqual(payload["provider_calls"], 0)
        self.assertEqual(payload["state"]["status"], "QUEUED")

    def test_timing_constants_source_and_frozen_inventory(self):
        sys.path.insert(0, str(SRC_ROOT))
        from max_smart_db.config import (
            EXECUTION_DEADLINE_US,
            FIXED_LEASE_US,
            RECOVERY_CLOCK_SKEW_MARGIN_US,
        )
        from max_smart_db.sql_policy import scan_migration_package

        self.assertEqual(EXECUTION_DEADLINE_US, 600_000_000)
        self.assertEqual(FIXED_LEASE_US, 720_000_000)
        self.assertEqual(RECOVERY_CLOCK_SKEW_MARGIN_US, 120_000_000)
        inventory = scan_migration_package()
        self.assertEqual(sum(len(v) for v in inventory.forward_sql.values()), 40)
        self.assertEqual(sum(len(v) for v in inventory.reverse_sql.values()), 40)
        source = (SRC_ROOT / "max_smart_db" / "claim.py").read_text()
        self.assertNotIn("provider.", source)
        self.assertNotIn("responses.create", source)


if __name__ == "__main__":
    unittest.main()
