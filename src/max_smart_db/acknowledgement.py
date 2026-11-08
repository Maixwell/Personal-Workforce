"""Owner acknowledgement of one unacknowledged unknown outcome."""

from uuid import UUID

from django.db import IntegrityError, OperationalError, transaction

from .application_validation import require_explicit_positive_integer_id
from .creation import _read_clock_once
from .models import Operation
from .prepare import PrepareInputError
from .reads import _operation_view
from .recovery import _ConditionalRace, _is_lock_failure, _require_one
from .repository import (
    Acknowledged,
    AlreadyAcknowledged,
    CommitAmbiguous,
    DatabaseBusy,
    NotFound,
    NotUnknown,
    OperationStatus,
    RepositoryIntegrityError,
)


def acknowledge_unknown(repository, actor_id, operation_id):
    """Set the acknowledgement time and clear the conversation block."""
    require_explicit_positive_integer_id(actor_id)
    if type(operation_id) is not UUID:
        raise PrepareInputError("operation_id_type")
    body_complete = False
    started = _Started()
    try:
        with transaction.atomic(durable=True):
            acknowledged_at = _read_clock_once(repository.clock)
            operation = Operation.objects.filter(
                id=operation_id,
                owner_id=actor_id,
            ).first()
            decision = _decision(operation)
            if decision == "acknowledge":
                _acknowledge(operation, acknowledged_at, started)
                operation.refresh_from_db()
                result = Acknowledged(_operation_view(operation))
            else:
                result = decision
            body_complete = True
    except _ConditionalRace:
        return _classify_race(actor_id, operation_id)
    except OperationalError as error:
        if body_complete:
            return _ambiguous(operation_id)
        if not started.mutation and _is_lock_failure(error):
            return DatabaseBusy()
        raise RepositoryIntegrityError() from None
    except IntegrityError:
        if body_complete:
            return _ambiguous(operation_id)
        raise RepositoryIntegrityError() from None
    except Exception:
        if body_complete:
            return _ambiguous(operation_id)
        raise
    return result


class _Started:
    mutation = False


def _decision(operation):
    if operation is None:
        return NotFound()
    if operation.status != "OUTCOME_UNKNOWN":
        return NotUnknown(OperationStatus(operation.status))
    if operation.acknowledged_at_us is not None:
        if operation.blocks_conversation != 0:
            raise RepositoryIntegrityError()
        return AlreadyAcknowledged(_operation_view(operation))
    if operation.blocks_conversation != 1:
        raise RepositoryIntegrityError()
    return "acknowledge"


def _acknowledge(operation, acknowledged_at, started):
    count = Operation.objects.filter(
        id=operation.id,
        owner_id=operation.owner_id,
        status="OUTCOME_UNKNOWN",
        acknowledged_at_us__isnull=True,
        blocks_conversation=1,
    ).update(
        acknowledged_at_us=acknowledged_at,
        blocks_conversation=0,
    )
    started.mutation = count != 0
    _require_one(count)


def _classify_race(actor_id, operation_id):
    operation = Operation.objects.filter(
        id=operation_id,
        owner_id=actor_id,
    ).first()
    decision = _decision(operation)
    if decision == "acknowledge":
        raise RepositoryIntegrityError()
    return decision


def _ambiguous(operation_id):
    return CommitAmbiguous(operation_id=operation_id)
