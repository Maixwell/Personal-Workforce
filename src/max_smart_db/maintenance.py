"""One-step exclusive maintenance for the approved V1 migration boundaries."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .config import PRODUCTION_DATABASE_PATH
from .safety import close_owned_resource
from .sql_policy import MigrationInventory, scan_migration_package


class MaintenanceError(RuntimeError):
    """Maintenance cannot make one approved boundary transition."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Boundary:
    name: str
    migrations: tuple[str, ...]


@dataclass(frozen=True)
class Transition:
    before: str
    after: str
    direction: str
    migration: str


_APP_LABEL = "max_smart_db"
_MIGRATIONS = (
    "0001_v1_schema",
    "0002_v1_integrity_triggers",
    "0003_v1_seeds_and_retention",
)
_TABLES = (
    "authorized_identity",
    "role",
    "conversation",
    "operation",
    "message",
    "idempotency_key",
    "global_model_slot",
)
_EXPLICIT_INDEXES = (
    "msv1_ai_one_enabled_uq",
    "msv1_conv_owner_updated_ix",
    "msv1_conv_owner_archived_ix",
    "msv1_conv_role_ix",
    "msv1_op_owner_ix",
    "msv1_op_conv_created_ix",
    "msv1_op_conv_status_ix",
    "msv1_op_created_ix",
    "msv1_op_status_resv_ix",
    "msv1_op_status_lease_ix",
    "msv1_op_conv_block_uq",
    "msv1_op_global_live_uq",
    "msv1_op_retry_once_uq",
    "msv1_msg_op_role_uq",
    "msv1_idem_conv_ix",
    "msv1_idem_expires_ix",
)
_AUTOMATIC_INDEXES = {
    "sqlite_autoindex_authorized_identity_1": "authorized_identity",
    "sqlite_autoindex_authorized_identity_2": "authorized_identity",
    "sqlite_autoindex_role_1": "role",
    "sqlite_autoindex_conversation_1": "conversation",
    "sqlite_autoindex_operation_1": "operation",
    "sqlite_autoindex_message_1": "message",
    "sqlite_autoindex_message_2": "message",
    "sqlite_autoindex_idempotency_key_1": "idempotency_key",
    "sqlite_autoindex_idempotency_key_2": "idempotency_key",
    "sqlite_autoindex_global_model_slot_1": "global_model_slot",
}
_INTEGRITY_TRIGGERS = (
    "msv1_ai_bu_immut_trg",
    "msv1_role_bu_immut_trg",
    "msv1_conv_bu_immut_trg",
    "msv1_op_bi_owner_match_trg",
    "msv1_op_bi_retry_valid_trg",
    "msv1_op_bu_immut_trg",
    "msv1_msg_bi_op_conv_trg",
    "msv1_msg_bu_immut_trg",
    "msv1_idem_bi_binding_trg",
    "msv1_idem_bi_no_replace_trg",
    "msv1_idem_bu_immut_trg",
)
_RETENTION_TRIGGERS = (
    "msv1_ai_bd_no_delete_trg",
    "msv1_role_bd_no_delete_trg",
    "msv1_conv_bd_no_delete_trg",
    "msv1_op_bd_no_delete_trg",
    "msv1_msg_bd_no_delete_trg",
    "msv1_slot_bd_no_delete_trg",
)
_BOUNDARIES = (
    Boundary("B0", ()),
    Boundary("B1", _MIGRATIONS[:1]),
    Boundary("B2", _MIGRATIONS[:2]),
    Boundary("B3", _MIGRATIONS),
)


def _exact_rows(cursor, statement, parameters=()):
    cursor.execute(statement, parameters)
    rows = cursor.fetchall()
    if not isinstance(rows, list) or any(
        not isinstance(row, tuple) for row in rows
    ):
        raise MaintenanceError(
            "query_result_invalid",
            "A maintenance verification query returned an invalid result.",
        )
    return rows


def _sqlite_objects(connection):
    with connection.cursor() as cursor:
        rows = _exact_rows(
            cursor,
            "SELECT type, name, tbl_name, sql "
            "FROM sqlite_schema ORDER BY type, name;",
        )
    objects = {}
    for row in rows:
        if (
            len(row) != 4
            or not all(isinstance(value, str) for value in row[:3])
            or (row[3] is not None and not isinstance(row[3], str))
        ):
            raise MaintenanceError(
                "schema_result_invalid",
                "SQLite schema inspection returned an invalid result.",
            )
        object_type, name, table, sql = row
        if (
            name in _TABLES
            or table in _TABLES
            or name.startswith("msv1_")
        ):
            if name in objects:
                raise MaintenanceError(
                    "schema_inventory_invalid",
                    "The V1 schema inventory contains a duplicate name.",
                )
            objects[name] = (object_type, table, sql)
    return objects, {row[1] for row in rows if len(row) == 4}


def _object_details(statement):
    header = re.match(
        r"CREATE (?:UNIQUE )?(TABLE|INDEX|TRIGGER) ([a-z0-9_]+)\b",
        statement,
    )
    if header is None:
        raise MaintenanceError(
            "approved_inventory_invalid",
            "The approved SQL inventory has an invalid object header.",
        )
    object_type = header.group(1).lower()
    name = header.group(2)
    if object_type == "table":
        table = name
    elif object_type == "index":
        match = re.search(r"\bON ([a-z0-9_]+)\b", statement)
        table = match.group(1) if match is not None else None
    else:
        match = re.search(r"\bON ([a-z0-9_]+)\nFOR EACH ROW\b", statement)
        table = match.group(1) if match is not None else None
    if table is None:
        raise MaintenanceError(
            "approved_inventory_invalid",
            "The approved SQL inventory has no target table.",
        )
    return name, object_type, table


def _expected_objects(boundary, inventory):
    if boundary.name == "B0":
        return {}
    allowed_names = set(_TABLES) | set(_EXPLICIT_INDEXES)
    if boundary.name in {"B2", "B3"}:
        allowed_names.update(_INTEGRITY_TRIGGERS)
    if boundary.name == "B3":
        allowed_names.update(_RETENTION_TRIGGERS)

    expected = {}
    for name, statement in inventory.object_sql.items():
        if name not in allowed_names:
            continue
        object_name, object_type, table = _object_details(statement)
        expected[object_name] = (
            object_type,
            table,
            statement.removesuffix(";"),
        )
    for name, table in _AUTOMATIC_INDEXES.items():
        expected[name] = ("index", table, None)
    return expected


def _recorder_rows(connection, all_schema_names):
    if "django_migrations" not in all_schema_names:
        return ()
    with connection.cursor() as cursor:
        rows = _exact_rows(
            cursor,
            "SELECT name FROM django_migrations "
            "WHERE app = %s ORDER BY name;",
            (_APP_LABEL,),
        )
    if any(
        len(row) != 1 or not isinstance(row[0], str)
        for row in rows
    ):
        raise MaintenanceError(
            "migration_recorder_invalid",
            "The Max Smart migration recorder state is invalid.",
        )
    return tuple(row[0] for row in rows)


def _verify_database_integrity(connection):
    with connection.cursor() as cursor:
        integrity = _exact_rows(cursor, "PRAGMA integrity_check;")
        foreign_keys = _exact_rows(cursor, "PRAGMA foreign_key_check;")
    if integrity != [("ok",)]:
        raise MaintenanceError(
            "integrity_check_failed",
            "SQLite integrity verification failed.",
        )
    if foreign_keys:
        raise MaintenanceError(
            "foreign_key_check_failed",
            "SQLite foreign-key verification failed.",
        )


def _seed_state(connection, objects):
    if "role" not in objects or "global_model_slot" not in objects:
        return None, None
    with connection.cursor() as cursor:
        role_rows = _exact_rows(
            cursor,
            "SELECT slug, display_name, active FROM role ORDER BY slug;",
        )
        slot_rows = _exact_rows(
            cursor,
            "SELECT id, operation_id, runner_id, "
            "reservation_expires_at_us, lease_expires_at_us, acquired_at_us "
            "FROM global_model_slot ORDER BY id;",
        )
    return role_rows, slot_rows


def _seeds_match(boundary, role_rows, slot_rows):
    if boundary.name == "B0":
        return role_rows is None and slot_rows is None
    if boundary.name in {"B1", "B2"}:
        return role_rows == [] and slot_rows == []
    if (
        role_rows != [("max-smart", "Max Smart", 1)]
        or len(slot_rows) != 1
        or len(slot_rows[0]) != 6
        or type(slot_rows[0][0]) is not int
        or slot_rows[0][0] != 1
    ):
        return False
    return type(role_rows[0][2]) is int


def classify_boundary(connection, inventory):
    """Return B0–B3 only for an exact recorder/object/seed match."""
    _verify_database_integrity(connection)
    objects, all_schema_names = _sqlite_objects(connection)
    recorder = _recorder_rows(connection, all_schema_names)
    role_rows, slot_rows = _seed_state(connection, objects)
    for boundary in _BOUNDARIES:
        if (
            recorder == boundary.migrations
            and objects == _expected_objects(boundary, inventory)
            and _seeds_match(boundary, role_rows, slot_rows)
        ):
            return boundary
    raise MaintenanceError(
        "unrecognized_boundary",
        "The database is not an approved B0-B3 boundary; no repair was attempted.",
    )


def _require_forward_0003_empty(connection):
    with connection.cursor() as cursor:
        role = _exact_rows(
            cursor,
            "SELECT EXISTS (SELECT 1 FROM role LIMIT 1);",
        )
        slot = _exact_rows(
            cursor,
            "SELECT EXISTS (SELECT 1 FROM global_model_slot LIMIT 1);",
        )
    if role != [(0,)] or slot != [(0,)]:
        raise MaintenanceError(
            "forward_0003_not_empty",
            "Forward 0003 requires empty role and global_model_slot tables.",
        )


def _transition(boundary, direction):
    if direction == "forward":
        mapping = {
            "B0": ("0001_v1_schema", "B1"),
            "B1": ("0002_v1_integrity_triggers", "B2"),
            "B2": ("0003_v1_seeds_and_retention", "B3"),
        }
    elif direction == "reverse":
        mapping = {
            "B1": (None, "B0"),
            "B2": ("0001_v1_schema", "B1"),
            "B3": ("0002_v1_integrity_triggers", "B2"),
        }
    else:
        raise MaintenanceError(
            "direction_invalid",
            "Maintenance direction must be forward or reverse.",
        )
    if boundary.name not in mapping:
        raise MaintenanceError(
            "transition_unavailable",
            "No approved migration transition exists in that direction.",
        )
    target_name, expected_boundary = mapping[boundary.name]
    migration_name = (
        "0001_v1_schema"
        if direction == "reverse" and boundary.name == "B1"
        else (
            target_name
            if direction == "forward"
            else boundary.migrations[-1]
        )
    )
    return target_name, expected_boundary, migration_name


def _verify_plan(plan, migration_name, direction, before):
    app_steps = [
        (migration.name, backwards)
        for migration, backwards in plan
        if migration.app_label == _APP_LABEL
    ]
    expected = [(migration_name, direction == "reverse")]
    if app_steps != expected:
        raise MaintenanceError(
            "migration_plan_invalid",
            "The migration executor did not produce one approved V1 step.",
        )
    if any(backwards for migration, backwards in plan if migration.app_label != _APP_LABEL):
        raise MaintenanceError(
            "migration_plan_invalid",
            "Framework migrations may not be reversed by maintenance.",
        )
    if before != "B0" and any(
        migration.app_label != _APP_LABEL for migration, _ in plan
    ):
        raise MaintenanceError(
            "migration_plan_invalid",
            "Only the approved V1 migration may run at this boundary.",
        )


def _assert_connection_contract(connection):
    if getattr(connection, "alias", None) != "default":
        raise MaintenanceError(
            "database_alias_invalid",
            "Exclusive maintenance supports only the default database.",
        )
    settings_dict = connection.settings_dict
    if (
        settings_dict.get("ATOMIC_REQUESTS") is not False
        or settings_dict.get("CONN_MAX_AGE") != 0
        or settings_dict.get("OPTIONS", {}).get("transaction_mode")
        != "IMMEDIATE"
    ):
        raise MaintenanceError(
            "transaction_configuration_invalid",
            "The approved IMMEDIATE transaction configuration is required.",
        )


def _perform_one_step(connection, direction, inventory, executor_class):
    from django.db.utils import NotSupportedError

    _assert_connection_contract(connection)
    if connection.in_atomic_block:
        raise NotSupportedError(
            "Exclusive maintenance cannot run inside an outer transaction."
        )
    before = classify_boundary(connection, inventory)
    target_name, expected_name, migration_name = _transition(
        before,
        direction,
    )
    if direction == "forward" and before.name == "B2":
        _require_forward_0003_empty(connection)
    executor = executor_class(connection)
    if executor.connection is not connection:
        raise MaintenanceError(
            "migration_connection_invalid",
            "MigrationExecutor did not retain the owned connection.",
        )
    target = (_APP_LABEL, target_name)
    plan = executor.migration_plan([target])
    _verify_plan(plan, migration_name, direction, before.name)
    executor.migrate([target], plan=plan)
    after = classify_boundary(connection, inventory)
    if after.name != expected_name:
        raise MaintenanceError(
            "post_migration_boundary_invalid",
            "The completed migration did not reach its approved boundary.",
        )
    return Transition(
        before=before.name,
        after=after.name,
        direction=direction,
        migration=migration_name,
    )


def _require_production_settings(settings):
    if (
        os.environ.get("DJANGO_SETTINGS_MODULE") != "max_smart_db.settings"
        or getattr(settings, "MAX_SMART_DATABASE_KIND", None) != "production"
        or settings.DATABASES.get("default", {}).get("NAME")
        != str(PRODUCTION_DATABASE_PATH)
    ):
        raise MaintenanceError(
            "production_settings_required",
            "Exclusive maintenance requires the approved production settings.",
        )


def run_exclusive_maintenance(direction):
    """Scan first, then perform exactly one production boundary transition."""
    inventory = scan_migration_package()

    # These imports intentionally occur only after the AST scanner succeeds.
    from django.conf import settings
    from django.db import DEFAULT_DB_ALIAS, connections
    from django.db.migrations.executor import MigrationExecutor

    _require_production_settings(settings)
    if DEFAULT_DB_ALIAS != "default":
        raise MaintenanceError(
            "database_alias_invalid",
            "Django's default database alias is not approved.",
        )
    connection = connections[DEFAULT_DB_ALIAS]
    if connections[DEFAULT_DB_ALIAS] is not connection:
        raise MaintenanceError(
            "migration_connection_invalid",
            "The owned Django connection could not be identified.",
        )
    try:
        transition = _perform_one_step(
            connection,
            direction,
            inventory,
            MigrationExecutor,
        )
    except BaseException as primary_exception:
        close_owned_resource(connection, primary_exception)
        raise
    connection.close()
    return transition
