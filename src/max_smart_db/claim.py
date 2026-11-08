"""Fenced QUEUED-to-RUNNING claim transaction."""

from uuid import UUID

from django.db import IntegrityError, OperationalError, transaction

from .application_validation import (
    SIGNED_INT64_MAX,
    require_explicit_positive_integer_id,
)
from .config import EXECUTION_DEADLINE_US, FIXED_LEASE_US
from .creation import _read_clock_once
from .models import GlobalModelSlot, Operation
from .prepare import PrepareInputError, _validate_runner
from .reads import _operation_view
from .recovery import (
    _Attempt,
    _ConditionalRace,
    _RecoverQueued,
    _is_lock_failure,
    _queued_corresponds,
    _recover_queued,
)
from .repository import (
    Claimed,
    ClaimedOperation,
    CommitAmbiguous,
    DatabaseBusy,
    ExpiredResubmit,
    NotFound,
    NotQueued,
    OwnershipLost,
    RepositoryIntegrityError,
    SlotMismatch,
)


def claim_running(repository, actor_id, operation_id):
    """Claim one live reservation without crossing the provider boundary."""
    require_explicit_positive_integer_id(actor_id)
    if type(operation_id) is not UUID:
        raise PrepareInputError("operation_id_type")
    _validate_runner(repository.runner_id)
    body_complete = False
    started = _Started()
    claimed_at = None
    try:
        with transaction.atomic(durable=True):
            claimed_at = _read_clock_once(repository.clock)
            operation = Operation.objects.filter(
                id=operation_id,
                owner_id=actor_id,
            ).first()
            if operation is None:
                result = NotFound()
            elif operation.status == "CANCELLED":
                result = ExpiredResubmit()
            elif operation.status != "QUEUED":
                result = NotQueued(_operation_view(operation))
            else:
                slot = GlobalModelSlot.objects.filter(pk=1).first()
                decision = _decision(
                    operation,
                    slot,
                    repository.runner_id,
                    claimed_at,
                )
                if decision == "claim":
                    execution_deadline = _checked_add(
                        claimed_at,
                        EXECUTION_DEADLINE_US,
                    )
                    lease_expires_at = _checked_add(
                        claimed_at,
                        FIXED_LEASE_US,
                    )
                    _claim(
                        operation,
                        slot,
                        claimed_at,
                        execution_deadline,
                        lease_expires_at,
                        started,
                    )
                    result = Claimed(
                        ClaimedOperation(
                            operation_id=operation.id,
                            actor_id=operation.owner_id,
                            conversation_id=operation.conversation_id,
                            request_hash=operation.request_hash,
                            fingerprint_version=operation.fingerprint_version,
                            expected_version=operation.expected_version,
                            runner_id=operation.runner_id,
                            claimed_at=claimed_at,
                            execution_deadline=execution_deadline,
                            lease_expires_at=lease_expires_at,
                        )
                    )
                elif decision == "expire":
                    attempt = _Attempt()
                    try:
                        _recover_queued(
                            _RecoverQueued(operation, slot),
                            claimed_at,
                            attempt,
                        )
                    finally:
                        started.mutation = attempt.mutation_started
                    result = ExpiredResubmit()
                else:
                    result = decision
            body_complete = True
    except _ConditionalRace:
        return _classify_race(
            actor_id,
            operation_id,
            repository.runner_id,
            claimed_at,
        )
    except OperationalError as error:
        if body_complete:
            return CommitAmbiguous(
                operation_id=operation_id,
                runner_id=repository.runner_id,
                claimed_at=claimed_at,
            )
        if not started.mutation and _is_lock_failure(error):
            return DatabaseBusy()
        raise RepositoryIntegrityError() from None
    except IntegrityError:
        if body_complete:
            return CommitAmbiguous(
                operation_id=operation_id,
                runner_id=repository.runner_id,
                claimed_at=claimed_at,
            )
        raise RepositoryIntegrityError() from None
    except Exception:
        if body_complete:
            return CommitAmbiguous(
                operation_id=operation_id,
                runner_id=repository.runner_id,
                claimed_at=claimed_at,
            )
        raise
    return result


class _Started:
    mutation = False


def _decision(operation, slot, runner_id, now):
    if operation.runner_id != runner_id:
        return OwnershipLost()
    if slot is None or slot.operation_id != operation.id:
        return SlotMismatch()
    if slot.runner_id != runner_id or not _queued_corresponds(operation, slot):
        return OwnershipLost()
    if operation.reservation_expires_at_us <= now:
        return "expire"
    return "claim"


def _checked_add(now, duration):
    if now > SIGNED_INT64_MAX - duration:
        raise RepositoryIntegrityError()
    return now + duration


def _claim(
    operation,
    slot,
    claimed_at,
    execution_deadline,
    lease_expires_at,
    started,
):
    operation_count = Operation.objects.filter(
        id=operation.id,
        owner_id=operation.owner_id,
        conversation_id=operation.conversation_id,
        status="QUEUED",
        blocks_conversation=1,
        runner_id=operation.runner_id,
        reservation_expires_at_us=operation.reservation_expires_at_us,
        reservation_expires_at_us__gt=claimed_at,
        provider_started_at_us__isnull=True,
        execution_deadline_us__isnull=True,
        lease_expires_at_us__isnull=True,
        finished_at_us__isnull=True,
        acknowledged_at_us__isnull=True,
    ).update(
        status="RUNNING",
        reservation_expires_at_us=None,
        provider_started_at_us=claimed_at,
        execution_deadline_us=execution_deadline,
        lease_expires_at_us=lease_expires_at,
    )
    started.mutation = operation_count != 0
    _require_one(operation_count)
    slot_count = GlobalModelSlot.objects.filter(
        id=1,
        operation_id=operation.id,
        runner_id=operation.runner_id,
        reservation_expires_at_us=slot.reservation_expires_at_us,
        lease_expires_at_us__isnull=True,
        acquired_at_us=slot.acquired_at_us,
    ).update(
        reservation_expires_at_us=None,
        lease_expires_at_us=lease_expires_at,
    )
    _require_one(slot_count)


def _require_one(rowcount):
    if rowcount == 1:
        return
    if rowcount == 0:
        raise _ConditionalRace()
    raise RepositoryIntegrityError()


def _classify_race(actor_id, operation_id, runner_id, now):
    operation = Operation.objects.filter(
        id=operation_id,
        owner_id=actor_id,
    ).first()
    if operation is None:
        return NotFound()
    if operation.status == "CANCELLED":
        return ExpiredResubmit()
    if operation.status != "QUEUED":
        return NotQueued(_operation_view(operation))
    slot = GlobalModelSlot.objects.filter(pk=1).first()
    decision = _decision(operation, slot, runner_id, now)
    if decision == "expire":
        return ExpiredResubmit()
    if decision == "claim":
        return OwnershipLost()
    return decision
