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
    TurnStatus,
)
from context import build_standing_context
from model_client import get_model_response
from session import SessionLoadError, load_session_messages, save_session_messages

# Conversation history is the chat turns in this session (user/assistant
# messages). It is different from long-term memory: memory is durable
# facts Adam chose to keep in docs/memory.md; history is stored in
# data/current_session.json via src/session.py.


def choose_session(project_root, existing_messages):
    """Start new, resume, or quit. None means no saved session."""
    if existing_messages is None:
        return []

    while True:
        print("A saved conversation was found.")
        choice = input("Resume (R), start new (N), or quit (Q): ").strip().upper()
        if choice == "R":
            return existing_messages
        if choice == "N":
            save_session_messages(project_root, [])
            return []
        if choice == "Q":
            raise SystemExit("Goodbye.")
        print("Please type R, N, or Q.")
        print()


def read_user_message():
    """Read one user message from the Terminal.

    A normal line is sent immediately.
    /multi starts a paste-friendly block ended by /send (or /cancel to discard).
    """
    first_line = input("You: ")
    command = first_line.strip()

    if not command:
        return None

    if command.lower() == "exit":
        return "exit"

    if command.lower() == "/multi":
        print("Multiline mode. Type /send on a line by itself to send, or /cancel to discard.")
        print()
        lines = []
        while True:
            line = input("> ")
            standalone = line.strip().lower()
            if standalone == "/send":
                return "\n".join(lines)
            if standalone == "/cancel":
                print("Discarded.")
                print()
                return None
            # Keep the line as typed, including blank lines and spacing.
            lines.append(line)

    # Single-line message: send immediately.
    return command


def user_wants_retry():
    """Ask once per failure. Only an explicit R submits the message again."""
    while True:
        choice = input("Retry this message (R) or cancel it (C): ").strip().upper()
        if choice == "R":
            return True
        if choice == "C":
            return False
        print("Please type R or C.")


def user_wants_save_retry():
    """Ask whether to retry an unsaved response or quit."""
    while True:
        choice = input("Retry saving (S) or quit (Q): ").strip().upper()
        if choice == "S":
            return True
        if choice == "Q":
            return False
        print("Please type S or Q.")


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


def run_terminal_turn(application, user_message):
    """Run one Terminal turn; return False only when the user chooses to quit."""
    result = application.submit_message(user_message)

    while True:
        if result.status is TurnStatus.MODEL_REQUEST_FAILED:
            print("The model request failed. Your message was not saved.")
            if user_wants_retry():
                result = application.retry_pending()
            else:
                result = application.cancel_pending()
            continue

        if result.status is TurnStatus.SAVE_FAILED:
            print("The response was received, but the conversation could not be saved.")
            if user_wants_save_retry():
                result = application.retry_save()
            else:
                print("Quitting. The unsaved response will be lost.")
                return False
            continue

        if result.status is TurnStatus.CANCELLED:
            print("Cancelled. Your message was not saved.")
            return True

        print()
        print("Max Smart:")
        print(result.assistant_text)
        print()
        return True


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

    try:
        existing_messages = load_session_messages(project_root)
    except SessionLoadError as error:
        raise SystemExit(str(error)) from error

    messages = choose_session(project_root, existing_messages)
    application = MaxSmartApplication(
        create_model_request(client, standing_context),
        create_session_saver(project_root),
        messages,
    )

    print("Max Smart V1.1. Type a message, type /multi for several lines, or type exit to quit.")
    print()

    while True:
        user_message = read_user_message()

        if user_message is None:
            continue

        if user_message.lower() == "exit":
            print("Goodbye.")
            break

        if not user_message.strip():
            continue

        if not run_terminal_turn(application, user_message):
            break


if __name__ == "__main__":
    main()
