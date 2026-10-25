"""
Max Smart V1 — interactive conversation in one Terminal session.

Loads Max's instructions, Adam's goals, and file-based long-term memory,
then loops: you type, Max replies.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

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

# --- 3. Load Max's standing instructions ---
# These come from docs/max-smart-instructions.md (role, challenge, V1 limits).
instructions_path = project_root / "docs" / "max-smart-instructions.md"
max_instructions = instructions_path.read_text(encoding="utf-8")

# --- 4. Load Adam's goals ---
# These come from docs/goals.md (priorities, preferences, boundaries).
goals_path = project_root / "docs" / "goals.md"
adam_goals = goals_path.read_text(encoding="utf-8")

# --- 5. Load Max's long-term memory ---
# Memory is currently:
# - file-based (docs/memory.md, ordinary markdown Adam can open)
# - transparent (nothing hidden in a database)
# - manually controlled by Adam (he edits the file; this program never writes it)
# - read-only from Max's perspective (Max may read it as context, not change it)
memory_path = project_root / "docs" / "memory.md"
long_term_memory = memory_path.read_text(encoding="utf-8")

# Combine the three sources into the Responses API "instructions" field.
standing_context = (
    "=== Max Smart's standing instructions ===\n\n"
    + max_instructions
    + "\n\n---\n\n"
    + "=== Adam's goals ===\n\n"
    + adam_goals
    + "\n\n---\n\n"
    + "=== Max Smart's long-term memory ===\n\n"
    + "This memory is file-based, transparent, and edited only by Adam. "
    + "You may use it as context. You must not modify docs/memory.md "
    + "or treat yourself as able to write memory.\n\n"
    + long_term_memory
)

# --- 6. Conversation loop ---
# previous_response_id links this turn to earlier turns in THIS run only.
# Chat history is not saved to disk. Closing the program forgets the chat.
# Long-term facts live only in docs/memory.md, which Adam edits by hand.
previous_response_id = None

print("Max Smart V1. Type a message, type /multi for several lines, or type exit to quit.")
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


while True:
    user_message = read_user_message()

    if user_message is None:
        continue

    if user_message.lower() == "exit":
        print("Goodbye.")
        break

    if not user_message.strip():
        continue

    # Build the API call. Later turns pass previous_response_id so Max
    # can refer to earlier messages in this conversation.
    request = {
        "model": "gpt-5.6-sol",
        "instructions": standing_context,
        "input": user_message,
    }
    if previous_response_id:
        request["previous_response_id"] = previous_response_id

    # The model response comes from the OpenAI Responses API (GPT-5.6 Sol).
    response = client.responses.create(**request)

    # Remember this reply's id so the next message stays in the same thread.
    previous_response_id = response.id

    print()
    print("Max Smart:")
    print(response.output_text)
    print()
