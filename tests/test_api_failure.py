"""Composition and Terminal tests for expected application failures."""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import httpx
from openai import APIConnectionError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from application import (  # noqa: E402
    MaxSmartApplication,
    ModelRequestError,
    SessionSaveError,
    TurnResult,
    TurnStatus,
)
from max_smart import (  # noqa: E402
    choose_session,
    create_model_request,
    create_openai_client,
    create_session_saver,
    main,
    read_user_message,
    run_terminal_turn,
    user_wants_retry,
    user_wants_save_retry,
)


SECRET_DETAIL = "Bearer sk-secret-key raw-response-body standing-context"


def api_connection_error():
    """Build a realistic API error without contacting the network."""
    request = httpx.Request(
        "POST",
        "https://api.openai.com/v1/responses",
        headers={"Authorization": "Bearer sk-secret-key"},
    )
    return APIConnectionError(message=SECRET_DETAIL, request=request)


class ApiFailureTests(unittest.TestCase):
    def test_client_construction_disables_sdk_automatic_retries(self):
        client = create_openai_client("sk-test-not-real")
        try:
            self.assertEqual(client.max_retries, 0)
        finally:
            client.close()

    def test_model_request_wrapper_uses_actual_client_context_and_input(self):
        client = Mock()
        api_input = [{"role": "user", "content": "Hello"}]

        with patch(
            "max_smart.get_model_response",
            return_value="Max's reply",
        ) as get_response:
            request_model = create_model_request(client, "standing context")
            result = request_model(api_input)

        self.assertEqual(result, "Max's reply")
        get_response.assert_called_once_with(
            client,
            "standing context",
            api_input,
        )

    def test_api_error_becomes_safe_model_request_error(self):
        error = api_connection_error()

        with patch("max_smart.get_model_response", side_effect=error):
            request_model = create_model_request(Mock(), "standing context")
            with self.assertRaises(ModelRequestError) as raised:
                request_model([])

        self.assertEqual(str(raised.exception), "The model request failed.")
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn(SECRET_DETAIL, str(raised.exception))
        self.assertNotIn("sk-secret-key", str(raised.exception))

    def test_sensitive_api_error_details_are_not_printed(self):
        client = Mock()
        client.responses.create.side_effect = api_connection_error()
        application = MaxSmartApplication(
            create_model_request(client, "standing context"),
            Mock(),
            [],
        )
        printed = []
        prompts = []

        def capture_input(prompt=""):
            prompts.append(prompt)
            return "C"

        with patch("builtins.input", side_effect=capture_input), patch(
            "builtins.print",
            side_effect=lambda *args, **kwargs: printed.append(
                " ".join(str(arg) for arg in args)
            ),
        ):
            self.assertTrue(run_terminal_turn(application, "Hello"))

        displayed = "\n".join(printed + prompts)
        self.assertNotIn(SECRET_DETAIL, displayed)
        self.assertNotIn("sk-secret-key", displayed)
        self.assertNotIn("standing-context", displayed)
        self.assertNotIn("raw-response-body", displayed)

    def test_unexpected_model_error_is_not_converted(self):
        error = TypeError("programming error")

        with patch("max_smart.get_model_response", side_effect=error):
            request_model = create_model_request(Mock(), "standing context")
            with self.assertRaises(TypeError) as raised:
                request_model([])

        self.assertIs(raised.exception, error)

    def test_save_wrapper_uses_project_root_and_converts_os_error(self):
        messages = [{"role": "user", "content": "Hello"}]

        with patch(
            "max_smart.save_session_messages",
            side_effect=OSError("private path detail"),
        ) as save_session:
            save_messages = create_session_saver("/project-root")
            with self.assertRaises(SessionSaveError) as raised:
                save_messages(messages)

        save_session.assert_called_once_with("/project-root", messages)
        self.assertEqual(
            str(raised.exception),
            "The conversation could not be saved.",
        )
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn("private path detail", str(raised.exception))

    def test_unexpected_save_error_is_not_converted(self):
        error = TypeError("programming error")

        with patch("max_smart.save_session_messages", side_effect=error):
            save_messages = create_session_saver("/project-root")
            with self.assertRaises(TypeError) as raised:
                save_messages([])

        self.assertIs(raised.exception, error)

    def test_main_uses_factory_client_in_model_request_wrapper(self):
        client = Mock()
        application = Mock()
        application.submit_message.return_value = TurnResult(
            TurnStatus.SUCCESS,
            "reply",
        )
        request_model = Mock()
        save_messages = Mock()

        with patch("max_smart.load_dotenv"), patch(
            "max_smart.os.getenv",
            return_value="sk-test-not-real",
        ), patch(
            "max_smart.create_openai_client",
            return_value=client,
        ) as create_client, patch(
            "max_smart.OpenAI",
            side_effect=AssertionError("main() bypassed create_openai_client"),
        ) as openai_constructor, patch(
            "max_smart.build_standing_context",
            return_value="standing context",
        ), patch(
            "max_smart.load_session_messages",
            return_value=None,
        ), patch(
            "max_smart.create_model_request",
            return_value=request_model,
        ) as create_request, patch(
            "max_smart.create_session_saver",
            return_value=save_messages,
        ), patch(
            "max_smart.MaxSmartApplication",
            return_value=application,
        ) as application_class, patch(
            "max_smart.read_user_message",
            side_effect=["hello", "exit"],
        ), patch("builtins.print"):
            main()

        create_client.assert_called_once_with("sk-test-not-real")
        openai_constructor.assert_not_called()
        create_request.assert_called_once_with(client, "standing context")
        application_class.assert_called_once_with(
            request_model,
            save_messages,
            [],
        )
        application.submit_message.assert_called_once_with("hello")

    def test_model_retry_prompt_precedes_explicit_retry(self):
        events = []
        application = Mock()

        def submit(_message):
            events.append("request")
            return TurnResult(TurnStatus.MODEL_REQUEST_FAILED)

        def approve_retry():
            events.append("retry prompt")
            return True

        def retry():
            events.append("retry request")
            return TurnResult(TurnStatus.SUCCESS, "Recovered reply")

        application.submit_message.side_effect = submit
        application.retry_pending.side_effect = retry

        with patch(
            "max_smart.user_wants_retry",
            side_effect=approve_retry,
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
            "max_smart.user_wants_retry",
            return_value=False,
        ), patch("builtins.print") as printed:
            should_continue = run_terminal_turn(application, "Hello")

        self.assertTrue(should_continue)
        application.retry_pending.assert_not_called()
        application.cancel_pending.assert_called_once_with()
        self.assertIn(
            call("The model request failed. Your message was not saved."),
            printed.call_args_list,
        )
        self.assertIn(
            call("Cancelled. Your message was not saved."),
            printed.call_args_list,
        )

    def test_save_retry_does_not_retry_model_and_prints_reply_once(self):
        application = Mock()
        application.submit_message.return_value = TurnResult(
            TurnStatus.SAVE_FAILED
        )
        application.retry_save.return_value = TurnResult(
            TurnStatus.SUCCESS,
            "Saved reply",
        )

        with patch(
            "max_smart.user_wants_save_retry",
            return_value=True,
        ), patch("builtins.print") as printed:
            should_continue = run_terminal_turn(application, "Hello")

        self.assertTrue(should_continue)
        application.retry_save.assert_called_once_with()
        application.retry_pending.assert_not_called()
        application.cancel_pending.assert_not_called()
        self.assertEqual(
            printed.call_args_list.count(call("Saved reply")),
            1,
        )

    def test_quit_after_save_failure_warns_and_prints_no_reply(self):
        application = Mock()
        application.submit_message.return_value = TurnResult(
            TurnStatus.SAVE_FAILED
        )

        with patch(
            "max_smart.user_wants_save_retry",
            return_value=False,
        ), patch("builtins.print") as printed:
            should_continue = run_terminal_turn(application, "Hello")

        self.assertFalse(should_continue)
        application.retry_save.assert_not_called()
        application.retry_pending.assert_not_called()
        self.assertIn(
            call("Quitting. The unsaved response will be lost."),
            printed.call_args_list,
        )
        displayed = "\n".join(
            " ".join(str(arg) for arg in item.args)
            for item in printed.call_args_list
        )
        self.assertNotIn("Max Smart:", displayed)

    def test_programming_error_from_application_is_not_swallowed(self):
        application = Mock()
        application.submit_message.side_effect = TypeError("programming error")

        with patch("max_smart.user_wants_retry") as retry_prompt, patch(
            "max_smart.user_wants_save_retry"
        ) as save_prompt, patch("builtins.print"):
            with self.assertRaisesRegex(TypeError, "programming error"):
                run_terminal_turn(application, "Hello")

        retry_prompt.assert_not_called()
        save_prompt.assert_not_called()

    def test_retry_prompts_reject_invalid_choices(self):
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

    def test_multiline_send_and_cancel_are_preserved(self):
        with patch(
            "builtins.input",
            side_effect=["/multi", " first ", "", "second", "/send"],
        ), patch("builtins.print"):
            self.assertEqual(
                read_user_message(),
                " first \n\nsecond",
            )

        with patch(
            "builtins.input",
            side_effect=["/multi", "discard this", "/cancel"],
        ), patch("builtins.print") as printed:
            self.assertIsNone(read_user_message())
        self.assertIn(call("Discarded."), printed.call_args_list)

    def test_startup_resume_new_and_quit_are_preserved(self):
        existing = [{"role": "user", "content": "Existing"}]

        with patch("builtins.input", return_value="R"), patch("builtins.print"):
            self.assertIs(choose_session("/project", existing), existing)

        with patch("builtins.input", return_value="N"), patch(
            "max_smart.save_session_messages"
        ) as save_session, patch("builtins.print"):
            self.assertEqual(choose_session("/project", existing), [])
        save_session.assert_called_once_with("/project", [])

        with patch("builtins.input", return_value="Q"), patch("builtins.print"):
            with self.assertRaisesRegex(SystemExit, "Goodbye"):
                choose_session("/project", existing)


if __name__ == "__main__":
    unittest.main()
