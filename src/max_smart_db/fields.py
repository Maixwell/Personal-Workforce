"""SQLite fields whose migration state must declare exact INTEGER storage."""

from django.db import models


class SQLiteInteger64Field(models.BigIntegerField):
    """Signed 64-bit Django integer represented as SQLite INTEGER."""

    def db_type(self, connection):
        return "integer"

    def rel_db_type(self, connection):
        return "integer"
