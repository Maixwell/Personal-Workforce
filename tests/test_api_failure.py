"""Application-level handling of expected OpenAI API failures."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from openai import APIConnectionError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from max_smart import create_openai_client, main, send_pending_message  # noqa: E402
from session import load_session_messages, save_session_messages  # noqa: E402


SECRET_DETAIL = "Bearer sk-secret-key raw-response-body standing-context"


def api_connection_error():
    """Build a realistic API error without contacting the network."""
    request = httpx.Request(
        "POST",
        "https://api.openai.com/v1/responses",
        headers={"Authorization": "Bearer sk-secret-key"},
    )
    return APIConnectionError(message=SECRET_DETAIL, request=request)


def successful_response(text):
    response = Mock()
    response.output_text = text
    return response


class ApiFailureTests(unittest.TestCase):
    def test_client_construction_disables_sdk_automatic_retries(self):
        client = create_openai_client("sk-test-not-real")
        try:
            self.assertEqual(client.max_retries, 0)
        finally:
            client.close()

    def test_main_uses_retry_free_client_factory(self):
        client = Mock()

        with patch("max_smart.load_dotenv") as load_dotenv, patch(
            "max_smart.os.getenv", return_value="sk-test-not-real"
        ), patch(
            "max_smart.create_openai_client", return_value=client
        ) as create_client, patch(
            "max_smart.OpenAI", side_effect=AssertionError("main() bypassed create_openai_client")
        ) as openai_constructor, patch(
            "max_smart.build_standing_context", return_value="standing context"
        ), patch(
            "max_smart.load_session_messages", return_value=None
        ), patch(
            "max_smart.read_user_message", side_effect=["hello", "exit"]
        ), patch(
            "max_smart.send_pending_message", return_value=[]
        ) as send_message, patch("builtins.print"):
            main()

        load_dotenv.assert_called_once()
        create_client.assert_called_once_with("sk-test-not-real")
        openai_constructor.assert_not_called()
        self.assertIs(send_message.call_args.args[0], client)

    def test_api_error_is_handled_and_keeps_message_for_explicit_retry(self):
        client = Mock()
        error = api_connection_error()
        responses = iter([
            error,
            successful_response("Recovered reply"),
        ])
        history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "earlier reply"}]
        pending = "please try this again"
        expected_input = history + [{"role": "user", "content": pending}]
        printed = []
        events = []

        def make_request(*args, **kwargs):
            events.append("request")
            result = next(responses)
            if isinstance(result, Exception):
                raise result
            return result

        def approve_retry(prompt):
            events.append("retry prompt")
            return "R"

        client.responses.create.side_effect = make_request

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("builtins.input", side_effect=approve_retry), patch(
                "builtins.print",
                side_effect=lambda *args, **kwargs: printed.append(
                    " ".join(str(arg) for arg in args)
                ),
            ):
                send_pending_message(
                    client,
                    "standing context",
                    temp_dir,
                    history,
                    pending,
                )

            self.assertNotIn("Traceback", "\n".join(printed))
            self.assertIn(
                "The model request failed. Your message was not saved.",
                printed,
            )
            calls = client.responses.create.call_args_list
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0].kwargs["input"], expected_input)
            self.assertEqual(calls[1].kwargs["input"], expected_input)
            self.assertEqual(events, ["request", "retry prompt", "request"])

    def test_successful_retry_saves_one_turn_without_duplicates(self):
        client = Mock()
        client.responses.create.side_effect = [
            api_connection_error(),
            successful_response("Recovered reply"),
        ]
        history = [{"role": "assistant", "content": "already saved"}]
        pending = "same pending message"

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("builtins.input", return_value="R"), patch(
                "builtins.print"
            ), patch(
                "max_smart.save_session_messages",
                wraps=save_session_messages,
            ) as save_session:
                send_pending_message(
                    client,
                    "standing context",
                    temp_dir,
                    history,
                    pending,
                )

            saved = load_session_messages(temp_dir)

        save_session.assert_called_once_with(temp_dir, saved)
        self.assertEqual(client.responses.create.call_count, 2)
        self.assertEqual(
            saved,
            [
                {"role": "assistant", "content": "already saved"},
                {"role": "user", "content": pending},
                {"role": "assistant", "content": "Recovered reply"},
            ],
        )
        self.assertEqual(history, saved)

    def test_cancel_does_not_record_failed_request(self):
        client = Mock()
        client.responses.create.side_effect = api_connection_error()
        history = [{"role": "user", "content": "keep this"}]
        original = [dict(message) for message in history]

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("builtins.input", side_effect=["nope", "C"]), patch(
                "builtins.print"
            ):
                returned = send_pending_message(
                    client,
                    "standing context",
                    temp_dir,
                    history,
                    "do not save this",
                )

            self.assertIsNone(load_session_messages(temp_dir))
            self.assertFalse((Path(temp_dir) / "data").exists())

        self.assertEqual(client.responses.create.call_count, 1)
        self.assertEqual(returned, original)
        self.assertEqual(history, original)

    def test_second_failure_then_cancel_does_not_append_or_save(self):
        client = Mock()
        client.responses.create.side_effect = [
            api_connection_error(),
            api_connection_error(),
        ]
        history = [{"role": "assistant", "content": "keep this"}]
        original = [dict(message) for message in history]

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("builtins.input", side_effect=["R", "C"]) as retry_prompt, patch(
                "builtins.print"
            ), patch("max_smart.save_session_messages") as save_session:
                returned = send_pending_message(
                    client,
                    "standing context",
                    temp_dir,
                    history,
                    "still pending",
                )

            self.assertFalse((Path(temp_dir) / "data").exists())

        self.assertEqual(client.responses.create.call_count, 2)
        self.assertEqual(retry_prompt.call_count, 2)
        save_session.assert_not_called()
        self.assertEqual(returned, original)
        self.assertEqual(history, original)

    def test_sensitive_exception_details_are_not_printed(self):
        client = Mock()
        error = api_connection_error()
        client.responses.create.side_effect = error
        printed = []
        prompts = []

        def capture_input(prompt=""):
            prompts.append(prompt)
            return "C"

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("builtins.input", side_effect=capture_input), patch(
                "builtins.print",
                side_effect=lambda *args, **kwargs: printed.append(
                    " ".join(str(arg) for arg in args)
                ),
            ):
                send_pending_message(client, "standing context", temp_dir, [], "hello")

        displayed = "\n".join(printed + prompts)
        self.assertIn(SECRET_DETAIL, str(error))
        self.assertNotIn(SECRET_DETAIL, displayed)
        self.assertNotIn("sk-secret-key", displayed)
        self.assertNotIn("standing-context", displayed)
        self.assertNotIn("raw-response-body", displayed)

    def test_programming_error_is_not_swallowed(self):
        client = Mock()
        client.responses.create.side_effect = TypeError("programming bug sk-secret-key")
        history = [{"role": "user", "content": "keep this"}]

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("builtins.input") as retry_prompt, patch("builtins.print"):
                with self.assertRaises(TypeError):
                    send_pending_message(
                        client,
                        "standing context",
                        temp_dir,
                        history,
                        "this should not be saved",
                    )

            self.assertIsNone(load_session_messages(temp_dir))

        retry_prompt.assert_not_called()
        self.assertEqual(client.responses.create.call_count, 1)
        self.assertEqual(history, [{"role": "user", "content": "keep this"}])


if __name__ == "__main__":
    unittest.main()
