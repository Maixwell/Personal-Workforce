"""Orchestrate one turn through a durable claim and its outcome."""

from dataclasses import dataclass
from uuid import UUID

from django.db import connection

from max_smart_db.config import EXECUTION_DEADLINE_US, FIXED_LEASE_US
from max_smart_db.application_validation import SIGNED_INT64_MAX
from max_smart_db.fingerprint import (
    FINGERPRINT_VERSION,
    FingerprintValidationError,
    fingerprint_v1,
)
from max_smart_db.repository import (
    Adopted,
    AlreadyOwned,
    AcknowledgedDiscard,
    AlreadySucceeded,
    Applied,
    Claimed,
    ClaimedOperation,
    CommitAmbiguous,
    ConversationView,
    DefinitelyFailed,
    DefiniteProviderCategory,
    DurableClaimConfirmed,
    DurableFailureObserved,
    DurableSuccessObserved,
    DurableUnknownObserved,
    NotFound,
    OperationStatus,
    AlreadyFailed,
    Failed,
    OperationView,
    OutcomeNotProven,
    PrepareCommand,
    Prepared,
    PreProviderFailureCategory,
    PreProviderFailureObserved,
    QueuedReservation,
    RetryCommand,
    RetryCreationObserved,
    RetryPrepared,
    RetryUnconfirmed,
    RecoveredUnknown,
    Reconciled,
    RetainedResult,
    UncertainProviderCategory,
    UncertainRecorded,
)


@dataclass(frozen=True)
class TurnRequest:
    """Explicit caller input. The actor is never inferred."""

    actor_id: int
    conversation_id: UUID
    request_content: str
    idempotency_key: str
    runner_id: str
    expected_version: int
    provider_id: str
    model_id: str
    adopt_operation_id: UUID | None = None


@dataclass(frozen=True)
class ProviderFailure:
    """A completed provider attempt that did not produce usable output."""

    category: DefiniteProviderCategory | UncertainProviderCategory


@dataclass(frozen=True)
class ProviderResponse:
    """One provider response after a proven claim. It is not a durable turn."""

    operation_id: UUID
    runner_id: str
    claimed_at: int
    output_text: str


@dataclass(frozen=True)
class DurableTurnCompleted:
    """The provider output is committed as one conversation turn."""

    operation_id: UUID
    claimed_at: int
    user_message: object
    assistant_message: object


@dataclass(frozen=True)
class DurableFailureRecorded:
    """A definite provider failure is durably recorded."""

    operation_id: UUID
    claimed_at: int
    failure_category: DefiniteProviderCategory


@dataclass(frozen=True)
class OutcomeUnconfirmed:
    """The provider attempt is not a durable conversation completion."""

    operation_id: UUID
    runner_id: str
    claimed_at: int
    retained_output: str | None


@dataclass(frozen=True)
class ContextUnavailable:
    """Standing context failed. The provider was not called."""

    operation_id: UUID


class StandingContextRejection(Exception):
    """An approved failure reported before provider execution."""

    def __init__(self, category):
        super().__init__("standing_context_rejected")
        self.category = category


@dataclass(frozen=True)
class DurablePreProviderFailure:
    """A definite pre-provider failure is durably recorded."""

    operation_id: UUID
    failure_category: PreProviderFailureCategory


@dataclass(frozen=True)
class PreProviderFailureUnconfirmed:
    """A pre-provider failure write was not confirmed."""

    operation_id: UUID
    runner_id: str


@dataclass(frozen=True)
class RestartQueued:
    """A persisted QUEUED reservation. It does not authorize execution."""

    operation: OperationView


@dataclass(frozen=True)
class RestartRunning:
    """A persisted RUNNING claim. It may already have reached the provider."""

    operation: OperationView


@dataclass(frozen=True)
class RestartUnknownBlocking:
    """An unacknowledged OUTCOME_UNKNOWN operation still blocks the conversation."""

    operation: OperationView


@dataclass(frozen=True)
class RestartUnknownAcknowledged:
    """An acknowledged OUTCOME_UNKNOWN operation is historical."""

    operation: OperationView


@dataclass(frozen=True)
class RestartCompleted:
    """A persisted SUCCEEDED, FAILED, or CANCELLED operation."""

    operation: OperationView


@dataclass(frozen=True)
class RestartUnproven:
    """The public operation row does not match an accepted restart class."""


@dataclass(frozen=True)
class RecoveryUnconfirmed:
    """Explicit recovery did not prove whether its commit finished."""

    operation_id: UUID | None = None


@dataclass(frozen=True)
class TransactionHeld:
    """A caller transaction was open, so context and the provider were skipped."""


@dataclass(frozen=True)
class ExecutionIdentityMismatch:
    """The caller input does not match the persisted execution request."""


@dataclass(frozen=True)
class RunnerMismatch:
    """The explicit runner is not the runner that would claim."""


@dataclass(frozen=True)
class AlreadyDispatched:
    """This claim was already sent to the provider by this service instance."""

    operation_id: UUID
    runner_id: str
    claimed_at: int


class ConversationService:
    """PREPARE or adopt, claim, call the provider once, then persist that outcome."""

    def __init__(self, repository, load_context, provider):
        self._repository = repository
        self._load_context = load_context
        self._provider = provider
        self._dispatched = set()
        self._claims = {}
        self._retained = {}

    def submit(self, request):
        """Return a repository result, a durable outcome, or an unconfirmed attempt."""
        if type(request) is not TurnRequest:
            raise TypeError("request_type")
        if (
            request.adopt_operation_id is not None
            and type(request.adopt_operation_id) is not UUID
        ):
            raise TypeError("adopt_operation_id_type")
        if request.runner_id != self._repository.runner_id:
            return RunnerMismatch()
        if not _outside_transaction():
            return TransactionHeld()

        reservation = self._boundary(request)
        if type(reservation) is not QueuedReservation:
            return reservation
        operation_id = reservation.operation_id
        if not _outside_transaction():
            return TransactionHeld()
        try:
            context = self._load_context(
                actor_id=request.actor_id,
                conversation_id=request.conversation_id,
                operation_id=operation_id,
                request_content=request.request_content,
            )
        except StandingContextRejection as rejected:
            return self._reject_before_provider(reservation, rejected.category)
        except Exception:
            return self._record_pre_provider(
                reservation,
                PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
            )
        if type(context) is StandingContextRejection:
            return self._reject_before_provider(reservation, context.category)
        if type(context) is not str:
            return self._record_pre_provider(
                reservation,
                PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
            )
        if not _outside_transaction():
            return TransactionHeld()

        claim = self._repository.claim_running(request.actor_id, operation_id)
        claimed = self._authorize(request, operation_id, claim)
        if type(claimed) is not ClaimedOperation:
            return claim
        identity = _identity(claimed)
        if identity in self._dispatched:
            return AlreadyDispatched(
                operation_id=identity[0],
                runner_id=identity[1],
                claimed_at=identity[2],
            )
        if not _outside_transaction():
            return TransactionHeld()
        self._claims[identity] = claimed
        self._dispatched.add(identity)
        try:
            output = self._provider(
                actor_id=request.actor_id,
                conversation_id=request.conversation_id,
                operation_id=identity[0],
                runner_id=identity[1],
                claimed_at=identity[2],
                request_content=request.request_content,
                standing_context=context,
            )
        except Exception:
            return self._persist(
                claimed,
                ("failure", UncertainProviderCategory.PROVIDER_OUTCOME_UNCERTAIN),
            )
        return self._persist(claimed, _interpret(output))

    def classify_restart(self, actor_id, operation_id):
        """Classify one persisted operation without writing or calling the provider."""
        _require_restart_identity(actor_id, operation_id)
        if not _outside_transaction():
            return TransactionHeld()
        viewed = self._repository.get_operation(actor_id, operation_id)
        if type(viewed) is NotFound:
            return viewed
        return _restart_class(viewed, actor_id, operation_id)

    def adopt_persisted(self, actor_id, operation_id):
        """Adopt one persisted reservation. A claim still has to authorize execution."""
        _require_restart_identity(actor_id, operation_id)
        if not _outside_transaction():
            return TransactionHeld()
        return self._repository.adopt_queued(actor_id, operation_id)

    def recover(self):
        """Recover the singleton slot once. This does not call the provider."""
        if not _outside_transaction():
            return TransactionHeld()
        result = self._repository.recover_slot()
        if type(result) is CommitAmbiguous:
            return RecoveryUnconfirmed(operation_id=result.operation_id)
        return result

    def create_retry(
        self,
        actor_id,
        source_operation_id,
        idempotency_key,
        expected_version,
    ):
        """Create one explicit retry. This does not call the provider."""
        _require_retry_request(
            actor_id,
            source_operation_id,
            idempotency_key,
            expected_version,
        )
        if not _outside_transaction():
            return TransactionHeld()
        result = self._repository.create_retry(RetryCommand(
            actor_id=actor_id,
            source_operation_id=source_operation_id,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
        ))
        if type(result) is not CommitAmbiguous:
            return result
        return self._resolve_ambiguous_retry(
            actor_id,
            source_operation_id,
            idempotency_key,
            result,
        )

    def _resolve_ambiguous_retry(
        self,
        actor_id,
        source_operation_id,
        idempotency_key,
        result,
    ):
        if (
            not _outside_transaction()
            or type(result.operation_id) is not UUID
            or result.runner_id != self._repository.runner_id
        ):
            return RetryUnconfirmed(
                operation_id=result.operation_id,
                runner_id=result.runner_id,
            )
        try:
            proof = self._repository.inspect_committed_retry(
                actor_id,
                result.operation_id,
                source_operation_id,
                result.runner_id,
                idempotency_key,
            )
        except Exception:
            return RetryUnconfirmed(
                operation_id=result.operation_id,
                runner_id=result.runner_id,
            )
        if (
            type(proof) is not RetryCreationObserved
            or proof.operation_id != result.operation_id
            or proof.retry_of != source_operation_id
            or proof.runner_id != result.runner_id
        ):
            return RetryUnconfirmed(
                operation_id=result.operation_id,
                runner_id=result.runner_id,
            )
        return RetryPrepared(
            reservation=QueuedReservation(
                operation_id=proof.operation_id,
                actor_id=actor_id,
                conversation_id=proof.conversation_id,
                request_hash=proof.request_hash,
                fingerprint_version=proof.fingerprint_version,
                expected_version=proof.expected_version,
                runner_id=proof.runner_id,
                reservation_expires_at=proof.reservation_expires_at,
            ),
            retry_of=proof.retry_of,
            duplicate_charge_warning=proof.duplicate_charge_warning,
        )

    def _boundary(self, request):
        if request.adopt_operation_id is None:
            prepared = self._repository.prepare(
                PrepareCommand(
                    actor_id=request.actor_id,
                    conversation_id=request.conversation_id,
                    expected_version=request.expected_version,
                    request_content=request.request_content,
                    idempotency_key=request.idempotency_key,
                    provider_id=request.provider_id,
                    model_id=request.model_id,
                )
            )
            if type(prepared) is not Prepared:
                return prepared
            return prepared.reservation

        proved = self._prove_adopted_execution(request)
        if type(proved) is not OperationView:
            return proved
        adopted = self._repository.adopt_queued(
            request.actor_id,
            request.adopt_operation_id,
        )
        if type(adopted) is not Adopted and type(adopted) is not AlreadyOwned:
            return adopted
        if not _reservation_matches(request, proved, adopted.reservation):
            return ExecutionIdentityMismatch()
        return adopted.reservation

    def _prove_adopted_execution(self, request):
        viewed = self._repository.get_operation(
            request.actor_id,
            request.adopt_operation_id,
        )
        if type(viewed) is NotFound:
            return viewed
        if not _operation_identity_matches(request, viewed):
            return ExecutionIdentityMismatch()
        conversation = self._repository.get_conversation(
            request.actor_id,
            viewed.conversation_id,
        )
        if not _execution_fingerprint_matches(request, viewed, conversation):
            return ExecutionIdentityMismatch()
        return viewed

    def _reject_before_provider(self, reservation, category):
        if type(category) is not PreProviderFailureCategory:
            return StandingContextRejection(category)
        return self._record_pre_provider(reservation, category)

    def _record_pre_provider(self, reservation, category):
        if not _outside_transaction():
            return _pre_provider_unconfirmed(reservation)
        try:
            result = self._repository.fail_pre_provider(reservation, category)
        except Exception:
            return _pre_provider_unconfirmed(reservation)
        if type(result) is Failed or type(result) is AlreadyFailed:
            if _confirmed_pre_provider(result.operation, reservation, category):
                return DurablePreProviderFailure(
                    operation_id=reservation.operation_id,
                    failure_category=category,
                )
            return result
        if type(result) is not CommitAmbiguous:
            return result
        return self._resolve_ambiguous_pre_provider(reservation)

    def _resolve_ambiguous_pre_provider(self, reservation):
        if not _outside_transaction():
            return _pre_provider_unconfirmed(reservation)
        try:
            proof = self._repository.inspect_pre_provider_failure(
                reservation.actor_id,
                reservation.operation_id,
                reservation.runner_id,
            )
        except Exception:
            return _pre_provider_unconfirmed(reservation)
        if (
            type(proof) is PreProviderFailureObserved
            and proof.operation_id == reservation.operation_id
        ):
            return DurablePreProviderFailure(
                operation_id=proof.operation_id,
                failure_category=proof.failure_category,
            )
        return _pre_provider_unconfirmed(reservation)

    def _authorize(self, request, operation_id, claim):
        if type(claim) is Claimed:
            claimed = claim.claimed_operation
            if (
                claimed.actor_id != request.actor_id
                or claimed.operation_id != operation_id
                or claimed.runner_id != self._repository.runner_id
                or claimed.conversation_id != request.conversation_id
                or type(claimed.claimed_at) is not int
            ):
                return None
            return claimed
        if type(claim) is not CommitAmbiguous:
            return None
        if (
            claim.operation_id != operation_id
            or type(claim.runner_id) is not str
            or claim.runner_id != self._repository.runner_id
            or type(claim.claimed_at) is not int
        ):
            return None
        try:
            proof = self._repository.confirm_durable_claim(
                request.actor_id,
                claim.operation_id,
                claim.runner_id,
                claim.claimed_at,
            )
        except Exception:
            return None
        if (
            type(proof) is not DurableClaimConfirmed
            or proof.operation_id != claim.operation_id
            or proof.runner_id != claim.runner_id
            or proof.claimed_at != claim.claimed_at
        ):
            return None
        return self._token_from_durable_claim(request, proof)

    def _token_from_durable_claim(self, request, proof):
        try:
            viewed = self._repository.get_operation(
                request.actor_id,
                proof.operation_id,
            )
        except Exception:
            return None
        if type(viewed) is not OperationView:
            return None
        if (
            viewed.status is not OperationStatus.RUNNING
            or viewed.actor_id != request.actor_id
            or viewed.conversation_id != request.conversation_id
            or viewed.runner_id != proof.runner_id
            or viewed.provider_started_at != proof.claimed_at
            or viewed.fingerprint_version != 1
            or type(viewed.expected_version) is not int
            or viewed.expected_version < 0
            or type(viewed.request_hash) is not str
            or len(viewed.request_hash) != 64
        ):
            return None
        if proof.claimed_at > SIGNED_INT64_MAX - FIXED_LEASE_US:
            return None
        return ClaimedOperation(
            operation_id=proof.operation_id,
            actor_id=request.actor_id,
            conversation_id=viewed.conversation_id,
            request_hash=viewed.request_hash,
            fingerprint_version=viewed.fingerprint_version,
            expected_version=viewed.expected_version,
            runner_id=proof.runner_id,
            claimed_at=proof.claimed_at,
            execution_deadline=proof.claimed_at + EXECUTION_DEADLINE_US,
            lease_expires_at=proof.claimed_at + FIXED_LEASE_US,
        )

    def _persist(self, claimed, completion):
        self._remember(claimed, completion)
        if not _outside_transaction():
            return _unconfirmed(claimed, completion)
        try:
            result = self._write(claimed, completion)
        except Exception:
            return _unconfirmed(claimed, completion)
        return self._resolve(claimed, completion, result, apply_retries=1)

    def _resolve(self, claimed, completion, result, apply_retries):
        if type(result) is Applied or type(result) is AlreadySucceeded:
            return self._success_if_observed(claimed, completion)
        if type(result) is DefinitelyFailed:
            return self._failure_if_observed(claimed, completion)
        if type(result) is UncertainRecorded:
            return result
        if type(result) is RecoveredUnknown:
            return self._unknown_if_observed(claimed, completion)
        if type(result) is Reconciled:
            return self._success_if_observed(claimed, completion)
        if type(result) is CommitAmbiguous:
            return self._after_ambiguous(
                claimed,
                completion,
                apply_retries,
                allow_reconcile=True,
            )
        return result

    def _after_ambiguous(
        self,
        claimed,
        completion,
        apply_retries,
        allow_reconcile,
    ):
        proof = self._proof(claimed)
        if type(proof) is DurableSuccessObserved:
            self._retained.pop(_identity(claimed), None)
            return _completed(proof)
        if type(proof) is DurableFailureObserved:
            if completion[0] == "success":
                return _unconfirmed(claimed, completion)
            self._retained.pop(_identity(claimed), None)
            return _recorded(proof)
        if (
            type(proof) is DurableClaimConfirmed
            and apply_retries > 0
            and _same_claim(proof, claimed)
        ):
            if not _outside_transaction():
                return _unconfirmed(claimed, completion)
            try:
                result = self._write(claimed, completion)
            except Exception:
                return _unconfirmed(claimed, completion)
            return self._resolve(
                claimed,
                completion,
                result,
                apply_retries=apply_retries - 1,
            )
        if (
            type(proof) is DurableUnknownObserved
            and allow_reconcile
            and completion[0] == "success"
            and _same_unknown(proof, claimed)
        ):
            return self._reconcile(claimed, completion)
        return _unconfirmed(claimed, completion)

    def _unknown_if_observed(self, claimed, completion):
        if completion[0] != "success":
            return _unconfirmed(claimed, completion)
        proof = self._proof(claimed)
        if type(proof) is DurableSuccessObserved:
            self._retained.pop(_identity(claimed), None)
            return _completed(proof)
        if type(proof) is not DurableUnknownObserved or not _same_unknown(
            proof,
            claimed,
        ):
            return _unconfirmed(claimed, completion)
        return self._reconcile(claimed, completion)

    def _reconcile(self, claimed, completion):
        if not _outside_transaction():
            return _unconfirmed(claimed, completion)
        try:
            result = self._repository.reconcile_late_success(
                RetainedResult(
                    claimed_operation=claimed,
                    output_text=completion[1],
                )
            )
        except Exception:
            return _unconfirmed(claimed, completion)
        if type(result) is Reconciled or type(result) is AlreadySucceeded:
            return self._success_if_observed(claimed, completion)
        if type(result) is CommitAmbiguous:
            return self._after_ambiguous(
                claimed,
                completion,
                apply_retries=0,
                allow_reconcile=False,
            )
        if type(result) is AcknowledgedDiscard:
            self._retained.pop(_identity(claimed), None)
        return result

    def _success_if_observed(self, claimed, completion):
        proof = self._proof(claimed)
        if type(proof) is DurableSuccessObserved and proof.claimed_at == claimed.claimed_at:
            self._retained.pop(_identity(claimed), None)
            return _completed(proof)
        return _unconfirmed(claimed, completion)

    def _failure_if_observed(self, claimed, completion):
        proof = self._proof(claimed)
        if (
            type(proof) is DurableFailureObserved
            and proof.claimed_at == claimed.claimed_at
            and proof.operation_id == claimed.operation_id
        ):
            self._retained.pop(_identity(claimed), None)
            return _recorded(proof)
        return _unconfirmed(claimed, completion)

    def _proof(self, claimed):
        if not _outside_transaction():
            return OutcomeNotProven()
        try:
            return self._repository.inspect_durable_outcome(
                claimed.actor_id,
                claimed.operation_id,
                claimed.runner_id,
                claimed.claimed_at,
            )
        except Exception:
            return OutcomeNotProven()

    def _write(self, claimed, completion):
        if completion[0] == "success":
            return self._repository.apply_success(claimed, completion[1])
        return self._repository.apply_failure(claimed, completion[1])

    def _remember(self, claimed, completion):
        if completion[0] == "success":
            self._retained[_identity(claimed)] = completion[1]


def _interpret(output):
    if type(output) is str:
        if _usable_output(output):
            return ("success", output)
        return ("failure", DefiniteProviderCategory.PROVIDER_OUTPUT_INVALID)
    if type(output) is ProviderFailure and (
        type(output.category) is DefiniteProviderCategory
        or type(output.category) is UncertainProviderCategory
    ):
        return ("failure", output.category)
    return ("failure", UncertainProviderCategory.PROVIDER_OUTCOME_UNCERTAIN)


def _usable_output(text):
    try:
        text.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return "\x00" not in text and bool(text.strip())


def _identity(claimed):
    return (claimed.operation_id, claimed.runner_id, claimed.claimed_at)


def _same_claim(proof, claimed):
    return (
        proof.operation_id == claimed.operation_id
        and proof.runner_id == claimed.runner_id
        and proof.claimed_at == claimed.claimed_at
    )


def _same_unknown(proof, claimed):
    return _same_claim(proof, claimed)


def _completed(proof):
    return DurableTurnCompleted(
        operation_id=proof.operation_id,
        claimed_at=proof.claimed_at,
        user_message=proof.user_message,
        assistant_message=proof.assistant_message,
    )


def _recorded(proof):
    return DurableFailureRecorded(
        operation_id=proof.operation_id,
        claimed_at=proof.claimed_at,
        failure_category=proof.failure_category,
    )


def _unconfirmed(claimed, completion):
    retained = completion[1] if completion[0] == "success" else None
    return OutcomeUnconfirmed(
        operation_id=claimed.operation_id,
        runner_id=claimed.runner_id,
        claimed_at=claimed.claimed_at,
        retained_output=retained,
    )


def _outside_transaction():
    return connection.get_autocommit() and not connection.in_atomic_block


def _pre_provider_unconfirmed(reservation):
    return PreProviderFailureUnconfirmed(
        operation_id=reservation.operation_id,
        runner_id=reservation.runner_id,
    )


def _confirmed_pre_provider(view, reservation, category):
    observed = getattr(view, "failure_category", None)
    return (
        type(view) is OperationView
        and view.operation_id == reservation.operation_id
        and view.actor_id == reservation.actor_id
        and view.conversation_id == reservation.conversation_id
        and view.status is OperationStatus.FAILED
        and view.provider_started_at is None
        and view.runner_id is None
        and view.blocks_conversation is False
        and view.acknowledged is False
        and observed is not None
        and observed.value == category.value
    )


def _operation_identity_matches(request, viewed):
    return (
        type(viewed) is OperationView
        and viewed.actor_id == request.actor_id
        and viewed.operation_id == request.adopt_operation_id
        and viewed.conversation_id == request.conversation_id
        and viewed.expected_version == request.expected_version
        and viewed.fingerprint_version == FINGERPRINT_VERSION
    )


def _execution_fingerprint_matches(request, viewed, conversation):
    if (
        type(conversation) is not ConversationView
        or conversation.actor_id != request.actor_id
        or conversation.conversation_id != viewed.conversation_id
        or type(conversation.role_slug) is not str
        or type(viewed.request_hash) is not str
    ):
        return False
    try:
        digest = fingerprint_v1(
            request.actor_id,
            request.conversation_id.hex,
            "message",
            request.expected_version,
            conversation.role_slug,
            request.provider_id,
            request.model_id,
            request.request_content,
        )
    except FingerprintValidationError:
        return False
    return digest == viewed.request_hash


def _reservation_matches(request, viewed, reservation):
    return (
        type(reservation) is QueuedReservation
        and reservation.operation_id == viewed.operation_id
        and reservation.actor_id == request.actor_id
        and reservation.conversation_id == request.conversation_id
        and reservation.expected_version == request.expected_version
        and reservation.fingerprint_version == FINGERPRINT_VERSION
        and reservation.request_hash == viewed.request_hash
        and reservation.runner_id == request.runner_id
    )


def _require_retry_request(
    actor_id,
    source_operation_id,
    idempotency_key,
    expected_version,
):
    if type(actor_id) is not int:
        raise TypeError("actor_id_type")
    if type(source_operation_id) is not UUID:
        raise TypeError("source_operation_id_type")
    if type(idempotency_key) is not str:
        raise TypeError("idempotency_key_type")
    if type(expected_version) is not int:
        raise TypeError("expected_version_type")


def _require_restart_identity(actor_id, operation_id):
    if type(actor_id) is not int:
        raise TypeError("actor_id_type")
    if type(operation_id) is not UUID:
        raise TypeError("operation_id_type")


def _restart_class(view, actor_id, operation_id):
    if (
        type(view) is not OperationView
        or view.actor_id != actor_id
        or view.operation_id != operation_id
    ):
        return RestartUnproven()
    status = view.status
    if status is OperationStatus.QUEUED and _queued_observation(view):
        return RestartQueued(view)
    if status is OperationStatus.RUNNING and _running_observation(view):
        return RestartRunning(view)
    if status is OperationStatus.OUTCOME_UNKNOWN and _blocking_unknown(view):
        return RestartUnknownBlocking(view)
    if (
        status is OperationStatus.OUTCOME_UNKNOWN
        and _acknowledged_unknown(view)
    ):
        return RestartUnknownAcknowledged(view)
    if status in (
        OperationStatus.SUCCEEDED,
        OperationStatus.FAILED,
        OperationStatus.CANCELLED,
    ) and _completed_observation(view):
        return RestartCompleted(view)
    return RestartUnproven()


def _queued_observation(view):
    return (
        view.blocks_conversation is True
        and view.acknowledged is False
        and view.provider_started_at is None
        and type(view.runner_id) is str
        and view.runner_id != ""
    )


def _running_observation(view):
    return (
        view.blocks_conversation is True
        and view.acknowledged is False
        and type(view.provider_started_at) is int
        and type(view.runner_id) is str
        and view.runner_id != ""
    )


def _blocking_unknown(view):
    return (
        view.blocks_conversation is True
        and view.acknowledged is False
        and type(view.provider_started_at) is int
        and type(view.runner_id) is str
        and view.runner_id != ""
    )


def _acknowledged_unknown(view):
    return view.acknowledged is True and view.blocks_conversation is False


def _completed_observation(view):
    return (
        view.blocks_conversation is False
        and view.acknowledged is False
        and view.runner_id is None
    )
