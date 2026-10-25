"""Regression tests for the OpenAI Responses API request wrapper."""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_client import get_model_response  # noqa: E402


class ModelClientTests(unittest.TestCase):
    def test_exact_request_contract_and_direct_response_text(self):
        client = Mock()
        response = Mock()
        response.output_text = "  Max's reply\n"
        client.responses.create.return_value = response
        api_input = [{"role": "user", "content": "Hello"}]
        standing_context = "\n  standing context  \n"

        result = get_model_response(client, standing_context, api_input)

        client.responses.create.assert_called_once_with(
            model="gpt-5.6-sol",
            instructions="\n  standing context  \n",
            input=api_input,
            store=False,
        )
        self.assertEqual(result, "  Max's reply\n")
        self.assertIs(result, response.output_text)

    def test_client_exception_propagates_without_retry(self):
        client = Mock()
        api_error = RuntimeError("API failed")
        client.responses.create.side_effect = api_error

        with self.assertRaises(RuntimeError) as raised:
            get_model_response(client, "standing context", [])

        self.assertIs(raised.exception, api_error)
        self.assertEqual(client.responses.create.call_count, 1)


if __name__ == "__main__":
    unittest.main()
