"""Atomic persistence of one successful RUNNING operation."""

import re
import uuid
from uuid import UUID

from django.db import IntegrityError, OperationalError, transaction

from .application_validation import (
    SIGNED_INT64_MAX,
    require_explicit_positive_integer_id,
)
from .creation import _read_clock_once
from .models import Conversation, GlobalModelSlot, Message, Operation
from .prepare import _validate_runner
from .reads import _message_view, _operation_view
from .recovery import (
    _ConditionalRace,
    _is_lock_failure,
    _release_slot,
    _running_corresponds,
)
from .repository import (
    AlreadySucceeded,
    Applied,
    ClaimedOperation,
    CommitAmbiguous,
    DatabaseBusy,
    InvalidState,
    OperationStatus,
    OwnershipLost,
    RecoveredUnknown,
    RepositoryIntegrityError,
    SlotMismatch,
    StaleVersion,
)


_HASH = re.compile(r"[0-9a-f]{64}")


class ApplySuccessInputError(ValueError):
    """The claim token or assistant output is invalid."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def apply_success(repository, claimed, output_text):
    """Commit one user/assistant turn and complete its RUNNING operation."""
    _validate_claim(claimed)
    _validate_runner(repository.runner_id)
    user_message_id = uuid.uuid4()
    assistant_message_id = uuid.uuid4()
    body_complete = False
    started = _Started()
    try:
        with transaction.atomic(durable=True):
            applied_at = _read_clock_once(repository.clock)
            operation = _owned_operation(claimed)
            initial = _initial_result(operation, claimed)
            if initial is not None:
                result = initial
            else:
                _validate_output(output_text)
                slot = GlobalModelSlot.objects.filter(pk=1).first()
                guarded = _guard_running(
                    operation,
                    slot,
                    claimed,
                    repository.runner_id,
                )
                if guarded is not None:
                    result = guarded
                else:
                    conversation = Conversation.objects.filter(
                        id=claimed.conversation_id,
                        owner_id=claimed.actor_id,
                    ).first()
                    conversation_result = _conversation_result(
                        conversation,
                        operation,
                    )
                    if conversation_result is not None:
                        result = conversation_result
                    else:
                        _apply(
                            operation,
                            slot,
                            conversation,
                            claimed,
                            output_text,
                            user_message_id,
                            assistant_message_id,
                            applied_at,
                            started,
                        )
                        operation.refresh_from_db()
                        result = Applied(_operation_view(operation))
            body_complete = True
    except _ConditionalRace:
        return _classify_race(claimed, repository.runner_id)
    except OperationalError as error:
        if body_complete:
            return _ambiguous(claimed)
        if not started.mutation and _is_lock_failure(error):
            return DatabaseBusy()
        raise RepositoryIntegrityError() from None
    except IntegrityError:
        if body_complete:
            return _ambiguous(claimed)
        raise RepositoryIntegrityError() from None
    except Exception:
        if body_complete:
            return _ambiguous(claimed)
        raise
    return result


class _Started:
    mutation = False


def _validate_claim(claimed):
    if type(claimed) is not ClaimedOperation:
        raise ApplySuccessInputError("claimed_type")
    require_explicit_positive_integer_id(claimed.actor_id)
    if type(claimed.operation_id) is not UUID:
        raise ApplySuccessInputError("operation_id_type")
    if type(claimed.conversation_id) is not UUID:
        raise ApplySuccessInputError("conversation_id_type")
    if (
        type(claimed.request_hash) is not str
        or _HASH.fullmatch(claimed.request_hash) is None
    ):
        raise ApplySuccessInputError("request_hash_invalid")
    _exact_int(claimed.fingerprint_version, 1, "fingerprint_version_invalid")
    _exact_int(claimed.expected_version, 0, "expected_version_invalid")
    _exact_int(claimed.claimed_at, 0, "claimed_at_invalid")
    _exact_int(
        claimed.execution_deadline,
        0,
        "execution_deadline_invalid",
    )
    _exact_int(claimed.lease_expires_at, 0, "lease_expires_at_invalid")
    if claimed.execution_deadline > claimed.lease_expires_at:
        raise ApplySuccessInputError("claim_timing_invalid")
    _validate_runner(claimed.runner_id)


def _validate_output(output_text):
    if type(output_text) is not str:
        raise ApplySuccessInputError("output_type")
    try:
        output_text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ApplySuccessInputError("output_encoding") from error
    if "\x00" in output_text:
        raise ApplySuccessInputError("output_nul")
    if not output_text.strip():
        raise ApplySuccessInputError("output_blank")


def _exact_int(value, minimum, code):
    if type(value) is not int or value < minimum or value > SIGNED_INT64_MAX:
        raise ApplySuccessInputError(code)


def _owned_operation(claimed):
    return Operation.objects.filter(
        id=claimed.operation_id,
        owner_id=claimed.actor_id,
    ).first()


def _initial_result(operation, claimed):
    if operation is None or not _same_request(operation, claimed):
        return OwnershipLost()
    if operation.status == "SUCCEEDED":
        return _already_succeeded(operation)
    if operation.status == "OUTCOME_UNKNOWN":
        return RecoveredUnknown(reconcile_required=True)
    if operation.status != "RUNNING":
        return InvalidState(OperationStatus(operation.status))
    return None


def _guard_running(operation, slot, claimed, runner_id):
    if (
        claimed.runner_id != runner_id
        or operation.runner_id != claimed.runner_id
        or operation.provider_started_at_us != claimed.claimed_at
        or operation.execution_deadline_us != claimed.execution_deadline
        or operation.lease_expires_at_us != claimed.lease_expires_at
    ):
        return OwnershipLost()
    if slot is None or slot.operation_id != operation.id:
        return SlotMismatch()
    if slot.runner_id != operation.runner_id:
        return OwnershipLost()
    if not _running_corresponds(operation, slot):
        return SlotMismatch()
    return None


def _conversation_result(conversation, operation):
    if conversation is None:
        return OwnershipLost()
    if conversation.archived_at_us is not None:
        return InvalidState(OperationStatus(operation.status))
    if conversation.version != operation.expected_version:
        return StaleVersion(actual=conversation.version)
    if (
        type(conversation.next_sequence) is not int
        or conversation.next_sequence < 1
        or conversation.next_sequence > SIGNED_INT64_MAX - 2
        or conversation.version > SIGNED_INT64_MAX - 1
    ):
        raise RepositoryIntegrityError()
    return None


def _apply(
    operation,
    slot,
    conversation,
    claimed,
    output_text,
    user_message_id,
    assistant_message_id,
    applied_at,
    started,
):
    observed = conversation.next_sequence
    conversation_count = Conversation.objects.filter(
        id=conversation.id,
        owner_id=claimed.actor_id,
        version=claimed.expected_version,
        next_sequence=observed,
        archived_at_us__isnull=True,
    ).update(
        version=claimed.expected_version + 1,
        next_sequence=observed + 2,
        updated_at_us=applied_at,
    )
    started.mutation = conversation_count != 0
    _require_one(conversation_count)
    _insert_message(
        user_message_id,
        conversation.id,
        observed,
        "user",
        operation.request_content,
        operation.id,
        applied_at,
    )
    _insert_message(
        assistant_message_id,
        conversation.id,
        observed + 1,
        "assistant",
        output_text,
        operation.id,
        applied_at,
    )
    operation_count = Operation.objects.filter(
        id=operation.id,
        owner_id=claimed.actor_id,
        conversation_id=claimed.conversation_id,
        status="RUNNING",
        blocks_conversation=1,
        runner_id=claimed.runner_id,
        reservation_expires_at_us__isnull=True,
        provider_started_at_us=claimed.claimed_at,
        execution_deadline_us=claimed.execution_deadline,
        lease_expires_at_us=claimed.lease_expires_at,
        finished_at_us__isnull=True,
        acknowledged_at_us__isnull=True,
        request_hash=claimed.request_hash,
        fingerprint_version=claimed.fingerprint_version,
        expected_version=claimed.expected_version,
    ).update(
        status="SUCCEEDED",
        blocks_conversation=0,
        runner_id=None,
        finished_at_us=applied_at,
    )
    _require_one(operation_count)
    _release_slot(slot, operation)


def _insert_message(
    message_id,
    conversation_id,
    sequence,
    role,
    content,
    operation_id,
    created_at,
):
    Message.objects.create(
        id=message_id,
        conversation_id=conversation_id,
        sequence=sequence,
        role=role,
        content=content,
        operation_id=operation_id,
        created_at_us=created_at,
    )


def _already_succeeded(operation):
    messages = tuple(
        Message.objects.filter(operation_id=operation.id).order_by("sequence")
    )
    if (
        len(messages) != 2
        or messages[0].role != "user"
        or messages[1].role != "assistant"
        or messages[1].sequence != messages[0].sequence + 1
        or messages[0].conversation_id != operation.conversation_id
        or messages[1].conversation_id != operation.conversation_id
        or messages[0].content != operation.request_content
    ):
        raise RepositoryIntegrityError()
    return AlreadySucceeded(
        operation=_operation_view(operation),
        user_message=_message_view(messages[0]),
        assistant_message=_message_view(messages[1]),
    )


def _same_request(operation, claimed):
    return (
        operation.conversation_id == claimed.conversation_id
        and operation.request_hash == claimed.request_hash
        and operation.fingerprint_version == claimed.fingerprint_version
        and operation.expected_version == claimed.expected_version
        and operation.provider_started_at_us == claimed.claimed_at
        and operation.execution_deadline_us == claimed.execution_deadline
        and operation.lease_expires_at_us == claimed.lease_expires_at
    )


def _require_one(rowcount):
    if rowcount == 1:
        return
    if rowcount == 0:
        raise _ConditionalRace()
    raise RepositoryIntegrityError()


def _classify_race(claimed, runner_id):
    operation = _owned_operation(claimed)
    initial = _initial_result(operation, claimed)
    if initial is not None:
        return initial
    slot = GlobalModelSlot.objects.filter(pk=1).first()
    guarded = _guard_running(operation, slot, claimed, runner_id)
    if guarded is not None:
        return guarded
    conversation = Conversation.objects.filter(
        id=claimed.conversation_id,
        owner_id=claimed.actor_id,
    ).first()
    conversation_result = _conversation_result(conversation, operation)
    if conversation_result is not None:
        return conversation_result
    return OwnershipLost()


def _ambiguous(claimed):
    return CommitAmbiguous(
        operation_id=claimed.operation_id,
        runner_id=claimed.runner_id,
        claimed_at=claimed.claimed_at,
        conversation_id=claimed.conversation_id,
    )
