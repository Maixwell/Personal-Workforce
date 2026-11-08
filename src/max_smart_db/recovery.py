"""Fenced recovery for the singleton global model slot."""

from dataclasses import dataclass

from django.db import IntegrityError, OperationalError, transaction

from .creation import _read_clock_once
from .models import GlobalModelSlot, Operation
from .repository import (
    CancelledExpiredQueued,
    CommitAmbiguous,
    DatabaseBusy,
    InconsistentSlot,
    LiveQueued,
    LiveRunning,
    MarkedOutcomeUnknown,
    OwnershipMismatch,
    RepositoryIntegrityError,
    SlotFree,
)


class _ConditionalRace(Exception):
    pass


@dataclass(frozen=True)
class _RecoverQueued:
    operation: Operation
    slot: GlobalModelSlot


@dataclass(frozen=True)
class _RecoverRunning:
    operation: Operation
    slot: GlobalModelSlot


@dataclass
class _Attempt:
    mutation_started: bool = False
    operation_id: object = None


def recover_slot(clock):
    """Classify or recover an expired singleton slot in one writer transaction."""
    body_complete = False
    attempt = None
    now = None
    try:
        with transaction.atomic(durable=True):
            now = _read_clock_once(clock)
            result, attempt = recover_slot_locked(now)
            body_complete = True
    except _ConditionalRace:
        return _classify_after_race(now)
    except OperationalError as error:
        if body_complete:
            return CommitAmbiguous(operation_id=attempt.operation_id)
        if (
            (attempt is None or not attempt.mutation_started)
            and _is_lock_failure(error)
        ):
            return DatabaseBusy()
        raise RepositoryIntegrityError() from None
    except IntegrityError:
        if body_complete:
            return CommitAmbiguous(operation_id=attempt.operation_id)
        raise RepositoryIntegrityError() from None
    except Exception:
        if body_complete:
            return CommitAmbiguous(operation_id=attempt.operation_id)
        raise
    return result


def recover_slot_locked(now):
    """Recover an eligible slot inside the caller's writer transaction."""
    attempt = _Attempt()
    classified = _inspect_slot(now)
    if isinstance(classified, _RecoverQueued):
        attempt.operation_id = classified.operation.id
        _recover_queued(classified, now, attempt)
        return CancelledExpiredQueued(), attempt
    if isinstance(classified, _RecoverRunning):
        attempt.operation_id = classified.operation.id
        _recover_running(classified, now, attempt)
        return MarkedOutcomeUnknown(), attempt
    return classified, attempt


def _inspect_slot(now):
    slot = GlobalModelSlot.objects.filter(pk=1).first()
    if slot is None:
        return InconsistentSlot()
    if slot.operation_id is None:
        if any(
            value is not None
            for value in (
                slot.runner_id,
                slot.reservation_expires_at_us,
                slot.lease_expires_at_us,
                slot.acquired_at_us,
            )
        ):
            return InconsistentSlot()
        return SlotFree()

    operation = Operation.objects.filter(pk=slot.operation_id).first()
    if operation is None:
        return InconsistentSlot()
    if operation.runner_id != slot.runner_id:
        return OwnershipMismatch()

    if operation.status == "QUEUED":
        if not _queued_corresponds(operation, slot):
            return InconsistentSlot()
        if operation.reservation_expires_at_us > now:
            return LiveQueued()
        return _RecoverQueued(operation, slot)

    if operation.status == "RUNNING":
        if not _running_corresponds(operation, slot):
            return InconsistentSlot()
        if (
            operation.execution_deadline_us > now
            or operation.lease_expires_at_us > now
        ):
            return LiveRunning()
        return _RecoverRunning(operation, slot)

    return InconsistentSlot()


def _queued_corresponds(operation, slot):
    return (
        operation.blocks_conversation == 1
        and operation.runner_id is not None
        and operation.reservation_expires_at_us is not None
        and operation.reservation_expires_at_us == slot.reservation_expires_at_us
        and operation.provider_started_at_us is None
        and operation.execution_deadline_us is None
        and operation.lease_expires_at_us is None
        and operation.finished_at_us is None
        and operation.acknowledged_at_us is None
        and slot.reservation_expires_at_us is not None
        and slot.lease_expires_at_us is None
        and slot.acquired_at_us is not None
    )


def _running_corresponds(operation, slot):
    return (
        operation.blocks_conversation == 1
        and operation.runner_id is not None
        and operation.reservation_expires_at_us is None
        and operation.provider_started_at_us is not None
        and operation.execution_deadline_us is not None
        and operation.lease_expires_at_us is not None
        and operation.lease_expires_at_us == slot.lease_expires_at_us
        and operation.finished_at_us is None
        and operation.acknowledged_at_us is None
        and slot.reservation_expires_at_us is None
        and slot.lease_expires_at_us is not None
        and slot.acquired_at_us is not None
    )


def _recover_queued(candidate, now, attempt):
    operation = candidate.operation
    slot = candidate.slot
    rowcount = Operation.objects.filter(
        id=operation.id,
        owner_id=operation.owner_id,
        conversation_id=operation.conversation_id,
        status="QUEUED",
        blocks_conversation=1,
        runner_id=operation.runner_id,
        reservation_expires_at_us=operation.reservation_expires_at_us,
        reservation_expires_at_us__lte=now,
        provider_started_at_us__isnull=True,
        execution_deadline_us__isnull=True,
        lease_expires_at_us__isnull=True,
        finished_at_us__isnull=True,
        acknowledged_at_us__isnull=True,
    ).update(
        status="CANCELLED",
        blocks_conversation=0,
        runner_id=None,
        reservation_expires_at_us=None,
        finished_at_us=now,
    )
    attempt.mutation_started = rowcount != 0
    _require_one(rowcount)
    _release_slot(slot, operation)


def _recover_running(candidate, now, attempt):
    operation = candidate.operation
    slot = candidate.slot
    rowcount = Operation.objects.filter(
        id=operation.id,
        owner_id=operation.owner_id,
        conversation_id=operation.conversation_id,
        status="RUNNING",
        blocks_conversation=1,
        runner_id=operation.runner_id,
        reservation_expires_at_us__isnull=True,
        provider_started_at_us=operation.provider_started_at_us,
        execution_deadline_us=operation.execution_deadline_us,
        execution_deadline_us__lte=now,
        lease_expires_at_us=operation.lease_expires_at_us,
        lease_expires_at_us__lte=now,
        finished_at_us__isnull=True,
        acknowledged_at_us__isnull=True,
    ).update(
        status="OUTCOME_UNKNOWN",
        finished_at_us=now,
    )
    attempt.mutation_started = rowcount != 0
    _require_one(rowcount)
    _release_slot(slot, operation)


def _release_slot(slot, operation):
    _require_one(
        GlobalModelSlot.objects.filter(
            id=1,
            operation_id=operation.id,
            runner_id=operation.runner_id,
            reservation_expires_at_us=slot.reservation_expires_at_us,
            lease_expires_at_us=slot.lease_expires_at_us,
            acquired_at_us=slot.acquired_at_us,
        ).update(
            operation_id=None,
            runner_id=None,
            reservation_expires_at_us=None,
            lease_expires_at_us=None,
            acquired_at_us=None,
        )
    )


def _require_one(rowcount):
    if rowcount == 1:
        return
    if rowcount == 0:
        raise _ConditionalRace()
    raise RepositoryIntegrityError()


def _classify_after_race(now):
    classified = _inspect_slot(now)
    if isinstance(classified, (_RecoverQueued, _RecoverRunning)):
        return InconsistentSlot()
    return classified


def _is_lock_failure(error):
    text = str(error).lower()
    return "database is locked" in text or "database is busy" in text
