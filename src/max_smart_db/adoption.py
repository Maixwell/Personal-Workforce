"""Fenced adoption of one unexpired QUEUED reservation."""

from uuid import UUID

from django.db import IntegrityError, OperationalError, transaction

from .application_validation import require_explicit_positive_integer_id
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
    Adopted,
    AlreadyOwned,
    CommitAmbiguous,
    DatabaseBusy,
    ExpiredResubmit,
    NotFound,
    NotQueued,
    OwnershipLost,
    QueuedReservation,
    RepositoryIntegrityError,
    SlotMismatch,
)


def adopt_queued(repository, actor_id, operation_id):
    """Adopt, recognize, or expire one QUEUED reservation."""
    require_explicit_positive_integer_id(actor_id)
    if type(operation_id) is not UUID:
        raise PrepareInputError("operation_id_type")
    _validate_runner(repository.runner_id)
    body_complete = False
    started = _Started()
    now = None
    try:
        with transaction.atomic(durable=True):
            now = _read_clock_once(repository.clock)
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
                    now,
                    repository.runner_id,
                )
                if decision == "adopt":
                    _transfer_runner(
                        operation,
                        slot,
                        repository.runner_id,
                        now,
                        started,
                    )
                    result = Adopted(_reservation(
                        operation,
                        repository.runner_id,
                    ))
                elif decision == "owned":
                    result = AlreadyOwned(_reservation(
                        operation,
                        operation.runner_id,
                    ))
                elif decision == "expire":
                    attempt = _Attempt()
                    try:
                        _recover_queued(
                            _RecoverQueued(operation, slot),
                            now,
                            attempt,
                        )
                    finally:
                        started.mutation = attempt.mutation_started
                    result = ExpiredResubmit()
                else:
                    result = decision
            body_complete = True
    except _ConditionalRace:
        return _classify_race(actor_id, operation_id)
    except OperationalError as error:
        if body_complete:
            return CommitAmbiguous(
                operation_id=operation_id,
                runner_id=repository.runner_id,
            )
        if not started.mutation and _is_lock_failure(error):
            return DatabaseBusy()
        raise RepositoryIntegrityError() from None
    except IntegrityError:
        if body_complete:
            return CommitAmbiguous(
                operation_id=operation_id,
                runner_id=repository.runner_id,
            )
        raise RepositoryIntegrityError() from None
    except Exception:
        if body_complete:
            return CommitAmbiguous(
                operation_id=operation_id,
                runner_id=repository.runner_id,
            )
        raise
    return result


def _decision(operation, slot, now, new_runner):
    if not _slot_matches(operation, slot):
        if slot is None or slot.operation_id != operation.id:
            return SlotMismatch()
        return OwnershipLost()
    if operation.reservation_expires_at_us > now:
        if operation.runner_id == new_runner:
            return "owned"
        return "adopt"
    return "expire"


def _slot_matches(operation, slot):
    return (
        slot is not None
        and slot.operation_id == operation.id
        and slot.runner_id == operation.runner_id
        and _queued_corresponds(operation, slot)
    )


class _Started:
    mutation = False


def _transfer_runner(operation, slot, new_runner, now, started):
    operation_count = Operation.objects.filter(
        id=operation.id,
        owner_id=operation.owner_id,
        conversation_id=operation.conversation_id,
        status="QUEUED",
        blocks_conversation=1,
        runner_id=operation.runner_id,
        reservation_expires_at_us=operation.reservation_expires_at_us,
        reservation_expires_at_us__gt=now,
        provider_started_at_us__isnull=True,
        execution_deadline_us__isnull=True,
        lease_expires_at_us__isnull=True,
        finished_at_us__isnull=True,
        acknowledged_at_us__isnull=True,
        request_hash=operation.request_hash,
        expected_version=operation.expected_version,
    ).update(runner_id=new_runner)
    started.mutation = operation_count != 0
    _require_one(operation_count)
    slot_count = GlobalModelSlot.objects.filter(
        id=1,
        operation_id=operation.id,
        runner_id=operation.runner_id,
        reservation_expires_at_us=slot.reservation_expires_at_us,
        lease_expires_at_us__isnull=True,
        acquired_at_us=slot.acquired_at_us,
    ).update(runner_id=new_runner)
    _require_one(slot_count)


def _reservation(operation, runner_id):
    return QueuedReservation(
        operation_id=operation.id,
        actor_id=operation.owner_id,
        conversation_id=operation.conversation_id,
        request_hash=operation.request_hash,
        fingerprint_version=operation.fingerprint_version,
        expected_version=operation.expected_version,
        runner_id=runner_id,
        reservation_expires_at=operation.reservation_expires_at_us,
    )


def _require_one(rowcount):
    if rowcount == 1:
        return
    if rowcount == 0:
        raise _ConditionalRace()
    raise RepositoryIntegrityError()


def _classify_race(actor_id, operation_id):
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
    return OwnershipLost()
