"""Provision the explicit local service owner.

The username is the approved literal. This module does not read an
operating-system account, grant privileges, or open the production database.
Password authentication is refused by an unusable password. The approved
contract does not require a separate inactive-account flag, so is_active
remains Django's default.
"""

from dataclasses import dataclass
from pathlib import Path

from max_smart_db.application_validation import SIGNED_INT64_MAX
from max_smart_db.config import PRODUCTION_DATABASE_PATH


LOCAL_SERVICE_USERNAME = "max-smart-local"


@dataclass(frozen=True)
class LocalIdentityProvisioned:
    """The persisted Django primary key that callers pass as actor_id."""

    owner_id: int


@dataclass(frozen=True)
class LocalIdentityRefused:
    """Provisioning did not create or modify a user."""

    code: str


def provision_local_identity():
    """Create max-smart-local once and return its assigned primary key."""

    if not _disposable_database():
        return LocalIdentityRefused("production_database")
    from django.contrib.auth.models import User
    from django.db import IntegrityError, transaction

    existing = User.objects.filter(username=LOCAL_SERVICE_USERNAME).first()
    if existing is not None:
        return _refuse_existing(existing)
    user = User(
        username=LOCAL_SERVICE_USERNAME,
        email="",
        first_name="",
        last_name="",
        is_staff=False,
        is_superuser=False,
        is_active=True,
    )
    user.set_unusable_password()
    try:
        with transaction.atomic():
            user.save()
    except IntegrityError:
        raced = User.objects.filter(username=LOCAL_SERVICE_USERNAME).first()
        if raced is None:
            return LocalIdentityRefused("identity_conflict")
        return _refuse_existing(raced)
    if type(user.pk) is not int or not 1 <= user.pk <= SIGNED_INT64_MAX:
        return LocalIdentityRefused("owner_id_invalid")
    return LocalIdentityProvisioned(owner_id=user.pk)


def _refuse_existing(user):
    if _matches_service_identity(user):
        return LocalIdentityRefused("duplicate_identity")
    return LocalIdentityRefused("unexpected_identity")


def _matches_service_identity(user):
    return (
        user.username == LOCAL_SERVICE_USERNAME
        and user.has_usable_password() is False
        and user.is_staff is False
        and user.is_superuser is False
        and user.is_active is True
        and user.email == ""
        and user.first_name == ""
        and user.last_name == ""
        and user.last_login is None
        and user.groups.exists() is False
        and user.user_permissions.exists() is False
    )


def _disposable_database():
    from django.conf import settings

    if getattr(settings, "MAX_SMART_DATABASE_KIND", None) != "test":
        return False
    configured = Path(settings.DATABASES["default"]["NAME"]).resolve()
    return configured != PRODUCTION_DATABASE_PATH.resolve()
