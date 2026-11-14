"""Explicit operator commands over an accepted ConversationService.

Nothing in this module runs at construction. Classification, recovery,
acknowledgement, and retry creation each require a direct call, and none of
them calls the provider. RestartQueued reports a persisted reservation; it
does not establish that the reservation is still live.

The accepted service source cannot expose acknowledgement. This module calls
the repository acknowledgement held by the service. When that write is
ambiguous, it accepts only the repository's fresh-snapshot proof. An unproven
read does not write again.
"""

from dataclasses import dataclass
from uuid import UUID

from django.db import connection

from conversation_service import TransactionHeld
from max_smart_db.repository import (
    AcknowledgementObserved,
    Acknowledged,
    AlreadyAcknowledged,
    CommitAmbiguous,
    OperationStatus,
    OperationView,
)


@dataclass(frozen=True)
class AcknowledgementUnconfirmed:
    """The acknowledgement write was not proven. No second write was made."""

    operation_id: UUID


class OperatorCommands:
    """Operator actions. Construction does not read or write."""

    def __init__(self, service):
        self._service = service

    def classify(self, owner_id, operation_id):
        """Return the accepted restart classification without mutating."""

        return self._service.classify_restart(owner_id, operation_id)

    def recover(self):
        """Recover the singleton slot once through the accepted service."""

        return self._service.recover()

    def acknowledge(self, owner_id, operation_id):
        """Acknowledge one owner-scoped unknown outcome, at most once."""

        _require_operation(owner_id, operation_id)
        if not _outside_transaction():
            return TransactionHeld()
        result = self._service._repository.acknowledge_unknown(
            owner_id,
            operation_id,
        )
        if type(result) is CommitAmbiguous:
            return self._prove_acknowledgement(owner_id, operation_id)
        if type(result) is Acknowledged or type(result) is AlreadyAcknowledged:
            self._drop_retained(operation_id)
        return result

    def create_retry(
        self,
        owner_id,
        source_operation_id,
        idempotency_key,
        expected_version,
    ):
        """Create one retry through the accepted service. No request text."""

        return self._service.create_retry(
            owner_id,
            source_operation_id,
            idempotency_key,
            expected_version,
        )

    def _prove_acknowledgement(self, owner_id, operation_id):
        try:
            proof = self._service._repository.inspect_acknowledged_unknown(
                owner_id,
                operation_id,
            )
        except Exception:
            return AcknowledgementUnconfirmed(operation_id)
        if type(proof) is not AcknowledgementObserved:
            return AcknowledgementUnconfirmed(operation_id)
        viewed = proof.operation
        if (
            type(viewed) is OperationView
            and viewed.operation_id == operation_id
            and viewed.actor_id == owner_id
            and viewed.status is OperationStatus.OUTCOME_UNKNOWN
            and viewed.acknowledged is True
            and viewed.blocks_conversation is False
        ):
            self._drop_retained(operation_id)
            return Acknowledged(viewed)
        return AcknowledgementUnconfirmed(operation_id)

    def _drop_retained(self, operation_id):
        retained = getattr(self._service, "_retained", None)
        if type(retained) is not dict:
            return
        for identity in list(retained):
            if identity[0] == operation_id:
                retained.pop(identity, None)


def _require_operation(owner_id, operation_id):
    if type(owner_id) is not int:
        raise TypeError("actor_id_type")
    if type(operation_id) is not UUID:
        raise TypeError("operation_id_type")


def _outside_transaction():
    return connection.get_autocommit() and not connection.in_atomic_block
