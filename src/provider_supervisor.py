"""Supervise one provider worker through the durable claim deadline.

The worker, not this supervisor, holds the global provider fence. After the
worker reports LOCKED, a fresh durable claim proof is required before GO.
No proof, a missed cutoff, or a lost control channel withholds GO. The
original allowance is not extended while the handshake waits. An unsafe
two-sample clock reading refuses dispatch before a worker starts and does
not change that allowance.
"""

import ctypes
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from ctypes.util import find_library
from dataclasses import dataclass
from pathlib import Path

from max_smart_db.application_validation import SIGNED_INT64_MAX
from max_smart_db.clock_safety import (
    ClockSampleAccepted,
    MonotonicNanosecondClock,
    UtcMicrosecondClock,
    sample_clock_safety,
)
from max_smart_db.config import (
    EXECUTION_DEADLINE_US,
    RECOVERY_CLOCK_SKEW_MARGIN_US,
)


_ALLOWANCE_US = EXECUTION_DEADLINE_US - RECOVERY_CLOCK_SKEW_MARGIN_US
_MAX_FRAME_BYTES = 1_000_000
_MODEL = "gpt-5.6-sol"


class _MissingFence:
    pass


_UNSET = _MissingFence()


@dataclass(frozen=True)
class SupervisionResult:
    """Local outcome of one supervised provider attempt."""

    classification: str
    output: str | None = None
    worker_started: bool = False
    calls: int | None = None
    max_retries: int | None = None
    store: bool | None = None
    model: str | None = None


def build_service_provider(
    *,
    wall_clock,
    fake,
    api_key="",
    monotonic_ns=time.monotonic_ns,
    safety_wall_clock=None,
    safety_monotonic_clock=None,
    popen=subprocess.Popen,
    stop=None,
    on_worker_start=None,
    before_popen=None,
    stderr=subprocess.DEVNULL,
    socket_port=None,
    ready_path=None,
    fence_config=_UNSET,
    prove_claim=None,
):
    """Return the ConversationService provider callable."""

    if fake is None:
        prove_claim = None
    checker = _durable_claim_matches if prove_claim is None else prove_claim

    def provider(
        *,
        actor_id,
        conversation_id,
        operation_id,
        runner_id,
        claimed_at,
        request_content,
        standing_context,
    ):
        def prove():
            try:
                return checker(
                    actor_id=actor_id,
                    operation_id=operation_id,
                    runner_id=runner_id,
                    claimed_at=claimed_at,
                ) is True
            except Exception:
                return False

        result = supervise(
            claimed_at=claimed_at,
            operation_id=str(operation_id),
            runner_id=runner_id,
            request_content=request_content,
            standing_context=standing_context,
            wall_clock=wall_clock,
            api_key=api_key,
            fake=fake,
            monotonic_ns=monotonic_ns,
            safety_wall_clock=safety_wall_clock,
            safety_monotonic_clock=safety_monotonic_clock,
            popen=popen,
            stop=stop,
            on_worker_start=on_worker_start,
            before_popen=before_popen,
            stderr=stderr,
            socket_port=socket_port,
            ready_path=ready_path,
            fence_config=fence_config,
            actor_id=actor_id,
            prove_claim=prove,
        )
        return _to_service(result)

    return provider


def supervise(
    *,
    claimed_at,
    operation_id,
    runner_id,
    request_content,
    standing_context,
    wall_clock,
    api_key="",
    fake=None,
    monotonic_ns=time.monotonic_ns,
    safety_wall_clock=None,
    safety_monotonic_clock=None,
    popen=subprocess.Popen,
    stop=None,
    on_worker_start=None,
    before_popen=None,
    stderr=subprocess.DEVNULL,
    socket_port=None,
    ready_path=None,
    fence_config=_UNSET,
    actor_id=None,
    prove_claim=None,
):
    """Run at most one worker. A missed cutoff never starts one."""

    if not _clock_dispatch_allowed(safety_wall_clock, safety_monotonic_clock):
        return SupervisionResult("definite_non_send", calls=0)
    allowance = _budget(claimed_at, wall_clock)
    if allowance is None:
        return _uncertain()
    if allowance <= 0:
        return SupervisionResult("definite_non_send")
    if fake is None:
        def prove_claim():
            try:
                return _durable_claim_matches(
                    actor_id=actor_id,
                    operation_id=operation_id,
                    runner_id=runner_id,
                    claimed_at=claimed_at,
                ) is True
            except Exception:
                return False
    deadline_ns = monotonic_ns() + allowance * 1_000
    config = _select_fence_config(fence_config)
    if config is None or not _fence_matches(config):
        return SupervisionResult("definite_non_send", calls=0)
    if before_popen is not None:
        before_popen()
    if monotonic_ns() >= deadline_ns:
        return SupervisionResult("definite_non_send")
    timeout_seconds = allowance / 1_000_000
    job = {
        "parent_pid": os.getpid(),
        "deadline_monotonic_ns": deadline_ns,
        "mode": "fake" if fake is not None else "openai",
        "fake": fake,
        "api_key": api_key,
        "request_content": request_content,
        "standing_context": standing_context,
        "operation_id": operation_id,
        "runner_id": runner_id,
        "claimed_at": claimed_at,
        "timeout_seconds": timeout_seconds,
        "socket_port": socket_port,
        "ready_path": ready_path,
        "fence_path": config.path,
        "fence_device": config.device,
        "fence_inode": config.inode,
    }
    def start_worker():
        parent, child = socket.socketpair()
        try:
            child.set_inheritable(True)
            payload_job = dict(job)
            payload_job["control_fd"] = child.fileno()
            payload = json.dumps(
                payload_job,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            payload += b"\n"
            started = popen(
                [sys.executable, "-m", "provider_worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                close_fds=True,
                start_new_session=True,
                pass_fds=(child.fileno(),),
                env=_worker_env(),
            )
        except Exception:
            parent.close()
            child.close()
            raise
        child.close()
        parent.set_inheritable(False)
        try:
            started.stdin.write(payload)
            started.stdin.close()
        except Exception:
            parent.close()
            _stop(started, None)
            raise
        return started, parent

    proc = None
    control = None
    try:
        proc, control = start_worker()
    except Exception:
        return _uncertain()
    try:
        if _handshake(proc, control, deadline_ns, prove_claim, config) != "go":
            _close_control(control)
            control = None
        if on_worker_start is not None:
            on_worker_start(proc.pid)
        return _finish(proc, deadline_ns, job, stop)
    finally:
        if proc is not None and proc.returncode is None:
            _stop(proc, stop)
        _close_control(control)
        if proc is not None:
            _close_pipes(proc)


def _clock_dispatch_allowed(wall_clock, monotonic_clock):
    """Accept dispatch only when the existing probe accepts both clocks."""

    if wall_clock is None:
        wall_clock = UtcMicrosecondClock()
    if monotonic_clock is None:
        monotonic_clock = MonotonicNanosecondClock()
    return type(sample_clock_safety(wall_clock, monotonic_clock)) is ClockSampleAccepted


def _budget(claimed_at, wall_clock):
    if (
        type(claimed_at) is not int
        or claimed_at < 0
        or claimed_at > SIGNED_INT64_MAX - _ALLOWANCE_US
    ):
        return None
    try:
        now = wall_clock()
    except Exception:
        return None
    if type(now) is not int:
        return None
    if now < claimed_at:
        return _ALLOWANCE_US
    return claimed_at + _ALLOWANCE_US - now


def _finish(proc, deadline_ns, job, stop):
    frame = _read_until(proc, deadline_ns)
    if _has_line(frame) and _direct_child_state(proc.pid) == "alive":
        _pause_until_exit(proc.pid, deadline_ns)
    now = time.monotonic_ns()
    if _direct_child_state(proc.pid) == "alive" or now >= deadline_ns:
        _stop(proc, stop)
        return _uncertain(worker_started=True)
    _reap_after_group_signal(proc)
    if proc.returncode != 0:
        return _uncertain(worker_started=True)
    return _classify(frame, job, worker_started=True)


def _read_until(proc, deadline_ns):
    descriptor = proc.stdout.fileno()
    buffer = bytearray()
    while time.monotonic_ns() < deadline_ns:
        if _direct_child_state(proc.pid) != "alive":
            _drain(descriptor, buffer)
            break
        remaining = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
        if remaining <= 0:
            break
        readable, _, _ = select.select([descriptor], [], [], min(remaining, 0.05))
        if not readable:
            continue
        chunk = os.read(descriptor, 65536)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > _MAX_FRAME_BYTES or b"\n" in buffer:
            break
    return bytes(buffer)


def _drain(descriptor, buffer):
    while len(buffer) <= _MAX_FRAME_BYTES:
        readable, _, _ = select.select([descriptor], [], [], 0)
        if not readable:
            return
        chunk = os.read(descriptor, 65536)
        if not chunk:
            return
        buffer.extend(chunk)
        if b"\n" in buffer:
            return


def _has_line(frame):
    return b"\n" in frame


def _close_pipes(proc):
    stream = getattr(proc, "stdout", None)
    if stream is not None and not getattr(stream, "closed", False):
        stream.close()


def _stop(proc, stop):
    """Reap one directly owned worker after a proven group signal, if any.

    True means SIGKILL reached that worker's own process group and this
    direct child was reaped. A missing, mismatched, or already reaped group
    is not signaled. A killpg failure still reaps an exited child, but that
    reap does not claim descendants were signaled. A child that is still
    running is not reported stopped.
    """

    if stop is not None:
        return stop(proc)
    return _reap_after_group_signal(proc)


def _reap_after_group_signal(proc):
    """Signal a proven worker group before the first reap, then wait."""

    if proc.returncode is not None:
        _close_pipes(proc)
        return False
    state = _direct_child_state(proc.pid)
    signaled = False
    if state == "alive":
        signaled = _signal_owned_worker_group(proc.pid)
        if not signaled and _direct_child_state(proc.pid) == "exited":
            signaled = _signal_exited_leader_group(proc.pid)
    elif state == "exited":
        signaled = _signal_exited_leader_group(proc.pid)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        return False
    _close_pipes(proc)
    return signaled and proc.returncode is not None


def _signal_owned_worker_group(pid):
    """SIGKILL a live worker session, or False when that group is not proven."""

    if type(pid) is not int or pid <= 0 or pid == os.getpid():
        return False
    if _direct_child_state(pid) != "alive":
        return False
    try:
        group = os.getpgid(pid)
    except OSError:
        return False
    if not _owned_group(pid, group):
        return False
    try:
        os.killpg(group, signal.SIGKILL)
    except OSError:
        return False
    return True


def _signal_exited_leader_group(pid):
    """SIGKILL the exited leader's group only while its pid remains unreaped.

    macOS getpgid fails for a zombie session leader. A live member whose
    group id is still that unreaped pid proves the original group. The pid
    is never used as a signal target after it has been reaped.
    """

    if not _exited_group_has_live_member(pid):
        return False
    if _direct_child_state(pid) != "exited":
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    if not _owned_group(pid, pid):
        return False
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        return False
    return True


def _owned_group(pid, group):
    return (
        type(group) is int
        and group > 0
        and group == pid
        and group != os.getpgrp()
        and group != os.getpid()
    )


def _direct_child_state(pid):
    """Return alive, exited, or unproven. This wait does not reap the child."""

    if type(pid) is not int or pid <= 0 or pid == os.getpid():
        return "unproven"
    try:
        info = os.waitid(
            os.P_PID,
            pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
    except OSError:
        return "unproven"
    if info is None:
        return "alive"
    if getattr(info, "si_pid", None) != pid:
        return "unproven"
    return "exited"


def _pause_until_exit(pid, deadline_ns):
    while time.monotonic_ns() < deadline_ns:
        if _direct_child_state(pid) != "alive":
            return
        remaining = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining))


def _exited_group_has_live_member(pid):
    if _direct_child_state(pid) != "exited" or not _owned_group(pid, pid):
        return False
    for candidate in _process_ids():
        if type(candidate) is not int or candidate <= 0 or candidate == pid:
            continue
        try:
            group = os.getpgid(candidate)
        except OSError:
            continue
        if group == pid and _direct_child_state(pid) == "exited":
            return True
    return False


def _process_ids():
    """Return currently listed pids, or none when the listing cannot be proven."""

    try:
        library = find_library("c")
        if not library:
            return ()
        libc = ctypes.CDLL(library, use_errno=True)
        list_pids = libc.proc_listallpids
        list_pids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        list_pids.restype = ctypes.c_int
        estimated = list_pids(None, 0)
        if estimated <= 0 or estimated > 100_000:
            return ()
        capacity = estimated + 64
        buffer = (ctypes.c_int * capacity)()
        count = list_pids(buffer, ctypes.sizeof(buffer))
    except (OSError, AttributeError):
        return ()
    if count <= 0 or count > capacity:
        return ()
    return tuple(buffer[index] for index in range(count))


def _classify(frame, job, *, worker_started):
    if len(frame) > _MAX_FRAME_BYTES:
        return _uncertain(worker_started=worker_started)
    line, separator, extra = frame.partition(b"\n")
    if separator != b"\n" or extra != b"":
        return _uncertain(worker_started=worker_started)
    try:
        parsed = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _uncertain(worker_started=worker_started)
    if type(parsed) is not dict:
        return _uncertain(worker_started=worker_started)
    if not _same_claim(parsed, job):
        return _uncertain(worker_started=worker_started)
    kind = parsed.get("kind")
    calls = parsed.get("calls")
    retries = parsed.get("max_retries")
    store = parsed.get("store")
    model = parsed.get("model")
    imported = parsed.get("openai_imported")
    if (
        type(calls) is not int
        or type(retries) is not int
        or type(store) is not bool
        or model != _MODEL
        or type(imported) is not bool
    ):
        return _uncertain(worker_started=worker_started)
    if job["mode"] == "fake" and imported is True:
        return _uncertain(worker_started=worker_started)
    if kind == "not_sent" and calls == 0 and retries == 0 and store is False:
        return SupervisionResult(
            "definite_non_send",
            worker_started=worker_started,
            calls=0,
            max_retries=0,
            store=False,
            model=_MODEL,
        )
    if (
        kind != "completed"
        or calls != 1
        or retries != 0
        or store is not False
        or parsed.get("request_bound") is not True
    ):
        return _uncertain(worker_started=worker_started)
    output = parsed.get("output")
    if not _usable(output):
        return SupervisionResult(
            "invalid_output",
            worker_started=worker_started,
            calls=1,
            max_retries=0,
            store=False,
            model=_MODEL,
        )
    return SupervisionResult(
        "success",
        output=output,
        worker_started=worker_started,
        calls=1,
        max_retries=0,
        store=False,
        model=_MODEL,
    )


def _same_claim(parsed, job):
    return (
        parsed.get("operation_id") == job["operation_id"]
        and parsed.get("runner_id") == job["runner_id"]
        and parsed.get("claimed_at") == job["claimed_at"]
    )


def _usable(text):
    if type(text) is not str:
        return False
    try:
        text.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return "\x00" not in text and bool(text.strip())


def _uncertain(worker_started=False):
    return SupervisionResult("uncertain", worker_started=worker_started)


def _handshake(proc, control, deadline_ns, prove_claim, fence_config):
    """LOCKED, then a fresh proof, then one GO. The budget is not reset."""

    if _read_control(proc, control, deadline_ns) != "locked":
        return "withheld"
    if time.monotonic_ns() >= deadline_ns or prove_claim is None:
        return "withheld"
    try:
        proven = prove_claim()
    except Exception:
        return "withheld"
    if proven is not True or time.monotonic_ns() >= deadline_ns:
        return "withheld"
    if not _fence_matches(fence_config):
        return "withheld"
    try:
        control.sendall(b'{"type":"go"}\n')
    except Exception:
        return "go"
    return "go"


def _read_control(proc, control, deadline_ns):
    if control is None:
        return None
    buffer = bytearray()
    while time.monotonic_ns() < deadline_ns:
        if _direct_child_state(proc.pid) != "alive":
            return None
        remaining = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
        if remaining <= 0:
            return None
        try:
            readable, _, _ = select.select([control], [], [], min(remaining, 0.05))
        except (OSError, ValueError):
            return None
        if not readable:
            continue
        try:
            chunk = control.recv(1024)
        except OSError:
            return None
        if not chunk:
            return None
        buffer.extend(chunk)
        if b"\n" not in buffer:
            if len(buffer) > 256:
                return None
            continue
        line, separator, extra = bytes(buffer).partition(b"\n")
        if separator != b"\n" or extra != b"":
            return None
        try:
            parsed = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if type(parsed) is not dict:
            return None
        kind = parsed.get("type")
        if kind not in ("locked", "go"):
            return None
        return kind
    return None


def _close_control(control):
    if control is None:
        return
    try:
        control.close()
    except OSError:
        pass


def _select_fence_config(fence_config):
    from provider_fence import ProviderFenceConfig, canonical_fence_config, shared_fence_config

    if fence_config is _UNSET:
        shared = shared_fence_config()
        if type(shared) is not ProviderFenceConfig:
            return None
        return shared
    return canonical_fence_config(fence_config)


def _fence_matches(config):
    from provider_fence import fence_matches_config

    return fence_matches_config(config)


def _durable_claim_matches(*, actor_id, operation_id, runner_id, claimed_at):
    from uuid import UUID

    from max_smart_db.claim_confirmation import confirm_durable_claim
    from max_smart_db.repository import DurableClaimConfirmed

    if type(operation_id) is str:
        try:
            operation_id = UUID(operation_id)
        except ValueError:
            return False
    if type(operation_id) is not UUID or type(claimed_at) is not int:
        return False
    proof = confirm_durable_claim(
        actor_id,
        operation_id,
        runner_id,
        claimed_at,
    )
    return (
        type(proof) is DurableClaimConfirmed
        and proof.operation_id == operation_id
        and proof.runner_id == runner_id
        and proof.claimed_at == claimed_at
    )


def _worker_env():
    environment = os.environ.copy()
    root = str(Path(__file__).resolve().parent)
    current = environment.get("PYTHONPATH", "")
    parts = [part for part in current.split(os.pathsep) if part]
    if root not in parts:
        environment["PYTHONPATH"] = os.pathsep.join((root, *parts)) if parts else root
    return environment


def _to_service(result):
    from conversation_service import ProviderFailure
    from max_smart_db.repository import (
        DefiniteProviderCategory,
        UncertainProviderCategory,
    )

    if result.classification == "success":
        return result.output
    if result.classification == "invalid_output":
        return ProviderFailure(DefiniteProviderCategory.PROVIDER_OUTPUT_INVALID)
    if result.classification == "definite_non_send":
        return ProviderFailure(DefiniteProviderCategory.PROVIDER_DEFINITE_FAILURE)
    return ProviderFailure(UncertainProviderCategory.PROVIDER_OUTCOME_UNCERTAIN)
