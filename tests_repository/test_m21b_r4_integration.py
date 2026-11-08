"""Disposable cross-method verification of the accepted repository lifecycle."""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"

_PREAMBLE = r"""
import base64
import json
import os
import sqlite3
import threading
import time
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
from django.db.models.query import QuerySet
from max_smart_db.config import (
    EXECUTION_DEADLINE_US,
    FIXED_LEASE_US,
    IDEMPOTENCY_EXPIRATION_US,
    QUEUED_RESERVATION_US,
    RECOVERY_CLOCK_SKEW_MARGIN_US,
)
from max_smart_db.creation import SqliteConversationRepository
from max_smart_db.models import (
    Conversation,
    GlobalModelSlot,
    IdempotencyKey,
    Message,
    Operation,
)
from max_smart_db.repository import (
    DefiniteProviderCategory,
    PreProviderFailureCategory,
    PrepareCommand,
    UncertainProviderCategory,
)
from max_smart_db.sql_policy import scan_migration_package
from max_smart_db.startup import StartupIntegrityError, verify_ordinary_startup

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

OWNER = User.objects.create(username="owner")
OTHER = User.objects.create(username="other")
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
FORMER = "BBBBBBBBBBBBBBBBBBBBBQ"


class Clock:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def now_us(self):
        self.calls += 1
        return self.value


def key_for(value):
    return base64.urlsafe_b64encode(bytes([value]) * 16).rstrip(b"=").decode()


def repository(clock, runner=RUNNER):
    return SqliteConversationRepository(clock, runner)


def command(actor, conversation_id, content, key, expected=0):
    return PrepareCommand(
        actor_id=actor,
        conversation_id=conversation_id,
        expected_version=expected,
        request_content=content,
        idempotency_key=key,
        provider_id="openai",
        model_id="gpt-5.6-sol",
    )


def open_conversation(repo):
    created = repo.create_conversation(OWNER.id, "max-smart", None)
    return created.conversation_id


def slot():
    row = GlobalModelSlot.objects.get(pk=1)
    return {
        "operation": None if row.operation_id is None else str(row.operation_id),
        "runner": row.runner_id,
        "reservation": row.reservation_expires_at_us,
        "lease": row.lease_expires_at_us,
    }


def operation_state(operation_id):
    row = Operation.objects.get(pk=operation_id)
    conversation = Conversation.objects.get(pk=row.conversation_id)
    messages = list(
        Message.objects.filter(operation_id=operation_id)
        .order_by("sequence")
        .values_list("sequence", "role", "content")
    )
    return {
        "status": row.status,
        "blocks": row.blocks_conversation,
        "runner": row.runner_id,
        "started": row.provider_started_at_us,
        "deadline": row.execution_deadline_us,
        "lease": row.lease_expires_at_us,
        "finished": row.finished_at_us,
        "category": row.failure_category,
        "acknowledged": row.acknowledged_at_us,
        "version": conversation.version,
        "next_sequence": conversation.next_sequence,
        "messages": messages,
    }


def _uuid_text(value):
    if value is None:
        return None
    text = value.hex() if isinstance(value, bytes) else str(value)
    text = text.replace("-", "")
    return (
        f"{text[0:8]}-{text[8:12]}-{text[12:16]}-"
        f"{text[16:20]}-{text[20:32]}"
    )


def fresh_snapshot(operation_id):
    connection.close()
    fresh = sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True)
    try:
        operation = fresh.execute(
            "SELECT status, blocks_conversation, runner_id, "
            "provider_started_at_us, execution_deadline_us, "
            "lease_expires_at_us, finished_at_us, failure_category, "
            "acknowledged_at_us, conversation_id "
            "FROM operation WHERE id = ?",
            (operation_id.hex,),
        ).fetchone()
        conversation = fresh.execute(
            "SELECT version, next_sequence FROM conversation WHERE id = ?",
            (operation[9],),
        ).fetchone()
        messages = fresh.execute(
            "SELECT sequence, role, content FROM message "
            "WHERE operation_id = ? ORDER BY sequence",
            (operation_id.hex,),
        ).fetchall()
        slot_row = fresh.execute(
            "SELECT operation_id, runner_id, reservation_expires_at_us, "
            "lease_expires_at_us FROM global_model_slot WHERE id = 1"
        ).fetchone()
        key_count = fresh.execute(
            "SELECT COUNT(*) FROM idempotency_key WHERE operation_id = ?",
            (operation_id.hex,),
        ).fetchone()[0]
        operation_count = fresh.execute(
            "SELECT COUNT(*) FROM operation"
        ).fetchone()[0]
    finally:
        fresh.close()
    return {
        "status": operation[0],
        "blocks": operation[1],
        "runner": operation[2],
        "started": operation[3],
        "deadline": operation[4],
        "lease": operation[5],
        "finished": operation[6],
        "category": operation[7],
        "acknowledged": operation[8],
        "version": conversation[0],
        "next_sequence": conversation[1],
        "messages": [list(row) for row in messages],
        "slot_operation": _uuid_text(slot_row[0]),
        "slot_runner": slot_row[1],
        "slot_reservation": slot_row[2],
        "slot_lease": slot_row[3],
        "key_count": key_count,
        "operation_count": operation_count,
    }


def post_commit_failure():
    real_commit = connection.commit
    state = {"committed": False}

    def commit_then_fail(*args, **kwargs):
        real_commit(*args, **kwargs)
        state["committed"] = True
        raise OperationalError("simulated post-commit transport failure")

    connection.commit = commit_then_fail
    return real_commit, state


def ordered_pair(first, second):
    from django.db import close_old_connections

    done = threading.Event()
    bucket = {"first": None, "second": None, "errors": []}

    def run_first():
        close_old_connections()
        try:
            bucket["first"] = first()
        except Exception as error:
            bucket["errors"].append("first:" + type(error).__name__)
        finally:
            done.set()
            close_old_connections()

    def run_second():
        close_old_connections()
        if not done.wait(30):
            bucket["errors"].append("second:timeout")
            return
        try:
            bucket["second"] = second()
        except Exception as error:
            bucket["errors"].append("second:" + type(error).__name__)
        finally:
            close_old_connections()

    threads = [
        threading.Thread(target=run_first),
        threading.Thread(target=run_second),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return bucket


def overlapping_writers(winner_call, loser_call):
    from django.db import close_old_connections

    winner_holds = threading.Event()
    loser_entered = threading.Event()
    roles = {}
    evidence = {}
    results = {"winner": None, "loser": None, "errors": []}

    def gate(execute, sql, params, many, context):
        role = roles.get(threading.get_ident())
        text = " ".join(sql.split()).upper() if isinstance(sql, str) else ""
        if text.startswith("BEGIN") and role == "winner":
            result = execute(sql, params, many, context)
            evidence["winner_begin_sql"] = text
            evidence["winner_begin_returned"] = time.monotonic_ns()
            winner_holds.set()
            return result
        if text.startswith("BEGIN") and role == "loser":
            evidence["loser_begin_sql"] = text
            evidence["loser_begin_entered"] = time.monotonic_ns()
            loser_entered.set()
            result = execute(sql, params, many, context)
            evidence["loser_begin_returned"] = time.monotonic_ns()
            return result
        if (
            role == "winner"
            and winner_holds.is_set()
            and not evidence.get("winner_waited")
        ):
            evidence["winner_waited"] = True
            if not loser_entered.wait(30):
                raise TimeoutError("loser did not enter BEGIN")
        return execute(sql, params, many, context)

    def run_winner():
        close_old_connections()
        roles[threading.get_ident()] = "winner"
        try:
            connection.ensure_connection()
            real_commit = connection.commit

            def commit_and_record(*args, **kwargs):
                evidence["winner_commit_entered"] = time.monotonic_ns()
                try:
                    return real_commit(*args, **kwargs)
                finally:
                    evidence["winner_commit_returned"] = time.monotonic_ns()

            connection.commit = commit_and_record
            with connection.execute_wrapper(gate):
                results["winner"] = winner_call()
        except Exception as error:
            results["errors"].append(
                "winner:" + type(error).__name__ + ": " + str(error)
            )
            winner_holds.set()
        finally:
            close_old_connections()

    def run_loser():
        close_old_connections()
        roles[threading.get_ident()] = "loser"
        if not winner_holds.wait(30):
            results["errors"].append("loser:winner-lock-timeout")
            return
        try:
            connection.ensure_connection()
            with connection.execute_wrapper(gate):
                results["loser"] = loser_call()
        except Exception as error:
            results["errors"].append(
                "loser:" + type(error).__name__ + ": " + str(error)
            )
            loser_entered.set()
        finally:
            close_old_connections()

    loser_thread = threading.Thread(target=run_loser)
    winner_thread = threading.Thread(target=run_winner)
    loser_thread.start()
    winner_thread.start()
    winner_thread.join()
    loser_thread.join()
    results["overlap"] = evidence
    return results


def startup_scans():
    connection.close()
    verification = verify_ordinary_startup(connection, scan_migration_package())
    return list(verification.scans)
"""


def _run(body, *, timeout=90):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "r4.sqlite3"
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
    return __import__("json").loads(result.stdout)


class IntegrationTests(unittest.TestCase):
    def test_success_history_duplicate_timing_and_startup(self):
        result = _run(
            """\
            clock = Clock(1_000_000)
            repo = repository(clock)
            clock.value = 1_000_000
            conversation_id = open_conversation(repo)
            clock.value = 2_000_000
            prepared = repo.prepare(command(
                OWNER.id,
                conversation_id,
                "hello request",
                key_for(1),
            ))
            clock.value = 3_000_000
            claimed = repo.claim_running(OWNER.id, prepared.reservation.operation_id)
            updates = []
            real_update = QuerySet.update
            def recording_update(self, **kwargs):
                count = real_update(self, **kwargs)
                updates.append([self.model.__name__, count])
                return count
            QuerySet.update = recording_update
            clock.value = 4_000_000
            try:
                applied = repo.apply_success(
                    claimed.claimed_operation,
                    "hello assistant",
                )
            finally:
                QuerySet.update = real_update
            duplicate = repo.apply_success(
                claimed.claimed_operation,
                "replacement assistant",
            )
            snapshot = repo.load_snapshot(OWNER.id, conversation_id)
            hidden = repo.load_snapshot(OTHER.id, conversation_id)
            stale = repo.prepare(command(
                OWNER.id,
                conversation_id,
                "later request",
                key_for(2),
                expected=0,
            ))
            inventory = scan_migration_package()
            startup = verify_ordinary_startup(connection, inventory)
            orphan_id = uuid.uuid4()
            Operation.objects.create(
                id=orphan_id,
                conversation_id=conversation_id,
                owner_id=OWNER.id,
                operation_type="message",
                provider_id="openai",
                model_id="gpt-5.6-sol",
                status="SUCCEEDED",
                blocks_conversation=0,
                expected_version=0,
                request_content="orphan",
                fingerprint_version=1,
                request_hash="ab" * 32,
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
            try:
                verify_ordinary_startup(connection, inventory)
                scan = "accepted"
            except StartupIntegrityError as error:
                scan = error.code
            try:
                Operation.objects.filter(
                    pk=prepared.reservation.operation_id,
                ).update(runner_id=RUNNER)
                shape = "accepted"
            except IntegrityError:
                shape = "rejected"
            good = operation_state(prepared.reservation.operation_id)
            print(json.dumps({
                "prepared": type(prepared).__name__,
                "reservation": prepared.reservation.reservation_expires_at,
                "claimed": type(claimed).__name__,
                "deadline": claimed.claimed_operation.execution_deadline,
                "lease": claimed.claimed_operation.lease_expires_at,
                "applied": type(applied).__name__,
                "updates": updates,
                "duplicate": type(duplicate).__name__,
                "duplicate_user": duplicate.user_message.content,
                "duplicate_assistant": duplicate.assistant_message.content,
                "version": snapshot.conversation.version,
                "next_sequence": snapshot.conversation.next_sequence,
                "messages": [
                    [message.sequence, message.role.value, message.content]
                    for message in snapshot.messages
                ],
                "hidden": type(hidden).__name__,
                "stale": type(stale).__name__,
                "stale_actual": stale.actual,
                "operations": Operation.objects.count(),
                "idempotency_expires": IdempotencyKey.objects.get(
                    operation_id=prepared.reservation.operation_id,
                ).expires_at_us,
                "boundary": startup.boundary,
                "scans": list(startup.scans),
                "forward": sum(len(v) for v in inventory.forward_sql.values()),
                "reverse": sum(len(v) for v in inventory.reverse_sql.values()),
                "seeds": len(inventory.seed_sql),
                "scan": scan,
                "shape": shape,
                "orphan_messages": Message.objects.filter(
                    operation_id=orphan_id,
                ).count(),
                "orphan_status": Operation.objects.get(pk=orphan_id).status,
                "good": good,
                "slot": slot(),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["prepared"], "Prepared")
        self.assertEqual(payload["reservation"], 2_000_000 + 60_000_000)
        self.assertEqual(payload["claimed"], "Claimed")
        self.assertEqual(payload["deadline"], 3_000_000 + 600_000_000)
        self.assertEqual(payload["lease"], 3_000_000 + 720_000_000)
        self.assertEqual(payload["lease"] - payload["deadline"], 120_000_000)
        self.assertEqual(payload["applied"], "Applied")
        self.assertEqual(
            payload["updates"],
            [
                ["Conversation", 1],
                ["Operation", 1],
                ["GlobalModelSlot", 1],
            ],
        )
        self.assertEqual(payload["duplicate"], "AlreadySucceeded")
        self.assertEqual(payload["duplicate_user"], "hello request")
        self.assertEqual(payload["duplicate_assistant"], "hello assistant")
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["next_sequence"], 3)
        self.assertEqual(
            payload["messages"],
            [
                [1, "user", "hello request"],
                [2, "assistant", "hello assistant"],
            ],
        )
        self.assertEqual(payload["hidden"], "NotFound")
        self.assertEqual(payload["stale"], "StaleVersion")
        self.assertEqual(payload["stale_actual"], 1)
        self.assertEqual(payload["operations"], 2)
        self.assertEqual(
            payload["idempotency_expires"],
            2_000_000 + 2_592_000_000_000,
        )
        self.assertEqual(payload["boundary"], "B3")
        self.assertEqual(
            payload["scans"],
            [
                "slot_correspondence",
                "operation_owner",
                "idempotency_binding",
                "message_status",
                "retry_eligibility",
            ],
        )
        self.assertEqual(payload["forward"], 40)
        self.assertEqual(payload["reverse"], 40)
        self.assertEqual(payload["seeds"], 4)
        self.assertEqual(payload["scan"], "message_status_failed")
        self.assertEqual(payload["shape"], "rejected")
        self.assertEqual(payload["orphan_messages"], 0)
        self.assertEqual(payload["orphan_status"], "SUCCEEDED")
        self.assertEqual(payload["good"]["status"], "SUCCEEDED")
        self.assertEqual(payload["good"]["messages"], [
            [1, "user", "hello request"],
            [2, "assistant", "hello assistant"],
        ])
        self.assertEqual(payload["good"]["version"], 1)
        self.assertIsNone(payload["good"]["runner"])
        self.assertIsNone(payload["slot"]["operation"])

    def test_idempotency_adoption_expiry_and_pre_provider(self):
        result = _run(
            """\
            clock = Clock(1_000)
            original = repository(clock, RUNNER)
            adopted = repository(clock, FORMER)
            conversation_id = open_conversation(original)
            first = original.prepare(command(
                OWNER.id,
                conversation_id,
                "bound request",
                key_for(3),
            ))
            repeated = original.prepare(command(
                OWNER.id,
                conversation_id,
                "bound request",
                key_for(3),
            ))
            mismatch = original.prepare(command(
                OWNER.id,
                conversation_id,
                "different request",
                key_for(3),
            ))
            first_id = first.reservation.operation_id
            expires_at = IdempotencyKey.objects.get(
                operation_id=first_id,
            ).expires_at_us
            clock.value = expires_at
            repeated_after = original.prepare(command(
                OWNER.id,
                conversation_id,
                "bound request",
                key_for(3),
            ))
            mismatch_after = original.prepare(command(
                OWNER.id,
                conversation_id,
                "third request",
                key_for(3),
            ))
            recovered = original.recover_slot()
            keys_after_expiry = list(
                IdempotencyKey.objects.order_by("id").values_list(
                    "operation_id",
                    "expires_at_us",
                )
            )
            clock.value = expires_at + 10
            boundary_id = open_conversation(original)
            boundary = original.prepare(command(
                OWNER.id,
                boundary_id,
                "boundary request",
                key_for(4),
            ))
            reservation_at = boundary.reservation.reservation_expires_at
            clock.value = reservation_at - 1
            live = original.recover_slot()
            clock.value = reservation_at
            expired = original.recover_slot()
            clock.value = reservation_at + 50
            adopted_id = open_conversation(original)
            queued = original.prepare(command(
                OWNER.id,
                adopted_id,
                "adopt me",
                key_for(5),
            ))
            adoption = adopted.adopt_queued(
                OWNER.id,
                queued.reservation.operation_id,
            )
            former_claim = original.claim_running(
                OWNER.id,
                queued.reservation.operation_id,
            )
            former_failure = original.fail_pre_provider(
                queued.reservation,
                PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
            )
            adopted_state = operation_state(queued.reservation.operation_id)
            adopted_slot = slot()
            clock.value = queued.reservation.reservation_expires_at
            released = original.recover_slot()
            clock.value = queued.reservation.reservation_expires_at + 20
            failed_id = open_conversation(original)
            queued_failure = original.prepare(command(
                OWNER.id,
                failed_id,
                "fail before provider",
                key_for(6),
            ))
            failed = original.fail_pre_provider(
                queued_failure.reservation,
                PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
            )
            failure_state = operation_state(
                queued_failure.reservation.operation_id,
            )
            print(json.dumps({
                "repeated": type(repeated).__name__,
                "same_operation": repeated.operation.operation_id == first_id,
                "mismatch": type(mismatch).__name__,
                "repeated_after": type(repeated_after).__name__,
                "same_after": repeated_after.operation.operation_id == first_id,
                "mismatch_after": type(mismatch_after).__name__,
                "recovered": type(recovered).__name__,
                "keys": [
                    [str(operation_id), expires]
                    for operation_id, expires in keys_after_expiry
                ],
                "expires_at": expires_at,
                "live": type(live).__name__,
                "expired": type(expired).__name__,
                "expired_state": operation_state(boundary.reservation.operation_id),
                "expired_slot": slot(),
                "adoption": type(adoption).__name__,
                "adopted_runner": adoption.reservation.runner_id,
                "former_claim": type(former_claim).__name__,
                "former_failure": type(former_failure).__name__,
                "adopted_state": adopted_state,
                "adopted_slot": adopted_slot,
                "released": type(released).__name__,
                "failed": type(failed).__name__,
                "failure_state": failure_state,
                "failure_slot": slot(),
                "reservation_delta": reservation_at - (expires_at + 10),
                "key_count": IdempotencyKey.objects.count(),
                "cleanup_method": hasattr(
                    SqliteConversationRepository,
                    "delete_expired_idempotency",
                ),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["repeated"], "Existing")
        self.assertTrue(payload["same_operation"])
        self.assertEqual(payload["mismatch"], "KeyBindingMismatch")
        self.assertEqual(payload["repeated_after"], "Existing")
        self.assertTrue(payload["same_after"])
        self.assertEqual(payload["mismatch_after"], "KeyBindingMismatch")
        self.assertEqual(payload["recovered"], "CancelledExpiredQueued")
        self.assertEqual(len(payload["keys"]), 1)
        self.assertEqual(payload["keys"][0][1], payload["expires_at"])
        self.assertEqual(payload["live"], "LiveQueued")
        self.assertEqual(payload["expired"], "CancelledExpiredQueued")
        self.assertEqual(payload["expired_state"]["status"], "CANCELLED")
        self.assertEqual(payload["expired_state"]["blocks"], 0)
        self.assertIsNone(payload["expired_slot"]["operation"])
        self.assertEqual(payload["reservation_delta"], 60_000_000)
        self.assertEqual(payload["adoption"], "Adopted")
        self.assertEqual(payload["adopted_runner"], "BBBBBBBBBBBBBBBBBBBBBQ")
        self.assertEqual(payload["former_claim"], "OwnershipLost")
        self.assertEqual(payload["former_failure"], "OwnershipLost")
        self.assertEqual(payload["adopted_state"]["status"], "QUEUED")
        self.assertEqual(
            payload["adopted_state"]["runner"],
            "BBBBBBBBBBBBBBBBBBBBBQ",
        )
        self.assertEqual(
            payload["adopted_slot"]["runner"],
            "BBBBBBBBBBBBBBBBBBBBBQ",
        )
        self.assertEqual(payload["released"], "CancelledExpiredQueued")
        self.assertEqual(payload["failed"], "Failed")
        self.assertEqual(payload["failure_state"]["status"], "FAILED")
        self.assertEqual(payload["failure_state"]["blocks"], 0)
        self.assertIsNone(payload["failure_state"]["runner"])
        self.assertIsNone(payload["failure_state"]["started"])
        self.assertEqual(
            payload["failure_state"]["category"],
            "context_unavailable",
        )
        self.assertEqual(payload["failure_state"]["version"], 0)
        self.assertIsNone(payload["failure_slot"]["operation"])
        self.assertEqual(payload["key_count"], 4)
        self.assertFalse(payload["cleanup_method"])

    def test_provider_recovery_late_success_and_acknowledgement(self):
        result = _run(
            """\
            clock = Clock(10_000)
            repo = repository(clock)
            definite_conversation = open_conversation(repo)
            definite_prepared = repo.prepare(command(
                OWNER.id,
                definite_conversation,
                "definite request",
                key_for(7),
            ))
            definite_claimed = repo.claim_running(
                OWNER.id,
                definite_prepared.reservation.operation_id,
            )
            clock.value = definite_claimed.claimed_operation.claimed_at + 5
            definite = repo.apply_failure(
                definite_claimed.claimed_operation,
                DefiniteProviderCategory.PROVIDER_DEFINITE_FAILURE,
            )
            definite_state = operation_state(
                definite_claimed.claimed_operation.operation_id,
            )
            definite_slot = slot()
            clock.value = 20_000
            unknown_conversation = open_conversation(repo)
            unknown_prepared = repo.prepare(command(
                OWNER.id,
                unknown_conversation,
                "unknown request",
                key_for(8),
            ))
            unknown_claimed = repo.claim_running(
                OWNER.id,
                unknown_prepared.reservation.operation_id,
            )
            token = unknown_claimed.claimed_operation
            fingerprint = repo.apply_success(
                type(token)(
                    operation_id=token.operation_id,
                    actor_id=token.actor_id,
                    conversation_id=token.conversation_id,
                    request_hash="cd" * 32,
                    fingerprint_version=token.fingerprint_version,
                    expected_version=token.expected_version,
                    runner_id=token.runner_id,
                    claimed_at=token.claimed_at,
                    execution_deadline=token.execution_deadline,
                    lease_expires_at=token.lease_expires_at,
                ),
                "should not persist",
            )
            fingerprint_messages = Message.objects.filter(
                operation_id=token.operation_id,
            ).count()
            uncertain = repo.apply_failure(
                token,
                UncertainProviderCategory.PROVIDER_OUTCOME_UNCERTAIN,
            )
            clock.value = token.execution_deadline
            before_lease = repo.recover_slot()
            clock.value = token.lease_expires_at
            recovered = repo.recover_slot()
            recovered_state = operation_state(token.operation_id)
            recovered_slot = slot()
            clock.value = 30_000
            acknowledged_conversation = open_conversation(repo)
            acknowledged_prepared = repo.prepare(command(
                OWNER.id,
                acknowledged_conversation,
                "acknowledge request",
                key_for(9),
            ))
            acknowledged_claimed = repo.claim_running(
                OWNER.id,
                acknowledged_prepared.reservation.operation_id,
            )
            acknowledged_token = acknowledged_claimed.claimed_operation
            repo.apply_failure(
                acknowledged_token,
                UncertainProviderCategory.PROVIDER_OUTCOME_UNCERTAIN,
            )
            clock.value = acknowledged_token.lease_expires_at
            repo.recover_slot()
            acknowledged = repo.acknowledge_unknown(
                OWNER.id,
                acknowledged_token.operation_id,
            )
            from max_smart_db.repository import RetainedResult
            discarded = repo.reconcile_late_success(RetainedResult(
                claimed_operation=acknowledged_token,
                output_text="too late",
            ))
            acknowledged_state = operation_state(
                acknowledged_token.operation_id,
            )
            clock.value = 40_000
            occupant_conversation = open_conversation(repo)
            occupant_prepared = repo.prepare(command(
                OWNER.id,
                occupant_conversation,
                "occupant request",
                key_for(10),
            ))
            occupant_claimed = repo.claim_running(
                OWNER.id,
                occupant_prepared.reservation.operation_id,
            )
            occupant_before = slot()
            reconciled = repo.reconcile_late_success(RetainedResult(
                claimed_operation=token,
                output_text="retained assistant",
            ))
            print(json.dumps({
                "definite": type(definite).__name__,
                "definite_state": definite_state,
                "definite_slot": definite_slot,
                "definite_claimed_at": definite_claimed.claimed_operation.claimed_at,
                "fingerprint": type(fingerprint).__name__,
                "fingerprint_messages": fingerprint_messages,
                "uncertain": type(uncertain).__name__,
                "recovery_at": uncertain.recovery_at,
                "lease": token.lease_expires_at,
                "deadline": token.execution_deadline,
                "before_lease": type(before_lease).__name__,
                "recovered": type(recovered).__name__,
                "recovered_state": recovered_state,
                "recovered_slot": recovered_slot,
                "acknowledged": type(acknowledged).__name__,
                "discarded": type(discarded).__name__,
                "acknowledged_state": acknowledged_state,
                "occupant_before": occupant_before,
                "reconciled": type(reconciled).__name__,
                "late_state": operation_state(token.operation_id),
                "occupant_after": slot(),
                "occupant_status": Operation.objects.get(
                    pk=occupant_claimed.claimed_operation.operation_id,
                ).status,
            }))
            """
        )
        payload = _payload(self, result)
        definite = payload["definite_state"]
        self.assertEqual(payload["definite"], "DefinitelyFailed")
        self.assertEqual(definite["status"], "FAILED")
        self.assertEqual(definite["blocks"], 0)
        self.assertIsNone(definite["runner"])
        claimed_at = payload["definite_claimed_at"]
        self.assertEqual(definite["started"], claimed_at)
        self.assertEqual(definite["deadline"], claimed_at + 600_000_000)
        self.assertEqual(definite["lease"], claimed_at + 720_000_000)
        self.assertIsNone(payload["definite_slot"]["operation"])
        self.assertEqual(definite["category"], "provider_definite_failure")
        self.assertEqual(definite["version"], 0)
        self.assertEqual(definite["messages"], [])
        self.assertEqual(payload["fingerprint"], "OwnershipLost")
        self.assertEqual(payload["fingerprint_messages"], 0)
        self.assertEqual(payload["uncertain"], "UncertainRecorded")
        self.assertEqual(payload["recovery_at"], payload["lease"])
        self.assertEqual(payload["deadline"] < payload["lease"], True)
        self.assertEqual(payload["before_lease"], "LiveRunning")
        self.assertEqual(payload["recovered"], "MarkedOutcomeUnknown")
        self.assertIsNone(payload["recovered_slot"]["operation"])
        self.assertEqual(payload["recovered_state"]["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(payload["recovered_state"]["blocks"], 1)
        self.assertEqual(
            payload["recovered_state"]["category"],
            "provider_outcome_uncertain",
        )
        self.assertEqual(
            payload["recovered_state"]["finished"],
            payload["lease"],
        )
        self.assertEqual(payload["acknowledged"], "Acknowledged")
        self.assertEqual(payload["discarded"], "AcknowledgedDiscard")
        acknowledged = payload["acknowledged_state"]
        self.assertEqual(acknowledged["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(acknowledged["blocks"], 0)
        self.assertEqual(acknowledged["category"], "provider_outcome_uncertain")
        self.assertEqual(acknowledged["messages"], [])
        self.assertEqual(acknowledged["version"], 0)
        self.assertIsNotNone(acknowledged["acknowledged"])
        self.assertEqual(payload["reconciled"], "Reconciled")
        late = payload["late_state"]
        self.assertEqual(late["status"], "SUCCEEDED")
        self.assertEqual(late["version"], 1)
        self.assertEqual(late["next_sequence"], 3)
        self.assertEqual(
            late["messages"],
            [
                [1, "user", "unknown request"],
                [2, "assistant", "retained assistant"],
            ],
        )
        self.assertEqual(late["category"], "provider_outcome_uncertain")
        self.assertEqual(payload["occupant_after"], payload["occupant_before"])
        self.assertEqual(payload["occupant_status"], "RUNNING")

    def test_fences_rollback_contention_and_ambiguous_commit(self):
        result = _run(
            """\
            from django.db import close_old_connections

            clock = Clock(50_000)
            repo = repository(clock)
            conversation_id = open_conversation(repo)
            holder = sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True)
            try:
                holder.execute("BEGIN IMMEDIATE")
                busy = repo.prepare(command(
                    OWNER.id,
                    conversation_id,
                    "busy request",
                    key_for(11),
                ))
                busy_calls = clock.calls
            finally:
                holder.rollback()
                holder.close()
            prepared = repo.prepare(command(
                OWNER.id,
                conversation_id,
                "rollback request",
                key_for(12),
            ))
            claimed = repo.claim_running(
                OWNER.id,
                prepared.reservation.operation_id,
            )
            before_apply = operation_state(prepared.reservation.operation_id)
            before_slot = slot()
            real_create = Message.objects.create
            def fail_create(*args, **kwargs):
                raise RuntimeError("message boundary")
            Message.objects.create = fail_create
            try:
                repo.apply_success(claimed.claimed_operation, "partial")
                partial = "accepted"
            except RuntimeError as error:
                partial = str(error)
            finally:
                Message.objects.create = real_create
            after_partial = operation_state(prepared.reservation.operation_id)
            partial_slot = slot()
            real_commit = connection.commit
            def fail_commit(*args, **kwargs):
                raise OperationalError("injected commit uncertainty")
            connection.commit = fail_commit
            try:
                ambiguous = repo.apply_success(
                    claimed.claimed_operation,
                    "ambiguous",
                )
            finally:
                connection.commit = real_commit
            connection.close()
            after_ambiguous = operation_state(prepared.reservation.operation_id)
            resolved = repo.apply_success(
                claimed.claimed_operation,
                "resolved assistant",
            )
            resolved_state = operation_state(prepared.reservation.operation_id)
            wrong = repo.prepare(command(
                OTHER.id,
                conversation_id,
                "foreign request",
                key_for(13),
            ))
            queued_conversation = open_conversation(repo)
            queued = repo.prepare(command(
                OWNER.id,
                queued_conversation,
                "claim rollback",
                key_for(14),
            ))
            real_update = QuerySet.update
            def fail_slot(self, **kwargs):
                if self.model is GlobalModelSlot:
                    raise RuntimeError("slot boundary")
                return real_update(self, **kwargs)
            QuerySet.update = fail_slot
            try:
                repo.claim_running(OWNER.id, queued.reservation.operation_id)
                claim_failure = "accepted"
            except RuntimeError as error:
                claim_failure = str(error)
            finally:
                QuerySet.update = real_update
            claim_state = operation_state(queued.reservation.operation_id)
            released = repo.fail_pre_provider(
                queued.reservation,
                PreProviderFailureCategory.MODEL_CONFIGURATION_INVALID,
            )
            left = open_conversation(repo)
            right = open_conversation(repo)
            barrier = threading.Barrier(2)
            outcomes = []
            failures = []
            def worker(conversation, key_byte):
                close_old_connections()
                worker_clock = Clock(80_000)
                worker_repo = repository(worker_clock)
                barrier.wait()
                try:
                    outcome = worker_repo.prepare(command(
                        OWNER.id,
                        conversation,
                        "competitor",
                        key_for(key_byte),
                    ))
                    outcomes.append(type(outcome).__name__)
                except Exception as error:
                    failures.append(type(error).__name__)
                finally:
                    close_old_connections()
            threads = [
                threading.Thread(target=worker, args=(left, 15)),
                threading.Thread(target=worker, args=(right, 16)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            connection.close()
            print(json.dumps({
                "busy": type(busy).__name__,
                "busy_calls": busy_calls,
                "before_apply": before_apply["status"],
                "before_slot": before_slot["operation"],
                "partial": partial,
                "after_partial": after_partial,
                "partial_slot": partial_slot["operation"],
                "ambiguous": type(ambiguous).__name__,
                "ambiguous_operation": str(ambiguous.operation_id),
                "after_ambiguous": after_ambiguous["status"],
                "ambiguous_messages": after_ambiguous["messages"],
                "resolved": type(resolved).__name__,
                "resolved_version": resolved_state["version"],
                "resolved_messages": resolved_state["messages"],
                "wrong": type(wrong).__name__,
                "claim_failure": claim_failure,
                "claim_state": claim_state["status"],
                "claim_slot": slot()["operation"] if False else str(
                    GlobalModelSlot.objects.get(pk=1).operation_id
                ) if GlobalModelSlot.objects.get(pk=1).operation_id else None,
                "released": type(released).__name__,
                "outcomes": sorted(outcomes),
                "failures": failures,
                "live_operations": Operation.objects.filter(
                    status__in=["QUEUED", "RUNNING"],
                ).count(),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["busy"], "DatabaseBusy")
        self.assertEqual(payload["busy_calls"], 1)
        self.assertEqual(payload["before_apply"], "RUNNING")
        self.assertEqual(payload["partial"], "message boundary")
        self.assertEqual(payload["after_partial"]["status"], "RUNNING")
        self.assertEqual(payload["after_partial"]["messages"], [])
        self.assertEqual(payload["after_partial"]["version"], 0)
        self.assertEqual(
            payload["partial_slot"],
            payload["before_slot"],
        )
        self.assertEqual(payload["ambiguous"], "CommitAmbiguous")
        self.assertEqual(payload["after_ambiguous"], "RUNNING")
        self.assertEqual(payload["ambiguous_messages"], [])
        self.assertEqual(payload["resolved"], "Applied")
        self.assertEqual(payload["resolved_version"], 1)
        self.assertEqual(
            payload["resolved_messages"],
            [
                [1, "user", "rollback request"],
                [2, "assistant", "resolved assistant"],
            ],
        )
        self.assertEqual(payload["wrong"], "NotFound")
        self.assertEqual(payload["claim_failure"], "slot boundary")
        self.assertEqual(payload["claim_state"], "QUEUED")
        self.assertEqual(payload["released"], "Failed")
        self.assertEqual(payload["outcomes"], ["ModelBusy", "Prepared"])
        self.assertEqual(payload["failures"], [])
        self.assertEqual(payload["live_operations"], 1)

    def test_commit_succeeds_before_reported_ambiguity(self):
        result = _run(
            """\
            from max_smart_db.repository import ClaimedOperation

            clock = Clock(100_000)
            repo = repository(clock)
            conversation_id = open_conversation(repo)
            real_commit, state = post_commit_failure()
            try:
                prepared = repo.prepare(command(
                    OWNER.id,
                    conversation_id,
                    "prepare despite reported failure",
                    key_for(21),
                ))
            finally:
                connection.commit = real_commit
            prepare_committed = state["committed"]
            prepare_fresh = fresh_snapshot(prepared.operation_id)
            prepare_scans = startup_scans()
            repeated = repo.prepare(command(
                OWNER.id,
                conversation_id,
                "prepare despite reported failure",
                key_for(21),
            ))
            after_repeat = fresh_snapshot(prepared.operation_id)
            real_commit, state = post_commit_failure()
            try:
                claimed = repo.claim_running(OWNER.id, prepared.operation_id)
            finally:
                connection.commit = real_commit
            claim_committed = state["committed"]
            claim_fresh = fresh_snapshot(prepared.operation_id)
            claim_scans = startup_scans()
            provider_calls = 0 if type(claimed).__name__ == "CommitAmbiguous" else 1
            repeated_claim = repo.claim_running(OWNER.id, prepared.operation_id)
            real_commit, state = post_commit_failure()
            try:
                applied = repo.apply_success(
                    ClaimedOperation(
                        operation_id=prepared.operation_id,
                        actor_id=OWNER.id,
                        conversation_id=conversation_id,
                        request_hash=repeated.operation.request_hash,
                        fingerprint_version=repeated.operation.fingerprint_version,
                        expected_version=repeated.operation.expected_version,
                        runner_id=RUNNER,
                        claimed_at=claim_fresh["started"],
                        execution_deadline=claim_fresh["deadline"],
                        lease_expires_at=claim_fresh["lease"],
                    ),
                    "durable assistant",
                )
            finally:
                connection.commit = real_commit
            apply_committed = state["committed"]
            apply_fresh = fresh_snapshot(prepared.operation_id)
            apply_scans = startup_scans()
            duplicate = repo.apply_success(
                ClaimedOperation(
                    operation_id=prepared.operation_id,
                    actor_id=OWNER.id,
                    conversation_id=conversation_id,
                    request_hash=repeated.operation.request_hash,
                    fingerprint_version=repeated.operation.fingerprint_version,
                    expected_version=repeated.operation.expected_version,
                    runner_id=RUNNER,
                    claimed_at=claim_fresh["started"],
                    execution_deadline=claim_fresh["deadline"],
                    lease_expires_at=claim_fresh["lease"],
                ),
                "must not replace",
            )
            duplicate_fresh = fresh_snapshot(prepared.operation_id)
            print(json.dumps({
                "prepare_type": type(prepared).__name__,
                "prepare_committed": prepare_committed,
                "prepare_operation": str(prepared.operation_id),
                "prepare_runner": prepared.runner_id,
                "prepare_fresh": prepare_fresh,
                "prepare_scans": prepare_scans,
                "repeated": type(repeated).__name__,
                "repeated_operation": str(repeated.operation.operation_id),
                "after_repeat": after_repeat,
                "claim_type": type(claimed).__name__,
                "claim_committed": claim_committed,
                "claim_operation": str(claimed.operation_id),
                "claim_runner": claimed.runner_id,
                "claim_at": claimed.claimed_at,
                "provider_calls": provider_calls,
                "claim_fresh": claim_fresh,
                "claim_scans": claim_scans,
                "repeated_claim": type(repeated_claim).__name__,
                "apply_type": type(applied).__name__,
                "apply_committed": apply_committed,
                "apply_operation": str(applied.operation_id),
                "apply_fresh": apply_fresh,
                "apply_scans": apply_scans,
                "duplicate": type(duplicate).__name__,
                "duplicate_assistant": duplicate.assistant_message.content,
                "duplicate_fresh": duplicate_fresh,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["prepare_type"], "CommitAmbiguous")
        self.assertTrue(payload["prepare_committed"])
        self.assertEqual(payload["prepare_runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        prepared = payload["prepare_fresh"]
        self.assertEqual(prepared["status"], "QUEUED")
        self.assertEqual(prepared["blocks"], 1)
        self.assertEqual(prepared["runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(prepared["slot_operation"], payload["prepare_operation"])
        self.assertEqual(prepared["slot_runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertIsNone(prepared["started"])
        self.assertEqual(prepared["slot_reservation"], 100_000 + 60_000_000)
        self.assertIsNone(prepared["slot_lease"])
        self.assertEqual(prepared["key_count"], 1)
        self.assertEqual(prepared["operation_count"], 1)
        self.assertEqual(prepared["messages"], [])
        self.assertEqual(payload["prepare_scans"][-5:], [
            "slot_correspondence",
            "operation_owner",
            "idempotency_binding",
            "message_status",
            "retry_eligibility",
        ])
        self.assertEqual(payload["repeated"], "Existing")
        self.assertEqual(payload["repeated_operation"], payload["prepare_operation"])
        self.assertEqual(payload["after_repeat"]["operation_count"], 1)
        self.assertEqual(payload["after_repeat"]["key_count"], 1)
        self.assertEqual(payload["claim_type"], "CommitAmbiguous")
        self.assertTrue(payload["claim_committed"])
        self.assertEqual(payload["claim_operation"], payload["prepare_operation"])
        self.assertEqual(payload["claim_runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(payload["provider_calls"], 0)
        claimed = payload["claim_fresh"]
        self.assertEqual(claimed["status"], "RUNNING")
        self.assertEqual(claimed["blocks"], 1)
        self.assertEqual(claimed["runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(claimed["started"], payload["claim_at"])
        self.assertEqual(claimed["deadline"], payload["claim_at"] + 600_000_000)
        self.assertEqual(claimed["lease"], payload["claim_at"] + 720_000_000)
        self.assertEqual(claimed["slot_operation"], payload["prepare_operation"])
        self.assertEqual(claimed["slot_runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertIsNone(claimed["slot_reservation"])
        self.assertEqual(claimed["slot_lease"], claimed["lease"])
        self.assertEqual(claimed["messages"], [])
        self.assertEqual(len(payload["claim_scans"]), 5)
        self.assertEqual(payload["repeated_claim"], "NotQueued")
        self.assertEqual(payload["apply_type"], "CommitAmbiguous")
        self.assertTrue(payload["apply_committed"])
        self.assertEqual(payload["apply_operation"], payload["prepare_operation"])
        applied = payload["apply_fresh"]
        self.assertEqual(applied["status"], "SUCCEEDED")
        self.assertEqual(applied["blocks"], 0)
        self.assertIsNone(applied["runner"])
        self.assertEqual(applied["version"], 1)
        self.assertEqual(applied["next_sequence"], 3)
        self.assertEqual(
            applied["messages"],
            [
                [1, "user", "prepare despite reported failure"],
                [2, "assistant", "durable assistant"],
            ],
        )
        self.assertIsNone(applied["slot_operation"])
        self.assertEqual(applied["key_count"], 1)
        self.assertEqual(applied["operation_count"], 1)
        self.assertEqual(len(payload["apply_scans"]), 5)
        self.assertEqual(payload["duplicate"], "AlreadySucceeded")
        self.assertEqual(payload["duplicate_assistant"], "durable assistant")
        self.assertEqual(payload["duplicate_fresh"]["version"], 1)
        self.assertEqual(payload["duplicate_fresh"]["next_sequence"], 3)
        self.assertEqual(payload["duplicate_fresh"]["messages"], applied["messages"])
        self.assertEqual(payload["duplicate_fresh"]["operation_count"], 1)

    def test_competing_writers_serialize_in_both_orders(self):
        result = _run(
            """\
            from max_smart_db.repository import RetainedResult

            clock = Clock(200_000)
            repo = repository(clock)

            def make_running(content, key_byte):
                conversation_id = open_conversation(repo)
                prepared = repo.prepare(command(
                    OWNER.id,
                    conversation_id,
                    content,
                    key_for(key_byte),
                ))
                claimed = repo.claim_running(
                    OWNER.id,
                    prepared.reservation.operation_id,
                )
                return claimed.claimed_operation

            def make_unknown(content, key_byte):
                token = make_running(content, key_byte)
                repo.apply_failure(
                    token,
                    UncertainProviderCategory.PROVIDER_OUTCOME_UNCERTAIN,
                )
                clock.value = token.lease_expires_at
                recovered = repo.recover_slot()
                return token, type(recovered).__name__

            apply_token = make_running("apply request", 31)
            connection.close()

            def apply_outcome(token):
                outcome = repository(Clock(token.lease_expires_at)).apply_success(
                    token,
                    "race assistant",
                )
                return type(outcome).__name__

            def recover_outcome(token):
                outcome = repository(Clock(token.lease_expires_at)).recover_slot()
                return type(outcome).__name__

            apply_first = ordered_pair(
                lambda: apply_outcome(apply_token),
                lambda: recover_outcome(apply_token),
            )
            apply_first_fresh = fresh_snapshot(apply_token.operation_id)
            apply_first_scans = startup_scans()
            recover_token = make_running("recover request", 32)
            connection.close()
            recover_first = ordered_pair(
                lambda: recover_outcome(recover_token),
                lambda: apply_outcome(recover_token),
            )
            recover_first_fresh = fresh_snapshot(recover_token.operation_id)
            recover_first_scans = startup_scans()

            clock.value = 300_000
            ack_token, ack_recovered = make_unknown("ack request", 33)
            late_token, late_recovered = make_unknown("late request", 34)
            occupant_conversation = open_conversation(repo)
            occupant = repo.prepare(command(
                OWNER.id,
                occupant_conversation,
                "occupant request",
                key_for(35),
            ))
            occupant_claim = repo.claim_running(
                OWNER.id,
                occupant.reservation.operation_id,
            )
            occupant_id = str(occupant_claim.claimed_operation.operation_id)
            occupant_before = slot()
            connection.close()

            def acknowledge_outcome(operation_id):
                outcome = repository(Clock(400_000)).acknowledge_unknown(
                    OWNER.id,
                    operation_id,
                )
                return type(outcome).__name__

            def reconcile_outcome(token):
                outcome = repository(Clock(400_000)).reconcile_late_success(
                    RetainedResult(
                        claimed_operation=token,
                        output_text="retained race assistant",
                    )
                )
                return type(outcome).__name__

            ack_first = ordered_pair(
                lambda: acknowledge_outcome(ack_token.operation_id),
                lambda: reconcile_outcome(ack_token),
            )
            ack_first_fresh = fresh_snapshot(ack_token.operation_id)
            ack_first_scans = startup_scans()
            late_first = ordered_pair(
                lambda: reconcile_outcome(late_token),
                lambda: acknowledge_outcome(late_token.operation_id),
            )
            late_first_fresh = fresh_snapshot(late_token.operation_id)
            late_first_scans = startup_scans()
            occupant_after = fresh_snapshot(
                occupant_claim.claimed_operation.operation_id
            )
            print(json.dumps({
                "setup": [ack_recovered, late_recovered],
                "apply_first": apply_first,
                "apply_first_fresh": apply_first_fresh,
                "apply_first_scans": apply_first_scans,
                "recover_first": recover_first,
                "recover_first_fresh": recover_first_fresh,
                "recover_first_scans": recover_first_scans,
                "occupant_before": occupant_before["operation"],
                "occupant_id": occupant_id,
                "ack_first": ack_first,
                "ack_first_fresh": ack_first_fresh,
                "ack_first_scans": ack_first_scans,
                "late_first": late_first,
                "late_first_fresh": late_first_fresh,
                "late_first_scans": late_first_scans,
                "occupant_after": occupant_after,
            }))
            """
        )
        payload = _payload(self, result)
        scans = [
            "slot_correspondence",
            "operation_owner",
            "idempotency_binding",
            "message_status",
            "retry_eligibility",
        ]
        self.assertEqual(payload["setup"], [
            "MarkedOutcomeUnknown",
            "MarkedOutcomeUnknown",
        ])
        self.assertEqual(payload["apply_first"]["errors"], [])
        self.assertEqual(payload["apply_first"]["first"], "Applied")
        self.assertEqual(payload["apply_first"]["second"], "SlotFree")
        apply_first = payload["apply_first_fresh"]
        self.assertEqual(apply_first["status"], "SUCCEEDED")
        self.assertEqual(apply_first["version"], 1)
        self.assertEqual(apply_first["next_sequence"], 3)
        self.assertEqual(
            apply_first["messages"],
            [
                [1, "user", "apply request"],
                [2, "assistant", "race assistant"],
            ],
        )
        self.assertIsNone(apply_first["slot_operation"])
        self.assertEqual(payload["apply_first_scans"], scans)
        self.assertEqual(payload["recover_first"]["errors"], [])
        self.assertEqual(payload["recover_first"]["first"], "MarkedOutcomeUnknown")
        self.assertEqual(payload["recover_first"]["second"], "RecoveredUnknown")
        recover_first = payload["recover_first_fresh"]
        self.assertEqual(recover_first["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(recover_first["blocks"], 1)
        self.assertIsNone(recover_first["acknowledged"])
        self.assertEqual(recover_first["version"], 0)
        self.assertEqual(recover_first["next_sequence"], 1)
        self.assertEqual(recover_first["messages"], [])
        self.assertIsNone(recover_first["slot_operation"])
        self.assertEqual(payload["recover_first_scans"], scans)
        self.assertEqual(payload["ack_first"]["errors"], [])
        self.assertEqual(payload["ack_first"]["first"], "Acknowledged")
        self.assertEqual(payload["ack_first"]["second"], "AcknowledgedDiscard")
        acknowledged = payload["ack_first_fresh"]
        self.assertEqual(acknowledged["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(acknowledged["blocks"], 0)
        self.assertIsNotNone(acknowledged["acknowledged"])
        self.assertEqual(acknowledged["category"], "provider_outcome_uncertain")
        self.assertEqual(acknowledged["messages"], [])
        self.assertEqual(acknowledged["version"], 0)
        self.assertEqual(acknowledged["slot_operation"], payload["occupant_id"])
        self.assertEqual(payload["ack_first_scans"], scans)
        self.assertEqual(payload["late_first"]["errors"], [])
        self.assertEqual(payload["late_first"]["first"], "Reconciled")
        self.assertEqual(payload["late_first"]["second"], "NotUnknown")
        retained = payload["late_first_fresh"]
        self.assertEqual(retained["status"], "SUCCEEDED")
        self.assertEqual(retained["version"], 1)
        self.assertEqual(retained["next_sequence"], 3)
        self.assertEqual(
            retained["messages"],
            [
                [1, "user", "late request"],
                [2, "assistant", "retained race assistant"],
            ],
        )
        self.assertEqual(retained["category"], "provider_outcome_uncertain")
        self.assertEqual(retained["slot_operation"], payload["occupant_id"])
        self.assertEqual(payload["late_first_scans"], scans)
        self.assertEqual(payload["occupant_before"], payload["occupant_id"])
        self.assertEqual(payload["occupant_after"]["status"], "RUNNING")
        self.assertEqual(payload["occupant_after"]["slot_operation"], payload["occupant_id"])
        self.assertEqual(payload["occupant_after"]["messages"], [])

    def test_overlapping_writers_block_inside_begin_immediate(self):
        result = _run(
            """\
            from max_smart_db.repository import RetainedResult

            clock = Clock(500_000)
            repo = repository(clock)

            def make_running(content, key_byte):
                conversation_id = open_conversation(repo)
                prepared = repo.prepare(command(
                    OWNER.id,
                    conversation_id,
                    content,
                    key_for(key_byte),
                ))
                claimed = repo.claim_running(
                    OWNER.id,
                    prepared.reservation.operation_id,
                )
                return claimed.claimed_operation

            def make_unknown(content, key_byte):
                token = make_running(content, key_byte)
                repo.apply_failure(
                    token,
                    UncertainProviderCategory.PROVIDER_OUTCOME_UNCERTAIN,
                )
                clock.value = token.lease_expires_at
                recovered = repo.recover_slot()
                return token, type(recovered).__name__

            def apply_outcome(token):
                outcome = repository(Clock(token.lease_expires_at)).apply_success(
                    token,
                    "overlap assistant",
                )
                return type(outcome).__name__

            def recover_outcome(token):
                outcome = repository(Clock(token.lease_expires_at)).recover_slot()
                return type(outcome).__name__

            apply_token = make_running("overlap apply request", 41)
            connection.close()
            apply_wins = overlapping_writers(
                lambda: apply_outcome(apply_token),
                lambda: recover_outcome(apply_token),
            )
            apply_fresh = fresh_snapshot(apply_token.operation_id)
            apply_scans = startup_scans()

            recover_token = make_running("overlap recover request", 42)
            connection.close()
            recover_wins = overlapping_writers(
                lambda: recover_outcome(recover_token),
                lambda: apply_outcome(recover_token),
            )
            recover_fresh = fresh_snapshot(recover_token.operation_id)
            recover_scans = startup_scans()

            clock.value = 600_000
            ack_token, ack_recovered = make_unknown("overlap ack request", 43)
            late_token, late_recovered = make_unknown("overlap late request", 44)
            occupant_conversation = open_conversation(repo)
            occupant = repo.prepare(command(
                OWNER.id,
                occupant_conversation,
                "overlap occupant request",
                key_for(45),
            ))
            occupant_claim = repo.claim_running(
                OWNER.id,
                occupant.reservation.operation_id,
            )
            occupant_id = str(occupant_claim.claimed_operation.operation_id)
            connection.close()

            def acknowledge_outcome(operation_id):
                outcome = repository(Clock(700_000)).acknowledge_unknown(
                    OWNER.id,
                    operation_id,
                )
                return type(outcome).__name__

            def reconcile_outcome(token):
                outcome = repository(Clock(700_000)).reconcile_late_success(
                    RetainedResult(
                        claimed_operation=token,
                        output_text="overlap retained assistant",
                    )
                )
                return type(outcome).__name__

            ack_wins = overlapping_writers(
                lambda: acknowledge_outcome(ack_token.operation_id),
                lambda: reconcile_outcome(ack_token),
            )
            ack_fresh = fresh_snapshot(ack_token.operation_id)
            ack_scans = startup_scans()
            late_wins = overlapping_writers(
                lambda: reconcile_outcome(late_token),
                lambda: acknowledge_outcome(late_token.operation_id),
            )
            late_fresh = fresh_snapshot(late_token.operation_id)
            late_scans = startup_scans()
            occupant_after = fresh_snapshot(
                occupant_claim.claimed_operation.operation_id
            )
            print(json.dumps({
                "setup": [ack_recovered, late_recovered],
                "occupant_id": occupant_id,
                "apply_wins": apply_wins,
                "apply_fresh": apply_fresh,
                "apply_scans": apply_scans,
                "recover_wins": recover_wins,
                "recover_fresh": recover_fresh,
                "recover_scans": recover_scans,
                "ack_wins": ack_wins,
                "ack_fresh": ack_fresh,
                "ack_scans": ack_scans,
                "late_wins": late_wins,
                "late_fresh": late_fresh,
                "late_scans": late_scans,
                "occupant_after": occupant_after,
            }))
            """,
            timeout=120,
        )
        payload = _payload(self, result)
        scans = [
            "slot_correspondence",
            "operation_owner",
            "idempotency_binding",
            "message_status",
            "retry_eligibility",
        ]
        self.assertEqual(payload["setup"], [
            "MarkedOutcomeUnknown",
            "MarkedOutcomeUnknown",
        ])
        self._assert_writer_overlap(payload["apply_wins"], "Applied", "SlotFree")
        applied = payload["apply_fresh"]
        self.assertEqual(applied["status"], "SUCCEEDED")
        self.assertEqual(applied["blocks"], 0)
        self.assertIsNone(applied["runner"])
        self.assertEqual(applied["version"], 1)
        self.assertEqual(applied["next_sequence"], 3)
        self.assertEqual(
            applied["messages"],
            [
                [1, "user", "overlap apply request"],
                [2, "assistant", "overlap assistant"],
            ],
        )
        self.assertIsNone(applied["slot_operation"])
        self.assertEqual(payload["apply_scans"], scans)
        self._assert_writer_overlap(
            payload["recover_wins"],
            "MarkedOutcomeUnknown",
            "RecoveredUnknown",
        )
        recovered = payload["recover_fresh"]
        self.assertEqual(recovered["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(recovered["blocks"], 1)
        self.assertIsNone(recovered["acknowledged"])
        self.assertEqual(recovered["version"], 0)
        self.assertEqual(recovered["next_sequence"], 1)
        self.assertEqual(recovered["messages"], [])
        self.assertIsNone(recovered["slot_operation"])
        self.assertEqual(payload["recover_scans"], scans)
        self._assert_writer_overlap(
            payload["ack_wins"],
            "Acknowledged",
            "AcknowledgedDiscard",
        )
        acknowledged = payload["ack_fresh"]
        self.assertEqual(acknowledged["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(acknowledged["blocks"], 0)
        self.assertIsNotNone(acknowledged["acknowledged"])
        self.assertEqual(acknowledged["category"], "provider_outcome_uncertain")
        self.assertEqual(acknowledged["messages"], [])
        self.assertEqual(acknowledged["version"], 0)
        self.assertEqual(acknowledged["slot_operation"], payload["occupant_id"])
        self.assertEqual(payload["ack_scans"], scans)
        self._assert_writer_overlap(
            payload["late_wins"],
            "Reconciled",
            "NotUnknown",
        )
        retained = payload["late_fresh"]
        self.assertEqual(retained["status"], "SUCCEEDED")
        self.assertEqual(retained["blocks"], 0)
        self.assertEqual(retained["version"], 1)
        self.assertEqual(retained["next_sequence"], 3)
        self.assertEqual(
            retained["messages"],
            [
                [1, "user", "overlap late request"],
                [2, "assistant", "overlap retained assistant"],
            ],
        )
        self.assertEqual(retained["category"], "provider_outcome_uncertain")
        self.assertEqual(retained["slot_operation"], payload["occupant_id"])
        self.assertEqual(payload["late_scans"], scans)
        self.assertEqual(payload["occupant_after"]["status"], "RUNNING")
        self.assertEqual(payload["occupant_after"]["blocks"], 1)
        self.assertEqual(
            payload["occupant_after"]["slot_operation"],
            payload["occupant_id"],
        )
        self.assertEqual(payload["occupant_after"]["messages"], [])

    def _assert_writer_overlap(self, pair, winner, loser):
        self.assertEqual(pair["errors"], [])
        self.assertEqual(pair["winner"], winner)
        self.assertEqual(pair["loser"], loser)
        overlap = pair["overlap"]
        self.assertEqual(overlap["winner_begin_sql"], "BEGIN IMMEDIATE")
        self.assertEqual(overlap["loser_begin_sql"], "BEGIN IMMEDIATE")
        self.assertTrue(overlap["winner_waited"])
        self.assertLess(
            overlap["winner_begin_returned"],
            overlap["loser_begin_entered"],
        )
        self.assertLess(
            overlap["loser_begin_entered"],
            overlap["winner_commit_entered"],
        )
        self.assertLess(
            overlap["winner_commit_entered"],
            overlap["loser_begin_returned"],
        )


if __name__ == "__main__":
    unittest.main()
