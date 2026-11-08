"""Create one conversation. This module does not implement later transitions."""

import uuid

from django.contrib.auth.models import User
from django.db import IntegrityError, OperationalError, connection, transaction

from .application_validation import (
    SIGNED_INT64_MAX,
    require_explicit_positive_integer_id,
)
from .models import Role
from .reads import SqliteConversationReader
from .repository import (
    CommitAmbiguous,
    ConversationConflict,
    ConversationView,
    DatabaseBusy,
    RepositoryIntegrityError,
)


class CreationInputError(ValueError):
    """Owner, role, title, or clock input cannot be persisted."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class SqliteConversationRepository(SqliteConversationReader):
    """Owner-scoped reads plus conversation creation."""

    def prepare(self, command):
        """Prepare one QUEUED operation without crossing the provider boundary."""
        from .prepare import prepare

        return prepare(self, command)

    def adopt_queued(self, actor_id, operation_id):
        """Transfer or expire one actor-owned QUEUED reservation."""
        from .adoption import adopt_queued

        return adopt_queued(self, actor_id, operation_id)

    def claim_running(self, actor_id, operation_id):
        """Claim one owned QUEUED reservation for provider execution."""
        from .claim import claim_running

        return claim_running(self, actor_id, operation_id)

    def fail_pre_provider(self, reservation, category):
        """Record one definite pre-provider failure."""
        from .pre_provider import fail_pre_provider

        return fail_pre_provider(self, reservation, category)

    def apply_failure(self, claimed, failure):
        """Record one definite or uncertain provider outcome."""
        from .provider_failure import apply_failure

        return apply_failure(self, claimed, failure)

    def recover_slot(self):
        """Classify or recover the singleton global slot."""
        from .recovery import recover_slot

        return recover_slot(self.clock)

    def create_conversation(self, actor_id, role_slug, title):
        """Insert one conversation and return its durable view."""
        require_explicit_positive_integer_id(actor_id)
        _require_role_slug(role_slug)
        _require_title(title)
        conversation_id = uuid.uuid4()
        finished = False
        try:
            with transaction.atomic(durable=True):
                created_at = _read_clock_once(self.clock)
                if not User.objects.filter(pk=actor_id).exists():
                    raise CreationInputError("owner_invalid")
                if not Role.objects.filter(slug=role_slug, active=1).exists():
                    raise CreationInputError("role_invalid")
                _insert_conversation(
                    conversation_id,
                    actor_id,
                    role_slug,
                    title,
                    created_at,
                )
                view = ConversationView(
                    conversation_id=conversation_id,
                    actor_id=actor_id,
                    role_slug=role_slug,
                    version=0,
                    next_sequence=1,
                    title=title,
                )
                finished = True
        except CreationInputError:
            raise
        except OperationalError as error:
            if finished:
                return CommitAmbiguous(conversation_id=conversation_id)
            if _is_lock_failure(error):
                return DatabaseBusy()
            raise
        except IntegrityError as error:
            if finished:
                return CommitAmbiguous(conversation_id=conversation_id)
            if _is_uniqueness_conflict(error):
                return ConversationConflict()
            raise RepositoryIntegrityError() from None
        except Exception:
            if finished:
                return CommitAmbiguous(conversation_id=conversation_id)
            raise
        return view


def _require_role_slug(value):
    if type(value) is not str:
        raise CreationInputError("role_type")


def _require_title(value):
    if value is not None and type(value) is not str:
        raise CreationInputError("title_type")


def _read_clock_once(clock):
    value = clock.now_us()
    if type(value) is not int or value < 0 or value > SIGNED_INT64_MAX:
        raise CreationInputError("clock_invalid")
    return value


def _insert_conversation(conversation_id, actor_id, role_slug, title, created_at):
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO conversation "
            "(id, owner_id, role_slug, version, next_sequence, title, "
            "created_at_us, updated_at_us, archived_at_us) "
            "VALUES (%s, %s, %s, 0, 1, %s, %s, %s, NULL)",
            (
                conversation_id.hex,
                actor_id,
                role_slug,
                title,
                created_at,
                created_at,
            ),
        )
        if cursor.rowcount != 1:
            raise RepositoryIntegrityError()


def _is_lock_failure(error):
    text = str(error).lower()
    return "database is locked" in text or "database is busy" in text


def _is_uniqueness_conflict(error):
    return "unique constraint failed" in str(error).lower()
