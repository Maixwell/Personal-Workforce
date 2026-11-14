"""Disposable production-readiness rehearsal. It does not activate production."""

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

_PREAMBLE = """\
import base64
import json
import os
import subprocess
import threading
import uuid
from pathlib import Path

database = Path(os.environ["MAX_SMART_TEST_DATABASE_PATH"])
if database.name == "max_smart.sqlite3":
    raise SystemExit("production database requested")
from max_smart_db.safety import initialize_sqlite_file, verify_sqlite_module_version
initialize_sqlite_file(database)

import django
django.setup()
from django.contrib.auth.models import User
from django.db import OperationalError, connection
from django.db.migrations.executor import MigrationExecutor
from context import build_standing_context
from durable_composition import (
    compose_disposable_service,
    open_disposable_service,
    turn_request,
)
from max_smart_db.clock_safety import (
    ClockSampleRejected,
    evaluate_clock_sample,
)
from max_smart_db.config import (
    EXECUTION_DEADLINE_US,
    FIXED_LEASE_US,
    RECOVERY_CLOCK_SKEW_MARGIN_US,
)
from max_smart_db.models import Conversation, GlobalModelSlot, Message, Operation
from max_smart_db.repository import PrepareCommand, PreProviderFailureCategory, RetainedResult
from operator_commands import OperatorCommands

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
PREPARED_AT = 1_000_000
ALLOWANCE = EXECUTION_DEADLINE_US - RECOVERY_CLOCK_SKEW_MARGIN_US


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
        "acknowledged": operation.acknowledged_at_us is not None,
        "version": conversation.version,
        "slot_operation": None if slot.operation_id is None else str(slot.operation_id),
        "messages": [
            {"role": row.role, "content": row.content}
            for row in Message.objects.filter(operation_id=operation_id).order_by("sequence")
        ],
    }


def compose(clock, fake="ok", **options):
    root = options.get("context_root")
    if "load_context" not in options and root is None:
        root = write_context(
            Path(os.environ["MAX_SMART_TEST_DATABASE_PATH"]).parent / "context"
        )
        options["context_root"] = root
    existing = User.objects.filter(username="max-smart-local").first()
    if existing is None:
        composed = compose_disposable_service(
            clock=clock,
            runner_id=RUNNER,
            fake=fake,
            **options,
        )
    else:
        composed = open_disposable_service(
            owner_id=existing.pk,
            clock=clock,
            runner_id=RUNNER,
            fake=fake,
            **options,
        )
    if type(composed).__name__ != "DisposableComposition":
        raise RuntimeError(type(composed).__name__)
    return composed, root


def conversation(composed):
    created = composed.repository.create_conversation(
        composed.owner_id,
        "max-smart",
        None,
    )
    if type(created).__name__ != "ConversationView":
        raise RuntimeError(type(created).__name__)
    return created.conversation_id


def prepare(composed, conversation_id, key, content="persisted original"):
    prepared = composed.repository.prepare(PrepareCommand(
        actor_id=composed.owner_id,
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


def commit_once(repository, name, durable, writes):
    real = getattr(repository, name)

    def wrapped(*args, **kwargs):
        writes["count"] += 1
        real_commit = connection.commit

        def commit(*commit_args, **commit_kwargs):
            if durable:
                real_commit(*commit_args, **commit_kwargs)
            raise OperationalError("simulated rehearsal commit")

        connection.commit = commit
        try:
            return real(*args, **kwargs)
        finally:
            connection.commit = real_commit

    setattr(repository, name, wrapped)
    return real
"""


def _run(body, *, timeout=90):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "m4s6-rehearsal.sqlite3"
            ),
            "PYTHONPATH": str(SRC_ROOT),
            "USER": "os-account",
            "LOGNAME": "os-account",
            "USERNAME": "os-account",
        }
        database = Path(temporary) / "m4s6-rehearsal.sqlite3"
        result = subprocess.run(
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
        leftover = [
            path.name
            for path in Path(temporary).iterdir()
            if path.name.startswith("m4s6-rehearsal.sqlite3")
        ]
        result.rehearsal_artifacts = leftover
        result.rehearsal_database = database
        return result


def _payload(testcase, result):
    testcase.assertEqual(
        result.returncode,
        0,
        result.stdout + "\n" + result.stderr,
    )
    testcase.assertFalse(
        result.rehearsal_database.is_relative_to(PROJECT_ROOT / "data")
    )
    return json.loads(result.stdout)


class ReadinessRehearsalTests(unittest.TestCase):
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

    def test_rehearsal_sources_stay_outside_terminal_and_production(self):
        composition = (SRC_ROOT / "durable_composition.py").read_text()
        operator = (SRC_ROOT / "operator_commands.py").read_text()
        terminal = (SRC_ROOT / "terminal.py").read_text()
        legacy = (SRC_ROOT / "max_smart.py").read_text()
        self.assertNotIn("sample_clock_safety", composition)
        self.assertNotIn("initialize_sqlite_file", composition)
        self.assertNotIn("verify_ordinary_startup", composition)
        self.assertNotIn("verify_fresh_bootstrap", composition)
        for banned in (
            "submit",
            "claim_running",
            "supervise",
            "provider_worker",
            "build_service_provider",
            "delete_expired_idempotency",
        ):
            self.assertNotIn(banned, operator)
        self.assertNotIn("operator_commands", terminal)
        self.assertNotIn("durable_composition", terminal)
        self.assertNotIn("operator_commands", legacy)
        self.assertNotIn("durable_composition", legacy)
        self.assertIn("current_session", (SRC_ROOT / "session.py").read_text())

    def test_integrated_turn_claims_before_the_fake_provider(self):
        payload = _payload(self, _run(
            """\
            clock = Clock(PREPARED_AT)
            jobs = []
            pops = {"count": 0}
            import time
            walls = {"count": 0}
            flags = {}

            def wall():
                walls["count"] += 1
                return clock.value

            def before_popen():
                operation = Operation.objects.get(owner_id=composed.owner_id)
                proof = composed.repository.confirm_durable_claim(
                    composed.owner_id,
                    operation.id,
                    RUNNER,
                    operation.provider_started_at_us,
                )
                flags["proof"] = type(proof).__name__
                flags["status"] = operation.status
                flags["autocommit"] = connection.get_autocommit()
                flags["atomic"] = connection.in_atomic_block
                flags["workers_before_start"] = pops["count"]
                flags["monotonic_ns"] = time.monotonic_ns()

            composed, root = compose(
                clock,
                popen=capturing_popen(jobs, pops),
                wall_clock=wall,
                before_popen=before_popen,
            )
            expected = build_standing_context(root)
            user = User.objects.get(pk=composed.owner_id)
            created = conversation(composed)
            completed = composed.service.submit(turn_request(
                actor_id=composed.owner_id,
                conversation_id=created,
                request_content="current request",
                idempotency_key=key_for(1),
                runner_id=RUNNER,
                expected_version=0,
            ))
            observed = facts(completed.operation_id)
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA journal_mode")
                journal = str(cursor.fetchone()[0]).lower()
            print(json.dumps({
                "type": type(completed).__name__,
                "owner_id": composed.owner_id,
                "username": user.username,
                "usable_password": user.has_usable_password(),
                "staff": user.is_staff,
                "superuser": user.is_superuser,
                "groups": user.groups.count(),
                "permissions": user.user_permissions.count(),
                "os_username": os.environ["USER"],
                "workers": pops["count"],
                "wall_calls": walls["count"],
                "flags": flags,
                "deadline_ns": jobs[0]["deadline_monotonic_ns"],
                "mode": jobs[0]["mode"],
                "api_key_empty": jobs[0]["api_key"] == "",
                "request": jobs[0]["request_content"],
                "context_exact": jobs[0]["standing_context"] == expected,
                "history_absent": "current request" not in jobs[0]["standing_context"],
                "facts": observed,
                "journal": journal,
                "sqlite": list(verify_sqlite_module_version()),
                "messages": Message.objects.filter(
                    conversation_id=created
                ).count(),
            }))
            """
        ))
        self.assertEqual(payload["type"], "DurableTurnCompleted")
        self.assertIsInstance(payload["owner_id"], int)
        self.assertGreaterEqual(payload["owner_id"], 1)
        self.assertEqual(payload["username"], "max-smart-local")
        self.assertFalse(payload["usable_password"])
        self.assertFalse(payload["staff"])
        self.assertFalse(payload["superuser"])
        self.assertEqual(payload["groups"], 0)
        self.assertEqual(payload["permissions"], 0)
        self.assertEqual(payload["os_username"], "os-account")
        self.assertNotEqual(payload["username"], payload["os_username"])
        self.assertEqual(payload["workers"], 1)
        self.assertEqual(payload["wall_calls"], 1)
        self.assertEqual(payload["flags"]["proof"], "DurableClaimConfirmed")
        self.assertEqual(payload["flags"]["status"], "RUNNING")
        self.assertIs(payload["flags"]["autocommit"], True)
        self.assertIs(payload["flags"]["atomic"], False)
        self.assertEqual(payload["flags"]["workers_before_start"], 0)
        remaining_ns = payload["deadline_ns"] - payload["flags"]["monotonic_ns"]
        self.assertGreater(remaining_ns, 0)
        self.assertLess(remaining_ns, (600_000_000 - 120_000_000) * 1_000)
        self.assertEqual(payload["mode"], "fake")
        self.assertTrue(payload["api_key_empty"])
        self.assertEqual(payload["request"], "current request")
        self.assertTrue(payload["context_exact"])
        self.assertTrue(payload["history_absent"])
        self.assertEqual(payload["facts"]["owner_id"], payload["owner_id"])
        self.assertEqual(payload["facts"]["content"], "current request")
        self.assertEqual(payload["facts"]["status"], "SUCCEEDED")
        self.assertEqual(payload["facts"]["blocks"], 0)
        self.assertIsNone(payload["facts"]["slot_operation"])
        self.assertEqual(payload["facts"]["version"], 1)
        self.assertEqual(payload["facts"]["deadline"], PREPARED := 1_000_000 + 600_000_000)
        self.assertEqual(payload["facts"]["lease"], 1_000_000 + 720_000_000)
        self.assertEqual(payload["facts"]["messages"], [
            {"role": "user", "content": "current request"},
            {"role": "assistant", "content": "answer"},
        ])
        self.assertEqual(payload["messages"], 2)
        self.assertEqual(payload["journal"], "wal")
        self.assertGreaterEqual(tuple(payload["sqlite"]), (3, 33, 0))

    def test_context_failure_and_uncertain_worker_do_not_invent_success(self):
        payload = _payload(self, _run(
            """\
            root = write_context(Path(os.environ["MAX_SMART_TEST_DATABASE_PATH"]).parent / "context")
            (Path(root) / "docs" / "memory.md").unlink()
            pops = {"count": 0}
            clock = Clock(PREPARED_AT)
            missing_service = compose_disposable_service(
                clock=clock,
                runner_id=RUNNER,
                fake="ok",
                context_root=root,
                popen=capturing_popen([], pops),
            )
            created = conversation(missing_service)
            after_missing = pops["count"]
            missing = missing_service.service.submit(turn_request(
                actor_id=missing_service.owner_id,
                conversation_id=created,
                request_content="current request",
                idempotency_key=key_for(1),
                runner_id=RUNNER,
                expected_version=0,
            ))
            clock.value = PREPARED_AT
            uncertain_service, _root = compose(
                clock,
                fake="crash",
                popen=capturing_popen([], pops),
                load_context=lambda **_kwargs: "standing",
            )
            uncertain_conversation = conversation(uncertain_service)
            uncertain = uncertain_service.service.submit(turn_request(
                actor_id=uncertain_service.owner_id,
                conversation_id=uncertain_conversation,
                request_content="current request",
                idempotency_key=key_for(2),
                runner_id=RUNNER,
                expected_version=0,
            ))
            restarted = OperatorCommands(type(uncertain_service.service)(
                uncertain_service.repository,
                lambda **_kwargs: "standing",
                lambda **_kwargs: pops.__setitem__("restart_provider", pops.get("restart_provider", 0) + 1),
            ))
            classified = restarted.classify(
                uncertain_service.owner_id,
                uncertain.operation.operation_id,
            )
            print(json.dumps({
                "missing": type(missing).__name__,
                "missing_category": missing.failure_category.value,
                "missing_facts": facts(missing.operation_id),
                "workers_after_missing": after_missing,
                "uncertain": type(uncertain).__name__,
                "uncertain_facts": facts(uncertain.operation.operation_id),
                "restart": type(classified).__name__,
                "restart_status": classified.operation.status.value,
                "restart_provider": pops.get("restart_provider", 0),
                "retained": len(restarted._service._retained),
            }))
            """
        ))
        self.assertEqual(payload["missing"], "DurablePreProviderFailure")
        self.assertEqual(payload["missing_category"], "context_unavailable")
        self.assertEqual(payload["missing_facts"]["status"], "FAILED")
        self.assertEqual(payload["missing_facts"]["messages"], [])
        self.assertEqual(payload["workers_after_missing"], 0)
        self.assertEqual(payload["uncertain"], "UncertainRecorded")
        self.assertEqual(payload["uncertain_facts"]["status"], "RUNNING")
        self.assertEqual(payload["uncertain_facts"]["blocks"], 1)
        self.assertEqual(payload["uncertain_facts"]["failure"], "provider_outcome_uncertain")
        self.assertIsNotNone(payload["uncertain_facts"]["slot_operation"])
        self.assertEqual(payload["restart"], "RestartRunning")
        self.assertEqual(payload["restart_status"], "RUNNING")
        self.assertEqual(payload["restart_provider"], 0)
        self.assertEqual(payload["retained"], 0)

    def test_ambiguous_claim_and_apply_are_proven_without_another_provider(self):
        payload = _payload(self, _run(
            """\
            keys = {"next": 1}

            def once(durable, method):
                key = keys["next"]
                keys["next"] += 1
                clock = Clock(PREPARED_AT)
                pops = {"count": 0}
                composed, _root = compose(
                    clock,
                    popen=capturing_popen([], pops),
                    load_context=lambda **_kwargs: "standing",
                )
                created = conversation(composed)
                writes = {"count": 0}
                real = commit_once(composed.repository, method, durable, writes)
                if method == "apply_success":
                    queued = prepare(composed, created, key)
                    claimed = composed.repository.claim_running(
                        composed.owner_id,
                        queued.operation_id,
                    )
                    composed.service._claims[(
                        queued.operation_id,
                        RUNNER,
                        claimed.claimed_operation.claimed_at,
                    )] = claimed.claimed_operation
                    composed.service._dispatched.add((
                        queued.operation_id,
                        RUNNER,
                        claimed.claimed_operation.claimed_at,
                    ))
                    outcome = composed.service._persist(
                        claimed.claimed_operation,
                        ("success", "answer"),
                    )
                    operation_id = queued.operation_id
                else:
                    outcome = composed.service.submit(turn_request(
                        actor_id=composed.owner_id,
                        conversation_id=created,
                        request_content="current request",
                        idempotency_key=key_for(key),
                        runner_id=RUNNER,
                        expected_version=0,
                    ))
                    operation_id = Operation.objects.get(conversation_id=created).id
                setattr(composed.repository, method, real)
                observed = facts(operation_id)
                if observed["status"] == "QUEUED":
                    clock.value = Operation.objects.get(
                        pk=operation_id
                    ).reservation_expires_at_us
                    composed.repository.recover_slot()
                return {
                    "type": type(outcome).__name__,
                    "writes": writes["count"],
                    "workers": pops["count"],
                    "facts": observed,
                }

            print(json.dumps({
                "claim_durable": once(True, "claim_running"),
                "claim_rolled": once(False, "claim_running"),
                "apply_durable": once(True, "apply_success"),
                "apply_rolled": once(False, "apply_success"),
            }))
            """
        ))
        self.assertEqual(payload["claim_durable"]["type"], "DurableTurnCompleted")
        self.assertEqual(payload["claim_durable"]["writes"], 1)
        self.assertEqual(payload["claim_durable"]["workers"], 1)
        self.assertEqual(payload["claim_durable"]["facts"]["status"], "SUCCEEDED")
        self.assertEqual(payload["claim_rolled"]["writes"], 1)
        self.assertEqual(payload["claim_rolled"]["workers"], 0)
        self.assertEqual(payload["claim_rolled"]["facts"]["status"], "QUEUED")
        self.assertEqual(payload["apply_durable"]["type"], "DurableTurnCompleted")
        self.assertEqual(payload["apply_durable"]["writes"], 1)
        self.assertEqual(payload["apply_durable"]["workers"], 0)
        self.assertEqual(payload["apply_durable"]["facts"]["messages"], [
            {"role": "user", "content": "persisted original"},
            {"role": "assistant", "content": "answer"},
        ])
        self.assertEqual(payload["apply_rolled"]["type"], "OutcomeUnconfirmed")
        self.assertEqual(payload["apply_rolled"]["writes"], 2)
        self.assertEqual(payload["apply_rolled"]["workers"], 0)
        self.assertEqual(payload["apply_rolled"]["facts"]["status"], "RUNNING")
        self.assertEqual(payload["apply_rolled"]["facts"]["messages"], [])

    def test_explicit_recovery_acknowledgement_and_retry_make_no_provider_calls(self):
        payload = _payload(self, _run(
            """\
            clock = Clock(PREPARED_AT)
            pops = {"count": 0}
            composed, _root = compose(
                clock,
                popen=capturing_popen([], pops),
                load_context=lambda **_kwargs: "standing",
                context_root=None,
            )
            operator = OperatorCommands(composed.service)
            before = Operation.objects.count()
            queued_id = conversation(composed)
            queued = prepare(composed, queued_id, 1)
            queued_class = operator.classify(composed.owner_id, queued.operation_id)
            clock.value = queued.reservation_expires_at
            still_queued = operator.classify(composed.owner_id, queued.operation_id)
            cancelled = operator.recover()
            cancelled_retry = operator.create_retry(
                composed.owner_id,
                queued.operation_id,
                key_for(11),
                0,
            )
            clock.value = cancelled_retry.reservation.reservation_expires_at
            operator.recover()
            clock.value = PREPARED_AT

            running_id = conversation(composed)
            running = prepare(composed, running_id, 2)
            claimed = composed.repository.claim_running(
                composed.owner_id,
                running.operation_id,
            )
            token = claimed.claimed_operation
            clock.value = token.execution_deadline
            at_deadline = operator.recover()
            clock.value = token.lease_expires_at
            unknown = operator.recover()
            blocking = operator.classify(composed.owner_id, running.operation_id)
            writes = {"count": 0}
            real_acknowledge = commit_once(
                composed.repository,
                "acknowledge_unknown",
                True,
                writes,
            )
            acknowledged = operator.acknowledge(
                composed.owner_id,
                running.operation_id,
            )
            composed.repository.acknowledge_unknown = real_acknowledge
            late = composed.repository.reconcile_late_success(
                RetainedResult(token, "kept answer")
            )
            failed_id = conversation(composed)
            failed = prepare(composed, failed_id, 3)
            composed.repository.fail_pre_provider(
                failed,
                PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
            )
            failed_retry = operator.create_retry(
                composed.owner_id,
                failed.operation_id,
                key_for(13),
                0,
            )
            clock.value = failed_retry.reservation.reservation_expires_at
            operator.recover()
            clock.value = PREPARED_AT
            warned = operator.create_retry(
                composed.owner_id,
                running.operation_id,
                key_for(12),
                0,
            )
            print(json.dumps({
                "provider": pops["count"],
                "operations_before_commands": before,
                "queued_class": type(queued_class).__name__,
                "still_queued": type(still_queued).__name__,
                "cancelled": type(cancelled).__name__,
                "cancelled_facts": facts(queued.operation_id),
                "cancelled_retry": type(cancelled_retry).__name__,
                "warning": cancelled_retry.duplicate_charge_warning,
                "at_deadline": type(at_deadline).__name__,
                "unknown": type(unknown).__name__,
                "blocking": type(blocking).__name__,
                "acknowledged": type(acknowledged).__name__,
                "writes": writes["count"],
                "ack_facts": facts(running.operation_id),
                "late": type(late).__name__,
                "warned": type(warned).__name__,
                "warned_flag": warned.duplicate_charge_warning,
                "warned_text": facts(warned.reservation.operation_id)["content"],
                "failed_retry": type(failed_retry).__name__,
                "failed_flag": failed_retry.duplicate_charge_warning,
                "failed_text": facts(failed_retry.reservation.operation_id)["content"],
            }))
            """
        ))
        self.assertEqual(payload["provider"], 0)
        self.assertEqual(payload["operations_before_commands"], 0)
        self.assertEqual(payload["queued_class"], "RestartQueued")
        self.assertEqual(payload["still_queued"], "RestartQueued")
        self.assertEqual(payload["cancelled"], "CancelledExpiredQueued")
        self.assertEqual(payload["cancelled_facts"]["status"], "CANCELLED")
        self.assertEqual(payload["cancelled_retry"], "RetryPrepared")
        self.assertFalse(payload["warning"])
        self.assertEqual(payload["at_deadline"], "LiveRunning")
        self.assertEqual(payload["unknown"], "MarkedOutcomeUnknown")
        self.assertEqual(payload["blocking"], "RestartUnknownBlocking")
        self.assertEqual(payload["acknowledged"], "Acknowledged")
        self.assertEqual(payload["writes"], 1)
        self.assertTrue(payload["ack_facts"]["acknowledged"])
        self.assertEqual(payload["ack_facts"]["blocks"], 0)
        self.assertEqual(payload["ack_facts"]["messages"], [])
        self.assertEqual(payload["late"], "AcknowledgedDiscard")
        self.assertEqual(payload["warned"], "RetryPrepared")
        self.assertTrue(payload["warned_flag"])
        self.assertEqual(payload["warned_text"], "persisted original")
        self.assertEqual(payload["failed_retry"], "RetryPrepared")
        self.assertFalse(payload["failed_flag"])
        self.assertEqual(payload["failed_text"], "persisted original")

    def test_identity_mismatch_cross_owner_and_one_retry_child(self):
        payload = _payload(self, _run(
            """\
            clock = Clock(PREPARED_AT)
            pops = {"count": 0}
            composed, _root = compose(
                clock,
                popen=capturing_popen([], pops),
                load_context=lambda **_kwargs: "standing",
                context_root=None,
            )
            operator = OperatorCommands(composed.service)
            other = User.objects.create(username="other")
            created = conversation(composed)
            queued = prepare(composed, created, 1, "persisted original")
            mismatch = composed.service.submit(turn_request(
                actor_id=composed.owner_id,
                conversation_id=created,
                request_content="different text",
                idempotency_key=key_for(2),
                runner_id=RUNNER,
                expected_version=0,
                adopt_operation_id=queued.operation_id,
            ))
            before_fail = facts(queued.operation_id)["status"]
            hidden = operator.classify(other.pk, queued.operation_id)
            hidden_ack = operator.acknowledge(other.pk, queued.operation_id)
            composed.repository.fail_pre_provider(
                queued,
                PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
            )
            barrier = threading.Barrier(2)
            outcomes = []

            def attempt(key):
                barrier.wait()
                outcome = operator.create_retry(
                    composed.owner_id,
                    queued.operation_id,
                    key_for(key),
                    0,
                )
                outcomes.append(type(outcome).__name__)

            threads = [
                threading.Thread(target=attempt, args=(20,)),
                threading.Thread(target=attempt, args=(21,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            print(json.dumps({
                "provider": pops["count"],
                "mismatch": type(mismatch).__name__,
                "before_fail": before_fail,
                "still": facts(queued.operation_id)["status"],
                "content": facts(queued.operation_id)["content"],
                "hidden": type(hidden).__name__,
                "hidden_ack": type(hidden_ack).__name__,
                "outcomes": sorted(outcomes),
                "children": Operation.objects.exclude(retry_of_id=None).count(),
            }))
            """
        ))
        self.assertEqual(payload["provider"], 0)
        self.assertEqual(payload["mismatch"], "ExecutionIdentityMismatch")
        self.assertEqual(payload["before_fail"], "QUEUED")
        self.assertEqual(payload["still"], "FAILED")
        self.assertEqual(payload["content"], "persisted original")
        self.assertEqual(payload["hidden"], "NotFound")
        self.assertEqual(payload["hidden_ack"], "NotFound")
        self.assertEqual(payload["outcomes"].count("RetryPrepared"), 1)
        self.assertIn(
            payload["outcomes"][0] if payload["outcomes"][1] == "RetryPrepared"
            else payload["outcomes"][1],
            ("ModelBusy", "RetrySourceNotEligible"),
        )
        self.assertEqual(payload["children"], 1)

    def test_clock_probe_rejects_the_margin_and_is_not_wired(self):
        payload = _payload(self, _run(
            """\
            margin = RECOVERY_CLOCK_SKEW_MARGIN_US
            equal = evaluate_clock_sample(0, 0, margin, 0)
            inside = evaluate_clock_sample(0, 0, margin - 1, 0)
            print(json.dumps({
                "equal": type(equal).__name__,
                "equal_code": equal.code,
                "inside": type(inside).__name__,
                "deadline": EXECUTION_DEADLINE_US,
                "lease": FIXED_LEASE_US,
                "margin": margin,
                "allowance": ALLOWANCE,
            }))
            """
        ))
        self.assertEqual(payload["equal"], "ClockSampleRejected")
        self.assertEqual(payload["equal_code"], "disagreement")
        self.assertEqual(payload["inside"], "ClockSampleAccepted")
        self.assertEqual(payload["deadline"], 600_000_000)
        self.assertEqual(payload["lease"], 720_000_000)
        self.assertEqual(payload["margin"], 120_000_000)
        self.assertEqual(payload["allowance"], 480_000_000)
        self.assertNotIn(
            "sample_clock_safety",
            (SRC_ROOT / "durable_composition.py").read_text(),
        )
