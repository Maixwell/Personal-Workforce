"""Database-only settings for out-of-repository SQLite tests."""

import os
from pathlib import Path

from provider_fence import (
    FenceSafetyError,
    env_fence_config,
    fence_matches_config,
    prepare_disposable_fence,
    publish_shared_fence_config,
)

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
_FENCE_PATH = TEST_DATABASE_PATH.parent.resolve(strict=True) / "provider.fence"
_PUBLISHED_FENCE = env_fence_config()
if _PUBLISHED_FENCE is None:
    if _FENCE_PATH.exists():
        raise FenceSafetyError("fence_identity_untrusted")
    MAX_SMART_PROVIDER_FENCE_CONFIG = prepare_disposable_fence(_FENCE_PATH)
    publish_shared_fence_config(MAX_SMART_PROVIDER_FENCE_CONFIG)
else:
    if (
        type(_PUBLISHED_FENCE).__name__ != "ProviderFenceConfig"
        or _PUBLISHED_FENCE.path != str(_FENCE_PATH)
        or not fence_matches_config(_PUBLISHED_FENCE)
    ):
        raise FenceSafetyError("fence_identity_mismatch")
    MAX_SMART_PROVIDER_FENCE_CONFIG = _PUBLISHED_FENCE
MAX_SMART_PROVIDER_FENCE = MAX_SMART_PROVIDER_FENCE_CONFIG.path

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
