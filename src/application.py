"""Interface-independent conversation orchestration for Max Smart."""

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple


Message = Dict[str, str]
RequestModel = Callable[[List[Message]], str]
SaveMessages = Callable[[List[Message]], None]


class ModelRequestError(Exception):
    """An expected model-request failure with no provider details."""


class SessionSaveError(Exception):
    """An expected session-save failure with no storage details."""


class ConversationStateError(RuntimeError):
    """An operation was attempted in the wrong conversation state."""


class TurnStatus(Enum):
    SUCCESS = "success"
    MODEL_REQUEST_FAILED = "model_request_failed"
    SAVE_FAILED = "save_failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ConversationMessage:
    """An immutable provider-neutral conversation message."""

    role: str
    content: str


@dataclass(frozen=True)
class PreparedTurn:
    """Immutable output of PREPARE for one explicit model attempt."""

    user_text: str
    base_messages: Tuple[ConversationMessage, ...]
    model_input: Tuple[ConversationMessage, ...]

    def request_messages(self) -> List[Message]:
        """Return a mutable copy suitable for a provider adapter."""
        return [
            {"role": message.role, "content": message.content}
            for message in self.model_input
        ]


class ModelOutcomeKind(Enum):
    SUCCESS = "success"
    DEFINITE_FAILURE = "definite_failure"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class ModelOutcome:
    """Provider-neutral result of exactly one REQUEST attempt."""

    kind: ModelOutcomeKind
    assistant_text: Optional[str] = None

    def __post_init__(self):
        if (
            self.kind is ModelOutcomeKind.SUCCESS
            and (
                not isinstance(self.assistant_text, str)
                or not self.assistant_text.strip()
            )
        ):
            raise ValueError(
                "A successful model outcome requires usable assistant text."
            )
        if (
            self.kind is not ModelOutcomeKind.SUCCESS
            and self.assistant_text is not None
        ):
            raise ValueError("A failed model outcome cannot include assistant text.")


@dataclass(frozen=True)
class AppliedTurn:
    """Immutable output of APPLY before persistence."""

    pending_message: Optional[str] = None
    proposed_messages: Optional[Tuple[ConversationMessage, ...]] = None
    assistant_text: Optional[str] = None

    def __post_init__(self):
        is_pending = self.pending_message is not None
        has_success = (
            self.proposed_messages is not None
            and self.assistant_text is not None
        )
        if is_pending == has_success:
            raise ValueError(
                "An applied turn must be either pending or a proposed success."
            )

    def messages_to_save(self) -> List[Message]:
        """Return a mutable copy of the proposed successful snapshot."""
        if self.proposed_messages is None:
            raise ConversationStateError("A pending turn has no snapshot to save.")
        return [
            {"role": message.role, "content": message.content}
            for message in self.proposed_messages
        ]


@dataclass(frozen=True)
class TurnResult:
    """The safe result of one explicit application operation."""

    status: TurnStatus
    assistant_text: Optional[str] = None

    def __post_init__(self):
        if self.status is TurnStatus.SUCCESS and self.assistant_text is None:
            raise ValueError("A successful turn requires assistant text.")
        if self.status is not TurnStatus.SUCCESS and self.assistant_text is not None:
            raise ValueError("Only a successful turn may include assistant text.")


def prepare_turn(messages: List[Message], text: str) -> PreparedTurn:
    """PREPARE one immutable attempt without requesting or persisting."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Message text must be a non-blank string.")

    base_messages = tuple(
        ConversationMessage(message["role"], message["content"])
        for message in messages
    )
    user_message = ConversationMessage("user", text)
    return PreparedTurn(
        user_text=text,
        base_messages=base_messages,
        model_input=base_messages + (user_message,),
    )


def request_prepared(
    request_model: RequestModel,
    prepared: PreparedTurn,
) -> ModelOutcome:
    """REQUEST once; conservatively classify expected failures as uncertain.

    The current provider adapter exposes only ModelRequestError and cannot
    prove that a failed request was not processed by the provider.
    """
    try:
        assistant_text = request_model(prepared.request_messages())
    except ModelRequestError:
        return ModelOutcome(ModelOutcomeKind.UNCERTAIN)
    if not isinstance(assistant_text, str) or not assistant_text.strip():
        return ModelOutcome(ModelOutcomeKind.DEFINITE_FAILURE)
    return ModelOutcome(ModelOutcomeKind.SUCCESS, assistant_text)


def apply_model_outcome(
    prepared: PreparedTurn,
    outcome: ModelOutcome,
) -> AppliedTurn:
    """APPLY a normalized outcome to produce a pending or proposed turn."""
    if outcome.kind in (
        ModelOutcomeKind.DEFINITE_FAILURE,
        ModelOutcomeKind.UNCERTAIN,
    ):
        return AppliedTurn(pending_message=prepared.user_text)

    proposed_messages = prepared.model_input + (
        ConversationMessage("assistant", outcome.assistant_text),
    )
    return AppliedTurn(
        proposed_messages=proposed_messages,
        assistant_text=outcome.assistant_text,
    )


class MaxSmartApplication:
    """Own conversation state and perform at most one request per operation."""

    def __init__(
        self,
        request_model: RequestModel,
        save_messages: SaveMessages,
        messages: List[Message],
    ):
        self._request_model = request_model
        self._save_messages = save_messages
        self._messages = self._copy_messages(messages)
        self._pending_message = None  # type: Optional[str]
        self._unsaved_messages = None  # type: Optional[List[Message]]
        self._unsaved_assistant_text = None  # type: Optional[str]

    @property
    def messages(self) -> List[Message]:
        """Return a copy of successfully saved conversation history."""
        return self._copy_messages(self._messages)

    @property
    def has_pending_message(self) -> bool:
        return self._pending_message is not None

    @property
    def has_unsaved_turn(self) -> bool:
        return self._unsaved_messages is not None

    def submit_message(self, text: str) -> TurnResult:
        """Submit one new message from the idle state."""
        prepared = prepare_turn(self._messages, text)
        self._require_idle("submit a new message")

        return self._execute_prepared(prepared)

    def retry_pending(self) -> TurnResult:
        """Retry the pending message once after explicit user approval."""
        if self._pending_message is None or self._unsaved_messages is not None:
            raise ConversationStateError("There is no pending message to retry.")
        prepared = prepare_turn(self._messages, self._pending_message)
        return self._execute_prepared(prepared)

    def cancel_pending(self) -> TurnResult:
        """Discard the pending message without requesting or saving."""
        if self._pending_message is None or self._unsaved_messages is not None:
            raise ConversationStateError("There is no pending message to cancel.")

        self._pending_message = None
        return TurnResult(TurnStatus.CANCELLED)

    def retry_save(self) -> TurnResult:
        """Retry the unsaved snapshot once without contacting the model."""
        if self._unsaved_messages is None or self._pending_message is not None:
            raise ConversationStateError("There is no unsaved turn to save.")
        return self._attempt_save()

    def _execute_prepared(self, prepared: PreparedTurn) -> TurnResult:
        outcome = request_prepared(self._request_model, prepared)
        applied = apply_model_outcome(prepared, outcome)

        if applied.pending_message is not None:
            self._pending_message = applied.pending_message
            return TurnResult(TurnStatus.MODEL_REQUEST_FAILED)

        self._unsaved_messages = applied.messages_to_save()
        self._unsaved_assistant_text = applied.assistant_text
        self._pending_message = None
        return self._attempt_save()

    def _attempt_save(self) -> TurnResult:
        try:
            self._save_messages(self._copy_messages(self._unsaved_messages))
        except SessionSaveError:
            return TurnResult(TurnStatus.SAVE_FAILED)

        assistant_text = self._unsaved_assistant_text
        self._messages = self._copy_messages(self._unsaved_messages)
        self._unsaved_messages = None
        self._unsaved_assistant_text = None
        return TurnResult(TurnStatus.SUCCESS, assistant_text)

    def _require_idle(self, operation: str) -> None:
        if self._pending_message is not None or self._unsaved_messages is not None:
            raise ConversationStateError(
                f"Cannot {operation} while another turn is unresolved."
            )

    @staticmethod
    def _copy_messages(messages: List[Message]) -> List[Message]:
        return [dict(message) for message in messages]
