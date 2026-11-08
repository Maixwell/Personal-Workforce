"""Database-free checks for the corrected repository interface."""

import ast
import sys
import unittest
from pathlib import Path
from typing import Union, get_args, get_type_hints
from uuid import UUID


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from max_smart_db.repository import (  # noqa: E402
    AlreadySucceeded,
    ConversationRepository,
    DefiniteProviderCategory,
    FailureClassification,
    MessageRole,
    MessageView,
    NotFound,
    OperationStatus,
    OperationView,
    PreProviderFailureCategory,
    ProviderFailureInput,
    ReadMessagesResult,
    StoredFailureCategory,
    UncertainProviderCategory,
)


class RepositoryContractCorrectionTests(unittest.TestCase):
    def test_operation_view_exposes_provider_start_and_schema_types(self):
        hints = get_type_hints(OperationView)
        self.assertEqual(hints["provider_started_at"], int | None)
        self.assertIs(hints["status"], OperationStatus)
        self.assertEqual(hints["failure_category"], StoredFailureCategory | None)
        self.assertEqual(
            tuple(item.value for item in OperationStatus),
            (
                "QUEUED",
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
                "OUTCOME_UNKNOWN",
                "CANCELLED",
            ),
        )

    def test_message_role_and_link_match_the_schema(self):
        hints = get_type_hints(MessageView)
        self.assertIs(hints["role"], MessageRole)
        self.assertEqual(hints["operation_id"], UUID | None)
        self.assertEqual(
            tuple(item.value for item in MessageRole),
            ("user", "assistant"),
        )

    def test_already_succeeded_carries_operation_and_both_messages(self):
        hints = get_type_hints(AlreadySucceeded)
        self.assertEqual(
            tuple(hints),
            ("operation", "user_message", "assistant_message"),
        )
        self.assertIs(hints["operation"], OperationView)
        self.assertIs(hints["user_message"], MessageView)
        self.assertIs(hints["assistant_message"], MessageView)

    def test_list_messages_preserves_actor_scoped_not_found(self):
        hints = get_type_hints(ConversationRepository.list_messages)
        self.assertIs(hints["return"], ReadMessagesResult)
        self.assertIn(NotFound, get_args(ReadMessagesResult))
        message_tuple = next(
            argument
            for argument in get_args(ReadMessagesResult)
            if argument is not NotFound
        )
        self.assertEqual(message_tuple, tuple[MessageView, ...])

    def test_failure_inputs_use_their_valid_domains(self):
        pre_provider = get_type_hints(
            ConversationRepository.fail_pre_provider,
        )["category"]
        provider = get_type_hints(
            ConversationRepository.apply_failure,
        )["failure"]
        self.assertIs(pre_provider, PreProviderFailureCategory)
        self.assertEqual(
            provider,
            Union[DefiniteProviderCategory, UncertainProviderCategory],
        )
        self.assertIs(provider, ProviderFailureInput)
        self.assertEqual(
            tuple(item.value for item in PreProviderFailureCategory),
            (
                "context_unavailable",
                "context_budget_exceeded",
                "model_configuration_invalid",
            ),
        )
        self.assertEqual(
            tuple(item.value for item in DefiniteProviderCategory),
            ("provider_definite_failure", "provider_output_invalid"),
        )
        self.assertEqual(
            tuple(item.value for item in UncertainProviderCategory),
            ("provider_outcome_uncertain",),
        )
        excluded = {
            FailureClassification.DEFINITE_PRE_PROVIDER.value,
            FailureClassification.PERSISTENCE_FAILED.value,
            StoredFailureCategory.PERSISTENCE_FAILED.value,
        }
        accepted = {
            item.value
            for item in (
                *PreProviderFailureCategory,
                *DefiniteProviderCategory,
                *UncertainProviderCategory,
            )
        }
        self.assertTrue(excluded.isdisjoint(
            {item.value for item in PreProviderFailureCategory}
            | {item.value for item in DefiniteProviderCategory}
            | {item.value for item in UncertainProviderCategory}
        ))
        self.assertIn("context_unavailable", accepted)
        self.assertNotIn("persistence_failed", {
            item.value for item in PreProviderFailureCategory
        } | {
            item.value for item in DefiniteProviderCategory
        } | {
            item.value for item in UncertainProviderCategory
        })

    def test_contract_module_still_has_no_repository_writes(self):
        source = (
            PROJECT_ROOT / "src" / "max_smart_db" / "repository.py"
        ).read_text(encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
