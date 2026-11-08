"""Install the frozen V1 integrity triggers."""

from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError


MIGRATION_0001_COUNT_SQL = "SELECT COUNT(*) FROM django_migrations WHERE app = 'max_smart_db' AND name = '0001_v1_schema';"
MIGRATION_0002_COUNT_SQL = "SELECT COUNT(*) FROM django_migrations WHERE app = 'max_smart_db' AND name = '0002_v1_integrity_triggers';"
MIGRATION_0003_COUNT_SQL = "SELECT COUNT(*) FROM django_migrations WHERE app = 'max_smart_db' AND name = '0003_v1_seeds_and_retention';"
UNEXPECTED_MIGRATION_COUNT_SQL = "SELECT COUNT(*) FROM django_migrations WHERE app = 'max_smart_db' AND (name IS NULL OR name NOT IN ('0001_v1_schema', '0002_v1_integrity_triggers', '0003_v1_seeds_and_retention'));"
AI_EMPTY_SQL = "SELECT EXISTS (SELECT 1 FROM authorized_identity LIMIT 1);"
ROLE_EMPTY_SQL = "SELECT EXISTS (SELECT 1 FROM role LIMIT 1);"
CONVERSATION_EMPTY_SQL = "SELECT EXISTS (SELECT 1 FROM conversation LIMIT 1);"
OPERATION_EMPTY_SQL = "SELECT EXISTS (SELECT 1 FROM operation LIMIT 1);"
MESSAGE_EMPTY_SQL = "SELECT EXISTS (SELECT 1 FROM message LIMIT 1);"
IDEMPOTENCY_EMPTY_SQL = "SELECT EXISTS (SELECT 1 FROM idempotency_key LIMIT 1);"
SLOT_EMPTY_SQL = "SELECT EXISTS (SELECT 1 FROM global_model_slot LIMIT 1);"


def forward_noop(apps, schema_editor):
    return None


def _exact_nonnegative_scalar(cursor):
    row = cursor.fetchone()
    extra = cursor.fetchone()
    if extra is not None or not isinstance(row, tuple) or len(row) != 1 or type(row[0]) is not int or row[0] < 0:
        return None
    return row[0]


def _record_named_count(messages, name, count, required):
    if count is None:
        messages.append(f"migration query returned an unexpected result: {name}")
    elif required and count == 0:
        messages.append(f"missing migration row: {name}")
    elif count > 1:
        messages.append(f"duplicate migration row: {name}")
    elif not required and count == 1:
        messages.append(f"unexpectedly present migration row: {name}")


def _record_emptiness(messages, table, value):
    if value == 1:
        messages.append(f"{table} is not empty")
    elif value != 0:
        messages.append(f"{table} emptiness query returned an unexpected result")


def guard_0002(apps, schema_editor):
    messages = []
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(MIGRATION_0001_COUNT_SQL)
        _record_named_count(messages, "0001_v1_schema", _exact_nonnegative_scalar(cursor), True)
        cursor.execute(MIGRATION_0002_COUNT_SQL)
        _record_named_count(messages, "0002_v1_integrity_triggers", _exact_nonnegative_scalar(cursor), True)
        cursor.execute(MIGRATION_0003_COUNT_SQL)
        _record_named_count(messages, "0003_v1_seeds_and_retention", _exact_nonnegative_scalar(cursor), False)
        cursor.execute(UNEXPECTED_MIGRATION_COUNT_SQL)
        unexpected = _exact_nonnegative_scalar(cursor)
        if unexpected is None:
            messages.append("migration query returned an unexpected result: max_smart_db")
        elif unexpected > 0:
            messages.append("unexpected migration row: max_smart_db")
        cursor.execute(AI_EMPTY_SQL)
        _record_emptiness(messages, "authorized_identity", _exact_nonnegative_scalar(cursor))
        cursor.execute(ROLE_EMPTY_SQL)
        _record_emptiness(messages, "role", _exact_nonnegative_scalar(cursor))
        cursor.execute(CONVERSATION_EMPTY_SQL)
        _record_emptiness(messages, "conversation", _exact_nonnegative_scalar(cursor))
        cursor.execute(OPERATION_EMPTY_SQL)
        _record_emptiness(messages, "operation", _exact_nonnegative_scalar(cursor))
        cursor.execute(MESSAGE_EMPTY_SQL)
        _record_emptiness(messages, "message", _exact_nonnegative_scalar(cursor))
        cursor.execute(IDEMPOTENCY_EMPTY_SQL)
        _record_emptiness(messages, "idempotency_key", _exact_nonnegative_scalar(cursor))
        cursor.execute(SLOT_EMPTY_SQL)
        _record_emptiness(messages, "global_model_slot", _exact_nonnegative_scalar(cursor))
    if messages:
        raise IrreversibleError("Cannot reverse migration: " + "; ".join(messages))


class Migration(migrations.Migration):
    atomic = True
    dependencies = [("max_smart_db", "0001_v1_schema")]
    operations = [
        migrations.RunSQL(
            sql=["""CREATE TRIGGER msv1_ai_bu_immut_trg
BEFORE UPDATE ON authorized_identity
FOR EACH ROW
WHEN NEW.id IS NOT OLD.id
  OR NEW.user_id IS NOT OLD.user_id
  OR NEW.tailscale_login IS NOT OLD.tailscale_login
  OR NEW.created_at_us IS NOT OLD.created_at_us
BEGIN
    SELECT RAISE(
        ABORT,
        'authorized identity binding is immutable'
    );
END;"""],
            reverse_sql=["""DROP TRIGGER msv1_ai_bu_immut_trg;"""],
        ),
        migrations.RunSQL(
            sql=["""CREATE TRIGGER msv1_role_bu_immut_trg
BEFORE UPDATE ON role
FOR EACH ROW
WHEN NEW.slug IS NOT OLD.slug
BEGIN
    SELECT RAISE(
        ABORT,
        'role slug is immutable'
    );
END;"""],
            reverse_sql=["""DROP TRIGGER msv1_role_bu_immut_trg;"""],
        ),
        migrations.RunSQL(
            sql=["""CREATE TRIGGER msv1_conv_bu_immut_trg
BEFORE UPDATE ON conversation
FOR EACH ROW
WHEN NEW.id IS NOT OLD.id
  OR NEW.owner_id IS NOT OLD.owner_id
  OR NEW.role_slug IS NOT OLD.role_slug
  OR NEW.created_at_us IS NOT OLD.created_at_us
BEGIN
    SELECT RAISE(
        ABORT,
        'conversation identity is immutable'
    );
END;"""],
            reverse_sql=["""DROP TRIGGER msv1_conv_bu_immut_trg;"""],
        ),
        migrations.RunSQL(
            sql=["""CREATE TRIGGER msv1_op_bi_owner_match_trg
BEFORE INSERT ON operation
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM conversation AS conversation_row
    WHERE conversation_row.id = NEW.conversation_id
      AND conversation_row.owner_id = NEW.owner_id
)
BEGIN
    SELECT RAISE(
        ABORT,
        'operation owner does not match conversation owner'
    );
END;"""],
            reverse_sql=["""DROP TRIGGER msv1_op_bi_owner_match_trg;"""],
        ),
        migrations.RunSQL(
            sql=["""CREATE TRIGGER msv1_op_bi_retry_valid_trg
BEFORE INSERT ON operation
FOR EACH ROW
WHEN NEW.retry_of IS NOT NULL
 AND (
    NEW.id = NEW.retry_of
    OR NOT EXISTS (
        SELECT 1
        FROM operation AS source
        WHERE source.id = NEW.retry_of
          AND NEW.id <> NEW.retry_of
          AND source.owner_id = NEW.owner_id
          AND source.conversation_id = NEW.conversation_id
          AND source.blocks_conversation = 0
          AND (
              source.status IN ('FAILED', 'CANCELLED')
              OR (
                  source.status = 'OUTCOME_UNKNOWN'
                  AND source.acknowledged_at_us IS NOT NULL
              )
          )
    )
)
BEGIN
    SELECT RAISE(
        ABORT,
        'retry source is not eligible'
    );
END;"""],
            reverse_sql=["""DROP TRIGGER msv1_op_bi_retry_valid_trg;"""],
        ),
        migrations.RunSQL(
            sql=["""CREATE TRIGGER msv1_op_bu_immut_trg
BEFORE UPDATE OF
    id,
    owner_id,
    conversation_id,
    request_content,
    request_hash,
    fingerprint_version,
    expected_version,
    operation_type,
    provider_id,
    model_id,
    retry_of,
    created_at_us
ON operation
FOR EACH ROW
BEGIN
    SELECT RAISE(
        ABORT,
        'operation identity fields are immutable'
    );
END;"""],
            reverse_sql=["""DROP TRIGGER msv1_op_bu_immut_trg;"""],
        ),
        migrations.RunSQL(
            sql=["""CREATE TRIGGER msv1_msg_bi_op_conv_trg
BEFORE INSERT ON message
FOR EACH ROW
WHEN NEW.operation_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1
    FROM operation AS operation_row
    WHERE operation_row.id = NEW.operation_id
      AND operation_row.conversation_id = NEW.conversation_id
)
BEGIN
    SELECT RAISE(
        ABORT,
        'message operation does not match conversation'
    );
END;"""],
            reverse_sql=["""DROP TRIGGER msv1_msg_bi_op_conv_trg;"""],
        ),
        migrations.RunSQL(
            sql=["""CREATE TRIGGER msv1_msg_bu_immut_trg
BEFORE UPDATE ON message
FOR EACH ROW
BEGIN
    SELECT RAISE(
        ABORT,
        'messages are immutable'
    );
END;"""],
            reverse_sql=["""DROP TRIGGER msv1_msg_bu_immut_trg;"""],
        ),
        migrations.RunSQL(
            sql=["""CREATE TRIGGER msv1_idem_bi_binding_trg
BEFORE INSERT ON idempotency_key
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM operation AS operation_row
    WHERE operation_row.id = NEW.operation_id
      AND operation_row.owner_id = NEW.owner_id
      AND operation_row.conversation_id = NEW.conversation_id
      AND operation_row.operation_type = NEW.operation_type
      AND operation_row.expected_version = NEW.expected_version
      AND operation_row.fingerprint_version = NEW.fingerprint_version
      AND operation_row.request_hash = NEW.request_hash
)
BEGIN
    SELECT RAISE(
        ABORT,
        'idempotency binding does not match operation'
    );
END;"""],
            reverse_sql=["""DROP TRIGGER msv1_idem_bi_binding_trg;"""],
        ),
        migrations.RunSQL(
            sql=["""CREATE TRIGGER msv1_idem_bi_no_replace_trg
BEFORE INSERT ON idempotency_key
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM idempotency_key AS existing
    WHERE existing.id = NEW.id
       OR existing.operation_id = NEW.operation_id
       OR (
           existing.owner_id = NEW.owner_id
           AND existing.key_digest_version = NEW.key_digest_version
           AND existing.key_digest = NEW.key_digest
       )
)
BEGIN
    SELECT RAISE(
        ABORT,
        'idempotency binding already exists'
    );
END;"""],
            reverse_sql=["""DROP TRIGGER msv1_idem_bi_no_replace_trg;"""],
        ),
        migrations.RunSQL(
            sql=["""CREATE TRIGGER msv1_idem_bu_immut_trg
BEFORE UPDATE ON idempotency_key
FOR EACH ROW
BEGIN
    SELECT RAISE(
        ABORT,
        'idempotency records are immutable'
    );
END;"""],
            reverse_sql=["""DROP TRIGGER msv1_idem_bu_immut_trg;"""],
        ),
        migrations.RunPython(forward_noop, guard_0002, atomic=None),
    ]
