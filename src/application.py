"""Interface-independent conversation orchestration for Max Smart."""

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional


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
class TurnResult:
    """The safe result of one explicit application operation."""

    status: TurnStatus
    assistant_text: Optional[str] = None

    def __post_init__(self):
        if self.status is TurnStatus.SUCCESS and self.assistant_text is None:
            raise ValueError("A successful turn requires assistant text.")
        if self.status is not TurnStatus.SUCCESS and self.assistant_text is not None:
            raise ValueError("Only a successful turn may include assistant text.")


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
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Message text must be a non-blank string.")
        self._require_idle("submit a new message")

        return self._attempt_message(text)

    def retry_pending(self) -> TurnResult:
        """Retry the pending message once after explicit user approval."""
        if self._pending_message is None or self._unsaved_messages is not None:
            raise ConversationStateError("There is no pending message to retry.")
        return self._attempt_message(self._pending_message)

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

    def _attempt_message(self, text: str) -> TurnResult:
        api_input = self._copy_messages(self._messages)
        api_input.append({"role": "user", "content": text})

        try:
            assistant_text = self._request_model(self._copy_messages(api_input))
        except ModelRequestError:
            self._pending_message = text
            return TurnResult(TurnStatus.MODEL_REQUEST_FAILED)

        proposed_messages = self._copy_messages(self._messages)
        proposed_messages.append(
            {"role": "user", "content": text}
        )
        proposed_messages.append(
            {"role": "assistant", "content": assistant_text}
        )

        self._unsaved_messages = proposed_messages
        self._unsaved_assistant_text = assistant_text
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
