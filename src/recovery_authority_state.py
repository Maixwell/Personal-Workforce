"""Pure authority-state classification and structural eligibility.

The caller prepares a closed observation after its own validation. This module
does not inspect files, authenticate peers, validate snapshots, prove fsync
durability, or establish source identity. Eligibility describes only whether a
requested generation step matches the classified state. It is not
authorization, execution, publication, or acceptance.

No caller-supplied label, username, UID, role, or process identity can make a
transition eligible. The future anchor broker authenticates peers and performs
durable publication.
"""

from dataclasses import dataclass


UNINITIALIZED = "UNINITIALIZED"
READY = "READY"
PENDING = "PENDING"
UNCERTAIN = "UNCERTAIN"
CORRUPT_LOST = "CORRUPT/LOST"
GENERATION_LIMIT = 999999

_OBSERVATION_FIELDS = frozenset(
    {
        "provisioned",
        "genesis_durable",
        "accepted_generation",
        "accepted_snapshot_bound",
        "pending_generation",
        "uncertain",
        "partial_publication",
    }
)


@dataclass(frozen=True)
class AuthorityState:
    """One classified authority state. Generation is None when it does not apply."""

    code: str
    generation: int | None = None


@dataclass(frozen=True)
class TransitionEligibility:
    """Structural eligibility of a requested generation step.

    This is not authentication, execution, publication, or durability.
    """

    eligible: bool
    reason: str


def classify(observation):
    """Return exactly one authority state for a prepared observation."""

    parsed = _parse_observation(observation)
    if parsed is None:
        return _corrupt()
    return _classify_parsed(parsed)


def evaluate_transition(observation, requested_action):
    """Return whether the classified observation structurally allows one step.

    The observation is classified internally. A constructed AuthorityState is
    not accepted as provenance. No actor argument exists.
    """

    state = classify(observation)
    action = _parse_action(requested_action)
    if action is None:
        return _ineligible("action_invalid")
    name, target = action
    if name == "repair":
        return _ineligible("repair_refused")
    if name == "new_epoch":
        return _ineligible("new_epoch_refused")
    if name == "genesis":
        return _ineligible("genesis_not_advancement")
    if name != "advance":
        return _ineligible("action_invalid")
    if state.code != READY:
        return _ineligible("state_ineligible")
    generation = state.generation
    if type(generation) is not int or isinstance(generation, bool):
        return _ineligible("state_ineligible")
    if generation < 0 or generation >= GENERATION_LIMIT:
        return _ineligible("generation_limit")
    if target != generation + 1:
        return _ineligible("generation_step_refused")
    return TransitionEligibility(True, "structurally_eligible")


def _parse_observation(observation):
    if type(observation) is not dict or set(observation) != _OBSERVATION_FIELDS:
        return None
    provisioned = observation["provisioned"]
    genesis = observation["genesis_durable"]
    accepted = observation["accepted_generation"]
    snapshot = observation["accepted_snapshot_bound"]
    pending = observation["pending_generation"]
    uncertain = observation["uncertain"]
    partial = observation["partial_publication"]
    if (
        type(provisioned) is not bool
        or type(genesis) is not bool
        or type(snapshot) is not bool
        or type(uncertain) is not bool
        or type(partial) is not bool
        or _invalid_optional_generation(accepted)
        or _invalid_optional_generation(pending)
    ):
        return None
    return {
        "provisioned": provisioned,
        "genesis_durable": genesis,
        "accepted_generation": accepted,
        "accepted_snapshot_bound": snapshot,
        "pending_generation": pending,
        "uncertain": uncertain,
        "partial_publication": partial,
    }


def _classify_parsed(observation):
    provisioned = observation["provisioned"]
    genesis = observation["genesis_durable"]
    accepted = observation["accepted_generation"]
    snapshot = observation["accepted_snapshot_bound"]
    pending = observation["pending_generation"]
    uncertain = observation["uncertain"]
    partial = observation["partial_publication"]
    has_record = (
        genesis
        or accepted is not None
        or pending is not None
        or snapshot
        or uncertain
        or partial
    )
    if not provisioned:
        if has_record:
            return _corrupt()
        return AuthorityState(UNINITIALIZED, None)
    if snapshot != (accepted is not None):
        return _corrupt()
    if (accepted is not None or pending is not None) and not genesis:
        return _corrupt()
    if _pending_conflicts(accepted, pending):
        return _corrupt()
    if uncertain or partial:
        if not genesis:
            return _corrupt()
        return AuthorityState(UNCERTAIN, pending if pending is not None else accepted)
    if not genesis:
        return _corrupt()
    if pending is not None and accepted is not None and pending == accepted:
        return AuthorityState(UNCERTAIN, pending)
    if pending is not None:
        return AuthorityState(PENDING, pending)
    if accepted is None:
        return AuthorityState(READY, 0)
    return AuthorityState(READY, accepted)


def _pending_conflicts(accepted, pending):
    if pending is None:
        return False
    if accepted is None:
        return pending != 1
    if pending == accepted:
        return False
    return pending != accepted + 1


def _invalid_optional_generation(value):
    if value is None:
        return False
    if type(value) is not int or isinstance(value, bool):
        return True
    return value < 1 or value > GENERATION_LIMIT


def _parse_action(requested_action):
    if type(requested_action) is not dict or "action" not in requested_action:
        return None
    name = requested_action["action"]
    if type(name) is not str:
        return None
    if name == "advance":
        if set(requested_action) != {"action", "target_generation"}:
            return None
        target = requested_action["target_generation"]
        if (
            type(target) is not int
            or isinstance(target, bool)
            or target < 1
            or target > GENERATION_LIMIT
        ):
            return None
        return name, target
    if name in {"repair", "new_epoch", "genesis"}:
        if set(requested_action) != {"action"}:
            return None
        return name, None
    return None


def _corrupt():
    return AuthorityState(CORRUPT_LOST, None)


def _ineligible(reason):
    return TransitionEligibility(False, reason)
