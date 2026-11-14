"""Disposable-database tests for the explicit local service identity."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
PRODUCTION_DATABASE = PROJECT_ROOT / "data" / "max_smart.sqlite3"
SESSION_FILE = PROJECT_ROOT / "data" / "current_session.json"
IDENTITY_SOURCE = SRC_ROOT / "max_smart_db" / "local_identity.py"
SERVICE_SOURCE = SRC_ROOT / "conversation_service.py"
TERMINAL_SOURCE = SRC_ROOT / "terminal.py"

_PREAMBLE = """\
import base64
import json
import os
import uuid
from pathlib import Path

database = Path(os.environ["MAX_SMART_TEST_DATABASE_PATH"])
if database.name == "max_smart.sqlite3":
    raise SystemExit("production database requested")
from max_smart_db.safety import initialize_sqlite_file
initialize_sqlite_file(database)

import django
django.setup()
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from max_smart_db.creation import SqliteConversationRepository
from max_smart_db.local_identity import provision_local_identity
from max_smart_db.models import Conversation, Operation
from max_smart_db.repository import PrepareCommand

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)


class Clock:
    def now_us(self):
        return 1_000_000


def key_for(value):
    return base64.urlsafe_b64encode(bytes([value]) * 16).rstrip(b"=").decode()


def identity_snapshot(user):
    user.refresh_from_db()
    return {
        "id": user.pk,
        "username": user.username,
        "password": user.password,
        "staff": user.is_staff,
        "superuser": user.is_superuser,
        "active": user.is_active,
        "email": user.email,
        "first": user.first_name,
        "last": user.last_name,
        "login": user.last_login,
        "groups": list(user.groups.values_list("pk", flat=True)),
        "permissions": list(user.user_permissions.values_list("pk", flat=True)),
    }
"""


def _run(body, *, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "m4s3-identity.sqlite3"
            ),
            "PYTHONPATH": str(SRC_ROOT),
            "USER": "os-account",
            "LOGNAME": "os-account",
            "USERNAME": "os-account",
        }
        return subprocess.run(
            (
                sys.executable,
                "-B",
                "-c",
                _PREAMBLE + "\n" + textwrap.dedent(body),
            ),
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )


def _payload(testcase, result):
    testcase.assertEqual(
        result.returncode,
        0,
        result.stdout + "\n" + result.stderr,
    )
    return json.loads(result.stdout)


class LocalIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session_stat = SESSION_FILE.stat()

    @classmethod
    def tearDownClass(cls):
        current = SESSION_FILE.stat()
        if (
            current.st_ino != cls.session_stat.st_ino
            or current.st_size != cls.session_stat.st_size
            or current.st_mtime_ns != cls.session_stat.st_mtime_ns
        ):
            raise AssertionError("terminal session file changed")
        if PRODUCTION_DATABASE.exists():
            raise AssertionError("production database created")

    def test_source_does_not_read_an_operating_system_account(self):
        source = IDENTITY_SOURCE.read_text()
        for banned in (
            "getpass",
            "getlogin",
            "getpwuid",
            "hostname",
            "os.environ",
            "getenv",
            "conversation_service",
            "terminal",
            "initialize_sqlite_file",
        ):
            self.assertNotIn(banned, source)
        self.assertLess(
            source.index("set_unusable_password()"),
            source.index("user.save()"),
        )
        self.assertNotIn("local_identity", SERVICE_SOURCE.read_text())
        self.assertNotIn("local_identity", TERMINAL_SOURCE.read_text())

    def test_creates_unusable_service_identity_with_assigned_key(self):
        payload = _payload(self, _run(
            """\
            other = User.objects.create(username="not-the-service")
            result = provision_local_identity()
            user = User.objects.get(username="max-smart-local")
            print(json.dumps({
                "type": type(result).__name__,
                "owner_type": type(result.owner_id).__name__,
                "owner_id": result.owner_id,
                "stored_id": user.pk,
                "username": user.username,
                "usable": user.has_usable_password(),
                "password_rejected": user.check_password("local-password"),
                "staff": user.is_staff,
                "superuser": user.is_superuser,
                "active": user.is_active,
                "groups": user.groups.count(),
                "permissions": user.user_permissions.count(),
                "other_id": other.pk,
                "names": sorted(User.objects.values_list("username", flat=True)),
            }))
            """
        ))
        self.assertEqual(payload["type"], "LocalIdentityProvisioned")
        self.assertEqual(payload["owner_type"], "int")
        self.assertEqual(payload["owner_id"], payload["stored_id"])
        self.assertNotEqual(payload["owner_id"], 1)
        self.assertNotEqual(payload["owner_id"], payload["other_id"])
        self.assertEqual(payload["username"], "max-smart-local")
        self.assertFalse(payload["usable"])
        self.assertFalse(payload["password_rejected"])
        self.assertFalse(payload["staff"])
        self.assertFalse(payload["superuser"])
        self.assertTrue(payload["active"])
        self.assertEqual(payload["groups"], 0)
        self.assertEqual(payload["permissions"], 0)
        self.assertEqual(
            payload["names"],
            ["max-smart-local", "not-the-service"],
        )

    def test_duplicate_provisioning_does_not_change_the_user(self):
        payload = _payload(self, _run(
            """\
            first = provision_local_identity()
            user = User.objects.get(pk=first.owner_id)
            before = identity_snapshot(user)
            second = provision_local_identity()
            print(json.dumps({
                "code": second.code,
                "unchanged": identity_snapshot(user) == before,
                "count": User.objects.filter(username="max-smart-local").count(),
                "owner_id": first.owner_id,
            }))
            """
        ))
        self.assertEqual(payload["code"], "duplicate_identity")
        self.assertTrue(payload["unchanged"])
        self.assertEqual(payload["count"], 1)

    def test_usable_password_fails_closed(self):
        payload = _payload(self, _run(
            """\
            user = User(username="max-smart-local", is_staff=False, is_superuser=False)
            user.set_password("disposable-test-secret")
            user.save()
            before = identity_snapshot(user)
            result = provision_local_identity()
            print(json.dumps({
                "code": result.code,
                "unchanged": identity_snapshot(user) == before,
                "usable": user.has_usable_password(),
                "count": User.objects.count(),
            }))
            """
        ))
        self.assertEqual(payload["code"], "unexpected_identity")
        self.assertTrue(payload["unchanged"])
        self.assertTrue(payload["usable"])
        self.assertEqual(payload["count"], 1)

    def test_staff_or_superuser_fails_closed(self):
        payload = _payload(self, _run(
            """\
            staff = User(username="max-smart-local", is_staff=True, is_superuser=False)
            staff.set_unusable_password()
            staff.save()
            before = identity_snapshot(staff)
            refused = provision_local_identity()
            staff_unchanged = identity_snapshot(staff) == before
            staff.delete()
            superuser = User(
                username="max-smart-local",
                is_staff=False,
                is_superuser=True,
            )
            superuser.set_unusable_password()
            superuser.save()
            before = identity_snapshot(superuser)
            refused_superuser = provision_local_identity()
            print(json.dumps({
                "staff_code": refused.code,
                "staff_unchanged": staff_unchanged,
                "superuser_code": refused_superuser.code,
                "superuser_unchanged": identity_snapshot(superuser) == before,
                "count": User.objects.filter(username="max-smart-local").count(),
            }))
            """
        ))
        self.assertEqual(payload["staff_code"], "unexpected_identity")
        self.assertTrue(payload["staff_unchanged"])
        self.assertEqual(payload["superuser_code"], "unexpected_identity")
        self.assertTrue(payload["superuser_unchanged"])
        self.assertEqual(payload["count"], 1)

    def test_existing_permission_fails_closed(self):
        payload = _payload(self, _run(
            """\
            user = User(username="max-smart-local", is_staff=False, is_superuser=False)
            user.set_unusable_password()
            user.save()
            permission = Permission.objects.create(
                codename="unapproved",
                name="unapproved",
                content_type=ContentType.objects.get_for_model(User),
            )
            user.user_permissions.add(permission)
            before = identity_snapshot(user)
            result = provision_local_identity()
            print(json.dumps({
                "code": result.code,
                "unchanged": identity_snapshot(user) == before,
                "permissions": user.user_permissions.count(),
            }))
            """
        ))
        self.assertEqual(payload["code"], "unexpected_identity")
        self.assertTrue(payload["unchanged"])
        self.assertEqual(payload["permissions"], 1)

    def test_owner_id_is_an_explicit_repository_actor(self):
        payload = _payload(self, _run(
            """\
            result = provision_local_identity()
            conversation_id = uuid.UUID("00112233445566778899aabbccddeeff")
            Conversation.objects.create(
                id=conversation_id,
                owner_id=result.owner_id,
                role_id="max-smart",
                version=0,
                next_sequence=1,
                title=None,
                created_at_us=0,
                updated_at_us=0,
            )
            prepared = SqliteConversationRepository(
                Clock(),
                "AAAAAAAAAAAAAAAAAAAAAA",
            ).prepare(PrepareCommand(
                actor_id=result.owner_id,
                conversation_id=conversation_id,
                expected_version=0,
                request_content="hello",
                idempotency_key=key_for(1),
                provider_id="openai",
                model_id="gpt-5.6-sol",
            ))
            operation = Operation.objects.get(pk=prepared.reservation.operation_id)
            print(json.dumps({
                "type": type(prepared).__name__,
                "actor_id": prepared.reservation.actor_id,
                "owner_id": operation.owner_id,
                "returned": result.owner_id,
            }))
            """
        ))
        self.assertEqual(payload["type"], "Prepared")
        self.assertEqual(payload["actor_id"], payload["returned"])
        self.assertEqual(payload["owner_id"], payload["returned"])

    def test_production_settings_do_not_create_a_user_or_database(self):
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.settings",
            "PYTHONPATH": str(SRC_ROOT),
        }
        environment.pop("MAX_SMART_TEST_DATABASE_PATH", None)
        result = subprocess.run(
            (
                sys.executable,
                "-B",
                "-c",
                textwrap.dedent(
                    """\
                    import django
                    django.setup()
                    from max_smart_db.local_identity import provision_local_identity
                    refused = provision_local_identity()
                    print(refused.code)
                    """
                ),
            ),
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
        self.assertEqual(result.stdout.strip(), "production_database")
        self.assertFalse(PRODUCTION_DATABASE.exists())


if __name__ == "__main__":
    unittest.main()
