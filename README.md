# Adam's Personal AI Workforce

This project is a personal AI workforce for Adam.

The first agent is a **Chief of Staff**. Its job is to help with planning, priorities, and day-to-day coordination — not to write software yet.

## Current status

A small **V1 Python prototype** exists. It is still a conversational Chief of Staff, not a full workforce.

- The program is `src/max_smart.py`
- It uses the OpenAI Responses API with GPT-5.6 Sol
- It loads Max's standing instructions, Adam's goals, and file-based memory
- It supports interactive Terminal conversation, including multiline input (`/multi`, `/send`, `/cancel`)
- Conversation context is kept during a running session. Resuming a conversation after restarting is not yet implemented
- Memory is read from project files; Max cannot currently modify those files itself
- External tools, integrations, and consequential actions are not implemented

## How to use this folder

Think of this as a home for notes and, later, agents.

- `README.md` — what this project is (this file)
- `docs/goals.example.md` — a generic template for local goals
- `docs/memory.example.md` — a generic template for local memory
- `docs/goals.md` — your local goals file; personal, ignored by Git, and not included in a fresh checkout
- `docs/memory.md` — your local memory file; personal, ignored by Git, and not included in a fresh checkout
- `.gitignore` — tells Git which files to ignore (for example secrets, conversation history, and personal context)

## Getting started (beginner)

1. Put your OpenAI API key in the `.env` file as `OPENAI_API_KEY=` (do not share or commit that file)
2. If `docs/goals.md` does not exist, copy `docs/goals.example.md` to `docs/goals.md`. If `docs/memory.md` does not exist, copy `docs/memory.example.md` to `docs/memory.md`. Edit only those local copies. Do not overwrite an existing personal copy.
3. From the project folder, run: `python3 src/max_smart.py`
4. Type a message after `You:`, or type `exit` to quit
5. Edit the local `docs/goals.md` and `docs/memory.md` when you want Max to know something durable; he reads those files when he starts
