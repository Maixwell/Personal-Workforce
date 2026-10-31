"""Tests for the Terminal-only Max Smart interface."""

import ast
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from application import TurnResult, TurnStatus  # noqa: E402
from terminal import (  # noqa: E402
    choose_session,
    read_user_message,
    run_terminal,
    run_terminal_turn,
    user_wants_retry,
    user_wants_save_retry,
)


class TerminalTests(unittest.TestCase):
    def test_normal_message_submission_and_exit(self):
        application = Mock()
        application.submit_message.return_value = TurnResult(
            TurnStatus.SUCCESS,
            "Max's reply",
        )

        with patch(
            "terminal.read_user_message",
            side_effect=["Hello", "exit"],
        ), patch("builtins.print") as printed:
            run_terminal(application)

        application.submit_message.assert_called_once_with("Hello")
        self.assertEqual(
            printed.call_args_list.count(call("Max's reply")),
            1,
        )
        self.assertIn(call("Goodbye."), printed.call_args_list)

    def test_blank_and_cancelled_input_are_skipped(self):
        application = Mock()

        with patch(
            "terminal.read_user_message",
            side_effect=[None, "   ", "exit"],
        ), patch("builtins.print"):
            run_terminal(application)

        application.submit_message.assert_not_called()

    def test_multiline_send_preserves_content(self):
        with patch(
            "builtins.input",
            side_effect=["/multi", " first ", "", "second", "/send"],
        ), patch("builtins.print"):
            result = read_user_message()

        self.assertEqual(result, " first \n\nsecond")

    def test_multiline_cancel_discards_content(self):
        with patch(
            "builtins.input",
            side_effect=["/multi", "discard this", "/cancel"],
        ), patch("builtins.print") as printed:
            result = read_user_message()

        self.assertIsNone(result)
        self.assertIn(call("Discarded."), printed.call_args_list)

    def test_single_line_commands_and_spacing_are_preserved(self):
        with patch("builtins.input", return_value="  hello  "):
            self.assertEqual(read_user_message(), "hello")
        with patch("builtins.input", return_value=" ExIt "):
            self.assertEqual(read_user_message(), "exit")
        with patch("builtins.input", return_value="   "):
            self.assertIsNone(read_user_message())

    def test_startup_without_saved_session_needs_no_prompt_or_save(self):
        start_new = Mock()

        with patch("builtins.input") as terminal_input:
            result = choose_session(None, start_new)

        self.assertEqual(result, [])
        terminal_input.assert_not_called()
        start_new.assert_not_called()

    def test_startup_resume_new_and_quit(self):
        existing = [{"role": "user", "content": "Existing"}]

        with patch("builtins.input", return_value="R"), patch("builtins.print"):
            start_new = Mock()
            self.assertIs(choose_session(existing, start_new), existing)
        start_new.assert_not_called()

        with patch("builtins.input", return_value="N"), patch("builtins.print"):
            start_new = Mock()
            self.assertEqual(choose_session(existing, start_new), [])
        start_new.assert_called_once_with()

        with patch("builtins.input", return_value="Q"), patch("builtins.print"):
            with self.assertRaisesRegex(SystemExit, "Goodbye"):
                choose_session(existing, Mock())

    def test_invalid_startup_choice_prompts_again(self):
        existing = [{"role": "user", "content": "Existing"}]

        with patch(
            "builtins.input",
            side_effect=["invalid", "R"],
        ), patch("builtins.print") as printed:
            result = choose_session(existing, Mock())

        self.assertIs(result, existing)
        self.assertIn(call("Please type R, N, or Q."), printed.call_args_list)

    def test_model_failure_retry_requires_prompt_first(self):
        events = []
        application = Mock()

        def submit(_message):
            events.append("request")
            return TurnResult(TurnStatus.MODEL_REQUEST_FAILED)

        def prompt():
            events.append("retry prompt")
            return True

        def retry():
            events.append("retry request")
            return TurnResult(TurnStatus.SUCCESS, "Recovered reply")

        application.submit_message.side_effect = submit
        application.retry_pending.side_effect = retry

        with patch(
            "terminal.user_wants_retry",
            side_effect=prompt,
        ), patch("builtins.print"):
            should_continue = run_terminal_turn(application, "Hello")

        self.assertTrue(should_continue)
        self.assertEqual(
            events,
            ["request", "retry prompt", "retry request"],
        )
        application.cancel_pending.assert_not_called()

    def test_model_failure_cancel_does_not_retry(self):
        application = Mock()
        application.submit_message.return_value = TurnResult(
            TurnStatus.MODEL_REQUEST_FAILED
        )
        application.cancel_pending.return_value = TurnResult(
            TurnStatus.CANCELLED
        )

        with patch(
            "terminal.user_wants_retry",
            return_value=False,
        ), patch("builtins.print") as printed:
            should_continue = run_terminal_turn(application, "Hello")

        self.assertTrue(should_continue)
        application.retry_pending.assert_not_called()
        application.cancel_pending.assert_called_once_with()
        self.assertIn(
            call("Cancelled. Your message was not saved."),
            printed.call_args_list,
        )

    def test_repeated_model_failure_requires_another_choice(self):
        application = Mock()
        application.submit_message.return_value = TurnResult(
            TurnStatus.MODEL_REQUEST_FAILED
        )
        application.retry_pending.side_effect = [
            TurnResult(TurnStatus.MODEL_REQUEST_FAILED),
            AssertionError("model retried without another explicit choice"),
        ]
        application.cancel_pending.return_value = TurnResult(
            TurnStatus.CANCELLED
        )

        with patch(
            "builtins.input",
            side_effect=["R", "C"],
        ), patch("builtins.print") as printed:
            should_continue = run_terminal_turn(application, "Hello")

        self.assertTrue(should_continue)
        application.submit_message.assert_called_once_with("Hello")
        application.retry_pending.assert_called_once_with()
        application.cancel_pending.assert_called_once_with()
        self.assertEqual(
            printed.call_args_list.count(
                call("The model request failed. Your message was not saved.")
            ),
            2,
        )

    def test_save_failure_retry_saves_without_model_retry(self):
        application = Mock()
        application.submit_message.return_value = TurnResult(
            TurnStatus.SAVE_FAILED
        )
        application.retry_save.return_value = TurnResult(
            TurnStatus.SUCCESS,
            "Saved reply",
        )

        with patch(
            "terminal.user_wants_save_retry",
            return_value=True,
        ), patch("builtins.print") as printed:
            should_continue = run_terminal_turn(application, "Hello")

        self.assertTrue(should_continue)
        application.retry_save.assert_called_once_with()
        application.retry_pending.assert_not_called()
        self.assertEqual(
            printed.call_args_list.count(call("Saved reply")),
            1,
        )

    def test_repeated_save_failure_can_quit_with_warning(self):
        application = Mock()
        application.submit_message.return_value = TurnResult(
            TurnStatus.SAVE_FAILED
        )
        application.retry_save.side_effect = [
            TurnResult(TurnStatus.SAVE_FAILED),
            AssertionError("save retried without another explicit choice"),
        ]

        with patch(
            "builtins.input",
            side_effect=["S", "Q"],
        ), patch("builtins.print") as printed:
            should_continue = run_terminal_turn(application, "Hello")

        self.assertFalse(should_continue)
        application.retry_save.assert_called_once_with()
        application.retry_pending.assert_not_called()
        self.assertEqual(
            printed.call_args_list.count(
                call("The response was received, but the conversation could not be saved.")
            ),
            2,
        )
        self.assertIn(
            call("Quitting. The unsaved response will be lost."),
            printed.call_args_list,
        )

    def test_immediate_quit_after_save_failure_warns_and_hides_reply(self):
        application = Mock()
        application.submit_message.return_value = TurnResult(
            TurnStatus.SAVE_FAILED
        )

        with patch(
            "terminal.user_wants_save_retry",
            return_value=False,
        ), patch("builtins.print") as printed:
            should_continue = run_terminal_turn(application, "Hello")

        self.assertFalse(should_continue)
        application.retry_save.assert_not_called()
        self.assertIn(
            call("Quitting. The unsaved response will be lost."),
            printed.call_args_list,
        )
        self.assertNotIn(call("Max Smart:"), printed.call_args_list)

    def test_run_terminal_stops_after_quitting_with_unsaved_response(self):
        application = Mock()

        with patch(
            "terminal.read_user_message",
            side_effect=["Hello", "exit"],
        ) as read_message, patch(
            "terminal.run_terminal_turn",
            return_value=False,
        ) as run_turn, patch("builtins.print"):
            run_terminal(application)

        read_message.assert_called_once_with()
        run_turn.assert_called_once_with(application, "Hello")

    def test_assistant_response_is_displayed_exactly_once(self):
        application = Mock()
        application.submit_message.return_value = TurnResult(
            TurnStatus.SUCCESS,
            "Only once",
        )

        with patch("builtins.print") as printed:
            self.assertTrue(run_terminal_turn(application, "Hello"))

        self.assertEqual(
            printed.call_args_list.count(call("Only once")),
            1,
        )

    def test_invalid_retry_choices_prompt_again(self):
        with patch(
            "builtins.input",
            side_effect=["invalid", "R"],
        ), patch("builtins.print") as printed:
            self.assertTrue(user_wants_retry())
        self.assertIn(call("Please type R or C."), printed.call_args_list)

        with patch(
            "builtins.input",
            side_effect=["invalid", "Q"],
        ), patch("builtins.print") as printed:
            self.assertFalse(user_wants_save_retry())
        self.assertIn(call("Please type S or Q."), printed.call_args_list)

    def test_programming_errors_are_not_swallowed(self):
        application = Mock()
        application.submit_message.side_effect = TypeError("programming error")

        with patch("terminal.user_wants_retry") as retry_prompt, patch(
            "terminal.user_wants_save_retry"
        ) as save_prompt, patch("builtins.print"):
            with self.assertRaisesRegex(TypeError, "programming error"):
                run_terminal_turn(application, "Hello")

        retry_prompt.assert_not_called()
        save_prompt.assert_not_called()

    def test_terminal_module_has_no_provider_storage_or_context_access(self):
        path = PROJECT_ROOT / "src" / "terminal.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_roots = set()
        referenced_names = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_roots.add(node.module.split(".")[0])
            elif isinstance(node, ast.Name):
                referenced_names.add(node.id)

        self.assertEqual(imported_roots, {"application"})
        self.assertTrue(
            {
                "openai",
                "dotenv",
                "os",
                "pathlib",
                "session",
                "context",
                "model_client",
            }.isdisjoint(imported_roots)
        )
        self.assertTrue(
            {
                "APIError",
                "OpenAI",
                "load_dotenv",
                "load_session_messages",
                "save_session_messages",
                "build_standing_context",
                "get_model_response",
                "project_root",
            }.isdisjoint(referenced_names)
        )


if __name__ == "__main__":
    unittest.main()
