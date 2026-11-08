"""Read-only proof of one durable RUNNING claim and its singleton slot."""

from uuid import UUID

from django.db import Error, connections

from .application_validation import (
    SIGNED_INT64_MAX,
    require_explicit_positive_integer_id,
)
from .config import EXECUTION_DEADLINE_US, FIXED_LEASE_US
from .prepare import _validate_runner
from .reads import ReadInputError
from .recovery import _running_corresponds
from .repository import ClaimNotConfirmed, DurableClaimConfirmed
from .safety import DatabaseSafetyError


def confirm_durable_claim(actor_id, operation_id, runner_id, claimed_at):
    """Return confirmation only when one fresh snapshot proves the claim."""
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
        return ClaimNotConfirmed()
    try:
        operation, slot = _fresh_snapshot(actor_id, operation_id)
    except (Error, DatabaseSafetyError):
        return ClaimNotConfirmed()
    if not _proven(operation, slot, runner_id, claimed_at, deadline, lease):
        return ClaimNotConfirmed()
    return DurableClaimConfirmed(
        operation_id=operation.id,
        runner_id=operation.runner_id,
        claimed_at=claimed_at,
    )


def _exact_offset(base, duration):
    if base > SIGNED_INT64_MAX - duration:
        return None
    return base + duration


def _open_owned_connection():
    """Return a new backend connection that is not the caller's connection."""
    return connections.create_connection("default")


def _fresh_snapshot(actor_id, operation_id):
    """Read the operation and slot through one owned connection."""
    owned = _open_owned_connection()
    try:
        with owned.cursor() as cursor:
            cursor.execute("BEGIN DEFERRED")
            try:
                operation = _read_operation(cursor, actor_id, operation_id)
                slot = _read_slot(cursor)
            finally:
                cursor.execute("ROLLBACK")
        return operation, slot
    finally:
        owned.close()


def _read_operation(cursor, actor_id, operation_id):
    cursor.execute(
        "SELECT status, blocks_conversation, runner_id, "
        "reservation_expires_at_us, provider_started_at_us, "
        "execution_deadline_us, lease_expires_at_us, finished_at_us, "
        "acknowledged_at_us FROM operation WHERE id = ? AND owner_id = ?",
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
    )


def _read_slot(cursor):
    cursor.execute(
        "SELECT operation_id, runner_id, reservation_expires_at_us, "
        "lease_expires_at_us, acquired_at_us "
        "FROM global_model_slot WHERE id = 1"
    )
    row = cursor.fetchone()
    if row is None:
        return None
    try:
        operation_id = None if row[0] is None else UUID(hex=str(row[0]))
    except ValueError:
        return None
    return _View(
        operation_id=operation_id,
        runner_id=row[1],
        reservation_expires_at_us=row[2],
        lease_expires_at_us=row[3],
        acquired_at_us=row[4],
    )


class _View:
    def __init__(self, **values):
        self.__dict__.update(values)


def _proven(operation, slot, runner_id, claimed_at, deadline, lease):
    if operation is None or slot is None:
        return False
    if operation.status != "RUNNING" or operation.runner_id != runner_id:
        return False
    if operation.provider_started_at_us != claimed_at:
        return False
    if operation.execution_deadline_us != deadline:
        return False
    if operation.lease_expires_at_us != lease:
        return False
    if slot.operation_id != operation.id or slot.runner_id != operation.runner_id:
        return False
    if slot.lease_expires_at_us != operation.lease_expires_at_us:
        return False
    if slot.reservation_expires_at_us is not None or slot.acquired_at_us is None:
        return False
    return _running_corresponds(operation, slot)
