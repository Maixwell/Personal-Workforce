# Max Smart — Technical Architecture

This document is a **living design** for Max Smart: how the system works now, and how it may grow later.

The first technical prototype **exists**. It is still a **deliberately small V1** — a conversational Chief of Staff, not a workforce, not always-on, and not connected to email, calendars, or other personal systems.

**What we have implemented:**

- A Python application at `src/max_smart.py`
- Session storage in `src/session.py` (`data/current_session.json`)
- Standing-context loading in `src/context.py`
- OpenAI GPT-5.6 Sol via the Responses API
- Secure API-key loading from `.env`
- Max Smart's standing instructions loaded from `docs/max-smart-instructions.md`
- Adam's goals loaded from `docs/goals.md`
- File-based long-term memory loaded from `docs/memory.md`
- A simple interactive Terminal conversation
- Terminal input: normal single-line messages; `/multi` to enter multiline input; `/send` to submit the multiline message as one user message; `/cancel` to discard a multiline draft
- Resumable conversation history saved locally in `data/current_session.json` (see section 9A)

The multiline Terminal input has been successfully tested and confirmed to deliver a large pasted prompt as a single message to Max Smart.

Max Smart is Adam's Chief of Staff. Cursor remains the development environment. The Python program is the current way Adam talks to Max Smart.

---

## 1. The big picture

Think of Max Smart as a **careful assistant sitting at a desk**, not a robot that can walk around the house.

On the desk there are a few things:

1. **A brain (the model)** — GPT-5.6 Sol, reached through the OpenAI Responses API. It reads Adam's words and writes a reply. It is not Max Smart by itself, and it cannot open Gmail.
2. **A job description (instructions)** — who Max Smart is, what “good” looks like, and what is forbidden. This currently comes from `docs/max-smart-instructions.md`.
3. **A pile of papers (context)** — the extra information we *choose* to show the model: Adam's goals, file-based memory, and the current Terminal conversation.
4. **A filing cabinet (memory)** — `docs/memory.md`. Adam edits it. Max reads it as context and cannot modify it.
5. **A set of tools (later)** — like a phone on the desk: “search the web,” “read the calendar.” Each tool is a *capability we add on purpose*. None are plugged in yet.
6. **A lock and a sign-off pad (permissions)** — Max Smart can *suggest* “send this email.” Adam must **approve** before anything consequential happens.
7. **A fenced yard (sandboxing, later)** — limits which folders, accounts, and actions the software is even *allowed* to touch, so a mistake cannot wipe the machine or leak private data.

**Current architecture:**

```
Adam
↓
Python application / control layer
↓
Max Smart's instructions + Adam's goals + file-based memory + current conversation
↓
OpenAI Responses API
↓
GPT-5.6 Sol
↓
Max Smart response
↓
Terminal
```

Python currently acts as the **communication and basic orchestration/control layer**. It loads files, keeps a local session of chat messages, sends each request (including that history) to the OpenAI Responses API, and prints text. It does not run tools or take external actions. The conversation is saved on disk, but replies are still produced by the cloud model — not processed entirely on the Mac.

Until tools exist, the loop ends at **text**: briefings, questions, “not this week” lists — the same job described in the blueprint.

---

## 2. The model

An **AI model** is a program trained to predict useful text. You give it a prompt (instructions + context + Adam's message). It returns words: a briefing, a challenge, a next step.

**What the model is good at**

- Turning messy notes into a short priority list
- Asking clarifying questions
- Comparing a plan to `docs/goals.md` (“this looks like taking on too much”)

**What the model is not**

- It is not a database. If a fact is not in the prompt or in files we load, it may guess. Max Smart must not pretend it knows.
- It is not automatically connected to Adam's life. Calendar, email, and money stay disconnected until we add tools *and* approval.
- It does not “become” Max Smart by itself. Max Smart is the **whole system**: model + instructions + context + (later) memory, tools, and locks.

**Swapping the model**

The model is like an engine in a car. We can change engines (a different cloud model, or later a local/open-weight model) **without rebuilding the car** — as long as the rest of the system talks to the model in a simple, standard way: “here is the prompt, give me text back.”

We should design so that:

- Instructions and files live in *this project*, not locked inside one vendor's app
- Tools and permissions sit *outside* the model (the model asks; the system decides whether to allow)

Then “try a different model” is a settings change, not a rewrite of Max Smart.

---

## 3. Instructions

Instructions are the **standing orders**. They tell the model *how to behave*, every time.

They will come mainly from written files Adam controls, for example:

- **Role** — Max Smart, Chief of Staff; smart enough to challenge Adam, disciplined enough not to act without him
- **Goals and taste** — `docs/goals.md` (priorities, more/less of, successful week, guiding principle)
- **Job boundaries** — `docs/chief-of-staff-blueprint.md` (what to help with, what never to do, Version 1 vs later)
- **This architecture** — how the pieces fit, so the system does not invent extra powers

In software terms, we will later load these files into the prompt (or a “system” message) at the start of a conversation.

**Beginner example:** If Adam dumps 12 projects, instructions should push Max Smart to *cut*, protect thinking time, and ask for approval — not to cheerlead a 40-item plan.

Adam can change behaviour by **editing the files**, not by hoping the model “remembers the vibe.”

---

## 4. Context

**Context** is whatever we **include in this turn** so the model can answer well. Models have a limited window (they can only “see” so much text at once).

**Version 1 context (intended):**

- Adam's latest message
- `docs/goals.md`
- The blueprint (and later this architecture file, if useful)
- Any extra notes Adam puts in the project (a weekly log, a brain-dump file)

**What context is not:** automatically reading all of Adam's email, disk, or phone. That would be a tool + a permission, and we are not connecting those.

**Practical habit:** keep source-of-truth notes **short and current**. A 3-page goals file beats a 300-page dump the model skims badly.

Later we can get smarter about *which* files to include (only this week's notes). That is still “context,” not magic memory.

---

## 5. Memory

**File-based persistent memory is now implemented.**

Current memory architecture:

- `docs/memory.md` is loaded when Max starts.
- The file is transparent and human-readable.
- Adam controls the contents.
- Max can read the memory as context but cannot modify it.
- Memory survives closing and restarting the Python program.
- This is persistent **application context**, not model training.
- Current memory is deliberately simple and manually curated.

A Terminal chat without this file would still forget the *conversation* when the program exits. `docs/memory.md` is the approved facts that come back on the next run.

**Future persistent / structured memory** (evolution from this file, not a replacement yet)

Examples people mean by more advanced “memory”:

- A database of facts
- Search over old briefings
- Automatic logs of what Max Smart proposed vs what Adam approved

Those can help, but they also create risk: hidden state Adam cannot see, or a system that “remembers” wrongly.

**Rule for now:** memory = **`docs/memory.md`**, edited by Adam. No database or automatic writing until there is a clear reason.

---

## 6. Tools

A **tool** is a specific action the *software* can take, other than “write text.”

Examples (all **future**, none connected):

- Web search → return a short briefing
- Read calendar → “you have two meetings Tuesday”
- Draft email → still **unsent** until Adam approves
- Create a file in this project (only in allowed folders)

**How it would work later**

1. The model says, in a structured way: “I want to use tool X with these arguments.”
2. The **system** (not the model alone) checks: is this tool allowed? Does it need Adam's yes?
3. If allowed, the system runs the tool and gives the result back to the model.
4. The model then writes something useful for Adam.

**Important:** giving the model a tool is like giving someone a key. We add keys one at a time, with a clear reason. No tools in the first technical version unless we explicitly decide even “write a markdown file” counts — and even that should be asked for, not done silently.

---

## 7. Permissions and approval

Adam stays in control. The blueprint already lists what must never happen without approval (messages as Adam, money, commitments, sharing private data, deleting important files, extra agents, new services, expanding powers, acting because “he would probably say yes”).

**Architecture translation:**

- **Default: suggest only.** Max Smart's first technical versions output text.
- **Consequential = stop.** Anything that leaves the project, spends money, talks to other people, or is hard to undo waits for an explicit yes.
- **No implied consent.** Past approvals do not license the next similar action unless Adam has set a narrow, written rule later.
- **Visible record (later).** When actions exist, log them in a way Max Smart cannot quietly hide or delete.

Until tools exist, “approval” mostly means: Adam **chooses** whether to follow a suggestion. That is enough for learning. When tools arrive, approval must become a real gate in the software, not a polite sentence in the prompt.

---

## 8. Security and sandboxing

**Sandboxing** means: run Max Smart in a **limited box**. Even if the model asks for something dangerous, the box cannot do it.

Simple picture: a guest in one room of the house. They can read the papers on the table (this project). They cannot open the safe (passwords), wander into other rooms (the rest of the computer), or phone the bank (external accounts) unless we install a phone *and* Adam unlocks it.

**What we might restrict later (not implementing now):**

- Which folders can be read or written
- No network, or only specific sites
- No access to `.env` or secrets
- No installing software
- No deleting outside a scrap folder

Prompts are not a sandbox. A model can be talked into ignoring instructions. **The fence has to be in the software and the computer**, not only in the job description.

We describe this now so we do not accidentally build a “helpful” script with full access to Adam's machine.

---

## 9. The first technical version

V1 **now exists** as `src/max_smart.py`, `src/session.py`, and `src/context.py`. It is still a deliberately small prototype.

V1 currently:

1. Reads Max's standing instructions.
2. Reads Adam's goals.
3. Reads Max's file-based long-term memory.
4. Accepts interactive messages in the Terminal (single-line, plus `/multi` / `/send` / `/cancel` for a pasted block sent as one user message).
5. Saves and resumes conversation history locally (details in section 9A).
6. Sends standing instructions, the full saved message history, and the new user message to GPT-5.6 Sol through the OpenAI Responses API (`store=False`).
7. Prints Max Smart's response.
8. Takes no consequential external actions.
9. Has no tools, external integrations, autonomous execution, databases, or multi-agent orchestration.

V1 exists primarily to **prove the conversational core** and **teach the architecture**. It is not a product, not a workforce, and not always-on. If this loop is not useful, we do not add tools to compensate.

---

## 9A. Current V1 implementation status

- Working (V1.1)
- Single-model
- Conversation-first
- File-based memory
- Locally saved, resumable conversation history
- Human-controlled
- No external actions
- Terminal interface
- Model: GPT-5.6 Sol
- API: OpenAI Responses API

The Terminal interface now supports:

- normal single-line messages
- `/multi` to enter multiline input
- `/send` to submit the multiline message as one user message
- `/cancel` to discard a multiline draft

The multiline Terminal input has been successfully tested and confirmed to deliver a large pasted prompt as a single message to Max Smart.

**Conversation history (V1.1)**

History is saved locally in `data/current_session.json` as a JSON object with a `messages` list. Each message has a `role` (`user` or `assistant`) and text `content`. The `data` directory is created when needed. `.gitignore` excludes `data/current_session.json` from Git.

The app no longer relies on `previous_response_id`. Each request sends the existing standing instructions (`instructions`) and the full saved message history plus the new user message (`input`) to the OpenAI Responses API, with `store=False`. After a successful API response, the user message and Max's text reply are appended to the local session file.

The history file lives on Adam's machine, but each turn is still sent to OpenAI for model processing. This is not a local-only model.

`docs/memory.md` remains separate: curated long-term memory, manually controlled by Adam, and read-only to Max. Conversation history is not that memory file.

**Current modules**

`src/session.py` owns loading and saving `data/current_session.json`: JSON structure `{"messages": [...]}`, validation of the top-level object, messages list, roles, and text content, and reporting invalid JSON or invalid UTF-8 via `SessionLoadError`. A failed load never deletes, resets, or overwrites the file.

`src/context.py` provides `build_standing_context(project_root)`. It reads `docs/max-smart-instructions.md`, `docs/goals.md`, and `docs/memory.md` in the original order and assembles them with the original labels and separators. It does not write to `docs/memory.md`.

`src/max_smart.py` still owns the R/N/Q startup menu, Terminal interaction, the OpenAI Responses API request with `store=False`, and saving the session after a successful response. Context loading has been extracted; model-request and Terminal logic have not.

**Startup**

- If no saved session exists, Max starts a new conversation.
- If a valid session exists, the user chooses Resume (`R`), New (`N`), or Quit (`Q`).
- Resume restores the conversation history.
- New begins with empty history and replaces the single saved session.
- Quit exits without starting a conversation.
- Invalid menu input prompts the user again.
- A malformed or unreadable session file produces an error and exits without silently deleting or overwriting the file.

**Limitations (this change)**

- Only one saved conversation is supported at present.
- Starting a new conversation replaces the saved session.
- Conversation history is separate from curated long-term memory.
- Max cannot modify `docs/memory.md`.
- No tools, external actions, integrations, additional agents, or background operation have been added by this change.

---

## 10. Future architecture

If Version 1 works, Max Smart can grow into a **coordinator**, not a pile of disconnected bots.

Sketch:

- **Max Smart (Chief of Staff)** — talks to Adam, holds priorities, challenges overload, decides what is worth delegating, **never** skips approval on consequential work
- **Specialist agents (later)** — e.g. a researcher, a meeting-prep helper — each with a **narrow** job and **fewer** permissions than Max Smart
- **Shared rules** — the same goals file, the same “never do this” list, the same approval gate
- **Max Smart asks Adam** before spawning a new specialist or connecting a new service

Specialists should make Adam's life simpler to understand, not a maze of agents. If Adam cannot say what each one is allowed to do, we have added too much.

---

## 11. What we are NOT building yet

Python, the basic OpenAI API connection, and the working conversational prototype are **no longer postponed**. They are part of the current V1 prototype.

The following are still **deliberately postponed**:

- Tools
- External service integrations
- Email/calendar
- Web search
- Messaging channels such as WhatsApp or iMessage
- Databases or advanced memory retrieval
- Multi-agent orchestration
- Board Advisor agents
- Sam Well and Nate Sharp implementations
- Dashboards/mobile apps
- Always-on operation
- Implemented sandboxing
- Permission UI
- Autonomous consequential actions
- Local/open-weight model setup

We expand **only** when there is a clear reason, as in section 19.

---

## 12. Board of Advisors — future architecture

This section **reserves** space in the architecture. We are **not** building a Board, advisors, extra agents, or new software now.

Later, Max Smart may coordinate a **Board of Advisors**: a small set of *perspectives* that help Adam think, not a committee that votes in his place.

**Conceptual structure**

```
Adam
│
├── Board of Advisors
│     ├── Strategic Advisor
│     ├── Business Advisor
│     ├── Investment Advisor
│     ├── Technology Advisor
│     └── Risk Advisor
│
└── Max Smart
      ├── Coordinates
      ├── Delegates
      ├── Challenges
      ├── Synthesises
      └── Brings recommendations back to Adam
```

**How the pieces relate**

- The **Board** provides perspectives, analysis, and challenges.
- **Max Smart** decides which perspectives are relevant to a question and coordinates the process.
- **Max Smart** synthesises areas of agreement, disagreement, assumptions, and important questions.
- **Adam** remains the decision-maker.
- Advisors must **not** make consequential decisions on Adam's behalf.
- Advisors should have **narrower permissions** than Max Smart.
- Advisors should only receive the **information necessary** for their particular task.
- Max Smart must **not** treat an advisor's recommendation as automatically correct.
- Different advisors may **disagree**, and that disagreement can be useful information.

The Board sits *beside* Max Smart in the picture so Adam stays at the top. In practice, Adam talks to **Max Smart**; Max Smart may consult advisors and then come back with a synthesis — not a pile of unfiltered voices, and not a decision already made.

This fits the rest of the architecture: same approval rules, same “no implied consent,” and no new tools or accounts just because an advisor exists on paper.

### Advisor roles

These are **potential future purposes**. They are not staffed, coded, or connected to anything today.

- **Strategic Advisor** — Helps with direction over months and years: what deserves focus, what to stop, whether a path matches Adam's goals and guiding principle (including rest, thinking time, and not filling every gap with more tasks).
- **Business Advisor** — Helps with building and running a business (or more than one): offers, customers, operations, and practical next steps — without making commitments or spending money.
- **Investment Advisor** — Helps Adam think about investing *as analysis and questions* (risks, assumptions, what is unknown). It does not place trades, move money, or decide for him.
- **Technology Advisor** — Helps Adam understand technical choices for this workforce and other systems: what to learn, what to postpone, when a tool or model is worth it — without installing or connecting things on its own.
- **Risk Advisor** — Stress-tests plans: privacy, over-automation, irreversible actions, too much autonomy, legal/financial/personal downside, and “are we building complexity we do not understand?”

**Roles first, agents later.** These start as **roles or personas** (different instruction sets Max Smart can apply, or ask a single model to speak *as*). A role should become a **separate agent** only when there is a clear reason for separate tools, data, memory, permissions, or capabilities. Disagreement between perspectives does not, by itself, require five running programs.

### Decision principle

The Board provides perspectives; Max Smart synthesises them; Adam makes the decision.

### Advisor identity and attribution

An advisor may be designed around a particular framework, a body of **publicly available** work, or a professional perspective (for example, a known way of thinking about strategy or risk).

If we later create an advisor *inspired by* a real person's publicly documented ideas, it must be clearly represented as an **AI advisor informed by those ideas** — **not** as that person, and **not** as speaking on their behalf.

No impersonation, no “this *is* so-and-so,” and no claiming private access to anyone's unpublished views.

---

## 13. Interface-independent and conversation-first architecture

This section **reserves** how Adam will *talk to* Max Smart later. We are **not** building apps, chatbots, messaging bots, or voice products now.

**Core principle:** Max Smart should be interface-independent. Different interfaces should provide channels into the **same** underlying Max Smart system rather than creating separate versions of Max.

**What that means:** the **brain of the system** (intelligence, memory, tools, permissions, and later orchestration of advisors/agents) lives in one place. A phone screen, a browser, a message thread, or a voice call is only a **door** into that place. Changing the door should not mean inventing a second Max.

**Intended future experience**

- The **primary** interaction should be **conversational**, similar to a modern AI chat: Adam types (or later speaks), Max Smart replies.
- Adam should be able to have an **ongoing** conversation with Max Smart, not a one-off form each time.
- Adam should eventually be able to ask Max Smart to **consult relevant Board Advisors** and receive a **synthesised** response (perspectives in; one coherent briefing out; Adam still decides).
- When it is useful, Adam may eventually **converse directly with a particular advisor** — still as a channel into the same system, with narrower permissions and clear attribution (an AI advisor, not a real person).
- Conversations should **keep appropriate context**, following whatever memory and privacy rules we have written: enough continuity to be useful, not a leak of everything to every channel.

Until that software exists, Cursor chat remains a **temporary stand-in**, not a second Max and not the iPhone app.

### Custom app

A dedicated **Max Smart application** for **iPhone and Mac**: the main, rich conversational experience, plus access to settings, memory, permissions, advisors, activity, and approvals.

This is the likely **home** for the most sensitive views (full memory, permission changes, approval history). Other channels should not automatically get the same powers.

### Web/Mac interface

A **browser or desktop** interface for richer work: reviewing projects, advisor reports, memory, permissions, integrations, and activity.

Same Max Smart underneath. A bigger desk, not a different employee.

### Messaging channels

**Possible later** support for platforms such as **iMessage** and **WhatsApp**, so Adam can message Max Smart **discreetly** when he is away from the main app.

These are **interfaces into Max Smart**, not separate Max Smart systems. A WhatsApp thread must not become “Max #2” with its own memory, tools, or rules.

### Channel security

Each interface is a **separate trust boundary**. A lock on the front door does not mean the garden gate is equally safe.

The architecture must **eventually** determine, **per channel**:

- How Adam is **authenticated** (proving it is him).
- What information Max Smart is **permitted to disclose**.
- Whether **sensitive** information is restricted to the **primary Max Smart application**.
- Which **actions** can be **initiated** from that channel.
- Which actions **always** need **explicit confirmation** (and whether confirmation must happen in the primary app).
- How conversations and channel activity are **logged**.
- How a **compromised messaging account** would be prevented from becoming **unrestricted access** to Max Smart (for example: limited replies, no new tools, no permission changes, no “send as Adam,” kill-switch / unlink).

Do **not** assume iMessage or WhatsApp has the **same** security properties as the private Max Smart application. Those apps are convenient; they are also accounts that can be stolen, forwarded, or shared. Convenience channels should default to **less** disclosure and **fewer** actions until proven otherwise.

### Voice

Reserve a future **voice** interface (talking to Max Smart) that uses the **same** underlying system and the **same** approval model. Voice is another door: it must not skip “ask Adam before consequential actions,” and it must not be a separate Max.

### Interface independence

Changing the interface should **not** require rebuilding Max Smart's core intelligence.

For example:

```
Adam → iPhone app → Max Smart
Adam → Mac/web → Max Smart
Adam → approved messaging channel → Max Smart
Adam → future voice interface → Max Smart
```

All should ultimately reach the **same** underlying system, subject to **channel-specific** authentication, permissions, and security controls.

### Future app architecture

The eventual **target** (not a build plan for today):

```
User interfaces
↓
Secure Max Smart backend
↓
Max Smart orchestration/core
↓
Model(s), memory, tools, Board Advisors and specialist agents
```

- **User interfaces** — app, web/Mac, messaging, voice: display conversation and collect Adam's words (and later taps for approve/deny).
- **Secure Max Smart backend** — authenticates Adam, enforces channel rules, holds secrets, talks to the core. Interfaces do not each carry their own copy of memory and tools.
- **Orchestration/core** — Max Smart's job: what to load as context, whether to consult advisors, whether a tool is even allowed to be *proposed*.
- **Model(s), memory, tools, Board, specialists** — the capabilities from earlier sections; still none of this is implemented in this step.

**Principle:** The interface should make Max Smart feel simple; the architecture underneath should make him controlled, auditable and extensible.

---

## 14. Three complementary AI roles

The personal AI workforce may eventually contain **three distinct primary conversational roles**. This is a **reservation**, not a build: we are not creating Sam Well, Nate Sharp, extra agents, or apps in this step.

These roles are complementary. They should stay **conceptually distinct**. Max Smart should not attempt to become every type of advisor or coach himself.

### Max Smart — Chief of Staff

Max Smart is the **central coordinator**.

His role is to help Adam see clearly, decide what matters, coordinate the appropriate perspectives, and turn decisions into practical next steps.

He may consult or coordinate other roles when their perspective is relevant, then **synthesise** the result for Adam.

Max Smart remains subject to the same approval, permission, privacy, and security principles defined elsewhere in this architecture.

### Sam Well — Personal Coach

Sam Well is Adam's **personal development and wellbeing coach**.

His purpose is to help Adam develop as a whole person, combining performance with balance, wellbeing, relationships, reflection, contentment, and personal growth.

Sam Well may eventually draw on established frameworks and perspectives from areas such as:

- Executive and personal coaching
- Psychology-informed approaches
- Cognitive behavioural approaches
- Family-systems perspectives
- Behaviour change
- Performance psychology
- Mindfulness and reflective practice
- Communication and relationships
- Values, purpose, and meaning
- Other approaches where there is a clear reason to consider them

Sam Well should distinguish between **evidence-supported** approaches, **professional opinion**, and more **speculative or contested** frameworks.

Sam Well must **not** present itself as a licensed psychologist, therapist, psychiatrist, hypnotherapist, or other credentialed professional.

It should **not** claim to diagnose Adam or possess professional credentials that it does not have.

Its role is to help Adam reflect, learn, identify patterns, test assumptions, develop useful behaviours, and pursue meaningful personal growth.

Where an issue appears to require professional clinical or other specialist support, Sam Well should **encourage appropriate human professional support** rather than presenting itself as a replacement.

Because personal-development conversations may contain **highly sensitive** information, Sam Well's memory and data should eventually have **particularly strong privacy controls**.

### Nate Sharp — Executive Coach

Nate Sharp is Adam's **professional performance and executive coach**.

His role is to help Adam become more effective in his professional life, particularly through:

- Strategic thinking
- Executive presence
- Account and business strategy
- Commercial judgement
- Prioritisation
- Leadership
- Preparation for important conversations
- Challenging assumptions
- Identifying blind spots
- Improving decision quality
- Maintaining focus on outcomes rather than activity
- Reflecting on professional performance and learning from experience

Nate Sharp should **challenge Adam constructively** rather than simply agreeing with him.

He should distinguish between **facts**, **assumptions**, **interpretations**, and **recommendations**.

Nate Sharp should **not** make decisions for Adam or take consequential actions on Adam's behalf.

### Separation of roles

**Max Smart asks:**

"What matters, what should we do, and who or what should help us think about it?"

**Sam Well asks:**

"How are you doing, who are you becoming, and is the way you are living aligned with what matters to you?"

**Nate Sharp asks:**

"How can you perform more effectively, think more strategically and create better outcomes professionally?"

The roles may sometimes address the **same issue** from different perspectives. That overlap is **intentional**.

For example, a major career decision might involve:

- **Max Smart** — clarifying the decision and coordinating the process
- **Sam Well** — examining personal values, wellbeing, relationships, and underlying patterns
- **Nate Sharp** — examining professional strategy, opportunity, risks, and execution

Different perspectives may **disagree**. Disagreement should be **surfaced** rather than artificially resolved.

**Adam remains the decision-maker.**

---

## 15. Coordination between Max Smart, Sam Well and Nate Sharp

Max Smart is the **primary coordinator** of the personal AI workforce. That does **not** make Max Smart the owner of Sam Well's or Nate Sharp's perspectives.

When Adam asks Max Smart a question, Max may determine that another perspective would be useful.

**Example**

Adam:

"I am considering taking on this major new business opportunity. What do you think?"

Max Smart may respond:

"I can help you structure the decision. This also touches your professional strategy and your broader goals, so I would like to consider Nate Sharp's and Sam Well's perspectives."

Max Smart may then synthesise:

- Areas of agreement
- Areas of disagreement
- Important assumptions
- Risks and unknowns
- Questions Adam should consider
- Potential next steps

Max Smart must **not** present another role's recommendation as established fact.

The final decision remains Adam's.

### Direct conversations

Adam may eventually choose to speak **directly** with Sam Well or Nate Sharp.

Those conversations should still use the **same underlying system** where appropriate, while maintaining the role's distinct instructions, memory boundaries, and permissions.

A direct conversation with Sam Well should **not** automatically expose Nate Sharp's professional context.

A direct conversation with Nate Sharp should **not** automatically expose Sam Well's sensitive personal-development context.

Context should be shared **only when necessary and permitted**.

---

## 16. Privacy and context boundaries between roles

**Knowing Adam well** does not mean every component gets unrestricted access to everything about Adam.

The eventual architecture should support:

- Role-specific memory
- Shared memory where explicitly appropriate
- Sensitive/private memory with stronger controls
- Explicit rules governing what information can move between roles
- Auditability of significant context sharing
- The ability for Adam to review or revoke access

In particular:

**Sam Well may contain highly sensitive personal information.**

That information should **not** automatically become available to Max Smart, Nate Sharp, Board Advisors, specialist agents, messaging channels, or external services.

**Nate Sharp may contain sensitive professional context.**

That information must **not** automatically flow into Max Smart's personal systems if it contains confidential employer or customer information.

Where professional information is involved, the system must respect the applicable **employer policies** and **approved AI environment**.

**Max Smart should not become a universal data dump.**

Its role is to coordinate **useful** context, not automatically accumulate every piece of information available to every other component.

---

## 17. Future role architecture

These three roles do **not** necessarily mean three separate running programs.

Initially, each may simply be a distinct **role / persona / instruction set** operating through the same underlying model or system.

A role should become a **separate agent** only when there is a clear reason for:

- Different tools
- Different permissions
- Different memory
- Different security boundaries
- Different models
- Different execution requirements
- Independent operation

The architecture should avoid creating multiple autonomous systems merely because multiple perspectives are useful.

### Guiding principle

**Roles first. Agents later.**

The purpose of the architecture is to create useful **separation of perspective and responsibility** without creating unnecessary technical complexity.

---

## 18. Personal versus professional AI boundary

There should be a **firm separation** between Adam's **personal AI workforce** and any AI environment used for Adam's **employment**.

Max Smart, Sam Well, and the personal AI workforce must **not** connect to corporate systems or ingest confidential company or customer information unless such use is **explicitly authorised** through an appropriate **enterprise-controlled** environment.

If Adam's employer provides an approved AI environment, a separate professional AI assistant or Work Coach may eventually be designed **within those controls**.

This would be a **separate system** from the personal AI workforce.

A future professional AI assistant could potentially perform roles similar to Nate Sharp, such as:

- Account strategy
- Executive preparation
- Commercial thinking
- Research
- Meeting preparation
- Professional prioritisation
- Strategic challenge

However, its access to corporate information, tools, email, calendar, CRM, and other systems would be determined **entirely** by the employer's approved policies, controls, and technology.

The personal Max Smart architecture must **not** be used to circumvent those controls.

### Principle

**Personal AI and professional enterprise AI should be complementary, not connected by default.**

Adam may learn from both systems, but information should cross the boundary **only when Adam is permitted to transfer it**.

---

## 19. Evolving architecture

These roles are deliberately **not frozen forever**.

Max Smart, Sam Well, and Nate Sharp may evolve as Adam learns what is useful.

Their responsibilities, instructions, memory structures, permissions, and relationships may change.

Architecture documents, goals, and role definitions should therefore be treated as **living design documents**.

However, significant changes to **permissions**, **external access**, **autonomy**, **security boundaries**, or **data sharing** should be **explicitly documented and approved** before implementation.

The system should evolve **deliberately** rather than acquiring capabilities accidentally.

### Guiding principle

**The architecture should grow with Adam's understanding, not ahead of it.**

### Current milestone

Max Smart V1 has successfully completed the first core loop:

Adam → Python → OpenAI Responses API → GPT-5.6 Sol → Max Smart response

The system now also loads persistent file-based memory:

docs/memory.md

This milestone demonstrates that Max Smart can retain deliberately approved information across separate program sessions without using a database or changing the underlying model.

The Priority & Capacity Review workflow has now been successfully tested end-to-end:

capture
→ clarify
→ distinguish values from vehicles
→ assess capacity
→ prioritise
→ challenge assumptions
→ propose allocation/sequencing
→ protect whole-life foundations
→ present a provisional recommendation
→ request explicit user review/approval

Important behavioural result:

- "That's everything" / equivalent statements now transition Max from capture into review rather than causing it to continue collecting items.
- Max explicitly distinguishes known, inferred, and unknown information.
- Max presents recommendations as provisional and does not treat them as approved decisions without explicit user approval.

The sophisticated test confirmed Max can:

- separate values/priorities from projects and activities
- distinguish the current economic engine, strategic capability investment, and financial experiments
- identify capacity consumption and potential attention costs
- challenge assumptions
- propose allocation and sequencing
- preserve user control over the final decision

The multiline Terminal input has been successfully tested and confirmed to deliver a large pasted prompt as a single message to Max Smart.

### Current milestone — 2026-10-25

Max Smart V1.1 resumable conversation has been verified:

- Session saved with a user message and assistant response.
- Session file contained the expected two messages.
- Conversation resumed after restarting Max.
- Max correctly recovered the test details `amber kite` and `Thursday`.
- Quit from the startup menu exited without starting a conversation.
- Starting a new conversation did not carry forward the prior conversation's context.
- A malformed JSON session file was detected; Max reported that the file had not been changed and exited.
- A session file containing invalid UTF-8 was detected; a clear error was displayed and the file was left unchanged.
- The saved session backup was restored and validated as JSON.

After extracting session storage into `src/session.py`, that behaviour was regression-tested: resume, new conversation, quit without modifying the session file, malformed JSON handling, and invalid UTF-8 handling. The valid session file was restored and verified after the damaged-file tests.

After extracting standing-context loading into `src/context.py`, the following checks passed:

- Python compilation.
- Whitespace check.
- Context-integrity assertions confirming the original files, order, labels, separators, and content remain in the assembled context.
- `docs/memory.md` remained unchanged.
- A successful live response after resuming a saved session.
