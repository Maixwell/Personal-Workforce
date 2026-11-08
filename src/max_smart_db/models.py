"""Runtime models matching the frozen V1 Django migration state.

Physical CHECK constraints remain in the frozen SQL and are intentionally
absent from this Django state.
"""

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from .fields import SQLiteInteger64Field


class _ExplicitTargetForeignKey(models.ForeignKey):
    """Keep an explicit to_field that names the target primary key.

    Django omits that argument during deconstruction. The frozen migration
    state records it for the role slug, so the rendered state must keep it.
    """

    def deconstruct(self):
        name, _path, args, kwargs = super().deconstruct()
        if self.remote_field.field_name:
            kwargs["to_field"] = self.remote_field.field_name
        return name, "django.db.models.ForeignKey", args, kwargs


class AuthorizedIdentity(models.Model):
    id = SQLiteInteger64Field(
        blank=False,
        db_column="id",
        primary_key=True,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=False,
        db_column="user_id",
        db_constraint=True,
        db_index=False,
        on_delete=models.DO_NOTHING,
        related_name="+",
    )
    tailscale_login = models.TextField(
        blank=False,
        db_column="tailscale_login",
    )
    enabled = models.IntegerField(
        blank=False,
        db_column="enabled",
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    created_at_us = SQLiteInteger64Field(
        blank=False,
        db_column="created_at_us",
        validators=[MinValueValidator(0)],
    )
    last_seen_at_us = SQLiteInteger64Field(
        blank=True,
        db_column="last_seen_at_us",
        null=True,
        validators=[MinValueValidator(0)],
    )

    class Meta:
        db_table = "authorized_identity"
        managed = True
        constraints = [
            models.UniqueConstraint(fields=("user",), name="msv1_ai_user_uq"),
            models.UniqueConstraint(
                fields=("tailscale_login",),
                name="msv1_ai_login_uq",
            ),
            models.UniqueConstraint(
                condition=models.Q(enabled=1),
                fields=("enabled",),
                name="msv1_ai_one_enabled_uq",
            ),
        ]
        indexes = []


class Role(models.Model):
    slug = models.TextField(
        blank=False,
        db_column="slug",
        primary_key=True,
    )
    display_name = models.TextField(
        blank=False,
        db_column="display_name",
    )
    active = models.IntegerField(
        blank=False,
        db_column="active",
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )

    class Meta:
        db_table = "role"
        managed = True
        constraints = []
        indexes = []


class Conversation(models.Model):
    id = models.UUIDField(
        blank=False,
        db_column="id",
        primary_key=True,
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=False,
        db_column="owner_id",
        db_constraint=True,
        db_index=False,
        on_delete=models.DO_NOTHING,
        related_name="+",
    )
    role = _ExplicitTargetForeignKey(
        Role,
        blank=False,
        db_column="role_slug",
        db_constraint=True,
        db_index=False,
        on_delete=models.DO_NOTHING,
        related_name="+",
        to_field="slug",
    )
    version = SQLiteInteger64Field(
        blank=False,
        db_column="version",
        validators=[MinValueValidator(0)],
    )
    next_sequence = SQLiteInteger64Field(
        blank=False,
        db_column="next_sequence",
        validators=[MinValueValidator(1)],
    )
    title = models.TextField(
        blank=True,
        db_column="title",
        null=True,
    )
    created_at_us = SQLiteInteger64Field(
        blank=False,
        db_column="created_at_us",
        validators=[MinValueValidator(0)],
    )
    updated_at_us = SQLiteInteger64Field(
        blank=False,
        db_column="updated_at_us",
        validators=[MinValueValidator(0)],
    )
    archived_at_us = SQLiteInteger64Field(
        blank=True,
        db_column="archived_at_us",
        null=True,
        validators=[MinValueValidator(0)],
    )

    class Meta:
        db_table = "conversation"
        managed = True
        constraints = []
        indexes = [
            models.Index(
                fields=("owner", "updated_at_us"),
                name="msv1_conv_owner_updated_ix",
            ),
            models.Index(
                fields=("owner", "archived_at_us"),
                name="msv1_conv_owner_archived_ix",
            ),
            models.Index(fields=("role",), name="msv1_conv_role_ix"),
        ]


class Operation(models.Model):
    id = models.UUIDField(
        blank=False,
        db_column="id",
        primary_key=True,
    )
    conversation = models.ForeignKey(
        Conversation,
        blank=False,
        db_column="conversation_id",
        db_constraint=True,
        db_index=False,
        on_delete=models.DO_NOTHING,
        related_name="+",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=False,
        db_column="owner_id",
        db_constraint=True,
        db_index=False,
        on_delete=models.DO_NOTHING,
        related_name="+",
    )
    operation_type = models.TextField(
        blank=False,
        db_column="operation_type",
    )
    provider_id = models.TextField(
        blank=False,
        db_column="provider_id",
    )
    model_id = models.TextField(
        blank=False,
        db_column="model_id",
    )
    status = models.TextField(
        blank=False,
        db_column="status",
    )
    blocks_conversation = models.IntegerField(
        blank=False,
        db_column="blocks_conversation",
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    expected_version = SQLiteInteger64Field(
        blank=False,
        db_column="expected_version",
        validators=[MinValueValidator(0)],
    )
    request_content = models.TextField(
        blank=False,
        db_column="request_content",
    )
    fingerprint_version = models.IntegerField(
        blank=False,
        db_column="fingerprint_version",
        validators=[MinValueValidator(1), MaxValueValidator(1)],
    )
    request_hash = models.TextField(
        blank=False,
        db_column="request_hash",
    )
    retry_of = models.ForeignKey(
        "self",
        blank=True,
        db_column="retry_of",
        db_constraint=True,
        db_index=False,
        null=True,
        on_delete=models.DO_NOTHING,
        related_name="+",
    )
    runner_id = models.TextField(
        blank=True,
        db_column="runner_id",
        null=True,
    )
    reservation_expires_at_us = SQLiteInteger64Field(
        blank=True,
        db_column="reservation_expires_at_us",
        null=True,
        validators=[MinValueValidator(0)],
    )
    provider_started_at_us = SQLiteInteger64Field(
        blank=True,
        db_column="provider_started_at_us",
        null=True,
        validators=[MinValueValidator(0)],
    )
    execution_deadline_us = SQLiteInteger64Field(
        blank=True,
        db_column="execution_deadline_us",
        null=True,
        validators=[MinValueValidator(0)],
    )
    lease_expires_at_us = SQLiteInteger64Field(
        blank=True,
        db_column="lease_expires_at_us",
        null=True,
        validators=[MinValueValidator(0)],
    )
    created_at_us = SQLiteInteger64Field(
        blank=False,
        db_column="created_at_us",
        validators=[MinValueValidator(0)],
    )
    finished_at_us = SQLiteInteger64Field(
        blank=True,
        db_column="finished_at_us",
        null=True,
        validators=[MinValueValidator(0)],
    )
    acknowledged_at_us = SQLiteInteger64Field(
        blank=True,
        db_column="acknowledged_at_us",
        null=True,
        validators=[MinValueValidator(0)],
    )
    failure_category = models.TextField(
        blank=True,
        db_column="failure_category",
        null=True,
    )

    class Meta:
        db_table = "operation"
        managed = True
        constraints = [
            models.UniqueConstraint(
                condition=models.Q(blocks_conversation=1),
                fields=("conversation",),
                name="msv1_op_conv_block_uq",
            ),
            models.UniqueConstraint(
                condition=models.Q(status__in=("QUEUED", "RUNNING")),
                fields=("blocks_conversation",),
                name="msv1_op_global_live_uq",
            ),
            models.UniqueConstraint(
                condition=models.Q(retry_of__isnull=False),
                fields=("retry_of",),
                name="msv1_op_retry_once_uq",
            ),
        ]
        indexes = [
            models.Index(fields=("owner",), name="msv1_op_owner_ix"),
            models.Index(
                fields=("conversation", "created_at_us"),
                name="msv1_op_conv_created_ix",
            ),
            models.Index(
                fields=("conversation", "status"),
                name="msv1_op_conv_status_ix",
            ),
            models.Index(fields=("created_at_us",), name="msv1_op_created_ix"),
            models.Index(
                fields=("status", "reservation_expires_at_us"),
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
        ]


class Message(models.Model):
    id = models.UUIDField(
        blank=False,
        db_column="id",
        primary_key=True,
    )
    conversation = models.ForeignKey(
        Conversation,
        blank=False,
        db_column="conversation_id",
        db_constraint=True,
        db_index=False,
        on_delete=models.DO_NOTHING,
        related_name="+",
    )
    sequence = SQLiteInteger64Field(
        blank=False,
        db_column="sequence",
        validators=[MinValueValidator(1)],
    )
    role = models.TextField(
        blank=False,
        db_column="role",
    )
    content = models.TextField(
        blank=False,
        db_column="content",
    )
    operation = models.ForeignKey(
        Operation,
        blank=True,
        db_column="operation_id",
        db_constraint=True,
        db_index=False,
        null=True,
        on_delete=models.DO_NOTHING,
        related_name="+",
    )
    created_at_us = SQLiteInteger64Field(
        blank=False,
        db_column="created_at_us",
        validators=[MinValueValidator(0)],
    )

    class Meta:
        db_table = "message"
        managed = True
        constraints = [
            models.UniqueConstraint(
                fields=("conversation", "sequence"),
                name="msv1_msg_conv_seq_uq",
            ),
            models.UniqueConstraint(
                condition=models.Q(operation__isnull=False),
                fields=("operation", "role"),
                name="msv1_msg_op_role_uq",
            ),
        ]
        indexes = []


class IdempotencyKey(models.Model):
    id = SQLiteInteger64Field(
        blank=False,
        db_column="id",
        primary_key=True,
        validators=[MinValueValidator(1)],
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=False,
        db_column="owner_id",
        db_constraint=True,
        db_index=False,
        on_delete=models.DO_NOTHING,
        related_name="+",
    )
    conversation = models.ForeignKey(
        Conversation,
        blank=False,
        db_column="conversation_id",
        db_constraint=True,
        db_index=False,
        on_delete=models.DO_NOTHING,
        related_name="+",
    )
    operation_type = models.TextField(
        blank=False,
        db_column="operation_type",
    )
    expected_version = SQLiteInteger64Field(
        blank=False,
        db_column="expected_version",
        validators=[MinValueValidator(0)],
    )
    key_digest_version = models.IntegerField(
        blank=False,
        db_column="key_digest_version",
        validators=[MinValueValidator(1), MaxValueValidator(1)],
    )
    key_digest = models.TextField(
        blank=False,
        db_column="key_digest",
    )
    fingerprint_version = models.IntegerField(
        blank=False,
        db_column="fingerprint_version",
        validators=[MinValueValidator(1), MaxValueValidator(1)],
    )
    request_hash = models.TextField(
        blank=False,
        db_column="request_hash",
    )
    operation = models.ForeignKey(
        Operation,
        blank=False,
        db_column="operation_id",
        db_constraint=True,
        db_index=False,
        on_delete=models.DO_NOTHING,
        related_name="+",
    )
    expires_at_us = SQLiteInteger64Field(
        blank=False,
        db_column="expires_at_us",
        validators=[MinValueValidator(0)],
    )

    class Meta:
        db_table = "idempotency_key"
        managed = True
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "key_digest_version", "key_digest"),
                name="msv1_idem_owner_key_uq",
            ),
            models.UniqueConstraint(
                fields=("operation",),
                name="msv1_idem_operation_uq",
            ),
        ]
        indexes = [
            models.Index(fields=("conversation",), name="msv1_idem_conv_ix"),
            models.Index(fields=("expires_at_us",), name="msv1_idem_expires_ix"),
        ]


class GlobalModelSlot(models.Model):
    id = SQLiteInteger64Field(
        blank=False,
        db_column="id",
        primary_key=True,
        validators=[MinValueValidator(1), MaxValueValidator(1)],
    )
    operation = models.ForeignKey(
        Operation,
        blank=True,
        db_column="operation_id",
        db_constraint=True,
        db_index=False,
        null=True,
        on_delete=models.DO_NOTHING,
        related_name="+",
    )
    runner_id = models.TextField(
        blank=True,
        db_column="runner_id",
        null=True,
    )
    reservation_expires_at_us = SQLiteInteger64Field(
        blank=True,
        db_column="reservation_expires_at_us",
        null=True,
        validators=[MinValueValidator(0)],
    )
    lease_expires_at_us = SQLiteInteger64Field(
        blank=True,
        db_column="lease_expires_at_us",
        null=True,
        validators=[MinValueValidator(0)],
    )
    acquired_at_us = SQLiteInteger64Field(
        blank=True,
        db_column="acquired_at_us",
        null=True,
        validators=[MinValueValidator(0)],
    )

    class Meta:
        db_table = "global_model_slot"
        managed = True
        constraints = [
            models.UniqueConstraint(
                fields=("operation",),
                name="msv1_slot_operation_uq",
            ),
        ]
        indexes = []
