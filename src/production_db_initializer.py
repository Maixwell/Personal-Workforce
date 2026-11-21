"""Explicit command for initializing the fixed Max Smart production database.

This module is inactive unless invoked directly. It accepts no database path,
does not read environment configuration, and stops after SQLite initialization.

The challenge response is deliberate confirmation under the approved
operating-system account. It is not independent owner authentication.
"""

import argparse
import getpass
import hmac
import os
import pwd
import secrets
import stat
import sys
from pathlib import Path

from max_smart_db.config import PRODUCTION_DATABASE_PATH, PROJECT_ROOT
from max_smart_db.safety import DatabaseSafetyError, initialize_sqlite_file


_APPROVED_USER = "smart"
_APPROVED_HOME = Path("/Users/smart")
_APPROVED_PROJECT_ROOT = Path("/Users/smart/Projects/Personal Workforce")
_AUTHORIZATION_PREFIX = "INITIALIZE MAX SMART PRODUCTION DATABASE"


class ProductionInitializationRefused(RuntimeError):
    """The command refused before calling the production initializer."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _new_authorization_challenge():
    """Return one confirmation nonce for this invocation.

    The nonce is not an authentication secret and does not establish an
    identity beyond the approved operating-system account.
    """

    return secrets.token_urlsafe(24)


def _authorization_response(challenge):
    """Return the deliberate confirmation response for one challenge."""

    return f"{_AUTHORIZATION_PREFIX} {challenge}"


def _authorization_valid(response, challenge):
    """Validate one invocation-bound confirmation without logging it."""

    return (
        type(response) is str
        and type(challenge) is str
        and bool(challenge)
        and hmac.compare_digest(response, _authorization_response(challenge))
    )


def _verify_invocation_context():
    """Require the accepted account and real repository location."""

    try:
        identity = pwd.getpwuid(os.geteuid())
    except (KeyError, OSError) as error:
        raise ProductionInitializationRefused("account_identity_unavailable") from error
    if (
        identity.pw_name != _APPROVED_USER
        or Path(identity.pw_dir) != _APPROVED_HOME
    ):
        raise ProductionInitializationRefused("account_identity_refused")
    if (
        PROJECT_ROOT != _APPROVED_PROJECT_ROOT
        or PRODUCTION_DATABASE_PATH
        != _APPROVED_PROJECT_ROOT / "data" / "max_smart.sqlite3"
    ):
        raise ProductionInitializationRefused("repository_location_refused")
    try:
        resolved = PROJECT_ROOT.resolve(strict=True)
        status = PROJECT_ROOT.lstat()
    except OSError as error:
        raise ProductionInitializationRefused("repository_identity_unavailable") from error
    if (
        resolved != PROJECT_ROOT
        or stat.S_ISLNK(status.st_mode)
        or not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) & 0o022
    ):
        raise ProductionInitializationRefused("repository_identity_refused")


def initialize_approved_production_database(response, challenge):
    """Initialize only the accepted path after deliberate confirmation.

    Confirmation cannot select or redirect the database path.
    """

    if not _authorization_valid(response, challenge):
        raise ProductionInitializationRefused("authorization_refused")
    _verify_invocation_context()
    initialize_sqlite_file(PRODUCTION_DATABASE_PATH, production=True)


def main(argv=None):
    """Request deliberate confirmation, initialize the fixed path, and stop."""

    parser = argparse.ArgumentParser(prog="production_db_initializer")
    parser.parse_args(argv)
    try:
        _verify_invocation_context()
    except ProductionInitializationRefused as error:
        _fail(error.code)
    challenge = _new_authorization_challenge()
    print(
        "This is deliberate confirmation under the approved operating-system "
        "account, not independent owner authentication."
    )
    print(f"Confirmation challenge: {challenge}")
    try:
        response = getpass.getpass("Confirmation response: ")
    except (EOFError, KeyboardInterrupt):
        _fail("authorization_required")
    try:
        initialize_approved_production_database(response, challenge)
    except ProductionInitializationRefused as error:
        _fail(error.code)
    except DatabaseSafetyError as error:
        _fail(error.code)
    print("production_database_initialized")


def _fail(code):
    print(code, file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
