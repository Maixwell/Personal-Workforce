"""The ordered PREPARE transaction for one QUEUED operation."""

import re
import uuid

from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Max

from .application_validation import (
    SIGNED_INT64_MAX,
    require_explicit_positive_integer_id,
)
from .config import IDEMPOTENCY_EXPIRATION_US, QUEUED_RESERVATION_US
from .creation import CreationInputError, _read_clock_once
from .fingerprint import FINGERPRINT_VERSION, fingerprint_v1
from .idempotency import idempotency_key_digest
from .models import (
    Conversation,
    GlobalModelSlot,
    IdempotencyKey,
    Operation,
)
from .reads import _operation_view
from .recovery import (
    _ConditionalRace,
    _is_lock_failure,
    _recovery_gate,
    recover_slot_locked,
)
from .repository import (
    CommitAmbiguous,
    ConversationBlocked,
    DatabaseBusy,
    Existing,
    KeyBindingMismatch,
    LiveQueued,
    LiveRunning,
    LocalProviderActive,
    ModelBusy,
    NotFound,
    OperationStatus,
    Prepared,
    PrepareCommand,
    ProviderFenceUnproven,
    QueuedReservation,
    RepositoryIntegrityError,
    SlotFree,
    StaleVersion,
    InconsistentSlot,
    OwnershipMismatch,
)
from provider_fence import FenceHeld, release_fence


_RUNNER = re.compile(r"^[A-Za-z0-9_-]{22}$")
_RUNNER_LAST = frozenset("AQgw")


class PrepareInputError(ValueError):
    """A PREPARE boundary value is invalid."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _PrepareDatabaseError(Exception):
    pass


def prepare(repository, command):
    """Run PREPARE while the global provider fence is held."""
    _validate_command(command)
    _validate_runner(repository.runner_id)
    gate = _recovery_gate(repository)
    if type(gate) is ProviderFenceUnproven:
        return gate
    if type(gate) is LocalProviderActive:
        return ModelBusy()
    if type(gate) is not FenceHeld:
        return ProviderFenceUnproven("fence_unproven")
    try:
        return _prepare_in_fence(repository, command, gate)
    finally:
        release_fence(gate)


def _prepare_in_fence(repository, command, permit):
    """Run PREPARE in the authoritative order."""
    key_digest = idempotency_key_digest(command.idempotency_key)
    operation_id = uuid.uuid4()
    body_complete = False
    delayed_integrity = False
    mutation_started = False
    reconciliation_id = operation_id
    try:
        with transaction.atomic(durable=True):
            now = _read_clock_once(repository.clock)

            conversation = Conversation.objects.select_related("role").filter(
                id=command.conversation_id,
                owner_id=command.actor_id,
            ).first()
            if conversation is None:
                result = NotFound()
            elif conversation.role.active != 1:
                raise RepositoryIntegrityError()
            else:
                request_hash = fingerprint_v1(
                    command.actor_id,
                    command.conversation_id.hex,
                    "message",
                    command.expected_version,
                    conversation.role_id,
                    command.provider_id,
                    command.model_id,
                    command.request_content,
                )
                existing = IdempotencyKey.objects.select_related(
                    "operation",
                ).filter(
                    owner_id=command.actor_id,
                    key_digest_version=1,
                    key_digest=key_digest,
                ).first()
                if existing is not None:
                    reconciliation_id = existing.operation_id
                    if _binding_matches(
                        existing,
                        command,
                        request_hash,
                    ):
                        result = Existing(_operation_view(existing.operation))
                    else:
                        result = KeyBindingMismatch()
                elif conversation.version != command.expected_version:
                    result = StaleVersion(actual=conversation.version)
                else:
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
                    recovery_result, recovery_attempt = recover_slot_locked(
                        now,
                        permit,
                    )
                    mutation_started = recovery_attempt.mutation_started
                    if recovery_attempt.operation_id is not None:
                        reconciliation_id = recovery_attempt.operation_id
                    if isinstance(
                        recovery_result,
                        (InconsistentSlot, OwnershipMismatch),
                    ):
                        raise RepositoryIntegrityError()
                    if isinstance(recovery_result, ProviderFenceUnproven):
                        result = recovery_result
                    elif isinstance(recovery_result, LocalProviderActive):
                        result = ModelBusy()
                    else:
                        blocker = Operation.objects.filter(
                            conversation_id=conversation.id,
                            blocks_conversation=1,
                        ).first()
                        if blocker is not None:
                            result = ConversationBlocked(
                                operation_id=blocker.id,
                                status=OperationStatus(blocker.status),
                            )
                        elif isinstance(
                            recovery_result,
                            (LiveQueued, LiveRunning),
                        ):
                            result = ModelBusy()
                        elif not isinstance(recovery_result, SlotFree):
                            # Successful recovery always leaves the slot free.
                            if GlobalModelSlot.objects.filter(
                                id=1,
                                operation_id__isnull=True,
                                runner_id__isnull=True,
                                reservation_expires_at_us__isnull=True,
                                lease_expires_at_us__isnull=True,
                                acquired_at_us__isnull=True,
                            ).count() != 1:
                                raise RepositoryIntegrityError()
                            result, delayed_integrity = _insert_preparation(
                                command,
                                operation_id,
                                request_hash,
                                key_digest,
                                now,
                                reservation_expires,
                                key_expires,
                                repository.runner_id,
                            )
                            mutation_started = (
                                mutation_started or not delayed_integrity
                            )
                            reconciliation_id = operation_id
                        else:
                            result, delayed_integrity = _insert_preparation(
                                command,
                                operation_id,
                                request_hash,
                                key_digest,
                                now,
                                reservation_expires,
                                key_expires,
                                repository.runner_id,
                            )
                            mutation_started = (
                                mutation_started or not delayed_integrity
                            )
                            reconciliation_id = operation_id
            body_complete = True
    except _ConditionalRace:
        raise RepositoryIntegrityError() from None
    except _PrepareDatabaseError:
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


def _insert_preparation(
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
            key_id = _next_idempotency_id()
            Operation.objects.create(
                id=operation_id,
                conversation_id=command.conversation_id,
                owner_id=command.actor_id,
                operation_type="message",
                provider_id=command.provider_id,
                model_id=command.model_id,
                status="QUEUED",
                blocks_conversation=1,
                expected_version=command.expected_version,
                request_content=command.request_content,
                fingerprint_version=FINGERPRINT_VERSION,
                request_hash=request_hash,
                retry_of=None,
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
                conversation_id=command.conversation_id,
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
    except (IntegrityError, RepositoryIntegrityError):
        return None, True
    except OperationalError as error:
        raise _PrepareDatabaseError() from error

    return (
        Prepared(
            QueuedReservation(
                operation_id=operation_id,
                actor_id=command.actor_id,
                conversation_id=command.conversation_id,
                request_hash=request_hash,
                fingerprint_version=FINGERPRINT_VERSION,
                expected_version=command.expected_version,
                runner_id=runner_id,
                reservation_expires_at=reservation_expires,
            )
        ),
        False,
    )


def _binding_matches(existing, command, request_hash):
    operation = existing.operation
    return (
        existing.conversation_id == command.conversation_id
        and existing.operation_type == "message"
        and existing.expected_version == command.expected_version
        and existing.fingerprint_version == FINGERPRINT_VERSION
        and existing.request_hash == request_hash
        and operation.owner_id == command.actor_id
        and operation.conversation_id == command.conversation_id
        and operation.operation_type == "message"
        and operation.expected_version == command.expected_version
        and operation.fingerprint_version == FINGERPRINT_VERSION
        and operation.request_hash == request_hash
        and operation.provider_id == command.provider_id
        and operation.model_id == command.model_id
    )


def _next_idempotency_id():
    maximum = IdempotencyKey.objects.aggregate(value=Max("id"))["value"]
    if maximum is None:
        candidate = 1
    elif type(maximum) is not int or maximum < 1:
        raise RepositoryIntegrityError()
    elif maximum == SIGNED_INT64_MAX:
        raise RepositoryIntegrityError()
    else:
        candidate = maximum + 1
    require_explicit_positive_integer_id(candidate)
    return candidate


def _validate_command(command):
    if type(command) is not PrepareCommand:
        raise PrepareInputError("command_type")
    require_explicit_positive_integer_id(command.actor_id)
    if type(command.conversation_id) is not uuid.UUID:
        raise PrepareInputError("conversation_id_type")
    if type(command.expected_version) is not int:
        raise PrepareInputError("expected_version_type")
    if (
        command.expected_version < 0
        or command.expected_version > SIGNED_INT64_MAX
    ):
        raise PrepareInputError("expected_version_range")
    if type(command.request_content) is not str:
        raise PrepareInputError("request_content_type")
    try:
        command.request_content.encode("utf-8")
    except UnicodeEncodeError:
        raise PrepareInputError("request_content_unicode") from None
    if (
        not command.request_content
        or len(command.request_content) > 20_000
        or "\x00" in command.request_content
    ):
        raise PrepareInputError("request_content_invalid")
    if command.provider_id != "openai" or type(command.provider_id) is not str:
        raise PrepareInputError("provider_invalid")
    if command.model_id != "gpt-5.6-sol" or type(command.model_id) is not str:
        raise PrepareInputError("model_invalid")


def _validate_runner(value):
    if (
        type(value) is not str
        or not _RUNNER.fullmatch(value)
        or value[-1] not in _RUNNER_LAST
    ):
        raise CreationInputError("runner_invalid")


def _checked_add(value, increment, code):
    if value > SIGNED_INT64_MAX - increment:
        raise PrepareInputError(code)
    return value + increment


def _require_one(rowcount):
    if rowcount == 1:
        return
    raise RepositoryIntegrityError()
