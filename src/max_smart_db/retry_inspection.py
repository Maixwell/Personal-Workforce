"""Read-only proof of one committed explicit retry."""

from uuid import UUID

from django.db import Error

from .application_validation import require_explicit_positive_integer_id
from .claim_confirmation import _open_owned_connection, _read_slot
from .idempotency import IdempotencyKeyError, idempotency_key_digest
from .prepare import _validate_runner
from .reads import ReadInputError
from .repository import RetryCreationNotProven, RetryCreationObserved
from .safety import DatabaseSafetyError


def inspect_committed_retry(
    actor_id,
    operation_id,
    source_operation_id,
    runner_id,
    idempotency_key,
):
    """Prove one committed retry from a single owned snapshot."""
    require_explicit_positive_integer_id(actor_id)
    if type(operation_id) is not UUID or type(source_operation_id) is not UUID:
        raise ReadInputError("identifier_type")
    _validate_runner(runner_id)
    try:
        key_digest = idempotency_key_digest(idempotency_key)
    except IdempotencyKeyError:
        return RetryCreationNotProven()
    try:
        operation, source, slot, messages, binding = _fresh_snapshot(
            actor_id,
            operation_id,
            source_operation_id,
            key_digest,
        )
    except (Error, DatabaseSafetyError):
        return RetryCreationNotProven()
    if not _proven(
        operation,
        source,
        slot,
        messages,
        binding,
        operation_id,
        source_operation_id,
        runner_id,
        actor_id,
        key_digest,
    ):
        return RetryCreationNotProven()
    return RetryCreationObserved(
        operation_id=operation_id,
        retry_of=source_operation_id,
        runner_id=operation["runner_id"],
        conversation_id=operation["conversation_id"],
        request_hash=operation["request_hash"],
        fingerprint_version=operation["fingerprint_version"],
        expected_version=operation["expected_version"],
        reservation_expires_at=operation["reservation_expires_at_us"],
        duplicate_charge_warning=source["status"] == "OUTCOME_UNKNOWN",
    )


def _fresh_snapshot(actor_id, operation_id, source_operation_id, key_digest):
    owned = _open_owned_connection()
    try:
        with owned.cursor() as cursor:
            cursor.execute("BEGIN DEFERRED")
            try:
                operation = _read_operation(cursor, actor_id, operation_id)
                source = _read_source(cursor, actor_id, source_operation_id)
                slot = _read_slot(cursor)
                messages = _message_count(cursor, operation_id)
                binding = _read_binding(cursor, actor_id, key_digest)
            finally:
                cursor.execute("ROLLBACK")
        return operation, source, slot, messages, binding
    finally:
        owned.close()


def _read_operation(cursor, actor_id, operation_id):
    cursor.execute(
        "SELECT conversation_id, status, blocks_conversation, runner_id, "
        "request_content, request_hash, fingerprint_version, expected_version, "
        "retry_of, reservation_expires_at_us, provider_started_at_us, "
        "execution_deadline_us, lease_expires_at_us, finished_at_us, "
        "acknowledged_at_us FROM operation WHERE id = ? AND owner_id = ?",
        (operation_id.hex, actor_id),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    try:
        conversation_id = UUID(hex=str(row[0]))
        retry_of = None if row[8] is None else UUID(hex=str(row[8]))
    except ValueError:
        return None
    return {
        "conversation_id": conversation_id,
        "status": row[1],
        "blocks_conversation": row[2],
        "runner_id": row[3],
        "request_content": row[4],
        "request_hash": row[5],
        "fingerprint_version": row[6],
        "expected_version": row[7],
        "retry_of": retry_of,
        "reservation_expires_at_us": row[9],
        "provider_started_at_us": row[10],
        "execution_deadline_us": row[11],
        "lease_expires_at_us": row[12],
        "finished_at_us": row[13],
        "acknowledged_at_us": row[14],
    }


def _read_source(cursor, actor_id, source_operation_id):
    cursor.execute(
        "SELECT conversation_id, status, blocks_conversation, request_content, "
        "acknowledged_at_us FROM operation WHERE id = ? AND owner_id = ?",
        (source_operation_id.hex, actor_id),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    try:
        conversation_id = UUID(hex=str(row[0]))
    except ValueError:
        return None
    return {
        "conversation_id": conversation_id,
        "status": row[1],
        "blocks_conversation": row[2],
        "request_content": row[3],
        "acknowledged_at_us": row[4],
    }


def _read_binding(cursor, actor_id, key_digest):
    cursor.execute(
        "SELECT owner_id, key_digest_version, key_digest, operation_id, "
        "expires_at_us FROM idempotency_key "
        "WHERE owner_id = ? AND key_digest_version = 1 AND key_digest = ?",
        (actor_id, key_digest),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        return None
    row = rows[0]
    try:
        bound_operation_id = UUID(hex=str(row[3]))
    except ValueError:
        return None
    return {
        "owner_id": row[0],
        "key_digest_version": row[1],
        "key_digest": row[2],
        "operation_id": bound_operation_id,
        "expires_at_us": row[4],
    }


def _message_count(cursor, operation_id):
    cursor.execute(
        "SELECT COUNT(*) FROM message WHERE operation_id = ?",
        (operation_id.hex,),
    )
    row = cursor.fetchone()
    if row is None or type(row[0]) is not int:
        return None
    return row[0]


def _proven(
    operation,
    source,
    slot,
    messages,
    binding,
    operation_id,
    source_operation_id,
    runner_id,
    actor_id,
    key_digest,
):
    if (
        operation is None
        or source is None
        or slot is None
        or messages != 0
        or not _binding_matches(binding, actor_id, operation_id, key_digest)
    ):
        return False
    if operation["retry_of"] != source_operation_id:
        return False
    if operation["conversation_id"] != source["conversation_id"]:
        return False
    if operation["request_content"] != source["request_content"]:
        return False
    if operation["status"] != "QUEUED" or operation["blocks_conversation"] != 1:
        return False
    if operation["runner_id"] != runner_id:
        return False
    if operation["provider_started_at_us"] is not None:
        return False
    if operation["execution_deadline_us"] is not None:
        return False
    if operation["lease_expires_at_us"] is not None:
        return False
    if operation["finished_at_us"] is not None:
        return False
    if operation["acknowledged_at_us"] is not None:
        return False
    if type(operation["reservation_expires_at_us"]) is not int:
        return False
    if type(operation["fingerprint_version"]) is not int:
        return False
    if type(operation["expected_version"]) is not int:
        return False
    if type(operation["request_hash"]) is not str:
        return False
    if slot.operation_id != operation_id or slot.runner_id != runner_id:
        return False
    if slot.reservation_expires_at_us != operation["reservation_expires_at_us"]:
        return False
    if slot.lease_expires_at_us is not None or slot.acquired_at_us is None:
        return False
    if source["blocks_conversation"] != 0:
        return False
    if source["status"] in ("FAILED", "CANCELLED"):
        return source["acknowledged_at_us"] is None
    return (
        source["status"] == "OUTCOME_UNKNOWN"
        and source["acknowledged_at_us"] is not None
    )


def _binding_matches(binding, actor_id, operation_id, key_digest):
    if binding is None:
        return False
    if type(binding["owner_id"]) is not int or binding["owner_id"] != actor_id:
        return False
    if binding["operation_id"] != operation_id:
        return False
    if binding["key_digest_version"] != 1:
        return False
    if type(binding["key_digest"]) is not str or binding["key_digest"] != key_digest:
        return False
    return (
        type(binding["expires_at_us"]) is int
        and binding["expires_at_us"] >= 0
    )
