# Adam's Personal AI Workforce

This project contains Max Smart, a small personal Chief of Staff application. Max runs as an interactive Python Terminal program, uses GPT-5.6 Sol through the OpenAI Responses API, loads explicitly maintained personal context, and saves one resumable conversation locally.

Max currently produces text only. It has no external tools, web interface, background execution, or authority to take consequential actions.

## Prerequisites

- macOS with [Homebrew](https://brew.sh/)
- Homebrew Python 3.13
- Git
- An OpenAI API key
- A project-local virtual environment named `.venv`

Install the required system tools if needed:

```sh
brew install python@3.13 git
```

Do not use the macOS system Python 3.9 for this project.

## Initial setup

Run these commands from the project root:

```sh
"$(brew --prefix python@3.13)/bin/python3.13" -m venv .venv
source .venv/bin/activate
./.venv/bin/python -m pip install --require-hashes -r requirements-lock.txt
./.venv/bin/python -B -m unittest discover -s tests -v
```

The activation command affects the current Terminal window. The explicit `.venv/bin/...` commands below continue to select the project environment even if it is not activated.

## Configuration

Max loads `.env` from the project root and reads the variable `OPENAI_API_KEY`. Create the file without overwriting an existing one:

```sh
if [ ! -e .env ]; then
  (umask 077; printf 'OPENAI_API_KEY=\n' > .env)
fi
```

Open `.env` in a text editor and place your key after the equals sign:

```text
OPENAI_API_KEY=your_key_here
```

Never commit or share `.env`; it is ignored by Git. `load_dotenv()` loads `OPENAI_API_KEY` into the process environment. Code running in that process can read the variable, and child processes may inherit it unless it is filtered. The composition root reads the key to construct the OpenAI client. The key is not passed to or referenced by the Application Engine or Terminal interface.

## Personal context

Max assembles standing context from three Markdown files when the program starts:

- `docs/max-smart-instructions.md` — version-controlled role, behaviour, and safety instructions.
- `docs/goals.example.md` — version-controlled generic template for goals.
- `docs/goals.md` — your local priorities, preferences, and boundaries.
- `docs/memory.example.md` — version-controlled generic template for durable memory.
- `docs/memory.md` — manually curated facts and preferences you deliberately want Max to retain.

The two personal files are required at runtime, local-only, and ignored by Git. Create either missing file from its example without overwriting an existing personal copy:

```sh
[ -e docs/goals.md ] || cp docs/goals.example.md docs/goals.md
[ -e docs/memory.md ] || cp docs/memory.example.md docs/memory.md
```

Edit only the local copies. Max reads them but does not write them. Long-term memory in `docs/memory.md` is separate from conversation history in `data/current_session.json`.

Personal context is stored locally, but it is included in requests to the OpenAI API so the model can answer. Do not put professional, employer, customer, or other data you are not permitted to send into this personal system.

## Running Max

Launch Max from the project root:

```sh
./.venv/bin/python src/max_smart.py
```

If no saved conversation exists, Max starts with empty history. If one exists, choose:

- `R` — resume the saved conversation.
- `N` — start a new conversation and replace the saved session with an empty one.
- `Q` — quit before starting.

During a conversation:

- Type a normal line to send it immediately.
- Type `/multi` to begin a multiline message.
- In multiline mode, type `/send` on its own line to send the complete block.
- In multiline mode, type `/cancel` on its own line to discard the block.
- Type `exit` at the normal `You:` prompt to quit.

If the model request fails, the message is not saved. Max asks whether to retry (`R`) or cancel (`C`). Every retry requires another explicit choice.

If the model succeeds but the conversation cannot be saved, the response is held only in memory and is not displayed. Max asks whether to retry saving (`S`) or quit (`Q`). Retrying the save does not call the model again. Quitting warns that the unsaved response will be lost, then exits.

An invalid or unreadable saved session stops startup without changing the file. The error may include the session path and operating-system error details. During Terminal interaction, Ctrl+C and Ctrl+D are not currently handled and may terminate Max with a traceback.

## Testing and development

Run the complete test suite:

```sh
./.venv/bin/python -B -m unittest discover -s tests -v
```

The currently verified snapshot is **64 passing tests**. This count will change as coverage is added.

Validate that the locked dependency set can be resolved using its recorded hashes:

```sh
./.venv/bin/python -m pip install --dry-run --ignore-installed \
  --require-hashes -r requirements-lock.txt
```

`pip-audit` is a separate development/security tool, not an application runtime dependency. Install the tested version into the activated development environment without adding it to either requirements file:

```sh
python -m pip install pip-audit==2.10.1
pip-audit --require-hashes --disable-pip -r requirements-lock.txt
```

An audit reports vulnerabilities known to its data sources at the time it runs. A clean result does not guarantee that a vulnerability will not be discovered later.

Before committing a completed milestone:

```sh
git status --short
git diff --check
git diff
./.venv/bin/python -B -m unittest discover -s tests -v
git add <reviewed-files>
git diff --cached --check
git diff --cached --stat
git diff --cached
git commit -m "Describe the completed milestone"
```

Do not stage `.env`, personal context, or local conversation history.

## Django database foundation

Use `./.venv/bin/python -B manage.py <command>` as the only supported entry
point for Django management commands. The project `manage.py` applies the
database preflight and the current read-only command allowlist. Do not invoke
`.venv/bin/django-admin` or `./.venv/bin/python -m django` directly: those
entry points bypass both controls. Normal Django database connections still
use the validating backend, but a command such as `dbshell` accesses SQLite
outside that backend. This is an operational guardrail against mistakes, not
a security boundary against the owner of the Mac.

Database files are validated before and after SQLite opens them. The
post-open checks revalidate the path, symbolic-link status, permissions,
ownership, and hard-link constraints. They do not detect replacement by a
different database file that otherwise satisfies all of those constraints
and is owned by the current user. Such a swap requires write access to the
`0700` data directory, so this remains an accepted race in the local,
single-user threat model. A before-and-after identity snapshot was not added
because it could detect some replacements but could not eliminate the race.

Normal database connections never create a SQLite file. Explicit
initialization is the only creation path, and it refuses to run if the
database path or any corresponding `-wal`, `-shm`, or `-journal` path already
exists. Creation reserves the database path exclusively so a concurrent file
cannot be opened or truncated.

Every database also requires a private
`<database>.initialization-state` file containing the exact approved READY
marker. Initialization durably writes an INCOMPLETE marker before creating
the database and atomically replaces it with an fsynced READY marker only
after SQLite setup, close, and post-creation checks succeed. Missing,
malformed, INCOMPLETE, or transient marker files fail closed. A failure after
the atomic marker replacement is reported as commit-uncertain and requires
owner inspection; initialization never deletes or repairs database artifacts.

## Current limitations

- Execution is synchronous.
- The session engine supports one process and one saved conversation.
- Concurrent web or mobile sessions are not supported.
- Session saving rejects symbolic links at the data-directory and session-file paths; session loading currently follows symbolic links.
- Pending model messages and unsaved turns exist only in process memory.
- Quitting after an unsaved response permanently loses that response.
- There is no web interface or authentication layer.
- There are no tools, integrations, autonomous actions, or additional agents.
- No professional, employer, or customer data should enter this personal system.

See `docs/architecture.md` for module boundaries, state transitions, persistence, and security details.
