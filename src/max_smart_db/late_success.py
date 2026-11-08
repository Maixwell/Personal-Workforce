"""Guarded late success after recovery has released the slot."""

import uuid

from django.db import IntegrityError, OperationalError, transaction

from .application_validation import SIGNED_INT64_MAX
from .apply_success import (
    ApplySuccessInputError,
    _already_succeeded,
    _insert_message,
    _require_one,
    _validate_claim,
    _validate_output,
)
from .creation import _read_clock_once
from .models import Conversation, Operation
from .prepare import _validate_runner
from .reads import _operation_view
from .recovery import _ConditionalRace, _is_lock_failure
from .repository import (
    AcknowledgedDiscard,
    CommitAmbiguous,
    DatabaseBusy,
    FingerprintMismatch,
    InvalidStateDiscard,
    Reconciled,
    RepositoryIntegrityError,
    RetainedResult,
    RunnerMismatchDiscard,
    StaleVersionDiscard,
)


def reconcile_late_success(repository, retained_result):
    """Commit one retained success without touching the singleton slot."""
    if type(retained_result) is not RetainedResult:
        raise ApplySuccessInputError("retained_type")
    claimed = retained_result.claimed_operation
    _validate_claim(claimed)
    _validate_runner(repository.runner_id)
    user_message_id = uuid.uuid4()
    assistant_message_id = uuid.uuid4()
    body_complete = False
    started = _Started()
    try:
        with transaction.atomic(durable=True):
            reconciled_at = _read_clock_once(repository.clock)
            operation = Operation.objects.filter(
                id=claimed.operation_id,
                owner_id=claimed.actor_id,
            ).first()
            decision = _decision(operation, claimed, repository.runner_id)
            if decision == "reconcile":
                _validate_output(retained_result.output_text)
                conversation = Conversation.objects.filter(
                    id=claimed.conversation_id,
                    owner_id=claimed.actor_id,
                ).first()
                conversation_decision = _conversation_decision(
                    conversation,
                    claimed,
                )
                if conversation_decision is not None:
                    result = conversation_decision
                else:
                    _reconcile(
                        operation,
                        conversation,
                        claimed,
                        retained_result.output_text,
                        user_message_id,
                        assistant_message_id,
                        reconciled_at,
                        started,
                    )
                    operation.refresh_from_db()
                    result = Reconciled(_operation_view(operation))
            elif decision == "succeeded":
                result = _already_succeeded(operation)
            else:
                result = decision
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


def _decision(operation, claimed, runner_id):
    if operation is None or operation.conversation_id != claimed.conversation_id:
        return InvalidStateDiscard()
    if (
        operation.request_hash != claimed.request_hash
        or operation.fingerprint_version != claimed.fingerprint_version
    ):
        return FingerprintMismatch()
    if operation.expected_version != claimed.expected_version:
        return StaleVersionDiscard()
    if (
        operation.provider_started_at_us != claimed.claimed_at
        or operation.execution_deadline_us != claimed.execution_deadline
        or operation.lease_expires_at_us != claimed.lease_expires_at
    ):
        return InvalidStateDiscard()
    if claimed.runner_id != runner_id:
        return RunnerMismatchDiscard()
    if operation.status == "SUCCEEDED":
        return "succeeded"
    if operation.acknowledged_at_us is not None or (
        operation.status == "OUTCOME_UNKNOWN"
        and operation.blocks_conversation != 1
    ):
        return AcknowledgedDiscard()
    if operation.status != "OUTCOME_UNKNOWN":
        return InvalidStateDiscard()
    if operation.runner_id != claimed.runner_id:
        return RunnerMismatchDiscard()
    if operation.blocks_conversation != 1:
        return InvalidStateDiscard()
    return "reconcile"


def _conversation_decision(conversation, claimed):
    if conversation is None or conversation.archived_at_us is not None:
        return InvalidStateDiscard()
    if conversation.version != claimed.expected_version:
        return StaleVersionDiscard()
    if (
        type(conversation.next_sequence) is not int
        or conversation.next_sequence < 1
        or conversation.next_sequence > SIGNED_INT64_MAX - 2
        or conversation.version > SIGNED_INT64_MAX - 1
    ):
        raise RepositoryIntegrityError()
    return None


def _reconcile(
    operation,
    conversation,
    claimed,
    output_text,
    user_message_id,
    assistant_message_id,
    reconciled_at,
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
        updated_at_us=reconciled_at,
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
        reconciled_at,
    )
    _insert_message(
        assistant_message_id,
        conversation.id,
        observed + 1,
        "assistant",
        output_text,
        operation.id,
        reconciled_at,
    )
    operation_count = Operation.objects.filter(
        id=operation.id,
        owner_id=claimed.actor_id,
        conversation_id=claimed.conversation_id,
        status="OUTCOME_UNKNOWN",
        blocks_conversation=1,
        runner_id=claimed.runner_id,
        reservation_expires_at_us__isnull=True,
        provider_started_at_us=claimed.claimed_at,
        execution_deadline_us=claimed.execution_deadline,
        lease_expires_at_us=claimed.lease_expires_at,
        finished_at_us=operation.finished_at_us,
        acknowledged_at_us__isnull=True,
        request_hash=claimed.request_hash,
        fingerprint_version=claimed.fingerprint_version,
        expected_version=claimed.expected_version,
        failure_category=operation.failure_category,
    ).update(
        status="SUCCEEDED",
        blocks_conversation=0,
        runner_id=None,
    )
    _require_one(operation_count)


def _classify_race(claimed, runner_id):
    operation = Operation.objects.filter(
        id=claimed.operation_id,
        owner_id=claimed.actor_id,
    ).first()
    decision = _decision(operation, claimed, runner_id)
    if decision == "succeeded":
        return _already_succeeded(operation)
    if decision != "reconcile":
        return decision
    conversation = Conversation.objects.filter(
        id=claimed.conversation_id,
        owner_id=claimed.actor_id,
    ).first()
    conversation_decision = _conversation_decision(conversation, claimed)
    if conversation_decision is not None:
        return conversation_decision
    return InvalidStateDiscard()


def _ambiguous(claimed):
    return CommitAmbiguous(
        operation_id=claimed.operation_id,
        runner_id=claimed.runner_id,
        claimed_at=claimed.claimed_at,
        conversation_id=claimed.conversation_id,
    )
