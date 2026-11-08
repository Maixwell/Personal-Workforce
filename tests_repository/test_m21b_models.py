"""Database-free R1 model and repository-contract checks."""

import ast
import os
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "max_smart_db.settings")

import django
from django.apps import apps
from django.db import models
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ProjectState


django.setup()

from max_smart_db.repository import (  # noqa: E402
    Clock,
    ConversationRepository,
    FailureClassification,
    RepositoryIntegrityError,
)
from max_smart_db.sql_policy import scan_migration_package  # noqa: E402


_MIGRATION_FILES = (
    "__init__.py",
    "0001_v1_schema.py",
    "0002_v1_integrity_triggers.py",
    "0003_v1_seeds_and_retention.py",
)
_MODEL_NAMES = (
    "AuthorizedIdentity",
    "Role",
    "Conversation",
    "Operation",
    "Message",
    "IdempotencyKey",
    "GlobalModelSlot",
)
_REPOSITORY_METHODS = (
    "create_conversation",
    "get_conversation",
    "load_snapshot",
    "list_messages",
    "prepare",
    "adopt_queued",
    "claim_running",
    "fail_pre_provider",
    "apply_success",
    "apply_failure",
    "reconcile_late_success",
    "recover_slot",
    "acknowledge_unknown",
    "get_operation",
    "delete_expired_idempotency",
)


class RuntimeModelCorrespondenceTests(unittest.TestCase):
    def test_runtime_models_match_migration_state_without_new_operations(self):
        inventory = scan_migration_package()
        self.assertEqual(sum(len(values) for values in inventory.forward_sql.values()), 40)
        self.assertEqual(sum(len(values) for values in inventory.reverse_sql.values()), 40)

        names = tuple(sorted(
            path.name
            for path in (SRC_ROOT / "max_smart_db" / "migrations").glob("*.py")
        ))
        self.assertEqual(names, tuple(sorted(_MIGRATION_FILES)))

        loader = MigrationLoader(None, ignore_no_migrations=True)
        changes = MigrationAutodetector(
            loader.project_state(),
            ProjectState.from_apps(apps),
        ).changes(graph=loader.graph)
        self.assertNotIn("max_smart_db", changes)

        config = apps.get_app_config("max_smart_db")
        self.assertEqual(
            tuple(model.__name__ for model in config.get_models()),
            _MODEL_NAMES,
        )
        for model in config.get_models():
            self.assertTrue(model._meta.managed)
            self.assertFalse(any(
                isinstance(constraint, models.CheckConstraint)
                for constraint in model._meta.constraints
            ))

    def test_repository_contract_exposes_interfaces_without_writes(self):
        self.assertTrue(issubclass(Clock, object))
        self.assertIn("now_us", Clock.__protocol_attrs__)
        for name in _REPOSITORY_METHODS:
            self.assertIn(name, ConversationRepository.__protocol_attrs__)
        self.assertEqual(
            tuple(item.value for item in FailureClassification),
            (
                "definite_pre_provider",
                "definite_provider",
                "uncertain_provider",
                "persistence_failed",
            ),
        )
        self.assertEqual(RepositoryIntegrityError.code, "repository_integrity")

        source = (SRC_ROOT / "max_smart_db" / "repository.py").read_text(
            encoding="utf-8",
        )
        tree = ast.parse(source)
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertTrue(called.isdisjoint({
            "execute",
            "executemany",
            "executescript",
            "atomic",
            "save",
            "create",
            "update",
            "delete",
            "bulk_create",
        }))
        self.assertNotIn("openai", sys.modules)


if __name__ == "__main__":
    unittest.main()
