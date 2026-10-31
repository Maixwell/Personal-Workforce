"""Composition tests for provider and storage failure boundaries."""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from openai import APIConnectionError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from application import (  # noqa: E402
    MaxSmartApplication,
    ModelRequestError,
    SessionSaveError,
)
from max_smart import (  # noqa: E402
    create_model_request,
    create_openai_client,
    create_session_saver,
    main,
)
from terminal import run_terminal_turn  # noqa: E402


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
        successful_response = Mock()
        successful_response.output_text = "Recovered reply"
        client.responses.create.side_effect = [
            api_connection_error(),
            successful_response,
        ]
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

    def test_main_wires_secure_dependencies_and_invokes_terminal(self):
        client = Mock()
        application = Mock()
        request_model = Mock()
        save_messages = Mock()
        existing_messages = [{"role": "user", "content": "Existing"}]
        selected_messages = [{"role": "assistant", "content": "Selected"}]

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
            return_value=existing_messages,
        ), patch(
            "max_smart.choose_session",
            return_value=selected_messages,
        ) as choose_session, patch(
            "max_smart.create_model_request",
            return_value=request_model,
        ) as create_request, patch(
            "max_smart.create_session_saver",
            return_value=save_messages,
        ) as create_saver, patch(
            "max_smart.MaxSmartApplication",
            return_value=application,
        ) as application_class, patch(
            "max_smart.run_terminal"
        ) as run_terminal, patch(
            "max_smart.save_session_messages"
        ) as save_session:
            main()

            start_new_session = choose_session.call_args.args[1]
            start_new_session()

        create_client.assert_called_once_with("sk-test-not-real")
        openai_constructor.assert_not_called()
        choose_session.assert_called_once()
        self.assertIs(
            choose_session.call_args.args[0],
            existing_messages,
        )
        save_session.assert_called_once_with(PROJECT_ROOT, [])
        create_request.assert_called_once_with(client, "standing context")
        create_saver.assert_called_once_with(PROJECT_ROOT)
        application_class.assert_called_once_with(
            request_model,
            save_messages,
            selected_messages,
        )
        run_terminal.assert_called_once_with(application)


if __name__ == "__main__":
    unittest.main()
