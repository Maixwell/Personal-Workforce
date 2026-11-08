"""Orchestrate one turn through a durable claim. Nothing here is persisted."""

from dataclasses import dataclass
from uuid import UUID

from django.db import connection

from max_smart_db.repository import (
    Adopted,
    AlreadyOwned,
    Claimed,
    CommitAmbiguous,
    DurableClaimConfirmed,
    PrepareCommand,
    Prepared,
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
class ProviderResponse:
    """One provider response after a proven claim. It is not a durable turn."""

    operation_id: UUID
    runner_id: str
    claimed_at: int
    output_text: str


@dataclass(frozen=True)
class ContextUnavailable:
    """Standing context failed. The provider was not called."""

    operation_id: UUID


@dataclass(frozen=True)
class TransactionHeld:
    """A caller transaction was open, so context and the provider were skipped."""


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
    """PREPARE or adopt, load context, claim, then call the provider at most once."""

    def __init__(self, repository, load_context, provider):
        self._repository = repository
        self._load_context = load_context
        self._provider = provider
        self._dispatched = set()

    def submit(self, request):
        """Return a repository result, or one non-durable provider response."""
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

        operation_id = self._boundary(request)
        if type(operation_id) is not UUID:
            return operation_id
        if not _outside_transaction():
            return TransactionHeld()
        try:
            context = self._load_context(
                actor_id=request.actor_id,
                conversation_id=request.conversation_id,
                operation_id=operation_id,
                request_content=request.request_content,
            )
        except Exception:
            return ContextUnavailable(operation_id=operation_id)
        if type(context) is not str:
            return ContextUnavailable(operation_id=operation_id)
        if not _outside_transaction():
            return TransactionHeld()

        claim = self._repository.claim_running(request.actor_id, operation_id)
        authorized = self._authorized(request.actor_id, operation_id, claim)
        if authorized is None:
            return claim
        if authorized in self._dispatched:
            return AlreadyDispatched(
                operation_id=authorized[0],
                runner_id=authorized[1],
                claimed_at=authorized[2],
            )
        if not _outside_transaction():
            return TransactionHeld()
        self._dispatched.add(authorized)
        output = self._provider(
            actor_id=request.actor_id,
            conversation_id=request.conversation_id,
            operation_id=authorized[0],
            runner_id=authorized[1],
            claimed_at=authorized[2],
            request_content=request.request_content,
            standing_context=context,
        )
        if type(output) is not str:
            raise TypeError("provider_output_type")
        return ProviderResponse(
            operation_id=authorized[0],
            runner_id=authorized[1],
            claimed_at=authorized[2],
            output_text=output,
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
            return prepared.reservation.operation_id

        adopted = self._repository.adopt_queued(
            request.actor_id,
            request.adopt_operation_id,
        )
        if type(adopted) is not Adopted and type(adopted) is not AlreadyOwned:
            return adopted
        if adopted.reservation.runner_id != request.runner_id:
            return adopted
        return adopted.reservation.operation_id

    def _authorized(self, actor_id, operation_id, claim):
        if type(claim) is Claimed:
            claimed = claim.claimed_operation
            if (
                claimed.actor_id != actor_id
                or claimed.operation_id != operation_id
                or claimed.runner_id != self._repository.runner_id
                or type(claimed.claimed_at) is not int
            ):
                return None
            return (
                claimed.operation_id,
                claimed.runner_id,
                claimed.claimed_at,
            )
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
                actor_id,
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
        return proof.operation_id, proof.runner_id, proof.claimed_at


def _outside_transaction():
    return connection.get_autocommit() and not connection.in_atomic_block
