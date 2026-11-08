"""Disposable-database tests for adopted execution identity."""

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
from django.db.migrations.executor import MigrationExecutor
from django.db import connection
from conversation_service import ConversationService, TurnRequest
from max_smart_db.creation import SqliteConversationRepository
from max_smart_db.models import Conversation, GlobalModelSlot, Message, Operation
from max_smart_db.repository import (
    PreProviderFailureCategory,
    PrepareCommand,
    RetryCommand,
)

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

OWNER = User.objects.create(username="owner")
CONVERSATION = uuid.UUID("00112233445566778899aabbccddeeff")
OTHER_CONVERSATION = uuid.UUID("00112233445566778899aabbccddee00")
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
NEW = "BBBBBBBBBBBBBBBBBBBBBQ"
ORIGINAL = "persisted original"
DIFFERENT = "different adopted request"
PREPARED_AT = 1_000_000

for conversation_id in (CONVERSATION, OTHER_CONVERSATION):
    Conversation.objects.create(
        id=conversation_id,
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

    def now_us(self):
        return self.value


def key_for(value):
    return base64.urlsafe_b64encode(bytes([value]) * 16).rstrip(b"=").decode()


def repository(now=PREPARED_AT, runner=RUNNER):
    clock = Clock(now)
    return SqliteConversationRepository(clock, runner), clock


def prepare(repo, key, content=ORIGINAL):
    prepared = repo.prepare(PrepareCommand(
        actor_id=OWNER.id,
        conversation_id=CONVERSATION,
        expected_version=0,
        request_content=content,
        idempotency_key=key_for(key),
        provider_id="openai",
        model_id="gpt-5.6-sol",
    ))
    if type(prepared).__name__ != "Prepared":
        raise RuntimeError(type(prepared).__name__)
    return prepared.reservation


def turn(operation_id, **overrides):
    values = {
        "actor_id": OWNER.id,
        "conversation_id": CONVERSATION,
        "request_content": ORIGINAL,
        "idempotency_key": key_for(90),
        "runner_id": NEW,
        "expected_version": 0,
        "provider_id": "openai",
        "model_id": "gpt-5.6-sol",
        "adopt_operation_id": operation_id,
    }
    values.update(overrides)
    return TurnRequest(**values)


def attempt(operation_id, **overrides):
    repo, _clock = repository(runner=NEW)
    counts = {"provider": 0, "claim": 0, "adopt": 0, "context": 0}
    real_claim = repo.claim_running
    real_adopt = repo.adopt_queued

    def claim_running(*args, **kwargs):
        counts["claim"] += 1
        return real_claim(*args, **kwargs)

    def adopt_queued(*args, **kwargs):
        counts["adopt"] += 1
        return real_adopt(*args, **kwargs)

    repo.claim_running = claim_running
    repo.adopt_queued = adopt_queued

    def loader(**kwargs):
        counts["context"] += 1
        return "standing-context"

    def provide(**kwargs):
        counts["provider"] += 1
        counts["seen"] = kwargs["request_content"]
        return "answer for " + kwargs["request_content"]

    outcome = ConversationService(repo, loader, provide).submit(
        turn(operation_id, **overrides)
    )
    return outcome, counts


def facts(operation_id):
    operation = Operation.objects.get(pk=operation_id)
    slot = GlobalModelSlot.objects.get(pk=1)
    messages = [
        {"role": row.role, "content": row.content}
        for row in Message.objects.order_by("sequence")
    ]
    return {
        "status": operation.status,
        "runner": operation.runner_id,
        "blocks": operation.blocks_conversation,
        "started": operation.provider_started_at_us,
        "content": operation.request_content,
        "messages": messages,
        "slot_runner": slot.runner_id,
        "slot_operation": None if slot.operation_id is None else str(slot.operation_id),
        "version": Conversation.objects.get(pk=operation.conversation_id).version,
    }


def report(operation_id, outcome, counts):
    described = {"type": type(outcome).__name__, "fields": len(getattr(outcome, "__dataclass_fields__", {}))}
    print(json.dumps({"result": described, "counts": counts, "state": facts(operation_id)}))
"""


def _run(body, *, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "m3-identity.sqlite3"
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


class AdoptedExecutionIdentityTests(unittest.TestCase):
    def _assert_untouched(self, payload):
        self.assertEqual(payload["result"]["type"], "ExecutionIdentityMismatch")
        self.assertEqual(payload["result"]["fields"], 0)
        self.assertEqual(payload["counts"], {
            "provider": 0,
            "claim": 0,
            "adopt": 0,
            "context": 0,
        })
        self.assertEqual(payload["state"]["status"], "QUEUED")
        self.assertEqual(payload["state"]["runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(payload["state"]["blocks"], 1)
        self.assertIsNone(payload["state"]["started"])
        self.assertEqual(payload["state"]["content"], "persisted original")
        self.assertEqual(payload["state"]["messages"], [])
        self.assertEqual(payload["state"]["slot_runner"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(payload["state"]["version"], 0)

    def test_different_request_content_does_not_claim(self):
        payload = _payload(self, _run(
            """\
            repo, _clock = repository()
            reservation = prepare(repo, 1)
            outcome, counts = attempt(
                reservation.operation_id,
                request_content=DIFFERENT,
            )
            report(reservation.operation_id, outcome, counts)
            """
        ))
        self._assert_untouched(payload)

    def test_wrong_conversation_does_not_claim(self):
        payload = _payload(self, _run(
            """\
            repo, _clock = repository()
            reservation = prepare(repo, 2)
            outcome, counts = attempt(
                reservation.operation_id,
                conversation_id=OTHER_CONVERSATION,
            )
            report(reservation.operation_id, outcome, counts)
            """
        ))
        self._assert_untouched(payload)

    def test_wrong_expected_version_does_not_claim(self):
        payload = _payload(self, _run(
            """\
            repo, _clock = repository()
            reservation = prepare(repo, 3)
            outcome, counts = attempt(
                reservation.operation_id,
                expected_version=4,
            )
            report(reservation.operation_id, outcome, counts)
            """
        ))
        self._assert_untouched(payload)

    def test_wrong_provider_does_not_claim(self):
        payload = _payload(self, _run(
            """\
            repo, _clock = repository()
            reservation = prepare(repo, 4)
            outcome, counts = attempt(
                reservation.operation_id,
                provider_id="other-provider",
            )
            report(reservation.operation_id, outcome, counts)
            """
        ))
        self._assert_untouched(payload)

    def test_wrong_model_does_not_claim(self):
        payload = _payload(self, _run(
            """\
            repo, _clock = repository()
            reservation = prepare(repo, 5)
            outcome, counts = attempt(
                reservation.operation_id,
                model_id="other-model",
            )
            report(reservation.operation_id, outcome, counts)
            """
        ))
        self._assert_untouched(payload)

    def test_matching_adoption_executes_once(self):
        payload = _payload(self, _run(
            """\
            repo, _clock = repository()
            reservation = prepare(repo, 6)
            outcome, counts = attempt(reservation.operation_id)
            report(reservation.operation_id, outcome, counts)
            """
        ))
        self.assertEqual(payload["result"]["type"], "DurableTurnCompleted")
        self.assertEqual(payload["counts"]["provider"], 1)
        self.assertEqual(payload["counts"]["claim"], 1)
        self.assertEqual(payload["counts"]["adopt"], 1)
        self.assertEqual(payload["counts"]["context"], 1)
        self.assertEqual(payload["counts"]["seen"], "persisted original")
        self.assertEqual(payload["state"]["status"], "SUCCEEDED")
        self.assertEqual(payload["state"]["messages"], [
            {"role": "user", "content": "persisted original"},
            {"role": "assistant", "content": "answer for persisted original"},
        ])

    def test_mismatched_retry_does_not_execute(self):
        payload = _payload(self, _run(
            """\
            repo, _clock = repository()
            source = prepare(repo, 7)
            failed = repo.fail_pre_provider(
                source,
                PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
            )
            if type(failed).__name__ != "Failed":
                raise RuntimeError(type(failed).__name__)
            created = repo.create_retry(RetryCommand(
                actor_id=OWNER.id,
                source_operation_id=source.operation_id,
                idempotency_key=key_for(8),
                expected_version=0,
            ))
            if type(created).__name__ != "RetryPrepared":
                raise RuntimeError(type(created).__name__)
            retry_id = created.reservation.operation_id
            outcome, counts = attempt(retry_id, request_content=DIFFERENT)
            report(retry_id, outcome, counts)
            """
        ))
        self._assert_untouched(payload)
        self.assertEqual(payload["state"]["messages"], [])

    def test_matching_retry_uses_persisted_request(self):
        payload = _payload(self, _run(
            """\
            repo, _clock = repository()
            source = prepare(repo, 9)
            failed = repo.fail_pre_provider(
                source,
                PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
            )
            if type(failed).__name__ != "Failed":
                raise RuntimeError(type(failed).__name__)
            created = repo.create_retry(RetryCommand(
                actor_id=OWNER.id,
                source_operation_id=source.operation_id,
                idempotency_key=key_for(10),
                expected_version=0,
            ))
            if type(created).__name__ != "RetryPrepared":
                raise RuntimeError(type(created).__name__)
            retry_id = created.reservation.operation_id
            outcome, counts = attempt(retry_id)
            report(retry_id, outcome, counts)
            """
        ))
        self.assertEqual(payload["result"]["type"], "DurableTurnCompleted")
        self.assertEqual(payload["counts"]["provider"], 1)
        self.assertEqual(payload["counts"]["claim"], 1)
        self.assertEqual(payload["counts"]["context"], 1)
        self.assertEqual(payload["counts"]["seen"], "persisted original")
        self.assertEqual(payload["state"]["messages"], [
            {"role": "user", "content": "persisted original"},
            {"role": "assistant", "content": "answer for persisted original"},
        ])

    def test_production_database_absent(self):
        self.assertFalse(PRODUCTION_DATABASE.exists())
