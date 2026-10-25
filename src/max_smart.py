"""
Max Smart V1.1 — interactive conversation with a resumable local session.

Loads Max's instructions, Adam's goals, and file-based long-term memory,
then loops: you type, Max replies. Chat turns are saved locally so a
session can be resumed after restart.
"""

import json
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
# - manually controlled by Adam (this program never writes it)
# - read-only from Max's perspective
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

# Conversation history is the chat turns in this session (user/assistant
# messages). It is different from long-term memory: memory is durable
# facts Adam chose to keep in docs/memory.md; history is what was said
# in this conversation and is stored only in data/current_session.json.
session_path = project_root / "data" / "current_session.json"


def load_session_messages():
    """Return saved messages, or None if there is no session file.

    A missing file means a new conversation. A file that exists but
    cannot be read is an error — we do not delete or overwrite it.
    """
    if not session_path.exists():
        return None

    try:
        data = json.loads(session_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise SystemExit(
            f"Could not read {session_path}. The file was not changed.\n"
            f"Fix or move it, then start again.\n({error})"
        ) from error
    except UnicodeDecodeError as error:
        raise SystemExit(
            f"{session_path} cannot be decoded as UTF-8. The file was not changed.\n"
            f"Fix or move it, then start again.\n({error})"
        ) from error
    except json.JSONDecodeError as error:
        raise SystemExit(
            f"{session_path} is not valid JSON. The file was not changed.\n"
            f"Fix or move it, then start again.\n({error})"
        ) from error

    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise SystemExit(
            f"{session_path} is missing a messages list. "
            "The file was not changed. Fix or move it, then start again."
        )

    messages = data["messages"]
    for message in messages:
        role_ok = isinstance(message, dict) and message.get("role") in (
            "user",
            "assistant",
        )
        content_ok = isinstance(message, dict) and isinstance(
            message.get("content"), str
        )
        if not role_ok or not content_ok:
            raise SystemExit(
                f"{session_path} has an invalid message. "
                "The file was not changed. Fix or move it, then start again."
            )
    return messages


def save_session_messages(messages):
    """Write the current conversation to data/current_session.json."""
    session_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"messages": messages}
    session_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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
            save_session_messages([])
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
messages = choose_session(load_session_messages())

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
    save_session_messages(messages)

    print()
    print("Max Smart:")
    print(assistant_text)
    print()
