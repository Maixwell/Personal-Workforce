"""Install the frozen V1 seeds and retention triggers."""

from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError


ROLE_INSERT_SQL = """INSERT INTO role (
    slug,
    display_name,
    active
)
VALUES (
    'max-smart',
    'Max Smart',
    1
);"""
ROLE_DELETE_SQL = """DELETE FROM role
WHERE slug = 'max-smart'
  AND display_name = 'Max Smart'
  AND active = 1;"""
SLOT_INSERT_SQL = """INSERT INTO global_model_slot (
    id,
    operation_id,
    runner_id,
    reservation_expires_at_us,
    lease_expires_at_us,
    acquired_at_us
)
VALUES (
    1,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL
);"""
SLOT_DELETE_SQL = """DELETE FROM global_model_slot
WHERE id = 1
  AND operation_id IS NULL
  AND runner_id IS NULL
  AND reservation_expires_at_us IS NULL
  AND lease_expires_at_us IS NULL
  AND acquired_at_us IS NULL;"""
MIGRATION_0001_COUNT_SQL = "SELECT COUNT(*) FROM django_migrations WHERE app = 'max_smart_db' AND name = '0001_v1_schema';"
MIGRATION_0002_COUNT_SQL = "SELECT COUNT(*) FROM django_migrations WHERE app = 'max_smart_db' AND name = '0002_v1_integrity_triggers';"
MIGRATION_0003_COUNT_SQL = "SELECT COUNT(*) FROM django_migrations WHERE app = 'max_smart_db' AND name = '0003_v1_seeds_and_retention';"
UNEXPECTED_MIGRATION_COUNT_SQL = "SELECT COUNT(*) FROM django_migrations WHERE app = 'max_smart_db' AND (name IS NULL OR name NOT IN ('0001_v1_schema', '0002_v1_integrity_triggers', '0003_v1_seeds_and_retention'));"
AI_EMPTY_SQL = "SELECT EXISTS (SELECT 1 FROM authorized_identity LIMIT 1);"
CONVERSATION_EMPTY_SQL = "SELECT EXISTS (SELECT 1 FROM conversation LIMIT 1);"
OPERATION_EMPTY_SQL = "SELECT EXISTS (SELECT 1 FROM operation LIMIT 1);"
MESSAGE_EMPTY_SQL = "SELECT EXISTS (SELECT 1 FROM message LIMIT 1);"
IDEMPOTENCY_EMPTY_SQL = "SELECT EXISTS (SELECT 1 FROM idempotency_key LIMIT 1);"
ROLE_BASELINE_SQL = "SELECT slug, display_name, active FROM role ORDER BY slug;"
SLOT_BASELINE_SQL = "SELECT id, operation_id, runner_id, reservation_expires_at_us, lease_expires_at_us, acquired_at_us FROM global_model_slot ORDER BY id;"


def forward_noop(apps, schema_editor):
    return None


def seed_role_forward(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(ROLE_INSERT_SQL)


def seed_role_reverse(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(ROLE_DELETE_SQL)
        if cursor.rowcount != 1:
            raise IrreversibleError("role seed reversal did not affect exactly one row")


def seed_slot_forward(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(SLOT_INSERT_SQL)


def seed_slot_reverse(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(SLOT_DELETE_SQL)
        if cursor.rowcount != 1:
            raise IrreversibleError("global_model_slot seed reversal did not affect exactly one row")


def _exact_nonnegative_scalar(cursor):
    row = cursor.fetchone()
    extra = cursor.fetchone()
    if extra is not None or not isinstance(row, tuple) or len(row) != 1 or type(row[0]) is not int or row[0] < 0:
        return None
    return row[0]


def _record_named_count(messages, name, count):
    if count is None:
        messages.append(f"migration query returned an unexpected result: {name}")
    elif count == 0:
        messages.append(f"missing migration row: {name}")
    elif count > 1:
        messages.append(f"duplicate migration row: {name}")


def _record_emptiness(messages, table, value):
    if value == 1:
        messages.append(f"{table} is not empty")
    elif value != 0:
        messages.append(f"{table} emptiness query returned an unexpected result")


def guard_0003(apps, schema_editor):
    messages = []
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(MIGRATION_0001_COUNT_SQL)
        _record_named_count(messages, "0001_v1_schema", _exact_nonnegative_scalar(cursor))
        cursor.execute(MIGRATION_0002_COUNT_SQL)
        _record_named_count(messages, "0002_v1_integrity_triggers", _exact_nonnegative_scalar(cursor))
        cursor.execute(MIGRATION_0003_COUNT_SQL)
        _record_named_count(messages, "0003_v1_seeds_and_retention", _exact_nonnegative_scalar(cursor))
        cursor.execute(UNEXPECTED_MIGRATION_COUNT_SQL)
        unexpected = _exact_nonnegative_scalar(cursor)
        if unexpected is None:
            messages.append("migration query returned an unexpected result: max_smart_db")
        elif unexpected > 0:
            messages.append("unexpected migration row: max_smart_db")
        cursor.execute(AI_EMPTY_SQL)
        _record_emptiness(messages, "authorized_identity", _exact_nonnegative_scalar(cursor))
        cursor.execute(CONVERSATION_EMPTY_SQL)
        _record_emptiness(messages, "conversation", _exact_nonnegative_scalar(cursor))
        cursor.execute(OPERATION_EMPTY_SQL)
        _record_emptiness(messages, "operation", _exact_nonnegative_scalar(cursor))
        cursor.execute(MESSAGE_EMPTY_SQL)
        _record_emptiness(messages, "message", _exact_nonnegative_scalar(cursor))
        cursor.execute(IDEMPOTENCY_EMPTY_SQL)
        _record_emptiness(messages, "idempotency_key", _exact_nonnegative_scalar(cursor))
        cursor.execute(ROLE_BASELINE_SQL)
        role_rows = cursor.fetchall()
        if not isinstance(role_rows, list) or any(not isinstance(row, tuple) or len(row) != 3 for row in role_rows):
            messages.append("role seed query returned an unexpected result")
        elif role_rows != [("max-smart", "Max Smart", 1)]:
            messages.append("role seed rows do not exactly match the approved baseline")
        cursor.execute(SLOT_BASELINE_SQL)
        slot_rows = cursor.fetchall()
        if not isinstance(slot_rows, list) or any(not isinstance(row, tuple) or len(row) != 6 for row in slot_rows):
            messages.append("global_model_slot seed query returned an unexpected result")
        elif slot_rows != [(1, None, None, None, None, None)]:
            messages.append("global_model_slot seed rows do not exactly match the approved baseline")
    if messages:
        raise IrreversibleError("Cannot reverse migration: " + "; ".join(messages))


class Migration(migrations.Migration):
    atomic = True
    dependencies = [("max_smart_db", "0002_v1_integrity_triggers")]
    operations = [
        migrations.RunPython(seed_role_forward, seed_role_reverse, atomic=None),
        migrations.RunPython(seed_slot_forward, seed_slot_reverse, atomic=None),
        migrations.RunSQL(
            sql=["""CREATE TRIGGER msv1_ai_bd_no_delete_trg
BEFORE DELETE ON authorized_identity
FOR EACH ROW
BEGIN
    SELECT RAISE(
        ABORT,
        'authorized identities cannot be deleted'
    );
END;"""],
            reverse_sql=["""DROP TRIGGER msv1_ai_bd_no_delete_trg;"""],
        ),
        migrations.RunSQL(
            sql=["""CREATE TRIGGER msv1_role_bd_no_delete_trg
BEFORE DELETE ON role
FOR EACH ROW
BEGIN
    SELECT RAISE(
        ABORT,
        'roles cannot be deleted'
    );
END;"""],
            reverse_sql=["""DROP TRIGGER msv1_role_bd_no_delete_trg;"""],
        ),
        migrations.RunSQL(
            sql=["""CREATE TRIGGER msv1_conv_bd_no_delete_trg
BEFORE DELETE ON conversation
FOR EACH ROW
BEGIN
    SELECT RAISE(
        ABORT,
        'conversations cannot be deleted'
    );
END;"""],
            reverse_sql=["""DROP TRIGGER msv1_conv_bd_no_delete_trg;"""],
        ),
        migrations.RunSQL(
            sql=["""CREATE TRIGGER msv1_op_bd_no_delete_trg
BEFORE DELETE ON operation
FOR EACH ROW
BEGIN
    SELECT RAISE(
        ABORT,
        'operations cannot be deleted'
    );
END;"""],
            reverse_sql=["""DROP TRIGGER msv1_op_bd_no_delete_trg;"""],
        ),
        migrations.RunSQL(
            sql=["""CREATE TRIGGER msv1_msg_bd_no_delete_trg
BEFORE DELETE ON message
FOR EACH ROW
BEGIN
    SELECT RAISE(
        ABORT,
        'messages cannot be deleted'
    );
END;"""],
            reverse_sql=["""DROP TRIGGER msv1_msg_bd_no_delete_trg;"""],
        ),
        migrations.RunSQL(
            sql=["""CREATE TRIGGER msv1_slot_bd_no_delete_trg
BEFORE DELETE ON global_model_slot
FOR EACH ROW
BEGIN
    SELECT RAISE(
        ABORT,
        'global model slot cannot be deleted'
    );
END;"""],
            reverse_sql=["""DROP TRIGGER msv1_slot_bd_no_delete_trg;"""],
        ),
        migrations.RunPython(forward_noop, guard_0003, atomic=None),
    ]
