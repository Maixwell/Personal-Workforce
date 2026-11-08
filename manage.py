#!/usr/bin/env python3
"""Explicit Django command bootstrap; commands never run at application startup."""

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

PRODUCTION_SETTINGS_MODULE = "max_smart_db.settings"
TEST_SETTINGS_MODULE = "max_smart_db.test_settings"
_REJECTED_OPTIONS = ("--settings", "--pythonpath")
# Public lifecycle commands remain disabled. M2.1b exposes only the bounded
# scanner-verified one-step maintenance command.
_ALLOWED_COMMANDS = frozenset(
    {
        "check",
        "exclusive_maintenance",
        "help",
        "version",
        "--help",
        "-h",
        "--version",
    }
)


def _refuse(code, message):
    print(f"Database preflight failed ({code}): {message}", file=sys.stderr)
    sys.exit(1)


def main(argv):
    from max_smart_db.safety import (
        DatabaseSafetyError,
        verify_production_database_environment,
    )

    for argument in argv[1:]:
        if any(
            argument == option or argument.startswith(f"{option}=")
            for option in _REJECTED_OPTIONS
        ):
            _refuse(
                "settings_override_rejected",
                "Select settings with DJANGO_SETTINGS_MODULE only.",
            )

    command = argv[1] if len(argv) > 1 else None
    if command not in _ALLOWED_COMMANDS:
        _refuse(
            "command_not_allowed",
            "This management command is not approved for public execution.",
        )

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", PRODUCTION_SETTINGS_MODULE)
    settings_module = os.environ["DJANGO_SETTINGS_MODULE"]
    if (
        command == "exclusive_maintenance"
        and settings_module != PRODUCTION_SETTINGS_MODULE
    ):
        _refuse(
            "production_settings_required",
            "Exclusive maintenance requires production settings.",
        )
    if settings_module == PRODUCTION_SETTINGS_MODULE:
        try:
            verify_production_database_environment()
        except DatabaseSafetyError as error:
            _refuse(error.code, str(error))
    elif settings_module != TEST_SETTINGS_MODULE:
        _refuse(
            "settings_module_conflict",
            "Only the approved database settings modules are supported.",
        )

    from django.core.management import execute_from_command_line

    execute_from_command_line(argv)


if __name__ == "__main__":
    main(sys.argv)
