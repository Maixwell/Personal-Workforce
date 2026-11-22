# Milestone 2 — SQLite data model and repository plan

Personal AI Workforce / Max Smart · implementation-ready design · no repository code changed

Revision 3 incorporates the first and second independent architecture and security reviews. No implementation is authorized by this document.

## 1. Scope

> **Milestone 2 boundary**
>
> Build the SQLite schema, migrations, connection safeguards, typed repository results, and short atomic transitions. Do not wire Terminal or Django views to them yet, do not import the private JSON file, and do not make a provider request from the repository.

### Authoritative invariants

- Conversation history is committed message data. Long-term personal memory remains in the separately controlled Markdown files and is never copied into these tables.
- A QUEUED reservation protects PREPARE and context assembly; a RUNNING lease begins only at claim. They have different clocks and must never be treated as one timeout.
- FAILED means a definite failure. An uncertain provider result remains RUNNING until both execution deadline and fixed lease expire, then becomes OUTCOME_UNKNOWN.
- A provider response is not a committed turn. Only atomic APPLY inserts both messages, advances version/sequence, marks SUCCEEDED, clears the block, and conditionally releases the slot.
- Recovery classifies durable state; it cannot prove an external provider request stopped and never automatically repeats one.
- Every conditional mutation requires exactly one affected row. Zero is a typed race/business result or integrity conflict; greater than one is always an integrity failure and rolls back.

### Delivery target

- **Milestone 2 domain tables:** 7
- **Writer model:** one SQLite writer at a time
- **Journal:** WAL on local storage
- **Durability:** `synchronous=FULL`
- **Write transactions:** short `BEGIN IMMEDIATE`
- **Provider calls in transaction:** never
- **Service orchestration:** Milestone 3
- **JSON import execution:** Milestone 4
- **Database path:** `data/max_smart.sqlite3`
- **Repository implementation:** one Django ORM repository shared by Terminal and Django
- **Time source:** injected `Clock`, read once per write transaction after `BEGIN IMMEDIATE` succeeds
- **Retry creation:** Milestone 3; Milestone 2 provides only schema constraints and typed rejection mapping

### Existing code versus target design

| Area | Current code | Milestone 2 implication |
|---|---|---|
| Engine | PREPARE / REQUEST / APPLY types exist, but `MaxSmartApplication` still owns mutable pending and unsaved state. | Keep engine provider-neutral. Repository stores durable operation state; Milestone 3 removes service dependence on the mutable wrapper. |
| Failure types | `ModelRequestError` is conservatively UNCERTAIN; unusable completed output is DEFINITE_FAILURE. | Repository accepts normalized classifications. It must not infer provider non-execution from HTTP status or timeout. |
| Persistence | `session.py` atomically replaces one JSON snapshot with role/content messages. | SQLite stores ordered rows, versions, operations, idempotency and slot ownership. JSON remains untouched in Milestone 2. |
| Context | `context.py` reads standing instructions, goals and long-term memory from files. | Those files remain separate and read-only; no memory/context table is introduced. |
| Provider | `model_client.py` returns `output_text` and sets `store=False`; no durable deadline is supplied yet. | Milestone 2 records deadline fields only. Adapter deadline orchestration belongs to Milestone 3. |
| Schema identity | No user, conversation, operation, sequence, version or runner identity exists in JSON. | All are introduced as durable metadata without changing Terminal behavior yet. |

> **Do not collapse these concepts**
>
> The slot is global, while the block is per conversation. A released slot does not necessarily mean the old provider request stopped. Likewise, an OUTCOME_UNKNOWN row can remain conversation-blocking after the slot has been released.

## 2. Schema

### Logical and physical schema

Logical UUID fields use Django `UUIDField`; SQLite stores them as fixed-width character values. Durable timestamps are proposed as UTC integer microseconds supplied by the service, so comparisons do not depend on text formatting or database-local time.

| Table | Columns | Owner | Authority and lifecycle |
|---|---|---|---|
| `authorized_identity` | `id INTEGER PK`; `user_id FK UNIQUE`; `tailscale_login TEXT UNIQUE`; `enabled INTEGER CHECK 0/1`; `created_at_us`; `last_seen_at_us` | Identity middleware (M6); schema created now because conversation ownership references the Django user. | Authoritative identity binding. Rows are disabled, not deleted. Partial unique index `UNIQUE(enabled) WHERE enabled = 1` permits at most one enabled identity in V1. |
| `role` | `slug TEXT PK`; `display_name TEXT`; `active INTEGER CHECK 0/1` | Application configuration migration. | Authoritative role allowlist. Seed `max-smart`; deactivate rather than delete while referenced. |
| `conversation` | `id UUID PK`; `owner_id FK`; `role_slug FK`; `version INTEGER >=0`; `next_sequence INTEGER >=1`; `title TEXT NULL`; `created_at_us`; `updated_at_us`; `archived_at_us NULL` | Conversation repository. | Authoritative aggregate metadata. Version advances once per committed mutation; `next_sequence` allocates ordered messages. Archive preserves restrictive FKs. |
| `operation` | `id UUID PK`; `conversation_id FK`; `owner_id FK`; `operation_type`; `provider_id`; `model_id`; `status`; `blocks_conversation`; `expected_version`; `request_content`; `fingerprint_version`; `request_hash`; `retry_of FK NULL UNIQUE`; `runner_id`; `reservation_expires_at_us`; `provider_started_at_us`; `execution_deadline_us`; `lease_expires_at_us`; `created_at_us`; `finished_at_us`; `acknowledged_at_us`; `failure_category` | Operation repository/state machine. | Authoritative lifecycle and immutable request binding. Created by PREPARE, transitioned conditionally, never physically deleted during normal retention. V1 `operation_type` is `message`; provider/model values come from explicit allowlists; `request_content` has the approved 20,000-character application limit. |
| `message` | `id UUID PK`; `conversation_id FK`; `sequence INTEGER`; `role CHECK user/assistant`; `content TEXT`; `operation_id FK NULL`; `created_at_us` | Conversation repository through APPLY/import. | Authoritative committed history. Imported legacy rows have `NULL operation_id`; normal turns link both rows to one SUCCEEDED operation. |
| `idempotency_key` | `id INTEGER PK`; `owner_id FK`; `conversation_id FK`; `operation_type`; `expected_version`; `key_digest_version`; `key_digest TEXT`; `fingerprint_version`; `request_hash`; `operation_id FK UNIQUE`; `expires_at_us` | PREPARE repository. | Authoritative binding from one owner-scoped high-entropy key digest to one operation. Unique `(owner_id, key_digest_version, key_digest)`. Cleanup only after expiry, terminal non-blocking operation, and recovery window. Raw keys are never stored. |
| `global_model_slot` | `id INTEGER PK CHECK id=1`; `operation_id FK UNIQUE NULL`; `runner_id NULL`; `reservation_expires_at_us NULL`; `lease_expires_at_us NULL`; `acquired_at_us NULL` | Slot repository, shared by Terminal and Django. | One permanent singleton row. NULL ownership fields mean free; acquisition/release updates this row conditionally instead of inserting/deleting it. |
| `django_migrations` | Django-managed migration identifier and applied timestamp | Django migration framework. | Authoritative schema history. Never repurpose as application state. |
| `legacy_import` (Milestone 4 migration) | `id`; `source_kind UNIQUE`; `source_sha256`; `conversation_id FK UNIQUE`; `imported_at_us`; `message_count`; `ordered_content_sha256`; `verified_at_us`; `accepted_at_us` | One-time importer, not Milestone 2 runtime. | Proposed idempotency/audit ledger for the JSON import. Add in Milestone 4 unless approved now; it avoids overloading conversation metadata. |

### Required constraints and indexes

#### Conversation and messages

- `UNIQUE(message.conversation_id, message.sequence)`.
- `UNIQUE(message.operation_id, message.role)` where `operation_id` is not null; prevents duplicate user/assistant insertion while permitting legacy NULL links.
- `CHECK version >= 0`, `next_sequence >= 1`, role in `('user','assistant')`, content type is text, and timestamps are non-negative.
- Do not require non-empty `message.content`: legacy history may contain an empty assistant message. New ordinary APPLY validates a non-blank assistant response before persistence.
- Indexes: `message(conversation_id, sequence)`, `conversation(owner_id, updated_at_us)`, `conversation(owner_id, archived_at_us)`.
- Restrictive FKs; archive conversations instead of cascading history.

#### Operations and blocking

- `CHECK status` in `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `OUTCOME_UNKNOWN`, `CANCELLED`; `CHECK operation_type = 'message'`; `CHECK expected_version >= 0`; `CHECK length(request_content) BETWEEN 1 AND 20000`; and checked/allowlisted provider and model identifiers.
- Status/block CHECK: QUEUED and RUNNING require `blocks_conversation=1`; SUCCEEDED, FAILED and CANCELLED require `blocks_conversation=0`; OUTCOME_UNKNOWN requires `blocks_conversation=1` exactly when `acknowledged_at_us IS NULL`, otherwise zero.
- Timing CHECK: QUEUED requires a reservation and null provider-start/deadline/lease; RUNNING requires provider-start/deadline/lease; SUCCEEDED and OUTCOME_UNKNOWN retain non-null provider-start/deadline/lease; CANCELLED requires `provider_started_at_us IS NULL`; FAILED may be either a pre-provider failure (all three start/deadline/lease fields NULL) or a provider-boundary failure (all three retained); every final state requires `finished_at_us`.
- `acknowledged_at_us` is non-null only for OUTCOME_UNKNOWN.
- Partial `UNIQUE(operation.conversation_id) WHERE blocks_conversation=1`. This is the authoritative blocking-operation reference; do not duplicate it on conversation.
- Partial unique index `UNIQUE(blocks_conversation) WHERE status IN ('QUEUED','RUNNING')` permits at most one globally live operation, complementing the singleton slot.
- `UNIQUE(retry_of) WHERE retry_of IS NOT NULL` permits at most one direct retry of an operation in V1.
- Indexes: `operation(conversation_id, created_at_us)`, `operation(conversation_id, status)`, `operation(created_at_us)`, `operation(status, reservation_expires_at_us)`, `operation(status, lease_expires_at_us, execution_deadline_us)`.

#### Idempotency and singleton slot

- `UNIQUE(owner_id, key_digest_version, key_digest)`; the row also binds conversation, type, expected version, `fingerprint_version` and `request_hash`.
- `UNIQUE(operation_id)` creates the one-to-one idempotency mapping; index `expires_at_us` for cleanup.
- `global_model_slot` has exactly one seeded row with `id=1`.
- Slot CHECK: either all ownership/timing columns are NULL, or `operation_id`, `runner_id` and `acquired_at` are all present with exactly one of `reservation_expires_at` or `lease_expires_at` present.
- The operation/slot status correspondence is enforced by repository transactions because SQLite CHECK constraints cannot safely inspect another table.
- A `BEFORE DELETE` trigger rejects deletion of the singleton slot row. The operation timing fields are authoritative; slot timing values are fenced copies checked transactionally and at startup.

### Cross-table integrity and immutability triggers

SQLite single-column foreign keys do not prove that redundant identity and binding columns agree. Milestone 2 therefore creates named triggers in a Django migration, and startup verifies that they exist:

- `BEFORE INSERT operation`: reject unless `operation.owner_id` equals the referenced conversation's owner.
- `BEFORE INSERT ON message`: when `operation_id` is non-null, reject unless the operation has the same `conversation_id`.
- `BEFORE UPDATE ON message`: always reject. Message history, including imported legacy rows, is immutable once inserted.
- `BEFORE INSERT ON idempotency_key`: reject unless owner, conversation, operation type, expected version, fingerprint version and request hash all equal the associated operation. This trigger reads the operation row, so PREPARE inserts the operation first.
- `BEFORE UPDATE OF owner_id, conversation_id, operation_type, expected_version, key_digest_version, key_digest, fingerprint_version, request_hash, operation_id ON idempotency_key`: reject every attempted binding change.
- `BEFORE INSERT ON operation` when `retry_of` is non-null: reject unless the source exists with the same owner and conversation, has `blocks_conversation = 0`, and has status FAILED, status CANCELLED, or status OUTCOME_UNKNOWN with non-null `acknowledged_at_us`. The source may itself be a retry, so linear chains are allowed. `UNIQUE(retry_of)` prevents branching.
- `BEFORE UPDATE OF owner_id, conversation_id, request_content, request_hash, fingerprint_version, expected_version, operation_type, provider_id, model_id, retry_of ON operation`: reject every attempted change. These identity fields are immutable after PREPARE, so no separate UPDATE-time retry check is needed.
- `BEFORE DELETE ON global_model_slot`: reject deletion.

SQLite triggers fire for exactly one event, so INSERT-time and UPDATE-time protections are always separate triggers. These triggers complement, rather than duplicate, ordinary foreign keys: foreign keys establish existence; triggers establish cross-row agreement that SQLite CHECK constraints cannot express. Repository transactions remain responsible for valid transitions and exact affected-row checks.

Triggers are not deferred. Django creates deferred SQLite foreign keys, but that never defers trigger execution, so statement order within a transaction must satisfy every trigger.

### Retry relationship

- `retry_of` may reference only a non-blocking FAILED, CANCELLED, or acknowledged OUTCOME_UNKNOWN operation. A still-blocking source, including unacknowledged OUTCOME_UNKNOWN, is rejected.
- Source and retry have the same owner and conversation, but the retry receives a new server-generated operation ID and a new idempotency key.
- Linear chains are allowed: if retry R1 itself fails, R2 may retry R1. This preserves the current Terminal ability to retry a pending message repeatedly.
- Each operation may be the direct source of at most one retry. `UNIQUE(retry_of)` prevents a chain from branching; a second retry of R1 is rejected.
- The possible-duplicate-charge warning applies when the direct source is OUTCOME_UNKNOWN.
- Reconciliation is always keyed by the original operation ID and runner, never by fingerprint. A retry may legitimately have the same fingerprint as its source.
- Creating a retry and setting `retry_of` is Milestone 3 service-layer work. Milestone 2 implements the constraints, triggers, typed mapping and constraint tests only; it adds no retry-creation method.

### Fingerprint and idempotency-key encoding

`fingerprint_version` is mandatory on both the operation and its idempotency binding. Version 1 is:

1. Domain tag: `max-smart/op-fingerprint/v1`.
2. Fixed field order: owner ID as decimal ASCII, conversation ID as a lowercase hyphenated UUID, operation type, expected version as decimal ASCII, agent role slug, provider ID, model ID, and exact message content.
3. Encode each field as UTF-8 bytes preceded by an unsigned 8-byte big-endian byte length.
4. Apply no Unicode normalization, whitespace trimming or newline conversion. `request_content` preserves the original message exactly, including leading/trailing whitespace and CRLF.
5. Compute SHA-256 over the complete encoding and store the lowercase hexadecimal digest.

This plan specifies that fingerprint V1 includes provider ID and model ID so one application key cannot silently select a different provider or model, including a future local model. That extends the architecture's original field list. Implementation of this encoding waits for Adam's explicit confirmation; if he rejects the extension, the version-1 field list is reduced to the architecture's original fields and the version tag stays `v1` only if no encoded fingerprint has been stored.

Idempotency keys must decode to at least 128 random bits in a validated, canonical, unpadded base64url format. Reject non-canonical input, including padding, non-alphabet characters, and encodings whose unused trailing bits are non-zero; the decoded bytes must re-encode to exactly the submitted text. Store `key_digest_version=1` and lowercase `SHA-256(decoded_key_bytes)`, never a digest of the base64url text and never the raw key. HMAC is intentionally not used: these high-entropy application keys are not authentication secrets, and an HMAC would add a secret-rotation lookup problem without a concrete security benefit. Cleanup may delete a key only when it is expired, its operation is terminal and non-blocking, and the recovery-retention window has elapsed.

### State-dependent operation checks

```text
QUEUED:
  blocks_conversation = 1
  runner_id, reservation_expires_at_us NOT NULL
  provider_started_at_us, execution_deadline_us, lease_expires_at_us NULL

RUNNING:
  blocks_conversation = 1
  runner_id, provider_started_at_us, execution_deadline_us, lease_expires_at_us NOT NULL
  execution_deadline_us <= lease_expires_at_us

SUCCEEDED:
  blocks_conversation = 0
  provider_started_at_us, execution_deadline_us, lease_expires_at_us NOT NULL
  finished_at_us NOT NULL

FAILED:
  blocks_conversation = 0
  finished_at_us NOT NULL
  Pre-provider FAILED has provider_started_at_us, execution_deadline_us and
  lease_expires_at_us NULL. Provider-boundary FAILED retains those three fields.

CANCELLED:
  blocks_conversation = 0
  provider_started_at_us NULL
  finished_at_us NOT NULL

OUTCOME_UNKNOWN:
  provider_started_at_us, execution_deadline_us, lease_expires_at_us NOT NULL
  finished_at_us NOT NULL
  blocks_conversation = 1 exactly while acknowledged_at_us IS NULL

Slot free:
  operation_id = runner_id = reservation_expires_at_us =
  lease_expires_at_us = acquired_at_us = NULL

Slot QUEUED:
  operation_id + runner_id + reservation + acquired_at; lease NULL

Slot RUNNING:
  operation_id + runner_id + lease + acquired_at; reservation NULL
```

Forbidden transitions are every transition not listed in Section 5, including any terminal state back to QUEUED or RUNNING, RUNNING to CANCELLED, FAILED or CANCELLED to any other state, and acknowledged OUTCOME_UNKNOWN to SUCCEEDED.

> **No `conversation.blocking_operation_id` column**
>
> The approved design already makes the blocking operation authoritative through `operation.conversation_id` plus `blocks_conversation` and a partial unique index. Adding a second pointer would require two-way consistency on every transition and creates avoidable split-brain metadata.

## 3. Contracts

### Repository contract shape

Public methods are aggregate transitions, not CRUD fragments. This prevents a service caller from acquiring the slot without creating the operation/block, or completing an operation without releasing its slot.

```python
# Immutable command/ownership tokens
QueuedReservation(operation_id, actor_id, conversation_id, request_hash,
                  fingerprint_version, expected_version, runner_id,
                  reservation_expires_at)
ClaimedOperation(operation_id, actor_id, conversation_id, request_hash,
                 fingerprint_version, expected_version, runner_id, claimed_at,
                 execution_deadline, lease_expires_at)
RetainedResult(claimed_operation, output_text)

# Construction: one repository per process
Clock.now_us() -> int  # UTC integer microseconds
ConversationRepository(clock: Clock, runner_id: str)

# Conversation reads
create_conversation(actor_id, role_slug, title) -> ConversationView
get_conversation(actor_id, conversation_id) -> ConversationView | NotFound
load_snapshot(actor_id, conversation_id) -> Snapshot | NotFound
list_messages(actor_id, conversation_id, after_sequence=None) -> tuple[Message, ...]

# Durable lifecycle
prepare(command) -> PrepareResult
adopt_queued(actor_id, operation_id) -> AdoptResult
claim_running(actor_id, operation_id) -> ClaimResult
fail_pre_provider(reservation, category) -> PreProviderFailureResult
apply_success(claimed, output_text) -> ApplySuccessResult
apply_failure(claimed, failure) -> ApplyFailureResult
reconcile_late_success(retained_result) -> ReconcileResult
recover_slot() -> RecoveryResult
acknowledge_unknown(actor_id, operation_id) -> AcknowledgeResult
get_operation(actor_id, operation_id) -> OperationView | NotFound

# Cleanup/maintenance
delete_expired_idempotency(limit) -> CleanupResult
```

The repository owns every write transaction, so callers never supply `now`, `claimed_at`, or another durable timestamp. Each write method opens `transaction.atomic(durable=True)`, waits for `BEGIN IMMEDIATE`, then calls the injected `Clock` exactly once. That single reading is used for every expiry comparison and every timestamp written by the transaction. For `claim_running`, it is the durable `provider_started_at_us` and the base for `execution_deadline_us` and `lease_expires_at_us`. The process's `runner_id` is fixed at repository construction rather than passed per call.

All operation reads are actor-scoped. A UUID owned by another actor returns the same `NotFound` result as a nonexistent UUID; `Forbidden` is not exposed for operation-object lookup because it would enable enumeration.

### Typed expected results

| Method | Success variants | Expected non-success variants |
|---|---|---|
| `prepare` | `Prepared(reservation)`; `Existing(operation)` | `KeyBindingMismatch`; `StaleVersion(actual)`; `ConversationBlocked(operation_id, status)`; `ModelBusy`; `NotFound`; `DatabaseBusy`; `CommitAmbiguous` |
| `adopt_queued` | `Adopted(reservation)`; `AlreadyOwned(reservation)` | `ExpiredResubmit`; `NotQueued(view)`; `OwnershipLost`; `SlotMismatch`; `NotFound`; `DatabaseBusy`; `CommitAmbiguous` |
| `claim_running` | `Claimed(ClaimedOperation)`, including `claimed_at` | `ExpiredResubmit`; `NotQueued(view)`; `OwnershipLost`; `SlotMismatch`; `NotFound`; `DatabaseBusy`; `CommitAmbiguous(operation_id, runner_id, claimed_at)` |
| `fail_pre_provider` | `Failed(OperationView)`; `AlreadyFailed(OperationView)` | `NotQueued(view)`; `OwnershipLost`; `SlotMismatch`; `DatabaseBusy`; `CommitAmbiguous` |
| `apply_success` | `Applied(OperationView)`; `AlreadySucceeded(OperationView)` | `RecoveredUnknown(reconcile_required)`; `OwnershipLost`; `SlotMismatch`; `StaleVersion(actual)`; `InvalidState(state)`; `DatabaseBusy`; `CommitAmbiguous` |
| `apply_failure` | `DefinitelyFailed(OperationView)`; `UncertainRecorded(OperationView, recovery_at)` | `AlreadyTerminal(OperationView)`; `OwnershipLost`; `SlotMismatch`; `InvalidState(state)`; `DatabaseBusy`; `CommitAmbiguous` |
| `reconcile_late_success` | `Reconciled(OperationView)`; `AlreadySucceeded(OperationView)` | `AcknowledgedDiscard`; `FingerprintMismatch`; `RunnerMismatchDiscard`; `StaleVersionDiscard`; `InvalidStateDiscard`; `DatabaseBusy`; `CommitAmbiguous` |
| `recover_slot` | `SlotFree`; `LiveQueued`; `LiveRunning`; `CancelledExpiredQueued`; `MarkedOutcomeUnknown` | `InconsistentSlot`; `OwnershipMismatch`; `DatabaseBusy`; `CommitAmbiguous` |
| `acknowledge_unknown` | `Acknowledged(OperationView)`; `AlreadyAcknowledged(OperationView)` | `NotUnknown(current_state)`; `NotFound`; `DatabaseBusy`; `CommitAmbiguous` |
| `create_conversation`, `delete_expired_idempotency` | Method-specific success | `DatabaseBusy`; `CommitAmbiguous`; method-specific constraint/race result |
| Milestone 3 retry creation | Defined in Milestone 3 | Includes `RetrySourceNotEligible` when the retry trigger rejects the source owner, conversation, state, blocking status or a second direct retry |

`DatabaseBusy` means the write lock was not obtained and no write occurred. `CommitAmbiguous` means the transaction reached a commit attempt whose durable outcome is unknown. Every write contract includes it; callers never blindly repeat the write and instead reread from a fresh connection.

The repository detects commit ambiguity by separating the transaction body from the commit: the body returns its result object, then any exception raised while leaving the durable atomic block is treated as a commit-phase failure. The repository closes that connection before any caller reread.

The retry trigger can be exercised in Milestone 2 only through schema-level fixtures. Milestone 3's retry method must map its trigger rejection to `RetrySourceNotEligible`; it must never surface a raw database error.

> **Ownership fencing correction**
>
> `claim_running` accepts the current `runner_id`. It does not accept a replacement-owner parameter. `apply_success` receives a `ClaimedOperation` carrying `runner_id`, and `apply_failure` receives the same claimed token. Every ordinary completion condition includes both operation ID and runner ID.

`AlreadySucceeded` always returns the previously committed durable operation and its two committed messages. It ignores the newly supplied `output_text`, never overwrites history, and may record only content-free mismatch diagnostics.

`RetainedResult` is the original immutable `ClaimedOperation` plus the successful output text. Late reconciliation requires its operation ID, fingerprint version/hash, expected version and `runner_id` all to match the durable operation. It never reads, acquires, releases or updates the global slot.

Expired adoption has a complete durable-state mapping:

- Expired QUEUED is atomically changed to CANCELLED, unblocked and released, then returns `ExpiredResubmit`.
- Existing CANCELLED returns `ExpiredResubmit`.
- RUNNING, SUCCEEDED, FAILED and OUTCOME_UNKNOWN return `NotQueued(view)`.
- No `NotQueued` or `ExpiredResubmit` result permits a provider call.

Under the status/block CHECK constraints, a non-blocking OUTCOME_UNKNOWN is necessarily acknowledged. `NoLongerBlockingDiscard` is therefore merged into `AcknowledgedDiscard`; any other state is `InvalidStateDiscard`.

The provider may be called only after `claim_running` returns `Claimed(claimed_at)`, or after `CommitAmbiguous(operation_id, runner_id, claimed_at)` is resolved by a fresh-connection reread confirming RUNNING with the same operation ID, the same `runner_id`, and `provider_started_at_us == claimed_at` from that result. Any other reread result, including QUEUED, CANCELLED, a different runner or a different start time, forbids the provider call. An unconfirmed claim never crosses the provider boundary.

### Failure classification at the repository boundary

- Definite pre-provider failure: QUEUED → FAILED, clear block and release matching reservation atomically.
- Definite provider failure: RUNNING → FAILED, clear block and release matching execution slot atomically.
- Uncertain provider outcome: record a sanitized category while leaving the operation RUNNING, blocked and slot-owned. Return the durable recovery time; do not release or replay.
- Successful provider output with failed persistence: retain the immutable result only in the service process; durable operation remains RUNNING. `retry_save` invokes `apply_success` again after rereading state.
- A generic `ModelRequestError` is not enough to prove definite failure. Provider adapter classification precedes repository application.
- A definite or uncertain failure result that arrives after recovery has changed RUNNING to OUTCOME_UNKNOWN cannot change that state: the conditional RUNNING update affects zero rows, the repository returns `AlreadyTerminal(OUTCOME_UNKNOWN)`, and it does not touch the slot.
- On guarded late success, retain `failure_category` as historical metadata rather than clearing it. The final status and committed messages are authoritative; the retained category explains the earlier uncertainty.

## 4. Transactions

### Transaction boundaries

| Method | Reads and conditional writes | Row-count rule | Atomic scope |
|---|---|---|---|
| `create_conversation` | `BEGIN IMMEDIATE`; validate owner/role; INSERT conversation. | Insert exactly 1. Zero/constraint maps to typed conflict; >1 impossible/integrity failure. | Conversation only. |
| PREPARE | `BEGIN IMMEDIATE`; after the lock is acquired read the `Clock` once; authorize; look up key digest; return Existing/KeyBindingMismatch if found; check version; recover slot if eligible; check blocker; in a savepoint insert operation, insert key binding, then conditionally acquire slot. | Operation insert 1; key insert 1; slot update 1. Under `BEGIN IMMEDIATE` no concurrent writer can insert the same key, so a unique-constraint error here is an integrity violation, not a race. | Operation, idempotency, block and acquisition commit together. Any recovery transition is committed even if the final result is `ConversationBlocked`, `ModelBusy` or an integrity violation in the savepoint. |
| Adopt QUEUED | `BEGIN IMMEDIATE`; after lock read the `Clock` once; read actor-owned operation + slot. If expired, conditionally QUEUED→CANCELLED, unblock and release; otherwise verify ownership and conditionally transfer operation runner then slot runner. | Every conditional operation/slot update exactly 1. Zero means ownership/state race and rolls back both. Existing same runner is verified without rewrite. | Adoption transfer or expiry cancellation/unblock/release commits atomically. |
| Claim RUNNING | `BEGIN IMMEDIATE`; after lock read the `Clock` once as `claimed_at`; reread QUEUED operation + slot; if expired perform cancellation path; otherwise conditionally update operation to RUNNING and slot reservation→lease using that same timestamp. | Operation 1 and slot 1. Zero is explicit state/ownership/expiry race; never success. | Claim and fixed deadline/lease are atomic. Provider execution waits for confirmed commit. |
| Pre-provider failure | `BEGIN IMMEDIATE`; conditional QUEUED→FAILED by ID+runner; clear block; conditional slot release by ID+runner. | Operation 1 and slot 1. Zero triggers reread and typed lost ownership/state result. | Failure, unblock and release atomic. |
| Ordinary success APPLY | `BEGIN IMMEDIATE`; after lock read the `Clock` once; reread claimed operation/slot and observed `next_sequence`; perform the exact conversation update below; insert user at observed and assistant at observed+1 from trusted values; conditionally RUNNING→SUCCEEDED by ID+runner; release slot. | Conversation 1, messages 2, operation 1, slot 1. Any mismatch rolls back all. | History, version/sequence, terminal state, unblock and release atomic. |
| Definite provider failure | `BEGIN IMMEDIATE`; verify claimed operation + slot; conditional RUNNING→FAILED; clear block; release slot. | Operation 1 and slot 1; zero maps after reread, >1 integrity failure. | Failure, unblock and release atomic. |
| Uncertain provider outcome | `BEGIN IMMEDIATE`; verify claimed operation remains RUNNING and matching slot remains owned; set sanitized uncertainty category only. | Operation 1. Slot is verified, not released. Zero is ownership/state race. | Operation remains blocked/RUNNING until recovery eligibility. |
| Late reconciliation | `BEGIN IMMEDIATE`; verify retained token's operation/runner/fingerprint/version; verify OUTCOME_UNKNOWN, unacknowledged and blocking; perform the same exact conversation update and message inserts; mark SUCCEEDED/unblock while retaining historical failure category. | Conversation 1, messages 2, operation 1. Zero is a typed discard. Never inspect/update slot. | History/version/state atomic; slot deliberately excluded. |
| Expired QUEUED recovery | `BEGIN IMMEDIATE`; verify status QUEUED, no provider start, reservation <= now and matching slot; mark CANCELLED/unblock; release slot. | Operation 1 and slot 1. Any zero rolls back and rereads. | Cancellation, unblock and release atomic. |
| Expired RUNNING recovery | `BEGIN IMMEDIATE`; require deadline <= now AND lease <= now with matching slot; mark OUTCOME_UNKNOWN; release slot. | Operation 1 and slot 1. Block remains true. Zero is live/race/inconsistency. | Unknown classification and release atomic; conversation unchanged. |
| Acknowledge unknown | `BEGIN IMMEDIATE`; actor-owned conditional OUTCOME_UNKNOWN + acknowledged NULL + blocking true update. | Operation exactly 1. Zero rereads to `AlreadyAcknowledged`, `NotUnknown`, `NotFound`, or race. | Set `acknowledged_at` and clear block; no slot or history change. |

### PREPARE control flow and recovery commit rule

PREPARE uses this exact order:

1. Authorize the actor against the conversation. Cross-owner and nonexistent objects both return `NotFound`.
2. Look up `(owner_id, key_digest_version, key_digest)`.
3. If found, compare the complete stored binding. Return `Existing` for an exact match or `KeyBindingMismatch` for any changed owner, conversation, operation type, expected version, fingerprint version/hash, provider or model. This lookup precedes blocker and slot checks, so the same key returns the original operation while it is QUEUED, RUNNING or SUCCEEDED.
4. Check the conversation version. Return `StaleVersion` here, before any recovery write.
5. Inspect and, if eligible, recover the global slot.
6. Check the conversation blocker. Return `ConversationBlocked` here.
7. Check the slot state left by step 5. Return `ModelBusy` if it is still owned by a live operation. No row has been inserted yet, so the global live-operation unique index is never used as a busy signal.
8. Inside a non-durable savepoint (`transaction.atomic()` nested in the durable block): insert the operation, insert the idempotency binding, then conditionally acquire the free slot.

The operation is inserted first because the idempotency-key trigger validates against it. Slot acquisition comes last so the slot never references an operation that does not yet exist.

Business outcomes are returned values, never exceptions raised from inside `transaction.atomic(durable=True)`. `Existing`, `KeyBindingMismatch` and `StaleVersion` are always decided before step 5, so they never follow a recovery write. `ConversationBlocked` and `ModelBusy` are the only business rejections that can follow a recovery transition, and that recovery is committed with them. `ConversationBlocked` contains the blocking operation ID and status, allowing OUTCOME_UNKNOWN acknowledgement.

`BEGIN IMMEDIATE` serializes all writers, so the step 2 lookup and step 7 slot check cannot be invalidated by a concurrent writer before step 8. An `IntegrityError` or unexpected row count in step 8 is therefore an integrity violation, not a race. The repository rolls back only the step 8 savepoint, lets the durable transaction commit any recovery already performed, then raises a redacted repository integrity error after the commit. An integrity violation inside the recovery step itself, or an unexpected exception before step 8, rolls back the whole transaction.

### Exact ordinary APPLY and late-reconciliation arithmetic

After all operation, runner, slot, fingerprint and expected-version guards pass, read `observed = conversation.next_sequence` and require one row from:

```sql
UPDATE conversation
SET version = version + 1,
    next_sequence = next_sequence + 2,
    updated_at_us = ?
WHERE id = ?
  AND owner_id = ?
  AND version = :expected
  AND next_sequence = :observed
  AND archived_at_us IS NULL;
```

Then, in the same transaction:

- Insert the user message at sequence `:observed`, using `operation.request_content` exactly—never caller-supplied content.
- Insert the non-blank assistant output at sequence `:observed + 1`.
- Link both messages to the same operation ID.
- Conditionally mark that operation SUCCEEDED and non-blocking.
- For ordinary APPLY only, conditionally release the matching operation/runner slot. Late reconciliation never reads or changes the slot.

The conversation update must affect exactly one row, both inserts must each affect one row, and the operation and ordinary slot updates must each affect exactly one row. Any failure rolls back the update and both inserts. The same arithmetic applies to late reconciliation, except its retained-result guards replace ordinary slot ownership and the slot is excluded.

Durable message invariants are:

- Every SUCCEEDED model operation has exactly one user and one assistant message linked to it, in adjacent sequence order.
- FAILED, CANCELLED and OUTCOME_UNKNOWN operations have no linked messages.
- Duplicate APPLY returns the already committed operation and messages without comparing or writing the newly supplied output.

### Reservation expiry during context assembly

#### Required claim behavior

- Adoption succeeds only while `reservation_expires_at` is later than the adoption transaction's post-lock `Clock` reading.
- Context assembly runs without a transaction.
- Claim checks expiry again using the claim transaction's single post-lock `Clock` reading.
- If still QUEUED but expired, claim atomically marks CANCELLED, clears `blocks_conversation`, and conditionally releases the slot using operation ID and recorded/current runner.
- Return `ExpiredResubmit`; do not call the provider.
- If recovery already cancelled it, return the same `ExpiredResubmit` from durable state.

#### Timing requirement

- Reservation duration must exceed measured worst-case authorized context loading, budgeting, request construction, scheduling jitter and the claim transaction's busy budget.
- Set the value as a tested configuration constant, not a database default.
- A context build approaching the reservation limit should abort before claim rather than gambling on expiry.
- Do not renew a QUEUED reservation silently; resubmission creates a new operation/key after cancellation.

> **Zero rows is never success**
>
> First roll back the enclosing transaction, then reread through an ownership-scoped query and return the method's typed race result. Do not continue later writes after one conditional update returns zero. More than one affected row is an invariant breach, must roll back, and should raise a redacted repository integrity error.

If an exception occurs at commit, do not assume rollback or success. Return `CommitAmbiguous` and reconcile from a fresh connection. Never blindly replay the write. `DatabaseBusy` is reserved for failure to acquire the write lock before any mutation.

### No long lock windows

```text
PREPARE transaction
  commit QUEUED reservation

adopt transaction
  commit runner transfer if required

NO TRANSACTION
  load standing context
  budget and construct provider-neutral request

claim transaction
  commit RUNNING + deadline + lease

NO TRANSACTION
  exactly one provider request

APPLY / failure transaction
  commit final durable transition
```

## 5. State machine

### Implementable transition matrix

| Transition | Trigger | Preconditions | Database and slot effects | Conversation | Provider request? |
|---|---|---|---|---|---|
| none → QUEUED | PREPARE | Authorized owner; current version; no blocker; slot free/recoverable; new or matching idempotency binding. | Create operation/key; acquire reservation slot; block conversation. | Version unchanged. | No |
| QUEUED → QUEUED | Adopt after PREPARE process restart | Unexpired; operation and slot match recorded runner; caller authorized. | Transfer operation and slot runner together. | Block/version unchanged. | No |
| QUEUED → CANCELLED | Expired adoption, execute, claim, startup or inline recovery | Reservation expired; `provider_started` absent; matching slot. | Set finished; unblock; release reservation slot. Return `ExpiredResubmit`. | Version unchanged. | No |
| QUEUED → FAILED | Definite context/budget/config failure | Current reservation owner; provider boundary not crossed. | Record sanitized category; finish; unblock; release slot. | Version unchanged. | No |
| QUEUED → RUNNING | Claim | Unexpired; current runner owns operation and slot. | Set `provider_started`, deadline, fixed lease; slot reservation becomes lease. | Block remains; version unchanged. | After commit, at most one |
| RUNNING → SUCCEEDED | Ordinary APPLY | Claimed runner/slot; fingerprint; expected version; usable retained response. | Insert user+assistant; advance sequence/version; finish/unblock; release slot. | Version +1. | No |
| RUNNING → FAILED | Definite provider failure | Claimed runner and slot; adapter safely proves definite category. | Record category; finish/unblock; release slot. | Version unchanged. | No |
| RUNNING → RUNNING | Uncertain result or save failure | Claim remains owned; no safe terminal commit. | Optional sanitized category only; retain block and slot. | Version unchanged. | Never automatically |
| RUNNING → OUTCOME_UNKNOWN | Recovery | Both execution deadline and fixed lease expired; matching slot. | Finish as unknown; retain block; release slot. | Version unchanged. | No |
| OUTCOME_UNKNOWN → SUCCEEDED | Automatic retained late-response reconciliation | Unacknowledged; still blocking; exact operation/runner/fingerprint/version; response held by the original process token. | Insert turn; advance sequence/version; finish/unblock. Slot untouched. | Version +1. | No |
| OUTCOME_UNKNOWN → OUTCOME_UNKNOWN | Owner acknowledges warning | Authorized; unacknowledged; blocking. | Set acknowledged; clear block. Preserve all history/metadata. | Version unchanged. | No |

### Provider response racing recovery

#### APPLY obtains writer first

- Ordinary APPLY verifies RUNNING ownership and slot.
- It atomically commits SUCCEEDED, messages, version and release.
- Recovery then observes a free slot/terminal operation and does nothing.

#### Recovery obtains writer first

- If both times expired, recovery commits OUTCOME_UNKNOWN and releases slot.
- Ordinary APPLY's conditional RUNNING update gets zero rows and rolls back.
- The service attempts guarded late reconciliation with its retained response; it never touches the slot.

#### Acknowledgement races reconciliation

- SQLite serializes the two short writers.
- If reconciliation wins, acknowledgement returns `NotUnknown` because state is SUCCEEDED.
- If acknowledgement wins, reconciliation's acknowledged/block guard gets zero rows and the late response is discarded.

> **Recovery is classification, not cancellation**
>
> Releasing an expired RUNNING slot permits new application work but does not prove OpenAI stopped. A stale process may return later; fencing and reconciliation guards protect history, while rare provider overlap and a second charge remain possible after explicit retry.

### Retry and late-result interaction

An explicit retry is a new `none → QUEUED` operation with `retry_of` set; it is never a transition of the source row. An acknowledged OUTCOME_UNKNOWN source remains historical and non-blocking. If its old provider result arrives after the retry is QUEUED, RUNNING or terminal, the source's acknowledgement causes `AcknowledgedDiscard`; reconciliation cannot target the retry because it is keyed by source operation ID and runner, not by the potentially identical fingerprint. The old result does not inspect or alter the retry's slot, the conversation, or the retry operation.

A late definite or uncertain failure after recovery also cannot rewrite OUTCOME_UNKNOWN: ordinary failure application is conditional on RUNNING and matching runner/slot, so it returns `AlreadyTerminal(OUTCOME_UNKNOWN)` with no slot or conversation change.

## 6. SQLite operations

### SQLite runtime configuration

| Control | Plan | Verification |
|---|---|---|
| Filesystem | Fixed path `data/max_smart.sqlite3` on local APFS, outside iCloud, Dropbox, network mounts and shared-host filesystems. | Resolve configured path; reject symlink DB/parent; assert regular local file policy at startup; verify git ignore coverage. |
| Permissions | Private directory `0700`; database, WAL, SHM and backups `0600`, subject to platform support. | Create under restrictive umask; inspect modes after connection creates sidecars. |
| Foreign keys | `PRAGMA foreign_keys=ON` on every connection. | Read back value; fail startup if not 1. |
| Journal | `PRAGMA journal_mode=WAL` during controlled initialization/migration. | Read back `journal_mode` on every process startup; fail closed if not `wal`. |
| Durability | `PRAGMA synchronous=FULL` on every connection. | Read back numeric/effective value and deployment-test exact Python/SQLite build. |
| Locking | Django `OPTIONS["transaction_mode"]="IMMEDIATE"` plus top-level `transaction.atomic(durable=True)` for every repository write. Nested durable write units are forbidden. Never use `SELECT FOR UPDATE`. | SQL trace/integration tests prove each top-level write starts `BEGIN IMMEDIATE`, is not nested, and remains short. |
| Busy timeout | 5,000 ms initial SQLite `busy_timeout`; recurring exhaustion is an architecture signal. | Contention tests with independent connections/processes. |
| Connections | One Django ORM repository implementation serves Terminal and Django. Terminal sets a database-only settings module and calls `django.setup()` during composition, uses the same database settings and migrations, scopes connections to units of work, and calls `close_old_connections()`/closes deterministically. No connection is shared across threads or inherited across fork. | Thread/process tests and connection-created hook tests. |

### Shared settings, dependency and test-runner boundaries

- **Database-only settings module.** One module defines `DATABASES` (path, `transaction_mode`, timeout, connection-created pragma hook), `INSTALLED_APPS` limited to the repository app, `USE_TZ=True`, and nothing web-related. Terminal uses this module directly. Future web settings import it rather than redefining database behavior, so both interfaces share one database configuration.
- **Terminal needs no web configuration.** It requires no `SECRET_KEY`, middleware, URLs, templates, static files, sessions or authentication settings, and it does not read `.env` for database configuration.
- **No automatic migration.** Neither Terminal nor Django startup runs `migrate`. If the migration state is not exactly the expected latest state, startup verification fails closed with a safe message telling Adam to run the explicit migration command after a verified backup.
- **Dependency approval.** Django is not currently a project dependency. M2.1 must not begin until Adam explicitly approves adding Django 5.2 LTS at an exact patch version to `requirements.txt` and the hashed `requirements-lock.txt`. No other new runtime dependency is planned for Milestone 2.
- **Test-runner coexistence.** The existing Milestone 1 command, `./.venv/bin/python -B -m unittest discover -s tests -v`, stays unchanged and must keep running its 77 tests without importing Django. Repository tests live in a separate top-level test package with its own command, `./.venv/bin/python -B -m unittest discover -s tests_repository -v`. That package's initializer selects the database-only test settings and calls `django.setup()`. Django's `manage.py test` runner is not used because it creates its own test database and flushes between tests. Both commands are required for Milestone 2 completion.

### Git and local-data protection

Implementation step M2.1 must add these protections before creating the database:

```gitignore
data/*.sqlite3*
data/*-wal
data/*-shm
data/backups/
data/.backup-*
data/.restore-*
data/.migration-rehearsal-*
```

A startup/preflight and CI check runs `git check-ignore --quiet` separately against `data/max_smart.sqlite3`, representative WAL/SHM paths, `data/backups/`, and temporary backup/restore/migration-rehearsal names. Interpret each result strictly:

- Exit `0`: the path is ignored. This is the only passing result.
- Exit `1`: the path is not ignored. Fail closed.
- Exit `128`, any other code, a timeout, or Git being unavailable: fail closed. An inconclusive check is never treated as a pass.

`git check-ignore` reports tracked paths as not ignored by default, and the check is never run with `--no-index`. As a second guard, `git ls-files` for the protected `data/` patterns must return no paths. If any database, sidecar, backup or temporary file is already tracked, initialization fails closed with a safe message to untrack it; the preflight never runs Git commands that change the index.

Every production database artifact is created inside the work tree at a protected path:

- live database and sidecars: `data/max_smart.sqlite3`, `-wal`, `-shm`;
- online backup temporaries and published backups: `data/backups/` or `data/.backup-*`;
- restore candidates: `data/.restore-*`;
- migration rehearsals: `data/.migration-rehearsal-*`.

Automated-test databases, templates and sidecars are created only in a private temporary directory outside the work tree. The test harness asserts the resolved temporary path is not inside the repository before creating any file.

The database, sidecars and backups must never be staged. File-level backup tools that do not provide a SQLite-aware snapshot must exclude the entire live `data/` database area.

### Bounded busy and commit-ambiguity policy

- Let SQLite's 5-second busy timeout perform the bounded wait. Do not stack a long application retry loop on top.
- Before provider execution, PREPARE/adopt/claim may make at most one additional fresh transaction attempt after a short randomized delay, and must reread all state. This is transaction retry, never provider retry.
- After provider execution, never blindly replay a write after an exception near commit. Open a fresh read connection and reconcile operation, messages, version and slot first.
- If the operation is already SUCCEEDED, return the durable result. If it is still RUNNING and the process retains the immutable response, surface save-failure and permit persistence-only retry. If state is OUTCOME_UNKNOWN, use guarded reconciliation.
- `DatabaseBusy` and `CommitAmbiguous` are sanitized typed repository outcomes. SQL parameters, message content, paths and provider details must not be logged.
- The repository reads its injected `Clock` exactly once per write transaction, only after `BEGIN IMMEDIATE` has acquired the write lock, so a busy wait cannot consume a reservation before the transition's authoritative timestamp is chosen. Callers never pass timestamps.
- Generate `runner_id` from at least 128 random bits once per process instance; never use a PID or thread ID.
- Durable expiry timestamps use UTC wall time. Milestone 3's adapter separately enforces the provider execution deadline with a monotonic clock. The fixed lease margin must tolerate bounded wall-clock correction; deployed clock-step tests set the final margin.

### Migrations and schema versioning

- Use Django migrations as the only schema-version authority. Milestone 2 creates the minimal Django project/settings needed for models without adding views, middleware or web behavior.
- Initial migration creates role, conversation, operation, message, idempotency and slot tables plus `authorized_identity` if the approved section-7 schema is implemented together.
- A data migration seeds role `max-smart` and the single free `global_model_slot` row `id=1`. Reverse migration must refuse if application data exists rather than silently deleting history.
- Express CHECKs and partial unique indexes as Django `Meta` `CheckConstraint` and conditional `UniqueConstraint` wherever possible, because Django's SQLite table rebuild recreates model-declared constraints. Use `RunSQL` only for triggers and anything Django cannot express.
- **Trigger-recreation rule.** On SQLite, Django applies many schema changes by rebuilding the table (create new table, copy, drop old, rename), which silently drops every trigger attached to that table. Any migration that alters a table carrying triggers must, in the same migration and after the rebuild, recreate every trigger on that table from the canonical trigger definitions. Reviewers check each migration for this, and the post-migration manifest comparison fails closed if any trigger, partial index or CHECK is missing or different.
- Every repository and concurrency test uses the fresh-database harness described in section 8, with a real on-disk SQLite database. Never use Django `TestCase`, `TransactionTestCase`, `flush`, or only an in-memory database: `TransactionTestCase` flushes tables between tests, which conflicts with the slot `BEFORE DELETE` trigger and would remove migration-seeded rows. Test forward migration, rollback in a disposable database, and simulated table-rebuild migrations with and without trigger recreation.
- Never run migrations automatically on Terminal or Django startup. Deployment takes a verified online backup before migration and runs migrations as an explicit step.
- Keep Gunicorn `preload_app` off. No startup verification may open SQLite in the master process before worker fork; alternatively, every inherited connection must be explicitly closed after fork before use.

### Fail-closed startup and pre-write verification

Each Terminal or Django process starts in this exact order:

1. **Verify, read-only.** Check the path, permissions and Git preflight, pragmas, migration state, schema manifest and every invariant listed below. Perform no writes.
2. **Recover once.** Only if step 1 passes, call `recover_slot()` once. This is the process's first write and uses the normal fenced recovery rules.
3. **Enable writes.** Only after recovery returns a typed non-error result (recovered, nothing to recover, or `LiveRunning`) does the repository accept other writes.

If verification fails, recovery never runs. If recovery returns `DatabaseBusy`, `CommitAmbiguous` or an integrity error, writes stay disabled until a later startup attempt verifies again.

The step 1 checks are:

- Verify exactly one `global_model_slot` row and the slot-delete trigger.
- If occupied, verify its operation ID and runner match the operation, and its reservation/lease timestamp equals the authoritative operation timing field for the operation's state.
- Verify at most one QUEUED or RUNNING operation globally.
- Verify every SUCCEEDED model operation has exactly two linked messages, one user and one assistant, in adjacent sequence order; verify every FAILED, CANCELLED and OUTCOME_UNKNOWN operation has none.
- Run `PRAGMA foreign_key_check` and require no rows.
- Run `PRAGMA quick_check` and require `ok`.
- Compare `sqlite_master` against the manifest of expected triggers, partial indexes and table definitions.

Any inconsistency disables repository writes and returns a safe operational error. Startup never auto-repairs. Lightweight checks repeated before every write are: pragma values, migration/schema version, singleton slot row count and expected trigger/index manifest version. The more expensive full message/integrity scans run at startup, after migration, after restore and during scheduled integrity verification.

### Backup and restore

#### Online backup

- Open a read source connection with required pragmas and use Python `sqlite3.Connection.backup` (SQLite online backup API) into a new private temporary database at a git-ignored `data/.backup-*` path or inside `data/backups/`, never elsewhere in the work tree.
- Run `quick_check`/`integrity_check` and verify schema migration version on the backup.
- `fsync` as supported, `chmod 0600`, then atomically publish into the approved encrypted destination and retention set.
- Record only backup timestamp, schema version, size and integrity result; never conversation content in logs.

#### Restore drill

- Stop application writers; preserve the damaged database and sidecars for diagnosis.
- Restore into a new private git-ignored `data/.restore-*` path, reject symlinks, run `integrity_check` and migration compatibility checks.
- Open read-only repository checks for conversations, sequence continuity, operation constraints and singleton slot.
- Atomically activate only after verification; then follow the normal startup order (verify, recover once, enable writes), never a blanket RUNNING rewrite.

> **Never copy only the live database file**
>
> In WAL mode, committed state may still reside in the WAL. A raw copy of the main file can omit committed transactions or produce an inconsistent backup. Network and cloud-synchronized filesystems also violate WAL's same-host shared-memory assumptions and can reorder or conflict with database sidecar updates.

### Request-content retention

`request_content` is required while an operation is live, blocking, retryable, or inside the idempotency/recovery window. V1 retains it with the operation because retries, auditability and late reconciliation require exact binding. Before any future purge of FAILED or CANCELLED request content, a separate privacy/retention decision must define the minimum age, preserve the fingerprint and failure metadata, exclude blocking operations, and account for backups. Milestone 2 implements no automatic content purge.

## 7. JSON migration

### One-time JSON-to-SQLite migration

> **Designed now, executed in Milestone 4**
>
> Milestone 2 must leave the repository ready for legacy messages, but it must not read or modify `data/current_session.json`. The importer is a separate, explicitly invoked Milestone 4 command.

| Phase | Required behavior | Failure behavior |
|---|---|---|
| Preflight | Stop Terminal; resolve only the documented session path; `lstat` parent/file; reject symlinks and non-regular files; open read-only; enforce a 64 MiB source-file cap; retain original bytes and SHA-256. | Abort without creating/importing rows; never print content. |
| Validate | Decode strict UTF-8; reject duplicate JSON object keys and strings containing lone Unicode surrogates; require top-level object and messages list; each message is an object with role user/assistant and string content. Preserve legacy empty assistant messages. Extra keys are ignored consistently with engine normalization. | Report generic indexed validation category, not message content; original remains untouched. |
| Plan | Assign one new conversation, sequence 1..N in exact array order, preserve role/content byte-for-character after JSON decoding, `operation_id NULL`, `version 0`, `next_sequence N+1`. | No writes yet. |
| Deduplicate | Look up `legacy_import` by `source_kind`. Same source hash returns existing verified result; different hash requires explicit operator decision and never merges automatically. | Abort on conflicting prior import. |
| Import transaction | `BEGIN IMMEDIATE`; create conversation; bulk insert ordered messages; insert `legacy_import` ledger with source hash, count and ordered-content hash; commit. | Any failure rolls back all SQLite rows. Original JSON is unchanged. |
| Verify | Fresh connection compares count, sequence continuity, roles, exact content and ordered-content hash; runs FK check and integrity check. | Mark import unverified; retain DB and JSON for diagnosis; Terminal remains on JSON. |
| Accept | While Terminal remains stopped, reread and re-hash the source; require it to equal the ledger's `source_sha256`; record `verified_at`, then obtain Adam's explicit acceptance before setting `accepted_at` and switching Terminal composition to SQLite. | On hash mismatch, abort and require a fresh import or explicit restart of the process. No automatic cutover. |
| Rollback | Before Terminal cutover, delete the imported conversation and ledger in one transaction after confirming no non-import operations reference it, or discard the new DB entirely. | Never delete/overwrite the JSON. After cutover, restore via reviewed backup rather than destructive ad hoc rollback. |

### Existing database handling

- An existing database is not proof of an existing import. Check migration version and the dedicated `legacy_import` ledger.
- If no import ledger exists, import into a new conversation only after verifying no current active conversation is designated as the migration target.
- If the same verified source hash exists, return its conversation ID without inserting anything.
- If a different source hash is bound to `source_kind=current_session_json`, stop. Do not append, overwrite, or guess which history is authoritative.
- Take an online database backup before import. Preserve its location/identifier without exposing content.
- Before Milestone 3 or 4, bind Terminal to the same approved local `owner_id` used by the web service. Milestone 2 tests use an explicit fixture identity and never infer ownership from the OS username.

> **Rollback boundary**
>
> The safe rollback period ends when either interface begins writing to SQLite. At that point the JSON is a preserved pre-migration artifact, not a writable fallback, because dual writes would create divergent histories.

After accepted cutover, the composition root refuses to initialize the JSON repository. If SQLite is unavailable or fails verification, Terminal fails closed with a safe error; it never silently falls back to JSON.

## 8. Test matrix

### Automated test matrix

No test below is claimed to pass; these are required implementation tests.

| Test | Type | On-disk DB | Extra connection/process | Expected durable state and typed result |
|---|---|---:|---|---|
| Fresh-database harness (N1) | Integration (fresh-database harness) | Yes, per test | No | Each test receives a new file copied from the migrated template; the singleton slot and seeded role exist at test start. No `flush`, truncation or slot deletion is ever issued. Test files sit outside the work tree and are removed with their sidecars at teardown. |
| Status/CHECK matrix | Integration (fresh-database harness) | Yes | No | Every invalid status/block/timing/acknowledgement/content-limit combination is rejected; transaction leaves no row. Valid combinations persist. |
| Cross-table insert triggers | Integration | Yes | No | Wrong operation owner, message conversation, idempotency binding, or retry scope raises an integrity error and leaves no row. |
| Message immutability (N5) | Integration | Yes | No | Any `UPDATE` of any message column, including content, role, sequence and links, on normal and imported rows raises an integrity error; the row is unchanged. |
| Linear retry chains (N3) | Schema-fixture integration | Yes | No | Fixtures insert rows directly because retry creation is Milestone 3 work. With O FAILED, R1 (`retry_of=O`) inserts. With R1 then FAILED, R2 (`retry_of=R1`) inserts. A second retry of R1 is rejected by `UNIQUE(retry_of)`. Sources that are QUEUED, RUNNING, SUCCEEDED, unacknowledged OUTCOME_UNKNOWN, still blocking, or in another conversation or owner are rejected. Acknowledged OUTCOME_UNKNOWN and CANCELLED sources are accepted. |
| PREPARE insertion order (N9) | Integration with SQL trace | Yes | No | Statement trace shows operation insert, then idempotency insert, then slot acquisition, all inside the step 8 savepoint; the idempotency trigger passes without deferral. |
| Immutable operation identity | Integration | Yes | No | Updating any protected identity field raises an integrity error; original row is unchanged. |
| Singleton and live uniqueness | Integration | Yes | No | Slot deletion, a second slot, second enabled identity, second conversation blocker, second live operation, or second retry of one source is rejected. |
| PREPARE recovery commits despite blocker (H1) | Integration | Yes | Fresh reread connection | Start with expired RUNNING in conversation C. `prepare` returns `ConversationBlocked(id, OUTCOME_UNKNOWN)`. A new connection confirms OUTCOME_UNKNOWN, still blocking and unacknowledged, a free slot, and no new operation or key. `acknowledge_unknown` succeeds. The next `prepare` returns `Prepared`. |
| PREPARE recovery boundaries (N4) | Integration | Yes | Fresh reread connection | `StaleVersion`, `Existing` and `KeyBindingMismatch` are returned before recovery and leave an expired operation unrecovered. `ConversationBlocked` and `ModelBusy` after a recovery transition leave the recovery committed. No new operation or key is created in any rejection case. |
| PREPARE integrity violation after recovery (N4) | Fault-injection integration | Yes | Fresh reread connection | An injected `IntegrityError` in the step 8 savepoint rolls back only the savepoint. A fresh reread shows the recovery committed, no new operation or key, and an unchanged slot. The caller receives a redacted repository integrity error, never `Existing` or `KeyBindingMismatch`. |
| Duplicate idempotency states | Integration | Yes | Optional second connection for race | Exact same key/binding returns `Existing` with original ID while QUEUED, RUNNING or SUCCEEDED. Changed content, version, conversation, provider or model returns `KeyBindingMismatch`. |
| Concurrent same-key PREPARE | Integration/concurrency | Yes | Two processes with barrier | `BEGIN IMMEDIATE` serializes both. The first returns `Prepared`; the second sees the committed key at step 2 and returns `Existing` (same binding) or `KeyBindingMismatch` (changed binding). No `IntegrityError` is raised. |
| Fingerprint vectors | Unit | No | No | Fixed lowercase SHA-256 digests match for ASCII, Unicode, trailing whitespace and CRLF, with owner ID encoded as decimal ASCII. Every bound-field change changes the digest; no normalization occurs. |
| Idempotency validation/cleanup | Unit + integration | Yes for cleanup | No | Low-entropy, padded, non-alphabet and non-canonical base64url keys, including non-zero trailing bits, are rejected. The stored digest equals SHA-256 of the decoded bytes, not of the text. Cleanup deletes only expired keys whose operations are terminal and non-blocking. |
| Global slot race | Multiprocess integration | Yes | Two processes with barrier | Exactly one PREPARE returns `Prepared`; the other returns `ModelBusy`; one live operation and one occupied slot remain. |
| Adoption state mapping | Integration | Yes | No | Same runner returns `AlreadyOwned`; transfer updates both rows; expired QUEUED becomes CANCELLED/unblocked/free and returns `ExpiredResubmit`; repeat returns `ExpiredResubmit`; RUNNING/SUCCEEDED/FAILED/OUTCOME_UNKNOWN return `NotQueued(view)` with no provider call. |
| Claim expiry during context assembly | Integration | Yes | No | Exact boundary uses valid while `expires > now`, expired at `<=`; expiry commits CANCELLED/unblock/release and returns `ExpiredResubmit`; provider mock remains uncalled. |
| Clock read after lock (N2) | Integration with fake clock | Yes | Second connection holds write lock | While the claim waits on the lock, the holder advances the fake clock past the reservation expiry, then releases. Claim returns `ExpiredResubmit` and the provider mock is uncalled. In the mirror case the clock stays before expiry and `Claimed.claimed_at` equals the advanced value. The fake clock records exactly one read per write transaction and none before `BEGIN IMMEDIATE` succeeds. |
| Claim commit ambiguity | Fault-injection integration | Yes | Fresh reread connection | Test both "commit happened" and "did not happen." `CommitAmbiguous` carries `claimed_at`. The provider is allowed only when the reread shows RUNNING with the same runner and `provider_started_at_us == claimed_at`. The unconfirmed case never calls the provider and cannot become falsely CANCELLED-after-provider. |
| Exact ordinary APPLY arithmetic | Integration | Yes | No | Version +1, `next_sequence` +2, user sequence `n`, assistant `n+1`, both linked to operation, user content exactly equals `operation.request_content`, operation SUCCEEDED/unblocked and slot free. |
| APPLY sequence/version drift | Integration | Yes | No | Mismatched expected version or observed sequence returns `StaleVersion`/typed conflict and rolls back conversation, messages, operation and slot changes. |
| Duplicate APPLY | Integration | Yes | No | Returns `AlreadySucceeded` with original durable messages; different supplied output is ignored and history is unchanged. |
| Failure paths | Integration | Yes | No | Pre-provider definite failure commits FAILED/unblocked/free; definite provider failure does the same from RUNNING; uncertain result remains RUNNING/blocked/slot-owned; repository never calls provider. |
| Recovery timing matrix | Integration | Yes | No | QUEUED recovers only at reservation expiry; RUNNING becomes OUTCOME_UNKNOWN only when deadline and lease are both `<= now`; otherwise `LiveRunning`. No provider call. |
| APPLY/recovery race | Multiprocess integration | Yes | Two processes with barrier | APPLY-first yields SUCCEEDED and free slot. Recovery-first yields OUTCOME_UNKNOWN then guarded reconciliation; no duplicate messages and reconciliation never changes the slot. |
| Acknowledge/reconcile race | Multiprocess integration | Yes | Two processes with barrier | Reconcile-first yields SUCCEEDED and acknowledgement `NotUnknown`; acknowledgement-first yields acknowledged OUTCOME_UNKNOWN and `AcknowledgedDiscard`. |
| Wrong-runner reconciliation | Integration | Yes | No | `RunnerMismatchDiscard`; operation, conversation, messages and slot are unchanged. |
| Late failure after recovery | Integration | Yes | No | Definite or uncertain late failure returns `AlreadyTerminal(OUTCOME_UNKNOWN)`; no state, history or slot change. |
| Retry/old-result isolation | Integration | Yes | No | Acknowledged OUTCOME_UNKNOWN source O has RUNNING retry R. Late success for O returns `AcknowledgedDiscard`; R, conversation and slot are unchanged. |
| Zero-row and >1-row handling | Fault-injection integration | Yes | Fresh reread connection | Every conditional zero maps to its typed race/ownership result after rollback. Simulated impossible >1 raises redacted integrity failure and rolls back. |
| Commit ambiguity for every write | Fault-injection integration | Yes | Fresh reread connection | For create, prepare, adopt, claim, fail, APPLY, reconcile, recover, acknowledge and cleanup, both commit possibilities are reconciled without blind replay or duplicate rows. |
| Busy timeout | Multiprocess integration | Yes | One process holds writer lock | Lock acquisition ends within bounded timeout and returns `DatabaseBusy`; no write occurred and no sensitive SQL/content is logged. |
| Durable transaction nesting | Integration | Yes | No | Calling a write inside an outer atomic block is rejected by `atomic(durable=True)`; normal write trace shows `BEGIN IMMEDIATE`. |
| Schema-object survival (N6) | Migration integration | Yes | No | Empty-to-latest, repeat migrate and rollback rehearsal preserve every named trigger, partial index and CHECK. A simulated table-rebuild migration that recreates its table's triggers passes the manifest check. The same rebuild without recreation is detected: startup verification reports the missing triggers and refuses writes. |
| Startup invariant failures | Integration | Yes | No | Corrupt fixture for each startup invariant causes safe read-only/refuse-writes mode; no automatic repair. Clean database enables writes. |
| Startup order (N10) | Integration | Yes | No | With a failing invariant and an expired RUNNING operation present, recovery is never attempted and the operation stays RUNNING. With a clean database, verification precedes `recover_slot()`, and no other write is accepted until recovery returns. Recovery returning `DatabaseBusy` leaves writes disabled. |
| No automatic migration (N8) | Integration | Yes | No | With an unapplied migration, Terminal and Django startup refuse writes with a safe message and do not apply it. Terminal composition succeeds with database-only settings and no `SECRET_KEY`. |
| Pragmas and connection ownership | Integration | Yes | Separate thread/process cases | Every new connection has FK ON, WAL, FULL and busy timeout; no cross-thread or inherited pre-fork connection is used. |
| Git-ignore preflight (H2, N7) | Filesystem integration in a temporary Git repository outside the work tree | No DB required | Subprocess `git check-ignore` | Database, WAL/SHM, backup and temporary names are ignored (exit 0) and pass. An unignored path (exit 1) fails initialization. Exit 128, a missing Git executable or a timeout fails closed. A protected path that was force-added to the temporary index is reported as not ignored and fails closed, and `git ls-files` detects it. The preflight never modifies the index. |
| Filesystem permissions/symlinks | Filesystem integration | Yes | No | Directory 0700 and database/sidecars 0600; DB or parent symlink and unsuitable filesystem are rejected without touching targets. |
| Backup/restore | Integration | Yes | Writer connection during backup | Online backup includes concurrent committed work, passes integrity/schema checks, failed backup never publishes, restored copy passes startup verification. |
| JSON importer validation (M4) | Integration | Yes | No | Missing/malformed/invalid UTF-8, duplicate keys, lone surrogates, oversize source and invalid roles fail with no rows; exact Unicode/order/content and legacy empty assistant content import unchanged; source remains unchanged. |
| JSON source drift/cutover (M4) | Integration | Yes | No | Changed source hash at acceptance aborts. Accepted cutover disables JSON repository; unavailable SQLite fails closed without fallback. |
| Ownership and enumeration | Integration | Yes | No | Every cross-owner read/write returns indistinguishable `NotFound`; no content or object existence leaks. |
| Logging/privacy | Unit + integration | Yes | No | Errors/logs contain no SQL parameters, messages, request content, idempotency keys, paths or provider details; database contains no standing-context or long-term-memory content. |

### Test mechanics

#### Unit tests

- Result variant mapping and immutable ownership tokens.
- Fingerprint canonicalization and timestamp calculations.
- State transition guards and zero-row classification.
- No provider or context dependencies imported by repository.

#### SQLite integration tests

- A fresh-database harness built on `unittest.TestCase`, never Django `TestCase`, `TransactionTestCase`, `flush` or only in-memory SQLite.
- Once per test run, the harness creates a private temporary directory outside the work tree, applies all migrations to a template database there using the database-only test settings, and runs full startup verification on it.
- For each test, the harness copies the template into a new file with the SQLite online backup API, sets WAL on the copy, closes all Django connections, points the default alias at the copy, and verifies pragmas and the schema manifest.
- At teardown, the harness closes every connection and deletes the copy and its sidecars. Race-test child processes receive the per-test path explicitly and open their own connections.
- Separate connections with WAL and actual `BEGIN IMMEDIATE`.
- Fault injection around each conditional write/commit.
- PRAGMA, migrations, backup and integrity verification.

#### Process race tests

- `multiprocessing` barriers synchronize competing writers.
- Independent runner IDs and connections.
- Bounded join timeout detects hangs.
- Assert one winner and inspect final durable invariants.

> **Clock testing**
>
> The repository receives a `Clock` at construction; no method accepts a timestamp. Each write transaction reads the clock once, after `BEGIN IMMEDIATE` has acquired the write lock. Tests inject a controllable fake clock to set exact boundary instants for reservation, deadline and lease, to advance time while a write waits on the lock, and to assert one read per transaction.

## 9. Decisions and delivery

### Decisions not settled by the architecture

| Decision | Recommended default | Why it needs explicit acceptance |
|---|---|---|
| Django runtime dependency | Add Django 5.2 LTS at an exact patch version to `requirements.txt` and the hashed lock file at M2.1. | Django is not yet a project dependency, and dependency changes require Adam's explicit approval. M2.1 cannot start without it. |
| Durable timestamp representation | UTC integer microseconds from the repository's injected clock, read once after the write lock. | The document says datetime but not SQLite encoding. Integer comparison is unambiguous; deployment must define wall-clock/skew policy. |
| Provider/model in fingerprint | Include both, as specified in fingerprint V1. | Adam must explicitly confirm before implementation because the approved architecture's original field list did not include them. Recommendation: approve inclusion to prevent key reuse from silently changing provider/model. |
| Idempotency key at rest | Resolved: validate at least 128 random base64url bits and store versioned unkeyed SHA-256 digest. | HMAC is rejected unless a future concrete threat requires it; secret rotation would complicate lookup without protecting high-entropy non-authentication keys. |
| Reservation duration and margins | Choose only after measuring worst-case context assembly; must exceed assembly + scheduling + claim busy budget. | Architecture intentionally leaves reservation, cleanup and skew constants to testing. |
| Uncertain outcome metadata | Allow a sanitized `failure_category` on RUNNING without changing status. | The document requires ordinary uncertain handling but does not explicitly say whether to persist its category before recovery. |
| Legacy import ledger | Add `legacy_import` in Milestone 4. | Idempotent/reversible import is required, but section 7 does not define durable import metadata. |
| Authorized identity timing | Create table in M2 if implementing the complete section-7 schema; populate/use it only in M6. | Roadmap separates schema and identity middleware. |
| Backup destination and retention | Implement destination-agnostic online backup primitive; activation waits for Adam's choice. | Architecture explicitly reserves this product/privacy decision. |
| Terminal owner identity | Resolve before Milestone 3 or 4; tests use a fixture identity. | Terminal and web must address the same owner row, but authentication wiring is outside Milestone 2. |

### Recommended implementation sequence

| Step | Deliverable | Exit criterion |
|---|---|---|
| M2.1 | After Adam approves the exact Django pin: database-only settings module, fixed database path, required `.gitignore` changes and connection hardening; no views, no web settings. | Strict `git check-ignore` and tracked-file preflight passes; startup verifies local path, permissions, WAL, FULL, FK and busy timeout; the existing 77-test command still runs without Django. |
| M2.2 | Models and initial migrations, all CHECKs/triggers/partial indexes, seed role and singleton slot; fresh-database test harness and separate repository test command. | All schema constraints, cross-table/immutability/retry-chain triggers and migration-rebuild tests (with and without trigger recreation) pass on real SQLite without `flush`. |
| M2.3 | Immutable repository commands/results, injected `Clock`, and ownership-scoped reads. | No generic exception is used for expected races; no provider dependency. |
| M2.4 | Durable PREPARE, adoption, claim, failure, APPLY, reconciliation, recovery and acknowledgement transactions; verify → recover → enable-writes startup order. | Exact row-count, recovery-commit, clock-after-lock, commit-ambiguity, startup-order and rollback/fault tests pass. |
| M2.5 | Online backup/restore primitives and operational checks. | Concurrent backup and temporary restore drill pass. |
| M2.6 | Cross-process race/security suite and independent review. | One slot winner, no partial writes, no ownership leak, no hangs. |

### Milestone 2 completion gate

- Schema and all section-7 invariants are represented by database constraints or explicitly identified transactional invariants.
- Every write contract documents exact row counts and has zero-row and rollback tests.
- Adoption exposes expired and current-state results; claim performs inline expiry cancellation; completion is fenced by runner identity.
- PREPARE recovery remains committed across business rejections; duplicate-key lookup precedes blocker and slot checks.
- No transaction spans context assembly or provider I/O; repository cannot invoke a model.
- Startup and inline recovery preserve live work and never replay uncertain work.
- Cross-table identity, status/blocking rules, operation immutability, exact APPLY arithmetic and retry scope have explicit database or transaction enforcement.
- Every database, sidecar, backup and rehearsal path is git-ignored and verified before initialization.
- WAL/FULL/FK/busy settings and local-file assumptions are verified on the deployed Python/SQLite versions.
- Repository writes take time only from the injected clock after the write lock; no method accepts a caller timestamp.
- Linear retry chains are constrained in the schema; retry creation and `RetrySourceNotEligible` mapping remain Milestone 3 work.
- Startup verifies read-only, recovers once, then enables writes; no process runs migrations automatically.
- Milestone 1's 77 tests remain green under the unchanged existing command; the new repository suite passes under its separate command for unit, integration and process-race runs.
- Terminal still writes only JSON until Milestone 4; the new repository remains unused by production flow until `ConversationService` in Milestone 3.

> **Plan recommendation**
>
> This revision is ready for a targeted check of second-review findings N1–N12. Implementation waits for that check, Adam's confirmation of provider/model inclusion in fingerprint V1, and Adam's approval of the exact Django dependency pin. Backup destination/retention and Terminal owner wiring may remain unresolved until their named later milestones.
