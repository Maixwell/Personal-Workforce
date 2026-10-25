"""Local conversation-session storage for Max Smart.

History lives in data/current_session.json. This is not long-term memory
(docs/memory.md). A failed load never deletes or overwrites the file.
"""

import json
from pathlib import Path


class SessionLoadError(Exception):
    """The session file exists but could not be used. The file was not changed."""


def session_path(project_root):
    """Return the session file path under the project data directory."""
    return Path(project_root) / "data" / "current_session.json"


def load_session_messages(project_root):
    """Return saved messages, or None if there is no session file.

    A missing file means a new conversation. A file that exists but
    cannot be read raises SessionLoadError — we do not delete or overwrite it.
    """
    path = session_path(project_root)
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise SessionLoadError(
            f"Could not read {path}. The file was not changed.\n"
            f"Fix or move it, then start again.\n({error})"
        ) from error
    except UnicodeDecodeError as error:
        raise SessionLoadError(
            f"{path} cannot be decoded as UTF-8. The file was not changed.\n"
            f"Fix or move it, then start again.\n({error})"
        ) from error
    except json.JSONDecodeError as error:
        raise SessionLoadError(
            f"{path} is not valid JSON. The file was not changed.\n"
            f"Fix or move it, then start again.\n({error})"
        ) from error

    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise SessionLoadError(
            f"{path} is missing a messages list. "
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
            raise SessionLoadError(
                f"{path} has an invalid message. "
                "The file was not changed. Fix or move it, then start again."
            )
    return messages


def save_session_messages(project_root, messages):
    """Write the current conversation to data/current_session.json."""
    path = session_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"messages": messages}
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
