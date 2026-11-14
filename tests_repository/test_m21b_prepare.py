"""Disposable-database tests for the ordered PREPARE transaction."""

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
import base64
import json
import os
import sqlite3
import threading
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
from max_smart_db.application_validation import SIGNED_INT64_MAX
from max_smart_db.config import (
    IDEMPOTENCY_EXPIRATION_US,
    QUEUED_RESERVATION_US,
)
from max_smart_db.creation import SqliteConversationRepository
from max_smart_db.fingerprint import fingerprint_v1
from max_smart_db.idempotency import (
    IdempotencyKeyError,
    idempotency_key_digest,
)
from max_smart_db.models import (
    Conversation,
    GlobalModelSlot,
    IdempotencyKey,
    Operation,
)
from max_smart_db.prepare import PrepareInputError
from max_smart_db.repository import (
    CommitAmbiguous,
    ConversationBlocked,
    DatabaseBusy,
    Existing,
    KeyBindingMismatch,
    ModelBusy,
    NotFound,
    PrepareCommand,
    Prepared,
    RepositoryIntegrityError,
    StaleVersion,
)
import max_smart_db.prepare as prepare_module

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

OWNER = User.objects.create(username="owner")
OTHER = User.objects.create(username="other")
CONVERSATION = uuid.UUID("00112233445566778899aabbccddeeff")
OTHER_CONVERSATION = uuid.UUID("11112222333344445555666677778888")
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"

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
Conversation.objects.create(
    id=OTHER_CONVERSATION,
    owner_id=OTHER.id,
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


def command(*, key=None, content="hello", expected=0, actor=None,
            conversation=None, provider="openai", model="gpt-5.6-sol"):
    return PrepareCommand(
        actor_id=OWNER.id if actor is None else actor,
        conversation_id=CONVERSATION if conversation is None else conversation,
        expected_version=expected,
        request_content=content,
        idempotency_key=key_for(1) if key is None else key,
        provider_id=provider,
        model_id=model,
    )


def repository(now=1_000):
    clock = Clock(now)
    return SqliteConversationRepository(clock, RUNNER), clock


def insert_live(identifier, conversation, owner, status, *, reservation=None,
                started=None, deadline=None, lease=None, runner=RUNNER,
                blocks=1, finished=None, failure=None):
    Operation.objects.create(
        id=identifier,
        conversation_id=conversation,
        owner_id=owner,
        operation_type="message",
        provider_id="openai",
        model_id="gpt-5.6-sol",
        status=status,
        blocks_conversation=blocks,
        expected_version=0,
        request_content="old",
        fingerprint_version=1,
        request_hash="b" * 64,
        retry_of=None,
        runner_id=runner,
        reservation_expires_at_us=reservation,
        provider_started_at_us=started,
        execution_deadline_us=deadline,
        lease_expires_at_us=lease,
        created_at_us=0,
        finished_at_us=finished,
        acknowledged_at_us=None,
        failure_category=failure,
    )


def occupy(identifier, *, reservation=None, lease=None, runner=RUNNER):
    GlobalModelSlot.objects.filter(pk=1).update(
        operation_id=identifier,
        runner_id=runner,
        reservation_expires_at_us=reservation,
        lease_expires_at_us=lease,
        acquired_at_us=0,
    )
"""


def _run_disposable(body, *, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        database = Path(temporary) / "r3c2.sqlite3"
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


class PrepareTests(unittest.TestCase):
    def test_success_uses_exact_expiries_digest_fingerprint_and_first_id(self):
        result = _run_disposable(
            """\
            repo, clock = repository()
            supplied_key = key_for(1)
            trace = []
            connection.connection.set_trace_callback(trace.append)
            outcome = repo.prepare(command(key=supplied_key))
            connection.connection.set_trace_callback(None)
            operation = Operation.objects.get(pk=outcome.reservation.operation_id)
            binding = IdempotencyKey.objects.get(operation_id=operation.id)
            slot = GlobalModelSlot.objects.get(pk=1)
            expected_hash = fingerprint_v1(
                OWNER.id,
                CONVERSATION.hex,
                "message",
                0,
                "max-smart",
                "openai",
                "gpt-5.6-sol",
                "hello",
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM idempotency_key WHERE key_digest = %s",
                    (supplied_key,),
                )
                raw_matches = cursor.fetchone()[0]
            print(json.dumps({
                "type": type(outcome).__name__,
                "calls": clock.calls,
                "reservation": outcome.reservation.reservation_expires_at,
                "operation": [
                    operation.status,
                    operation.blocks_conversation,
                    operation.runner_id,
                    operation.reservation_expires_at_us,
                    operation.created_at_us,
                    operation.request_hash,
                ],
                "binding": [
                    binding.id,
                    binding.key_digest_version,
                    binding.key_digest,
                    binding.expires_at_us,
                    binding.request_hash,
                ],
                "slot": [
                    str(slot.operation_id),
                    slot.runner_id,
                    slot.reservation_expires_at_us,
                    slot.lease_expires_at_us,
                    slot.acquired_at_us,
                ],
                "expected_digest": idempotency_key_digest(supplied_key),
                "expected_hash": expected_hash,
                "raw_matches": raw_matches,
                "insert_order": [
                    next(
                        index for index, statement in enumerate(trace)
                        if statement.startswith('INSERT INTO "operation"')
                    ),
                    next(
                        index for index, statement in enumerate(trace)
                        if statement.startswith('INSERT INTO "idempotency_key"')
                    ),
                    next(
                        index for index, statement in enumerate(trace)
                        if statement.startswith('UPDATE "global_model_slot"')
                    ),
                ],
                "constants": [
                    QUEUED_RESERVATION_US,
                    IDEMPOTENCY_EXPIRATION_US,
                ],
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "Prepared")
        self.assertEqual(payload["calls"], 1)
        self.assertEqual(payload["constants"], [60_000_000, 2_592_000_000_000])
        self.assertEqual(payload["reservation"], 60_001_000)
        self.assertEqual(payload["operation"][:5], [
            "QUEUED",
            1,
            "AAAAAAAAAAAAAAAAAAAAAA",
            60_001_000,
            1_000,
        ])
        self.assertEqual(payload["operation"][5], payload["expected_hash"])
        self.assertEqual(payload["binding"][:2], [1, 1])
        self.assertEqual(payload["binding"][2], payload["expected_digest"])
        self.assertEqual(payload["binding"][3], 2_592_000_001_000)
        self.assertEqual(payload["binding"][4], payload["expected_hash"])
        self.assertEqual(payload["slot"][1:], [
            "AAAAAAAAAAAAAAAAAAAAAA",
            60_001_000,
            None,
            1_000,
        ])
        self.assertEqual(payload["raw_matches"], 0)
        self.assertEqual(
            payload["insert_order"],
            sorted(payload["insert_order"]),
        )

    def test_existing_expired_binding_precedes_version_blocker_and_slot(self):
        result = _run_disposable(
            """\
            repo, clock = repository()
            first = repo.prepare(command())
            operation_id = first.reservation.operation_id
            expiration = IdempotencyKey.objects.get(
                operation_id=operation_id
            ).expires_at_us
            clock.value = expiration + 1
            Conversation.objects.filter(pk=CONVERSATION).update(version=9)
            exact = repo.prepare(command())
            mismatch = repo.prepare(command(content="changed"))
            print(json.dumps({
                "types": [type(exact).__name__, type(mismatch).__name__],
                "same_id": str(exact.operation.operation_id) == str(operation_id),
                "expired": clock.value > expiration,
                "calls": clock.calls,
                "counts": [
                    Operation.objects.count(),
                    IdempotencyKey.objects.count(),
                ],
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["types"], ["Existing", "KeyBindingMismatch"])
        self.assertTrue(payload["same_id"])
        self.assertTrue(payload["expired"])
        self.assertEqual(payload["calls"], 3)
        self.assertEqual(payload["counts"], [1, 1])

    def test_stale_version_precedes_recovery(self):
        result = _run_disposable(
            """\
            identifier = uuid.UUID("22222222222222222222222222222222")
            insert_live(
                identifier,
                OTHER_CONVERSATION,
                OTHER.id,
                "RUNNING",
                started=1,
                deadline=2,
                lease=2,
            )
            occupy(identifier, lease=2)
            Conversation.objects.filter(pk=CONVERSATION).update(version=1)
            repo, clock = repository(now=100)
            outcome = repo.prepare(command(expected=0))
            operation = Operation.objects.get(pk=identifier)
            slot = GlobalModelSlot.objects.get(pk=1)
            print(json.dumps({
                "type": type(outcome).__name__,
                "actual": outcome.actual,
                "calls": clock.calls,
                "operation": operation.status,
                "slot": str(slot.operation_id),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "StaleVersion")
        self.assertEqual(payload["actual"], 1)
        self.assertEqual(payload["calls"], 1)
        self.assertEqual(payload["operation"], "RUNNING")
        self.assertEqual(
            payload["slot"],
            "22222222-2222-2222-2222-222222222222",
        )

    def test_recovery_commits_with_blocked_and_live_slot_returns_busy(self):
        blocked = _run_disposable(
            """\
            identifier = uuid.UUID("33333333333333333333333333333333")
            insert_live(
                identifier,
                CONVERSATION,
                OWNER.id,
                "RUNNING",
                started=1,
                deadline=2,
                lease=2,
            )
            occupy(identifier, lease=2)
            repo, clock = repository(now=100)
            outcome = repo.prepare(command())
            connection.close()
            operation = Operation.objects.get(pk=identifier)
            slot = GlobalModelSlot.objects.get(pk=1)
            print(json.dumps({
                "type": type(outcome).__name__,
                "blocker": str(outcome.operation_id),
                "status": outcome.status.value,
                "operation": [operation.status, operation.blocks_conversation],
                "slot_free": slot.operation_id is None,
                "new_counts": [
                    Operation.objects.count(),
                    IdempotencyKey.objects.count(),
                ],
                "calls": clock.calls,
            }))
            """
        )
        payload = _payload(self, blocked)
        self.assertEqual(payload["type"], "ConversationBlocked")
        self.assertEqual(payload["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(payload["operation"], ["OUTCOME_UNKNOWN", 1])
        self.assertTrue(payload["slot_free"])
        self.assertEqual(payload["new_counts"], [1, 0])
        self.assertEqual(payload["calls"], 1)

        busy = _run_disposable(
            """\
            identifier = uuid.UUID("44444444444444444444444444444444")
            insert_live(
                identifier,
                OTHER_CONVERSATION,
                OTHER.id,
                "QUEUED",
                reservation=101,
            )
            occupy(identifier, reservation=101)
            repo, clock = repository(now=100)
            outcome = repo.prepare(command())
            print(json.dumps({
                "type": type(outcome).__name__,
                "counts": [
                    Operation.objects.count(),
                    IdempotencyKey.objects.count(),
                ],
                "calls": clock.calls,
            }))
            """
        )
        payload = _payload(self, busy)
        self.assertEqual(payload["type"], "ModelBusy")
        self.assertEqual(payload["counts"], [1, 0])
        self.assertEqual(payload["calls"], 1)

    def test_recovery_survives_savepoint_integrity_failure(self):
        result = _run_disposable(
            """\
            identifier = uuid.UUID("55555555555555555555555555555555")
            insert_live(
                identifier,
                OTHER_CONVERSATION,
                OTHER.id,
                "RUNNING",
                started=1,
                deadline=2,
                lease=2,
            )
            occupy(identifier, lease=2)
            repo, clock = repository(now=100)
            real_create = IdempotencyKey.objects.create
            def reject_key(**kwargs):
                raise IntegrityError("injected key insert failure")
            IdempotencyKey.objects.create = reject_key
            try:
                try:
                    repo.prepare(command())
                except RepositoryIntegrityError:
                    outcome = "RepositoryIntegrityError"
                else:
                    outcome = "accepted"
            finally:
                IdempotencyKey.objects.create = real_create
            connection.close()
            recovered = Operation.objects.get(pk=identifier)
            slot = GlobalModelSlot.objects.get(pk=1)
            print(json.dumps({
                "outcome": outcome,
                "recovered": [
                    recovered.status,
                    recovered.blocks_conversation,
                ],
                "slot_free": slot.operation_id is None,
                "counts": [
                    Operation.objects.count(),
                    IdempotencyKey.objects.count(),
                ],
                "calls": clock.calls,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["outcome"], "RepositoryIntegrityError")
        self.assertEqual(payload["recovered"], ["OUTCOME_UNKNOWN", 1])
        self.assertTrue(payload["slot_free"])
        self.assertEqual(payload["counts"], [1, 0])
        self.assertEqual(payload["calls"], 1)

    def test_zero_slot_acquisition_rolls_back_new_rows(self):
        result = _run_disposable(
            """\
            repo, clock = repository()
            real_update = QuerySet.update
            def zero_slot(self, **kwargs):
                if (
                    self.model is GlobalModelSlot
                    and kwargs.get("operation_id") is not None
                ):
                    return 0
                return real_update(self, **kwargs)
            QuerySet.update = zero_slot
            try:
                try:
                    repo.prepare(command())
                except RepositoryIntegrityError:
                    outcome = "RepositoryIntegrityError"
                else:
                    outcome = "accepted"
            finally:
                QuerySet.update = real_update
            slot = GlobalModelSlot.objects.get(pk=1)
            print(json.dumps({
                "outcome": outcome,
                "calls": clock.calls,
                "counts": [
                    Operation.objects.count(),
                    IdempotencyKey.objects.count(),
                ],
                "slot_free": slot.operation_id is None,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["outcome"], "RepositoryIntegrityError")
        self.assertEqual(payload["calls"], 1)
        self.assertEqual(payload["counts"], [0, 0])
        self.assertTrue(payload["slot_free"])

    def test_id_allocation_first_max_plus_one_and_exhaustion(self):
        first = _run_disposable(
            """\
            repo, clock = repository()
            outcome = repo.prepare(command())
            print(json.dumps(
                IdempotencyKey.objects.get(
                    operation_id=outcome.reservation.operation_id
                ).id
            ))
            """
        )
        self.assertEqual(_payload(self, first), 1)

        next_id = _run_disposable(
            """\
            terminal = uuid.UUID("66666666666666666666666666666666")
            insert_live(
                terminal,
                OTHER_CONVERSATION,
                OTHER.id,
                "CANCELLED",
                runner=None,
                blocks=0,
                finished=1,
            )
            IdempotencyKey.objects.create(
                id=41,
                owner_id=OTHER.id,
                conversation_id=OTHER_CONVERSATION,
                operation_type="message",
                expected_version=0,
                key_digest_version=1,
                key_digest="c" * 64,
                fingerprint_version=1,
                request_hash="b" * 64,
                operation_id=terminal,
                expires_at_us=1,
            )
            repo, clock = repository()
            outcome = repo.prepare(command())
            print(json.dumps(
                IdempotencyKey.objects.get(
                    operation_id=outcome.reservation.operation_id
                ).id
            ))
            """
        )
        self.assertEqual(_payload(self, next_id), 42)

        exhausted = _run_disposable(
            """\
            terminal = uuid.UUID("77777777777777777777777777777777")
            insert_live(
                terminal,
                OTHER_CONVERSATION,
                OTHER.id,
                "CANCELLED",
                runner=None,
                blocks=0,
                finished=1,
            )
            IdempotencyKey.objects.create(
                id=SIGNED_INT64_MAX,
                owner_id=OTHER.id,
                conversation_id=OTHER_CONVERSATION,
                operation_type="message",
                expected_version=0,
                key_digest_version=1,
                key_digest="d" * 64,
                fingerprint_version=1,
                request_hash="b" * 64,
                operation_id=terminal,
                expires_at_us=1,
            )
            repo, clock = repository()
            try:
                repo.prepare(command())
            except RepositoryIntegrityError:
                outcome = "RepositoryIntegrityError"
            else:
                outcome = "accepted"
            print(json.dumps({
                "outcome": outcome,
                "counts": [
                    Operation.objects.count(),
                    IdempotencyKey.objects.count(),
                ],
            }))
            """
        )
        payload = _payload(self, exhausted)
        self.assertEqual(payload["outcome"], "RepositoryIntegrityError")
        self.assertEqual(payload["counts"], [1, 1])

    def test_timestamp_overflow_rejects_without_persistence(self):
        result = _run_disposable(
            """\
            repo, clock = repository(
                now=SIGNED_INT64_MAX - IDEMPOTENCY_EXPIRATION_US + 1
            )
            try:
                repo.prepare(command())
            except PrepareInputError as error:
                outcome = error.code
            else:
                outcome = "accepted"
            print(json.dumps({
                "outcome": outcome,
                "calls": clock.calls,
                "counts": [
                    Operation.objects.count(),
                    IdempotencyKey.objects.count(),
                ],
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["outcome"], "idempotency_timestamp_overflow")
        self.assertEqual(payload["calls"], 1)
        self.assertEqual(payload["counts"], [0, 0])

    def test_key_validation_is_canonical_and_database_free(self):
        sys.path.insert(0, str(SRC_ROOT))
        from max_smart_db.idempotency import (
            IdempotencyKeyError,
            idempotency_key_digest,
        )

        canonical = "AAAAAAAAAAAAAAAAAAAAAA"
        self.assertEqual(
            idempotency_key_digest(canonical),
            "374708fff7719dd5979ec875d56cd2286f6d3cf7ec317a3b25632aab28ec37bb",
        )
        for value, code in (
            (canonical + "=", "idempotency_key_format"),
            ("A" * 10, "idempotency_key_entropy"),
            ("AB", "idempotency_key_canonical"),
            ("A+AAAAAAAAAAAAAAAAAAAA", "idempotency_key_format"),
            (b"A" * 22, "idempotency_key_type"),
        ):
            with self.subTest(value=value):
                with self.assertRaises(IdempotencyKeyError) as caught:
                    idempotency_key_digest(value)
                self.assertEqual(caught.exception.code, code)

    def test_authorization_and_boundary_inputs_fail_before_inserts(self):
        result = _run_disposable(
            """\
            repo, clock = repository()
            unauthorized = repo.prepare(command(actor=OTHER.id))
            rejected = []
            for kwargs in (
                {"provider": "other"},
                {"model": "other"},
                {"content": ""},
                {"content": "bad\\x00content"},
                {"expected": -1},
                {"key": "short"},
            ):
                try:
                    repo.prepare(command(**kwargs))
                except Exception as error:
                    rejected.append(type(error).__name__)
                else:
                    rejected.append("accepted")
            print(json.dumps({
                "not_found": type(unauthorized).__name__,
                "rejected": rejected,
                "counts": [
                    Operation.objects.count(),
                    IdempotencyKey.objects.count(),
                ],
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["not_found"], "NotFound")
        self.assertEqual(payload["rejected"], [
            "PrepareInputError",
            "PrepareInputError",
            "PrepareInputError",
            "PrepareInputError",
            "PrepareInputError",
            "IdempotencyKeyError",
        ])
        self.assertEqual(payload["counts"], [0, 0])

    def test_busy_and_commit_ambiguity_are_typed(self):
        result = _run_disposable(
            """\
            repo, clock = repository()
            holder = sqlite3.connect(
                f"{database.as_uri()}?mode=rw",
                uri=True,
            )
            try:
                holder.execute("BEGIN IMMEDIATE")
                busy = repo.prepare(command())
            finally:
                holder.rollback()
                holder.close()
            busy_calls = clock.calls

            allocated = uuid.UUID("88888888888888888888888888888888")
            real_uuid4 = prepare_module.uuid.uuid4
            prepare_module.uuid.uuid4 = lambda: allocated
            real_commit = connection.commit
            def fail_commit(*args, **kwargs):
                raise OperationalError("injected commit failure")
            connection.commit = fail_commit
            try:
                ambiguous = repo.prepare(command())
            finally:
                connection.commit = real_commit
                prepare_module.uuid.uuid4 = real_uuid4
            connection.close()
            print(json.dumps({
                "busy": type(busy).__name__,
                "busy_calls": busy_calls,
                "ambiguous": type(ambiguous).__name__,
                "operation_id": str(ambiguous.operation_id),
                "runner": ambiguous.runner_id,
                "calls": clock.calls,
                "counts": [
                    Operation.objects.count(),
                    IdempotencyKey.objects.count(),
                ],
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["busy"], "DatabaseBusy")
        self.assertEqual(payload["busy_calls"], 0)
        self.assertEqual(payload["ambiguous"], "CommitAmbiguous")
        self.assertEqual(
            payload["operation_id"],
            "88888888-8888-8888-8888-888888888888",
        )
        self.assertEqual(payload["runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(payload["calls"], 1)
        self.assertEqual(payload["counts"], [0, 0])

    def test_concurrent_same_key_serializes_to_prepared_then_existing(self):
        result = _run_disposable(
            """\
            from django.db import close_old_connections

            barrier = threading.Barrier(2)
            outcomes = []
            failures = []

            def worker():
                close_old_connections()
                repo, worker_clock = repository()
                barrier.wait()
                try:
                    outcome = repo.prepare(command())
                    outcomes.append(type(outcome).__name__)
                except Exception as error:
                    failures.append(type(error).__name__)
                finally:
                    close_old_connections()

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            connection.close()
            print(json.dumps({
                "outcomes": sorted(outcomes),
                "failures": failures,
                "counts": [
                    Operation.objects.count(),
                    IdempotencyKey.objects.count(),
                ],
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["outcomes"].count("Prepared"), 1)
        self.assertIn(
            payload["outcomes"][0]
            if payload["outcomes"][1] == "Prepared"
            else payload["outcomes"][1],
            ("Existing", "ModelBusy"),
        )
        self.assertEqual(payload["failures"], [])
        self.assertEqual(payload["counts"], [1, 1])

    def test_no_cleanup_or_raw_key_storage_code_is_present(self):
        source = (SRC_ROOT / "max_smart_db" / "prepare.py").read_text(
            encoding="utf-8",
        )
        self.assertNotIn(".delete(", source)
        self.assertNotIn("last_insert_rowid", source)
        self.assertNotIn("cleanup", source.lower())
        self.assertNotIn("idempotency_key=command.idempotency_key", source)


if __name__ == "__main__":
    unittest.main()
