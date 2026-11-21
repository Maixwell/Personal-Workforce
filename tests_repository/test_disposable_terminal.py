"""Focused tests for the explicit disposable development Terminal."""

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


def _child_environment(**updates):
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DJANGO_SETTINGS_MODULE", "MAX_SMART_TEST_DATABASE_PATH"}
    }
    environment.update({
        "PYTHONPATH": str(SRC_ROOT),
        "USER": "os-account",
        "LOGNAME": "os-account",
        "USERNAME": "os-account",
    })
    environment.update(updates)
    return environment


def _run(script, *, timeout=90, stdin=None, environment_updates=None):
    with tempfile.TemporaryDirectory() as temporary:
        database = Path(temporary).resolve() / "private" / "disposable.sqlite3"
        environment = _child_environment(DISPOSABLE_TERMINAL_DATABASE=str(database))
        if environment_updates:
            environment.update(environment_updates)
        result = subprocess.run(
            (sys.executable, "-B", "-c", textwrap.dedent(script)),
            cwd=PROJECT_ROOT,
            env=environment,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        return result, database


def _payload(testcase, result):
    testcase.assertEqual(
        result.returncode,
        0,
        result.stdout + "\n" + result.stderr,
    )
    return json.loads(result.stdout)


class DisposableTerminalTests(unittest.TestCase):
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

    def test_two_messages_continue_one_conversation(self):
        result, _database = _run(
            """\
            import json
            import os
            import subprocess
            from pathlib import Path

            from disposable_terminal import (
                DisposableTerminalSession,
                activate_disposable_database,
                create_fresh_disposable_database,
                run_disposable_terminal,
                write_disposable_context,
            )

            database = create_fresh_disposable_database(os.environ["DISPOSABLE_TERMINAL_DATABASE"])
            activate_disposable_database(database)
            from max_smart_db.models import Conversation, Message, Operation
            jobs = []
            pops = {"count": 0}

            def capturing_popen(*args, **kwargs):
                pops["count"] += 1
                proc = subprocess.Popen(*args, **kwargs)
                original = proc.stdin.write

                def write(payload):
                    job = json.loads(payload.decode("utf-8").splitlines()[0])
                    jobs.append({
                        "mode": job["mode"],
                        "fake": job["fake"],
                        "api_key_empty": job["api_key"] == "",
                    })
                    return original(payload)

                proc.stdin.write = write
                return proc

            lines = iter(["first request", "second request", "exit"])
            output = []
            session = DisposableTerminalSession(
                fake="ok",
                context_root=write_disposable_context(database.parent / "terminal-context"),
                popen=capturing_popen,
            )
            reason = run_disposable_terminal(
                session,
                read_line=lambda _prompt: next(lines),
                write_line=output.append,
            )
            conversation = Conversation.objects.get()
            print(json.dumps({
                "reason": reason,
                "output": output,
                "workers": pops["count"],
                "jobs": jobs,
                "keys": session.idempotency_keys,
                "conversation_id": str(session.conversation_id),
                "owner_id": session.owner_id,
                "expected_version": session.expected_version,
                "stored_conversation": str(conversation.id),
                "stored_owner": conversation.owner_id,
                "stored_version": conversation.version,
                "conversations": Conversation.objects.count(),
                "operations": Operation.objects.count(),
                "messages": [
                    row.content
                    for row in Message.objects.order_by("sequence")
                ],
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["reason"], "exit")
        self.assertEqual(payload["workers"], 2)
        self.assertEqual(payload["conversations"], 1)
        self.assertEqual(payload["operations"], 2)
        self.assertEqual(payload["conversation_id"], payload["stored_conversation"])
        self.assertEqual(payload["owner_id"], payload["stored_owner"])
        self.assertEqual(payload["expected_version"], 2)
        self.assertEqual(payload["stored_version"], 2)
        self.assertEqual(len(payload["keys"]), 2)
        self.assertNotEqual(payload["keys"][0], payload["keys"][1])
        self.assertEqual(payload["messages"], [
            "first request",
            "answer",
            "second request",
            "answer",
        ])
        self.assertEqual(payload["output"].count("Assistant:"), 2)
        self.assertEqual(payload["output"].count("answer"), 2)
        self.assertEqual(payload["jobs"], [
            {"mode": "fake", "fake": "ok", "api_key_empty": True},
            {"mode": "fake", "fake": "ok", "api_key_empty": True},
        ])

    def test_blank_input_and_exit_do_not_call_provider(self):
        result, _database = _run(
            """\
            import json
            import os

            from disposable_terminal import (
                DisposableTerminalSession,
                activate_disposable_database,
                create_fresh_disposable_database,
                run_disposable_terminal,
                write_disposable_context,
            )

            database = create_fresh_disposable_database(os.environ["DISPOSABLE_TERMINAL_DATABASE"])
            activate_disposable_database(database)
            from max_smart_db.models import Conversation, Operation
            calls = {"count": 0}

            def popen(*_args, **_kwargs):
                calls["count"] += 1
                raise AssertionError("provider process started")

            lines = iter(["", "   ", "exit", "after-exit"])
            output = []
            session = DisposableTerminalSession(
                fake="ok",
                context_root=write_disposable_context(database.parent / "terminal-context"),
                popen=popen,
            )
            reason = run_disposable_terminal(
                session,
                read_line=lambda _prompt: next(lines),
                write_line=output.append,
            )
            print(json.dumps({
                "reason": reason,
                "output": output,
                "calls": calls["count"],
                "keys": session.idempotency_keys,
                "conversations": Conversation.objects.count(),
                "operations": Operation.objects.count(),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["reason"], "exit")
        self.assertEqual(payload["calls"], 0)
        self.assertEqual(payload["keys"], [])
        self.assertEqual(payload["conversations"], 0)
        self.assertEqual(payload["operations"], 0)
        self.assertIn("Goodbye.", payload["output"])
        self.assertNotIn("Assistant:", payload["output"])

    def test_recorded_failure_does_not_retry_or_continue_without_identity(self):
        result, _database = _run(
            """\
            import json
            import os
            import subprocess
            from pathlib import Path

            from disposable_terminal import (
                DisposableTerminalSession,
                activate_disposable_database,
                create_fresh_disposable_database,
                run_disposable_terminal,
                write_disposable_context,
            )

            database = create_fresh_disposable_database(os.environ["DISPOSABLE_TERMINAL_DATABASE"])
            activate_disposable_database(database)
            from max_smart_db.models import Conversation, Message, Operation
            pops = {"count": 0}

            def capturing_popen(*args, **kwargs):
                pops["count"] += 1
                proc = subprocess.Popen(*args, **kwargs)
                original = proc.stdin.write

                def write(payload):
                    return original(payload)

                proc.stdin.write = write
                return proc

            lines = iter(["blank output", "must-not-submit"])
            reads = {"count": 0}

            def read_line(_prompt):
                reads["count"] += 1
                return next(lines)

            output = []
            session = DisposableTerminalSession(
                fake="blank",
                context_root=write_disposable_context(database.parent / "terminal-context"),
                popen=capturing_popen,
            )
            reason = run_disposable_terminal(
                session,
                read_line=read_line,
                write_line=output.append,
            )
            print(json.dumps({
                "reason": reason,
                "output": output,
                "reads": reads["count"],
                "workers": pops["count"],
                "keys": len(session.idempotency_keys),
                "conversation_id": None if session.conversation_id is None else str(session.conversation_id),
                "version": list(Conversation.objects.values_list("version", flat=True)),
                "messages": Message.objects.count(),
                "operations": list(Operation.objects.values_list("status", flat=True)),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["reason"], "refused")
        self.assertEqual(payload["reads"], 1)
        self.assertEqual(payload["workers"], 1)
        self.assertEqual(payload["keys"], 1)
        self.assertIsNone(payload["conversation_id"])
        self.assertEqual(payload["output"], [
            "Disposable development Terminal. Type a message, or type exit to quit.",
            "Refused: DurableFailureRecorded",
        ])
        self.assertNotIn("Assistant:", payload["output"])
        self.assertEqual(payload["version"], [0])
        self.assertEqual(payload["messages"], 0)
        self.assertEqual(payload["operations"], ["FAILED"])

    def test_refusal_policy_stops_uncertain_results_without_retry(self):
        result, _database = _run(
            """\
            import json
            import os
            from uuid import uuid4

            from disposable_terminal import (
                DisposableTerminalSession,
                activate_disposable_database,
                create_fresh_disposable_database,
                refusal_ends_session,
                run_disposable_terminal,
            )

            activate_disposable_database(
                create_fresh_disposable_database(os.environ["DISPOSABLE_TERMINAL_DATABASE"])
            )
            import terminal_durable_turn
            from terminal_durable_turn import (
                DisposableTerminalTurnCompleted,
                DisposableTerminalTurnRefused,
            )

            policy = {
                code: {
                    "before": refusal_ends_session(code, conversation_established=False),
                    "after": refusal_ends_session(code, conversation_established=True),
                }
                for code in (
                    "OutcomeUnconfirmed",
                    "version_unproven",
                    "repository_integrity",
                    "DurableFailureRecorded",
                    "StaleVersion",
                )
            }
            calls = []

            def submit(**kwargs):
                calls.append(kwargs["request_content"])
                if len(calls) == 1:
                    return DisposableTerminalTurnCompleted(
                        assistant_text="answer",
                        conversation_id=uuid4(),
                        next_expected_version=1,
                        operation_id=uuid4(),
                        owner_id=7,
                    )
                if kwargs["request_content"] == "failed turn":
                    return DisposableTerminalTurnRefused("DurableFailureRecorded")
                return DisposableTerminalTurnRefused("OutcomeUnconfirmed")

            terminal_durable_turn.submit_disposable_terminal_turn = submit
            lines = iter(["first", "failed turn", "after failure", "after uncertain"])
            output = []
            session = DisposableTerminalSession(fake="ok", context_root="unused")
            reason = run_disposable_terminal(
                session,
                read_line=lambda _prompt: next(lines),
                write_line=output.append,
            )
            print(json.dumps({
                "policy": policy,
                "reason": reason,
                "calls": calls,
                "keys": len(session.idempotency_keys),
                "version": session.expected_version,
                "owner_id": session.owner_id,
                "output": output,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertTrue(payload["policy"]["OutcomeUnconfirmed"]["before"])
        self.assertTrue(payload["policy"]["OutcomeUnconfirmed"]["after"])
        self.assertTrue(payload["policy"]["version_unproven"]["after"])
        self.assertTrue(payload["policy"]["repository_integrity"]["after"])
        self.assertTrue(payload["policy"]["StaleVersion"]["after"])
        self.assertTrue(payload["policy"]["DurableFailureRecorded"]["before"])
        self.assertFalse(payload["policy"]["DurableFailureRecorded"]["after"])
        self.assertEqual(payload["reason"], "refused")
        self.assertEqual(payload["calls"], ["first", "failed turn", "after failure"])
        self.assertEqual(payload["calls"].count("failed turn"), 1)
        self.assertEqual(payload["keys"], 3)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["owner_id"], 7)
        self.assertEqual(payload["output"].count("answer"), 1)
        self.assertIn("Refused: OutcomeUnconfirmed", payload["output"])

    def test_unexpected_error_remains_visible(self):
        result, _database = _run(
            """\
            import os

            from disposable_terminal import (
                DisposableTerminalSession,
                activate_disposable_database,
                create_fresh_disposable_database,
                run_disposable_terminal,
            )

            activate_disposable_database(
                create_fresh_disposable_database(os.environ["DISPOSABLE_TERMINAL_DATABASE"])
            )
            import terminal_durable_turn

            def explode(**_kwargs):
                raise RuntimeError("visible-defect")

            terminal_durable_turn.submit_disposable_terminal_turn = explode
            output = []
            session = DisposableTerminalSession(fake="ok", context_root="unused")
            try:
                run_disposable_terminal(
                    session,
                    read_line=lambda _prompt: "hello",
                    write_line=output.append,
                )
            except RuntimeError as error:
                print(f"{error}|{output}")
            else:
                raise SystemExit("error-hidden")
            """
        )
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
        self.assertIn("visible-defect", result.stdout)
        self.assertNotIn("Assistant:", result.stdout)

    def test_missing_fake_and_unsuitable_paths_fail_before_mutation(self):
        missing, missing_database = _run(
            """\
            import os
            import sys
            from disposable_terminal import main

            database = os.environ["DISPOSABLE_TERMINAL_DATABASE"]
            os.environ["MAX_SMART_TEST_DATABASE_PATH"] = database + ".env"
            try:
                main(["--database", database, "--fake", ""])
            except SystemExit as error:
                print(f"{os.path.exists(database)}|{os.path.exists(database + '.env')}", file=sys.stderr)
                raise SystemExit(0)
            raise SystemExit("missing-fake-created-database")
            """
        )
        self.assertEqual(missing.returncode, 0, missing.stdout + "\n" + missing.stderr)
        self.assertIn("fake_provider_required", missing.stderr)
        self.assertIn("False|False", missing.stderr)
        self.assertFalse(missing_database.exists())

        production, _database = _run(
            """\
            import os
            from pathlib import Path
            from disposable_terminal import main

            production = Path(os.environ["PROJECT_DATABASE"])
            try:
                main(["--database", str(production), "--fake", "ok"])
            except SystemExit:
                print(str(production.exists()).lower())
                raise SystemExit(0)
            raise SystemExit("production-path-accepted")
            """,
            environment_updates={"PROJECT_DATABASE": str(PRODUCTION_DATABASE)},
        )
        self.assertEqual(production.returncode, 0, production.stdout + "\n" + production.stderr)
        self.assertIn("test_path_in_repository", production.stderr)
        self.assertIn("false", production.stdout)
        self.assertFalse(PRODUCTION_DATABASE.exists())

        existing, existing_database = _run(
            """\
            import os
            from pathlib import Path
            from disposable_terminal import main

            database = Path(os.environ["DISPOSABLE_TERMINAL_DATABASE"])
            database.parent.mkdir(mode=0o700)
            database.write_bytes(b"keep")
            before = database.read_bytes()
            try:
                main(["--database", str(database), "--fake", "ok"])
            except SystemExit as error:
                print(str(database.read_bytes() == before).lower())
                raise SystemExit(0)
            raise SystemExit("existing-database-mutated")
            """
        )
        self.assertEqual(existing.returncode, 0, existing.stdout + "\n" + existing.stderr)
        self.assertIn("database_initialization_artifact_exists", existing.stderr)
        self.assertIn("true", existing.stdout)
        self.assertFalse(existing_database.exists())

    def test_non_test_settings_refuse_before_any_filesystem_mutation(self):
        result, database = _run(
            """\
            import json
            import os
            import subprocess
            from pathlib import Path
            from disposable_terminal import main

            database = Path(os.environ["DISPOSABLE_TERMINAL_DATABASE"])
            calls = {"count": 0}
            original = subprocess.Popen

            def popen(*args, **kwargs):
                calls["count"] += 1
                return original(*args, **kwargs)

            subprocess.Popen = popen
            try:
                main(["--database", str(database), "--fake", "ok"])
            except SystemExit:
                artifacts = [
                    database,
                    Path(f"{database}-wal"),
                    Path(f"{database}-shm"),
                    Path(f"{database}-journal"),
                    Path(f"{database}.initialization-state"),
                    Path(f"{database}.initialization-state.tmp"),
                    database.parent / "provider.fence",
                ]
                print(json.dumps({
                    "parent": database.parent.exists(),
                    "artifacts": [path.exists() for path in artifacts],
                    "workers": calls["count"],
                }))
                raise SystemExit(0)
            raise SystemExit("non-test-settings-accepted")
            """,
            environment_updates={
                "DJANGO_SETTINGS_MODULE": "max_smart_db.production_settings",
            },
        )
        payload = _payload(self, result)
        self.assertIn("settings_refused", result.stderr)
        self.assertFalse(payload["parent"])
        self.assertEqual(payload["artifacts"], [False] * 7)
        self.assertEqual(payload["workers"], 0)
        self.assertFalse(database.exists())

    def test_relative_database_path_is_refused_without_mutation(self):
        result, _database = _run(
            """\
            import json
            from pathlib import Path
            from disposable_terminal import main

            database = Path("relative-disposable.sqlite3")
            artifacts = [
                database,
                Path(f"{database}.initialization-state"),
                Path(f"{database}.initialization-state.tmp"),
            ]
            try:
                main(["--database", str(database), "--fake", "ok"])
            except SystemExit:
                print(json.dumps([path.exists() for path in artifacts]))
                raise SystemExit(0)
            raise SystemExit("relative-path-accepted")
            """
        )
        payload = _payload(self, result)
        self.assertIn("test_path_not_absolute", result.stderr)
        self.assertEqual(payload, [False, False, False])

    def test_symlinked_parent_is_refused_without_mutation(self):
        result, _database = _run(
            """\
            import json
            import os
            from pathlib import Path
            from disposable_terminal import main

            base = Path(os.environ["DISPOSABLE_TERMINAL_DATABASE"]).parent.parent
            real_parent = base / "real-parent"
            linked_parent = base / "linked-parent"
            real_parent.mkdir(mode=0o700)
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            database = linked_parent / "disposable.sqlite3"
            resolved_database = real_parent / database.name
            artifacts = [
                resolved_database,
                Path(f"{resolved_database}.initialization-state"),
                Path(f"{resolved_database}.initialization-state.tmp"),
                real_parent / "provider.fence",
            ]
            before = {
                "link_inode": linked_parent.lstat().st_ino,
                "target_inode": real_parent.stat().st_ino,
            }
            try:
                main(["--database", str(database), "--fake", "ok"])
            except SystemExit:
                after = {
                    "link_inode": linked_parent.lstat().st_ino,
                    "target_inode": real_parent.stat().st_ino,
                }
                print(json.dumps({
                    "artifacts": [path.exists() for path in artifacts],
                    "metadata_unchanged": before == after,
                }))
                raise SystemExit(0)
            raise SystemExit("symlink-parent-accepted")
            """
        )
        payload = _payload(self, result)
        self.assertIn("path_symlink", result.stderr)
        self.assertEqual(payload["artifacts"], [False] * 4)
        self.assertTrue(payload["metadata_unchanged"])

    def test_explicit_command_exits_without_provider_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary).resolve() / "private" / "disposable.sqlite3"
            result = subprocess.run(
                (
                    sys.executable,
                    "-B",
                    "-m",
                    "disposable_terminal",
                    "--database",
                    str(database),
                    "--fake",
                    "ok",
                ),
                cwd=PROJECT_ROOT,
                env=_child_environment(
                    MAX_SMART_TEST_DATABASE_PATH=str(PRODUCTION_DATABASE),
                ),
                input="exit\n",
                capture_output=True,
                text=True,
                check=False,
                timeout=90,
            )
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
        self.assertIn("Goodbye.", result.stdout)
        self.assertNotIn("Assistant:", result.stdout)
        self.assertFalse(PRODUCTION_DATABASE.exists())

    def test_live_terminal_is_not_used(self):
        source = (SRC_ROOT / "disposable_terminal.py").read_text(encoding="utf-8")
        for banned in (
            "current_session",
            "OpenAI",
            "operator_commands",
            "max_smart.py",
            "load_dotenv",
        ):
            self.assertNotIn(banned, source)
        for name in ("max_smart.py", "terminal.py"):
            live = (SRC_ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("disposable_terminal", live)
            self.assertNotIn("submit_disposable_terminal_turn", live)
