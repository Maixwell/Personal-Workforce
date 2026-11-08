"""Disposable-database proof of one durable operation outcome."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
PRODUCTION_DATABASE = PROJECT_ROOT / "data" / "max_smart.sqlite3"

_PREAMBLE = """\
import json
import os
import uuid
from pathlib import Path

database = Path(os.environ["MAX_SMART_TEST_DATABASE_PATH"])
if database.name == "max_smart.sqlite3":
    raise SystemExit("production database requested")
from max_smart_db.safety import initialize_sqlite_file
initialize_sqlite_file(database)

import django
django.setup()
from django.contrib.auth.models import User
from django.db import OperationalError, connection, connections
from django.db.migrations.executor import MigrationExecutor
from max_smart_db.creation import SqliteConversationRepository
from max_smart_db.models import Conversation, GlobalModelSlot, Message, Operation
from max_smart_db.repository import DefiniteProviderCategory, RetainedResult

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)

OWNER = User.objects.create(username="owner")
OTHER = User.objects.create(username="other")
CONVERSATION = uuid.UUID("00112233445566778899aabbccddeeff")
ALT_CONVERSATION = uuid.UUID("abcdefabcdefabcdefabcdefabcdefab")
OPERATION = uuid.UUID("11111111111111111111111111111111")
ALT_OPERATION = uuid.UUID("22222222222222222222222222222222")
THIRD_OPERATION = uuid.UUID("44444444444444444444444444444444")
MISSING = uuid.UUID("33333333333333333333333333333333")
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
STALE = "BBBBBBBBBBBBBBBBBBBBBQ"
HASH = "ab" * 32
CLAIMED_AT = 123_456
DEADLINE = CLAIMED_AT + 600_000_000
LEASE = CLAIMED_AT + 720_000_000

Conversation.objects.create(
    id=CONVERSATION,
    owner_id=OWNER.id,
    role_id="max-smart",
    version=0,
    next_sequence=1,
    title=None,
    created_at_us=0,
    updated_at_us=0,
)


class Clock:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def now_us(self):
        self.calls += 1
        return self.value


def repository(now=CLAIMED_AT):
    clock = Clock(now)
    return SqliteConversationRepository(clock, RUNNER), clock


def queued(identifier, conversation=CONVERSATION):
    Operation.objects.create(
        id=identifier,
        conversation_id=conversation,
        owner_id=OWNER.id,
        operation_type="message",
        provider_id="openai",
        model_id="gpt-5.6-sol",
        status="QUEUED",
        blocks_conversation=1,
        expected_version=0,
        request_content="hello",
        fingerprint_version=1,
        request_hash=HASH,
        retry_of=None,
        runner_id=RUNNER,
        reservation_expires_at_us=1_000_000_000,
        provider_started_at_us=None,
        execution_deadline_us=None,
        lease_expires_at_us=None,
        created_at_us=1,
        finished_at_us=None,
        acknowledged_at_us=None,
        failure_category=None,
    )
    GlobalModelSlot.objects.filter(pk=1).update(
        operation_id=identifier,
        runner_id=RUNNER,
        reservation_expires_at_us=1_000_000_000,
        lease_expires_at_us=None,
        acquired_at_us=7,
    )


def claim(identifier=OPERATION):
    queued(identifier)
    repo, clock = repository()
    outcome = repo.claim_running(OWNER.id, identifier)
    if type(outcome).__name__ != "Claimed":
        raise RuntimeError(type(outcome).__name__)
    return repo, clock, outcome.claimed_operation


def described(outcome):
    payload = {"type": type(outcome).__name__}
    if payload["type"] == "DurableSuccessObserved":
        payload["claimed_at"] = outcome.claimed_at
        payload["operation_id"] = str(outcome.operation_id)
        payload["messages"] = [
            [
                outcome.user_message.sequence,
                outcome.user_message.role.value,
                outcome.user_message.content,
                str(outcome.user_message.operation_id),
            ],
            [
                outcome.assistant_message.sequence,
                outcome.assistant_message.role.value,
                outcome.assistant_message.content,
                str(outcome.assistant_message.operation_id),
            ],
        ]
    elif payload["type"] == "DurableClaimConfirmed":
        payload["claimed_at"] = outcome.claimed_at
        payload["runner_id"] = outcome.runner_id
    elif payload["type"] == "DurableUnknownObserved":
        payload["claimed_at"] = outcome.claimed_at
        payload["runner_id"] = outcome.runner_id
    elif payload["type"] == "DurableFailureObserved":
        payload["claimed_at"] = outcome.claimed_at
        payload["failure_category"] = outcome.failure_category.value
    return payload


def inspect(repo, actor_id, operation_id, runner_id, claimed_at):
    before = repo.clock.calls
    outcome = repo.inspect_durable_outcome(
        actor_id,
        operation_id,
        runner_id,
        claimed_at,
    )
    return described(outcome), repo.clock.calls - before


def fresh(operation_id):
    owned = connections.create_connection("default")
    try:
        with owned.cursor() as cursor:
            cursor.execute("BEGIN DEFERRED")
            cursor.execute(
                "SELECT status, blocks_conversation, runner_id, "
                "provider_started_at_us, finished_at_us, failure_category "
                "FROM operation WHERE id = ?",
                (operation_id.hex,),
            )
            operation = cursor.fetchone()
            cursor.execute(
                "SELECT COUNT(*) FROM message WHERE operation_id = ?",
                (operation_id.hex,),
            )
            messages = cursor.fetchone()[0]
            cursor.execute(
                "SELECT operation_id, lease_expires_at_us "
                "FROM global_model_slot WHERE id = 1"
            )
            slot = cursor.fetchone()
            cursor.execute(
                "SELECT version, next_sequence FROM conversation WHERE id = ?",
                (CONVERSATION.hex,),
            )
            conversation = cursor.fetchone()
            cursor.execute("ROLLBACK")
    finally:
        owned.close()
    return {
        "operation": None if operation is None else list(operation),
        "messages": messages,
        "slot_operation": None if slot[0] is None else str(slot[0]),
        "slot_lease": slot[1],
        "version": conversation[0],
        "next_sequence": conversation[1],
    }
"""


def _run(body, *, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "m3s2-outcome.sqlite3"
            ),
            "PYTHONPATH": str(SRC_ROOT),
        }
        return subprocess.run(
            (
                sys.executable,
                "-B",
                "-c",
                _PREAMBLE + "\n" + textwrap.dedent(body),
            ),
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )


def _payload(testcase, result):
    testcase.assertEqual(
        result.returncode,
        0,
        result.stdout + "\n" + result.stderr,
    )
    return json.loads(result.stdout)


class DurableOutcomeInspectionTests(unittest.TestCase):
    def test_applied_success_proves_the_message_pair_and_version(self):
        result = _run(
            """\
            repo, clock, claimed = claim()
            applied = repo.apply_success(claimed, "durable answer")
            proof, clock_delta = inspect(
                repo, OWNER.id, OPERATION, RUNNER, CLAIMED_AT,
            )
            observed = fresh(OPERATION)
            Conversation.objects.filter(pk=CONVERSATION).update(version=99)
            mismatched, _delta = inspect(
                repo, OWNER.id, OPERATION, RUNNER, CLAIMED_AT,
            )
            print(json.dumps({
                "applied": type(applied).__name__,
                "proof": proof,
                "clock_delta": clock_delta,
                "claim_calls": clock.calls,
                "observed": observed,
                "version_mismatch": mismatched,
                "message_count": Message.objects.count(),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["applied"], "Applied")
        self.assertEqual(payload["proof"]["type"], "DurableSuccessObserved")
        self.assertEqual(payload["proof"]["claimed_at"], 123_456)
        self.assertEqual(
            payload["proof"]["messages"],
            [
                [1, "user", "hello", "11111111-1111-1111-1111-111111111111"],
                [
                    2,
                    "assistant",
                    "durable answer",
                    "11111111-1111-1111-1111-111111111111",
                ],
            ],
        )
        self.assertEqual(payload["clock_delta"], 0)
        self.assertEqual(payload["observed"]["version"], 1)
        self.assertEqual(payload["observed"]["next_sequence"], 3)
        self.assertEqual(payload["observed"]["messages"], 2)
        self.assertEqual(payload["observed"]["operation"][0], "SUCCEEDED")
        self.assertIsNone(payload["observed"]["slot_operation"])
        self.assertEqual(payload["version_mismatch"]["type"], "OutcomeNotProven")
        self.assertEqual(payload["message_count"], 2)

    def test_committed_failure_is_proven_without_becoming_success(self):
        result = _run(
            """\
            repo, clock, claimed = claim()
            failed = repo.apply_failure(
                claimed,
                DefiniteProviderCategory.PROVIDER_DEFINITE_FAILURE,
            )
            proof, clock_delta = inspect(
                repo, OWNER.id, OPERATION, RUNNER, CLAIMED_AT,
            )
            observed = fresh(OPERATION)
            Operation.objects.create(
                id=ALT_OPERATION,
                conversation_id=CONVERSATION,
                owner_id=OWNER.id,
                operation_type="message",
                provider_id="openai",
                model_id="gpt-5.6-sol",
                status="FAILED",
                blocks_conversation=0,
                expected_version=0,
                request_content="hello",
                fingerprint_version=1,
                request_hash=HASH,
                retry_of=None,
                runner_id=None,
                reservation_expires_at_us=None,
                provider_started_at_us=CLAIMED_AT,
                execution_deadline_us=DEADLINE,
                lease_expires_at_us=LEASE,
                created_at_us=1,
                finished_at_us=CLAIMED_AT,
                acknowledged_at_us=None,
                failure_category="provider_output_invalid",
            )
            output_invalid, _delta = inspect(
                repo, OWNER.id, ALT_OPERATION, RUNNER, CLAIMED_AT,
            )
            Operation.objects.create(
                id=THIRD_OPERATION,
                conversation_id=CONVERSATION,
                owner_id=OWNER.id,
                operation_type="message",
                provider_id="openai",
                model_id="gpt-5.6-sol",
                status="FAILED",
                blocks_conversation=0,
                expected_version=0,
                request_content="hello",
                fingerprint_version=1,
                request_hash=HASH,
                retry_of=None,
                runner_id=None,
                reservation_expires_at_us=None,
                provider_started_at_us=None,
                execution_deadline_us=None,
                lease_expires_at_us=None,
                created_at_us=1,
                finished_at_us=1,
                acknowledged_at_us=None,
                failure_category="context_unavailable",
            )
            pre_provider, _delta = inspect(
                repo, OWNER.id, THIRD_OPERATION, RUNNER, CLAIMED_AT,
            )
            print(json.dumps({
                "failed": type(failed).__name__,
                "proof": proof,
                "clock_delta": clock_delta,
                "observed": observed,
                "output_invalid": output_invalid,
                "pre_provider": pre_provider,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["failed"], "DefinitelyFailed")
        self.assertEqual(payload["proof"]["type"], "DurableFailureObserved")
        self.assertEqual(
            payload["proof"]["failure_category"],
            "provider_definite_failure",
        )
        self.assertEqual(payload["proof"]["claimed_at"], 123_456)
        self.assertEqual(payload["clock_delta"], 0)
        self.assertEqual(payload["observed"]["operation"][0], "FAILED")
        self.assertEqual(payload["observed"]["operation"][1], 0)
        self.assertIsNone(payload["observed"]["operation"][2])
        self.assertEqual(payload["observed"]["messages"], 0)
        self.assertIsNone(payload["observed"]["slot_operation"])
        self.assertEqual(payload["observed"]["version"], 0)
        self.assertEqual(
            payload["output_invalid"]["failure_category"],
            "provider_output_invalid",
        )
        self.assertEqual(payload["pre_provider"]["type"], "OutcomeNotProven")

    def test_late_reconciliation_proves_a_later_message_clock(self):
        result = _run(
            """\
            repo, clock, claimed = claim()
            recovered = SqliteConversationRepository(
                Clock(LEASE),
                RUNNER,
            ).recover_slot()
            late, late_clock = repository(LEASE + 5)
            reconciled = late.reconcile_late_success(
                RetainedResult(
                    claimed_operation=claimed,
                    output_text="kept answer",
                )
            )
            proof, clock_delta = inspect(
                repo, OWNER.id, OPERATION, RUNNER, CLAIMED_AT,
            )
            created = list(
                Message.objects.filter(operation_id=OPERATION)
                .order_by("sequence")
                .values_list("created_at_us", flat=True)
            )
            print(json.dumps({
                "recovered": type(recovered).__name__,
                "reconciled": type(reconciled).__name__,
                "proof": proof,
                "clock_delta": clock_delta,
                "late_calls": late_clock.calls,
                "finished": Operation.objects.get(pk=OPERATION).finished_at_us,
                "created": created,
                "observed": fresh(OPERATION),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["recovered"], "MarkedOutcomeUnknown")
        self.assertEqual(payload["reconciled"], "Reconciled")
        self.assertEqual(payload["proof"]["type"], "DurableSuccessObserved")
        self.assertEqual(
            payload["proof"]["messages"][1][2],
            "kept answer",
        )
        self.assertEqual(payload["clock_delta"], 0)
        self.assertEqual(payload["late_calls"], 1)
        self.assertEqual(payload["finished"], 720_123_456)
        self.assertEqual(payload["created"], [720_123_461, 720_123_461])
        self.assertEqual(payload["observed"]["messages"], 2)
        self.assertEqual(payload["observed"]["version"], 1)
        self.assertEqual(payload["observed"]["next_sequence"], 3)
        self.assertEqual(payload["observed"]["operation"][0], "SUCCEEDED")

    def test_ambiguous_late_reconciliation_commit_is_proven_once(self):
        result = _run(
            """\
            repo, clock, claimed = claim()
            SqliteConversationRepository(Clock(LEASE), RUNNER).recover_slot()
            late, late_clock = repository(LEASE + 5)
            real_commit = connection.commit

            def commit(*args, **kwargs):
                real_commit(*args, **kwargs)
                raise OperationalError("simulated post-commit failure")

            connection.commit = commit
            try:
                outcome = late.reconcile_late_success(
                    RetainedResult(
                        claimed_operation=claimed,
                        output_text="kept answer",
                    )
                )
            finally:
                connection.commit = real_commit
            proof, clock_delta = inspect(
                repo, OWNER.id, OPERATION, RUNNER, CLAIMED_AT,
            )
            print(json.dumps({
                "reconciled": type(outcome).__name__,
                "proof": proof,
                "clock_delta": clock_delta,
                "late_calls": late_clock.calls,
                "finished": Operation.objects.get(pk=OPERATION).finished_at_us,
                "created": list(
                    Message.objects.filter(operation_id=OPERATION)
                    .order_by("sequence")
                    .values_list("created_at_us", flat=True)
                ),
                "messages": Message.objects.count(),
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["reconciled"], "CommitAmbiguous")
        self.assertEqual(payload["proof"]["type"], "DurableSuccessObserved")
        self.assertEqual(payload["clock_delta"], 0)
        self.assertEqual(payload["late_calls"], 1)
        self.assertEqual(payload["finished"], 720_123_456)
        self.assertEqual(payload["created"], [720_123_461, 720_123_461])
        self.assertEqual(payload["messages"], 2)

    def test_malformed_success_evidence_fails_closed(self):
        result = _run(
            """\
            def plant(
                identifier,
                conversation,
                roles=("user", "assistant"),
                created=(LEASE + 5, LEASE + 5),
                finished=LEASE,
                version=1,
                next_sequence=3,
                message_conversation=None,
                content="hello",
                owner=OWNER.id,
            ):
                Conversation.objects.create(
                    id=conversation,
                    owner_id=owner,
                    role_id="max-smart",
                    version=version,
                    next_sequence=next_sequence,
                    title=None,
                    created_at_us=0,
                    updated_at_us=0,
                )
                Operation.objects.create(
                    id=identifier,
                    conversation_id=conversation,
                    owner_id=owner,
                    operation_type="message",
                    provider_id="openai",
                    model_id="gpt-5.6-sol",
                    status="SUCCEEDED",
                    blocks_conversation=0,
                    expected_version=0,
                    request_content="hello",
                    fingerprint_version=1,
                    request_hash=HASH,
                    retry_of=None,
                    runner_id=None,
                    reservation_expires_at_us=None,
                    provider_started_at_us=CLAIMED_AT,
                    execution_deadline_us=DEADLINE,
                    lease_expires_at_us=LEASE,
                    created_at_us=1,
                    finished_at_us=finished,
                    acknowledged_at_us=None,
                    failure_category=None,
                )
                home = conversation if message_conversation is None else message_conversation
                for index, role in enumerate(roles):
                    Message.objects.create(
                        id=uuid.uuid4(),
                        conversation_id=home,
                        sequence=index + 1,
                        role=role,
                        content=content if role == "user" else "kept answer",
                        operation_id=identifier,
                        created_at_us=created[index],
                    )

            cases = (
                ("order", uuid.UUID("01010101010101010101010101010101"), uuid.UUID("12121212121212121212121212121212"), {"roles": ("assistant", "user")}),
                ("owner", uuid.UUID("13131313131313131313131313131313"), uuid.UUID("14141414141414141414141414141414"), {"owner": OTHER.id}),
                ("version", uuid.UUID("15151515151515151515151515151515"), uuid.UUID("16161616161616161616161616161616"), {"version": 99}),
                ("sequence", uuid.UUID("17171717171717171717171717171717"), uuid.UUID("18181818181818181818181818181818"), {"next_sequence": 4}),
                ("early", uuid.UUID("19191919191919191919191919191919"), uuid.UUID("20202020202020202020202020202020"), {"created": (LEASE - 1, LEASE - 1)}),
                ("before_lease", uuid.UUID("21212121212121212121212121212121"), uuid.UUID("23232323232323232323232323232323"), {"finished": CLAIMED_AT, "created": (CLAIMED_AT + 5, CLAIMED_AT + 5)}),
                ("split_clock", uuid.UUID("24242424242424242424242424242424"), uuid.UUID("25252525252525252525252525252525"), {"created": (LEASE + 5, LEASE + 6)}),
                ("partial", uuid.UUID("26262626262626262626262626262626"), uuid.UUID("27272727272727272727272727272727"), {"roles": ("user",)}),
            )
            proofs = {}
            for label, operation_id, conversation_id, changes in cases:
                plant(operation_id, conversation_id, **changes)
                proof, _delta = inspect(
                    repository()[0], OWNER.id, operation_id, RUNNER, CLAIMED_AT,
                )
                proofs[label] = proof["type"]
            print(json.dumps(proofs))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(
            set(payload),
            {
                "order",
                "owner",
                "version",
                "sequence",
                "early",
                "before_lease",
                "split_clock",
                "partial",
            },
        )
        for label, proof in payload.items():
            self.assertEqual(proof, "OutcomeNotProven", label)

    def test_running_claim_recovered_unknown_and_acknowledgement(self):
        result = _run(
            """\
            repo, clock, claimed = claim()
            running, running_delta = inspect(
                repo, OWNER.id, OPERATION, RUNNER, CLAIMED_AT,
            )
            wrong_runner, _delta = inspect(
                repo, OWNER.id, OPERATION, STALE, CLAIMED_AT,
            )
            recovery_repo, recovery_clock = repository(LEASE)
            recovered = recovery_repo.recover_slot()
            unknown, unknown_delta = inspect(
                repo, OWNER.id, OPERATION, RUNNER, CLAIMED_AT,
            )
            unknown_runner, _delta = inspect(
                repo, OWNER.id, OPERATION, STALE, CLAIMED_AT,
            )
            released = fresh(OPERATION)
            acknowledged = recovery_repo.acknowledge_unknown(OWNER.id, OPERATION)
            after_ack, _delta = inspect(
                repo, OWNER.id, OPERATION, RUNNER, CLAIMED_AT,
            )
            print(json.dumps({
                "running": running,
                "running_delta": running_delta,
                "wrong_runner": wrong_runner,
                "recovered": type(recovered).__name__,
                "unknown": unknown,
                "unknown_delta": unknown_delta,
                "unknown_runner": unknown_runner,
                "released": released,
                "acknowledged": type(acknowledged).__name__,
                "after_ack": after_ack,
                "claim_clock": clock.calls,
                "recovery_clock": recovery_clock.calls,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["running"]["type"], "DurableClaimConfirmed")
        self.assertEqual(payload["running"]["runner_id"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(payload["running"]["claimed_at"], 123_456)
        self.assertEqual(payload["running_delta"], 0)
        self.assertEqual(payload["wrong_runner"]["type"], "OutcomeNotProven")
        self.assertEqual(payload["recovered"], "MarkedOutcomeUnknown")
        self.assertEqual(payload["unknown"]["type"], "DurableUnknownObserved")
        self.assertEqual(payload["unknown"]["runner_id"], "AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(payload["unknown"]["claimed_at"], 123_456)
        self.assertEqual(payload["unknown_delta"], 0)
        self.assertEqual(payload["unknown_runner"]["type"], "OutcomeNotProven")
        self.assertEqual(payload["released"]["operation"][0], "OUTCOME_UNKNOWN")
        self.assertEqual(payload["released"]["operation"][1], 1)
        self.assertIsNone(payload["released"]["slot_operation"])
        self.assertEqual(payload["released"]["messages"], 0)
        self.assertEqual(payload["acknowledged"], "Acknowledged")
        self.assertEqual(payload["after_ack"]["type"], "OutcomeNotProven")

    def test_identity_and_message_mismatches_fail_closed(self):
        result = _run(
            """\
            repo, clock, claimed = claim()
            repo.apply_success(claimed, "durable answer")
            wrong_actor, _delta = inspect(
                repo, OTHER.id, OPERATION, RUNNER, CLAIMED_AT,
            )
            missing, _delta = inspect(
                repo, OWNER.id, MISSING, RUNNER, CLAIMED_AT,
            )
            wrong_time, _delta = inspect(
                repo, OWNER.id, OPERATION, RUNNER, CLAIMED_AT + 1,
            )
            Conversation.objects.create(
                id=ALT_CONVERSATION,
                owner_id=OWNER.id,
                role_id="max-smart",
                version=1,
                next_sequence=3,
                title=None,
                created_at_us=0,
                updated_at_us=0,
            )
            Operation.objects.create(
                id=ALT_OPERATION,
                conversation_id=ALT_CONVERSATION,
                owner_id=OWNER.id,
                operation_type="message",
                provider_id="openai",
                model_id="gpt-5.6-sol",
                status="SUCCEEDED",
                blocks_conversation=0,
                expected_version=0,
                request_content="hello",
                fingerprint_version=1,
                request_hash=HASH,
                retry_of=None,
                runner_id=None,
                reservation_expires_at_us=None,
                provider_started_at_us=CLAIMED_AT,
                execution_deadline_us=DEADLINE,
                lease_expires_at_us=LEASE,
                created_at_us=1,
                finished_at_us=CLAIMED_AT,
                acknowledged_at_us=None,
                failure_category=None,
            )
            Message.objects.create(
                id=uuid.UUID("55555555555555555555555555555555"),
                conversation_id=ALT_CONVERSATION,
                sequence=1,
                role="user",
                content="not-the-request",
                operation_id=ALT_OPERATION,
                created_at_us=CLAIMED_AT,
            )
            Message.objects.create(
                id=uuid.UUID("66666666666666666666666666666666"),
                conversation_id=ALT_CONVERSATION,
                sequence=2,
                role="assistant",
                content="durable answer",
                operation_id=ALT_OPERATION,
                created_at_us=CLAIMED_AT,
            )
            malformed, _delta = inspect(
                repo, OWNER.id, ALT_OPERATION, RUNNER, CLAIMED_AT,
            )
            print(json.dumps({
                "wrong_actor": wrong_actor,
                "missing": missing,
                "wrong_time": wrong_time,
                "malformed": malformed,
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["wrong_actor"]["type"], "OutcomeNotProven")
        self.assertEqual(payload["missing"]["type"], "OutcomeNotProven")
        self.assertEqual(payload["wrong_time"]["type"], "OutcomeNotProven")
        self.assertEqual(payload["malformed"]["type"], "OutcomeNotProven")
        self.assertNotIn("content", json.dumps(payload["wrong_actor"]))

    def test_ambiguous_apply_commits_are_classified_from_durable_state(self):
        def scenario(commit_name, failure):
            failure_line = "None"
            if failure:
                failure_line = (
                    "DefiniteProviderCategory.PROVIDER_DEFINITE_FAILURE"
                )
            return _payload(self, _run(
                f"""\
                def wrapper(real_commit):
                    def commit(*args, **kwargs):
                        if {commit_name!r} == "rollback":
                            raise OperationalError(
                                "simulated rollback before commit"
                            )
                        real_commit(*args, **kwargs)
                        raise OperationalError("simulated post-commit failure")
                    return commit

                repo, clock, claimed = claim()
                real_commit = connection.commit
                connection.commit = wrapper(real_commit)
                try:
                    if {failure_line} is None:
                        outcome = repo.apply_success(claimed, "durable answer")
                    else:
                        outcome = repo.apply_failure(claimed, {failure_line})
                finally:
                    connection.commit = real_commit
                proof, clock_delta = inspect(
                    repo, OWNER.id, OPERATION, RUNNER, CLAIMED_AT,
                )
                print(json.dumps({{
                    "apply": type(outcome).__name__,
                    "proof": proof,
                    "clock_delta": clock_delta,
                    "fresh": fresh(OPERATION),
                }}))
                """
            ))

        rolled_success = scenario("rollback", False)
        durable_success = scenario("commit", False)
        rolled_failure = scenario("rollback", True)
        durable_failure = scenario("commit", True)
        self.assertEqual(rolled_success["apply"], "CommitAmbiguous")
        self.assertEqual(
            rolled_success["proof"]["type"],
            "DurableClaimConfirmed",
        )
        self.assertEqual(rolled_success["fresh"]["operation"][0], "RUNNING")
        self.assertEqual(rolled_success["fresh"]["messages"], 0)
        self.assertEqual(rolled_success["clock_delta"], 0)
        self.assertEqual(durable_success["apply"], "CommitAmbiguous")
        self.assertEqual(
            durable_success["proof"]["type"],
            "DurableSuccessObserved",
        )
        self.assertEqual(durable_success["fresh"]["operation"][0], "SUCCEEDED")
        self.assertEqual(durable_success["fresh"]["messages"], 2)
        self.assertEqual(durable_success["fresh"]["version"], 1)
        self.assertEqual(rolled_failure["apply"], "CommitAmbiguous")
        self.assertEqual(
            rolled_failure["proof"]["type"],
            "DurableClaimConfirmed",
        )
        self.assertEqual(rolled_failure["fresh"]["operation"][0], "RUNNING")
        self.assertEqual(durable_failure["apply"], "CommitAmbiguous")
        self.assertEqual(
            durable_failure["proof"]["type"],
            "DurableFailureObserved",
        )
        self.assertEqual(durable_failure["fresh"]["operation"][0], "FAILED")
        self.assertEqual(durable_failure["fresh"]["messages"], 0)
        self.assertEqual(durable_failure["fresh"]["version"], 0)

    def test_inspection_leaves_caller_transactions_untouched(self):
        result = _run(
            """\
            repo, clock, claimed = claim()
            real_create = connections.create_connection

            def failing_create(alias):
                owned = real_create(alias)
                real_cursor = owned.cursor

                def cursor(*args, **kwargs):
                    wrapped = real_cursor(*args, **kwargs)
                    real_execute = wrapped.execute

                    def execute(sql, params=None):
                        if "FROM message" in str(sql):
                            raise OperationalError("simulated incomplete outcome read")
                        if params is None:
                            return real_execute(sql)
                        return real_execute(sql, params)

                    wrapped.execute = execute
                    return wrapped

                owned.cursor = cursor
                return owned

            connection.ensure_connection()
            before_failed = id(connection.connection)
            connections.create_connection = failing_create
            try:
                failed_outcome = repo.inspect_durable_outcome(
                    OWNER.id, OPERATION, RUNNER, CLAIMED_AT,
                )
            finally:
                connections.create_connection = real_create
            failed_same = id(connection.connection) == before_failed

            connection.set_autocommit(False)
            write_identity = id(connection.connection)
            write_cursor = connection.cursor()
            write_cursor.execute("BEGIN IMMEDIATE")
            write_cursor.execute(
                "UPDATE operation SET status = 'SUCCEEDED', "
                "blocks_conversation = 0, runner_id = NULL, "
                "finished_at_us = ? WHERE id = ?",
                (CLAIMED_AT, OPERATION.hex),
            )
            write_cursor.execute(
                "INSERT INTO message "
                "(id, conversation_id, sequence, role, content, "
                "operation_id, created_at_us) "
                "VALUES (?, ?, 1, 'user', 'hello', ?, ?)",
                (
                    "77777777777777777777777777777777",
                    CONVERSATION.hex,
                    OPERATION.hex,
                    CLAIMED_AT,
                ),
            )
            write_cursor.execute(
                "INSERT INTO message "
                "(id, conversation_id, sequence, role, content, "
                "operation_id, created_at_us) "
                "VALUES (?, ?, 2, 'assistant', 'uncommitted', ?, ?)",
                (
                    "88888888888888888888888888888888",
                    CONVERSATION.hex,
                    OPERATION.hex,
                    CLAIMED_AT,
                ),
            )
            write_cursor.execute(
                "SELECT status FROM operation WHERE id = ?",
                (OPERATION.hex,),
            )
            caller_status = write_cursor.fetchone()[0]
            false_proof, _delta = inspect(
                repo, OWNER.id, OPERATION, RUNNER, CLAIMED_AT,
            )
            write_cursor.execute(
                "SELECT status FROM operation WHERE id = ?",
                (OPERATION.hex,),
            )
            caller_after = write_cursor.fetchone()[0]
            write_same = id(connection.connection) == write_identity
            write_open = connection.connection.in_transaction
            write_autocommit = connection.get_autocommit()
            connection.rollback()
            connection.set_autocommit(True)
            durable = fresh(OPERATION)

            connection.ensure_connection()
            read_identity = id(connection.connection)
            read_autocommit = connection.get_autocommit()
            read_cursor = connection.cursor()
            read_cursor.execute("BEGIN DEFERRED")
            read_cursor.execute(
                "SELECT lease_expires_at_us FROM global_model_slot WHERE id = 1"
            )
            pinned = read_cursor.fetchone()[0]
            writer = connections.create_connection("default")
            try:
                with writer.cursor() as writer_cursor:
                    writer_cursor.execute("BEGIN IMMEDIATE")
                    writer_cursor.execute(
                        "UPDATE global_model_slot "
                        "SET lease_expires_at_us = ? WHERE id = 1",
                        (pinned + 9,),
                    )
                    writer_cursor.execute("COMMIT")
            finally:
                writer.close()
            read_proof, _delta = inspect(
                repo, OWNER.id, OPERATION, RUNNER, CLAIMED_AT,
            )
            read_cursor.execute(
                "SELECT lease_expires_at_us FROM global_model_slot WHERE id = 1"
            )
            read_still = read_cursor.fetchone()[0]
            read_open = connection.connection.in_transaction
            read_same = (
                id(connection.connection) == read_identity
                and connection.get_autocommit() == read_autocommit
            )
            read_cursor.execute("ROLLBACK")
            read_cursor.close()
            committed_lease = fresh(OPERATION)["slot_lease"]
            print(json.dumps({
                "failed_type": type(failed_outcome).__name__,
                "failed_same": failed_same,
                "read_proof": read_proof,
                "read_unchanged": read_still == pinned,
                "read_open": read_open,
                "read_same": read_same,
                "committed_lease": committed_lease,
                "caller_status": caller_status,
                "caller_after": caller_after,
                "false_proof": false_proof,
                "write_same": write_same,
                "write_open": write_open,
                "write_autocommit": write_autocommit,
                "durable_status": durable["operation"][0],
                "durable_messages": durable["messages"],
            }))
            """
        )
        payload = _payload(self, result)
        self.assertEqual(payload["failed_type"], "OutcomeNotProven")
        self.assertTrue(payload["failed_same"])
        self.assertEqual(payload["read_proof"]["type"], "OutcomeNotProven")
        self.assertTrue(payload["read_unchanged"])
        self.assertTrue(payload["read_open"])
        self.assertTrue(payload["read_same"])
        self.assertEqual(payload["committed_lease"], 720_123_456 + 9)
        self.assertEqual(payload["caller_status"], "SUCCEEDED")
        self.assertEqual(payload["caller_after"], "SUCCEEDED")
        self.assertEqual(payload["false_proof"]["type"], "DurableClaimConfirmed")
        self.assertTrue(payload["write_same"])
        self.assertTrue(payload["write_open"])
        self.assertFalse(payload["write_autocommit"])
        self.assertEqual(payload["durable_status"], "RUNNING")
        self.assertEqual(payload["durable_messages"], 0)

    def test_inspection_source_does_not_mutate_or_open_production(self):
        source = (SRC_ROOT / "max_smart_db" / "outcome_inspection.py").read_text()
        self.assertNotIn("now_us", source)
        self.assertNotIn("apply_success", source)
        self.assertNotIn("apply_failure", source)
        self.assertNotIn("BEGIN IMMEDIATE", source)
        self.assertFalse(PRODUCTION_DATABASE.exists())
