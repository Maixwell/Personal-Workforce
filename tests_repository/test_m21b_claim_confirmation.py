"""Disposable-database proof of a durable RUNNING claim and its slot."""

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
import hashlib
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
from django.db import IntegrityError, OperationalError, connection
from django.db.migrations.executor import MigrationExecutor
from max_smart_db.creation import SqliteConversationRepository
from max_smart_db.models import Conversation, GlobalModelSlot, Operation

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

OWNER = User.objects.create(username="owner")
OTHER = User.objects.create(username="other")
CONVERSATION = uuid.UUID("00112233445566778899aabbccddeeff")
OPERATION = uuid.UUID("11111111111111111111111111111111")
OTHER_OPERATION = uuid.UUID("22222222222222222222222222222222")
MISSING = uuid.UUID("33333333333333333333333333333333")
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
STALE = "BBBBBBBBBBBBBBBBBBBBBQ"
HASH = "ab" * 32
CLAIMED_AT = 123_456
DEADLINE = CLAIMED_AT + 600_000_000
LEASE = CLAIMED_AT + 720_000_000
PROVIDER_CALLS = 0


class Clock:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def now_us(self):
        self.calls += 1
        return self.value


def repository():
    return SqliteConversationRepository(Clock(CLAIMED_AT), RUNNER)


def queued(identifier):
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
        operation_id=identifier,
        runner_id=RUNNER,
        reservation_expires_at_us=1_000_000_000,
        lease_expires_at_us=None,
        acquired_at_us=7,
    )


def release_slot():
    GlobalModelSlot.objects.filter(pk=1).update(
        operation_id=None,
        runner_id=None,
        reservation_expires_at_us=None,
        lease_expires_at_us=None,
        acquired_at_us=None,
    )


def rewind_queued():
    if Operation.objects.filter(pk=OPERATION).exists():
        Operation.objects.filter(pk=OPERATION).update(
            status="QUEUED",
            blocks_conversation=1,
            runner_id=RUNNER,
            reservation_expires_at_us=1_000_000_000,
            provider_started_at_us=None,
            execution_deadline_us=None,
            lease_expires_at_us=None,
            finished_at_us=None,
            acknowledged_at_us=None,
            failure_category=None,
        )
    else:
        queued(OPERATION)
    GlobalModelSlot.objects.filter(pk=1).update(
        operation_id=OPERATION,
        runner_id=RUNNER,
        reservation_expires_at_us=1_000_000_000,
        lease_expires_at_us=None,
        acquired_at_us=7,
    )


def reset_running():
    rewind_queued()
    outcome = repository().claim_running(OWNER.id, OPERATION)
    if type(outcome).__name__ != "Claimed":
        raise RuntimeError(type(outcome).__name__)
    return outcome


def fresh_rows():
    connection.close()
    fresh = sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True)
    try:
        operation = fresh.execute(
            "SELECT status, runner_id, provider_started_at_us, "
            "execution_deadline_us, lease_expires_at_us, "
            "reservation_expires_at_us FROM operation WHERE id = ?",
            (OPERATION.hex,),
        ).fetchone()
        slot = fresh.execute(
            "SELECT operation_id, runner_id, reservation_expires_at_us, "
            "lease_expires_at_us, acquired_at_us "
            "FROM global_model_slot WHERE id = 1"
        ).fetchone()
        count = fresh.execute("SELECT COUNT(*) FROM operation").fetchone()[0]
    finally:
        fresh.close()
    return {
        "operation": None if operation is None else list(operation),
        "slot": list(slot),
        "operation_count": count,
    }


def schema_digest():
    connection.close()
    fresh = sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True)
    try:
        rows = fresh.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL ORDER BY type, name"
        ).fetchall()
    finally:
        fresh.close()
    payload = "\\n".join(f"{kind}|{name}|{sql}" for kind, name, sql in rows)
    return {
        "digest": hashlib.sha256(payload.encode()).hexdigest(),
        "objects": len(rows),
    }


def confirm(actor_id, operation_id, runner_id, claimed_at):
    before = fresh_rows()
    outcome = repository().confirm_durable_claim(
        actor_id,
        operation_id,
        runner_id,
        claimed_at,
    )
    after = fresh_rows()
    return {
        "type": type(outcome).__name__,
        "operation_id": getattr(outcome, "operation_id", None),
        "runner_id": getattr(outcome, "runner_id", None),
        "claimed_at": getattr(outcome, "claimed_at", None),
        "stable": before == after,
        "provider_calls": PROVIDER_CALLS,
    }


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
"""


def _run(body, *, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "claim-confirmation.sqlite3"
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


class DurableClaimConfirmationTests(unittest.TestCase):
    def test_valid_running_claim_is_confirmed_without_mutation(self):
        result = _run(
            """\
            clock = Clock(CLAIMED_AT)
            repo = SqliteConversationRepository(clock, RUNNER)
            queued(OPERATION)
            schema_before = schema_digest()
            claimed = repo.claim_running(OWNER.id, OPERATION)
            proof = confirm(OWNER.id, OPERATION, RUNNER, CLAIMED_AT)
            print(json.dumps({
                "claim": type(claimed).__name__,
                "clock_calls": clock.calls,
                "proof": proof,
                "rows": fresh_rows(),
                "schema_before": schema_before,
                "schema_after": schema_digest(),
            }, default=str))
            """
        )
        payload = _payload(self, result)
        proof = payload["proof"]
        self.assertEqual(payload["claim"], "Claimed")
        self.assertEqual(payload["clock_calls"], 1)
        self.assertEqual(proof["type"], "DurableClaimConfirmed")
        self.assertEqual(
            proof["operation_id"],
            "11111111-1111-1111-1111-111111111111",
        )
        self.assertEqual(proof["runner_id"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(proof["claimed_at"], 123_456)
        self.assertTrue(proof["stable"])
        self.assertEqual(proof["provider_calls"], 0)
        self.assertEqual(
            payload["rows"]["operation"],
            ["RUNNING", "AAAAAAAAAAAAAAAAAAAAAA", 123_456, 600_123_456, 720_123_456, None],
        )
        self.assertEqual(payload["rows"]["slot"][1], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertIsNone(payload["rows"]["slot"][2])
        self.assertEqual(payload["rows"]["slot"][3], 720_123_456)
        self.assertIsNotNone(payload["rows"]["slot"][4])
        self.assertEqual(payload["schema_before"], payload["schema_after"])

    def test_mismatches_do_not_confirm_or_mutate(self):
        result = _run(
            """\
            def case(name, mutate=None, **arguments):
                reset_running()
                if mutate is not None:
                    mutate()
                proof = confirm(
                    arguments.get("actor_id", OWNER.id),
                    arguments.get("operation_id", OPERATION),
                    arguments.get("runner_id", RUNNER),
                    arguments.get("claimed_at", CLAIMED_AT),
                )
                proof["name"] = name
                return proof

            def wrong_deadline():
                Operation.objects.filter(pk=OPERATION).update(
                    execution_deadline_us=DEADLINE + 1,
                )

            def wrong_operation_lease():
                Operation.objects.filter(pk=OPERATION).update(
                    lease_expires_at_us=LEASE + 1,
                )
                GlobalModelSlot.objects.filter(pk=1).update(
                    lease_expires_at_us=LEASE + 1,
                )

            def point_slot_elsewhere():
                Operation.objects.create(
                    id=OTHER_OPERATION,
                    conversation_id=CONVERSATION,
                    owner_id=OWNER.id,
                    operation_type="message",
                    provider_id="openai",
                    model_id="gpt-5.6-sol",
                    status="SUCCEEDED",
                    blocks_conversation=0,
                    expected_version=4,
                    request_content="other",
                    fingerprint_version=1,
                    request_hash=HASH,
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
                GlobalModelSlot.objects.filter(pk=1).update(
                    operation_id=OTHER_OPERATION,
                    runner_id=RUNNER,
                    reservation_expires_at_us=None,
                    lease_expires_at_us=3,
                    acquired_at_us=7,
                )

            def slot_runner():
                GlobalModelSlot.objects.filter(pk=1).update(runner_id=STALE)

            def slot_lease():
                GlobalModelSlot.objects.filter(pk=1).update(
                    lease_expires_at_us=LEASE + 5,
                )

            def reserved_slot():
                GlobalModelSlot.objects.filter(pk=1).update(
                    reservation_expires_at_us=50,
                    lease_expires_at_us=None,
                )

            reset_running()
            occupied = fresh_rows()["slot"][4]
            try:
                GlobalModelSlot.objects.filter(pk=1).update(acquired_at_us=None)
                illegal = "accepted"
            except IntegrityError:
                illegal = "rejected"
            acquired_after_rejection = fresh_rows()["slot"][4]

            schema_before = schema_digest()
            proofs = [
                case("missing", operation_id=MISSING),
                case("cross_owner", actor_id=OTHER.id),
                case("wrong_runner", runner_id=STALE),
                case("wrong_claimed_at", claimed_at=CLAIMED_AT + 1),
                case("wrong_deadline", wrong_deadline),
                case("wrong_operation_lease", wrong_operation_lease),
                case("slot_operation", point_slot_elsewhere),
                case("slot_runner", slot_runner),
                case("slot_lease", slot_lease),
                case("reserved_slot", reserved_slot),
                case("absent_acquisition", release_slot),
            ]
            print(json.dumps({
                "illegal_absent_acquisition": illegal,
                "acquired_unchanged": acquired_after_rejection == occupied,
                "proofs": proofs,
                "schema_same": schema_before == schema_digest(),
            }, default=str))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["illegal_absent_acquisition"], "rejected")
        self.assertTrue(payload["acquired_unchanged"])
        self.assertTrue(payload["schema_same"])
        self.assertEqual(
            [proof["name"] for proof in payload["proofs"]],
            [
                "missing",
                "cross_owner",
                "wrong_runner",
                "wrong_claimed_at",
                "wrong_deadline",
                "wrong_operation_lease",
                "slot_operation",
                "slot_runner",
                "slot_lease",
                "reserved_slot",
                "absent_acquisition",
            ],
        )
        for proof in payload["proofs"]:
            self.assertEqual(proof["type"], "ClaimNotConfirmed", proof["name"])
            self.assertIsNone(proof["operation_id"])
            self.assertTrue(proof["stable"], proof["name"])
            self.assertEqual(proof["provider_calls"], 0)

    def test_ambiguous_commit_is_confirmed_only_after_it_is_durable(self):
        result = _run(
            """\
            def claim_with_commit(commit):
                rewind_queued()
                repo = repository()
                real_commit = connection.commit
                connection.commit = commit(real_commit)
                try:
                    outcome = repo.claim_running(OWNER.id, OPERATION)
                finally:
                    connection.commit = real_commit
                return outcome

            def fail_before(real_commit):
                def commit(*args, **kwargs):
                    raise OperationalError("simulated rollback before commit")
                return commit

            def succeed_then_fail(real_commit):
                def commit(*args, **kwargs):
                    real_commit(*args, **kwargs)
                    raise OperationalError("simulated post-commit transport failure")
                return commit

            rolled_back = claim_with_commit(fail_before)
            rolled_back_rows = fresh_rows()
            rolled_back_proof = confirm(OWNER.id, OPERATION, RUNNER, CLAIMED_AT)
            durable = claim_with_commit(succeed_then_fail)
            durable_proof = confirm(OWNER.id, OPERATION, RUNNER, CLAIMED_AT)
            print(json.dumps({
                "rolled_back_claim": type(rolled_back).__name__,
                "rolled_back_rows": rolled_back_rows,
                "rolled_back_proof": rolled_back_proof,
                "durable_claim": type(durable).__name__,
                "durable_claimed_at": getattr(durable, "claimed_at", None),
                "durable_proof": durable_proof,
                "durable_rows": fresh_rows(),
            }, default=str))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["rolled_back_claim"], "CommitAmbiguous")
        self.assertEqual(payload["rolled_back_rows"]["operation"][0], "QUEUED")
        self.assertEqual(payload["rolled_back_proof"]["type"], "ClaimNotConfirmed")
        self.assertTrue(payload["rolled_back_proof"]["stable"])
        self.assertEqual(payload["rolled_back_proof"]["provider_calls"], 0)
        self.assertEqual(payload["durable_claim"], "CommitAmbiguous")
        self.assertEqual(payload["durable_claimed_at"], 123_456)
        self.assertEqual(payload["durable_proof"]["type"], "DurableClaimConfirmed")
        self.assertEqual(payload["durable_proof"]["claimed_at"], 123_456)
        self.assertEqual(payload["durable_proof"]["runner_id"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertTrue(payload["durable_proof"]["stable"])
        self.assertEqual(payload["durable_proof"]["provider_calls"], 0)
        self.assertEqual(payload["durable_rows"]["operation"][0], "RUNNING")
        self.assertEqual(payload["durable_rows"]["operation"][2], 123_456)
        self.assertEqual(payload["durable_rows"]["slot"][3], 720_123_456)

    def test_confirmation_leaves_caller_transactions_untouched(self):
        result = _run(
            """\
            from django.db import connections

            def caller_state(cursor):
                cursor.execute(
                    "SELECT status FROM operation WHERE id = ?",
                    (OPERATION.hex,),
                )
                status = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT lease_expires_at_us FROM global_model_slot WHERE id = 1"
                )
                return {
                    "status": status,
                    "lease": cursor.fetchone()[0],
                    "autocommit": connection.get_autocommit(),
                    "identity": id(connection.connection),
                    "in_transaction": connection.connection.in_transaction,
                }

            reset_running()
            real_create = connections.create_connection

            def failing_create(alias):
                owned = real_create(alias)
                real_cursor = owned.cursor

                def cursor(*args, **kwargs):
                    wrapped = real_cursor(*args, **kwargs)
                    real_execute = wrapped.execute

                    def execute(sql, params=None):
                        if "global_model_slot" in str(sql):
                            raise OperationalError(
                                "simulated incomplete claim read"
                            )
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
                failed_outcome = repository().confirm_durable_claim(
                    OWNER.id,
                    OPERATION,
                    RUNNER,
                    CLAIMED_AT,
                )
            finally:
                connections.create_connection = real_create
            failed_identity = id(connection.connection)
            failed = {
                "type": type(failed_outcome).__name__,
                "provider_calls": PROVIDER_CALLS,
            }

            connection.ensure_connection()
            read_identity = id(connection.connection)
            read_autocommit = connection.get_autocommit()
            read_cursor = connection.cursor()
            read_cursor.execute("BEGIN DEFERRED")
            read_cursor.execute(
                "SELECT lease_expires_at_us FROM global_model_slot WHERE id = 1"
            )
            pinned = read_cursor.fetchone()[0]
            writer = connections.create_connection("default")
            try:
                with writer.cursor() as writer_cursor:
                    writer_cursor.execute("BEGIN IMMEDIATE")
                    writer_cursor.execute(
                        "UPDATE global_model_slot "
                        "SET lease_expires_at_us = ? WHERE id = 1",
                        (pinned + 9,),
                    )
                    writer_cursor.execute("COMMIT")
            finally:
                writer.close()
            read_proof = repository().confirm_durable_claim(
                OWNER.id,
                OPERATION,
                RUNNER,
                CLAIMED_AT,
            )
            read_cursor.execute(
                "SELECT lease_expires_at_us FROM global_model_slot WHERE id = 1"
            )
            read_still = read_cursor.fetchone()[0]
            read_open = connection.connection.in_transaction
            read_same = (
                id(connection.connection) == read_identity
                and connection.get_autocommit() == read_autocommit
            )
            read_cursor.execute("ROLLBACK")
            read_cursor.close()
            committed_lease = fresh_rows()["slot"][3]

            rewind_queued()
            connection.set_autocommit(False)
            write_identity = id(connection.connection)
            write_cursor = connection.cursor()
            write_cursor.execute("BEGIN IMMEDIATE")
            write_cursor.execute(
                "UPDATE operation SET status = 'RUNNING', "
                "reservation_expires_at_us = NULL, "
                "provider_started_at_us = ?, execution_deadline_us = ?, "
                "lease_expires_at_us = ? WHERE id = ?",
                (CLAIMED_AT, DEADLINE, LEASE, OPERATION.hex),
            )
            write_cursor.execute(
                "UPDATE global_model_slot SET reservation_expires_at_us = NULL, "
                "lease_expires_at_us = ? WHERE id = 1",
                (LEASE,),
            )
            false_before = caller_state(write_cursor)
            false_proof = repository().confirm_durable_claim(
                OWNER.id,
                OPERATION,
                RUNNER,
                CLAIMED_AT,
            )
            false_after = caller_state(write_cursor)
            connection.rollback()
            connection.set_autocommit(True)
            false_durable = fresh_rows()["operation"][0]

            reset_running()
            connection.set_autocommit(False)
            durable_identity = id(connection.connection)
            durable_cursor = connection.cursor()
            durable_cursor.execute("BEGIN IMMEDIATE")
            durable_cursor.execute(
                "UPDATE global_model_slot "
                "SET lease_expires_at_us = lease_expires_at_us + 11 WHERE id = 1"
            )
            durable_before = caller_state(durable_cursor)
            durable_proof = repository().confirm_durable_claim(
                OWNER.id,
                OPERATION,
                RUNNER,
                CLAIMED_AT,
            )
            durable_after = caller_state(durable_cursor)
            connection.rollback()
            connection.set_autocommit(True)
            print(json.dumps({
                "failed": failed,
                "failed_same_connection": failed_identity == before_failed,
                "read_proof": type(read_proof).__name__,
                "read_snapshot_unchanged": read_still == pinned,
                "read_transaction_open": read_open,
                "read_same_connection": read_same,
                "committed_lease": committed_lease,
                "false_proof": type(false_proof).__name__,
                "false_before": false_before,
                "false_after": false_after,
                "false_same_connection": false_after["identity"] == write_identity,
                "false_durable": false_durable,
                "durable_proof": type(durable_proof).__name__,
                "durable_before": durable_before,
                "durable_after": durable_after,
                "durable_same_connection": (
                    durable_after["identity"] == durable_identity
                ),
                "provider_calls": PROVIDER_CALLS,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["failed"]["type"], "ClaimNotConfirmed")
        self.assertEqual(payload["failed"]["provider_calls"], 0)
        self.assertTrue(payload["failed_same_connection"])
        self.assertEqual(payload["read_proof"], "ClaimNotConfirmed")
        self.assertTrue(payload["read_snapshot_unchanged"])
        self.assertTrue(payload["read_transaction_open"])
        self.assertTrue(payload["read_same_connection"])
        self.assertEqual(payload["committed_lease"], 720_123_456 + 9)
        self.assertEqual(payload["false_proof"], "ClaimNotConfirmed")
        self.assertEqual(payload["false_before"]["status"], "RUNNING")
        self.assertEqual(payload["false_after"], payload["false_before"])
        self.assertFalse(payload["false_after"]["autocommit"])
        self.assertTrue(payload["false_after"]["in_transaction"])
        self.assertTrue(payload["false_same_connection"])
        self.assertEqual(payload["false_durable"], "QUEUED")
        self.assertEqual(payload["durable_proof"], "DurableClaimConfirmed")
        self.assertEqual(payload["durable_after"], payload["durable_before"])
        self.assertNotEqual(payload["durable_before"]["lease"], 720_123_456)
        self.assertFalse(payload["durable_after"]["autocommit"])
        self.assertTrue(payload["durable_after"]["in_transaction"])
        self.assertTrue(payload["durable_same_connection"])
        self.assertEqual(payload["provider_calls"], 0)


if __name__ == "__main__":
    unittest.main()
