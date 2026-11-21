"""Disposable Terminal turn adapter tests. No production resources are used."""

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
PRODUCTION_FENCE = PROJECT_ROOT / "data" / "provider.fence"
TRUST_PIN = Path("/Users/smart/Library/Application Support/MaxSmartTrust")
SESSION_FILE = PROJECT_ROOT / "data" / "current_session.json"
ADAPTER_SOURCE = SRC_ROOT / "terminal_durable_turn.py"

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
from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from max_smart_db.config import PRODUCTION_DATABASE_PATH
from max_smart_db.models import Conversation, Message, Operation
from terminal_durable_turn import (
    DisposableTerminalTurnCompleted,
    submit_disposable_terminal_turn,
)

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
PREPARED_AT = 1_000_000


class Clock:
    def __init__(self, value):
        self.value = value

    def now_us(self):
        return self.value


def key_for(value):
    return base64.urlsafe_b64encode(bytes([value]) * 16).rstrip(b"=").decode()


def write_context(root):
    docs = Path(root) / "docs"
    docs.mkdir(parents=True, exist_ok=True)
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
            job = json.loads(payload.decode("utf-8").splitlines()[0])
            jobs.append({
                "mode": job["mode"],
                "fake": job["fake"],
                "api_key_empty": job["api_key"] == "",
                "fence_path": job["fence_path"],
            })
            return original(payload)

        proc.stdin.write = write
        return proc

    return popen


def submit(pops, jobs, **overrides):
    arguments = {
        "clock": Clock(PREPARED_AT),
        "runner_id": RUNNER,
        "fake": "ok",
        "context_root": write_context(database.parent / "context"),
        "request_content": "current request",
        "idempotency_key": key_for(1),
        "expected_version": 0,
        "popen": capturing_popen(jobs, pops),
    }
    arguments.update(overrides)
    return submit_disposable_terminal_turn(**arguments)


def described(result):
    if type(result) is DisposableTerminalTurnCompleted:
        return {
            "completed": True,
            "assistant_text": result.assistant_text,
            "conversation_id": str(result.conversation_id),
            "next_expected_version": result.next_expected_version,
            "operation_id": str(result.operation_id),
            "owner_id": result.owner_id,
        }
    return {
        "completed": False,
        "code": result.code,
        "outcome": type(result.outcome).__name__ if result.outcome is not None else None,
    }
"""


def _run(body, *, timeout=90):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "terminal-durable-turn.sqlite3"
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


class DisposableTerminalTurnTests(unittest.TestCase):
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
        if PRODUCTION_FENCE.exists():
            raise AssertionError("production fence created")
        if TRUST_PIN.exists():
            raise AssertionError("trust pin created")

    def test_one_message_completes_one_durable_turn(self):
        payload = _payload(self, _run(
            """\
            jobs = []
            pops = {"count": 0}
            result = submit(pops, jobs)
            viewed = Conversation.objects.get(pk=result.conversation_id)
            operation = Operation.objects.get(pk=result.operation_id)
            messages = [
                {"role": row.role, "content": row.content}
                for row in Message.objects.order_by("sequence")
            ]
            print(json.dumps({
                "result": described(result),
                "workers": pops["count"],
                "job": jobs[0],
                "conversations": Conversation.objects.count(),
                "version": viewed.version,
                "status": operation.status,
                "messages": messages,
                "fence_under_database": Path(jobs[0]["fence_path"]).resolve() == database.parent.resolve() / "provider.fence",
                "production_database_absent": not PRODUCTION_DATABASE_PATH.exists(),
            }))
            """
        ))
        self.assertEqual(payload["result"]["completed"], True)
        self.assertEqual(payload["result"]["assistant_text"], "answer")
        self.assertEqual(payload["result"]["next_expected_version"], 1)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["workers"], 1)
        self.assertEqual(payload["job"]["mode"], "fake")
        self.assertEqual(payload["job"]["fake"], "ok")
        self.assertTrue(payload["job"]["api_key_empty"])
        self.assertTrue(payload["fence_under_database"])
        self.assertEqual(payload["conversations"], 1)
        self.assertEqual(payload["status"], "SUCCEEDED")
        self.assertEqual(payload["messages"], [
            {"role": "user", "content": "current request"},
            {"role": "assistant", "content": "answer"},
        ])
        self.assertTrue(payload["production_database_absent"])

    def test_blank_and_missing_fake_do_not_execute(self):
        payload = _payload(self, _run(
            """\
            jobs = []
            pops = {"count": 0}
            blank = described(submit(pops, jobs, request_content=""))
            spaces = described(submit(pops, jobs, request_content="   "))
            missing = described(submit(pops, jobs, fake=None))
            empty = described(submit(pops, jobs, fake=""))
            print(json.dumps({
                "blank": blank,
                "spaces": spaces,
                "missing": missing,
                "empty": empty,
                "workers": pops["count"],
                "conversations": Conversation.objects.count(),
                "users": User.objects.count(),
                "messages": Message.objects.count(),
            }))
            """
        ))
        self.assertEqual(payload["blank"]["code"], "blank_message")
        self.assertEqual(payload["spaces"]["code"], "blank_message")
        self.assertEqual(payload["missing"]["code"], "fake_provider_required")
        self.assertEqual(payload["empty"]["code"], "fake_provider_required")
        self.assertFalse(payload["blank"]["completed"])
        self.assertEqual(payload["workers"], 0)
        self.assertEqual(payload["conversations"], 0)
        self.assertEqual(payload["users"], 0)
        self.assertEqual(payload["messages"], 0)

    def test_invalid_inputs_refuse_before_execution(self):
        payload = _payload(self, _run(
            """\
            class BadClock:
                def now_us(self):
                    return -1

            jobs = []
            pops = {"count": 0}
            short_key = base64.urlsafe_b64encode(bytes([1]) * 15).rstrip(b"=").decode()
            refusals = {
                "type": described(submit(pops, jobs, idempotency_key=None)),
                "format": described(submit(pops, jobs, idempotency_key="!!!")),
                "entropy": described(submit(pops, jobs, idempotency_key=short_key)),
                "runner": described(submit(pops, jobs, runner_id="bad")),
                "clock": described(submit(pops, jobs, clock=BadClock())),
                "version": described(submit(pops, jobs, expected_version=-1)),
            }
            print(json.dumps({
                "refusals": refusals,
                "workers": pops["count"],
                "conversations": Conversation.objects.count(),
                "operations": Operation.objects.count(),
                "users": User.objects.count(),
                "messages": Message.objects.count(),
            }))
            """
        ))
        self.assertEqual(payload["refusals"]["type"]["code"], "idempotency_key_type")
        self.assertEqual(payload["refusals"]["format"]["code"], "idempotency_key_format")
        self.assertEqual(payload["refusals"]["entropy"]["code"], "idempotency_key_entropy")
        self.assertEqual(payload["refusals"]["runner"]["code"], "runner_invalid")
        self.assertEqual(payload["refusals"]["clock"]["code"], "clock_invalid")
        self.assertEqual(payload["refusals"]["version"]["code"], "expected_version_range")
        for refusal in payload["refusals"].values():
            self.assertFalse(refusal["completed"])
            self.assertNotIn("assistant_text", refusal)
        self.assertEqual(payload["workers"], 0)
        self.assertEqual(payload["conversations"], 0)
        self.assertEqual(payload["operations"], 0)
        self.assertEqual(payload["users"], 0)
        self.assertEqual(payload["messages"], 0)

    def test_creation_exception_does_not_execute_provider(self):
        payload = _payload(self, _run(
            """\
            from max_smart_db.models import Role
            Role.objects.filter(pk="max-smart").update(active=0)
            jobs = []
            pops = {"count": 0}
            result = described(submit(pops, jobs, idempotency_key=key_for(4)))
            print(json.dumps({
                "result": result,
                "workers": pops["count"],
                "conversations": Conversation.objects.count(),
                "operations": Operation.objects.count(),
                "messages": Message.objects.count(),
            }))
            """
        ))
        self.assertFalse(payload["result"]["completed"])
        self.assertEqual(payload["result"]["code"], "role_invalid")
        self.assertNotIn("assistant_text", payload["result"])
        self.assertEqual(payload["workers"], 0)
        self.assertEqual(payload["conversations"], 0)
        self.assertEqual(payload["operations"], 0)
        self.assertEqual(payload["messages"], 0)

    def test_continuation_requires_valid_owner_before_execution(self):
        payload = _payload(self, _run(
            """\
            jobs = []
            pops = {"count": 0}
            conversation_id = uuid.uuid4()
            missing = described(submit(
                pops,
                jobs,
                conversation_id=conversation_id,
                idempotency_key=key_for(5),
            ))
            malformed = described(submit(
                pops,
                jobs,
                conversation_id=conversation_id,
                owner_id="1",
                idempotency_key=key_for(5),
            ))
            unknown = described(submit(
                pops,
                jobs,
                conversation_id=conversation_id,
                owner_id=0,
                idempotency_key=key_for(5),
            ))
            print(json.dumps({
                "missing": missing,
                "malformed": malformed,
                "unknown": unknown,
                "workers": pops["count"],
                "conversations": Conversation.objects.count(),
                "operations": Operation.objects.count(),
                "users": User.objects.count(),
                "messages": Message.objects.count(),
            }))
            """
        ))
        self.assertEqual(payload["missing"]["code"], "owner_required")
        self.assertEqual(payload["malformed"]["code"], "owner_id_type")
        self.assertEqual(payload["unknown"]["code"], "owner_mismatch")
        for refusal in (
            payload["missing"],
            payload["malformed"],
            payload["unknown"],
        ):
            self.assertFalse(refusal["completed"])
            self.assertNotIn("assistant_text", refusal)
        self.assertEqual(payload["workers"], 0)
        self.assertEqual(payload["conversations"], 0)
        self.assertEqual(payload["operations"], 0)
        self.assertEqual(payload["users"], 0)
        self.assertEqual(payload["messages"], 0)

    def test_production_and_non_disposable_databases_are_refused(self):
        payload = _payload(self, _run(
            """\
            jobs = []
            pops = {"count": 0}
            settings.MAX_SMART_DATABASE_KIND = "production"
            kind_refusal = described(submit(pops, jobs))
            settings.MAX_SMART_DATABASE_KIND = "test"
            settings.DATABASES["default"]["NAME"] = str(PRODUCTION_DATABASE_PATH)
            path_refusal = described(submit(pops, jobs))
            print(json.dumps({
                "kind": kind_refusal,
                "path": path_refusal,
                "workers": pops["count"],
                "conversations": Conversation.objects.count(),
                "users": User.objects.count(),
                "production_database_absent": not PRODUCTION_DATABASE_PATH.exists(),
            }))
            """
        ))
        self.assertEqual(payload["kind"]["code"], "production_database")
        self.assertEqual(payload["path"]["code"], "production_database")
        self.assertFalse(payload["kind"]["completed"])
        self.assertFalse(payload["path"]["completed"])
        self.assertEqual(payload["workers"], 0)
        self.assertEqual(payload["conversations"], 0)
        self.assertEqual(payload["users"], 0)
        self.assertTrue(payload["production_database_absent"])

    def test_provider_failure_is_not_success(self):
        payload = _payload(self, _run(
            """\
            jobs = []
            pops = {"count": 0}
            result = submit(pops, jobs, fake="blank", idempotency_key=key_for(2))
            print(json.dumps({
                "result": described(result),
                "workers": pops["count"],
                "conversations": Conversation.objects.count(),
                "messages": Message.objects.count(),
                "versions": list(Conversation.objects.values_list("version", flat=True)),
                "statuses": list(Operation.objects.values_list("status", flat=True)),
            }))
            """
        ))
        self.assertFalse(payload["result"]["completed"])
        self.assertEqual(payload["result"]["code"], "DurableFailureRecorded")
        self.assertNotIn("assistant_text", payload["result"])
        self.assertEqual(payload["workers"], 1)
        self.assertEqual(payload["conversations"], 1)
        self.assertEqual(payload["messages"], 0)
        self.assertEqual(payload["versions"], [0])
        self.assertEqual(payload["statuses"], ["FAILED"])

    def test_duplicate_idempotency_key_does_not_create_another_turn(self):
        payload = _payload(self, _run(
            """\
            jobs = []
            pops = {"count": 0}
            first = submit(pops, jobs)
            duplicate = submit(
                pops,
                jobs,
                conversation_id=first.conversation_id,
                owner_id=first.owner_id,
                idempotency_key=key_for(1),
                expected_version=0,
                request_content="current request",
            )
            stale = submit(
                pops,
                jobs,
                conversation_id=first.conversation_id,
                owner_id=first.owner_id,
                idempotency_key=key_for(3),
                expected_version=0,
                request_content="another request",
            )
            print(json.dumps({
                "first": described(first),
                "duplicate": described(duplicate),
                "stale": described(stale),
                "workers": pops["count"],
                "conversations": Conversation.objects.count(),
                "operations": Operation.objects.count(),
                "messages": Message.objects.count(),
                "version": Conversation.objects.get().version,
            }))
            """
        ))
        self.assertTrue(payload["first"]["completed"])
        self.assertEqual(payload["first"]["assistant_text"], "answer")
        self.assertFalse(payload["duplicate"]["completed"])
        self.assertEqual(payload["duplicate"]["code"], "Existing")
        self.assertFalse(payload["stale"]["completed"])
        self.assertEqual(payload["stale"]["code"], "StaleVersion")
        self.assertEqual(payload["workers"], 1)
        self.assertEqual(payload["conversations"], 1)
        self.assertEqual(payload["operations"], 1)
        self.assertEqual(payload["messages"], 2)
        self.assertEqual(payload["version"], 1)

    def test_live_terminal_does_not_reference_the_adapter(self):
        adapter = ADAPTER_SOURCE.read_text(encoding="utf-8")
        for banned in (
            "current_session",
            "OpenAI",
            "operator_commands",
            "initialize_sqlite_file",
            "max_smart.py",
            "provider_worker",
        ):
            self.assertNotIn(banned, adapter)
        for name in ("max_smart.py", "terminal.py"):
            source = (SRC_ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("terminal_durable_turn", source)
            self.assertNotIn("submit_disposable_terminal_turn", source)
