"""Supervise one provider worker through the durable claim deadline."""

import json
import os
import select
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from max_smart_db.application_validation import SIGNED_INT64_MAX
from max_smart_db.config import (
    EXECUTION_DEADLINE_US,
    RECOVERY_CLOCK_SKEW_MARGIN_US,
)


_ALLOWANCE_US = EXECUTION_DEADLINE_US - RECOVERY_CLOCK_SKEW_MARGIN_US
_MAX_FRAME_BYTES = 1_000_000
_MODEL = "gpt-5.6-sol"


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
    popen=subprocess.Popen,
    stop=None,
    on_worker_start=None,
    before_popen=None,
    stderr=subprocess.DEVNULL,
    socket_port=None,
    ready_path=None,
):
    """Return the ConversationService provider callable."""

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
            popen=popen,
            stop=stop,
            on_worker_start=on_worker_start,
            before_popen=before_popen,
            stderr=stderr,
            socket_port=socket_port,
            ready_path=ready_path,
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
    popen=subprocess.Popen,
    stop=None,
    on_worker_start=None,
    before_popen=None,
    stderr=subprocess.DEVNULL,
    socket_port=None,
    ready_path=None,
):
    """Run at most one worker. A missed cutoff never starts one."""

    allowance = _budget(claimed_at, wall_clock)
    if allowance is None:
        return _uncertain()
    if allowance <= 0:
        return SupervisionResult("definite_non_send")
    deadline_ns = monotonic_ns() + allowance * 1_000
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
    }
    try:
        proc = spawn_worker(job, popen=popen, stderr=stderr)
    except Exception:
        return _uncertain()
    try:
        if on_worker_start is not None:
            on_worker_start(proc.pid)
        return _finish(proc, deadline_ns, job, stop)
    finally:
        if proc.poll() is None:
            _stop(proc, stop)
        _close_pipes(proc)


def spawn_worker(job, *, popen=subprocess.Popen, stderr=subprocess.DEVNULL):
    """Start one session-leading worker. The job travels on stdin."""

    payload = json.dumps(job, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload += b"\n"
    proc = popen(
        [sys.executable, "-m", "provider_worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr,
        close_fds=True,
        start_new_session=True,
        env=_worker_env(),
    )
    try:
        proc.stdin.write(payload)
        proc.stdin.close()
    except Exception:
        _stop(proc, None)
        raise
    return proc


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
    if proc.poll() is None and _has_line(frame):
        remaining = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
        if remaining > 0:
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                pass
    now = time.monotonic_ns()
    if proc.poll() is None or now >= deadline_ns:
        _stop(proc, stop)
        return _uncertain(worker_started=True)
    if proc.returncode != 0:
        return _uncertain(worker_started=True)
    return _classify(frame, job, worker_started=True)


def _read_until(proc, deadline_ns):
    descriptor = proc.stdout.fileno()
    buffer = bytearray()
    while time.monotonic_ns() < deadline_ns:
        if proc.poll() is not None:
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
    """Reap one directly owned worker.

    True means SIGKILL was delivered to that worker's own process group and
    this direct child was reaped. A group that is already gone, or a killpg
    failure, still reaps an exited child through wait, but that reap does not
    claim descendants were signaled. A child that is still running is not
    reported stopped.
    """

    if stop is not None:
        return stop(proc)
    already_reaped = proc.poll() is not None
    signaled = False if already_reaped else _signal_owned_worker_group(proc.pid)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return False
    return signaled and proc.returncode is not None


def _signal_owned_worker_group(pid):
    """SIGKILL the worker session, or False when that group is not established."""

    if type(pid) is not int or pid <= 0 or pid == os.getpid():
        return False
    try:
        group = os.getpgid(pid)
    except OSError:
        return False
    if (
        type(group) is not int
        or group <= 0
        or group != pid
        or group == os.getpgrp()
    ):
        return False
    try:
        os.killpg(group, signal.SIGKILL)
    except OSError:
        return False
    return True


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
