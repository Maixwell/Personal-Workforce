"""Disposable-database tests for fenced global-slot recovery."""

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
from max_smart_db.repository import (
    CancelledExpiredQueued,
    CommitAmbiguous,
    DatabaseBusy,
    InconsistentSlot,
    LiveQueued,
    LiveRunning,
    MarkedOutcomeUnknown,
    OwnershipMismatch,
    SlotFree,
)
import max_smart_db.recovery as recovery

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

OWNER = User.objects.create(username="owner")
CONVERSATION = uuid.UUID("00112233445566778899aabbccddeeff")
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
OTHER_RUNNER = "BBBBBBBBBBBBBBBBBBBBBQ"

with connection.cursor() as cursor:
    cursor.execute(
        "INSERT INTO conversation "
        "(id, owner_id, role_slug, version, next_sequence, title, "
        "created_at_us, updated_at_us, archived_at_us) "
        "VALUES (%s, %s, 'max-smart', 0, 1, NULL, 0, 0, NULL)",
        (CONVERSATION.hex, OWNER.id),
    )


class Clock:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def now_us(self):
        self.calls += 1
        return self.value


def insert_operation(identifier, status, *, reservation=None, started=None,
                     deadline=None, lease=None, runner=RUNNER):
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO operation "
            "(id, conversation_id, owner_id, operation_type, provider_id, "
            "model_id, status, blocks_conversation, expected_version, "
            "request_content, fingerprint_version, request_hash, retry_of, "
            "runner_id, reservation_expires_at_us, provider_started_at_us, "
            "execution_deadline_us, lease_expires_at_us, created_at_us, "
            "finished_at_us, acknowledged_at_us, failure_category) "
            "VALUES (%s, %s, %s, 'message', 'openai', 'gpt-5.6-sol', "
            "%s, 1, 0, 'request', 1, %s, NULL, %s, %s, %s, %s, %s, "
            "0, NULL, NULL, NULL)",
            (
                identifier.hex,
                CONVERSATION.hex,
                OWNER.id,
                status,
                "a" * 64,
                runner,
                reservation,
                started,
                deadline,
                lease,
            ),
        )


def occupy_slot(identifier, *, reservation=None, lease=None, runner=RUNNER):
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE global_model_slot SET operation_id = %s, runner_id = %s, "
            "reservation_expires_at_us = %s, lease_expires_at_us = %s, "
            "acquired_at_us = 1 WHERE id = 1",
            (identifier.hex, runner, reservation, lease),
        )


def durable_state(identifier):
    operation = Operation.objects.get(pk=identifier)
    slot = GlobalModelSlot.objects.get(pk=1)
    return {
        "operation": [
            operation.status,
            operation.blocks_conversation,
            operation.runner_id,
            operation.reservation_expires_at_us,
            operation.provider_started_at_us,
            operation.execution_deadline_us,
            operation.lease_expires_at_us,
            operation.finished_at_us,
            operation.failure_category,
        ],
        "slot": [
            None if slot.operation_id is None else str(slot.operation_id),
            slot.runner_id,
            slot.reservation_expires_at_us,
            slot.lease_expires_at_us,
            slot.acquired_at_us,
        ],
    }
"""


def _run_disposable(body, *, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        database = Path(temporary) / "r3c1.sqlite3"
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(database),
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


class SlotRecoveryTests(unittest.TestCase):
    def test_free_and_live_states_are_classified_without_mutation(self):
        result = _run_disposable(
            """\
            clock = Clock(100)
            repository = SqliteConversationRepository(clock, OTHER_RUNNER)
            free = repository.recover_slot()

            queued_id = uuid.UUID("11111111111111111111111111111111")
            insert_operation(queued_id, "QUEUED", reservation=101)
            occupy_slot(queued_id, reservation=101)
            live_queued = repository.recover_slot()
            queued_state = durable_state(queued_id)

            Operation.objects.filter(pk=queued_id).update(
                status="CANCELLED",
                blocks_conversation=0,
                runner_id=None,
                reservation_expires_at_us=None,
                finished_at_us=100,
            )
            GlobalModelSlot.objects.filter(pk=1).update(
                operation_id=None,
                runner_id=None,
                reservation_expires_at_us=None,
                lease_expires_at_us=None,
                acquired_at_us=None,
            )
            running_id = uuid.UUID("22222222222222222222222222222222")
            insert_operation(
                running_id,
                "RUNNING",
                started=10,
                deadline=101,
                lease=101,
            )
            occupy_slot(running_id, lease=101)
            live_running = repository.recover_slot()
            running_state = durable_state(running_id)
            print(json.dumps({
                "types": [
                    type(free).__name__,
                    type(live_queued).__name__,
                    type(live_running).__name__,
                ],
                "calls": clock.calls,
                "queued": queued_state,
                "running": running_state,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(
            payload["types"],
            ["SlotFree", "LiveQueued", "LiveRunning"],
        )
        self.assertEqual(payload["calls"], 3)
        self.assertEqual(payload["queued"]["operation"][0], "QUEUED")
        self.assertEqual(payload["queued"]["slot"][0], str(
            __import__("uuid").UUID("11111111111111111111111111111111")
        ))
        self.assertEqual(payload["running"]["operation"][0], "RUNNING")
        self.assertEqual(payload["running"]["slot"][0], str(
            __import__("uuid").UUID("22222222222222222222222222222222")
        ))

    def test_queued_expiry_recovers_at_exact_boundary(self):
        result = _run_disposable(
            """\
            identifier = uuid.UUID("33333333333333333333333333333333")
            insert_operation(identifier, "QUEUED", reservation=100)
            occupy_slot(identifier, reservation=100)
            clock = Clock(100)
            outcome = SqliteConversationRepository(
                clock, OTHER_RUNNER
            ).recover_slot()
            print(json.dumps({
                "type": type(outcome).__name__,
                "calls": clock.calls,
                "state": durable_state(identifier),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "CancelledExpiredQueued")
        self.assertEqual(payload["calls"], 1)
        self.assertEqual(
            payload["state"]["operation"],
            ["CANCELLED", 0, None, None, None, None, None, 100, None],
        )
        self.assertEqual(
            payload["state"]["slot"],
            [None, None, None, None, None],
        )

    def test_running_requires_both_deadline_and_lease_expiry(self):
        scenarios = (
            ("both", 100, 100, "MarkedOutcomeUnknown"),
            ("deadline_only", 100, 101, "LiveRunning"),
        )
        for label, deadline, lease, expected in scenarios:
            with self.subTest(label=label):
                result = _run_disposable(
                    f"""\
                    identifier = uuid.UUID("44444444444444444444444444444444")
                    insert_operation(
                        identifier,
                        "RUNNING",
                        started=10,
                        deadline={deadline},
                        lease={lease},
                    )
                    occupy_slot(identifier, lease={lease})
                    clock = Clock(100)
                    outcome = SqliteConversationRepository(
                        clock, OTHER_RUNNER
                    ).recover_slot()
                    print(json.dumps({{
                        "type": type(outcome).__name__,
                        "calls": clock.calls,
                        "state": durable_state(identifier),
                    }}))
                    """
                )
                payload = _payload(self, result)
                self.assertEqual(payload["type"], expected)
                self.assertEqual(payload["calls"], 1)
                if expected == "MarkedOutcomeUnknown":
                    self.assertEqual(
                        payload["state"]["operation"],
                        [
                            "OUTCOME_UNKNOWN",
                            1,
                            "AAAAAAAAAAAAAAAAAAAAAA",
                            None,
                            10,
                            100,
                            100,
                            100,
                            None,
                        ],
                    )
                    self.assertEqual(
                        payload["state"]["slot"],
                        [None, None, None, None, None],
                    )
                else:
                    self.assertEqual(
                        payload["state"]["operation"][0],
                        "RUNNING",
                    )
                    self.assertIsNotNone(payload["state"]["slot"][0])

    def test_lease_only_expiry_is_rejected_by_the_frozen_timing_check(self):
        result = _run_disposable(
            """\
            try:
                insert_operation(
                    uuid.UUID("45454545454545454545454545454545"),
                    "RUNNING",
                    started=10,
                    deadline=101,
                    lease=100,
                )
            except Exception as error:
                rejected = (
                    type(error).__name__,
                    "msv1_op_timing_ck" in str(error),
                )
            else:
                rejected = ("accepted", False)
            print(json.dumps(rejected))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload, ["IntegrityError", True])

    def test_stale_runner_and_slot_correspondence_are_typed(self):
        for label, slot_runner, slot_lease, expected in (
            (
                "runner",
                "BBBBBBBBBBBBBBBBBBBBBQ",
                100,
                "OwnershipMismatch",
            ),
            (
                "lease",
                "AAAAAAAAAAAAAAAAAAAAAA",
                99,
                "InconsistentSlot",
            ),
        ):
            with self.subTest(label=label):
                result = _run_disposable(
                    f"""\
                    identifier = uuid.UUID("55555555555555555555555555555555")
                    insert_operation(
                        identifier,
                        "RUNNING",
                        started=10,
                        deadline=100,
                        lease=100,
                    )
                    occupy_slot(
                        identifier,
                        lease={slot_lease},
                        runner={slot_runner!r},
                    )
                    clock = Clock(100)
                    outcome = SqliteConversationRepository(
                        clock, OTHER_RUNNER
                    ).recover_slot()
                    print(json.dumps({{
                        "type": type(outcome).__name__,
                        "calls": clock.calls,
                        "state": durable_state(identifier),
                    }}))
                    """
                )
                payload = _payload(self, result)
                self.assertEqual(payload["type"], expected)
                self.assertEqual(payload["calls"], 1)
                self.assertEqual(payload["state"]["operation"][0], "RUNNING")
                self.assertIsNotNone(payload["state"]["slot"][0])

    def test_zero_row_and_failure_after_first_update_roll_back(self):
        result = _run_disposable(
            """\
            identifier = uuid.UUID("66666666666666666666666666666666")
            insert_operation(identifier, "QUEUED", reservation=100)
            occupy_slot(identifier, reservation=100)
            clock = Clock(100)
            repository = SqliteConversationRepository(clock, OTHER_RUNNER)
            real_update = QuerySet.update

            def zero_operation(self, **kwargs):
                if self.model is Operation:
                    return 0
                return real_update(self, **kwargs)

            QuerySet.update = zero_operation
            try:
                zero = repository.recover_slot()
            finally:
                QuerySet.update = real_update
            zero_state = durable_state(identifier)

            real_release = recovery._release_slot
            def fail_release(slot, operation):
                raise RuntimeError("injected after operation update")
            recovery._release_slot = fail_release
            try:
                try:
                    repository.recover_slot()
                except RuntimeError as error:
                    failure = str(error)
                else:
                    failure = "accepted"
            finally:
                recovery._release_slot = real_release
            connection.close()
            rollback_state = durable_state(identifier)
            print(json.dumps({
                "zero": type(zero).__name__,
                "failure": failure,
                "calls": clock.calls,
                "zero_state": zero_state,
                "rollback_state": rollback_state,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["zero"], "InconsistentSlot")
        self.assertEqual(payload["failure"], "injected after operation update")
        self.assertEqual(payload["calls"], 2)
        self.assertEqual(payload["zero_state"], payload["rollback_state"])
        self.assertEqual(payload["rollback_state"]["operation"][0], "QUEUED")
        self.assertIsNotNone(payload["rollback_state"]["slot"][0])

    def test_writer_contention_and_commit_ambiguity_are_distinct(self):
        result = _run_disposable(
            """\
            identifier = uuid.UUID("77777777777777777777777777777777")
            insert_operation(identifier, "QUEUED", reservation=100)
            occupy_slot(identifier, reservation=100)
            clock = Clock(100)
            repository = SqliteConversationRepository(clock, OTHER_RUNNER)

            holder = sqlite3.connect(
                f"{database.as_uri()}?mode=rw",
                uri=True,
            )
            try:
                holder.execute("BEGIN IMMEDIATE")
                busy = repository.recover_slot()
            finally:
                holder.rollback()
                holder.close()
            busy_calls = clock.calls

            real_commit = connection.commit
            def fail_commit(*args, **kwargs):
                raise OperationalError("injected commit failure")
            connection.commit = fail_commit
            try:
                ambiguous = repository.recover_slot()
            finally:
                connection.commit = real_commit
            connection.close()
            state = durable_state(identifier)
            print(json.dumps({
                "busy": type(busy).__name__,
                "busy_calls": busy_calls,
                "ambiguous": type(ambiguous).__name__,
                "ambiguous_id": str(ambiguous.operation_id),
                "calls": clock.calls,
                "state": state,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["busy"], "DatabaseBusy")
        self.assertEqual(payload["busy_calls"], 0)
        self.assertEqual(payload["ambiguous"], "CommitAmbiguous")
        self.assertEqual(
            payload["ambiguous_id"],
            "77777777-7777-7777-7777-777777777777",
        )
        self.assertEqual(payload["calls"], 1)
        self.assertEqual(payload["state"]["operation"][0], "QUEUED")
        self.assertIsNotNone(payload["state"]["slot"][0])

    def test_frozen_migration_inventory_is_unchanged(self):
        sys.path.insert(0, str(SRC_ROOT))
        from max_smart_db.sql_policy import scan_migration_package

        inventory = scan_migration_package()
        self.assertEqual(
            sum(len(values) for values in inventory.forward_sql.values()),
            40,
        )
        self.assertEqual(
            sum(len(values) for values in inventory.reverse_sql.values()),
            40,
        )
        self.assertEqual(
            tuple(sorted(
                path.name
                for path in (
                    SRC_ROOT / "max_smart_db" / "migrations"
                ).glob("*.py")
            )),
            (
                "0001_v1_schema.py",
                "0002_v1_integrity_triggers.py",
                "0003_v1_seeds_and_retention.py",
                "__init__.py",
            ),
        )


if __name__ == "__main__":
    unittest.main()
