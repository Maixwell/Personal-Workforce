"""Create the frozen V1 schema and matching Django migration state."""

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError

import django.db.models.deletion

import max_smart_db.fields


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
    if (
        extra is not None
        or not isinstance(row, tuple)
        or len(row) != 1
        or type(row[0]) is not int
        or row[0] < 0
    ):
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


def _raise_guard_failures(messages):
    if messages:
        raise IrreversibleError("Cannot reverse migration: " + "; ".join(messages))


def guard_0001(apps, schema_editor):
    messages = []
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(MIGRATION_0001_COUNT_SQL)
        _record_named_count(messages, "0001_v1_schema", _exact_nonnegative_scalar(cursor), True)
        cursor.execute(MIGRATION_0002_COUNT_SQL)
        _record_named_count(messages, "0002_v1_integrity_triggers", _exact_nonnegative_scalar(cursor), False)
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
    _raise_guard_failures(messages)


class Migration(migrations.Migration):
    atomic = True
    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]
    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
            migrations.RunSQL(
                sql=["""CREATE TABLE authorized_identity (
    id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    tailscale_login TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    created_at_us INTEGER NOT NULL,
    last_seen_at_us INTEGER NULL,
    CONSTRAINT msv1_ai_pk
        PRIMARY KEY (id),
    CONSTRAINT msv1_ai_user_fk
        FOREIGN KEY (user_id)
        REFERENCES auth_user (id)
        ON DELETE NO ACTION
        ON UPDATE NO ACTION
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT msv1_ai_user_uq
        UNIQUE (user_id),
    CONSTRAINT msv1_ai_login_uq
        UNIQUE (tailscale_login),
    CONSTRAINT msv1_ai_login_ck
        CHECK (
            typeof(tailscale_login) = 'text'
            AND tailscale_login <> ''
        ),
    CONSTRAINT msv1_ai_enabled_ck
        CHECK (
            typeof(enabled) = 'integer'
            AND enabled IN (0, 1)
        ),
    CONSTRAINT msv1_ai_created_ck
        CHECK (
            typeof(created_at_us) = 'integer'
            AND created_at_us >= 0
        ),
    CONSTRAINT msv1_ai_seen_ck
        CHECK (
            last_seen_at_us IS NULL
            OR (
                typeof(last_seen_at_us) = 'integer'
                AND last_seen_at_us >= 0
            )
        )
);"""],
                reverse_sql=["""DROP TABLE authorized_identity;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE TABLE role (
    slug TEXT NOT NULL,
    display_name TEXT NOT NULL,
    active INTEGER NOT NULL,
    CONSTRAINT msv1_role_pk
        PRIMARY KEY (slug),
    CONSTRAINT msv1_role_slug_ck
        CHECK (
            typeof(slug) = 'text'
            AND slug <> ''
        ),
    CONSTRAINT msv1_role_name_ck
        CHECK (
            typeof(display_name) = 'text'
            AND display_name <> ''
        ),
    CONSTRAINT msv1_role_active_ck
        CHECK (
            typeof(active) = 'integer'
            AND active IN (0, 1)
        )
);"""],
                reverse_sql=["""DROP TABLE role;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE TABLE conversation (
    id CHAR(32) NOT NULL,
    owner_id INTEGER NOT NULL,
    role_slug TEXT NOT NULL,
    version INTEGER NOT NULL,
    next_sequence INTEGER NOT NULL,
    title TEXT NULL,
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL,
    archived_at_us INTEGER NULL,
    CONSTRAINT msv1_conv_pk
        PRIMARY KEY (id),
    CONSTRAINT msv1_conv_owner_fk
        FOREIGN KEY (owner_id)
        REFERENCES auth_user (id)
        ON DELETE NO ACTION
        ON UPDATE NO ACTION
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT msv1_conv_role_fk
        FOREIGN KEY (role_slug)
        REFERENCES role (slug)
        ON DELETE NO ACTION
        ON UPDATE NO ACTION
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT msv1_conv_id_fmt_ck
        CHECK (
            typeof(id) = 'text'
            AND length(id) = 32
            AND instr(id, char(0)) = 0
            AND id NOT GLOB '*[^0-9a-f]*'
        ),
    CONSTRAINT msv1_conv_version_ck
        CHECK (
            typeof(version) = 'integer'
            AND version >= 0
        ),
    CONSTRAINT msv1_conv_next_seq_ck
        CHECK (
            typeof(next_sequence) = 'integer'
            AND next_sequence >= 1
        ),
    CONSTRAINT msv1_conv_title_ck
        CHECK (
            title IS NULL
            OR typeof(title) = 'text'
        ),
    CONSTRAINT msv1_conv_times_ck
        CHECK (
            typeof(created_at_us) = 'integer'
            AND created_at_us >= 0
            AND typeof(updated_at_us) = 'integer'
            AND updated_at_us >= 0
            AND (
                archived_at_us IS NULL
                OR (
                    typeof(archived_at_us) = 'integer'
                    AND archived_at_us >= 0
                )
            )
        )
);"""],
                reverse_sql=["""DROP TABLE conversation;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE TABLE operation (
    id CHAR(32) NOT NULL,
    conversation_id CHAR(32) NOT NULL,
    owner_id INTEGER NOT NULL,
    operation_type TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    status TEXT NOT NULL,
    blocks_conversation INTEGER NOT NULL,
    expected_version INTEGER NOT NULL,
    request_content TEXT NOT NULL,
    fingerprint_version INTEGER NOT NULL,
    request_hash TEXT NOT NULL,
    retry_of CHAR(32) NULL,
    runner_id TEXT NULL,
    reservation_expires_at_us INTEGER NULL,
    provider_started_at_us INTEGER NULL,
    execution_deadline_us INTEGER NULL,
    lease_expires_at_us INTEGER NULL,
    created_at_us INTEGER NOT NULL,
    finished_at_us INTEGER NULL,
    acknowledged_at_us INTEGER NULL,
    failure_category TEXT NULL,
    CONSTRAINT msv1_op_pk
        PRIMARY KEY (id),
    CONSTRAINT msv1_op_conv_fk
        FOREIGN KEY (conversation_id)
        REFERENCES conversation (id)
        ON DELETE NO ACTION
        ON UPDATE NO ACTION
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT msv1_op_owner_fk
        FOREIGN KEY (owner_id)
        REFERENCES auth_user (id)
        ON DELETE NO ACTION
        ON UPDATE NO ACTION
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT msv1_op_retry_fk
        FOREIGN KEY (retry_of)
        REFERENCES operation (id)
        ON DELETE NO ACTION
        ON UPDATE NO ACTION
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT msv1_op_id_fmt_ck
        CHECK (
            typeof(id) = 'text'
            AND length(id) = 32
            AND instr(id, char(0)) = 0
            AND id NOT GLOB '*[^0-9a-f]*'
        ),
    CONSTRAINT msv1_op_owner_type_ck
        CHECK (typeof(owner_id) = 'integer'),
    CONSTRAINT msv1_op_type_ck
        CHECK (operation_type = 'message'),
    CONSTRAINT msv1_op_provider_ck
        CHECK (provider_id = 'openai'),
    CONSTRAINT msv1_op_model_ck
        CHECK (model_id = 'gpt-5.6-sol'),
    CONSTRAINT msv1_op_status_ck
        CHECK (
            status IN (
                'QUEUED',
                'RUNNING',
                'SUCCEEDED',
                'FAILED',
                'OUTCOME_UNKNOWN',
                'CANCELLED'
            )
        ),
    CONSTRAINT msv1_op_block_ck
        CHECK (
            typeof(blocks_conversation) = 'integer'
            AND blocks_conversation IN (0, 1)
        ),
    CONSTRAINT msv1_op_expected_ck
        CHECK (
            typeof(expected_version) = 'integer'
            AND expected_version >= 0
        ),
    CONSTRAINT msv1_op_request_ck
        CHECK (
            typeof(request_content) = 'text'
            AND instr(request_content, char(0)) = 0
            AND length(request_content) BETWEEN 1 AND 20000
        ),
    CONSTRAINT msv1_op_fp_ver_ck
        CHECK (
            typeof(fingerprint_version) = 'integer'
            AND fingerprint_version = 1
        ),
    CONSTRAINT msv1_op_hash_ck
        CHECK (
            typeof(request_hash) = 'text'
            AND length(request_hash) = 64
            AND instr(request_hash, char(0)) = 0
            AND request_hash NOT GLOB '*[^0-9a-f]*'
        ),
    CONSTRAINT msv1_op_runner_ck
        CHECK (
            runner_id IS NULL
            OR (
                typeof(runner_id) = 'text'
                AND length(runner_id) = 22
                AND instr(runner_id, char(0)) = 0
                AND runner_id NOT GLOB '*[^A-Za-z0-9_-]*'
                AND substr(runner_id, -1, 1) IN ('A', 'Q', 'g', 'w')
            )
        ),
    CONSTRAINT msv1_op_created_ck
        CHECK (
            typeof(created_at_us) = 'integer'
            AND created_at_us >= 0
        ),
    CONSTRAINT msv1_op_resv_ck
        CHECK (
            reservation_expires_at_us IS NULL
            OR (
                typeof(reservation_expires_at_us) = 'integer'
                AND reservation_expires_at_us >= 0
            )
        ),
    CONSTRAINT msv1_op_started_ck
        CHECK (
            provider_started_at_us IS NULL
            OR (
                typeof(provider_started_at_us) = 'integer'
                AND provider_started_at_us >= 0
            )
        ),
    CONSTRAINT msv1_op_deadline_ck
        CHECK (
            execution_deadline_us IS NULL
            OR (
                typeof(execution_deadline_us) = 'integer'
                AND execution_deadline_us >= 0
            )
        ),
    CONSTRAINT msv1_op_lease_ck
        CHECK (
            lease_expires_at_us IS NULL
            OR (
                typeof(lease_expires_at_us) = 'integer'
                AND lease_expires_at_us >= 0
            )
        ),
    CONSTRAINT msv1_op_finished_ck
        CHECK (
            finished_at_us IS NULL
            OR (
                typeof(finished_at_us) = 'integer'
                AND finished_at_us >= 0
            )
        ),
    CONSTRAINT msv1_op_acked_ck
        CHECK (
            acknowledged_at_us IS NULL
            OR (
                typeof(acknowledged_at_us) = 'integer'
                AND acknowledged_at_us >= 0
            )
        ),
    CONSTRAINT msv1_op_shape_ck
        CHECK (
            (
                status = 'QUEUED'
                AND blocks_conversation = 1
                AND runner_id IS NOT NULL
                AND reservation_expires_at_us IS NOT NULL
                AND provider_started_at_us IS NULL
                AND execution_deadline_us IS NULL
                AND lease_expires_at_us IS NULL
                AND finished_at_us IS NULL
                AND acknowledged_at_us IS NULL
            )
            OR
            (
                status = 'RUNNING'
                AND blocks_conversation = 1
                AND runner_id IS NOT NULL
                AND reservation_expires_at_us IS NULL
                AND provider_started_at_us IS NOT NULL
                AND execution_deadline_us IS NOT NULL
                AND lease_expires_at_us IS NOT NULL
                AND finished_at_us IS NULL
                AND acknowledged_at_us IS NULL
            )
            OR
            (
                status = 'SUCCEEDED'
                AND blocks_conversation = 0
                AND runner_id IS NULL
                AND reservation_expires_at_us IS NULL
                AND provider_started_at_us IS NOT NULL
                AND execution_deadline_us IS NOT NULL
                AND lease_expires_at_us IS NOT NULL
                AND finished_at_us IS NOT NULL
                AND acknowledged_at_us IS NULL
            )
            OR
            (
                status = 'FAILED'
                AND blocks_conversation = 0
                AND runner_id IS NULL
                AND reservation_expires_at_us IS NULL
                AND finished_at_us IS NOT NULL
                AND acknowledged_at_us IS NULL
                AND (
                    (
                        provider_started_at_us IS NULL
                        AND execution_deadline_us IS NULL
                        AND lease_expires_at_us IS NULL
                    )
                    OR
                    (
                        provider_started_at_us IS NOT NULL
                        AND execution_deadline_us IS NOT NULL
                        AND lease_expires_at_us IS NOT NULL
                    )
                )
            )
            OR
            (
                status = 'CANCELLED'
                AND blocks_conversation = 0
                AND runner_id IS NULL
                AND reservation_expires_at_us IS NULL
                AND provider_started_at_us IS NULL
                AND execution_deadline_us IS NULL
                AND lease_expires_at_us IS NULL
                AND finished_at_us IS NOT NULL
                AND acknowledged_at_us IS NULL
            )
            OR
            (
                status = 'OUTCOME_UNKNOWN'
                AND runner_id IS NOT NULL
                AND reservation_expires_at_us IS NULL
                AND provider_started_at_us IS NOT NULL
                AND execution_deadline_us IS NOT NULL
                AND lease_expires_at_us IS NOT NULL
                AND finished_at_us IS NOT NULL
                AND (
                    (
                        acknowledged_at_us IS NULL
                        AND blocks_conversation = 1
                    )
                    OR
                    (
                        acknowledged_at_us IS NOT NULL
                        AND blocks_conversation = 0
                    )
                )
            )
        ),
    CONSTRAINT msv1_op_timing_ck
        CHECK (
            execution_deadline_us IS NULL
            OR execution_deadline_us <= lease_expires_at_us
        ),
    CONSTRAINT msv1_op_failure_ck
        CHECK (
            (
                status IN ('QUEUED', 'CANCELLED')
                AND failure_category IS NULL
            )
            OR
            (
                status = 'FAILED'
                AND failure_category IS NOT NULL
                AND failure_category IN (
                    'context_unavailable',
                    'context_budget_exceeded',
                    'model_configuration_invalid',
                    'provider_definite_failure',
                    'provider_output_invalid'
                )
            )
            OR
            (
                status IN ('RUNNING', 'OUTCOME_UNKNOWN', 'SUCCEEDED')
                AND (
                    failure_category IS NULL
                    OR failure_category IN (
                        'provider_outcome_uncertain',
                        'persistence_failed'
                    )
                )
            )
        )
);"""],
                reverse_sql=["""DROP TABLE operation;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE TABLE message (
    id CHAR(32) NOT NULL,
    conversation_id CHAR(32) NOT NULL,
    sequence INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    operation_id CHAR(32) NULL,
    created_at_us INTEGER NOT NULL,
    CONSTRAINT msv1_msg_pk
        PRIMARY KEY (id),
    CONSTRAINT msv1_msg_conv_fk
        FOREIGN KEY (conversation_id)
        REFERENCES conversation (id)
        ON DELETE NO ACTION
        ON UPDATE NO ACTION
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT msv1_msg_op_fk
        FOREIGN KEY (operation_id)
        REFERENCES operation (id)
        ON DELETE NO ACTION
        ON UPDATE NO ACTION
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT msv1_msg_conv_seq_uq
        UNIQUE (conversation_id, sequence),
    CONSTRAINT msv1_msg_id_fmt_ck
        CHECK (
            typeof(id) = 'text'
            AND length(id) = 32
            AND instr(id, char(0)) = 0
            AND id NOT GLOB '*[^0-9a-f]*'
        ),
    CONSTRAINT msv1_msg_seq_ck
        CHECK (
            typeof(sequence) = 'integer'
            AND sequence >= 1
        ),
    CONSTRAINT msv1_msg_role_ck
        CHECK (role IN ('user', 'assistant')),
    CONSTRAINT msv1_msg_content_ck
        CHECK (typeof(content) = 'text' AND instr(content, char(0)) = 0),
    CONSTRAINT msv1_msg_created_ck
        CHECK (
            typeof(created_at_us) = 'integer'
            AND created_at_us >= 0
        )
);"""],
                reverse_sql=["""DROP TABLE message;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE TABLE idempotency_key (
    id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    conversation_id CHAR(32) NOT NULL,
    operation_type TEXT NOT NULL,
    expected_version INTEGER NOT NULL,
    key_digest_version INTEGER NOT NULL,
    key_digest TEXT NOT NULL,
    fingerprint_version INTEGER NOT NULL,
    request_hash TEXT NOT NULL,
    operation_id CHAR(32) NOT NULL,
    expires_at_us INTEGER NOT NULL,
    CONSTRAINT msv1_idem_pk
        PRIMARY KEY (id),
    CONSTRAINT msv1_idem_owner_fk
        FOREIGN KEY (owner_id)
        REFERENCES auth_user (id)
        ON DELETE NO ACTION
        ON UPDATE NO ACTION
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT msv1_idem_conv_fk
        FOREIGN KEY (conversation_id)
        REFERENCES conversation (id)
        ON DELETE NO ACTION
        ON UPDATE NO ACTION
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT msv1_idem_op_fk
        FOREIGN KEY (operation_id)
        REFERENCES operation (id)
        ON DELETE NO ACTION
        ON UPDATE NO ACTION
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT msv1_idem_owner_key_uq
        UNIQUE (owner_id, key_digest_version, key_digest),
    CONSTRAINT msv1_idem_operation_uq
        UNIQUE (operation_id),
    CONSTRAINT msv1_idem_owner_type_ck
        CHECK (typeof(owner_id) = 'integer'),
    CONSTRAINT msv1_idem_type_ck
        CHECK (operation_type = 'message'),
    CONSTRAINT msv1_idem_expected_ck
        CHECK (
            typeof(expected_version) = 'integer'
            AND expected_version >= 0
        ),
    CONSTRAINT msv1_idem_key_ver_ck
        CHECK (
            typeof(key_digest_version) = 'integer'
            AND key_digest_version = 1
        ),
    CONSTRAINT msv1_idem_key_hash_ck
        CHECK (
            typeof(key_digest) = 'text'
            AND length(key_digest) = 64
            AND instr(key_digest, char(0)) = 0
            AND key_digest NOT GLOB '*[^0-9a-f]*'
        ),
    CONSTRAINT msv1_idem_fp_ver_ck
        CHECK (
            typeof(fingerprint_version) = 'integer'
            AND fingerprint_version = 1
        ),
    CONSTRAINT msv1_idem_req_hash_ck
        CHECK (
            typeof(request_hash) = 'text'
            AND length(request_hash) = 64
            AND instr(request_hash, char(0)) = 0
            AND request_hash NOT GLOB '*[^0-9a-f]*'
        ),
    CONSTRAINT msv1_idem_expires_ck
        CHECK (
            typeof(expires_at_us) = 'integer'
            AND expires_at_us >= 0
        )
);"""],
                reverse_sql=["""DROP TABLE idempotency_key;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE TABLE global_model_slot (
    id INTEGER NOT NULL,
    operation_id CHAR(32) NULL,
    runner_id TEXT NULL,
    reservation_expires_at_us INTEGER NULL,
    lease_expires_at_us INTEGER NULL,
    acquired_at_us INTEGER NULL,
    CONSTRAINT msv1_slot_pk
        PRIMARY KEY (id),
    CONSTRAINT msv1_slot_op_fk
        FOREIGN KEY (operation_id)
        REFERENCES operation (id)
        ON DELETE NO ACTION
        ON UPDATE NO ACTION
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT msv1_slot_operation_uq
        UNIQUE (operation_id),
    CONSTRAINT msv1_slot_id_ck
        CHECK (
            typeof(id) = 'integer'
            AND id = 1
        ),
    CONSTRAINT msv1_slot_runner_ck
        CHECK (
            runner_id IS NULL
            OR (
                typeof(runner_id) = 'text'
                AND length(runner_id) = 22
                AND instr(runner_id, char(0)) = 0
                AND runner_id NOT GLOB '*[^A-Za-z0-9_-]*'
                AND substr(runner_id, -1, 1) IN ('A', 'Q', 'g', 'w')
            )
        ),
    CONSTRAINT msv1_slot_resv_ck
        CHECK (
            reservation_expires_at_us IS NULL
            OR (
                typeof(reservation_expires_at_us) = 'integer'
                AND reservation_expires_at_us >= 0
            )
        ),
    CONSTRAINT msv1_slot_lease_ck
        CHECK (
            lease_expires_at_us IS NULL
            OR (
                typeof(lease_expires_at_us) = 'integer'
                AND lease_expires_at_us >= 0
            )
        ),
    CONSTRAINT msv1_slot_acquired_ck
        CHECK (
            acquired_at_us IS NULL
            OR (
                typeof(acquired_at_us) = 'integer'
                AND acquired_at_us >= 0
            )
        ),
    CONSTRAINT msv1_slot_shape_ck
        CHECK (
            (
                operation_id IS NULL
                AND runner_id IS NULL
                AND reservation_expires_at_us IS NULL
                AND lease_expires_at_us IS NULL
                AND acquired_at_us IS NULL
            )
            OR
            (
                operation_id IS NOT NULL
                AND runner_id IS NOT NULL
                AND acquired_at_us IS NOT NULL
                AND (
                    (
                        reservation_expires_at_us IS NOT NULL
                        AND lease_expires_at_us IS NULL
                    )
                    OR
                    (
                        reservation_expires_at_us IS NULL
                        AND lease_expires_at_us IS NOT NULL
                    )
                )
            )
        )
);"""],
                reverse_sql=["""DROP TABLE global_model_slot;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE UNIQUE INDEX msv1_ai_one_enabled_uq
    ON authorized_identity (enabled)
    WHERE enabled = 1;"""],
                reverse_sql=["""DROP INDEX msv1_ai_one_enabled_uq;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE INDEX msv1_conv_owner_updated_ix
    ON conversation (owner_id, updated_at_us);"""],
                reverse_sql=["""DROP INDEX msv1_conv_owner_updated_ix;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE INDEX msv1_conv_owner_archived_ix
    ON conversation (owner_id, archived_at_us);"""],
                reverse_sql=["""DROP INDEX msv1_conv_owner_archived_ix;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE INDEX msv1_conv_role_ix
    ON conversation (role_slug);"""],
                reverse_sql=["""DROP INDEX msv1_conv_role_ix;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE INDEX msv1_op_owner_ix
    ON operation (owner_id);"""],
                reverse_sql=["""DROP INDEX msv1_op_owner_ix;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE INDEX msv1_op_conv_created_ix
    ON operation (conversation_id, created_at_us);"""],
                reverse_sql=["""DROP INDEX msv1_op_conv_created_ix;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE INDEX msv1_op_conv_status_ix
    ON operation (conversation_id, status);"""],
                reverse_sql=["""DROP INDEX msv1_op_conv_status_ix;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE INDEX msv1_op_created_ix
    ON operation (created_at_us);"""],
                reverse_sql=["""DROP INDEX msv1_op_created_ix;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE INDEX msv1_op_status_resv_ix
    ON operation (status, reservation_expires_at_us);"""],
                reverse_sql=["""DROP INDEX msv1_op_status_resv_ix;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE INDEX msv1_op_status_lease_ix
    ON operation (
        status,
        lease_expires_at_us,
        execution_deadline_us
    );"""],
                reverse_sql=["""DROP INDEX msv1_op_status_lease_ix;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE UNIQUE INDEX msv1_op_conv_block_uq
    ON operation (conversation_id)
    WHERE blocks_conversation = 1;"""],
                reverse_sql=["""DROP INDEX msv1_op_conv_block_uq;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE UNIQUE INDEX msv1_op_global_live_uq
    ON operation (blocks_conversation)
    WHERE status IN ('QUEUED', 'RUNNING');"""],
                reverse_sql=["""DROP INDEX msv1_op_global_live_uq;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE UNIQUE INDEX msv1_op_retry_once_uq
    ON operation (retry_of)
    WHERE retry_of IS NOT NULL;"""],
                reverse_sql=["""DROP INDEX msv1_op_retry_once_uq;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE UNIQUE INDEX msv1_msg_op_role_uq
    ON message (operation_id, role)
    WHERE operation_id IS NOT NULL;"""],
                reverse_sql=["""DROP INDEX msv1_msg_op_role_uq;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE INDEX msv1_idem_conv_ix
    ON idempotency_key (conversation_id);"""],
                reverse_sql=["""DROP INDEX msv1_idem_conv_ix;"""],
            ),
            migrations.RunSQL(
                sql=["""CREATE INDEX msv1_idem_expires_ix
    ON idempotency_key (expires_at_us);"""],
                reverse_sql=["""DROP INDEX msv1_idem_expires_ix;"""],
            ),
            ],
            state_operations=[
                migrations.CreateModel(
                    name="AuthorizedIdentity",
                    fields=[
                        (
                            "id",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=False,
                                db_column="id",
                                primary_key=True,
                                serialize=False,
                            ),
                        ),
                        (
                            "user",
                            models.ForeignKey(
                                blank=False,
                                db_column="user_id",
                                db_constraint=True,
                                db_index=False,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="+",
                                to=settings.AUTH_USER_MODEL,
                            ),
                        ),
                        (
                            "tailscale_login",
                            models.TextField(
                                blank=False,
                                db_column="tailscale_login",
                            ),
                        ),
                        (
                            "enabled",
                            models.IntegerField(
                                blank=False,
                                db_column="enabled",
                                validators=[
                                    MinValueValidator(0),
                                    MaxValueValidator(1),
                                ],
                            ),
                        ),
                        (
                            "created_at_us",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=False,
                                db_column="created_at_us",
                                validators=[MinValueValidator(0)],
                            ),
                        ),
                        (
                            "last_seen_at_us",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=True,
                                db_column="last_seen_at_us",
                                null=True,
                                validators=[MinValueValidator(0)],
                            ),
                        ),
                    ],
                    options={
                        "db_table": "authorized_identity",
                        "managed": True,
                        "constraints": [
                            models.UniqueConstraint(
                                fields=("user",),
                                name="msv1_ai_user_uq",
                            ),
                            models.UniqueConstraint(
                                fields=("tailscale_login",),
                                name="msv1_ai_login_uq",
                            ),
                            models.UniqueConstraint(
                                condition=models.Q(enabled=1),
                                fields=("enabled",),
                                name="msv1_ai_one_enabled_uq",
                            ),
                        ],
                        "indexes": [],
                    },
                ),
                migrations.CreateModel(
                    name="Role",
                    fields=[
                        (
                            "slug",
                            models.TextField(
                                blank=False,
                                db_column="slug",
                                primary_key=True,
                                serialize=False,
                            ),
                        ),
                        (
                            "display_name",
                            models.TextField(
                                blank=False,
                                db_column="display_name",
                            ),
                        ),
                        (
                            "active",
                            models.IntegerField(
                                blank=False,
                                db_column="active",
                                validators=[
                                    MinValueValidator(0),
                                    MaxValueValidator(1),
                                ],
                            ),
                        ),
                    ],
                    options={
                        "db_table": "role",
                        "managed": True,
                        "constraints": [],
                        "indexes": [],
                    },
                ),
                migrations.CreateModel(
                    name="Conversation",
                    fields=[
                        (
                            "id",
                            models.UUIDField(
                                blank=False,
                                db_column="id",
                                primary_key=True,
                                serialize=False,
                            ),
                        ),
                        (
                            "owner",
                            models.ForeignKey(
                                blank=False,
                                db_column="owner_id",
                                db_constraint=True,
                                db_index=False,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="+",
                                to=settings.AUTH_USER_MODEL,
                            ),
                        ),
                        (
                            "role",
                            models.ForeignKey(
                                blank=False,
                                db_column="role_slug",
                                db_constraint=True,
                                db_index=False,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="+",
                                to="max_smart_db.role",
                                to_field="slug",
                            ),
                        ),
                        (
                            "version",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=False,
                                db_column="version",
                                validators=[MinValueValidator(0)],
                            ),
                        ),
                        (
                            "next_sequence",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=False,
                                db_column="next_sequence",
                                validators=[MinValueValidator(1)],
                            ),
                        ),
                        (
                            "title",
                            models.TextField(
                                blank=True,
                                db_column="title",
                                null=True,
                            ),
                        ),
                        (
                            "created_at_us",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=False,
                                db_column="created_at_us",
                                validators=[MinValueValidator(0)],
                            ),
                        ),
                        (
                            "updated_at_us",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=False,
                                db_column="updated_at_us",
                                validators=[MinValueValidator(0)],
                            ),
                        ),
                        (
                            "archived_at_us",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=True,
                                db_column="archived_at_us",
                                null=True,
                                validators=[MinValueValidator(0)],
                            ),
                        ),
                    ],
                    options={
                        "db_table": "conversation",
                        "managed": True,
                        "constraints": [],
                        "indexes": [
                            models.Index(
                                fields=("owner", "updated_at_us"),
                                name="msv1_conv_owner_updated_ix",
                            ),
                            models.Index(
                                fields=("owner", "archived_at_us"),
                                name="msv1_conv_owner_archived_ix",
                            ),
                            models.Index(
                                fields=("role",),
                                name="msv1_conv_role_ix",
                            ),
                        ],
                    },
                ),
                migrations.CreateModel(
                    name="Operation",
                    fields=[
                        (
                            "id",
                            models.UUIDField(
                                blank=False,
                                db_column="id",
                                primary_key=True,
                                serialize=False,
                            ),
                        ),
                        (
                            "conversation",
                            models.ForeignKey(
                                blank=False,
                                db_column="conversation_id",
                                db_constraint=True,
                                db_index=False,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="+",
                                to="max_smart_db.conversation",
                            ),
                        ),
                        (
                            "owner",
                            models.ForeignKey(
                                blank=False,
                                db_column="owner_id",
                                db_constraint=True,
                                db_index=False,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="+",
                                to=settings.AUTH_USER_MODEL,
                            ),
                        ),
                        (
                            "operation_type",
                            models.TextField(
                                blank=False,
                                db_column="operation_type",
                            ),
                        ),
                        (
                            "provider_id",
                            models.TextField(
                                blank=False,
                                db_column="provider_id",
                            ),
                        ),
                        (
                            "model_id",
                            models.TextField(
                                blank=False,
                                db_column="model_id",
                            ),
                        ),
                        (
                            "status",
                            models.TextField(
                                blank=False,
                                db_column="status",
                            ),
                        ),
                        (
                            "blocks_conversation",
                            models.IntegerField(
                                blank=False,
                                db_column="blocks_conversation",
                                validators=[
                                    MinValueValidator(0),
                                    MaxValueValidator(1),
                                ],
                            ),
                        ),
                        (
                            "expected_version",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=False,
                                db_column="expected_version",
                                validators=[MinValueValidator(0)],
                            ),
                        ),
                        (
                            "request_content",
                            models.TextField(
                                blank=False,
                                db_column="request_content",
                            ),
                        ),
                        (
                            "fingerprint_version",
                            models.IntegerField(
                                blank=False,
                                db_column="fingerprint_version",
                                validators=[
                                    MinValueValidator(1),
                                    MaxValueValidator(1),
                                ],
                            ),
                        ),
                        (
                            "request_hash",
                            models.TextField(
                                blank=False,
                                db_column="request_hash",
                            ),
                        ),
                        (
                            "retry_of",
                            models.ForeignKey(
                                blank=True,
                                db_column="retry_of",
                                db_constraint=True,
                                db_index=False,
                                null=True,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="+",
                                to="max_smart_db.operation",
                            ),
                        ),
                        (
                            "runner_id",
                            models.TextField(
                                blank=True,
                                db_column="runner_id",
                                null=True,
                            ),
                        ),
                        (
                            "reservation_expires_at_us",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=True,
                                db_column="reservation_expires_at_us",
                                null=True,
                                validators=[MinValueValidator(0)],
                            ),
                        ),
                        (
                            "provider_started_at_us",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=True,
                                db_column="provider_started_at_us",
                                null=True,
                                validators=[MinValueValidator(0)],
                            ),
                        ),
                        (
                            "execution_deadline_us",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=True,
                                db_column="execution_deadline_us",
                                null=True,
                                validators=[MinValueValidator(0)],
                            ),
                        ),
                        (
                            "lease_expires_at_us",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=True,
                                db_column="lease_expires_at_us",
                                null=True,
                                validators=[MinValueValidator(0)],
                            ),
                        ),
                        (
                            "created_at_us",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=False,
                                db_column="created_at_us",
                                validators=[MinValueValidator(0)],
                            ),
                        ),
                        (
                            "finished_at_us",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=True,
                                db_column="finished_at_us",
                                null=True,
                                validators=[MinValueValidator(0)],
                            ),
                        ),
                        (
                            "acknowledged_at_us",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=True,
                                db_column="acknowledged_at_us",
                                null=True,
                                validators=[MinValueValidator(0)],
                            ),
                        ),
                        (
                            "failure_category",
                            models.TextField(
                                blank=True,
                                db_column="failure_category",
                                null=True,
                            ),
                        ),
                    ],
                    options={
                        "db_table": "operation",
                        "managed": True,
                        "constraints": [
                            models.UniqueConstraint(
                                condition=models.Q(blocks_conversation=1),
                                fields=("conversation",),
                                name="msv1_op_conv_block_uq",
                            ),
                            models.UniqueConstraint(
                                condition=models.Q(
                                    status__in=("QUEUED", "RUNNING"),
                                ),
                                fields=("blocks_conversation",),
                                name="msv1_op_global_live_uq",
                            ),
                            models.UniqueConstraint(
                                condition=models.Q(retry_of__isnull=False),
                                fields=("retry_of",),
                                name="msv1_op_retry_once_uq",
                            ),
                        ],
                        "indexes": [
                            models.Index(
                                fields=("owner",),
                                name="msv1_op_owner_ix",
                            ),
                            models.Index(
                                fields=("conversation", "created_at_us"),
                                name="msv1_op_conv_created_ix",
                            ),
                            models.Index(
                                fields=("conversation", "status"),
                                name="msv1_op_conv_status_ix",
                            ),
                            models.Index(
                                fields=("created_at_us",),
                                name="msv1_op_created_ix",
                            ),
                            models.Index(
                                fields=(
                                    "status",
                                    "reservation_expires_at_us",
                                ),
                                name="msv1_op_status_resv_ix",
                            ),
                            models.Index(
                                fields=(
                                    "status",
                                    "lease_expires_at_us",
                                    "execution_deadline_us",
                                ),
                                name="msv1_op_status_lease_ix",
                            ),
                        ],
                    },
                ),
                migrations.CreateModel(
                    name="Message",
                    fields=[
                        (
                            "id",
                            models.UUIDField(
                                blank=False,
                                db_column="id",
                                primary_key=True,
                                serialize=False,
                            ),
                        ),
                        (
                            "conversation",
                            models.ForeignKey(
                                blank=False,
                                db_column="conversation_id",
                                db_constraint=True,
                                db_index=False,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="+",
                                to="max_smart_db.conversation",
                            ),
                        ),
                        (
                            "sequence",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=False,
                                db_column="sequence",
                                validators=[MinValueValidator(1)],
                            ),
                        ),
                        (
                            "role",
                            models.TextField(
                                blank=False,
                                db_column="role",
                            ),
                        ),
                        (
                            "content",
                            models.TextField(
                                blank=False,
                                db_column="content",
                            ),
                        ),
                        (
                            "operation",
                            models.ForeignKey(
                                blank=True,
                                db_column="operation_id",
                                db_constraint=True,
                                db_index=False,
                                null=True,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="+",
                                to="max_smart_db.operation",
                            ),
                        ),
                        (
                            "created_at_us",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=False,
                                db_column="created_at_us",
                                validators=[MinValueValidator(0)],
                            ),
                        ),
                    ],
                    options={
                        "db_table": "message",
                        "managed": True,
                        "constraints": [
                            models.UniqueConstraint(
                                fields=("conversation", "sequence"),
                                name="msv1_msg_conv_seq_uq",
                            ),
                            models.UniqueConstraint(
                                condition=models.Q(operation__isnull=False),
                                fields=("operation", "role"),
                                name="msv1_msg_op_role_uq",
                            ),
                        ],
                        "indexes": [],
                    },
                ),
                migrations.CreateModel(
                    name="IdempotencyKey",
                    fields=[
                        (
                            "id",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=False,
                                db_column="id",
                                primary_key=True,
                                serialize=False,
                                validators=[MinValueValidator(1)],
                            ),
                        ),
                        (
                            "owner",
                            models.ForeignKey(
                                blank=False,
                                db_column="owner_id",
                                db_constraint=True,
                                db_index=False,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="+",
                                to=settings.AUTH_USER_MODEL,
                            ),
                        ),
                        (
                            "conversation",
                            models.ForeignKey(
                                blank=False,
                                db_column="conversation_id",
                                db_constraint=True,
                                db_index=False,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="+",
                                to="max_smart_db.conversation",
                            ),
                        ),
                        (
                            "operation_type",
                            models.TextField(
                                blank=False,
                                db_column="operation_type",
                            ),
                        ),
                        (
                            "expected_version",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=False,
                                db_column="expected_version",
                                validators=[MinValueValidator(0)],
                            ),
                        ),
                        (
                            "key_digest_version",
                            models.IntegerField(
                                blank=False,
                                db_column="key_digest_version",
                                validators=[
                                    MinValueValidator(1),
                                    MaxValueValidator(1),
                                ],
                            ),
                        ),
                        (
                            "key_digest",
                            models.TextField(
                                blank=False,
                                db_column="key_digest",
                            ),
                        ),
                        (
                            "fingerprint_version",
                            models.IntegerField(
                                blank=False,
                                db_column="fingerprint_version",
                                validators=[
                                    MinValueValidator(1),
                                    MaxValueValidator(1),
                                ],
                            ),
                        ),
                        (
                            "request_hash",
                            models.TextField(
                                blank=False,
                                db_column="request_hash",
                            ),
                        ),
                        (
                            "operation",
                            models.ForeignKey(
                                blank=False,
                                db_column="operation_id",
                                db_constraint=True,
                                db_index=False,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="+",
                                to="max_smart_db.operation",
                            ),
                        ),
                        (
                            "expires_at_us",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=False,
                                db_column="expires_at_us",
                                validators=[MinValueValidator(0)],
                            ),
                        ),
                    ],
                    options={
                        "db_table": "idempotency_key",
                        "managed": True,
                        "constraints": [
                            models.UniqueConstraint(
                                fields=(
                                    "owner",
                                    "key_digest_version",
                                    "key_digest",
                                ),
                                name="msv1_idem_owner_key_uq",
                            ),
                            models.UniqueConstraint(
                                fields=("operation",),
                                name="msv1_idem_operation_uq",
                            ),
                        ],
                        "indexes": [
                            models.Index(
                                fields=("conversation",),
                                name="msv1_idem_conv_ix",
                            ),
                            models.Index(
                                fields=("expires_at_us",),
                                name="msv1_idem_expires_ix",
                            ),
                        ],
                    },
                ),
                migrations.CreateModel(
                    name="GlobalModelSlot",
                    fields=[
                        (
                            "id",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=False,
                                db_column="id",
                                primary_key=True,
                                serialize=False,
                                validators=[
                                    MinValueValidator(1),
                                    MaxValueValidator(1),
                                ],
                            ),
                        ),
                        (
                            "operation",
                            models.ForeignKey(
                                blank=True,
                                db_column="operation_id",
                                db_constraint=True,
                                db_index=False,
                                null=True,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="+",
                                to="max_smart_db.operation",
                            ),
                        ),
                        (
                            "runner_id",
                            models.TextField(
                                blank=True,
                                db_column="runner_id",
                                null=True,
                            ),
                        ),
                        (
                            "reservation_expires_at_us",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=True,
                                db_column="reservation_expires_at_us",
                                null=True,
                                validators=[MinValueValidator(0)],
                            ),
                        ),
                        (
                            "lease_expires_at_us",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=True,
                                db_column="lease_expires_at_us",
                                null=True,
                                validators=[MinValueValidator(0)],
                            ),
                        ),
                        (
                            "acquired_at_us",
                            max_smart_db.fields.SQLiteInteger64Field(
                                blank=True,
                                db_column="acquired_at_us",
                                null=True,
                                validators=[MinValueValidator(0)],
                            ),
                        ),
                    ],
                    options={
                        "db_table": "global_model_slot",
                        "managed": True,
                        "constraints": [
                            models.UniqueConstraint(
                                fields=("operation",),
                                name="msv1_slot_operation_uq",
                            ),
                        ],
                        "indexes": [],
                    },
                ),
            ],
        ),
        migrations.RunPython(forward_noop, guard_0001, atomic=None),
    ]
