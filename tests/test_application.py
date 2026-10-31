"""Unit tests for the interface-independent conversation engine."""

import ast
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from application import (  # noqa: E402
    ConversationStateError,
    MaxSmartApplication,
    ModelRequestError,
    SessionSaveError,
    TurnResult,
    TurnStatus,
)


class MaxSmartApplicationTests(unittest.TestCase):
    def make_application(self, request=None, save=None, messages=None):
        return MaxSmartApplication(
            request or Mock(return_value="reply"),
            save or Mock(),
            messages or [],
        )

    def test_successful_first_submission_requests_and_saves_once(self):
        request = Mock(return_value="Max's reply")
        save = Mock()
        history = [{"role": "assistant", "content": "Earlier reply"}]
        application = self.make_application(request, save, history)

        result = application.submit_message("Hello")

        expected_input = history + [{"role": "user", "content": "Hello"}]
        expected_saved = expected_input + [
            {"role": "assistant", "content": "Max's reply"}
        ]
        request.assert_called_once_with(expected_input)
        save.assert_called_once_with(expected_saved)
        self.assertEqual(
            result,
            TurnResult(TurnStatus.SUCCESS, "Max's reply"),
        )
        self.assertEqual(application.messages, expected_saved)
        self.assertFalse(application.has_pending_message)
        self.assertFalse(application.has_unsaved_turn)

    def test_model_failure_retains_pending_message_without_saving_or_retrying(self):
        request = Mock(side_effect=ModelRequestError("secret provider detail"))
        save = Mock()
        history = [{"role": "assistant", "content": "Keep this"}]
        application = self.make_application(request, save, history)

        result = application.submit_message("Still pending")

        self.assertEqual(result, TurnResult(TurnStatus.MODEL_REQUEST_FAILED))
        self.assertIsNone(result.assistant_text)
        self.assertEqual(request.call_count, 1)
        save.assert_not_called()
        self.assertEqual(application.messages, history)
        self.assertTrue(application.has_pending_message)
        self.assertFalse(application.has_unsaved_turn)

    def test_successful_retry_uses_identical_input_and_saves_once(self):
        request = Mock(
            side_effect=[ModelRequestError("failed"), "Recovered reply"]
        )
        save = Mock()
        history = [{"role": "user", "content": "Earlier"}]
        application = self.make_application(request, save, history)

        first_result = application.submit_message("Try this")
        second_result = application.retry_pending()

        self.assertEqual(first_result.status, TurnStatus.MODEL_REQUEST_FAILED)
        self.assertEqual(
            second_result,
            TurnResult(TurnStatus.SUCCESS, "Recovered reply"),
        )
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            request.call_args_list[0].args[0],
            request.call_args_list[1].args[0],
        )
        save.assert_called_once_with(
            [
                {"role": "user", "content": "Earlier"},
                {"role": "user", "content": "Try this"},
                {"role": "assistant", "content": "Recovered reply"},
            ]
        )

    def test_repeated_model_failure_then_cancel_leaves_history_unchanged(self):
        request = Mock(
            side_effect=[
                ModelRequestError("first"),
                ModelRequestError("second"),
            ]
        )
        save = Mock()
        history = [{"role": "assistant", "content": "Keep this"}]
        application = self.make_application(request, save, history)

        self.assertEqual(
            application.submit_message("Pending").status,
            TurnStatus.MODEL_REQUEST_FAILED,
        )
        self.assertEqual(
            application.retry_pending().status,
            TurnStatus.MODEL_REQUEST_FAILED,
        )
        result = application.cancel_pending()

        self.assertEqual(result, TurnResult(TurnStatus.CANCELLED))
        self.assertEqual(request.call_count, 2)
        save.assert_not_called()
        self.assertEqual(application.messages, history)
        self.assertFalse(application.has_pending_message)

    def test_cancel_makes_no_request_or_save(self):
        request = Mock(side_effect=ModelRequestError("failed"))
        save = Mock()
        application = self.make_application(request, save)
        application.submit_message("Pending")
        request.reset_mock()

        result = application.cancel_pending()

        self.assertEqual(result.status, TurnStatus.CANCELLED)
        request.assert_not_called()
        save.assert_not_called()

    def test_wrong_state_operations_raise_without_side_effects(self):
        request = Mock(side_effect=ModelRequestError("failed"))
        save = Mock()
        idle = self.make_application(request, save)

        for operation in (
            idle.retry_pending,
            idle.cancel_pending,
            idle.retry_save,
        ):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(ConversationStateError):
                    operation()
        request.assert_not_called()
        save.assert_not_called()

        idle.submit_message("Pending")
        request.reset_mock()
        for operation in (
            lambda: idle.submit_message("Another"),
            idle.retry_save,
        ):
            with self.subTest(state="pending"):
                with self.assertRaises(ConversationStateError):
                    operation()
        request.assert_not_called()
        save.assert_not_called()
        self.assertTrue(idle.has_pending_message)

        unsaved_save = Mock(side_effect=SessionSaveError("failed"))
        unsaved = self.make_application(Mock(return_value="reply"), unsaved_save)
        unsaved.submit_message("Unsaved")
        unsaved_save.reset_mock()
        for operation in (
            lambda: unsaved.submit_message("Another"),
            unsaved.retry_pending,
            unsaved.cancel_pending,
        ):
            with self.subTest(state="unsaved"):
                with self.assertRaises(ConversationStateError):
                    operation()
        unsaved_save.assert_not_called()
        self.assertTrue(unsaved.has_unsaved_turn)

    def test_save_failure_exposes_no_reply_and_retains_unsaved_snapshot(self):
        request = Mock(return_value="private successful reply")
        save = Mock(side_effect=SessionSaveError("private path detail"))
        application = self.make_application(request, save)

        result = application.submit_message("Hello")

        self.assertEqual(result, TurnResult(TurnStatus.SAVE_FAILED))
        self.assertIsNone(result.assistant_text)
        self.assertEqual(application.messages, [])
        self.assertFalse(application.has_pending_message)
        self.assertTrue(application.has_unsaved_turn)
        request.assert_called_once()
        save.assert_called_once()

    def test_retry_save_succeeds_without_another_model_request(self):
        request = Mock(return_value="reply")
        save = Mock(side_effect=[SessionSaveError("failed"), None])
        application = self.make_application(request, save)
        application.submit_message("Hello")
        first_saved_snapshot = save.call_args.args[0]

        result = application.retry_save()

        self.assertEqual(result, TurnResult(TurnStatus.SUCCESS, "reply"))
        self.assertEqual(request.call_count, 1)
        self.assertEqual(save.call_count, 2)
        self.assertEqual(save.call_args.args[0], first_saved_snapshot)
        self.assertEqual(application.messages, first_saved_snapshot)
        self.assertFalse(application.has_unsaved_turn)

    def test_repeated_save_failure_retains_same_snapshot(self):
        request = Mock(return_value="reply")
        save = Mock(side_effect=SessionSaveError("failed"))
        application = self.make_application(request, save)
        application.submit_message("Hello")
        first_snapshot = save.call_args.args[0]

        result = application.retry_save()

        self.assertEqual(result.status, TurnStatus.SAVE_FAILED)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(save.call_count, 2)
        self.assertEqual(save.call_args.args[0], first_snapshot)
        self.assertTrue(application.has_unsaved_turn)
        self.assertEqual(application.messages, [])

    def test_save_failure_after_model_retry_cannot_retry_model_again(self):
        request = Mock(
            side_effect=[ModelRequestError("failed"), "successful reply"]
        )
        save = Mock(side_effect=SessionSaveError("save failed"))
        application = self.make_application(request, save)
        application.submit_message("Hello")

        result = application.retry_pending()

        self.assertEqual(result.status, TurnStatus.SAVE_FAILED)
        self.assertEqual(request.call_count, 2)
        self.assertFalse(application.has_pending_message)
        self.assertTrue(application.has_unsaved_turn)
        with self.assertRaises(ConversationStateError):
            application.retry_pending()
        self.assertEqual(request.call_count, 2)

    def test_unexpected_request_exception_propagates(self):
        request = Mock(side_effect=TypeError("programming error"))
        save = Mock()
        application = self.make_application(request, save)

        with self.assertRaisesRegex(TypeError, "programming error"):
            application.submit_message("Hello")

        save.assert_not_called()
        self.assertEqual(application.messages, [])
        self.assertFalse(application.has_pending_message)

    def test_unexpected_retry_exception_preserves_existing_pending_message(self):
        request = Mock(
            side_effect=[
                ModelRequestError("expected failure"),
                TypeError("programming error"),
            ]
        )
        save = Mock()
        application = self.make_application(request, save)
        application.submit_message("Hello")

        with self.assertRaisesRegex(TypeError, "programming error"):
            application.retry_pending()

        self.assertTrue(application.has_pending_message)
        self.assertEqual(application.messages, [])
        save.assert_not_called()

    def test_unexpected_save_exception_propagates_and_preserves_unsaved_turn(self):
        request = Mock(return_value="reply")
        save = Mock(side_effect=[TypeError("programming error"), None])
        application = self.make_application(request, save)

        with self.assertRaisesRegex(TypeError, "programming error"):
            application.submit_message("Hello")

        self.assertTrue(application.has_unsaved_turn)
        self.assertFalse(application.has_pending_message)
        self.assertEqual(application.messages, [])

        result = application.retry_save()
        self.assertEqual(result, TurnResult(TurnStatus.SUCCESS, "reply"))
        self.assertEqual(request.call_count, 1)

    def test_expected_exception_details_do_not_leak_in_result(self):
        secret = "Bearer sk-secret private-response-body"
        application = self.make_application(
            Mock(side_effect=ModelRequestError(secret)),
            Mock(),
        )

        result = application.submit_message("Hello")

        self.assertNotIn(secret, repr(result))
        self.assertIsNone(result.assistant_text)

    def test_external_mutation_cannot_change_history(self):
        original = [{"role": "user", "content": "Original"}]
        application = self.make_application(messages=original)

        original[0]["content"] = "Changed outside"
        original.append({"role": "assistant", "content": "Added outside"})
        returned = application.messages
        returned[0]["content"] = "Changed returned copy"
        returned.append({"role": "assistant", "content": "Also outside"})

        self.assertEqual(
            application.messages,
            [{"role": "user", "content": "Original"}],
        )

    def test_model_callback_cannot_mutate_engine_history(self):
        def mutating_request(api_input):
            api_input[0]["content"] = "mutated"
            api_input.clear()
            return "reply"

        application = self.make_application(
            mutating_request,
            Mock(),
            [{"role": "user", "content": "Original"}],
        )

        application.submit_message("New")

        self.assertEqual(
            application.messages,
            [
                {"role": "user", "content": "Original"},
                {"role": "user", "content": "New"},
                {"role": "assistant", "content": "reply"},
            ],
        )

    def test_save_callback_cannot_mutate_engine_state(self):
        def mutating_save(messages):
            messages[0]["content"] = "mutated"
            messages.clear()

        application = self.make_application(
            Mock(return_value="reply"),
            mutating_save,
        )

        result = application.submit_message("Original")

        self.assertEqual(result.status, TurnStatus.SUCCESS)
        self.assertEqual(
            application.messages,
            [
                {"role": "user", "content": "Original"},
                {"role": "assistant", "content": "reply"},
            ],
        )

    def test_blank_and_non_string_input_are_rejected_without_side_effects(self):
        request = Mock()
        save = Mock()
        application = self.make_application(request, save)

        for invalid in ("", "   \t\n", None, 123):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    application.submit_message(invalid)

        request.assert_not_called()
        save.assert_not_called()
        self.assertEqual(application.messages, [])

    def test_multiline_and_whitespace_padded_valid_input_is_preserved(self):
        request = Mock(return_value="reply")
        save = Mock()
        application = self.make_application(request, save)
        text = "  first line  \n\n\tsecond line \n"

        application.submit_message(text)

        self.assertEqual(
            request.call_args.args[0],
            [{"role": "user", "content": text}],
        )
        self.assertEqual(
            save.call_args.args[0][0],
            {"role": "user", "content": text},
        )

    def test_turn_result_rejects_inconsistent_assistant_text(self):
        with self.assertRaises(ValueError):
            TurnResult(TurnStatus.SUCCESS)
        with self.assertRaises(ValueError):
            TurnResult(TurnStatus.CANCELLED, "must not be present")

    def test_application_module_has_only_standard_library_engine_imports(self):
        path = PROJECT_ROOT / "src" / "application.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_roots = set()
        called_names = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_roots.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called_names.add(node.func.id)

        self.assertEqual(imported_roots, {"dataclasses", "enum", "typing"})
        self.assertTrue(
            {"openai", "dotenv", "os", "pathlib", "session"}.isdisjoint(
                imported_roots
            )
        )
        self.assertTrue({"input", "print"}.isdisjoint(called_names))


if __name__ == "__main__":
    unittest.main()
