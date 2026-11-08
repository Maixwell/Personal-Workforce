"""Reject any Django connection that bypassed the Max Smart SQLite backend."""

from django.db.backends.signals import connection_created
from django.dispatch import receiver

from .backend.base import DatabaseWrapper
from .safety import DatabaseSafetyError, close_owned_resource


def discard_connection(connection, primary_exception):
    """Close and detach a connection so Django cannot reuse it."""
    raw_connection = connection.connection
    connection.connection = None
    if raw_connection is not None:
        close_owned_resource(raw_connection, primary_exception)


@receiver(connection_created, dispatch_uid="max_smart_db.require_safe_backend")
def require_safe_backend(sender, connection, **kwargs):
    if not isinstance(connection, DatabaseWrapper):
        failure = DatabaseSafetyError(
            "unsupported_database",
            "Only the approved Max Smart SQLite backend is supported.",
        )
        discard_connection(connection, failure)
        raise failure
