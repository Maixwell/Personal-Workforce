"""Owner-scoped repository reads. This module does not write."""

from contextlib import contextmanager
from uuid import UUID

from django.db import connection

from .models import Conversation, Message, Operation
from .repository import (
    ConversationView,
    MessageRole,
    MessageView,
    NotFound,
    OperationStatus,
    OperationView,
    RepositoryIntegrityError,
    Snapshot,
    StoredFailureCategory,
)


class ReadInputError(ValueError):
    """A read argument does not match the accepted input contract."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class SqliteConversationReader:
    """Read conversations, operations, and committed messages."""

    def __init__(self, clock, runner_id):
        self.clock = clock
        self.runner_id = runner_id

    def get_conversation(self, actor_id, conversation_id):
        """Return one owned conversation, or NotFound."""
        _require_actor(actor_id)
        _require_uuid(conversation_id)
        row = _owned_conversation(actor_id, conversation_id)
        if row is None:
            return NotFound()
        return _conversation_view(row)

    def get_operation(self, actor_id, operation_id):
        """Return one owned operation, or NotFound."""
        _require_actor(actor_id)
        _require_uuid(operation_id)
        row = Operation.objects.filter(
            id=operation_id,
            owner_id=actor_id,
        ).first()
        if row is None:
            return NotFound()
        return _operation_view(row)

    def confirm_durable_claim(
        self,
        actor_id,
        operation_id,
        runner_id,
        claimed_at,
    ):
        """Prove one committed RUNNING claim without writing."""
        from .claim_confirmation import confirm_durable_claim

        return confirm_durable_claim(
            actor_id,
            operation_id,
            runner_id,
            claimed_at,
        )

    def inspect_durable_outcome(
        self,
        actor_id,
        operation_id,
        runner_id,
        claimed_at,
    ):
        """Classify one committed outcome without writing."""
        from .outcome_inspection import inspect_durable_outcome

        return inspect_durable_outcome(
            actor_id,
            operation_id,
            runner_id,
            claimed_at,
        )

    def inspect_pre_provider_failure(self, actor_id, operation_id, runner_id):
        """Classify one pre-provider reservation without writing."""
        from .pre_provider_inspection import inspect_pre_provider_failure

        return inspect_pre_provider_failure(
            actor_id,
            operation_id,
            runner_id,
        )

    def load_snapshot(self, actor_id, conversation_id):
        """Return one owned snapshot from a single deferred read."""
        _require_actor(actor_id)
        _require_uuid(conversation_id)
        with _deferred_read():
            row = _owned_conversation(actor_id, conversation_id)
            if row is None:
                return NotFound()
            messages = _message_views(conversation_id, None)
            return Snapshot(_conversation_view(row), tuple(messages))

    def list_messages(self, actor_id, conversation_id, after_sequence=None):
        """Return sequence-ascending messages strictly after the cursor."""
        _require_actor(actor_id)
        _require_uuid(conversation_id)
        cursor = _require_cursor(after_sequence)
        with _deferred_read():
            if _owned_conversation(actor_id, conversation_id) is None:
                return NotFound()
            return tuple(_message_views(conversation_id, cursor))


def _require_actor(actor_id):
    if type(actor_id) is not int:
        raise ReadInputError("actor_type")


def _require_uuid(value):
    if type(value) is not UUID:
        raise ReadInputError("identifier_type")


def _require_cursor(value):
    if value is None:
        return None
    if type(value) is not int:
        raise ReadInputError("cursor_type")
    if value < 0:
        raise ReadInputError("cursor_range")
    return value


@contextmanager
def _deferred_read():
    if connection.in_atomic_block:
        raise ReadInputError("read_inside_write_transaction")
    connection.ensure_connection()
    with connection.cursor() as cursor:
        cursor.execute("BEGIN DEFERRED")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("ROLLBACK")


def _owned_conversation(actor_id, conversation_id):
    return Conversation.objects.filter(
        id=conversation_id,
        owner_id=actor_id,
    ).first()


def _message_views(conversation_id, after_sequence):
    queryset = Message.objects.filter(conversation_id=conversation_id)
    if after_sequence is not None:
        queryset = queryset.filter(sequence__gt=after_sequence)
    return [
        _message_view(row)
        for row in queryset.order_by("sequence")
    ]


def _conversation_view(row):
    return ConversationView(
        conversation_id=row.id,
        actor_id=row.owner_id,
        role_slug=row.role_id,
        version=row.version,
        next_sequence=row.next_sequence,
        title=row.title,
    )


def _operation_view(row):
    return OperationView(
        operation_id=row.id,
        actor_id=row.owner_id,
        conversation_id=row.conversation_id,
        status=OperationStatus(row.status),
        runner_id=row.runner_id,
        request_hash=row.request_hash,
        fingerprint_version=row.fingerprint_version,
        expected_version=row.expected_version,
        provider_started_at=row.provider_started_at_us,
        failure_category=(
            None
            if row.failure_category is None
            else StoredFailureCategory(row.failure_category)
        ),
        blocks_conversation=_zero_or_one(row.blocks_conversation),
        acknowledged=row.acknowledged_at_us is not None,
    )


def _message_view(row):
    return MessageView(
        message_id=row.id,
        conversation_id=row.conversation_id,
        sequence=row.sequence,
        role=MessageRole(row.role),
        content=row.content,
        operation_id=row.operation_id,
    )


def _zero_or_one(value):
    if value == 0:
        return False
    if value == 1:
        return True
    raise RepositoryIntegrityError()
