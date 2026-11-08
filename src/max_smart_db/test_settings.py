"""Database-only settings for out-of-repository SQLite tests."""

import os
from pathlib import Path

from .config import database_configuration
from .safety import DatabaseSafetyError, validate_test_database_path


_test_database_value = os.environ.get("MAX_SMART_TEST_DATABASE_PATH")
if not _test_database_value:
    raise DatabaseSafetyError(
        "test_path_missing",
        "MAX_SMART_TEST_DATABASE_PATH is required for database tests.",
    )

TEST_DATABASE_PATH = Path(_test_database_value)
TEST_DATABASE_PATH = validate_test_database_path(TEST_DATABASE_PATH)

DATABASES = {
    "default": database_configuration(TEST_DATABASE_PATH),
}
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "max_smart_db.apps.MaxSmartDatabaseConfig",
]
MAX_SMART_DATABASE_KIND = "test"
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
