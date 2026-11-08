"""Read-only classification of one pre-provider reservation."""

from uuid import UUID

from django.db import Error

from .application_validation import (
    SIGNED_INT64_MAX,
    require_explicit_positive_integer_id,
)
from .claim_confirmation import _open_owned_connection, _read_slot
from .prepare import _validate_runner
from .reads import ReadInputError
from .repository import (
    PreProviderFailureCategory,
    PreProviderFailureObserved,
    PreProviderStateNotProven,
    QueuedReservationObserved,
)
from .safety import DatabaseSafetyError


_PRE_PROVIDER_CATEGORIES = frozenset(
    item.value for item in PreProviderFailureCategory
)


def inspect_pre_provider_failure(actor_id, operation_id, runner_id):
    """Classify one committed reservation from a single owned snapshot."""
    require_explicit_positive_integer_id(actor_id)
    if type(operation_id) is not UUID:
        raise ReadInputError("identifier_type")
    _validate_runner(runner_id)
    try:
        operation, message_count, slot = _fresh_snapshot(actor_id, operation_id)
    except (Error, DatabaseSafetyError):
        return PreProviderStateNotProven()
    if _pre_provider_failure(operation, message_count, slot, operation_id):
        return PreProviderFailureObserved(
            operation_id=operation_id,
            failure_category=PreProviderFailureCategory(
                operation.failure_category
            ),
        )
    if _queued_reservation(operation, message_count, slot, operation_id, runner_id):
        return QueuedReservationObserved(
            operation_id=operation_id,
            runner_id=operation.runner_id,
            reservation_expires_at=operation.reservation_expires_at_us,
        )
    return PreProviderStateNotProven()


def _fresh_snapshot(actor_id, operation_id):
    """Read the operation, its messages, and the slot through one connection."""
    owned = _open_owned_connection()
    try:
        with owned.cursor() as cursor:
            cursor.execute("BEGIN DEFERRED")
            try:
                operation = _read_operation(cursor, actor_id, operation_id)
                message_count = _message_count(cursor, operation_id)
                slot = _read_slot(cursor)
            finally:
                cursor.execute("ROLLBACK")
        return operation, message_count, slot
    finally:
        owned.close()


def _read_operation(cursor, actor_id, operation_id):
    cursor.execute(
        "SELECT status, blocks_conversation, runner_id, "
        "reservation_expires_at_us, provider_started_at_us, "
        "execution_deadline_us, lease_expires_at_us, finished_at_us, "
        "acknowledged_at_us, failure_category "
        "FROM operation WHERE id = ? AND owner_id = ?",
        (operation_id.hex, actor_id),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return _View(
        id=operation_id,
        status=row[0],
        blocks_conversation=row[1],
        runner_id=row[2],
        reservation_expires_at_us=row[3],
        provider_started_at_us=row[4],
        execution_deadline_us=row[5],
        lease_expires_at_us=row[6],
        finished_at_us=row[7],
        acknowledged_at_us=row[8],
        failure_category=row[9],
    )


def _message_count(cursor, operation_id):
    cursor.execute(
        "SELECT COUNT(*) FROM message WHERE operation_id = ?",
        (operation_id.hex,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return row[0]


def _pre_provider_failure(operation, message_count, slot, operation_id):
    return (
        operation is not None
        and operation.status == "FAILED"
        and operation.blocks_conversation == 0
        and operation.runner_id is None
        and operation.reservation_expires_at_us is None
        and operation.provider_started_at_us is None
        and operation.execution_deadline_us is None
        and operation.lease_expires_at_us is None
        and operation.acknowledged_at_us is None
        and _timestamp(operation.finished_at_us)
        and operation.failure_category in _PRE_PROVIDER_CATEGORIES
        and message_count == 0
        and _slot_released(slot, operation_id)
    )


def _queued_reservation(operation, message_count, slot, operation_id, runner_id):
    return (
        operation is not None
        and operation.status == "QUEUED"
        and operation.blocks_conversation == 1
        and operation.runner_id == runner_id
        and _timestamp(operation.reservation_expires_at_us)
        and operation.provider_started_at_us is None
        and operation.execution_deadline_us is None
        and operation.lease_expires_at_us is None
        and operation.finished_at_us is None
        and operation.acknowledged_at_us is None
        and operation.failure_category is None
        and message_count == 0
        and slot is not None
        and slot.operation_id == operation_id
        and slot.runner_id == runner_id
        and slot.reservation_expires_at_us == operation.reservation_expires_at_us
        and slot.lease_expires_at_us is None
        and _timestamp(slot.acquired_at_us)
    )


def _slot_released(slot, operation_id):
    if slot is None or slot.operation_id == operation_id:
        return False
    if slot.operation_id is None:
        return (
            slot.runner_id is None
            and slot.reservation_expires_at_us is None
            and slot.lease_expires_at_us is None
            and slot.acquired_at_us is None
        )
    return type(slot.operation_id) is UUID


def _timestamp(value):
    return type(value) is int and 0 <= value <= SIGNED_INT64_MAX


class _View:
    def __init__(self, **values):
        self.__dict__.update(values)
