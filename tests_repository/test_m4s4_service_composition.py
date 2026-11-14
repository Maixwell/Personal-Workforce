"""Disposable composition tests for one durable conversation turn."""

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
COMPOSITION_SOURCE = SRC_ROOT / "durable_composition.py"

_PREAMBLE = """\
import base64
import json
import os
import subprocess
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
from context import build_standing_context
from durable_composition import (
    compose_disposable_service,
    open_disposable_service,
    turn_request,
)
from max_smart_db.config import EXECUTION_DEADLINE_US, FIXED_LEASE_US
from max_smart_db.models import Conversation, GlobalModelSlot, Message, Operation
from max_smart_db.repository import (
    PrepareCommand,
    PreProviderFailureCategory,
    RetryCommand,
)

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
NEW_RUNNER = "BBBBBBBBBBBBBBBBBBBBBQ"
PREPARED_AT = 1_000_000
ALLOWANCE = EXECUTION_DEADLINE_US - 120_000_000


class Clock:
    def __init__(self, value):
        self.value = value

    def now_us(self):
        return self.value


def key_for(value):
    return base64.urlsafe_b64encode(bytes([value]) * 16).rstrip(b"=").decode()


def write_context(root):
    docs = Path(root) / "docs"
    docs.mkdir(parents=True)
    (docs / "max-smart-instructions.md").write_text("INSTRUCTIONS-BODY\\n", encoding="utf-8")
    (docs / "goals.md").write_text("GOALS-BODY\\n", encoding="utf-8")
    (docs / "memory.md").write_text("MEMORY-BODY\\n", encoding="utf-8")
    return root


def capturing_popen(jobs, pops):
    def popen(*args, **kwargs):
        pops["count"] += 1
        proc = subprocess.Popen(*args, **kwargs)
        original = proc.stdin.write

        def write(payload):
            jobs.append(json.loads(payload.decode("utf-8").splitlines()[0]))
            return original(payload)

        proc.stdin.write = write
        return proc

    return popen


def facts(operation_id):
    operation = Operation.objects.get(pk=operation_id)
    conversation = Conversation.objects.get(pk=operation.conversation_id)
    slot = GlobalModelSlot.objects.get(pk=1)
    return {
        "status": operation.status,
        "owner_id": operation.owner_id,
        "content": operation.request_content,
        "failure": operation.failure_category,
        "blocks": operation.blocks_conversation,
        "started": operation.provider_started_at_us,
        "deadline": operation.execution_deadline_us,
        "lease": operation.lease_expires_at_us,
        "acknowledged": operation.acknowledged_at_us,
        "version": conversation.version,
        "slot_operation": None if slot.operation_id is None else str(slot.operation_id),
        "slot_lease": slot.lease_expires_at_us,
        "messages": [
            {"role": row.role, "content": row.content}
            for row in Message.objects.filter(operation_id=operation_id).order_by("sequence")
        ],
    }
"""


def _run(body, *, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "m4s4-composition.sqlite3"
            ),
            "PYTHONPATH": str(SRC_ROOT),
            "USER": "os-account",
            "LOGNAME": "os-account",
            "USERNAME": "os-account",
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


class ServiceCompositionTests(unittest.TestCase):
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

    def test_source_stays_outside_terminal_and_history(self):
        source = COMPOSITION_SOURCE.read_text()
        for banned in (
            "getpass",
            "getlogin",
            "getpwuid",
            "hostname",
            "os.environ",
            "getenv",
            "list_messages",
            "sample_clock_safety",
            "initialize_sqlite_file",
            "terminal",
            "max_smart.py",
        ):
            self.assertNotIn(banned, source)
        self.assertNotIn("durable_composition", (SRC_ROOT / "terminal.py").read_text())
        self.assertNotIn("durable_composition", (SRC_ROOT / "max_smart.py").read_text())
        self.assertNotIn(
            "durable_composition",
            (SRC_ROOT / "conversation_service.py").read_text(),
        )

    def test_complete_turn_uses_provisioned_owner_and_full_context(self):
        payload = _payload(self, _run(
            """\
            import context as context_module
            other = User.objects.create(username="not-the-service")
            refused = open_disposable_service(
                owner_id=other.pk,
                clock=Clock(PREPARED_AT),
                runner_id=RUNNER,
                fake="ok",
                load_context=lambda **_kwargs: "unused",
            )
            root = write_context(Path(os.environ["MAX_SMART_TEST_DATABASE_PATH"]).parent / "context")
            expected_context = build_standing_context(root)
            flags = {}
            real_builder = context_module.build_standing_context

            def watched_builder(path):
                flags["context_autocommit"] = connection.get_autocommit()
                flags["context_atomic"] = connection.in_atomic_block
                return real_builder(path)

            context_module.build_standing_context = watched_builder
            jobs = []
            pops = {"count": 0}

            def on_start(_pid):
                flags["provider_autocommit"] = connection.get_autocommit()
                flags["provider_atomic"] = connection.in_atomic_block

            composed = compose_disposable_service(
                clock=Clock(PREPARED_AT),
                runner_id=RUNNER,
                fake="ok",
                context_root=root,
                popen=capturing_popen(jobs, pops),
                on_worker_start=on_start,
            )
            duplicate = compose_disposable_service(
                clock=Clock(PREPARED_AT),
                runner_id=RUNNER,
                fake="ok",
                context_root=root,
            )
            created = composed.repository.create_conversation(
                composed.owner_id,
                "max-smart",
                None,
            )
            if type(created).__name__ != "ConversationView":
                raise RuntimeError(type(created).__name__)
            first = composed.service.submit(turn_request(
                actor_id=composed.owner_id,
                conversation_id=created.conversation_id,
                request_content="current request",
                idempotency_key=key_for(1),
                runner_id=RUNNER,
                expected_version=0,
            ))
            first_facts = facts(first.operation_id)
            second = composed.service.submit(turn_request(
                actor_id=composed.owner_id,
                conversation_id=created.conversation_id,
                request_content="next request",
                idempotency_key=key_for(2),
                runner_id=RUNNER,
                expected_version=1,
            ))
            print(json.dumps({
                "refused": type(refused).__name__,
                "refused_code": refused.code,
                "duplicate": duplicate.code,
                "owner_id": composed.owner_id,
                "owner_name": User.objects.get(pk=composed.owner_id).username,
                "service_users": User.objects.filter(username="max-smart-local").count(),
                "first_type": type(first).__name__,
                "workers": pops["count"],
                "flags": flags,
                "first_input": jobs[0]["request_content"],
                "second_input": jobs[1]["request_content"],
                "instructions_match": (
                    jobs[0]["standing_context"] == expected_context
                    and jobs[1]["standing_context"] == expected_context
                ),
                "history_absent": (
                    "current request" not in jobs[0]["standing_context"]
                    and "answer" not in jobs[1]["standing_context"]
                    and "current request" not in jobs[1]["request_content"]
                ),
                "first": first_facts,
                "second_type": type(second).__name__,
                "second": facts(second.operation_id),
            }))
            """
        ))
        self.assertEqual(payload["refused"], "CompositionRefused")
        self.assertEqual(payload["refused_code"], "owner_mismatch")
        self.assertEqual(payload["duplicate"], "duplicate_identity")
        self.assertNotEqual(payload["owner_id"], 1)
        self.assertEqual(payload["owner_name"], "max-smart-local")
        self.assertEqual(payload["service_users"], 1)
        self.assertEqual(payload["first_type"], "DurableTurnCompleted")
        self.assertEqual(payload["workers"], 2)
        self.assertIs(payload["flags"]["context_autocommit"], True)
        self.assertIs(payload["flags"]["context_atomic"], False)
        self.assertIs(payload["flags"]["provider_autocommit"], True)
        self.assertIs(payload["flags"]["provider_atomic"], False)
        self.assertEqual(payload["first_input"], "current request")
        self.assertEqual(payload["second_input"], "next request")
        self.assertTrue(payload["instructions_match"])
        self.assertTrue(payload["history_absent"])
        self.assertEqual(payload["first"]["owner_id"], payload["owner_id"])
        self.assertEqual(payload["first"]["content"], "current request")
        self.assertEqual(payload["first"]["status"], "SUCCEEDED")
        self.assertEqual(payload["first"]["version"], 1)
        self.assertIsNone(payload["first"]["slot_operation"])
        self.assertEqual(payload["first"]["messages"], [
            {"role": "user", "content": "current request"},
            {"role": "assistant", "content": "answer"},
        ])
        self.assertEqual(payload["second_type"], "DurableTurnCompleted")
        self.assertEqual(payload["second"]["version"], 2)
        self.assertEqual(payload["second"]["messages"], [
            {"role": "user", "content": "next request"},
            {"role": "assistant", "content": "answer"},
        ])

    def test_context_failures_do_not_call_the_provider(self):
        payload = _payload(self, _run(
            """\
            root = write_context(Path(os.environ["MAX_SMART_TEST_DATABASE_PATH"]).parent / "context")
            (Path(root) / "docs" / "memory.md").unlink()
            pops = {"count": 0}
            composed = compose_disposable_service(
                clock=Clock(PREPARED_AT),
                runner_id=RUNNER,
                fake="ok",
                context_root=root,
                popen=capturing_popen([], pops),
            )
            created = composed.repository.create_conversation(
                composed.owner_id,
                "max-smart",
                None,
            )
            missing = composed.service.submit(turn_request(
                actor_id=composed.owner_id,
                conversation_id=created.conversation_id,
                request_content="current request",
                idempotency_key=key_for(1),
                runner_id=RUNNER,
                expected_version=0,
            ))
            malformed_root = composed.repository.create_conversation(
                composed.owner_id,
                "max-smart",
                None,
            )
            malformed = open_disposable_service(
                owner_id=composed.owner_id,
                clock=Clock(PREPARED_AT),
                runner_id=RUNNER,
                fake="ok",
                load_context=lambda **_kwargs: None,
                popen=capturing_popen([], pops),
            )
            malformed_result = malformed.service.submit(turn_request(
                actor_id=composed.owner_id,
                conversation_id=malformed_root.conversation_id,
                request_content="current request",
                idempotency_key=key_for(2),
                runner_id=RUNNER,
                expected_version=0,
            ))
            print(json.dumps({
                "missing": type(missing).__name__,
                "missing_category": missing.failure_category.value,
                "missing_facts": facts(missing.operation_id),
                "malformed": type(malformed_result).__name__,
                "malformed_category": malformed_result.failure_category.value,
                "malformed_facts": facts(malformed_result.operation_id),
                "workers": pops["count"],
            }))
            """
        ))
        self.assertEqual(payload["missing"], "DurablePreProviderFailure")
        self.assertEqual(payload["missing_category"], "context_unavailable")
        self.assertEqual(payload["missing_facts"]["status"], "FAILED")
        self.assertEqual(payload["missing_facts"]["messages"], [])
        self.assertIsNone(payload["missing_facts"]["started"])
        self.assertEqual(payload["malformed"], "DurablePreProviderFailure")
        self.assertEqual(payload["malformed_category"], "context_unavailable")
        self.assertEqual(payload["malformed_facts"]["messages"], [])
        self.assertEqual(payload["workers"], 0)

    def test_killed_worker_preserves_uncertain_running(self):
        payload = _payload(self, _run(
            """\
            import socket
            import threading
            jobs = []
            pops = {"count": 0}
            server = socket.socket()
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            held = []

            def accept_and_hold():
                accepted, _address = server.accept()
                held.append(accepted)

            threading.Thread(target=accept_and_hold, daemon=True).start()
            composed = compose_disposable_service(
                clock=Clock(PREPARED_AT),
                runner_id=RUNNER,
                fake="block_read",
                load_context=lambda **_kwargs: "STANDING-ONLY",
                wall_clock=lambda: PREPARED_AT + ALLOWANCE - 400_000,
                socket_port=server.getsockname()[1],
                popen=capturing_popen(jobs, pops),
            )
            created = composed.repository.create_conversation(
                composed.owner_id,
                "max-smart",
                None,
            )
            request = turn_request(
                actor_id=composed.owner_id,
                conversation_id=created.conversation_id,
                request_content="current request",
                idempotency_key=key_for(1),
                runner_id=RUNNER,
                expected_version=0,
            )
            try:
                outcome = composed.service.submit(request)
                again = composed.service.submit(request)
            finally:
                server.close()
                for accepted in held:
                    accepted.close()
            stored = facts(outcome.operation.operation_id)
            print(json.dumps({
                "type": type(outcome).__name__,
                "again": type(again).__name__,
                "workers": pops["count"],
                "retries": Operation.objects.exclude(retry_of_id=None).count(),
                "operation_id": str(outcome.operation.operation_id),
                "facts": stored,
                "deadline": PREPARED_AT + EXECUTION_DEADLINE_US,
                "lease": PREPARED_AT + FIXED_LEASE_US,
            }))
            """
        ))
        self.assertEqual(payload["type"], "UncertainRecorded")
        self.assertEqual(payload["again"], "Existing")
        self.assertEqual(payload["workers"], 1)
        self.assertEqual(payload["retries"], 0)
        self.assertEqual(payload["facts"]["status"], "RUNNING")
        self.assertEqual(payload["facts"]["blocks"], 1)
        self.assertEqual(payload["facts"]["failure"], "provider_outcome_uncertain")
        self.assertIsNone(payload["facts"]["acknowledged"])
        self.assertEqual(payload["facts"]["deadline"], payload["deadline"])
        self.assertEqual(payload["facts"]["lease"], payload["lease"])
        self.assertEqual(payload["facts"]["slot_lease"], payload["lease"])
        self.assertEqual(payload["facts"]["messages"], [])
        self.assertEqual(payload["facts"]["slot_operation"], payload["operation_id"])

    def test_ambiguous_claim_and_apply_dispatch_once(self):
        payload = _payload(self, _run(
            """\
            pops = {"count": 0}
            composed = compose_disposable_service(
                clock=Clock(PREPARED_AT),
                runner_id=RUNNER,
                fake="ok",
                load_context=lambda **_kwargs: "STANDING-ONLY",
                popen=capturing_popen([], pops),
            )
            real_apply = composed.repository.apply_success
            writes = {"count": 0}

            def apply_success(*args, **kwargs):
                writes["count"] += 1
                real_commit = connection.commit

                def commit(*commit_args, **commit_kwargs):
                    real_commit(*commit_args, **commit_kwargs)
                    raise OperationalError("simulated post-commit failure")

                connection.commit = commit
                try:
                    return real_apply(*args, **kwargs)
                finally:
                    connection.commit = real_commit

            composed.repository.apply_success = apply_success
            created = composed.repository.create_conversation(
                composed.owner_id,
                "max-smart",
                None,
            )
            applied = composed.service.submit(turn_request(
                actor_id=composed.owner_id,
                conversation_id=created.conversation_id,
                request_content="current request",
                idempotency_key=key_for(1),
                runner_id=RUNNER,
                expected_version=0,
            ))
            apply_workers = pops["count"]
            apply_writes = writes["count"]
            apply_facts = facts(applied.operation_id)

            claim_created = composed.repository.create_conversation(
                composed.owner_id,
                "max-smart",
                None,
            )
            real_claim = composed.repository.claim_running

            def claim_running(*args, **kwargs):
                real_commit = connection.commit

                def commit(*commit_args, **commit_kwargs):
                    real_commit(*commit_args, **commit_kwargs)
                    raise OperationalError("simulated post-commit failure")

                connection.commit = commit
                try:
                    return real_claim(*args, **kwargs)
                finally:
                    connection.commit = real_commit

            composed.repository.claim_running = claim_running
            claimed = composed.service.submit(turn_request(
                actor_id=composed.owner_id,
                conversation_id=claim_created.conversation_id,
                request_content="current request",
                idempotency_key=key_for(2),
                runner_id=RUNNER,
                expected_version=0,
            ))
            print(json.dumps({
                "apply_type": type(applied).__name__,
                "apply_workers": apply_workers,
                "apply_writes": apply_writes,
                "apply_facts": apply_facts,
                "claim_type": type(claimed).__name__,
                "claim_workers": pops["count"],
                "claim_facts": facts(claimed.operation_id),
            }))
            """
        ))
        self.assertEqual(payload["apply_type"], "DurableTurnCompleted")
        self.assertEqual(payload["apply_workers"], 1)
        self.assertEqual(payload["apply_writes"], 1)
        self.assertEqual(payload["apply_facts"]["status"], "SUCCEEDED")
        self.assertEqual(len(payload["apply_facts"]["messages"]), 2)
        self.assertEqual(payload["claim_type"], "DurableTurnCompleted")
        self.assertEqual(payload["claim_workers"], 2)
        self.assertEqual(payload["claim_facts"]["status"], "SUCCEEDED")
        self.assertEqual(len(payload["claim_facts"]["messages"]), 2)

    def test_restart_after_claim_does_not_replay_or_recover(self):
        payload = _payload(self, _run(
            """\
            pops = {"count": 0}
            composed = compose_disposable_service(
                clock=Clock(PREPARED_AT),
                runner_id=RUNNER,
                fake="ok",
                load_context=lambda **_kwargs: "STANDING-ONLY",
                popen=capturing_popen([], pops),
            )
            created = composed.repository.create_conversation(
                composed.owner_id,
                "max-smart",
                None,
            )
            prepared = composed.repository.prepare(PrepareCommand(
                actor_id=composed.owner_id,
                conversation_id=created.conversation_id,
                expected_version=0,
                request_content="current request",
                idempotency_key=key_for(1),
                provider_id="openai",
                model_id="gpt-5.6-sol",
            ))
            claimed = composed.repository.claim_running(
                composed.owner_id,
                prepared.reservation.operation_id,
            )
            restarted = open_disposable_service(
                owner_id=composed.owner_id,
                clock=Clock(PREPARED_AT),
                runner_id=NEW_RUNNER,
                fake="ok",
                load_context=lambda **_kwargs: "STANDING-ONLY",
                popen=capturing_popen([], pops),
            )
            classified = restarted.service.classify_restart(
                composed.owner_id,
                prepared.reservation.operation_id,
            )
            replay = restarted.service.submit(turn_request(
                actor_id=composed.owner_id,
                conversation_id=created.conversation_id,
                request_content="current request",
                idempotency_key=key_for(1),
                runner_id=NEW_RUNNER,
                expected_version=0,
            ))
            recovered = restarted.service.recover()
            print(json.dumps({
                "claimed": type(claimed).__name__,
                "classified": type(classified).__name__,
                "status": classified.operation.status.value,
                "replay": type(replay).__name__,
                "recovered": type(recovered).__name__,
                "workers": pops["count"],
                "facts": facts(prepared.reservation.operation_id),
            }))
            """
        ))
        self.assertEqual(payload["claimed"], "Claimed")
        self.assertEqual(payload["classified"], "RestartRunning")
        self.assertEqual(payload["status"], "RUNNING")
        self.assertEqual(payload["replay"], "Existing")
        self.assertEqual(payload["recovered"], "LiveRunning")
        self.assertEqual(payload["workers"], 0)
        self.assertEqual(payload["facts"]["status"], "RUNNING")
        self.assertEqual(payload["facts"]["messages"], [])
        self.assertEqual(payload["facts"]["blocks"], 1)

    def test_explicit_recovery_is_not_part_of_submit(self):
        payload = _payload(self, _run(
            """\
            clock = Clock(PREPARED_AT)
            pops = {"count": 0}
            composed = compose_disposable_service(
                clock=clock,
                runner_id=RUNNER,
                fake="ok",
                load_context=lambda **_kwargs: "STANDING-ONLY",
                popen=capturing_popen([], pops),
            )
            created = composed.repository.create_conversation(
                composed.owner_id,
                "max-smart",
                None,
            )
            prepared = composed.repository.prepare(PrepareCommand(
                actor_id=composed.owner_id,
                conversation_id=created.conversation_id,
                expected_version=0,
                request_content="current request",
                idempotency_key=key_for(1),
                provider_id="openai",
                model_id="gpt-5.6-sol",
            ))
            clock.value = prepared.reservation.reservation_expires_at
            recovered = composed.service.recover()
            print(json.dumps({
                "type": type(recovered).__name__,
                "workers": pops["count"],
                "facts": facts(prepared.reservation.operation_id),
            }))
            """
        ))
        self.assertEqual(payload["type"], "CancelledExpiredQueued")
        self.assertEqual(payload["workers"], 0)
        self.assertEqual(payload["facts"]["status"], "CANCELLED")
        self.assertIsNone(payload["facts"]["slot_operation"])

    def test_mismatched_adopted_and_retry_requests_do_not_claim(self):
        payload = _payload(self, _run(
            """\
            pops = {"count": 0}
            claims = {"count": 0}
            composed = compose_disposable_service(
                clock=Clock(PREPARED_AT),
                runner_id=RUNNER,
                fake="ok",
                load_context=lambda **_kwargs: "STANDING-ONLY",
                popen=capturing_popen([], pops),
            )
            real_claim = composed.repository.claim_running

            def claim_running(*args, **kwargs):
                claims["count"] += 1
                return real_claim(*args, **kwargs)

            composed.repository.claim_running = claim_running
            created = composed.repository.create_conversation(
                composed.owner_id,
                "max-smart",
                None,
            )
            prepared = composed.repository.prepare(PrepareCommand(
                actor_id=composed.owner_id,
                conversation_id=created.conversation_id,
                expected_version=0,
                request_content="persisted original",
                idempotency_key=key_for(1),
                provider_id="openai",
                model_id="gpt-5.6-sol",
            ))
            adopted = composed.service.submit(turn_request(
                actor_id=composed.owner_id,
                conversation_id=created.conversation_id,
                request_content="different request",
                idempotency_key=key_for(2),
                runner_id=RUNNER,
                expected_version=0,
                adopt_operation_id=prepared.reservation.operation_id,
            ))
            failed = composed.repository.fail_pre_provider(
                prepared.reservation,
                PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
            )
            created_retry = composed.repository.create_retry(RetryCommand(
                actor_id=composed.owner_id,
                source_operation_id=prepared.reservation.operation_id,
                idempotency_key=key_for(3),
                expected_version=0,
            ))
            retried = composed.service.submit(turn_request(
                actor_id=composed.owner_id,
                conversation_id=created.conversation_id,
                request_content="different request",
                idempotency_key=key_for(4),
                runner_id=RUNNER,
                expected_version=0,
                adopt_operation_id=created_retry.reservation.operation_id,
            ))
            print(json.dumps({
                "failed": type(failed).__name__,
                "retry": type(created_retry).__name__,
                "adopted": type(adopted).__name__,
                "retried": type(retried).__name__,
                "claims": claims["count"],
                "workers": pops["count"],
                "queued": facts(prepared.reservation.operation_id),
                "retry_facts": facts(created_retry.reservation.operation_id),
            }))
            """
        ))
        self.assertEqual(payload["failed"], "Failed")
        self.assertEqual(payload["retry"], "RetryPrepared")
        self.assertEqual(payload["adopted"], "ExecutionIdentityMismatch")
        self.assertEqual(payload["retried"], "ExecutionIdentityMismatch")
        self.assertEqual(payload["claims"], 0)
        self.assertEqual(payload["workers"], 0)
        self.assertEqual(payload["queued"]["status"], "FAILED")
        self.assertEqual(payload["queued"]["content"], "persisted original")
        self.assertEqual(payload["retry_facts"]["status"], "QUEUED")
        self.assertEqual(payload["retry_facts"]["content"], "persisted original")
        self.assertEqual(payload["retry_facts"]["messages"], [])

    def test_held_transaction_skips_context_and_provider(self):
        payload = _payload(self, _run(
            """\
            pops = {"count": 0}
            calls = {"context": 0}
            composed = compose_disposable_service(
                clock=Clock(PREPARED_AT),
                runner_id=RUNNER,
                fake="ok",
                load_context=lambda **_kwargs: calls.__setitem__("context", calls["context"] + 1),
                popen=capturing_popen([], pops),
            )
            with transaction.atomic():
                held = composed.service.submit(turn_request(
                    actor_id=composed.owner_id,
                    conversation_id=uuid.uuid4(),
                    request_content="current request",
                    idempotency_key=key_for(1),
                    runner_id=RUNNER,
                    expected_version=0,
                ))
            print(json.dumps({
                "type": type(held).__name__,
                "workers": pops["count"],
                "context": calls["context"],
                "operations": Operation.objects.count(),
            }))
            """
        ))
        self.assertEqual(payload["type"], "TransactionHeld")
        self.assertEqual(payload["workers"], 0)
        self.assertEqual(payload["context"], 0)
        self.assertEqual(payload["operations"], 0)

    def test_production_settings_do_not_open_the_database(self):
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.settings",
            "PYTHONPATH": str(SRC_ROOT),
        }
        environment.pop("MAX_SMART_TEST_DATABASE_PATH", None)
        result = subprocess.run(
            (
                sys.executable,
                "-B",
                "-c",
                textwrap.dedent(
                    """\
                    import django
                    django.setup()
                    from durable_composition import compose_disposable_service
                    refused = compose_disposable_service(
                        clock=None,
                        runner_id="AAAAAAAAAAAAAAAAAAAAAA",
                        fake="ok",
                        load_context=lambda **_kwargs: "unused",
                    )
                    print(refused.code)
                    """
                ),
            ),
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
        self.assertEqual(result.stdout.strip(), "production_database")
        self.assertFalse(PRODUCTION_DATABASE.exists())


if __name__ == "__main__":
    unittest.main()
