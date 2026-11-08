"""Read-only classification of one durable operation outcome."""

from uuid import UUID

from django.db import Error

from .application_validation import (
    SIGNED_INT64_MAX,
    require_explicit_positive_integer_id,
)
from .claim_confirmation import (
    _exact_offset,
    _open_owned_connection,
    _proven,
    _read_slot,
)
from .config import EXECUTION_DEADLINE_US, FIXED_LEASE_US
from .prepare import _validate_runner
from .reads import ReadInputError
from .repository import (
    DefiniteProviderCategory,
    DurableClaimConfirmed,
    DurableFailureObserved,
    DurableSuccessObserved,
    DurableUnknownObserved,
    MessageRole,
    MessageView,
    OutcomeNotProven,
)
from .safety import DatabaseSafetyError


_DEFINITE_FAILURES = frozenset(item.value for item in DefiniteProviderCategory)
_RETAINED_CATEGORIES = frozenset((
    None,
    "provider_outcome_uncertain",
    "persistence_failed",
))


def inspect_durable_outcome(actor_id, operation_id, runner_id, claimed_at):
    """Classify one committed outcome from a single owned snapshot."""
    require_explicit_positive_integer_id(actor_id)
    if type(operation_id) is not UUID:
        raise ReadInputError("identifier_type")
    _validate_runner(runner_id)
    if type(claimed_at) is not int:
        raise ReadInputError("claimed_at_type")
    if claimed_at < 0 or claimed_at > SIGNED_INT64_MAX:
        raise ReadInputError("claimed_at_range")
    deadline = _exact_offset(claimed_at, EXECUTION_DEADLINE_US)
    lease = _exact_offset(claimed_at, FIXED_LEASE_US)
    if deadline is None or lease is None:
        return OutcomeNotProven()
    try:
        operation, conversation, messages, slot = _fresh_snapshot(
            actor_id,
            operation_id,
        )
    except (Error, DatabaseSafetyError):
        return OutcomeNotProven()
    return _classify(
        operation,
        conversation,
        messages,
        slot,
        operation_id,
        runner_id,
        claimed_at,
        deadline,
        lease,
    )


def _fresh_snapshot(actor_id, operation_id):
    """Read outcome evidence through one owned connection."""
    owned = _open_owned_connection()
    try:
        with owned.cursor() as cursor:
            cursor.execute("BEGIN DEFERRED")
            try:
                operation = _read_operation(cursor, actor_id, operation_id)
                conversation = None
                messages = ()
                if operation is not None:
                    conversation = _read_conversation(
                        cursor,
                        operation.conversation_id,
                        actor_id,
                    )
                    messages = _read_messages(cursor, operation_id)
                slot = _read_slot(cursor)
            finally:
                cursor.execute("ROLLBACK")
        return operation, conversation, messages, slot
    finally:
        owned.close()


def _read_operation(cursor, actor_id, operation_id):
    cursor.execute(
        "SELECT conversation_id, status, blocks_conversation, runner_id, "
        "reservation_expires_at_us, provider_started_at_us, "
        "execution_deadline_us, lease_expires_at_us, finished_at_us, "
        "acknowledged_at_us, expected_version, request_content, "
        "failure_category FROM operation WHERE id = ? AND owner_id = ?",
        (operation_id.hex, actor_id),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    try:
        conversation_id = UUID(hex=str(row[0]))
    except (TypeError, ValueError):
        return None
    return _View(
        id=operation_id,
        conversation_id=conversation_id,
        status=row[1],
        blocks_conversation=row[2],
        runner_id=row[3],
        reservation_expires_at_us=row[4],
        provider_started_at_us=row[5],
        execution_deadline_us=row[6],
        lease_expires_at_us=row[7],
        finished_at_us=row[8],
        acknowledged_at_us=row[9],
        expected_version=row[10],
        request_content=row[11],
        failure_category=row[12],
    )


def _read_conversation(cursor, conversation_id, actor_id):
    cursor.execute(
        "SELECT version, next_sequence, archived_at_us "
        "FROM conversation WHERE id = ? AND owner_id = ?",
        (conversation_id.hex, actor_id),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return _View(
        version=row[0],
        next_sequence=row[1],
        archived_at_us=row[2],
    )


def _read_messages(cursor, operation_id):
    cursor.execute(
        "SELECT id, conversation_id, sequence, role, content, created_at_us "
        "FROM message WHERE operation_id = ? ORDER BY sequence",
        (operation_id.hex,),
    )
    return tuple(cursor.fetchall())


def _classify(
    operation,
    conversation,
    messages,
    slot,
    operation_id,
    runner_id,
    claimed_at,
    deadline,
    lease,
):
    if operation is None:
        return OutcomeNotProven()
    if operation.status == "SUCCEEDED":
        proven = _succeeded(
            operation,
            conversation,
            messages,
            claimed_at,
            deadline,
            lease,
        )
        if proven is None:
            return OutcomeNotProven()
        user_message, assistant_message = proven
        return DurableSuccessObserved(
            operation_id=operation_id,
            claimed_at=claimed_at,
            user_message=user_message,
            assistant_message=assistant_message,
        )
    if _proven(operation, slot, runner_id, claimed_at, deadline, lease):
        return DurableClaimConfirmed(
            operation_id=operation.id,
            runner_id=operation.runner_id,
            claimed_at=claimed_at,
        )
    if operation.status == "OUTCOME_UNKNOWN" and _unknown(
        operation,
        messages,
        runner_id,
        claimed_at,
        deadline,
        lease,
    ):
        return DurableUnknownObserved(
            operation_id=operation_id,
            runner_id=operation.runner_id,
            claimed_at=claimed_at,
        )
    if operation.status == "FAILED":
        category = _failed(operation, messages, claimed_at, deadline, lease)
        if category is None:
            return OutcomeNotProven()
        return DurableFailureObserved(
            operation_id=operation_id,
            claimed_at=claimed_at,
            failure_category=category,
        )
    return OutcomeNotProven()


def _same_claim_timing(operation, claimed_at, deadline, lease):
    return (
        operation.provider_started_at_us == claimed_at
        and operation.execution_deadline_us == deadline
        and operation.lease_expires_at_us == lease
        and operation.reservation_expires_at_us is None
        and operation.acknowledged_at_us is None
    )


def _succeeded(operation, conversation, messages, claimed_at, deadline, lease):
    if not _same_claim_timing(operation, claimed_at, deadline, lease):
        return None
    if (
        operation.blocks_conversation != 0
        or operation.runner_id is not None
        or type(operation.finished_at_us) is not int
        or operation.finished_at_us < 0
        or operation.failure_category not in _RETAINED_CATEGORIES
        or type(operation.expected_version) is not int
        or type(operation.request_content) is not str
        or not operation.request_content
    ):
        return None
    if (
        conversation is None
        or conversation.archived_at_us is not None
        or type(conversation.version) is not int
        or type(conversation.next_sequence) is not int
        or operation.expected_version > SIGNED_INT64_MAX - 1
        or conversation.version != operation.expected_version + 1
    ):
        return None
    pair = _message_pair(operation, messages)
    if pair is None:
        return None
    user_message, _assistant_message = pair
    if (
        user_message.sequence > SIGNED_INT64_MAX - 2
        or conversation.next_sequence != user_message.sequence + 2
    ):
        return None
    return pair


def _message_pair(operation, messages):
    if len(messages) != 2:
        return None
    if not _created_matches(messages[0], operation) or not _created_matches(
        messages[1],
        operation,
    ):
        return None
    user = _message_view(messages[0], operation)
    assistant = _message_view(messages[1], operation)
    if user is None or assistant is None:
        return None
    if user.role is not MessageRole.USER or assistant.role is not MessageRole.ASSISTANT:
        return None
    if assistant.sequence != user.sequence + 1:
        return None
    if user.content != operation.request_content or not assistant.content.strip():
        return None
    if "\x00" in user.content or "\x00" in assistant.content:
        return None
    return user, assistant


def _message_view(row, operation):
    try:
        message_id = UUID(hex=str(row[0]))
        conversation_id = UUID(hex=str(row[1]))
    except (TypeError, ValueError):
        return None
    sequence = row[2]
    role = row[3]
    content = row[4]
    if (
        conversation_id != operation.conversation_id
        or type(sequence) is not int
        or sequence < 1
        or type(content) is not str
        or role not in ("user", "assistant")
    ):
        return None
    return MessageView(
        message_id=message_id,
        conversation_id=conversation_id,
        sequence=sequence,
        role=MessageRole(role),
        content=content,
        operation_id=operation.id,
    )


def _created_matches(row, operation):
    created_at = row[5]
    return type(created_at) is int and created_at == operation.finished_at_us


def _unknown(operation, messages, runner_id, claimed_at, deadline, lease):
    return (
        _same_claim_timing(operation, claimed_at, deadline, lease)
        and operation.blocks_conversation == 1
        and operation.runner_id == runner_id
        and type(operation.finished_at_us) is int
        and operation.finished_at_us >= 0
        and operation.failure_category in _RETAINED_CATEGORIES
        and len(messages) == 0
    )


def _failed(operation, messages, claimed_at, deadline, lease):
    if not (
        _same_claim_timing(operation, claimed_at, deadline, lease)
        and operation.blocks_conversation == 0
        and operation.runner_id is None
        and type(operation.finished_at_us) is int
        and operation.finished_at_us >= 0
        and operation.failure_category in _DEFINITE_FAILURES
        and len(messages) == 0
    ):
        return None
    return DefiniteProviderCategory(operation.failure_category)


class _View:
    def __init__(self, **values):
        self.__dict__.update(values)
