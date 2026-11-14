"""Read-only proof of one committed unknown-outcome acknowledgement."""

from uuid import UUID

from django.db import Error

from .application_validation import (
    SIGNED_INT64_MAX,
    require_explicit_positive_integer_id,
)
from .claim_confirmation import _open_owned_connection
from .reads import ReadInputError
from .repository import (
    AcknowledgementNotProven,
    AcknowledgementObserved,
    OperationStatus,
    OperationView,
    StoredFailureCategory,
)
from .safety import DatabaseSafetyError


_SNAPSHOT_SQL = (
    "SELECT id, owner_id, conversation_id, status, runner_id, request_hash, "
    "fingerprint_version, expected_version, provider_started_at_us, "
    "failure_category, blocks_conversation, acknowledged_at_us "
    "FROM operation WHERE id = ? AND owner_id = ?"
)
_FAILURES = frozenset(item.value for item in StoredFailureCategory)


def inspect_acknowledged_unknown(actor_id, operation_id):
    """Return proof only when one owned snapshot shows the acknowledgement."""
    require_explicit_positive_integer_id(actor_id)
    if type(operation_id) is not UUID:
        raise ReadInputError("identifier_type")
    try:
        rows = _fresh_snapshot(actor_id, operation_id)
    except (Error, DatabaseSafetyError):
        return AcknowledgementNotProven()
    operation = _proven(rows, actor_id, operation_id)
    if operation is None:
        return AcknowledgementNotProven()
    return AcknowledgementObserved(operation)


def _fresh_snapshot(actor_id, operation_id):
    """Read one owner-scoped operation through a connection the caller does not own."""
    owned = _open_owned_connection()
    try:
        with owned.cursor() as cursor:
            cursor.execute("BEGIN DEFERRED")
            try:
                cursor.execute(_SNAPSHOT_SQL, (operation_id.hex, actor_id))
                rows = cursor.fetchall()
            finally:
                cursor.execute("ROLLBACK")
        return rows
    finally:
        owned.close()


def _proven(rows, actor_id, operation_id):
    if type(rows) not in (list, tuple) or len(rows) != 1:
        return None
    row = rows[0]
    if len(row) != 12:
        return None
    observed_id = _identifier(row[0])
    if observed_id != operation_id or row[1] != actor_id or type(row[1]) is not int:
        return None
    conversation_id = _identifier(row[2])
    if conversation_id is None or row[3] != "OUTCOME_UNKNOWN":
        return None
    if type(row[4]) is not str or row[4] == "":
        return None
    if type(row[5]) is not str or not _whole(row[6]) or not _whole(row[7]):
        return None
    if row[8] is not None and not _timestamp(row[8]):
        return None
    if row[9] is not None and row[9] not in _FAILURES:
        return None
    if type(row[10]) is not int or row[10] != 0:
        return None
    if not _timestamp(row[11]):
        return None
    return OperationView(
        operation_id=observed_id,
        actor_id=actor_id,
        conversation_id=conversation_id,
        status=OperationStatus.OUTCOME_UNKNOWN,
        runner_id=row[4],
        request_hash=row[5],
        fingerprint_version=row[6],
        expected_version=row[7],
        provider_started_at=row[8],
        failure_category=(
            None if row[9] is None else StoredFailureCategory(row[9])
        ),
        blocks_conversation=False,
        acknowledged=True,
    )


def _identifier(value):
    if type(value) is UUID:
        return value
    if type(value) is not str:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _whole(value):
    return type(value) is int and 0 <= value <= SIGNED_INT64_MAX


def _timestamp(value):
    return _whole(value)
