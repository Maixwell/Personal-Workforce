"""One disposable Terminal turn through the accepted conversation service.

The live JSON Terminal is unchanged. This module does not open the production
database, provider fence, trust pin, or current session, and it does not call
the provider except through the accepted disposable composition.
"""

from dataclasses import dataclass
from uuid import UUID

from conversation_service import DurableTurnCompleted
from durable_composition import (
    DisposableComposition,
    compose_disposable_service,
    open_disposable_service,
    turn_request,
)
from max_smart_db.application_validation import SIGNED_INT64_MAX
from max_smart_db.creation import CreationInputError, _read_clock_once
from max_smart_db.idempotency import IdempotencyKeyError, idempotency_key_digest
from max_smart_db.prepare import PrepareInputError, _validate_runner
from max_smart_db.repository import (
    ConversationView,
    MessageRole,
    MessageView,
    RepositoryIntegrityError,
)


_ROLE_SLUG = "max-smart"


@dataclass(frozen=True)
class DisposableTerminalTurnCompleted:
    """One durable turn completed by the accepted conversation service."""

    assistant_text: str
    conversation_id: UUID
    next_expected_version: int
    operation_id: UUID
    owner_id: int


@dataclass(frozen=True)
class DisposableTerminalTurnRefused:
    """The turn did not complete. No assistant text is included."""

    code: str
    outcome: object = None


def submit_disposable_terminal_turn(
    *,
    clock,
    runner_id,
    fake,
    context_root,
    request_content,
    idempotency_key,
    expected_version,
    conversation_id=None,
    owner_id=None,
    popen=None,
    on_worker_start=None,
):
    """Submit one user message through the accepted disposable service.

    The first call provisions the local owner with compose_disposable_service
    and creates one conversation. A call that names an existing conversation
    uses open_disposable_service for that same owner, because the accepted
    identity provision refuses a second compose. The caller supplies the
    idempotency key and expected version unchanged.
    """

    if type(request_content) is not str or request_content.strip() == "":
        return DisposableTerminalTurnRefused("blank_message")
    if type(fake) is not str or fake == "":
        return DisposableTerminalTurnRefused("fake_provider_required")
    if type(expected_version) is not int:
        return DisposableTerminalTurnRefused("expected_version_type")
    if expected_version < 0 or expected_version > SIGNED_INT64_MAX:
        return DisposableTerminalTurnRefused("expected_version_range")
    if conversation_id is not None and type(conversation_id) is not UUID:
        return DisposableTerminalTurnRefused("conversation_id_type")
    if owner_id is not None and type(owner_id) is not int:
        return DisposableTerminalTurnRefused("owner_id_type")
    refused = _preflight_turn_inputs(clock, runner_id, idempotency_key)
    if refused is not None:
        return refused

    options = {}
    if popen is not None:
        options["popen"] = popen
    if on_worker_start is not None:
        options["on_worker_start"] = on_worker_start

    if conversation_id is None:
        composed = compose_disposable_service(
            clock=clock,
            runner_id=runner_id,
            fake=fake,
            context_root=context_root,
            **options,
        )
    else:
        if owner_id is None:
            return DisposableTerminalTurnRefused("owner_required")
        composed = open_disposable_service(
            owner_id=owner_id,
            clock=clock,
            runner_id=runner_id,
            fake=fake,
            context_root=context_root,
            **options,
        )
    if type(composed) is not DisposableComposition:
        return DisposableTerminalTurnRefused(_refusal_code(composed), composed)

    if conversation_id is None:
        try:
            created = composed.repository.create_conversation(
                composed.owner_id,
                _ROLE_SLUG,
                None,
            )
        except (CreationInputError, RepositoryIntegrityError) as error:
            return _refused_exception(error)
        if type(created) is not ConversationView:
            return DisposableTerminalTurnRefused(_refusal_code(created), created)
        conversation_id = created.conversation_id

    try:
        outcome = composed.service.submit(
            turn_request(
                actor_id=composed.owner_id,
                conversation_id=conversation_id,
                request_content=request_content,
                idempotency_key=idempotency_key,
                runner_id=composed.runner_id,
                expected_version=expected_version,
            )
        )
    except (
        IdempotencyKeyError,
        CreationInputError,
        PrepareInputError,
        RepositoryIntegrityError,
    ) as error:
        return _refused_exception(error)
    if type(outcome) is not DurableTurnCompleted:
        return DisposableTerminalTurnRefused(type(outcome).__name__, outcome)

    message = outcome.assistant_message
    if (
        type(message) is not MessageView
        or message.role is not MessageRole.ASSISTANT
        or type(message.content) is not str
        or message.content.strip() == ""
        or message.conversation_id != conversation_id
    ):
        return DisposableTerminalTurnRefused("assistant_text_unproven", outcome)

    try:
        viewed = composed.repository.get_conversation(
            composed.owner_id,
            conversation_id,
        )
    except RepositoryIntegrityError as error:
        return _refused_exception(error)
    if (
        type(viewed) is not ConversationView
        or type(viewed.version) is not int
        or viewed.conversation_id != conversation_id
    ):
        return DisposableTerminalTurnRefused("version_unproven", outcome)
    return DisposableTerminalTurnCompleted(
        assistant_text=message.content,
        conversation_id=conversation_id,
        next_expected_version=viewed.version,
        operation_id=outcome.operation_id,
        owner_id=composed.owner_id,
    )


def _preflight_turn_inputs(clock, runner_id, idempotency_key):
    """Reject invalid turn inputs before composition or provider execution."""

    try:
        idempotency_key_digest(idempotency_key)
        _validate_runner(runner_id)
        _read_clock_once(clock)
    except (IdempotencyKeyError, CreationInputError) as error:
        return _refused_exception(error)
    return None


def _refused_exception(error):
    """Return one refusal for a documented input or repository failure."""

    return DisposableTerminalTurnRefused(_refusal_code(error), error)


def _refusal_code(outcome):
    code = getattr(outcome, "code", None)
    if type(code) is str and code != "":
        return code
    return type(outcome).__name__
