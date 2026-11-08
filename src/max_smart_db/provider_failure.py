"""Definite or uncertain failure of one RUNNING claim."""

import re
from uuid import UUID

from django.db.models import Q
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
    _release_slot,
    _running_corresponds,
)
from .repository import (
    AlreadyTerminal,
    ClaimedOperation,
    CommitAmbiguous,
    DatabaseBusy,
    DefiniteProviderCategory,
    DefinitelyFailed,
    InvalidState,
    OperationStatus,
    OwnershipLost,
    RepositoryIntegrityError,
    SlotMismatch,
    UncertainProviderCategory,
    UncertainRecorded,
)


_HASH = re.compile(r"[0-9a-f]{64}")
_TERMINAL = {"FAILED", "SUCCEEDED", "CANCELLED", "OUTCOME_UNKNOWN"}
_UNCERTAIN = UncertainProviderCategory.PROVIDER_OUTCOME_UNCERTAIN.value


class ProviderFailureInputError(ValueError):
    """The claim token or failure classification cannot enter the transaction."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def apply_failure(repository, claimed, failure):
    """Apply one provider failure without calling the provider."""
    definite = _validate(claimed, failure)
    _validate_runner(repository.runner_id)
    body_complete = False
    started = _Started()
    try:
        with transaction.atomic(durable=True):
            finished_at = _read_clock_once(repository.clock)
            operation = _owned_operation(claimed)
            slot = GlobalModelSlot.objects.filter(pk=1).first()
            decision = _decision(operation, slot, claimed, repository.runner_id)
            if decision == "ready" and definite:
                _fail_definite(operation, slot, claimed, failure, finished_at, started)
                operation.refresh_from_db()
                result = DefinitelyFailed(_operation_view(operation))
            elif decision == "ready":
                _record_uncertain(operation, claimed, started)
                operation.refresh_from_db()
                result = _uncertain(operation)
            elif decision == "uncertain_recorded" and definite:
                _fail_definite(operation, slot, claimed, failure, finished_at, started)
                operation.refresh_from_db()
                result = DefinitelyFailed(_operation_view(operation))
            elif decision == "uncertain_recorded":
                result = _uncertain(operation)
            elif decision == "terminal":
                result = AlreadyTerminal(_operation_view(operation))
            elif decision == "invalid":
                result = InvalidState(OperationStatus(operation.status))
            else:
                result = decision
            body_complete = True
    except _ConditionalRace:
        return _classify_race(claimed, repository.runner_id, definite)
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


def _validate(claimed, failure):
    definite = type(failure) is DefiniteProviderCategory
    if not definite and type(failure) is not UncertainProviderCategory:
        raise ProviderFailureInputError("failure_classification_invalid")
    if type(claimed) is not ClaimedOperation:
        raise ProviderFailureInputError("claimed_type")
    require_explicit_positive_integer_id(claimed.actor_id)
    if type(claimed.operation_id) is not UUID:
        raise ProviderFailureInputError("operation_id_type")
    if type(claimed.conversation_id) is not UUID:
        raise ProviderFailureInputError("conversation_id_type")
    if (
        type(claimed.request_hash) is not str
        or _HASH.fullmatch(claimed.request_hash) is None
    ):
        raise ProviderFailureInputError("request_hash_invalid")
    _exact_int(claimed.fingerprint_version, 1, "fingerprint_version_invalid")
    _exact_int(claimed.expected_version, 0, "expected_version_invalid")
    _exact_int(claimed.claimed_at, 0, "claimed_at_invalid")
    _exact_int(claimed.execution_deadline, 0, "execution_deadline_invalid")
    _exact_int(claimed.lease_expires_at, 0, "lease_expires_at_invalid")
    if claimed.execution_deadline > claimed.lease_expires_at:
        raise ProviderFailureInputError("claim_timing_invalid")
    _validate_runner(claimed.runner_id)
    return definite


def _exact_int(value, minimum, code):
    if type(value) is not int or value < minimum or value > SIGNED_INT64_MAX:
        raise ProviderFailureInputError(code)


def _owned_operation(claimed):
    return Operation.objects.filter(
        id=claimed.operation_id,
        owner_id=claimed.actor_id,
    ).first()


def _decision(operation, slot, claimed, runner_id):
    if operation is None or not _same_request(operation, claimed):
        return OwnershipLost()
    if operation.status in _TERMINAL:
        return "terminal"
    if operation.status != "RUNNING":
        return "invalid"
    if (
        claimed.runner_id != runner_id
        or operation.runner_id != claimed.runner_id
        or operation.provider_started_at_us != claimed.claimed_at
        or operation.execution_deadline_us != claimed.execution_deadline
        or operation.lease_expires_at_us != claimed.lease_expires_at
    ):
        return OwnershipLost()
    if operation.failure_category not in (None, _UNCERTAIN):
        return "invalid"
    if slot is None or slot.operation_id != operation.id:
        return SlotMismatch()
    if slot.runner_id != operation.runner_id:
        return OwnershipLost()
    if not _running_corresponds(operation, slot):
        return SlotMismatch()
    if operation.failure_category == _UNCERTAIN:
        return "uncertain_recorded"
    return "ready"


def _same_request(operation, claimed):
    return (
        operation.conversation_id == claimed.conversation_id
        and operation.request_hash == claimed.request_hash
        and operation.fingerprint_version == claimed.fingerprint_version
        and operation.expected_version == claimed.expected_version
    )


def _fail_definite(operation, slot, claimed, failure, finished_at, started):
    rowcount = _fenced(operation, claimed).filter(
        Q(failure_category__isnull=True) | Q(failure_category=_UNCERTAIN)
    ).update(
        status="FAILED",
        blocks_conversation=0,
        runner_id=None,
        reservation_expires_at_us=None,
        finished_at_us=finished_at,
        failure_category=failure.value,
    )
    started.mutation = rowcount != 0
    _require_one(rowcount)
    _release_slot(slot, operation)


def _record_uncertain(operation, claimed, started):
    rowcount = _fenced(operation, claimed).filter(
        failure_category__isnull=True
    ).update(failure_category=_UNCERTAIN)
    started.mutation = rowcount != 0
    _require_one(rowcount)


def _fenced(operation, claimed):
    return Operation.objects.filter(
        id=operation.id,
        owner_id=operation.owner_id,
        conversation_id=operation.conversation_id,
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
    )


def _uncertain(operation):
    return UncertainRecorded(
        operation=_operation_view(operation),
        recovery_at=operation.lease_expires_at_us,
    )


def _require_one(rowcount):
    if rowcount == 1:
        return
    if rowcount == 0:
        raise _ConditionalRace()
    raise RepositoryIntegrityError()


def _classify_race(claimed, runner_id, definite):
    operation = _owned_operation(claimed)
    slot = GlobalModelSlot.objects.filter(pk=1).first()
    decision = _decision(operation, slot, claimed, runner_id)
    if decision == "terminal":
        return AlreadyTerminal(_operation_view(operation))
    if decision == "invalid":
        return InvalidState(OperationStatus(operation.status))
    if decision == "uncertain_recorded" and not definite:
        return _uncertain(operation)
    if decision in {"ready", "uncertain_recorded"}:
        return OwnershipLost()
    return decision


def _ambiguous(claimed):
    return CommitAmbiguous(
        operation_id=claimed.operation_id,
        runner_id=claimed.runner_id,
        claimed_at=claimed.claimed_at,
        conversation_id=claimed.conversation_id,
    )
