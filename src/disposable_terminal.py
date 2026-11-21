"""Explicit disposable development Terminal.

Run this module directly. It does not replace the live JSON Terminal.
"""

import argparse
import base64
import os
import secrets
import sys
from pathlib import Path

from max_smart_db.clock_safety import UtcMicrosecondClock
from max_smart_db.safety import (
    DatabaseSafetyError,
    initialize_sqlite_file,
    validate_test_database_path,
)


ACCEPTED_RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
_TEST_SETTINGS = "max_smart_db.test_settings"
_VERSION_PRESERVED = frozenset({
    "DurableFailureRecorded",
    "DurablePreProviderFailure",
    "Existing",
    "KeyBindingMismatch",
})


class DisposableTerminalSession:
    """Identity and version carried between explicit user messages."""

    def __init__(self, *, fake, context_root, clock=None, runner_id=ACCEPTED_RUNNER, popen=None):
        self.fake = fake
        self.context_root = context_root
        self.clock = UtcMicrosecondClock() if clock is None else clock
        self.runner_id = runner_id
        self.popen = popen
        self.conversation_id = None
        self.owner_id = None
        self.expected_version = 0
        self.idempotency_keys = []


def create_fresh_disposable_database(database_path):
    """Validate and initialize one new disposable database path.

    The path is the caller's explicit argument. An environment variable is
    not read and is not initialized.
    """

    database_path = Path(database_path)
    resolved = validate_test_database_path(database_path)
    if resolved != database_path:
        raise DatabaseSafetyError(
            "path_symlink",
            "The database path must not contain symbolic links.",
        )
    initialize_sqlite_file(database_path, production=False)
    return database_path


def require_disposable_test_settings():
    """Reject a preconfigured non-test settings module without mutation."""

    configured = os.environ.get("DJANGO_SETTINGS_MODULE")
    if configured not in (None, "", _TEST_SETTINGS):
        raise DatabaseSafetyError(
            "settings_refused",
            "Disposable Terminal requires the accepted test settings.",
        )


def activate_disposable_database(database_path):
    """Migrate one database just initialized by the accepted test procedure."""

    database_path = Path(database_path).resolve()
    require_disposable_test_settings()
    os.environ["DJANGO_SETTINGS_MODULE"] = _TEST_SETTINGS
    os.environ["MAX_SMART_TEST_DATABASE_PATH"] = str(database_path)
    import django
    from django.apps import apps
    from django.conf import settings

    if not apps.ready:
        django.setup()
    if getattr(settings, "MAX_SMART_DATABASE_KIND", None) != "test":
        raise DatabaseSafetyError(
            "database_not_disposable",
            "The configured database is not a disposable test database.",
        )
    configured_path = Path(settings.DATABASES["default"]["NAME"]).resolve()
    if configured_path != database_path:
        raise DatabaseSafetyError(
            "database_settings_mismatch",
            "Django is not using the freshly initialized disposable database.",
        )
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    return database_path


def write_disposable_context(context_root):
    """Write standing-context files beside the disposable database."""

    docs = Path(context_root) / "docs"
    docs.mkdir(mode=0o700, parents=True, exist_ok=True)
    (docs / "max-smart-instructions.md").write_text(
        "INSTRUCTIONS-BODY\n",
        encoding="utf-8",
    )
    (docs / "goals.md").write_text("GOALS-BODY\n", encoding="utf-8")
    (docs / "memory.md").write_text("MEMORY-BODY\n", encoding="utf-8")
    return Path(context_root)


def new_idempotency_key():
    """Return one new canonical 16-byte idempotency key."""

    return base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode("ascii")


def refusal_ends_session(code, *, conversation_established):
    """Return whether the loop must stop after this refusal.

    A recorded definite failure preserves the conversation version because
    only a successful apply advances it. Uncertain outcomes stop the session.
    """

    if not conversation_established:
        return True
    return code not in _VERSION_PRESERVED


def run_disposable_terminal(session, *, read_line, write_line):
    """Read explicit user messages until exit or an unsafe refusal."""

    if type(session.fake) is not str or session.fake == "":
        write_line("Refused: fake_provider_required")
        return "fake_provider_required"
    write_line("Disposable development Terminal. Type a message, or type exit to quit.")
    while True:
        try:
            line = read_line("You: ")
        except EOFError:
            return "eof"
        except KeyboardInterrupt:
            return "interrupt"
        if line is None:
            return "eof"
        if line.strip() == "":
            continue
        if line.strip().lower() == "exit":
            write_line("Goodbye.")
            return "exit"
        key = new_idempotency_key()
        session.idempotency_keys.append(key)
        arguments = {
            "clock": session.clock,
            "runner_id": session.runner_id,
            "fake": session.fake,
            "context_root": session.context_root,
            "request_content": line,
            "idempotency_key": key,
            "expected_version": session.expected_version,
        }
        if session.conversation_id is not None:
            arguments["conversation_id"] = session.conversation_id
            arguments["owner_id"] = session.owner_id
        if session.popen is not None:
            arguments["popen"] = session.popen
        import terminal_durable_turn

        result = terminal_durable_turn.submit_disposable_terminal_turn(**arguments)
        completed = terminal_durable_turn.DisposableTerminalTurnCompleted
        refused = terminal_durable_turn.DisposableTerminalTurnRefused
        if type(result) is completed:
            session.conversation_id = result.conversation_id
            session.owner_id = result.owner_id
            session.expected_version = result.next_expected_version
            write_line("Assistant:")
            write_line(result.assistant_text)
            continue
        if type(result) is not refused:
            raise RuntimeError("disposable_terminal_result_invalid")
        write_line(f"Refused: {result.code}")
        if refusal_ends_session(
            result.code,
            conversation_established=session.conversation_id is not None,
        ):
            return "refused"


def main(argv=None):
    """Initialize one explicit disposable database and run the loop."""

    parser = argparse.ArgumentParser(prog="disposable_terminal")
    parser.add_argument("--database", required=True)
    parser.add_argument("--fake", required=True)
    args = parser.parse_args(argv)
    if type(args.fake) is not str or args.fake == "":
        _fail("fake_provider_required")
    try:
        require_disposable_test_settings()
        database = create_fresh_disposable_database(args.database)
        activate_disposable_database(database)
    except DatabaseSafetyError as error:
        _fail(error.code)
    context_root = write_disposable_context(database.parent / "terminal-context")
    session = DisposableTerminalSession(fake=args.fake, context_root=context_root)
    run_disposable_terminal(session, read_line=input, write_line=print)


def _fail(code):
    print(code, file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
