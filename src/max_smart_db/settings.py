"""Production database-only Django settings.

Importing these settings does not create, connect to, or migrate the database.
"""

from .config import PRODUCTION_DATABASE_PATH, database_configuration


DATABASES = {
    "default": database_configuration(PRODUCTION_DATABASE_PATH),
}
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "max_smart_db.apps.MaxSmartDatabaseConfig",
]
MAX_SMART_DATABASE_KIND = "production"
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
