"""Definite pre-provider failure of one QUEUED reservation."""

import re
from uuid import UUID

from django.db import IntegrityError, OperationalError, transaction

from .application_validation import (
    SIGNED_INT64_MAX,
    require_explicit_positive_integer_id,
)
from .creation import _read_clock_once
from .models import GlobalModelSlot, Operation
from .prepare import _validate_runner
from .reads import _operation_view
from .recovery import (
    _ConditionalRace,
    _is_lock_failure,
    _queued_corresponds,
    _release_slot,
)
from .repository import (
    AlreadyFailed,
    CommitAmbiguous,
    DatabaseBusy,
    Failed,
    NotQueued,
    OwnershipLost,
    PreProviderFailureCategory,
    QueuedReservation,
    RepositoryIntegrityError,
    SlotMismatch,
)


_HASH = re.compile(r"[0-9a-f]{64}")


class PreProviderInputError(ValueError):
    """The reservation or failure category cannot enter the transaction."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def fail_pre_provider(repository, reservation, category):
    """Fail one QUEUED reservation before provider execution."""
    _validate(reservation, category)
    _validate_runner(repository.runner_id)
    body_complete = False
    started = _Started()
    try:
        with transaction.atomic(durable=True):
            finished_at = _read_clock_once(repository.clock)
            operation = _owned_operation(reservation)
            slot = GlobalModelSlot.objects.filter(pk=1).first()
            decision = _decision(
                operation,
                slot,
                reservation,
                repository.runner_id,
            )
            if decision == "fail":
                _fail(operation, slot, reservation, category, finished_at, started)
                operation.refresh_from_db()
                result = Failed(_operation_view(operation))
            elif decision == "already":
                result = AlreadyFailed(_operation_view(operation))
            elif decision == "not_queued":
                result = NotQueued(_operation_view(operation))
            else:
                result = decision
            body_complete = True
    except _ConditionalRace:
        return _classify_race(reservation, repository.runner_id)
    except OperationalError as error:
        if body_complete:
            return _ambiguous(reservation)
        if not started.mutation and _is_lock_failure(error):
            return DatabaseBusy()
        raise RepositoryIntegrityError() from None
    except IntegrityError:
        if body_complete:
            return _ambiguous(reservation)
        raise RepositoryIntegrityError() from None
    except Exception:
        if body_complete:
            return _ambiguous(reservation)
        raise
    return result


class _Started:
    mutation = False


def _validate(reservation, category):
    if type(category) is not PreProviderFailureCategory:
        raise PreProviderInputError("failure_category_invalid")
    if type(reservation) is not QueuedReservation:
        raise PreProviderInputError("reservation_type")
    require_explicit_positive_integer_id(reservation.actor_id)
    if type(reservation.operation_id) is not UUID:
        raise PreProviderInputError("operation_id_type")
    if type(reservation.conversation_id) is not UUID:
        raise PreProviderInputError("conversation_id_type")
    if (
        type(reservation.request_hash) is not str
        or _HASH.fullmatch(reservation.request_hash) is None
    ):
        raise PreProviderInputError("request_hash_invalid")
    _exact_int(reservation.fingerprint_version, 1, "fingerprint_version_invalid")
    _exact_int(reservation.expected_version, 0, "expected_version_invalid")
    _exact_int(
        reservation.reservation_expires_at,
        0,
        "reservation_timestamp_invalid",
    )
    _validate_runner(reservation.runner_id)


def _exact_int(value, minimum, code):
    if type(value) is not int or value < minimum or value > SIGNED_INT64_MAX:
        raise PreProviderInputError(code)


def _owned_operation(reservation):
    return Operation.objects.filter(
        id=reservation.operation_id,
        owner_id=reservation.actor_id,
    ).first()


def _decision(operation, slot, reservation, runner_id):
    if operation is None or not _same_request(operation, reservation):
        return OwnershipLost()
    if operation.status == "FAILED":
        return "already"
    if operation.status != "QUEUED":
        return "not_queued"
    if (
        reservation.runner_id != runner_id
        or operation.runner_id != reservation.runner_id
        or operation.reservation_expires_at_us != reservation.reservation_expires_at
    ):
        return OwnershipLost()
    if not _provider_not_started(operation):
        raise RepositoryIntegrityError()
    if slot is None or slot.operation_id != operation.id:
        return SlotMismatch()
    if slot.runner_id != operation.runner_id:
        return OwnershipLost()
    if not _queued_corresponds(operation, slot):
        return SlotMismatch()
    return "fail"


def _same_request(operation, reservation):
    return (
        operation.conversation_id == reservation.conversation_id
        and operation.request_hash == reservation.request_hash
        and operation.fingerprint_version == reservation.fingerprint_version
        and operation.expected_version == reservation.expected_version
    )


def _provider_not_started(operation):
    return (
        operation.provider_started_at_us is None
        and operation.execution_deadline_us is None
        and operation.lease_expires_at_us is None
        and operation.finished_at_us is None
        and operation.acknowledged_at_us is None
        and operation.blocks_conversation == 1
    )


def _fail(operation, slot, reservation, category, finished_at, started):
    rowcount = Operation.objects.filter(
        id=operation.id,
        owner_id=operation.owner_id,
        conversation_id=operation.conversation_id,
        status="QUEUED",
        blocks_conversation=1,
        runner_id=reservation.runner_id,
        reservation_expires_at_us=reservation.reservation_expires_at,
        provider_started_at_us__isnull=True,
        execution_deadline_us__isnull=True,
        lease_expires_at_us__isnull=True,
        finished_at_us__isnull=True,
        acknowledged_at_us__isnull=True,
        failure_category__isnull=True,
        request_hash=reservation.request_hash,
        fingerprint_version=reservation.fingerprint_version,
        expected_version=reservation.expected_version,
    ).update(
        status="FAILED",
        blocks_conversation=0,
        runner_id=None,
        reservation_expires_at_us=None,
        finished_at_us=finished_at,
        failure_category=category.value,
    )
    started.mutation = rowcount != 0
    _require_one(rowcount)
    _release_slot(slot, operation)


def _require_one(rowcount):
    if rowcount == 1:
        return
    if rowcount == 0:
        raise _ConditionalRace()
    raise RepositoryIntegrityError()


def _classify_race(reservation, runner_id):
    operation = _owned_operation(reservation)
    slot = GlobalModelSlot.objects.filter(pk=1).first()
    decision = _decision(operation, slot, reservation, runner_id)
    if decision == "already":
        return AlreadyFailed(_operation_view(operation))
    if decision == "not_queued":
        return NotQueued(_operation_view(operation))
    if decision == "fail":
        return OwnershipLost()
    return decision


def _ambiguous(reservation):
    return CommitAmbiguous(
        operation_id=reservation.operation_id,
        runner_id=reservation.runner_id,
        conversation_id=reservation.conversation_id,
    )
