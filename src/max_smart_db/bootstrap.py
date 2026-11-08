"""Explicit Django bootstrap for future Terminal and web composition roots."""

import os

import django

from .safety import (
    DatabaseSafetyError,
    verify_production_database_environment,
)


DATABASE_SETTINGS_MODULE = "max_smart_db.settings"


def setup_database_django():
    """Configure database-only Django without opening or migrating SQLite."""
    configured = os.environ.get("DJANGO_SETTINGS_MODULE")
    if configured not in (None, DATABASE_SETTINGS_MODULE):
        raise DatabaseSafetyError(
            "settings_module_conflict",
            "A different Django settings module is already configured.",
        )
    verify_production_database_environment()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", DATABASE_SETTINGS_MODULE)
    django.setup()
