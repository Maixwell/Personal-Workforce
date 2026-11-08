"""Disposable-database tests for pre-provider failure handling."""

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
from conversation_service import (
    ConversationService,
    PreProviderFailureUnconfirmed,
    StandingContextRejection,
    TurnRequest,
)
from max_smart_db.creation import SqliteConversationRepository
from max_smart_db.models import Conversation, GlobalModelSlot, Message, Operation
from max_smart_db.prepare import PrepareInputError
from max_smart_db.repository import (
    CommitAmbiguous,
    PreProviderFailureCategory,
    PreProviderStateNotProven,
)

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

OWNER = User.objects.create(username="owner")
CONVERSATION = __import__("uuid").UUID("00112233445566778899aabbccddeeff")
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
STALE = "BBBBBBBBBBBBBBBBBBBBBQ"

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


def repository(now=123_456, runner=RUNNER):
    return SqliteConversationRepository(Clock(now), runner)


def request(key=1, model="gpt-5.6-sol", expected=0):
    return TurnRequest(
        actor_id=OWNER.id,
        conversation_id=CONVERSATION,
        request_content="hello",
        idempotency_key=key_for(key),
        runner_id=RUNNER,
        expected_version=expected,
        provider_id="openai",
        model_id=model,
    )


def facts(operation_id):
    operation = Operation.objects.get(pk=operation_id)
    slot = GlobalModelSlot.objects.get(pk=1)
    return {
        "status": operation.status,
        "runner": operation.runner_id,
        "started": operation.provider_started_at_us,
        "category": operation.failure_category,
        "slot_operation": None if slot.operation_id is None else str(slot.operation_id),
        "slot_runner": slot.runner_id,
        "messages": Message.objects.filter(operation_id=operation_id).count(),
        "version": Conversation.objects.get(pk=CONVERSATION).version,
    }


def service(load, repo=None, counts=None):
    if repo is None:
        repo = repository()
    box = counts if counts is not None else {"provider": 0, "failure": 0}
    real_failure = repo.fail_pre_provider

    def fail_pre_provider(reservation, category):
        box["failure"] += 1
        box["failure_atomic"] = connection.in_atomic_block
        return real_failure(reservation, category)

    repo.fail_pre_provider = fail_pre_provider

    def provide(**kwargs):
        box["provider"] += 1
        return "model-output"

    def load_context(**kwargs):
        box["context_atomic"] = connection.in_atomic_block
        box["context_autocommit"] = connection.get_autocommit()
        return load(box, **kwargs)

    return ConversationService(repo, load_context, provide), box, repo
"""


def _run(body, *, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "m3s3-service.sqlite3"
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


class PreProviderServiceTests(unittest.TestCase):
    def test_context_failure_records_pre_provider_failure(self):
        result = _run(
            """\
            def load(box, **kwargs):
                raise RuntimeError("standing context unavailable")

            handler, box, repo = service(load)
            outcome = handler.submit(request())
            operation = Operation.objects.get(owner_id=OWNER.id)
            print(json.dumps({
                "type": type(outcome).__name__,
                "category": outcome.failure_category.value,
                "provider": box["provider"],
                "failure_writes": box["failure"],
                "context_atomic": box["context_atomic"],
                "context_autocommit": box["context_autocommit"],
                "autocommit": connection.get_autocommit(),
                "atomic": connection.in_atomic_block,
                "facts": facts(operation.id),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "DurablePreProviderFailure")
        self.assertEqual(payload["category"], "context_unavailable")
        self.assertEqual(payload["provider"], 0)
        self.assertEqual(payload["failure_writes"], 1)
        self.assertFalse(payload["context_atomic"])
        self.assertTrue(payload["context_autocommit"])
        self.assertTrue(payload["autocommit"])
        self.assertFalse(payload["atomic"])
        self.assertEqual(payload["facts"]["status"], "FAILED")
        self.assertIsNone(payload["facts"]["started"])
        self.assertIsNone(payload["facts"]["slot_operation"])
        self.assertEqual(payload["facts"]["messages"], 0)
        self.assertEqual(payload["facts"]["version"], 0)

    def test_approved_categories_are_recorded_without_a_provider_call(self):
        result = _run(
            """\
            def once(key, category):
                def load(box, **kwargs):
                    return StandingContextRejection(category)
                handler, box, repo = service(load)
                outcome = handler.submit(request(key))
                operation = Operation.objects.get(pk=outcome.operation_id)
                row = {
                    "category": outcome.failure_category.value,
                    "stored": operation.failure_category,
                    "provider": box["provider"],
                    "failure_writes": box["failure"],
                    "slot": facts(operation.id)["slot_operation"],
                }
                return row

            print(json.dumps([
                once(2, PreProviderFailureCategory.CONTEXT_BUDGET_EXCEEDED),
                once(3, PreProviderFailureCategory.MODEL_CONFIGURATION_INVALID),
            ]))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(
            [row["category"] for row in payload],
            ["context_budget_exceeded", "model_configuration_invalid"],
        )
        for row in payload:
            self.assertEqual(row["stored"], row["category"])
            self.assertEqual(row["provider"], 0)
            self.assertEqual(row["failure_writes"], 1)
            self.assertIsNone(row["slot"])

    def test_invalid_model_does_not_create_an_operation(self):
        result = _run(
            """\
            def load(box, **kwargs):
                raise RuntimeError("should not load")

            handler, box, repo = service(load)
            try:
                handler.submit(request(model="not-a-model"))
                error = None
            except PrepareInputError as raised:
                error = raised.code
            print(json.dumps({
                "error": error,
                "provider": box["provider"],
                "failure_writes": box["failure"],
                "operations": Operation.objects.count(),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["error"], "model_invalid")
        self.assertEqual(payload["provider"], 0)
        self.assertEqual(payload["failure_writes"], 0)
        self.assertEqual(payload["operations"], 0)

    def test_unapproved_rejection_is_preserved_without_a_write(self):
        result = _run(
            """\
            def load(box, **kwargs):
                return StandingContextRejection("not-a-category")

            handler, box, repo = service(load)
            outcome = handler.submit(request(4))
            operation = Operation.objects.get(owner_id=OWNER.id)
            print(json.dumps({
                "type": type(outcome).__name__,
                "category": outcome.category,
                "provider": box["provider"],
                "failure_writes": box["failure"],
                "status": operation.status,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "StandingContextRejection")
        self.assertEqual(payload["category"], "not-a-category")
        self.assertEqual(payload["provider"], 0)
        self.assertEqual(payload["failure_writes"], 0)
        self.assertEqual(payload["status"], "QUEUED")

    def test_ambiguous_failure_commit_is_confirmed_without_a_second_write(self):
        payload = self._ambiguous(True)
        self.assertEqual(payload["failed"], "CommitAmbiguous")
        self.assertEqual(payload["type"], "DurablePreProviderFailure")
        self.assertEqual(payload["category"], "context_unavailable")
        self.assertEqual(payload["provider"], 0)
        self.assertEqual(payload["failure_writes"], 1)
        self.assertEqual(payload["facts"]["status"], "FAILED")
        self.assertIsNone(payload["facts"]["slot_operation"])

    def test_ambiguous_failure_rollback_is_not_retried(self):
        payload = self._ambiguous(False)
        self.assertEqual(payload["failed"], "CommitAmbiguous")
        self.assertEqual(payload["type"], "PreProviderFailureUnconfirmed")
        self.assertEqual(payload["provider"], 0)
        self.assertEqual(payload["failure_writes"], 1)
        self.assertEqual(payload["facts"]["status"], "QUEUED")
        self.assertEqual(payload["facts"]["runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(
            payload["facts"]["slot_operation"],
            payload["operation_id"],
        )

    def test_inconclusive_inspection_fails_closed(self):
        result = _run(
            """\
            def scenario(mode):
                repo = repository()
                real_failure = repo.fail_pre_provider
                real_inspect = repo.inspect_pre_provider_failure

                def fail_pre_provider(reservation, category):
                    real_failure(reservation, category)
                    return CommitAmbiguous(
                        operation_id=reservation.operation_id,
                        runner_id=reservation.runner_id,
                        conversation_id=reservation.conversation_id,
                    )

                def inspect_pre_provider_failure(*args, **kwargs):
                    if mode == "error":
                        raise OperationalError("simulated incomplete read")
                    return PreProviderStateNotProven()

                repo.fail_pre_provider = fail_pre_provider
                repo.inspect_pre_provider_failure = inspect_pre_provider_failure

                def load(box, **kwargs):
                    raise RuntimeError("standing context unavailable")

                handler, box, repo = service(load, repo)
                outcome = handler.submit(request(5 if mode == "error" else 6))
                operation = Operation.objects.get(pk=outcome.operation_id)
                return {
                    "mode": mode,
                    "type": type(outcome).__name__,
                    "provider": box["provider"],
                    "status": operation.status,
                }

            print(json.dumps([scenario("error"), scenario("unproven")]))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(len(payload), 2)
        for row in payload:
            self.assertEqual(row["type"], "PreProviderFailureUnconfirmed", row["mode"])
            self.assertEqual(row["provider"], 0, row["mode"])
            self.assertEqual(row["status"], "FAILED", row["mode"])

    def test_adopted_runner_cannot_be_failed_by_the_original_runner(self):
        result = _run(
            """\
            def load(box, **kwargs):
                operation = Operation.objects.get(pk=kwargs["operation_id"])
                adopted = SqliteConversationRepository(
                    Clock(operation.reservation_expires_at_us - 1),
                    STALE,
                ).adopt_queued(OWNER.id, operation.id)
                box["adopted"] = type(adopted).__name__
                raise RuntimeError("context after adoption")

            handler, box, repo = service(load)
            outcome = handler.submit(request(7))
            operation = Operation.objects.get(owner_id=OWNER.id)
            print(json.dumps({
                "type": type(outcome).__name__,
                "adopted": box["adopted"],
                "provider": box["provider"],
                "failure_writes": box["failure"],
                "facts": facts(operation.id),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["adopted"], "Adopted")
        self.assertEqual(payload["type"], "OwnershipLost")
        self.assertEqual(payload["provider"], 0)
        self.assertEqual(payload["failure_writes"], 1)
        self.assertEqual(payload["facts"]["status"], "QUEUED")
        self.assertEqual(payload["facts"]["runner"], "BBBBBBBBBBBBBBBBBBBBBQ")
        self.assertEqual(payload["facts"]["slot_runner"], "BBBBBBBBBBBBBBBBBBBBBQ")

    def test_recovered_reservation_is_not_failed_again(self):
        result = _run(
            """\
            def load(box, **kwargs):
                operation = Operation.objects.get(pk=kwargs["operation_id"])
                recovered = SqliteConversationRepository(
                    Clock(operation.reservation_expires_at_us),
                    RUNNER,
                ).recover_slot()
                box["recovered"] = type(recovered).__name__
                raise RuntimeError("context after recovery")

            handler, box, repo = service(load)
            outcome = handler.submit(request(8))
            operation = Operation.objects.get(owner_id=OWNER.id)
            print(json.dumps({
                "type": type(outcome).__name__,
                "recovered": box["recovered"],
                "provider": box["provider"],
                "failure_writes": box["failure"],
                "facts": facts(operation.id),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["recovered"], "CancelledExpiredQueued")
        self.assertEqual(payload["type"], "NotQueued")
        self.assertEqual(payload["provider"], 0)
        self.assertEqual(payload["failure_writes"], 1)
        self.assertEqual(payload["facts"]["status"], "CANCELLED")
        self.assertIsNone(payload["facts"]["category"])
        self.assertIsNone(payload["facts"]["slot_operation"])

    def test_prepare_rejection_does_not_invoke_the_provider(self):
        result = _run(
            """\
            def load(box, **kwargs):
                raise RuntimeError("should not load")

            handler, box, repo = service(load)
            stale = handler.submit(request(9, expected=4))
            print(json.dumps({
                "type": type(stale).__name__,
                "provider": box["provider"],
                "failure_writes": box["failure"],
                "operations": Operation.objects.count(),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "StaleVersion")
        self.assertEqual(payload["provider"], 0)
        self.assertEqual(payload["failure_writes"], 0)
        self.assertEqual(payload["operations"], 0)

    def test_outer_transaction_is_left_open(self):
        result = _run(
            """\
            def load(box, **kwargs):
                raise RuntimeError("should not load")

            handler, box, repo = service(load)
            with transaction.atomic():
                outcome = handler.submit(request(10))
                open_during = connection.in_atomic_block
            print(json.dumps({
                "type": type(outcome).__name__,
                "open_during": open_during,
                "provider": box["provider"],
                "failure_writes": box["failure"],
                "operations": Operation.objects.count(),
                "autocommit": connection.get_autocommit(),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["type"], "TransactionHeld")
        self.assertTrue(payload["open_during"])
        self.assertEqual(payload["provider"], 0)
        self.assertEqual(payload["failure_writes"], 0)
        self.assertEqual(payload["operations"], 0)
        self.assertTrue(payload["autocommit"])

    def test_production_database_is_absent(self):
        self.assertFalse(PRODUCTION_DATABASE.exists())

    def _ambiguous(self, durable):
        return _payload(self, _run(
            f"""\
            repo = repository()
            real_failure = repo.fail_pre_provider
            state = {{"writes": 0}}

            def fail_pre_provider(reservation, category):
                state["writes"] += 1
                if state["writes"] > 1:
                    return real_failure(reservation, category)
                real_commit = connection.commit

                def commit(*args, **kwargs):
                    if {durable!r}:
                        real_commit(*args, **kwargs)
                        raise OperationalError("simulated post-commit failure")
                    raise OperationalError("simulated rollback before commit")

                connection.commit = commit
                try:
                    return real_failure(reservation, category)
                finally:
                    connection.commit = real_commit

            repo.fail_pre_provider = fail_pre_provider

            def load(box, **kwargs):
                raise RuntimeError("standing context unavailable")

            handler, box, repo = service(load, repo)
            outcome = handler.submit(request(11))
            operation = Operation.objects.get(owner_id=OWNER.id)
            print(json.dumps({{
                "failed": "CommitAmbiguous",
                "type": type(outcome).__name__,
                "category": getattr(
                    getattr(outcome, "failure_category", None),
                    "value",
                    None,
                ),
                "provider": box["provider"],
                "failure_writes": state["writes"],
                "operation_id": str(operation.id),
                "facts": facts(operation.id),
            }}))
            """
        ))
