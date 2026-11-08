"""Django app configuration for database connection safeguards."""

from django.apps import AppConfig


class MaxSmartDatabaseConfig(AppConfig):
    name = "max_smart_db"
    verbose_name = "Max Smart database foundation"

    def ready(self):
        from . import connections  # noqa: F401
