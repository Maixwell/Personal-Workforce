"""Disposable composition of the accepted durable conversation turn.

Prior SQLite messages are not read and are not added to the provider
instructions or input. No history-window, context-budget, or truncation
policy is applied. The two-sample clock probe is not a startup gate, and
this module does not initialize the production database.
"""

from dataclasses import dataclass
from pathlib import Path

from max_smart_db.config import PRODUCTION_DATABASE_PATH
from max_smart_db.local_identity import LOCAL_SERVICE_USERNAME


ACCEPTED_PROVIDER_ID = "openai"
ACCEPTED_MODEL_ID = "gpt-5.6-sol"


@dataclass(frozen=True)
class DisposableComposition:
    """Accepted service dependencies for one disposable database."""

    owner_id: int
    repository: object
    service: object
    runner_id: str


@dataclass(frozen=True)
class CompositionRefused:
    """Composition did not create a user, claim, or provider process."""

    code: str


def compose_disposable_service(
    *,
    clock,
    runner_id,
    fake,
    context_root=None,
    load_context=None,
    wall_clock=None,
    monotonic_ns=None,
    popen=None,
    stop=None,
    on_worker_start=None,
    before_popen=None,
    stderr=None,
    socket_port=None,
    ready_path=None,
):
    """Provision max-smart-local and wire one disposable service."""

    if not _disposable_database():
        return CompositionRefused("production_database")
    from max_smart_db.local_identity import (
        LocalIdentityProvisioned,
        provision_local_identity,
    )

    identity = provision_local_identity()
    if type(identity) is not LocalIdentityProvisioned:
        return identity
    return open_disposable_service(
        owner_id=identity.owner_id,
        clock=clock,
        runner_id=runner_id,
        fake=fake,
        context_root=context_root,
        load_context=load_context,
        wall_clock=wall_clock,
        monotonic_ns=monotonic_ns,
        popen=popen,
        stop=stop,
        on_worker_start=on_worker_start,
        before_popen=before_popen,
        stderr=stderr,
        socket_port=socket_port,
        ready_path=ready_path,
    )


def open_disposable_service(
    *,
    owner_id,
    clock,
    runner_id,
    fake,
    context_root=None,
    load_context=None,
    wall_clock=None,
    monotonic_ns=None,
    popen=None,
    stop=None,
    on_worker_start=None,
    before_popen=None,
    stderr=None,
    socket_port=None,
    ready_path=None,
):
    """Wire an already provisioned owner without creating another user."""

    if not _disposable_database():
        return CompositionRefused("production_database")
    if type(fake) is not str or fake == "":
        return CompositionRefused("fake_provider_required")
    if (load_context is None) == (context_root is None):
        return CompositionRefused("context_source_required")
    if type(owner_id) is not int or not _known_service_owner(owner_id):
        return CompositionRefused("owner_mismatch")
    from conversation_service import ConversationService
    from max_smart_db.creation import SqliteConversationRepository
    from provider_supervisor import build_service_provider

    if load_context is None:
        load_context = standing_context_loader(context_root)
    repository = SqliteConversationRepository(clock, runner_id)
    provider = build_service_provider(
        **_provider_arguments(
            clock,
            fake,
            wall_clock=wall_clock,
            monotonic_ns=monotonic_ns,
            popen=popen,
            stop=stop,
            on_worker_start=on_worker_start,
            before_popen=before_popen,
            stderr=stderr,
            socket_port=socket_port,
            ready_path=ready_path,
        )
    )
    return DisposableComposition(
        owner_id=owner_id,
        repository=repository,
        service=ConversationService(repository, load_context, provider),
        runner_id=runner_id,
    )


def standing_context_loader(context_root):
    """Return the accepted instructions, goals, and memory text.

    The loader never reads repository messages.
    """

    from context import build_standing_context

    root = Path(context_root)

    def load_context(
        *,
        actor_id,
        conversation_id,
        operation_id,
        request_content,
    ):
        return build_standing_context(root)

    return load_context


def turn_request(
    *,
    actor_id,
    conversation_id,
    request_content,
    idempotency_key,
    runner_id,
    expected_version,
    adopt_operation_id=None,
):
    """Build a turn with the accepted provider and model identifiers."""

    from conversation_service import TurnRequest

    return TurnRequest(
        actor_id=actor_id,
        conversation_id=conversation_id,
        request_content=request_content,
        idempotency_key=idempotency_key,
        runner_id=runner_id,
        expected_version=expected_version,
        provider_id=ACCEPTED_PROVIDER_ID,
        model_id=ACCEPTED_MODEL_ID,
        adopt_operation_id=adopt_operation_id,
    )


def _provider_arguments(clock, fake, **options):
    arguments = {
        "wall_clock": clock.now_us if options["wall_clock"] is None else options["wall_clock"],
        "fake": fake,
        "api_key": "",
    }
    for name in (
        "monotonic_ns",
        "popen",
        "stop",
        "on_worker_start",
        "before_popen",
        "stderr",
        "socket_port",
        "ready_path",
    ):
        if options[name] is not None:
            arguments[name] = options[name]
    return arguments


def _known_service_owner(owner_id):
    from django.contrib.auth.models import User

    user = User.objects.filter(pk=owner_id).first()
    return (
        user is not None
        and user.username == LOCAL_SERVICE_USERNAME
        and user.has_usable_password() is False
        and user.is_staff is False
        and user.is_superuser is False
    )


def _disposable_database():
    from django.conf import settings

    if getattr(settings, "MAX_SMART_DATABASE_KIND", None) != "test":
        return False
    configured = Path(settings.DATABASES["default"]["NAME"]).resolve()
    return configured != PRODUCTION_DATABASE_PATH.resolve()
