"""In-memory tests for structural authority eligibility.

These tests do not create accounts, open databases, or inspect the filesystem.
"""

import copy
import inspect
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import recovery_authority_state as authority  # noqa: E402


def _observation(**overrides):
    observation = {
        "provisioned": False,
        "genesis_durable": False,
        "accepted_generation": None,
        "accepted_snapshot_bound": False,
        "pending_generation": None,
        "uncertain": False,
        "partial_publication": False,
    }
    observation.update(overrides)
    return observation


def _ready(generation, **overrides):
    observation = _observation(
        provisioned=True,
        genesis_durable=True,
        accepted_generation=None if generation == 0 else generation,
        accepted_snapshot_bound=generation != 0,
    )
    observation.update(overrides)
    return observation


def _advance(target):
    return {"action": "advance", "target_generation": target}


class AuthorityStateTests(unittest.TestCase):
    def test_empty_unprovisioned_observation_is_uninitialized(self):
        state = authority.classify(_observation())
        self.assertEqual((state.code, state.generation), (authority.UNINITIALIZED, None))

    def test_explicit_durable_genesis_is_ready_zero(self):
        state = authority.classify(_ready(0))
        self.assertEqual((state.code, state.generation), (authority.READY, 0))

    def test_complete_accepted_generation_is_ready(self):
        state = authority.classify(_ready(4))
        self.assertEqual((state.code, state.generation), (authority.READY, 4))

    def test_exact_next_step_is_structurally_eligible(self):
        for generation in (0, 1, 4):
            decision = authority.evaluate_transition(_ready(generation), _advance(generation + 1))
            self.assertTrue(decision.eligible)
            self.assertEqual(decision.reason, "structurally_eligible")
            self.assertFalse(hasattr(decision, "permitted"))
            self.assertFalse(hasattr(decision, "actor_authenticated"))

    def test_spoofed_actor_label_cannot_create_eligibility(self):
        parameters = list(inspect.signature(authority.evaluate_transition).parameters)
        self.assertEqual(parameters, ["observation", "requested_action"])
        labeled = _advance(2)
        labeled["actor"] = "uid-501-client"
        decision = authority.evaluate_transition(_ready(1), labeled)
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, "action_invalid")

    def test_forged_state_cannot_create_eligibility(self):
        forged = authority.AuthorityState(authority.READY, 7)
        decision = authority.evaluate_transition(forged, _advance(8))
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, "state_ineligible")
        self.assertEqual(authority.classify(forged).code, authority.CORRUPT_LOST)

    def test_missing_and_malformed_observations_fail_closed(self):
        missing = _observation()
        del missing["provisioned"]
        cases = [
            None,
            [],
            missing,
            _observation(provisioned=1),
            _observation(genesis_durable="true"),
            _observation(accepted_snapshot_bound=None),
        ]
        for observation in cases:
            with self.subTest(observation=observation):
                self.assertEqual(authority.classify(observation).code, authority.CORRUPT_LOST)
                decision = authority.evaluate_transition(observation, _advance(1))
                self.assertFalse(decision.eligible)

    def test_unknown_observation_and_action_fields_fail_closed(self):
        unknown = _ready(1)
        unknown["extra"] = False
        action = _advance(2)
        action["note"] = "ignored"
        self.assertEqual(authority.classify(unknown).code, authority.CORRUPT_LOST)
        self.assertFalse(authority.evaluate_transition(_ready(1), action).eligible)

    def test_invalid_generation_types_and_boundaries_fail_closed(self):
        cases = [
            _ready(1) | {"accepted_generation": True},
            _ready(1) | {"accepted_generation": 0},
            _ready(1) | {"accepted_generation": -1},
            _ready(1) | {"accepted_generation": authority.GENERATION_LIMIT + 1},
            _ready(1) | {"pending_generation": True},
        ]
        for observation in cases:
            with self.subTest(observation=observation):
                self.assertEqual(authority.classify(observation).code, authority.CORRUPT_LOST)
                self.assertFalse(authority.evaluate_transition(observation, _advance(2)).eligible)
        ceiling = _ready(authority.GENERATION_LIMIT)
        self.assertEqual(authority.classify(ceiling).code, authority.READY)
        self.assertFalse(
            authority.evaluate_transition(
                ceiling,
                _advance(authority.GENERATION_LIMIT + 1),
            ).eligible
        )
        self.assertFalse(authority.evaluate_transition(_ready(1), _advance(True)).eligible)

    def test_skip_ahead_attempt_is_ineligible(self):
        decision = authority.evaluate_transition(_ready(4), _advance(6))
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, "generation_step_refused")

    def test_pending_and_accepted_conflicts_fail_closed(self):
        conflict = _ready(2, pending_generation=5)
        same = _ready(3, pending_generation=3)
        exact = _ready(3, pending_generation=4)
        self.assertEqual(authority.classify(conflict).code, authority.CORRUPT_LOST)
        self.assertEqual(authority.classify(same).code, authority.UNCERTAIN)
        pending = authority.classify(exact)
        self.assertEqual((pending.code, pending.generation), (authority.PENDING, 4))
        for observation in (conflict, same, exact):
            self.assertFalse(authority.evaluate_transition(observation, _advance(4)).eligible)

    def test_pending_or_uncertain_restart_does_not_resolve(self):
        pending_observation = _ready(3, pending_generation=4)
        uncertain_observation = _ready(3, partial_publication=True)
        self.assertEqual(authority.classify(pending_observation).code, authority.PENDING)
        self.assertEqual(authority.classify(pending_observation).code, authority.PENDING)
        self.assertEqual(authority.classify(uncertain_observation).code, authority.UNCERTAIN)
        self.assertEqual(authority.classify(uncertain_observation).code, authority.UNCERTAIN)
        for observation in (pending_observation, uncertain_observation):
            for action in (_advance(4), {"action": "repair"}):
                self.assertFalse(authority.evaluate_transition(observation, action).eligible)

    def test_non_ready_states_cannot_advance(self):
        observations = [
            _observation(),
            _ready(0, pending_generation=1),
            _ready(1, uncertain=True),
            _observation(provisioned=True),
        ]
        self.assertEqual(
            [authority.classify(item).code for item in observations],
            [
                authority.UNINITIALIZED,
                authority.PENDING,
                authority.UNCERTAIN,
                authority.CORRUPT_LOST,
            ],
        )
        for observation in observations:
            self.assertFalse(authority.evaluate_transition(observation, _advance(1)).eligible)

    def test_repair_genesis_and_new_epoch_are_refused(self):
        reasons = {
            "repair": "repair_refused",
            "genesis": "genesis_not_advancement",
            "new_epoch": "new_epoch_refused",
        }
        for name, reason in reasons.items():
            decision = authority.evaluate_transition(_ready(2), {"action": name})
            self.assertFalse(decision.eligible)
            self.assertEqual(decision.reason, reason)

    def test_observation_is_not_mutated(self):
        observation = _ready(2, pending_generation=3)
        original = copy.deepcopy(observation)
        authority.classify(observation)
        authority.evaluate_transition(observation, _advance(3))
        self.assertEqual(observation, original)

    def test_module_does_not_establish_recovery_or_client_identity(self):
        names = set(dir(authority))
        self.assertTrue(names.isdisjoint({"ACTOR_UID_501", "ACTOR_RECOVERY", "getpeereid"}))
        decision = authority.evaluate_transition(_ready(1), _advance(2))
        self.assertTrue(decision.eligible)
        self.assertFalse(hasattr(decision, "actor_authenticated"))
        self.assertNotIn("actor", inspect.signature(authority.evaluate_transition).parameters)


if __name__ == "__main__":
    unittest.main()
