"""Terminal interface for Max Smart."""

from application import TurnStatus


def choose_session(existing_messages, start_new_session):
    """Prompt to resume, start new, or quit. None means no saved session."""
    if existing_messages is None:
        return []

    while True:
        print("A saved conversation was found.")
        choice = input("Resume (R), start new (N), or quit (Q): ").strip().upper()
        if choice == "R":
            return existing_messages
        if choice == "N":
            start_new_session()
            return []
        if choice == "Q":
            raise SystemExit("Goodbye.")
        print("Please type R, N, or Q.")
        print()


def read_user_message():
    """Read one normal or multiline user message from the Terminal."""
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

    return command


def user_wants_retry():
    """Ask once per model failure. Only an explicit R retries."""
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


def run_terminal(application):
    """Run the interactive Terminal conversation."""
    print("Max Smart V1.1. Type a message, type /multi for several lines, or type exit to quit.")
    print()

    while True:
        user_message = read_user_message()

        if user_message is None:
            continue

        if user_message.lower() == "exit":
            print("Goodbye.")
            return

        if not user_message.strip():
            continue

        if not run_terminal_turn(application, user_message):
            return
