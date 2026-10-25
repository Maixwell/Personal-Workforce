"""
Max Smart V1.1 — interactive conversation with a resumable local session.

Loads Max's instructions, Adam's goals, and file-based long-term memory,
then loops: you type, Max replies. Chat turns are saved locally so a
session can be resumed after restart.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from context import build_standing_context
from session import SessionLoadError, load_session_messages, save_session_messages

# --- 1. Load the secret API key from the project .env file ---
# The key stays out of the code. Path is relative to this file so
# the program still finds .env if you run it from another folder.
project_root = Path(__file__).resolve().parent.parent
load_dotenv(project_root / ".env")

api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise SystemExit(
        "OPENAI_API_KEY is missing. Add it to the .env file in the project root."
    )

# --- 2. Create an OpenAI client that will talk to the API ---
client = OpenAI(api_key=api_key)

# --- 3–5. Standing context (instructions, goals, read-only memory) ---
standing_context = build_standing_context(project_root)

# Conversation history is the chat turns in this session (user/assistant
# messages). It is different from long-term memory: memory is durable
# facts Adam chose to keep in docs/memory.md; history is stored in
# data/current_session.json via src/session.py.


def choose_session(existing_messages):
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


# --- 6. Conversation loop ---
try:
    existing_messages = load_session_messages(project_root)
except SessionLoadError as error:
    raise SystemExit(str(error)) from error

messages = choose_session(existing_messages)

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

    # Send the full local history plus this turn. The model does not see
    # earlier turns unless we include them here (store=False, no response ids).
    api_input = messages + [{"role": "user", "content": user_message}]

    response = client.responses.create(
        model="gpt-5.6-sol",
        instructions=standing_context,
        input=api_input,
        store=False,
    )

    assistant_text = response.output_text

    messages.append({"role": "user", "content": user_message})
    messages.append({"role": "assistant", "content": assistant_text})
    save_session_messages(project_root, messages)

    print()
    print("Max Smart:")
    print(assistant_text)
    print()
