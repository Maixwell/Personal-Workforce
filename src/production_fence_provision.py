"""Explicit command for the fixed Max Smart production fence and trust pin.

This module is inactive unless invoked directly. It accepts no fence or pin
path, does not read environment configuration, and does not create parent
directories. It does not publish a runtime fence identity, start a provider,
or initialize production storage.

Confirmation is the authorization event. The operator must return the receipt
binding exactly. A matching binding does not establish an identity beyond the
approved operating-system account, and it cannot redirect the path.
"""

import argparse
import getpass
import json
import os
import pwd
import stat
import sys
from pathlib import Path

from max_smart_db.config import PROJECT_ROOT
from provider_fence import (
    PRODUCTION_FENCE_PATH,
    FenceSafetyError,
    _APPROVED_PIN_DIRECTORY,
    accept_production_fence_receipt,
    begin_production_fence_provision,
    complete_production_fence_provision,
    require_production_fence_parents,
)


_APPROVED_USER = "smart"
_APPROVED_HOME = Path("/Users/smart")
_APPROVED_PROJECT_ROOT = Path("/Users/smart/Projects/Personal Workforce")
_APPROVED_FENCE_PATH = _APPROVED_PROJECT_ROOT / "data" / "provider.fence"


class ProductionFenceRefused(RuntimeError):
    """The command refused before creating a production fence artifact."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _verify_fixed_context():
    """Require the accepted account and the fixed repository location."""

    try:
        identity = pwd.getpwuid(os.geteuid())
    except (KeyError, OSError) as error:
        raise ProductionFenceRefused("account_identity_unavailable") from error
    if identity.pw_name != _APPROVED_USER or Path(identity.pw_dir) != _APPROVED_HOME:
        raise ProductionFenceRefused("account_identity_refused")
    if (
        PROJECT_ROOT != _APPROVED_PROJECT_ROOT
        or PRODUCTION_FENCE_PATH != _APPROVED_FENCE_PATH
        or _APPROVED_PIN_DIRECTORY
        != Path("/Users/smart/Library/Application Support/MaxSmartTrust")
    ):
        raise ProductionFenceRefused("repository_location_refused")
    try:
        status = _APPROVED_PROJECT_ROOT.lstat()
        resolved = _APPROVED_PROJECT_ROOT.resolve(strict=True)
    except OSError as error:
        raise ProductionFenceRefused("repository_identity_unavailable") from error
    if (
        resolved != _APPROVED_PROJECT_ROOT
        or stat.S_ISLNK(status.st_mode)
        or not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) & 0o022
    ):
        raise ProductionFenceRefused("repository_identity_refused")


def _binding_text(binding):
    """Return the canonical receipt binding the operator must retype."""

    return json.dumps(
        binding,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decision_from_response(response):
    """Parse one typed binding without treating it as a path selector."""

    if type(response) is not str:
        return {}
    try:
        decision = json.loads(response)
    except (UnicodeError, json.JSONDecodeError):
        return {}
    if type(decision) is not dict:
        return {}
    return decision


def provision_approved_production_fence(confirm):
    """Provision the fixed fence and pin after exact receipt confirmation.

    Confirmation cannot select or redirect either path. A missing parent,
    an existing artifact, or a failed child is left unchanged and is not
    retried by this function.
    """

    _verify_fixed_context()
    if not callable(confirm):
        raise ProductionFenceRefused("acceptance_confirmation_refused")
    require_production_fence_parents()
    process = begin_production_fence_provision()
    try:
        completion = complete_production_fence_provision(process)
    except Exception:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        raise
    return accept_production_fence_receipt(completion, confirm)


def _fail(code):
    print(f"production fence provisioning refused: {code}", file=sys.stderr)
    raise SystemExit(1)


def main(argv=None):
    """Confirm the fixed receipt binding, provision that chain, and stop."""

    parser = argparse.ArgumentParser(prog="production_fence_provision")
    parser.parse_args(argv)
    try:
        def confirm(binding):
            print(_binding_text(binding))
            response = getpass.getpass("Retype the receipt binding exactly: ")
            return _decision_from_response(response)

        provision_approved_production_fence(confirm)
    except (ProductionFenceRefused, FenceSafetyError) as error:
        _fail(error.code)


if __name__ == "__main__":
    main()
