"""Create one explicit retry without replaying the source operation."""

import uuid

from django.db import IntegrityError, OperationalError, transaction

from .application_validation import (
    SIGNED_INT64_MAX,
    require_explicit_positive_integer_id,
)
from .config import IDEMPOTENCY_EXPIRATION_US, QUEUED_RESERVATION_US
from .creation import _read_clock_once
from .fingerprint import FINGERPRINT_VERSION, fingerprint_v1
from .idempotency import idempotency_key_digest
from .models import Conversation, GlobalModelSlot, IdempotencyKey, Operation
from .prepare import (
    PrepareInputError,
    _checked_add,
    _require_one,
    _validate_runner,
)
from .reads import _operation_view
from .recovery import (
    _ConditionalRace,
    _is_lock_failure,
    recover_slot_locked,
)
from .repository import (
    CommitAmbiguous,
    ConversationBlocked,
    DatabaseBusy,
    InconsistentSlot,
    KeyBindingMismatch,
    LiveQueued,
    LiveRunning,
    ModelBusy,
    NotFound,
    OperationStatus,
    OwnershipMismatch,
    QueuedReservation,
    RepositoryIntegrityError,
    RetryCommand,
    RetryExisting,
    RetryPrepared,
    RetrySourceNotEligible,
    SlotFree,
    StaleVersion,
)


class _RetryDatabaseError(Exception):
    pass


class _SourceRejected(Exception):
    pass


def create_retry(repository, command):
    """Insert one new QUEUED retry, or return the existing key binding."""
    _validate_command(command)
    _validate_runner(repository.runner_id)
    key_digest = idempotency_key_digest(command.idempotency_key)
    operation_id = uuid.uuid4()
    body_complete = False
    delayed_integrity = False
    mutation_started = False
    reconciliation_id = operation_id
    try:
        with transaction.atomic(durable=True):
            now = _read_clock_once(repository.clock)
            source = Operation.objects.filter(
                id=command.source_operation_id,
                owner_id=command.actor_id,
            ).first()
            if source is None:
                result = NotFound()
            else:
                existing = IdempotencyKey.objects.select_related(
                    "operation",
                ).filter(
                    owner_id=command.actor_id,
                    key_digest_version=1,
                    key_digest=key_digest,
                ).first()
                if existing is not None:
                    reconciliation_id = existing.operation_id
                    conversation = _owned_conversation(source, command.actor_id)
                    request_hash = _request_hash(source, command, conversation)
                    if _retry_binding_matches(
                        existing,
                        source,
                        command,
                        request_hash,
                    ):
                        result = RetryExisting(
                            operation=_operation_view(existing.operation),
                            retry_of=source.id,
                            duplicate_charge_warning=_duplicate_charge(source),
                        )
                    else:
                        result = KeyBindingMismatch()
                elif (
                    not _source_eligible(source)
                    or Operation.objects.filter(retry_of_id=source.id).exists()
                ):
                    result = RetrySourceNotEligible()
                else:
                    conversation = _owned_conversation(source, command.actor_id)
                    request_hash = _request_hash(source, command, conversation)
                    if conversation.version != command.expected_version:
                        result = StaleVersion(actual=conversation.version)
                    else:
                        (
                            result,
                            delayed_integrity,
                            mutation_started,
                            reconciliation_id,
                        ) = _reserve_retry(
                            repository,
                            source,
                            command,
                            conversation,
                            operation_id,
                            request_hash,
                            key_digest,
                            now,
                        )
            body_complete = True
    except _SourceRejected:
        return RetrySourceNotEligible()
    except _ConditionalRace:
        raise RepositoryIntegrityError() from None
    except _RetryDatabaseError:
        raise RepositoryIntegrityError() from None
    except OperationalError as error:
        if body_complete:
            return CommitAmbiguous(
                operation_id=reconciliation_id,
                runner_id=repository.runner_id,
            )
        if not mutation_started and _is_lock_failure(error):
            return DatabaseBusy()
        raise RepositoryIntegrityError() from None
    except IntegrityError:
        if body_complete:
            return CommitAmbiguous(
                operation_id=reconciliation_id,
                runner_id=repository.runner_id,
            )
        raise RepositoryIntegrityError() from None
    except Exception:
        if body_complete:
            return CommitAmbiguous(
                operation_id=reconciliation_id,
                runner_id=repository.runner_id,
            )
        raise

    if delayed_integrity:
        raise RepositoryIntegrityError()
    return result


def _owned_conversation(source, actor_id):
    conversation = Conversation.objects.select_related("role").filter(
        id=source.conversation_id,
        owner_id=actor_id,
    ).first()
    if conversation is None or conversation.role.active != 1:
        raise RepositoryIntegrityError()
    return conversation


def _request_hash(source, command, conversation):
    return fingerprint_v1(
        command.actor_id,
        source.conversation_id.hex,
        "message",
        command.expected_version,
        conversation.role_id,
        source.provider_id,
        source.model_id,
        source.request_content,
    )


def _reserve_retry(
    repository,
    source,
    command,
    conversation,
    operation_id,
    request_hash,
    key_digest,
    now,
):
    reservation_expires = _checked_add(
        now,
        QUEUED_RESERVATION_US,
        "reservation_timestamp_overflow",
    )
    key_expires = _checked_add(
        now,
        IDEMPOTENCY_EXPIRATION_US,
        "idempotency_timestamp_overflow",
    )
    recovery_result, recovery_attempt = recover_slot_locked(now)
    mutation_started = recovery_attempt.mutation_started
    reconciliation_id = operation_id
    if recovery_attempt.operation_id is not None:
        reconciliation_id = recovery_attempt.operation_id
    if isinstance(recovery_result, (InconsistentSlot, OwnershipMismatch)):
        raise RepositoryIntegrityError()
    blocker = Operation.objects.filter(
        conversation_id=conversation.id,
        blocks_conversation=1,
    ).first()
    if blocker is not None:
        return (
            ConversationBlocked(
                operation_id=blocker.id,
                status=OperationStatus(blocker.status),
            ),
            False,
            mutation_started,
            reconciliation_id,
        )
    if isinstance(recovery_result, (LiveQueued, LiveRunning)):
        return ModelBusy(), False, mutation_started, reconciliation_id
    if not isinstance(recovery_result, SlotFree):
        if GlobalModelSlot.objects.filter(
            id=1,
            operation_id__isnull=True,
            runner_id__isnull=True,
            reservation_expires_at_us__isnull=True,
            lease_expires_at_us__isnull=True,
            acquired_at_us__isnull=True,
        ).count() != 1:
            raise RepositoryIntegrityError()
    result, delayed_integrity = _insert_retry(
        source,
        command,
        operation_id,
        request_hash,
        key_digest,
        now,
        reservation_expires,
        key_expires,
        repository.runner_id,
    )
    return (
        result,
        delayed_integrity,
        mutation_started or not delayed_integrity,
        operation_id,
    )


def _source_eligible(source):
    if source.blocks_conversation != 0:
        return False
    if source.status in ("FAILED", "CANCELLED"):
        return True
    return (
        source.status == "OUTCOME_UNKNOWN"
        and source.acknowledged_at_us is not None
    )


def _duplicate_charge(source):
    return source.status == "OUTCOME_UNKNOWN"


def _retry_binding_matches(existing, source, command, request_hash):
    operation = existing.operation
    return (
        operation.retry_of_id == source.id
        and existing.conversation_id == source.conversation_id
        and existing.operation_type == "message"
        and existing.expected_version == command.expected_version
        and existing.fingerprint_version == FINGERPRINT_VERSION
        and existing.request_hash == request_hash
        and operation.owner_id == command.actor_id
        and operation.conversation_id == source.conversation_id
        and operation.operation_type == "message"
        and operation.expected_version == command.expected_version
        and operation.fingerprint_version == FINGERPRINT_VERSION
        and operation.request_hash == request_hash
        and operation.provider_id == source.provider_id
        and operation.model_id == source.model_id
        and operation.request_content == source.request_content
    )


def _insert_retry(
    source,
    command,
    operation_id,
    request_hash,
    key_digest,
    now,
    reservation_expires,
    key_expires,
    runner_id,
):
    try:
        with transaction.atomic():
            key_id = _next_key_id()
            Operation.objects.create(
                id=operation_id,
                conversation_id=source.conversation_id,
                owner_id=command.actor_id,
                operation_type="message",
                provider_id=source.provider_id,
                model_id=source.model_id,
                status="QUEUED",
                blocks_conversation=1,
                expected_version=command.expected_version,
                request_content=source.request_content,
                fingerprint_version=FINGERPRINT_VERSION,
                request_hash=request_hash,
                retry_of_id=source.id,
                runner_id=runner_id,
                reservation_expires_at_us=reservation_expires,
                provider_started_at_us=None,
                execution_deadline_us=None,
                lease_expires_at_us=None,
                created_at_us=now,
                finished_at_us=None,
                acknowledged_at_us=None,
                failure_category=None,
            )
            IdempotencyKey.objects.create(
                id=key_id,
                owner_id=command.actor_id,
                conversation_id=source.conversation_id,
                operation_type="message",
                expected_version=command.expected_version,
                key_digest_version=1,
                key_digest=key_digest,
                fingerprint_version=FINGERPRINT_VERSION,
                request_hash=request_hash,
                operation_id=operation_id,
                expires_at_us=key_expires,
            )
            rowcount = GlobalModelSlot.objects.filter(
                id=1,
                operation_id__isnull=True,
                runner_id__isnull=True,
                reservation_expires_at_us__isnull=True,
                lease_expires_at_us__isnull=True,
                acquired_at_us__isnull=True,
            ).update(
                operation_id=operation_id,
                runner_id=runner_id,
                reservation_expires_at_us=reservation_expires,
                lease_expires_at_us=None,
                acquired_at_us=now,
            )
            _require_one(rowcount)
    except IntegrityError as error:
        if _retry_rejected(error):
            raise _SourceRejected() from None
        return None, True
    except RepositoryIntegrityError:
        return None, True
    except OperationalError as error:
        raise _RetryDatabaseError() from error
    return (
        RetryPrepared(
            reservation=QueuedReservation(
                operation_id=operation_id,
                actor_id=command.actor_id,
                conversation_id=source.conversation_id,
                request_hash=request_hash,
                fingerprint_version=FINGERPRINT_VERSION,
                expected_version=command.expected_version,
                runner_id=runner_id,
                reservation_expires_at=reservation_expires,
            ),
            retry_of=source.id,
            duplicate_charge_warning=_duplicate_charge(source),
        ),
        False,
    )


def _retry_rejected(error):
    text = str(error).lower()
    return (
        "retry source is not eligible" in text
        or "retry_of" in text
    )


def _next_key_id():
    from .prepare import _next_idempotency_id

    return _next_idempotency_id()


def _validate_command(command):
    if type(command) is not RetryCommand:
        raise PrepareInputError("command_type")
    require_explicit_positive_integer_id(command.actor_id)
    if type(command.source_operation_id) is not uuid.UUID:
        raise PrepareInputError("source_operation_id_type")
    if type(command.expected_version) is not int:
        raise PrepareInputError("expected_version_type")
    if (
        command.expected_version < 0
        or command.expected_version > SIGNED_INT64_MAX
    ):
        raise PrepareInputError("expected_version_range")
