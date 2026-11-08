"""Typed repository contracts. R1 defines interfaces and does not write."""

from enum import Enum
from typing import Protocol, Union
from uuid import UUID

from dataclasses import dataclass


class Clock(Protocol):
    """UTC microsecond source supplied by the caller."""

    def now_us(self) -> int:
        """Return one UTC timestamp. Repository writes must not accept one."""


class FailureClassification(Enum):
    """Boundary classification before a repository transition is selected."""

    DEFINITE_PRE_PROVIDER = "definite_pre_provider"
    DEFINITE_PROVIDER = "definite_provider"
    UNCERTAIN_PROVIDER = "uncertain_provider"
    PERSISTENCE_FAILED = "persistence_failed"


class StoredFailureCategory(Enum):
    """Categories permitted by the frozen operation failure check."""

    CONTEXT_UNAVAILABLE = "context_unavailable"
    CONTEXT_BUDGET_EXCEEDED = "context_budget_exceeded"
    MODEL_CONFIGURATION_INVALID = "model_configuration_invalid"
    PROVIDER_DEFINITE_FAILURE = "provider_definite_failure"
    PROVIDER_OUTPUT_INVALID = "provider_output_invalid"
    PROVIDER_OUTCOME_UNCERTAIN = "provider_outcome_uncertain"
    PERSISTENCE_FAILED = "persistence_failed"


class OperationStatus(Enum):
    """Statuses permitted by the frozen operation status check."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    CANCELLED = "CANCELLED"


class MessageRole(Enum):
    """Roles permitted by the frozen message role check."""

    USER = "user"
    ASSISTANT = "assistant"


class PreProviderFailureCategory(Enum):
    """Definite failure categories valid before the provider boundary."""

    CONTEXT_UNAVAILABLE = "context_unavailable"
    CONTEXT_BUDGET_EXCEEDED = "context_budget_exceeded"
    MODEL_CONFIGURATION_INVALID = "model_configuration_invalid"


class DefiniteProviderCategory(Enum):
    """Stored categories valid for a definite provider failure."""

    PROVIDER_DEFINITE_FAILURE = "provider_definite_failure"
    PROVIDER_OUTPUT_INVALID = "provider_output_invalid"


class UncertainProviderCategory(Enum):
    """Stored category valid for an uncertain provider outcome."""

    PROVIDER_OUTCOME_UNCERTAIN = "provider_outcome_uncertain"


ProviderFailureInput = Union[
    DefiniteProviderCategory,
    UncertainProviderCategory,
]


class RepositoryIntegrityError(RuntimeError):
    """Redacted integrity failure. It carries no row content."""

    code = "repository_integrity"


@dataclass(frozen=True)
class PrepareCommand:
    """Caller-supplied PREPARE input. It contains no timestamp."""

    actor_id: int
    conversation_id: UUID
    expected_version: int
    request_content: str
    idempotency_key: str
    provider_id: str
    model_id: str


@dataclass(frozen=True)
class QueuedReservation:
    operation_id: UUID
    actor_id: int
    conversation_id: UUID
    request_hash: str
    fingerprint_version: int
    expected_version: int
    runner_id: str
    reservation_expires_at: int


@dataclass(frozen=True)
class ClaimedOperation:
    operation_id: UUID
    actor_id: int
    conversation_id: UUID
    request_hash: str
    fingerprint_version: int
    expected_version: int
    runner_id: str
    claimed_at: int
    execution_deadline: int
    lease_expires_at: int


@dataclass(frozen=True)
class RetainedResult:
    claimed_operation: ClaimedOperation
    output_text: str


@dataclass(frozen=True)
class ConversationView:
    conversation_id: UUID
    actor_id: int
    role_slug: str
    version: int
    next_sequence: int
    title: str | None


@dataclass(frozen=True)
class MessageView:
    message_id: UUID
    conversation_id: UUID
    sequence: int
    role: MessageRole
    content: str
    operation_id: UUID | None


@dataclass(frozen=True)
class Snapshot:
    conversation: ConversationView
    messages: tuple[MessageView, ...]


@dataclass(frozen=True)
class OperationView:
    operation_id: UUID
    actor_id: int
    conversation_id: UUID
    status: OperationStatus
    runner_id: str | None
    request_hash: str
    fingerprint_version: int
    expected_version: int
    provider_started_at: int | None
    failure_category: StoredFailureCategory | None
    blocks_conversation: bool
    acknowledged: bool


@dataclass(frozen=True)
class NotFound:
    """Missing and cross-owner objects share this result."""


@dataclass(frozen=True)
class DatabaseBusy:
    """The write lock was not acquired and no write occurred."""


@dataclass(frozen=True)
class CommitAmbiguous:
    """Commit outcome is unknown. Callers reread instead of replaying."""

    operation_id: UUID | None = None
    runner_id: str | None = None
    claimed_at: int | None = None
    conversation_id: UUID | None = None


@dataclass(frozen=True)
class ConversationConflict:
    """An expected uniqueness conflict. It carries no row payload."""


@dataclass(frozen=True)
class Prepared:
    reservation: QueuedReservation


@dataclass(frozen=True)
class Existing:
    operation: OperationView


@dataclass(frozen=True)
class KeyBindingMismatch:
    pass


@dataclass(frozen=True)
class StaleVersion:
    actual: int


@dataclass(frozen=True)
class ConversationBlocked:
    operation_id: UUID
    status: OperationStatus


@dataclass(frozen=True)
class ModelBusy:
    pass


@dataclass(frozen=True)
class Adopted:
    reservation: QueuedReservation


@dataclass(frozen=True)
class AlreadyOwned:
    reservation: QueuedReservation


@dataclass(frozen=True)
class ExpiredResubmit:
    pass


@dataclass(frozen=True)
class NotQueued:
    view: OperationView


@dataclass(frozen=True)
class OwnershipLost:
    pass


@dataclass(frozen=True)
class SlotMismatch:
    pass


@dataclass(frozen=True)
class Claimed:
    claimed_operation: ClaimedOperation


@dataclass(frozen=True)
class DurableClaimConfirmed:
    """A fresh snapshot proved this exact durable RUNNING claim."""

    operation_id: UUID
    runner_id: str
    claimed_at: int


@dataclass(frozen=True)
class ClaimNotConfirmed:
    """The snapshot did not prove a durable claim."""


@dataclass(frozen=True)
class Failed:
    operation: OperationView


@dataclass(frozen=True)
class AlreadyFailed:
    operation: OperationView


@dataclass(frozen=True)
class Applied:
    operation: OperationView


@dataclass(frozen=True)
class AlreadySucceeded:
    """Previously committed operation and its two linked messages."""

    operation: OperationView
    user_message: MessageView
    assistant_message: MessageView


@dataclass(frozen=True)
class RecoveredUnknown:
    reconcile_required: bool


@dataclass(frozen=True)
class InvalidState:
    state: OperationStatus


@dataclass(frozen=True)
class DefinitelyFailed:
    operation: OperationView


@dataclass(frozen=True)
class UncertainRecorded:
    operation: OperationView
    recovery_at: int


@dataclass(frozen=True)
class AlreadyTerminal:
    operation: OperationView


@dataclass(frozen=True)
class Reconciled:
    operation: OperationView


@dataclass(frozen=True)
class AcknowledgedDiscard:
    pass


@dataclass(frozen=True)
class FingerprintMismatch:
    pass


@dataclass(frozen=True)
class RunnerMismatchDiscard:
    pass


@dataclass(frozen=True)
class StaleVersionDiscard:
    pass


@dataclass(frozen=True)
class InvalidStateDiscard:
    pass


@dataclass(frozen=True)
class SlotFree:
    pass


@dataclass(frozen=True)
class LiveQueued:
    pass


@dataclass(frozen=True)
class LiveRunning:
    pass


@dataclass(frozen=True)
class CancelledExpiredQueued:
    pass


@dataclass(frozen=True)
class MarkedOutcomeUnknown:
    pass


@dataclass(frozen=True)
class InconsistentSlot:
    pass


@dataclass(frozen=True)
class OwnershipMismatch:
    pass


@dataclass(frozen=True)
class Acknowledged:
    operation: OperationView


@dataclass(frozen=True)
class AlreadyAcknowledged:
    operation: OperationView


@dataclass(frozen=True)
class NotUnknown:
    current_state: OperationStatus


@dataclass(frozen=True)
class CleanupResult:
    deleted_count: int


PrepareResult = Union[
    Prepared,
    Existing,
    KeyBindingMismatch,
    StaleVersion,
    ConversationBlocked,
    ModelBusy,
    NotFound,
    DatabaseBusy,
    CommitAmbiguous,
]
AdoptResult = Union[
    Adopted,
    AlreadyOwned,
    ExpiredResubmit,
    NotQueued,
    OwnershipLost,
    SlotMismatch,
    NotFound,
    DatabaseBusy,
    CommitAmbiguous,
]
ClaimResult = Union[
    Claimed,
    ExpiredResubmit,
    NotQueued,
    OwnershipLost,
    SlotMismatch,
    NotFound,
    DatabaseBusy,
    CommitAmbiguous,
]
ConfirmDurableClaimResult = Union[DurableClaimConfirmed, ClaimNotConfirmed]
PreProviderFailureResult = Union[
    Failed,
    AlreadyFailed,
    NotQueued,
    OwnershipLost,
    SlotMismatch,
    DatabaseBusy,
    CommitAmbiguous,
]
ApplySuccessResult = Union[
    Applied,
    AlreadySucceeded,
    RecoveredUnknown,
    OwnershipLost,
    SlotMismatch,
    StaleVersion,
    InvalidState,
    DatabaseBusy,
    CommitAmbiguous,
]
ApplyFailureResult = Union[
    DefinitelyFailed,
    UncertainRecorded,
    AlreadyTerminal,
    OwnershipLost,
    SlotMismatch,
    InvalidState,
    DatabaseBusy,
    CommitAmbiguous,
]
ReconcileResult = Union[
    Reconciled,
    AlreadySucceeded,
    AcknowledgedDiscard,
    FingerprintMismatch,
    RunnerMismatchDiscard,
    StaleVersionDiscard,
    InvalidStateDiscard,
    DatabaseBusy,
    CommitAmbiguous,
]
RecoveryResult = Union[
    SlotFree,
    LiveQueued,
    LiveRunning,
    CancelledExpiredQueued,
    MarkedOutcomeUnknown,
    InconsistentSlot,
    OwnershipMismatch,
    DatabaseBusy,
    CommitAmbiguous,
]
AcknowledgeResult = Union[
    Acknowledged,
    AlreadyAcknowledged,
    NotUnknown,
    NotFound,
    DatabaseBusy,
    CommitAmbiguous,
]
CreateConversationResult = Union[
    ConversationView,
    ConversationConflict,
    DatabaseBusy,
    CommitAmbiguous,
]
ReadConversationResult = Union[ConversationView, NotFound]
ReadSnapshotResult = Union[Snapshot, NotFound]
ReadOperationResult = Union[OperationView, NotFound]
ReadMessagesResult = Union[tuple[MessageView, ...], NotFound]
CleanupOperationResult = Union[CleanupResult, DatabaseBusy, CommitAmbiguous]


class ConversationRepository(Protocol):
    """Aggregate repository contract. R1 has no transition implementation."""

    clock: Clock
    runner_id: str

    def create_conversation(
        self,
        actor_id: int,
        role_slug: str,
        title: str | None,
    ) -> CreateConversationResult:
        """Create one conversation. Not implemented in R1."""

    def get_conversation(
        self,
        actor_id: int,
        conversation_id: UUID,
    ) -> ReadConversationResult:
        """Read one actor-scoped conversation. Not implemented in R1."""

    def load_snapshot(
        self,
        actor_id: int,
        conversation_id: UUID,
    ) -> ReadSnapshotResult:
        """Read one actor-scoped snapshot. Not implemented in R1."""

    def list_messages(
        self,
        actor_id: int,
        conversation_id: UUID,
        after_sequence: int | None = None,
    ) -> ReadMessagesResult:
        """Read actor-scoped messages. Missing and cross-owner return NotFound.

        Not implemented in R1.
        """

    def prepare(self, command: PrepareCommand) -> PrepareResult:
        """Prepare one operation. Not implemented in R1."""

    def adopt_queued(
        self,
        actor_id: int,
        operation_id: UUID,
    ) -> AdoptResult:
        """Adopt one queued operation. Not implemented in R1."""

    def claim_running(
        self,
        actor_id: int,
        operation_id: UUID,
    ) -> ClaimResult:
        """Claim one queued operation. Not implemented in R1."""

    def confirm_durable_claim(
        self,
        actor_id: int,
        operation_id: UUID,
        runner_id: str,
        claimed_at: int,
    ) -> ConfirmDurableClaimResult:
        """Prove one committed RUNNING claim from a fresh read snapshot."""

    def fail_pre_provider(
        self,
        reservation: QueuedReservation,
        category: PreProviderFailureCategory,
    ) -> PreProviderFailureResult:
        """Record a definite pre-provider failure. Not implemented in R1."""

    def apply_success(
        self,
        claimed: ClaimedOperation,
        output_text: str,
    ) -> ApplySuccessResult:
        """Apply one successful output. Not implemented in R1."""

    def apply_failure(
        self,
        claimed: ClaimedOperation,
        failure: ProviderFailureInput,
    ) -> ApplyFailureResult:
        """Apply one definite or uncertain provider outcome. Not implemented in R1."""

    def reconcile_late_success(
        self,
        retained_result: RetainedResult,
    ) -> ReconcileResult:
        """Reconcile one retained success. Not implemented in R1."""

    def recover_slot(self) -> RecoveryResult:
        """Classify or recover the global slot. Not implemented in R1."""

    def acknowledge_unknown(
        self,
        actor_id: int,
        operation_id: UUID,
    ) -> AcknowledgeResult:
        """Acknowledge one unknown outcome. Not implemented in R1."""

    def get_operation(
        self,
        actor_id: int,
        operation_id: UUID,
    ) -> ReadOperationResult:
        """Read one actor-scoped operation. Not implemented in R1."""

    def delete_expired_idempotency(
        self,
        limit: int,
    ) -> CleanupOperationResult:
        """Delete expired idempotency rows. Not implemented in R1."""
