"""Isolated provider worker. It has no database or repository access."""

import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time

from provider_fence import ProviderFenceConfig, acquire_worker_fence


_MAX_JOB_BYTES = 1_000_000
_MODEL = "gpt-5.6-sol"
_FAKE_SCENARIOS = frozenset({
    "ok",
    "blank",
    "nul",
    "block_read",
    "block_read_child",
    "crash",
    "partial",
    "frame_then_block",
    "not_sent",
    "double_call",
})


def main():
    try:
        raw = sys.stdin.buffer.read(_MAX_JOB_BYTES + 1)
    except Exception:
        os._exit(1)
    _silence_stderr()
    try:
        _run_job(raw)
    except Exception:
        os._exit(1)


def _silence_stderr():
    sys.tracebacklimit = 0
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 2)
    finally:
        os.close(devnull)


def _run_job(raw):
    if len(raw) > _MAX_JOB_BYTES or not raw.endswith(b"\n"):
        os._exit(1)
    job = json.loads(raw.decode("utf-8"))
    deadline_ns = job["deadline_monotonic_ns"]
    parent_pid = job["parent_pid"]
    if type(deadline_ns) is not int or type(parent_pid) is not int:
        os._exit(1)
    if not _arm_guards(parent_pid, deadline_ns):
        _emit(_not_sent(job))
        return
    if time.monotonic_ns() >= deadline_ns:
        _emit(_not_sent(job))
        return
    if not _authorize(job, deadline_ns):
        _emit(_not_sent(job))
        return
    if time.monotonic_ns() >= deadline_ns:
        _emit(_not_sent(job))
        return
    _execute(job)


def _authorize(job, deadline_ns):
    """Lock the fence, report LOCKED, and wait for one GO on the control fd."""

    path = job.get("fence_path")
    device = job.get("fence_device")
    inode = job.get("fence_inode")
    mode = job.get("mode")
    descriptor = job.get("control_fd")
    if (
        type(path) is not str
        or type(device) is not int
        or type(inode) is not int
        or mode not in ("fake", "openai")
        or type(descriptor) is not int
        or descriptor < 0
    ):
        return False
    try:
        os.set_inheritable(descriptor, False)
    except OSError:
        return False
    if not acquire_worker_fence(ProviderFenceConfig(path, device, inode)):
        return False
    try:
        _write_locked(descriptor, mode)
    except OSError:
        return False
    if _read_control(descriptor, deadline_ns) != "go":
        return False
    if time.monotonic_ns() >= deadline_ns:
        return False
    watcher = threading.Thread(
        target=_watch_control,
        args=(descriptor,),
        daemon=True,
    )
    watcher.start()
    return True


def _watch_control(descriptor):
    try:
        while True:
            readable, _, _ = select.select([descriptor], [], [], 1)
            if not readable:
                continue
            chunk = os.read(descriptor, 1024)
            break
    except Exception:
        chunk = b""
    os.killpg(os.getpgrp(), signal.SIGKILL)


def _write_locked(descriptor, mode):
    payload = json.dumps(
        {"type": "locked", "mode": mode},
        separators=(",", ":"),
    ).encode("utf-8")
    payload += b"\n"
    view = payload
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("control_write")
        view = view[written:]


def _read_control(descriptor, deadline_ns):
    buffer = bytearray()
    while time.monotonic_ns() < deadline_ns:
        remaining = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
        if remaining <= 0:
            return None
        try:
            readable, _, _ = select.select(
                [descriptor],
                [],
                [],
                min(remaining, 0.05),
            )
        except (OSError, ValueError):
            return None
        if not readable:
            continue
        try:
            chunk = os.read(descriptor, 1024)
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
        if type(parsed) is not dict or parsed.get("type") != "go":
            return None
        return "go"
    return None


def _arm_guards(parent_pid, deadline_ns):
    if parent_pid <= 0:
        return False
    parent_ready = threading.Event()
    deadline_ready = threading.Event()
    threading.Thread(
        target=_watch_parent,
        args=(parent_pid, parent_ready),
        daemon=True,
    ).start()
    threading.Thread(
        target=_watch_deadline,
        args=(deadline_ns, deadline_ready),
        daemon=True,
    ).start()
    return (
        parent_ready.wait(timeout=2)
        and deadline_ready.wait(timeout=2)
    )


def _watch_parent(parent_pid, ready):
    try:
        queue = select.kqueue()
        event = select.kevent(
            parent_pid,
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
            fflags=select.KQ_NOTE_EXIT,
        )
        queue.control([event], 0)
    except Exception:
        os.killpg(os.getpgrp(), signal.SIGKILL)
        return
    ready.set()
    queue.control(None, 1)
    os.killpg(os.getpgrp(), signal.SIGKILL)


def _watch_deadline(deadline_ns, ready):
    ready.set()
    delay_ns = deadline_ns - time.monotonic_ns()
    if delay_ns > 0:
        time.sleep(delay_ns / 1_000_000_000)
    os.killpg(os.getpgrp(), signal.SIGKILL)


def _execute(job):
    mode = job.get("mode")
    if mode == "fake":
        scenario = job.get("fake")
        if scenario not in _FAKE_SCENARIOS:
            _emit(_uncertain(job, calls=0))
            return
        if scenario == "crash":
            os._exit(1)
        if scenario == "partial":
            sys.stdout.buffer.write(b"{")
            sys.stdout.flush()
            os._exit(0)
        if scenario == "not_sent":
            _emit(_not_sent(job))
            return
        client = _adapter_client(_fake_create(job), timeout_seconds=job["timeout_seconds"])
    elif mode == "openai":
        client = _adapter_client(
            _openai_create(job["api_key"], job["timeout_seconds"]),
            timeout_seconds=job["timeout_seconds"],
        )
        scenario = "openai"
    else:
        _emit(_uncertain(job, calls=0))
        return
    if time.monotonic_ns() >= job["deadline_monotonic_ns"]:
        _emit(_not_sent(job))
        return
    calls = 2 if scenario == "double_call" else 1
    output = None
    for _ in range(calls):
        output = _perform_request(
            client,
            standing_context=job["standing_context"],
            request_content=job["request_content"],
            timeout_seconds=job["timeout_seconds"],
        )
    _emit(_completed(job, client, output))


def _adapter_client(create, *, timeout_seconds):
    client = _CallClient(create, timeout_seconds)
    return client


class _CallClient:
    def __init__(self, create, timeout_seconds):
        self._create = create
        self.max_retries = 0
        self.timeout = timeout_seconds
        self.calls = 0
        self.kwargs = None
        self.responses = self

    def create(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        return self._create(**kwargs)


def _fake_create(job):
    scenario = job["fake"]

    def create(**kwargs):
        if scenario == "frame_then_block":
            _write_frame(_completed(job, _FrameView(kwargs), "should-not-accept"))
            _block_until_killed(job)
            raise RuntimeError("socket_closed")
        if scenario in ("block_read", "block_read_child"):
            _block_until_killed(job)
            raise RuntimeError("socket_closed")
        if scenario == "blank":
            text = ""
        elif scenario == "nul":
            text = "\x00"
        else:
            text = "answer"
        return _Text(text)

    return create


class _FrameView:
    """Evidence for a frame written from inside a blocked request."""

    calls = 1
    max_retries = 0

    def __init__(self, kwargs):
        self.kwargs = kwargs


class _Text:
    def __init__(self, output_text):
        self.output_text = output_text


def _openai_create(api_key, timeout_seconds):
    from openai import OpenAI

    client = OpenAI(api_key=api_key, max_retries=0, timeout=timeout_seconds)

    def create(**kwargs):
        return client.responses.create(**kwargs)

    return create


def _perform_request(client, *, standing_context, request_content, timeout_seconds):
    response = client.responses.create(
        model=_MODEL,
        instructions=standing_context,
        input=request_content,
        store=False,
        timeout=timeout_seconds,
    )
    output = response.output_text
    if type(output) is not str:
        raise RuntimeError("output_type")
    return output


def _block_until_killed(job):
    if job.get("fake") == "block_read_child":
        _spawn_same_group_descendant(job)
    port = job["socket_port"]
    if type(port) is not int or port < 1 or port > 65535:
        os._exit(1)
    connected = socket.create_connection(("127.0.0.1", port))
    _publish_ready(job)
    connected.recv(1)


def _spawn_same_group_descendant(job):
    descendant = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=False,
    )
    job["descendant_pid"] = descendant.pid


def _publish_ready(job):
    path = job.get("ready_path")
    if type(path) is not str or path == "":
        return
    payload = json.dumps({
        "worker": os.getpid(),
        "child": job.get("descendant_pid"),
    }).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)


def _completed(job, client, output):
    kwargs = client.kwargs or {}
    return {
        "kind": "completed",
        "operation_id": job["operation_id"],
        "runner_id": job["runner_id"],
        "claimed_at": job["claimed_at"],
        "output": output,
        "calls": client.calls,
        "max_retries": client.max_retries,
        "store": kwargs.get("store"),
        "model": kwargs.get("model"),
        "openai_imported": "openai" in sys.modules,
        "request_bound": (
            kwargs.get("input") == job["request_content"]
            and kwargs.get("instructions") == job["standing_context"]
            and kwargs.get("timeout") == job["timeout_seconds"]
        ),
    }


def _not_sent(job):
    return _uncertain(job, calls=0, kind="not_sent")


def _uncertain(job, calls, kind="uncertain"):
    return {
        "kind": kind,
        "operation_id": job.get("operation_id"),
        "runner_id": job.get("runner_id"),
        "claimed_at": job.get("claimed_at"),
        "calls": calls,
        "max_retries": 0,
        "store": False,
        "model": _MODEL,
        "openai_imported": "openai" in sys.modules,
    }


def _emit(frame):
    _write_frame(frame)


def _write_frame(frame):
    sys.stdout.buffer.write(json.dumps(frame, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
