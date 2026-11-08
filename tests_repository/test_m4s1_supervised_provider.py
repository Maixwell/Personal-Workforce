"""Disposable tests for the supervised provider deadline."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
PRODUCTION_DATABASE = PROJECT_ROOT / "data" / "max_smart.sqlite3"
SESSION_FILE = PROJECT_ROOT / "data" / "current_session.json"
sys.path.insert(0, str(SRC_ROOT))

from provider_supervisor import build_service_provider, spawn_worker, supervise  # noqa: E402


SECRET = "sk-test-do-not-leak"
REQUEST_SECRET = "request-do-not-leak"
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
OPERATION = "11111111-1111-1111-1111-111111111111"
CLAIMED_AT = 1_000_000
ALLOWANCE_US = 480_000_000


def _listen(testcase):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    accepted = []

    def accept():
        connection, _address = server.accept()
        accepted.append(connection)

    def close():
        for connection in accepted:
            connection.close()
        server.close()

    threading.Thread(target=accept, daemon=True).start()
    testcase.addCleanup(close)
    return server


def _dead(pid):
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.02)
    return False


def _ready(path, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        time.sleep(0.01)
    raise AssertionError("worker did not reach the socket read")


def _job(deadline_ns, **overrides):
    job = {
        "parent_pid": os.getpid(),
        "deadline_monotonic_ns": deadline_ns,
        "mode": "fake",
        "fake": "block_read",
        "api_key": SECRET,
        "request_content": REQUEST_SECRET,
        "standing_context": "standing",
        "operation_id": OPERATION,
        "runner_id": RUNNER,
        "claimed_at": CLAIMED_AT,
        "timeout_seconds": 1,
        "socket_port": None,
        "ready_path": None,
    }
    job.update(overrides)
    return job


class SupervisedProviderTests(unittest.TestCase):
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

    def test_worker_source_has_no_database_access(self):
        source = (SRC_ROOT / "provider_worker.py").read_text(encoding="utf-8")
        for banned in ("django", "sqlite3", "max_smart_db", "conversation_service"):
            self.assertNotIn(banned, source)
        self.assertNotIn("setsid", source)

    def test_cutoff_already_reached_starts_no_worker(self):
        def reject_popen(*_args, **_kwargs):
            raise AssertionError("worker started")

        result = supervise(
            claimed_at=CLAIMED_AT,
            operation_id=OPERATION,
            runner_id=RUNNER,
            request_content=REQUEST_SECRET,
            standing_context="standing",
            wall_clock=lambda: CLAIMED_AT + ALLOWANCE_US,
            api_key=SECRET,
            fake="ok",
            popen=reject_popen,
        )
        self.assertEqual(result.classification, "definite_non_send")
        self.assertFalse(result.worker_started)

    def test_delayed_start_past_cutoff_starts_no_worker(self):
        starts = []

        def wall():
            return CLAIMED_AT + ALLOWANCE_US - 100_000

        result = supervise(
            claimed_at=CLAIMED_AT,
            operation_id=OPERATION,
            runner_id=RUNNER,
            request_content="hello",
            standing_context="standing",
            wall_clock=wall,
            fake="ok",
            before_popen=lambda: time.sleep(0.2),
            on_worker_start=starts.append,
        )
        self.assertEqual(result.classification, "definite_non_send")
        self.assertEqual(starts, [])
        self.assertFalse(result.worker_started)

    def test_successful_response_is_one_request(self):
        result = supervise(
            claimed_at=CLAIMED_AT,
            operation_id=OPERATION,
            runner_id=RUNNER,
            request_content=REQUEST_SECRET,
            standing_context="standing",
            wall_clock=lambda: CLAIMED_AT,
            api_key=SECRET,
            fake="ok",
        )
        self.assertEqual(result.classification, "success")
        self.assertEqual(result.output, "answer")
        self.assertEqual(result.calls, 1)
        self.assertEqual(result.max_retries, 0)
        self.assertIs(result.store, False)
        self.assertEqual(result.model, "gpt-5.6-sol")
        self.assertTrue(result.worker_started)

    def test_blank_completed_output_is_invalid(self):
        result = supervise(
            claimed_at=CLAIMED_AT,
            operation_id=OPERATION,
            runner_id=RUNNER,
            request_content="hello",
            standing_context="standing",
            wall_clock=lambda: CLAIMED_AT,
            fake="blank",
        )
        self.assertEqual(result.classification, "invalid_output")
        self.assertIsNone(result.output)
        self.assertEqual(result.calls, 1)

    def test_nul_completed_output_is_invalid(self):
        result = supervise(
            claimed_at=CLAIMED_AT,
            operation_id=OPERATION,
            runner_id=RUNNER,
            request_content="hello",
            standing_context="standing",
            wall_clock=lambda: CLAIMED_AT,
            fake="nul",
        )
        self.assertEqual(result.classification, "invalid_output")

    def test_socket_timeout_is_uncertain_and_reaped(self):
        server = _listen(self)
        seen = []
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "ready"
            result = supervise(
                claimed_at=CLAIMED_AT,
                operation_id=OPERATION,
                runner_id=RUNNER,
                request_content=REQUEST_SECRET,
                standing_context="standing",
                wall_clock=lambda: CLAIMED_AT + ALLOWANCE_US - 400_000,
                api_key=SECRET,
                fake="block_read",
                socket_port=server.getsockname()[1],
                ready_path=str(ready),
                on_worker_start=seen.append,
            )
        self.assertEqual(result.classification, "uncertain")
        self.assertIsNone(result.output)
        self.assertEqual(len(seen), 1)
        self.assertTrue(_dead(seen[0]))

    def test_same_group_descendant_is_killed(self):
        server = _listen(self)
        seen = {}
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "ready"

            def started(pid):
                info = _ready(ready, 1.5)
                self.assertEqual(os.getpgid(pid), pid)
                self.assertEqual(os.getpgid(info["child"]), pid)
                seen["worker"] = pid
                seen["child"] = info["child"]

            result = supervise(
                claimed_at=CLAIMED_AT,
                operation_id=OPERATION,
                runner_id=RUNNER,
                request_content="hello",
                standing_context="standing",
                wall_clock=lambda: CLAIMED_AT + ALLOWANCE_US - 2_000_000,
                fake="block_read_child",
                socket_port=server.getsockname()[1],
                ready_path=str(ready),
                on_worker_start=started,
            )
        self.assertEqual(result.classification, "uncertain")
        self.assertTrue(_dead(seen["worker"]))
        self.assertTrue(_dead(seen["child"]))

    def test_crash_and_partial_frame_are_uncertain(self):
        for scenario in ("crash", "partial"):
            with self.subTest(scenario=scenario):
                result = supervise(
                    claimed_at=CLAIMED_AT,
                    operation_id=OPERATION,
                    runner_id=RUNNER,
                    request_content="hello",
                    standing_context="standing",
                    wall_clock=lambda: CLAIMED_AT,
                    fake=scenario,
                )
                self.assertEqual(result.classification, "uncertain")
                self.assertIsNone(result.output)

    def test_completed_bytes_without_exit_are_not_accepted(self):
        server = _listen(self)
        result = supervise(
            claimed_at=CLAIMED_AT,
            operation_id=OPERATION,
            runner_id=RUNNER,
            request_content="hello",
            standing_context="standing",
            wall_clock=lambda: CLAIMED_AT + ALLOWANCE_US - 400_000,
            fake="frame_then_block",
            socket_port=server.getsockname()[1],
        )
        self.assertEqual(result.classification, "uncertain")
        self.assertIsNone(result.output)

    def test_double_request_frame_is_rejected(self):
        result = supervise(
            claimed_at=CLAIMED_AT,
            operation_id=OPERATION,
            runner_id=RUNNER,
            request_content="hello",
            standing_context="standing",
            wall_clock=lambda: CLAIMED_AT,
            fake="double_call",
        )
        self.assertEqual(result.classification, "uncertain")

    def test_backward_wall_clock_does_not_extend_budget(self):
        readings = []

        def wall():
            readings.append("read")
            return CLAIMED_AT + ALLOWANCE_US - 300_000

        server = _listen(self)
        started = time.monotonic()
        result = supervise(
            claimed_at=CLAIMED_AT,
            operation_id=OPERATION,
            runner_id=RUNNER,
            request_content="hello",
            standing_context="standing",
            wall_clock=wall,
            fake="block_read",
            socket_port=server.getsockname()[1],
        )
        elapsed = time.monotonic() - started
        self.assertEqual(readings, ["read"])
        self.assertEqual(result.classification, "uncertain")
        self.assertLess(elapsed, 1.2)

    def test_delayed_start_keeps_original_deadline(self):
        server = _listen(self)
        times = {}

        def started(_pid):
            times["started"] = time.monotonic()

        started_at = time.monotonic()
        result = supervise(
            claimed_at=CLAIMED_AT,
            operation_id=OPERATION,
            runner_id=RUNNER,
            request_content="hello",
            standing_context="standing",
            wall_clock=lambda: CLAIMED_AT + ALLOWANCE_US - 800_000,
            fake="block_read",
            socket_port=server.getsockname()[1],
            before_popen=lambda: time.sleep(0.35),
            on_worker_start=started,
        )
        lifetime = time.monotonic() - times["started"]
        total = time.monotonic() - started_at
        self.assertEqual(result.classification, "uncertain")
        self.assertLess(lifetime, 0.65)
        self.assertGreater(total, 0.7)
        self.assertLess(total, 1.15)

    def test_credentials_stay_off_argv_and_stderr(self):
        processes = []

        def popen(args, **kwargs):
            process = subprocess.Popen(args, **kwargs)
            processes.append((args, process))
            return process

        result = supervise(
            claimed_at=CLAIMED_AT,
            operation_id=OPERATION,
            runner_id=RUNNER,
            request_content=REQUEST_SECRET,
            standing_context="standing",
            wall_clock=lambda: CLAIMED_AT,
            api_key=SECRET,
            fake="ok",
            popen=popen,
            stderr=subprocess.PIPE,
        )
        args, process = processes[0]
        error = process.stderr.read()
        process.stderr.close()
        self.assertEqual(result.classification, "success")
        self.assertEqual(len(processes), 1)
        self.assertNotIn(SECRET, " ".join(args))
        self.assertNotIn(REQUEST_SECRET, " ".join(args))
        self.assertNotIn(SECRET.encode("utf-8"), error)
        self.assertNotIn(REQUEST_SECRET.encode("utf-8"), error)
        self.assertEqual(args[1:], ["-m", "provider_worker"])

    def test_worker_self_cutoff_while_parent_lives(self):
        server = _listen(self)
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "ready"
            started = time.monotonic()
            process = spawn_worker(_job(
                time.monotonic_ns() + 1_500_000_000,
                socket_port=server.getsockname()[1],
                ready_path=str(ready),
            ))
            self.addCleanup(lambda: _ensure_dead(process))
            _ready(ready, 2)
            code = process.wait(timeout=3)
        self.assertEqual(code, -9)
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertGreater(time.monotonic() - started, 0.8)

    def test_supervisor_disappearance_stops_worker(self):
        server = _listen(self)
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "ready"
            job = _job(
                time.monotonic_ns() + 20_000_000_000,
                socket_port=server.getsockname()[1],
                ready_path=str(ready),
            )
            launcher = subprocess.Popen(
                [sys.executable, "-c", _LAUNCHER],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_child_env(),
            )
            stdout, stderr = launcher.communicate(
                json.dumps(job).encode("utf-8"),
                timeout=5,
            )
        self.assertEqual(launcher.returncode, 0, stderr.decode())
        worker_pid = int(stdout.decode().strip())
        self.addCleanup(lambda: _kill_if_alive(worker_pid))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not _dead(worker_pid):
            time.sleep(0.02)
        self.assertTrue(_dead(worker_pid))

    def test_unconfirmed_stop_is_uncertain_and_not_retried(self):
        server = _listen(self)
        calls = {"popen": 0, "stop": 0}

        def popen(*args, **kwargs):
            calls["popen"] += 1
            return subprocess.Popen(*args, **kwargs)

        def stop(process):
            calls["stop"] += 1
            if calls["stop"] == 1:
                return False
            os.killpg(process.pid, 9)
            process.wait(timeout=2)
            return True

        result = supervise(
            claimed_at=CLAIMED_AT,
            operation_id=OPERATION,
            runner_id=RUNNER,
            request_content="hello",
            standing_context="standing",
            wall_clock=lambda: CLAIMED_AT + ALLOWANCE_US - 300_000,
            fake="block_read",
            socket_port=server.getsockname()[1],
            popen=popen,
            stop=stop,
        )
        self.assertEqual(result.classification, "uncertain")
        self.assertEqual(calls["popen"], 1)
        self.assertGreaterEqual(calls["stop"], 2)

    def test_service_integration_uses_disposable_database(self):
        payload = _payload(self, _run(
            """\
            import socket
            import subprocess
            import threading
            from provider_supervisor import build_service_provider
            from max_smart_db.config import (
                EXECUTION_DEADLINE_US,
                RECOVERY_CLOCK_SKEW_MARGIN_US,
            )

            ALLOWANCE = EXECUTION_DEADLINE_US - RECOVERY_CLOCK_SKEW_MARGIN_US
            repo, _clock = repository()
            reservation = prepare(repo, 1)
            pops = {"count": 0}
            real_popen = subprocess.Popen

            def counting_popen(*args, **kwargs):
                pops["count"] += 1
                return real_popen(*args, **kwargs)

            flags = {}

            def on_start(_pid):
                flags["autocommit"] = connection.get_autocommit()
                flags["atomic"] = connection.in_atomic_block
                again = service.submit(turn(
                    reservation.operation_id,
                    reservation.conversation_id,
                ))
                flags["again"] = type(again).__name__

            provider = build_service_provider(
                wall_clock=lambda: PREPARED_AT,
                fake="ok",
                api_key="sk-test-do-not-leak",
                popen=counting_popen,
                on_worker_start=on_start,
            )
            service = ConversationService(
                repo,
                lambda **_kwargs: "standing-context",
                provider,
            )
            outcome = service.submit(turn(
                reservation.operation_id,
                reservation.conversation_id,
            ))
            success = {
                "result": type(outcome).__name__,
                "workers": pops["count"],
                "autocommit": flags["autocommit"],
                "atomic": flags["atomic"],
                "again": flags["again"],
                "state": facts(reservation.operation_id),
            }

            pops["count"] = 0
            blank_repo, _blank_clock = repository()
            blank_reservation = prepare(blank_repo, 2)
            blank_provider = build_service_provider(
                wall_clock=lambda: PREPARED_AT,
                fake="blank",
                popen=counting_popen,
            )
            blank = ConversationService(
                blank_repo,
                lambda **_kwargs: "standing-context",
                blank_provider,
            ).submit(turn(
                blank_reservation.operation_id,
                blank_reservation.conversation_id,
                key=2,
            ))
            invalid = {
                "result": type(blank).__name__,
                "category": blank.failure_category.value,
                "workers": pops["count"],
                "state": facts(blank_reservation.operation_id),
            }

            pops["count"] = 0
            cutoff_repo, _cutoff_clock = repository()
            cutoff_reservation = prepare(cutoff_repo, 3)

            def reject_popen(*_args, **_kwargs):
                pops["count"] += 1
                raise AssertionError("worker started")

            cutoff_provider = build_service_provider(
                wall_clock=lambda: PREPARED_AT + ALLOWANCE,
                fake="ok",
                popen=reject_popen,
            )
            cutoff = ConversationService(
                cutoff_repo,
                lambda **_kwargs: "standing-context",
                cutoff_provider,
            ).submit(turn(
                cutoff_reservation.operation_id,
                cutoff_reservation.conversation_id,
                key=3,
            ))
            missed = {
                "result": type(cutoff).__name__,
                "category": cutoff.failure_category.value,
                "workers": pops["count"],
                "state": facts(cutoff_reservation.operation_id),
            }

            pops["count"] = 0
            held_repo, _held_clock = repository()
            with transaction.atomic():
                held = ConversationService(
                    held_repo,
                    lambda **_kwargs: "standing-context",
                    build_service_provider(
                        wall_clock=lambda: PREPARED_AT,
                        fake="ok",
                        popen=counting_popen,
                    ),
                ).submit(turn(uuid.uuid4(), uuid.uuid4(), key=4))
            transaction_held = {
                "result": type(held).__name__,
                "workers": pops["count"],
            }

            server = socket.socket()
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            held = []

            def accept_and_hold():
                connection, _address = server.accept()
                held.append(connection)

            threading.Thread(target=accept_and_hold, daemon=True).start()
            pops["count"] = 0
            blocked_repo, _blocked_clock = repository()
            blocked_reservation = prepare(blocked_repo, 5)
            blocked = ConversationService(
                blocked_repo,
                lambda **_kwargs: "standing-context",
                build_service_provider(
                    wall_clock=lambda: PREPARED_AT + ALLOWANCE - 400_000,
                    fake="block_read",
                    socket_port=server.getsockname()[1],
                    popen=counting_popen,
                ),
            ).submit(turn(
                blocked_reservation.operation_id,
                blocked_reservation.conversation_id,
                key=5,
            ))
            timed_out = {
                "result": type(blocked).__name__,
                "workers": pops["count"],
                "state": facts(blocked_reservation.operation_id),
            }
            server.close()
            print(json.dumps({
                "success": success,
                "invalid": invalid,
                "missed": missed,
                "held": transaction_held,
                "timed_out": timed_out,
            }))
            """
        ))
        self.assertEqual(payload["success"]["result"], "DurableTurnCompleted")
        self.assertEqual(payload["success"]["workers"], 1)
        self.assertIs(payload["success"]["autocommit"], True)
        self.assertIs(payload["success"]["atomic"], False)
        self.assertEqual(payload["success"]["again"], "NotQueued")
        self.assertEqual(payload["success"]["state"]["status"], "SUCCEEDED")
        self.assertEqual(payload["success"]["state"]["messages"], [
            {"role": "user", "content": "persisted original"},
            {"role": "assistant", "content": "answer"},
        ])
        self.assertEqual(payload["invalid"]["result"], "DurableFailureRecorded")
        self.assertEqual(payload["invalid"]["category"], "provider_output_invalid")
        self.assertEqual(payload["invalid"]["workers"], 1)
        self.assertEqual(payload["invalid"]["state"]["messages"], [])
        self.assertEqual(payload["missed"]["result"], "DurableFailureRecorded")
        self.assertEqual(payload["missed"]["category"], "provider_definite_failure")
        self.assertEqual(payload["missed"]["workers"], 0)
        self.assertEqual(payload["missed"]["state"]["status"], "FAILED")
        self.assertEqual(payload["held"]["result"], "TransactionHeld")
        self.assertEqual(payload["held"]["workers"], 0)
        self.assertEqual(payload["timed_out"]["result"], "UncertainRecorded")
        self.assertEqual(payload["timed_out"]["workers"], 1)
        self.assertEqual(payload["timed_out"]["state"]["status"], "RUNNING")
        self.assertEqual(
            payload["timed_out"]["state"]["failure"],
            "provider_outcome_uncertain",
        )


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
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from conversation_service import ConversationService, TurnRequest
from max_smart_db.creation import SqliteConversationRepository
from max_smart_db.models import Conversation, Message, Operation

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

OWNER = User.objects.create(username="owner")
CONVERSATION = uuid.UUID("00112233445566778899aabbccddeeff")
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
PREPARED_AT = 1_000_000
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

    def now_us(self):
        return self.value


def key_for(value):
    return base64.urlsafe_b64encode(bytes([value]) * 16).rstrip(b"=").decode()


def repository():
    clock = Clock(PREPARED_AT)
    return SqliteConversationRepository(clock, RUNNER), clock


def prepare(repo, key):
    from max_smart_db.repository import PrepareCommand
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
        "messages": [
            {"role": row.role, "content": row.content}
            for row in Message.objects.filter(
                operation_id=operation_id,
            ).order_by("sequence")
        ],
    }
"""


_LAUNCHER = """\
import json
import os
import sys
import time
from provider_supervisor import spawn_worker
job = json.loads(sys.stdin.buffer.read().decode("utf-8"))
job["parent_pid"] = os.getpid()
process = spawn_worker(job)
ready = job["ready_path"]
deadline = time.monotonic() + 5
while time.monotonic() < deadline and not os.path.exists(ready):
    if process.poll() is not None:
        os._exit(1)
    time.sleep(0.01)
sys.stdout.write(str(process.pid) + "\\n")
sys.stdout.flush()
os._exit(0)
"""


def _child_env():
    current = os.environ.get("PYTHONPATH", "")
    return {
        **os.environ,
        "PYTHONPATH": str(SRC_ROOT) + (os.pathsep + current if current else ""),
    }


def _ensure_dead(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, 9)
        except OSError:
            pass
        process.wait(timeout=2)
    if process.stdout is not None and not process.stdout.closed:
        process.stdout.close()


def _kill_if_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return
    try:
        os.killpg(pid, 9)
    except OSError:
        pass


def _run(body, *, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "m4s1-provider.sqlite3"
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


if __name__ == "__main__":
    unittest.main()
