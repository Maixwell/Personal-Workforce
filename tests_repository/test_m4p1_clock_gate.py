"""Disposable pre-dispatch clock gate for the supervised provider."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
PRODUCTION_DATABASE = PROJECT_ROOT / "data" / "max_smart.sqlite3"
PRODUCTION_FENCE = PROJECT_ROOT / "data" / "provider.fence"
SESSION_FILE = PROJECT_ROOT / "data" / "current_session.json"
sys.path.insert(0, str(SRC_ROOT))

from max_smart_db.application_validation import SIGNED_INT64_MAX  # noqa: E402
from max_smart_db.config import (  # noqa: E402
    EXECUTION_DEADLINE_US,
    FIXED_LEASE_US,
    RECOVERY_CLOCK_SKEW_MARGIN_US,
)
from provider_fence import (  # noqa: E402
    prepare_disposable_fence,
    publish_shared_fence_config,
)
from provider_supervisor import supervise  # noqa: E402


MARGIN = RECOVERY_CLOCK_SKEW_MARGIN_US
CLAIMED_AT = 1_000_000
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
OPERATION = "11111111-1111-1111-1111-111111111111"
_FENCE_ENV = (
    "MAX_SMART_PROVIDER_FENCE",
    "MAX_SMART_PROVIDER_FENCE_DEVICE",
    "MAX_SMART_PROVIDER_FENCE_INODE",
)
_TEST_FENCE = None


class ScriptedClock:
    def __init__(self, values, method):
        self.values = list(values)
        self.method = method
        self.calls = 0

    def _next(self):
        self.calls += 1
        if not self.values:
            raise RuntimeError("clock exhausted")
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def now_us(self):
        if self.method != "wall":
            raise RuntimeError("wrong clock")
        return self._next()

    def now_ns(self):
        if self.method != "monotonic":
            raise RuntimeError("wrong clock")
        return self._next()


def _drop_published_fence():
    for name in _FENCE_ENV:
        os.environ.pop(name, None)


class ClockGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _TEST_FENCE
        cls.session_stat = SESSION_FILE.stat()
        _drop_published_fence()
        directory = Path(tempfile.mkdtemp(prefix="m4p1-provider-fence-")).resolve()
        os.chmod(directory, 0o700)
        cls.fence_directory = directory
        _TEST_FENCE = prepare_disposable_fence(directory / "provider.fence")
        publish_shared_fence_config(_TEST_FENCE)

    @classmethod
    def tearDownClass(cls):
        _drop_published_fence()
        shutil.rmtree(cls.fence_directory, ignore_errors=True)
        current = SESSION_FILE.stat()
        if (
            current.st_ino != cls.session_stat.st_ino
            or current.st_size != cls.session_stat.st_size
            or current.st_mtime_ns != cls.session_stat.st_mtime_ns
        ):
            raise AssertionError("terminal session file changed")
        if PRODUCTION_DATABASE.exists():
            raise AssertionError("production database created")
        if PRODUCTION_FENCE.exists():
            raise AssertionError("production fence created")

    def test_probe_is_a_pre_dispatch_gate_not_a_startup_gate(self):
        supervisor = (SRC_ROOT / "provider_supervisor.py").read_text(encoding="utf-8")
        composition = (SRC_ROOT / "durable_composition.py").read_text(encoding="utf-8")
        self.assertIn("sample_clock_safety", supervisor)
        self.assertNotIn("sample_clock_safety", composition)
        self.assertEqual(EXECUTION_DEADLINE_US, 600_000_000)
        self.assertEqual(FIXED_LEASE_US, 720_000_000)
        self.assertEqual(MARGIN, 120_000_000)
        self.assertEqual(EXECUTION_DEADLINE_US - MARGIN, 480_000_000)

    def test_valid_sample_allows_one_fake_execution(self):
        calls = {"wall": 0, "monotonic": 0, "popen": 0}

        def wall():
            calls["wall"] += 1
            return CLAIMED_AT

        def monotonic():
            calls["monotonic"] += 1
            return time.monotonic_ns()

        def popen(*args, **kwargs):
            calls["popen"] += 1
            return subprocess.Popen(*args, **kwargs)

        result = supervise(
            claimed_at=CLAIMED_AT,
            operation_id=OPERATION,
            runner_id=RUNNER,
            request_content="hello",
            standing_context="standing",
            wall_clock=wall,
            monotonic_ns=monotonic,
            safety_wall_clock=ScriptedClock((0, MARGIN - 1), "wall"),
            safety_monotonic_clock=ScriptedClock((0, 0), "monotonic"),
            fake="ok",
            popen=popen,
            fence_config=_TEST_FENCE,
            prove_claim=lambda: True,
        )
        self.assertEqual(result.classification, "success")
        self.assertEqual(result.output, "answer")
        self.assertEqual(result.calls, 1)
        self.assertTrue(result.worker_started)
        self.assertEqual(calls, {"wall": 1, "monotonic": 2, "popen": 1})

    def test_unsafe_samples_refuse_dispatch_before_a_worker(self):
        cases = (
            ("wall_regressed", (2_000_000, 1_999_999), (5_000, 6_000)),
            ("disagreement", (0, MARGIN), (0, 0)),
            ("monotonic_disagreement", (0, 0), (0, MARGIN * 1_000)),
            ("wall_invalid", (None, 1), (0, 1)),
            ("wall_bool", (True, 1), (0, 1)),
            ("wall_overflow", (SIGNED_INT64_MAX + 1, 1), (0, 1)),
            ("monotonic_overflow", (0, 1), (SIGNED_INT64_MAX + 1, 1)),
            ("monotonic_negative", (0, 1), (-1, 0)),
            ("monotonic_regressed", (1_000, 1_001), (5_000, 4_999)),
            ("wall_exception", (RuntimeError("secret path"),), (0, 1)),
            ("monotonic_exception", (0, 1), (0, RuntimeError("secret path"))),
        )
        for name, wall_values, mono_values in cases:
            with self.subTest(name=name):
                self._assert_refused(wall_values, mono_values)

    def test_service_gate_preserves_one_dispatch_and_ambiguous_claims(self):
        payload = _payload(self, _run(_COMPLETED_BODY))
        mismatched = _payload(self, _run(_MISMATCHED_BODY))
        self.assertEqual(payload["accepted"]["result"], "DurableTurnCompleted")
        self.assertEqual(payload["accepted"]["workers"], 1)
        self.assertEqual(payload["accepted"]["again"], "NotQueued")
        self.assertEqual(payload["accepted"]["clock_reads"], 4)
        self.assertEqual(payload["accepted"]["state"]["status"], "SUCCEEDED")
        self.assertEqual(
            payload["accepted"]["state"]["deadline"],
            payload["accepted"]["state"]["started"] + EXECUTION_DEADLINE_US,
        )
        self.assertEqual(payload["accepted"]["state"]["messages"], [
            {"role": "user", "content": "persisted original"},
            {"role": "assistant", "content": "answer"},
        ])
        self.assertEqual(payload["rejected"]["result"], "DurableFailureRecorded")
        self.assertEqual(payload["rejected"]["category"], "provider_definite_failure")
        self.assertEqual(payload["rejected"]["workers"], 0)
        self.assertEqual(payload["rejected"]["clock_reads"], 4)
        self.assertEqual(payload["rejected"]["again"], "NotQueued")
        self.assertEqual(payload["rejected"]["state"]["status"], "FAILED")
        self.assertEqual(payload["rejected"]["state"]["messages"], [])
        self.assertEqual(payload["rolled"]["type"], "CommitAmbiguous")
        self.assertEqual(payload["rolled"]["workers"], 0)
        self.assertEqual(payload["rolled"]["clock_reads"], 0)
        self.assertEqual(payload["rolled"]["status"], "QUEUED")
        self.assertEqual(mismatched["type"], "CommitAmbiguous")
        self.assertEqual(mismatched["workers"], 0)
        self.assertEqual(mismatched["clock_reads"], 0)
        self.assertEqual(mismatched["status"], "RUNNING")

    def _assert_refused(self, wall_values, mono_values):
        calls = {"wall": 0, "monotonic": 0, "popen": 0, "before": 0}

        def wall():
            calls["wall"] += 1
            return CLAIMED_AT

        def monotonic():
            calls["monotonic"] += 1
            raise AssertionError("deadline clock read")

        def popen(*_args, **_kwargs):
            calls["popen"] += 1
            raise AssertionError("worker started")

        def before():
            calls["before"] += 1

        result = supervise(
            claimed_at=CLAIMED_AT,
            operation_id=OPERATION,
            runner_id=RUNNER,
            request_content="hello",
            standing_context="standing",
            wall_clock=wall,
            monotonic_ns=monotonic,
            safety_wall_clock=ScriptedClock(wall_values, "wall"),
            safety_monotonic_clock=ScriptedClock(mono_values, "monotonic"),
            fake="ok",
            popen=popen,
            before_popen=before,
            fence_config=_TEST_FENCE,
            prove_claim=lambda: True,
        )
        self.assertEqual(result.classification, "definite_non_send")
        self.assertFalse(result.worker_started)
        self.assertEqual(result.calls, 0)
        self.assertIsNone(result.output)
        self.assertNotIn("secret", repr(result))
        self.assertEqual(calls, {"wall": 0, "monotonic": 0, "popen": 0, "before": 0})


_SERVICE_BODY = """\
import subprocess
from django.db import OperationalError
from max_smart_db.repository import DurableClaimConfirmed
from provider_supervisor import build_service_provider

MARGIN = RECOVERY_CLOCK_SKEW_MARGIN_US


class Probe:
    def __init__(self, values, method):
        self.values = list(values)
        self.method = method

    def _next(self, reads):
        reads["count"] += 1
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def now_us(self):
        if self.method != "wall":
            raise RuntimeError("wrong clock")
        return self._next(self.reads)

    def now_ns(self):
        if self.method != "monotonic":
            raise RuntimeError("wrong clock")
        return self._next(self.reads)


def counting_provider(fake, wall_values, mono_values, reads, pops, on_start=None):
    wall = Probe(wall_values, "wall")
    mono = Probe(mono_values, "monotonic")
    wall.reads = reads
    mono.reads = reads

    def popen(*args, **kwargs):
        pops["count"] += 1
        return subprocess.Popen(*args, **kwargs)

    return build_service_provider(
        wall_clock=lambda: PREPARED_AT,
        fake=fake,
        popen=popen,
        on_worker_start=on_start,
        safety_wall_clock=wall,
        safety_monotonic_clock=mono,
    )


def accepted_turn():
    repo, _clock = repository()
    reservation = prepare(repo, 1)
    pops = {"count": 0}
    reads = {"count": 0}
    flags = {}

    def on_start(_pid):
        again = service.submit(turn(
            reservation.operation_id,
            reservation.conversation_id,
        ))
        flags["again"] = type(again).__name__

    service = ConversationService(
        repo,
        lambda **_kwargs: "standing-context",
        counting_provider(
            "ok",
            (0, MARGIN - 1),
            (0, 0),
            reads,
            pops,
            on_start,
        ),
    )
    outcome = service.submit(turn(
        reservation.operation_id,
        reservation.conversation_id,
    ))
    return {
        "result": type(outcome).__name__,
        "workers": pops["count"],
        "clock_reads": reads["count"],
        "again": flags["again"],
        "state": facts(reservation.operation_id),
    }


def rejected_turn():
    repo, _clock = repository()
    reservation = prepare(repo, 2)
    pops = {"count": 0}
    reads = {"count": 0}
    service = ConversationService(
        repo,
        lambda **_kwargs: "standing-context",
        counting_provider(
            "ok",
            (0, MARGIN),
            (0, 0),
            reads,
            pops,
        ),
    )
    request = turn(
        reservation.operation_id,
        reservation.conversation_id,
        key=2,
    )
    outcome = service.submit(request)
    again = service.submit(request)
    return {
        "result": type(outcome).__name__,
        "category": outcome.failure_category.value,
        "workers": pops["count"],
        "clock_reads": reads["count"],
        "again": type(again).__name__,
        "state": facts(reservation.operation_id),
    }


def rolled_claim():
    repo, _clock = repository()
    reservation = prepare(repo, 3)
    pops = {"count": 0}
    reads = {"count": 0}
    service = ConversationService(
        repo,
        lambda **_kwargs: "standing-context",
        counting_provider("ok", (0, 1), (0, 1), reads, pops),
    )
    real_commit = connection.commit
    commits = {"count": 0}

    def wrapped(*args, **kwargs):
        commits["count"] += 1
        if commits["count"] == 1:
            return real_commit(*args, **kwargs)
        raise OperationalError("simulated rollback before commit")

    connection.commit = wrapped
    try:
        outcome = service.submit(turn(
            reservation.operation_id,
            reservation.conversation_id,
            key=3,
        ))
    finally:
        connection.commit = real_commit
    operation = Operation.objects.get(pk=reservation.operation_id)
    return {
        "type": type(outcome).__name__,
        "workers": pops["count"],
        "clock_reads": reads["count"],
        "status": operation.status,
    }


def mismatched_claim():
    repo, _clock = repository()
    reservation = prepare(repo, 4)
    pops = {"count": 0}
    reads = {"count": 0}
    real_confirm = repo.confirm_durable_claim

    def mismatch(*args):
        proof = real_confirm(*args)
        return DurableClaimConfirmed(
            operation_id=proof.operation_id,
            runner_id=proof.runner_id,
            claimed_at=proof.claimed_at + 1,
        )

    repo.confirm_durable_claim = mismatch
    service = ConversationService(
        repo,
        lambda **_kwargs: "standing-context",
        counting_provider("ok", (0, 1), (0, 1), reads, pops),
    )
    real_commit = connection.commit
    commits = {"count": 0}

    def wrapped(*args, **kwargs):
        commits["count"] += 1
        real_commit(*args, **kwargs)
        if commits["count"] >= 2:
            raise OperationalError("simulated post-commit failure")

    connection.commit = wrapped
    try:
        outcome = service.submit(turn(
            reservation.operation_id,
            reservation.conversation_id,
            key=4,
        ))
    finally:
        connection.commit = real_commit
    operation = Operation.objects.get(pk=reservation.operation_id)
    return {
        "type": type(outcome).__name__,
        "workers": pops["count"],
        "clock_reads": reads["count"],
        "status": operation.status,
    }


"""


_COMPLETED_BODY = _SERVICE_BODY + """\
print(json.dumps({
    "accepted": accepted_turn(),
    "rejected": rejected_turn(),
    "rolled": rolled_claim(),
}))
"""


_MISMATCHED_BODY = _SERVICE_BODY + """\
print(json.dumps(mismatched_claim()))
"""


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
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from conversation_service import ConversationService, TurnRequest
from max_smart_db.creation import SqliteConversationRepository
from max_smart_db.config import (
    EXECUTION_DEADLINE_US,
    RECOVERY_CLOCK_SKEW_MARGIN_US,
)
from max_smart_db.models import Conversation, Message, Operation
from max_smart_db.repository import PrepareCommand

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

OWNER = User.objects.create(username="owner")
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
PREPARED_AT = 1_000_000


class Clock:
    def __init__(self, value):
        self.value = value

    def now_us(self):
        return self.value


def key_for(value):
    return base64.urlsafe_b64encode(bytes([value]) * 16).rstrip(b"=").decode()


def repository():
    clock = Clock(PREPARED_AT)
    return SqliteConversationRepository(clock, RUNNER), clock


def prepare(repo, key):
    conversation_id = uuid.uuid4()
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
    prepared = repo.prepare(PrepareCommand(
        actor_id=OWNER.id,
        conversation_id=conversation_id,
        expected_version=0,
        request_content="persisted original",
        idempotency_key=key_for(key),
        provider_id="openai",
        model_id="gpt-5.6-sol",
    ))
    if type(prepared).__name__ != "Prepared":
        raise RuntimeError(type(prepared).__name__)
    return prepared.reservation


def turn(operation_id, conversation_id, key=1):
    return TurnRequest(
        actor_id=OWNER.id,
        conversation_id=conversation_id,
        request_content="persisted original",
        idempotency_key=key_for(key),
        runner_id=RUNNER,
        expected_version=0,
        provider_id="openai",
        model_id="gpt-5.6-sol",
        adopt_operation_id=operation_id,
    )


def facts(operation_id):
    operation = Operation.objects.get(pk=operation_id)
    return {
        "status": operation.status,
        "failure": operation.failure_category,
        "started": operation.provider_started_at_us,
        "deadline": operation.execution_deadline_us,
        "messages": [
            {"role": row.role, "content": row.content}
            for row in Message.objects.filter(
                operation_id=operation_id,
            ).order_by("sequence")
        ],
    }
"""


def _run(body, *, timeout=90):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "m4p1-clock-gate.sqlite3"
            ),
            "PYTHONPATH": str(SRC_ROOT),
        }
        for name in _FENCE_ENV:
            environment.pop(name, None)
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


if __name__ == "__main__":
    unittest.main()
