"""Read-only V1 bootstrap and ordinary-startup verification."""

from __future__ import annotations

from dataclasses import dataclass

from .maintenance import (
    MaintenanceError,
    _EXPLICIT_INDEXES,
    _INTEGRITY_TRIGGERS,
    _MIGRATIONS,
    _RETENTION_TRIGGERS,
    _TABLES,
    Boundary,
    _expected_objects,
    _sqlite_objects,
    classify_boundary,
)


class StartupIntegrityError(RuntimeError):
    """Readiness cannot be enabled without exposing row content."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StartupVerification:
    mode: str
    boundary: str
    scans: tuple[str, ...]


@dataclass(frozen=True)
class SchemaAllowlistVerification:
    framework: tuple[tuple[str, str, str], ...]
    v1_names: tuple[str, ...]


@dataclass(frozen=True)
class BoundaryDiagnostic:
    migration_counts: tuple[tuple[str, int | None], ...]
    unexpected_migration_count: int | None
    present_tables: tuple[str, ...]
    present_indexes: tuple[str, ...]
    present_triggers: tuple[str, ...]
    unexpected_object_counts: tuple[tuple[str, int], ...]
    integrity_check: str
    foreign_key_check: str
    role_seed: str
    slot_seed: str


_MIGRATION_NAMES = (
    "0001_v1_schema",
    "0002_v1_integrity_triggers",
    "0003_v1_seeds_and_retention",
)
_EXPECTED_INDEXES = frozenset(_EXPLICIT_INDEXES)
_EXPECTED_TRIGGERS = frozenset(
    (*_INTEGRITY_TRIGGERS, *_RETENTION_TRIGGERS)
)
# Owner-approved Package D framework inventory: exact type, name, tbl_name.
_APPROVED_FRAMEWORK_SCHEMA = {
    ("table", "auth_group", "auth_group"),
    ("table", "auth_group_permissions", "auth_group_permissions"),
    ("table", "auth_permission", "auth_permission"),
    ("table", "auth_user", "auth_user"),
    ("table", "auth_user_groups", "auth_user_groups"),
    ("table", "auth_user_user_permissions", "auth_user_user_permissions"),
    ("table", "django_content_type", "django_content_type"),
    ("table", "django_migrations", "django_migrations"),
    ("table", "sqlite_sequence", "sqlite_sequence"),
    ("index", "auth_group_permissions_group_id_b120cbf9", "auth_group_permissions"),
    (
        "index",
        "auth_group_permissions_group_id_permission_id_0cd325b0_uniq",
        "auth_group_permissions",
    ),
    ("index", "auth_group_permissions_permission_id_84c5c92e", "auth_group_permissions"),
    ("index", "auth_permission_content_type_id_2f476e4b", "auth_permission"),
    (
        "index",
        "auth_permission_content_type_id_codename_01ab375a_uniq",
        "auth_permission",
    ),
    ("index", "auth_user_groups_group_id_97559544", "auth_user_groups"),
    ("index", "auth_user_groups_user_id_6a12ed8b", "auth_user_groups"),
    (
        "index",
        "auth_user_groups_user_id_group_id_94350c0c_uniq",
        "auth_user_groups",
    ),
    (
        "index",
        "auth_user_user_permissions_permission_id_1fbb5f2c",
        "auth_user_user_permissions",
    ),
    (
        "index",
        "auth_user_user_permissions_user_id_a95ead1b",
        "auth_user_user_permissions",
    ),
    (
        "index",
        "auth_user_user_permissions_user_id_permission_id_14a6b632_uniq",
        "auth_user_user_permissions",
    ),
    (
        "index",
        "django_content_type_app_label_model_76bd3d3b_uniq",
        "django_content_type",
    ),
    ("index", "sqlite_autoindex_auth_group_1", "auth_group"),
    ("index", "sqlite_autoindex_auth_user_1", "auth_user"),
}
_APPROVED_FRAMEWORK_BY_NAME = {
    name: (object_type, table)
    for object_type, name, table in _APPROVED_FRAMEWORK_SCHEMA
}

_SLOT_CORRESPONDENCE_SQL = """\
SELECT EXISTS (
    SELECT 1
    FROM global_model_slot AS slot
    WHERE slot.id = 1
      AND (
          (
              slot.operation_id IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM operation AS live
                  WHERE live.status IN ('QUEUED', 'RUNNING')
              )
          )
          OR
          (
              slot.operation_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM operation AS live
                  WHERE live.id = slot.operation_id
                    AND live.status IN ('QUEUED', 'RUNNING')
                    AND live.runner_id IS slot.runner_id
                    AND (
                        (
                            live.status = 'QUEUED'
                            AND live.reservation_expires_at_us
                                IS slot.reservation_expires_at_us
                            AND slot.lease_expires_at_us IS NULL
                        )
                        OR
                        (
                            live.status = 'RUNNING'
                            AND live.lease_expires_at_us
                                IS slot.lease_expires_at_us
                            AND slot.reservation_expires_at_us IS NULL
                        )
                    )
              )
          )
      )
);"""

_OPERATION_OWNER_SQL = """\
SELECT EXISTS (
    SELECT 1
    FROM operation AS operation_row
    JOIN conversation AS conversation_row
      ON conversation_row.id = operation_row.conversation_id
    WHERE operation_row.owner_id IS NOT conversation_row.owner_id
);"""

_IDEMPOTENCY_BINDING_SQL = """\
SELECT EXISTS (
    SELECT 1
    FROM idempotency_key AS idempotency_row
    JOIN operation AS operation_row
      ON operation_row.id = idempotency_row.operation_id
    WHERE idempotency_row.owner_id IS NOT operation_row.owner_id
       OR idempotency_row.conversation_id IS NOT operation_row.conversation_id
       OR idempotency_row.operation_type IS NOT operation_row.operation_type
       OR idempotency_row.expected_version IS NOT operation_row.expected_version
       OR idempotency_row.fingerprint_version
            IS NOT operation_row.fingerprint_version
       OR idempotency_row.request_hash IS NOT operation_row.request_hash
);"""

_MESSAGE_STATUS_SQL = """\
SELECT EXISTS (
    SELECT 1
    FROM operation AS operation_row
    WHERE (
        operation_row.status = 'SUCCEEDED'
        AND NOT (
            (SELECT COUNT(*) FROM message
             WHERE operation_id = operation_row.id) = 2
            AND
            (SELECT COUNT(*) FROM message
             WHERE operation_id = operation_row.id AND role = 'user') = 1
            AND
            (SELECT COUNT(*) FROM message
             WHERE operation_id = operation_row.id AND role = 'assistant') = 1
            AND
            abs(
                (SELECT sequence FROM message
                 WHERE operation_id = operation_row.id AND role = 'user')
                -
                (SELECT sequence FROM message
                 WHERE operation_id = operation_row.id AND role = 'assistant')
            ) = 1
        )
    )
    OR (
        operation_row.status <> 'SUCCEEDED'
        AND EXISTS (
            SELECT 1 FROM message
            WHERE operation_id = operation_row.id
        )
    )
    OR EXISTS (
        SELECT 1
        FROM message AS linked_message
        WHERE linked_message.operation_id = operation_row.id
          AND linked_message.conversation_id
                IS NOT operation_row.conversation_id
    )
);"""

_RETRY_ELIGIBILITY_SQL = """\
SELECT EXISTS (
    SELECT 1
    FROM operation AS retry
    JOIN operation AS source
      ON source.id = retry.retry_of
    WHERE retry.retry_of IS NOT NULL
      AND (
          retry.id = source.id
          OR retry.owner_id IS NOT source.owner_id
          OR retry.conversation_id IS NOT source.conversation_id
          OR source.blocks_conversation <> 0
          OR NOT (
              source.status IN ('FAILED', 'CANCELLED')
              OR (
                  source.status = 'OUTCOME_UNKNOWN'
                  AND source.acknowledged_at_us IS NOT NULL
              )
          )
      )
);"""

_RELATIONAL_SCANS = (
    ("slot_correspondence", _SLOT_CORRESPONDENCE_SQL),
    ("operation_owner", _OPERATION_OWNER_SQL),
    ("idempotency_binding", _IDEMPOTENCY_BINDING_SQL),
    ("message_status", _MESSAGE_STATUS_SQL),
    ("retry_eligibility", _RETRY_ELIGIBILITY_SQL),
)


def _exact_boolean_scalar(cursor):
    row = cursor.fetchone()
    extra = cursor.fetchone()
    if (
        extra is not None
        or not isinstance(row, tuple)
        or len(row) != 1
        or type(row[0]) is not int
        or row[0] not in (0, 1)
    ):
        return None
    return row[0]


def _require_b3(connection, inventory):
    try:
        boundary = classify_boundary(connection, inventory)
    except Exception as error:
        raise StartupIntegrityError(
            "boundary_invalid",
            "The database is not at an approved startup boundary.",
        ) from error
    if boundary.name != "B3":
        raise StartupIntegrityError(
            "boundary_not_ready",
            "Startup requires the complete B3 migration boundary.",
        )
    return boundary


def verify_approved_schema(connection, inventory):
    """Match the approved framework inventory and frozen V1 inventory separately."""
    try:
        objects, _schema_names = _sqlite_objects(connection)
    except MaintenanceError as error:
        raise StartupIntegrityError(
            "schema_result_invalid",
            "SQLite schema inspection returned an invalid result.",
        ) from error
    expected = _expected_objects(
        Boundary("B3", _MIGRATIONS),
        inventory,
    )
    if objects != expected:
        raise StartupIntegrityError(
            "v1_schema_invalid",
            "The frozen V1 schema inventory does not match.",
        )

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT type, name, tbl_name FROM sqlite_schema "
            "ORDER BY type, name;"
        )
        rows = cursor.fetchall()
    if not isinstance(rows, list) or any(
        not isinstance(row, tuple)
        or len(row) != 3
        or not all(isinstance(value, str) for value in row)
        for row in rows
    ):
        raise StartupIntegrityError(
            "schema_result_invalid",
            "SQLite schema inspection returned an invalid result.",
        )

    actual = {}
    for object_type, name, table in rows:
        if name in objects:
            continue
        if object_type not in {"table", "index"}:
            raise StartupIntegrityError(
                "framework_schema_type_rejected",
                "A framework schema object has an unapproved type.",
            )
        if name in actual:
            raise StartupIntegrityError(
                "schema_result_invalid",
                "SQLite schema inspection returned an invalid result.",
            )
        actual[name] = (object_type, table)

    if any(name not in _APPROVED_FRAMEWORK_BY_NAME for name in actual):
        raise StartupIntegrityError(
            "framework_schema_unexpected",
            "An unapproved framework schema object is present.",
        )
    if any(
        actual[name] != identity
        for name, identity in _APPROVED_FRAMEWORK_BY_NAME.items()
        if name in actual
    ):
        raise StartupIntegrityError(
            "framework_schema_mismatch",
            "A framework schema object does not match its approved association.",
        )
    if any(name not in actual for name in _APPROVED_FRAMEWORK_BY_NAME):
        raise StartupIntegrityError(
            "framework_schema_missing",
            "An approved framework schema object is missing.",
        )
    return SchemaAllowlistVerification(
        framework=tuple(sorted(
            (object_type, name, table)
            for name, (object_type, table) in actual.items()
        )),
        v1_names=tuple(sorted(objects)),
    )


def verify_fresh_bootstrap(connection, inventory):
    """Require B3 and exact free seeds without Decision 7 scans."""
    boundary = _require_b3(connection, inventory)
    verify_approved_schema(connection, inventory)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT slug, display_name, active FROM role ORDER BY slug;"
        )
        roles = cursor.fetchall()
        cursor.execute(
            "SELECT id, operation_id, runner_id, "
            "reservation_expires_at_us, lease_expires_at_us, acquired_at_us "
            "FROM global_model_slot ORDER BY id;"
        )
        slots = cursor.fetchall()
    if roles != [("max-smart", "Max Smart", 1)]:
        raise StartupIntegrityError(
            "role_seed_invalid",
            "The role seed does not match the approved bootstrap baseline.",
        )
    if slots != [(1, None, None, None, None, None)]:
        raise StartupIntegrityError(
            "slot_seed_invalid",
            "The slot seed does not match the approved free bootstrap baseline.",
        )
    return StartupVerification("fresh_bootstrap", boundary.name, ())


def verify_ordinary_startup(connection, inventory):
    """Require B3, permit an occupied slot, and run all five scans."""
    boundary = _require_b3(connection, inventory)
    verify_approved_schema(connection, inventory)
    completed = []
    with connection.cursor() as cursor:
        for name, statement in _RELATIONAL_SCANS:
            cursor.execute(statement)
            result = _exact_boolean_scalar(cursor)
            if result is None:
                raise StartupIntegrityError(
                    f"{name}_result_invalid",
                    "A startup integrity scan returned an invalid result.",
                )
            if result:
                raise StartupIntegrityError(
                    f"{name}_failed",
                    "A startup relational integrity requirement failed.",
                )
            completed.append(name)
    return StartupVerification(
        "ordinary_startup",
        boundary.name,
        tuple(completed),
    )


def _safe_query(connection, statement, parameters=()):
    try:
        with connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            rows = cursor.fetchall()
    except Exception:
        return None
    if not isinstance(rows, list) or any(
        not isinstance(row, tuple) for row in rows
    ):
        return None
    return rows


def boundary_diagnostics(connection):
    """Return only fixed names, counts, inventory and check status."""
    schema_rows = _safe_query(
        connection,
        "SELECT type, name, tbl_name FROM sqlite_schema "
        "ORDER BY type, name;",
    )
    schema_rows = schema_rows if schema_rows is not None else []
    schema_names = {
        row[1]
        for row in schema_rows
        if len(row) == 3 and isinstance(row[1], str)
    }

    migration_counts = []
    for name in _MIGRATION_NAMES:
        if "django_migrations" not in schema_names:
            migration_counts.append((name, 0))
            continue
        rows = _safe_query(
            connection,
            "SELECT COUNT(*) FROM django_migrations "
            "WHERE app = %s AND name = %s;",
            ("max_smart_db", name),
        )
        value = (
            rows[0][0]
            if rows is not None
            and len(rows) == 1
            and len(rows[0]) == 1
            and type(rows[0][0]) is int
            and rows[0][0] >= 0
            else None
        )
        migration_counts.append((name, value))

    if "django_migrations" not in schema_names:
        unexpected_migrations = 0
    else:
        rows = _safe_query(
            connection,
            "SELECT COUNT(*) FROM django_migrations "
            "WHERE app = 'max_smart_db' "
            "AND (name IS NULL OR name NOT IN "
            "('0001_v1_schema', '0002_v1_integrity_triggers', "
            "'0003_v1_seeds_and_retention'));",
        )
        unexpected_migrations = (
            rows[0][0]
            if rows is not None
            and len(rows) == 1
            and len(rows[0]) == 1
            and type(rows[0][0]) is int
            and rows[0][0] >= 0
            else None
        )

    present_tables = []
    present_indexes = []
    present_triggers = []
    unexpected = {"table": 0, "index": 0, "trigger": 0, "view": 0}
    for row in schema_rows:
        if (
            len(row) != 3
            or not all(isinstance(value, str) for value in row)
        ):
            continue
        object_type, name, table = row
        if object_type == "table" and name in _TABLES:
            present_tables.append(name)
        elif object_type == "index" and name in _EXPECTED_INDEXES:
            present_indexes.append(name)
        elif object_type == "trigger" and name in _EXPECTED_TRIGGERS:
            present_triggers.append(name)
        elif (
            name.startswith("msv1_")
            or table in _TABLES
            or name in _TABLES
        ):
            unexpected[object_type] = unexpected.get(object_type, 0) + 1

    integrity_rows = _safe_query(connection, "PRAGMA integrity_check;")
    foreign_key_rows = _safe_query(connection, "PRAGMA foreign_key_check;")
    integrity_status = (
        "ok"
        if integrity_rows == [("ok",)]
        else ("unreadable" if integrity_rows is None else "failed")
    )
    foreign_key_status = (
        "ok"
        if foreign_key_rows == []
        else ("unreadable" if foreign_key_rows is None else "failed")
    )

    role_rows = (
        _safe_query(
            connection,
            "SELECT slug, display_name, active FROM role ORDER BY slug;",
        )
        if "role" in schema_names
        else []
    )
    slot_rows = (
        _safe_query(
            connection,
            "SELECT id, operation_id, runner_id, "
            "reservation_expires_at_us, lease_expires_at_us, acquired_at_us "
            "FROM global_model_slot ORDER BY id;",
        )
        if "global_model_slot" in schema_names
        else []
    )
    if role_rows is None:
        role_status = "unreadable"
    elif role_rows == []:
        role_status = "absent"
    elif role_rows == [("max-smart", "Max Smart", 1)]:
        role_status = "present"
    else:
        role_status = "different"

    if slot_rows is None:
        slot_status = "unreadable"
    elif slot_rows == []:
        slot_status = "absent"
    elif slot_rows == [(1, None, None, None, None, None)]:
        slot_status = "present_free"
    elif (
        len(slot_rows) == 1
        and len(slot_rows[0]) == 6
        and type(slot_rows[0][0]) is int
        and slot_rows[0][0] == 1
    ):
        slot_status = "present_occupied"
    else:
        slot_status = "different"

    return BoundaryDiagnostic(
        migration_counts=tuple(migration_counts),
        unexpected_migration_count=unexpected_migrations,
        present_tables=tuple(sorted(present_tables)),
        present_indexes=tuple(sorted(present_indexes)),
        present_triggers=tuple(sorted(present_triggers)),
        unexpected_object_counts=tuple(sorted(unexpected.items())),
        integrity_check=integrity_status,
        foreign_key_check=foreign_key_status,
        role_seed=role_status,
        slot_seed=slot_status,
    )
