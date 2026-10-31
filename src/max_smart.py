"""
Max Smart V1.1 — interactive conversation with a resumable local session.

Loads Max's instructions, Adam's goals, and file-based long-term memory,
then loops: you type, Max replies. Chat turns are saved locally so a
session can be resumed after restart.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import APIError, OpenAI

from application import (
    MaxSmartApplication,
    ModelRequestError,
    SessionSaveError,
)
from context import build_standing_context
from model_client import get_model_response
from session import SessionLoadError, load_session_messages, save_session_messages
from terminal import choose_session, run_terminal


def create_model_request(client, standing_context):
    """Create the engine's provider-neutral model request callable."""
    def request_model(api_input):
        try:
            return get_model_response(client, standing_context, api_input)
        except APIError:
            raise ModelRequestError("The model request failed.") from None

    return request_model


def create_session_saver(project_root):
    """Create the engine's path-free session-save callable."""
    def save_messages(messages):
        try:
            save_session_messages(project_root, messages)
        except OSError:
            raise SessionSaveError("The conversation could not be saved.") from None

    return save_messages


def create_openai_client(api_key):
    """Create Max's client without SDK-level automatic retries."""
    return OpenAI(api_key=api_key, max_retries=0)


def main():
    """Load local configuration and run the Terminal conversation."""
    # The key stays out of the code. Path is relative to this file so
    # the program still finds .env if launched from another folder.
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit(
            "OPENAI_API_KEY is missing. Add it to the .env file in the project root."
        )

    client = create_openai_client(api_key)
    standing_context = build_standing_context(project_root)

    # Conversation history is this session's chat turns, stored by session.py.
    # It is separate from long-term memory in docs/memory.md.
    try:
        existing_messages = load_session_messages(project_root)
    except SessionLoadError as error:
        raise SystemExit(str(error)) from error

    messages = choose_session(
        existing_messages,
        lambda: save_session_messages(project_root, []),
    )
    application = MaxSmartApplication(
        create_model_request(client, standing_context),
        create_session_saver(project_root),
        messages,
    )
    run_terminal(application)


if __name__ == "__main__":
    main()
