"""Django SQLite wrapper that rejects unsafe databases before Django sees them."""

from pathlib import Path

from django.conf import settings
from django.db.backends.sqlite3 import base as sqlite_base
from django.utils.asyncio import async_unsafe

from ..safety import (
    DatabaseSafetyError,
    close_owned_resource,
    verify_production_database_before_connect,
    verify_required_sqlite_pragmas,
    verify_sqlite_connection_version,
    verify_sqlite_module_version,
    verify_test_database_before_connect,
)


def verify_database_files(database_path):
    database_kind = getattr(settings, "MAX_SMART_DATABASE_KIND", None)
    if database_kind == "production":
        verify_production_database_before_connect(database_path)
    elif database_kind == "test":
        verify_test_database_before_connect(database_path)
    else:
        raise DatabaseSafetyError(
            "database_kind_invalid",
            "The database configuration kind is invalid.",
        )


def no_create_uri(database_path):
    """Return a read-write SQLite URI that fails instead of creating a file."""
    return f"{Path(database_path).as_uri()}?mode=rw"


def verify_sqlite_pragmas(raw_connection):
    """Read back all approved safeguards in their required order."""
    verify_required_sqlite_pragmas(raw_connection)


class DatabaseWrapper(sqlite_base.DatabaseWrapper):
    """Validate before opening; a rejected connection is never stored."""

    def get_connection_params(self):
        params = super().get_connection_params()
        params["database"] = no_create_uri(self.settings_dict["NAME"])
        params["uri"] = True
        return params

    @async_unsafe
    def get_new_connection(self, conn_params):
        database_path = Path(self.settings_dict["NAME"])
        module_version = verify_sqlite_module_version()
        verify_database_files(database_path)
        raw_connection = sqlite_base.Database.connect(**conn_params)
        try:
            # This must remain the first SQL issued on the raw handle.
            verify_sqlite_connection_version(raw_connection, module_version)
            verify_database_files(database_path)
            sqlite_base.register_functions(raw_connection)
            raw_connection.execute("PRAGMA foreign_keys = ON")
            raw_connection.execute("PRAGMA legacy_alter_table = OFF")
            for init_command in self.init_commands:
                if init_command := init_command.strip():
                    raw_connection.execute(init_command)
            raw_connection.execute("PRAGMA recursive_triggers = ON")
            verify_sqlite_pragmas(raw_connection)
        except BaseException as primary_exception:
            close_owned_resource(raw_connection, primary_exception)
            raise
        return raw_connection
