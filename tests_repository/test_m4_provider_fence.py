"""Disposable tests for the global provider fence and its recovery handshake."""

import errno
import fcntl
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
PRODUCTION_DATABASE = PROJECT_ROOT / "data" / "max_smart.sqlite3"
PRODUCTION_FENCE = PROJECT_ROOT / "data" / "provider.fence"
SESSION_FILE = PROJECT_ROOT / "data" / "current_session.json"
sys.path.insert(0, str(SRC_ROOT))

from provider_fence import (  # noqa: E402
    FenceBusy,
    FenceHeld,
    FenceSafetyError,
    FenceUnproven,
    ProviderFenceConfig,
    acquire_exclusive,
    canonical_fence_config,
    prepare_disposable_fence,
    publish_shared_fence_config,
    release_fence,
    shared_fence_config,
)
from provider_supervisor import supervise  # noqa: E402


RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
OPERATION = "11111111-1111-1111-1111-111111111111"
CLAIMED_AT = 1_000_000
ALLOWANCE_US = 480_000_000


def _directory():
    directory = Path(tempfile.mkdtemp(prefix="provider-fence-")).resolve()
    os.chmod(directory, 0o700)
    return directory


def _private(path):
    path.mkdir()
    os.chmod(path, 0o700)
    return path


def _fence(directory, name="provider.fence"):
    return prepare_disposable_fence(directory / name)


_FENCE_ENV = (
    "MAX_SMART_PROVIDER_FENCE",
    "MAX_SMART_PROVIDER_FENCE_DEVICE",
    "MAX_SMART_PROVIDER_FENCE_INODE",
)


def _drop_published_fence():
    for name in _FENCE_ENV:
        os.environ.pop(name, None)


def _publish(config):
    publish_shared_fence_config(config)


def _spawn_fake_worker(job, *, popen=subprocess.Popen, stderr=subprocess.DEVNULL):
    """Start only a fake worker for tests that must observe its process.

    An OpenAI-mode job is rejected before a process or control socket exists.
    """

    if job.get("mode") != "fake" or type(job.get("fake")) is not str:
        raise RuntimeError("test support cannot start a real provider worker")
    parent, child = socket.socketpair()
    try:
        child.set_inheritable(True)
        payload_job = dict(job)
        payload_job["mode"] = "fake"
        payload_job["control_fd"] = child.fileno()
        payload = json.dumps(
            payload_job,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        environment = os.environ.copy()
        root = str(SRC_ROOT)
        current = environment.get("PYTHONPATH", "")
        parts = [part for part in current.split(os.pathsep) if part]
        if root not in parts:
            environment["PYTHONPATH"] = (
                os.pathsep.join((root, *parts)) if parts else root
            )
        process = popen(
            [sys.executable, "-m", "provider_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            close_fds=True,
            start_new_session=True,
            pass_fds=(child.fileno(),),
            env=environment,
        )
    except Exception:
        parent.close()
        child.close()
        raise
    child.close()
    parent.set_inheritable(False)
    process._provider_control = parent
    try:
        process.stdin.write(payload)
        process.stdin.close()
    except Exception:
        parent.close()
        process.kill()
        raise
    return process


def _read_worker_frame(process, deadline_ns):
    control = getattr(process, "_provider_control", None)
    if control is None:
        return None
    previous = control.gettimeout()
    buffer = bytearray()
    try:
        while time.monotonic_ns() < deadline_ns:
            remaining = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
            if remaining <= 0:
                return None
            control.settimeout(min(remaining, 0.05))
            try:
                chunk = control.recv(1024)
            except (TimeoutError, socket.timeout):
                continue
            if not chunk:
                return None
            buffer.extend(chunk)
            if b"\n" not in buffer:
                continue
            line, _, extra = bytes(buffer).partition(b"\n")
            if extra:
                return None
            try:
                parsed = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            if type(parsed) is not dict:
                return None
            return parsed
        return None
    finally:
        try:
            control.settimeout(previous)
        except OSError:
            pass


def _release_fake_locked_worker(process, deadline_ns, **_caller):
    """Release only a worker that itself reported fake mode.

    A caller-supplied mode label is ignored. The GO bytes live in this test
    helper, not in a production authorization function.
    """

    frame = _read_worker_frame(process, deadline_ns)
    if (
        type(frame) is not dict
        or frame.get("type") != "locked"
        or frame.get("mode") != "fake"
    ):
        return False
    if time.monotonic_ns() >= deadline_ns:
        return False
    process._provider_control.sendall(b'{"type":"go"}\n')
    return True


def _job(fence, deadline_ns, **overrides):
    job = {
        "parent_pid": os.getpid(),
        "deadline_monotonic_ns": deadline_ns,
        "mode": "fake",
        "fake": "ok",
        "api_key": "",
        "request_content": "hello",
        "standing_context": "standing",
        "operation_id": OPERATION,
        "runner_id": RUNNER,
        "claimed_at": CLAIMED_AT,
        "timeout_seconds": 1,
        "socket_port": None,
        "ready_path": None,
        "fence_path": fence.path,
        "fence_device": fence.device,
        "fence_inode": fence.inode,
    }
    job.update(overrides)
    return job


def _supervise(fence, **overrides):
    arguments = {
        "claimed_at": CLAIMED_AT,
        "operation_id": OPERATION,
        "runner_id": RUNNER,
        "request_content": "hello",
        "standing_context": "standing",
        "wall_clock": lambda: CLAIMED_AT,
        "fake": "ok",
        "fence_config": fence,
        "prove_claim": lambda: True,
    }
    arguments.update(overrides)
    return supervise(**arguments)


def _hold(config):
    process = subprocess.Popen(
        (sys.executable, "-B", "-c", _HOLDER),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": str(SRC_ROOT),
            "FENCE_PATH": config.path,
            "FENCE_DEVICE": str(config.device),
            "FENCE_INODE": str(config.inode),
        },
    )
    line = process.stdout.readline().strip()
    if line != f"held {config.inode}":
        error = process.stderr.read()
        process.kill()
        process.wait(timeout=2)
        raise AssertionError(f"{line}\n{error}")
    return process


def _release_holder(process):
    if process.poll() is None:
        try:
            process.stdin.write("\n\n")
            process.stdin.flush()
        except OSError:
            process.kill()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()


def _locked(path):
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            return error.errno in (errno.EAGAIN, errno.EACCES)
        fcntl.lockf(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _close_worker(process):
    control = getattr(process, "_provider_control", None)
    if control is not None:
        process._provider_control = None
        control.close()
    if process.poll() is None:
        try:
            os.kill(process.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    if process.stdout is not None and not process.stdout.closed:
        process.stdout.close()


def _listen(testcase):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    accepted = []

    def accept():
        connection, _address = server.accept()
        accepted.append(connection)

    threading.Thread(target=accept, daemon=True).start()

    def close():
        for connection in accepted:
            connection.close()
        server.close()

    testcase.addCleanup(close)
    return server.getsockname()[1]


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class ProviderFenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session_stat = SESSION_FILE.stat()

    @classmethod
    def setUp(self):
        _drop_published_fence()

    @classmethod
    def tearDownClass(cls):
        _drop_published_fence()
        current = SESSION_FILE.stat()
        if (
            current.st_ino != cls.session_stat.st_ino
            or current.st_size != cls.session_stat.st_size
            or current.st_mtime_ns != cls.session_stat.st_mtime_ns
        ):
            raise AssertionError("terminal session file changed")
        if PRODUCTION_DATABASE.exists():
            raise AssertionError("production database created")
        if PRODUCTION_FENCE.exists():
            raise AssertionError("production fence created")
        leftover = _provider_workers()
        if leftover:
            raise AssertionError(f"provider workers still running: {leftover}")

    def test_locked_proof_and_go_allow_one_request(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        fence = _fence(directory)
        path = Path(fence.path)
        before = path.stat()
        self.assertEqual(path.read_bytes(), b"max-smart-provider-fence-v1\n")
        _publish(fence)
        result = _supervise(fence)
        after = path.stat()
        self.assertEqual(result.classification, "success")
        self.assertEqual(result.calls, 1)
        self.assertEqual(result.output, "answer")
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        self.assertEqual((fence.device, fence.inode), (before.st_dev, before.st_ino))
        held = acquire_exclusive(fence)
        self.assertIsInstance(held, FenceHeld)
        release_fence(held)

    def test_withheld_go_makes_no_provider_request(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        fence = _fence(directory)
        _publish(fence)
        ready = directory / "ready"
        result = _supervise(fence, ready_path=str(ready), prove_claim=lambda: False)
        self.assertEqual(result.classification, "definite_non_send")
        self.assertEqual(result.calls, 0)
        self.assertFalse(ready.exists())

    def test_control_eof_before_go_cannot_dispatch(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        fence = _fence(directory)
        _publish(fence)
        ready = directory / "ready"
        process = _spawn_fake_worker(_job(
            fence,
            time.monotonic_ns() + 5_000_000_000,
            fake="block_read",
            ready_path=str(ready),
        ))
        self.addCleanup(_close_worker, process)
        process._provider_control.close()
        process._provider_control = None
        code = process.wait(timeout=2)
        self.assertEqual(code, 0)
        self.assertFalse(ready.exists())
        frame = json.loads(process.stdout.readline().decode("utf-8"))
        self.assertEqual(frame["kind"], "not_sent")
        self.assertEqual(frame["calls"], 0)

    def test_deadline_during_handshake_does_not_dispatch(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        fence = _fence(directory)
        _publish(fence)
        ready = directory / "ready"
        started = time.monotonic()

        def prove_after_deadline():
            time.sleep(0.4)
            return True

        result = _supervise(
            fence,
            wall_clock=lambda: CLAIMED_AT + ALLOWANCE_US - 200_000,
            fake="block_read",
            ready_path=str(ready),
            prove_claim=prove_after_deadline,
        )
        self.assertNotEqual(result.classification, "success")
        self.assertFalse(ready.exists())
        self.assertLess(time.monotonic() - started, 2)

    def test_worker_crash_before_lock_releases_nothing(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        missing = directory / "provider.fence"
        config = ProviderFenceConfig(str(missing), 1, 1)
        _publish(config)
        process = _spawn_fake_worker(_job(config, time.monotonic_ns() + 2_000_000_000))
        self.addCleanup(_close_worker, process)
        code = process.wait(timeout=2)
        self.assertEqual(code, 0)
        self.assertFalse(missing.exists())
        self.assertIsInstance(acquire_exclusive(config), FenceUnproven)

    def test_worker_crash_after_lock_and_before_go_releases_the_fence(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        fence = _fence(directory)
        _publish(fence)
        ready = directory / "ready"
        process = _spawn_fake_worker(_job(
            fence,
            time.monotonic_ns() + 5_000_000_000,
            fake="block_read",
            ready_path=str(ready),
        ))
        self.addCleanup(_close_worker, process)
        deadline = time.monotonic() + 2
        busy = None
        while time.monotonic() < deadline:
            busy = acquire_exclusive(fence)
            if isinstance(busy, FenceBusy):
                break
            release_fence(busy)
            time.sleep(0.01)
        self.assertIsInstance(busy, FenceBusy)
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
        self.assertFalse(ready.exists())
        held = acquire_exclusive(fence)
        self.assertIsInstance(held, FenceHeld)
        release_fence(held)

    def test_normal_exit_releases_the_lock_and_a_hang_keeps_it(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        fence = _fence(directory)
        _publish(fence)
        result = _supervise(fence)
        self.assertEqual(result.classification, "success")
        held = acquire_exclusive(fence)
        self.assertIsInstance(held, FenceHeld)
        release_fence(held)

        ready = directory / "ready"
        blocked = _supervise(
            fence,
            wall_clock=lambda: CLAIMED_AT + ALLOWANCE_US - 400_000,
            fake="block_read",
            socket_port=_listen(self),
            ready_path=str(ready),
        )
        self.assertEqual(blocked.classification, "uncertain")
        self.assertTrue(ready.exists())
        released = acquire_exclusive(fence)
        self.assertIsInstance(released, FenceHeld)
        release_fence(released)

    def test_hanging_worker_retains_the_lock_until_terminated(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        fence = _fence(directory)
        _publish(fence)
        ready = directory / "ready"
        process = _spawn_fake_worker(_job(
            fence,
            time.monotonic_ns() + 30_000_000_000,
            fake="block_read",
            socket_port=_listen(self),
            ready_path=str(ready),
        ))
        self.addCleanup(_close_worker, process)
        self.assertTrue(_release_fake_locked_worker(
            process,
            time.monotonic_ns() + 2_000_000_000,
            mode="fake",
        ))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.01)
        self.assertTrue(ready.exists())
        self.assertIsInstance(acquire_exclusive(fence), FenceBusy)
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
        held = acquire_exclusive(fence)
        self.assertIsInstance(held, FenceHeld)
        release_fence(held)

    def test_invalid_fence_files_fail_closed_without_replacement(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        fence = _fence(directory)
        path = Path(fence.path)
        original = path.stat()
        missing = ProviderFenceConfig(str(directory / "missing.fence"), 1, 1)
        self.assertIsInstance(acquire_exclusive(missing), FenceUnproven)
        self.assertFalse((directory / "missing.fence").exists())

        os.chmod(path, 0o644)
        self.assertIsInstance(acquire_exclusive(fence), FenceUnproven)
        os.chmod(path, 0o600)

        link = directory / "extra-link"
        os.link(path, link)
        self.assertIsInstance(acquire_exclusive(fence), FenceUnproven)
        os.unlink(link)
        self.assertEqual(path.stat().st_nlink, 1)

        payload = path.read_bytes()
        path.write_bytes(b"corrupt\n")
        os.chmod(path, 0o600)
        self.assertIsInstance(acquire_exclusive(fence), FenceUnproven)
        path.write_bytes(payload)
        os.chmod(path, 0o600)

        replacement = directory / "replacement"
        replacement.write_bytes(payload)
        os.chmod(replacement, 0o600)
        os.unlink(path)
        replacement.rename(path)
        os.chmod(path, 0o600)
        replaced = acquire_exclusive(fence)
        self.assertIsInstance(replaced, FenceUnproven)
        self.assertNotEqual(path.stat().st_ino, original.st_ino)

        os.unlink(path)
        outside = directory / "target"
        outside.write_bytes(payload)
        os.chmod(outside, 0o600)
        os.symlink(outside, path)
        self.assertIsInstance(acquire_exclusive(fence), FenceUnproven)
        self.assertTrue(path.is_symlink())

    def test_production_fence_is_never_created(self):
        with self.assertRaises(FenceSafetyError) as raised:
            prepare_disposable_fence(PRODUCTION_FENCE)
        self.assertEqual(raised.exception.code, "production_fence_refused")
        self.assertFalse(PRODUCTION_FENCE.exists())
        self.assertFalse(PRODUCTION_DATABASE.exists())

    def test_distinct_fence_files_do_not_share_a_lock(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        first = _fence(directory, "one.fence")
        second = _fence(directory, "two.fence")
        held = acquire_exclusive(first)
        self.assertIsInstance(held, FenceHeld)
        other = acquire_exclusive(second)
        self.assertIsInstance(other, FenceHeld)
        release_fence(other)
        release_fence(held)

    def test_descendant_does_not_hold_the_fence_or_run_a_provider(self):
        source = (SRC_ROOT / "provider_worker.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("subprocess.Popen"), 1)
        self.assertIn("close_fds=True", source)
        self.assertIn("time.sleep(120)", source)
        self.assertNotIn("provider_worker", source.split("subprocess.Popen", 1)[1][:400])
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        fence = _fence(directory)
        _publish(fence)
        ready = directory / "ready"
        process = _spawn_fake_worker(_job(
            fence,
            time.monotonic_ns() + 20_000_000_000,
            fake="block_read_child",
            socket_port=_listen(self),
            ready_path=str(ready),
        ))
        self.addCleanup(_close_worker, process)
        self.assertTrue(_release_fake_locked_worker(
            process,
            time.monotonic_ns() + 2_000_000_000,
            mode="fake",
        ))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.01)
        published = json.loads(ready.read_text(encoding="utf-8"))
        child = published["child"]
        self.addCleanup(lambda: os.kill(child, signal.SIGKILL) if _alive(child) else None)
        command = subprocess.run(
            ("ps", "-p", str(child), "-o", "command="),
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        self.assertIn("time.sleep(120)", command)
        self.assertNotIn("provider_worker", command)
        self.assertIsInstance(acquire_exclusive(fence), FenceBusy)
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
        self.assertTrue(_alive(child))
        held = acquire_exclusive(fence)
        self.assertIsInstance(held, FenceHeld)
        release_fence(held)

    def test_supervisor_loss_before_and_after_go(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        fence = _fence(directory)
        _publish(fence)
        early = directory / "early"
        early_worker = _spawn_fake_worker(_job(
            fence,
            time.monotonic_ns() + 5_000_000_000,
            fake="block_read",
            ready_path=str(early),
        ))
        self.addCleanup(_close_worker, early_worker)
        early_worker._provider_control.close()
        early_worker._provider_control = None
        self.assertEqual(early_worker.wait(timeout=2), 0)
        self.assertFalse(early.exists())

        late = directory / "late"
        late_worker = _spawn_fake_worker(_job(
            fence,
            time.monotonic_ns() + 5_000_000_000,
            fake="block_read",
            socket_port=_listen(self),
            ready_path=str(late),
        ))
        self.addCleanup(_close_worker, late_worker)
        self.assertTrue(_release_fake_locked_worker(
            late_worker,
            time.monotonic_ns() + 2_000_000_000,
            mode="fake",
        ))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not late.exists():
            time.sleep(0.01)
        self.assertTrue(late.exists())
        late_worker._provider_control.close()
        late_worker._provider_control = None
        code = late_worker.wait(timeout=2)
        self.assertNotEqual(code, 0)

    def test_repository_races_obey_one_fence(self):
        payload = _payload(self, _run(_RACE_BODY))
        self.assertEqual(payload["go"]["classification"], "success")
        self.assertEqual(payload["go"]["calls"], 1)
        self.assertEqual(payload["go"]["messages"], [
            {"role": "user", "content": "persisted original"},
            {"role": "assistant", "content": "answer"},
        ])
        self.assertEqual(payload["recovery_first"]["classification"], "definite_non_send")
        self.assertEqual(payload["recovery_first"]["calls"], 0)
        self.assertEqual(payload["recovery_first"]["status"], "OUTCOME_UNKNOWN")
        self.assertIsNone(payload["recovery_first"]["slot"])
        self.assertEqual(payload["worker_first"]["recovery"], "LocalProviderActive")
        self.assertEqual(payload["worker_first"]["status"], "RUNNING")
        self.assertEqual(payload["worker_first"]["slot"], payload["worker_first"]["operation"])
        self.assertEqual(payload["worker_first"]["peers"], ["LocalProviderActive", "LocalProviderActive"])
        self.assertEqual(payload["prepare"]["result"], "ModelBusy")
        self.assertEqual(payload["prepare"]["status"], "RUNNING")
        self.assertEqual(payload["prepare"]["operations"], 0)
        self.assertEqual(payload["retry"]["result"], "ModelBusy")
        self.assertEqual(payload["retry"]["status"], "RUNNING")
        self.assertEqual(payload["operator"]["active"], "LocalProviderActive")
        self.assertEqual(payload["operator"]["unproven"], "ProviderFenceUnproven")
        self.assertEqual(payload["operator"]["status"], "RUNNING")
        self.assertFalse(payload["operator"]["recreated"])
        self.assertEqual(payload["roots"]["result"], "ProviderFenceUnproven")
        self.assertEqual(payload["roots"]["status"], "RUNNING")
        self.assertEqual(payload["acknowledged"]["warning"], True)
        self.assertEqual(payload["acknowledged"]["status"], "QUEUED")
        self.assertEqual(stat.S_IMODE(payload["fence_mode"]), 0o600)

    def test_replacement_inode_is_not_a_new_lock_domain(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        original = _fence(directory)
        other_directory = _directory()
        self.addCleanup(shutil.rmtree, other_directory, True)
        replacement = _fence(other_directory)
        _publish(original)
        holder = _hold(original)
        self.addCleanup(_release_holder, holder)
        os.replace(replacement.path, original.path)
        refused = acquire_exclusive(original)
        self.assertIsInstance(refused, FenceUnproven)
        self.assertEqual(refused.code, "fence_identity")
        spawned = []

        def refuse_spawn(*_args, **_kwargs):
            spawned.append(1)
            raise AssertionError("provider spawned")

        result = _supervise(original, popen=refuse_spawn)
        self.assertEqual(result.classification, "definite_non_send")
        self.assertEqual(result.calls, 0)
        self.assertFalse(result.worker_started)
        self.assertEqual(spawned, [])
        holder.stdin.write("\n")
        holder.stdin.flush()
        still = holder.stdout.readline().strip()
        self.assertEqual(still, f"still {original.inode}")
        self.assertIsNone(holder.poll())

    def test_replacement_after_locked_withholds_go(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        original = _fence(directory)
        other_directory = _directory()
        self.addCleanup(shutil.rmtree, other_directory, True)
        replacement = _fence(other_directory)
        _publish(original)
        ready = directory / "ready"

        def replace_before_go():
            os.replace(replacement.path, original.path)
            return True

        result = _supervise(
            original,
            ready_path=str(ready),
            prove_claim=replace_before_go,
        )
        self.assertEqual(result.classification, "definite_non_send")
        self.assertEqual(result.calls, 0)
        self.assertFalse(ready.exists())
        self.assertNotEqual(Path(original.path).stat().st_ino, original.inode)

    def test_symlink_ancestors_and_final_component_are_rejected(self):
        root = _directory()
        self.addCleanup(shutil.rmtree, root, True)
        grand = _private(root / "grand")
        parent = _private(grand / "parent")
        config = _fence(parent)
        held = acquire_exclusive(config)
        self.assertIsInstance(held, FenceHeld)
        release_fence(held)

        alias_grand = root / "alias-grand"
        alias_grand.symlink_to(grand, target_is_directory=True)
        through_grand = ProviderFenceConfig(
            str(alias_grand / "parent" / "provider.fence"),
            config.device,
            config.inode,
        )
        self.assertIsInstance(acquire_exclusive(through_grand), FenceUnproven)
        with self.assertRaises(FenceSafetyError) as grand_error:
            prepare_disposable_fence(alias_grand / "parent" / "other.fence")
        self.assertEqual(grand_error.exception.code, "fence_symlink")

        real_parent = _private(root / "real-parent")
        immediate = _fence(real_parent, "provider.fence")
        alias_parent = root / "alias-parent"
        alias_parent.symlink_to(real_parent, target_is_directory=True)
        through_parent = ProviderFenceConfig(
            str(alias_parent / "provider.fence"),
            immediate.device,
            immediate.inode,
        )
        self.assertIsInstance(acquire_exclusive(through_parent), FenceUnproven)
        with self.assertRaises(FenceSafetyError) as parent_error:
            prepare_disposable_fence(alias_parent / "other.fence")
        self.assertEqual(parent_error.exception.code, "fence_symlink")

        final_link = root / "final.fence"
        final_link.symlink_to(config.path)
        through_final = ProviderFenceConfig(
            str(final_link),
            config.device,
            config.inode,
        )
        self.assertIsInstance(acquire_exclusive(through_final), FenceUnproven)
        self.assertFalse((root / "other.fence").exists())
        valid = acquire_exclusive(config)
        self.assertIsInstance(valid, FenceHeld)
        release_fence(valid)

    def test_retargeted_ancestor_during_hold_is_rejected(self):
        root = _directory()
        self.addCleanup(shutil.rmtree, root, True)
        grand = _private(root / "grand")
        parent = _private(grand / "parent")
        config = _fence(parent)
        other = _private(root / "other")
        other_parent = _private(other / "parent")
        decoy = _fence(other_parent)
        holder = _hold(config)
        self.addCleanup(_release_holder, holder)
        os.rename(grand, root / "grand-kept")
        grand.symlink_to(other, target_is_directory=True)
        refused = acquire_exclusive(config)
        self.assertIsInstance(refused, FenceUnproven)
        kept = root / "grand-kept" / "parent" / "provider.fence"
        self.assertTrue(_locked(kept))
        self.assertNotEqual(Path(decoy.path).stat().st_ino, config.inode)
        holder.stdin.write("\n")
        holder.stdin.flush()
        self.assertEqual(holder.stdout.readline().strip(), f"still {config.inode}")

    def test_second_fence_root_cannot_dispatch(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        canonical = _fence(directory)
        other_directory = _directory()
        self.addCleanup(shutil.rmtree, other_directory, True)
        decoy = _fence(other_directory)
        publish_shared_fence_config(canonical)
        self.addCleanup(_drop_published_fence)
        holder = _hold(canonical)
        self.addCleanup(_release_holder, holder)
        spawned = []

        def refuse_spawn(*_args, **_kwargs):
            spawned.append(1)
            raise AssertionError("provider spawned")

        result = _supervise(decoy, popen=refuse_spawn)
        self.assertEqual(result.classification, "definite_non_send")
        self.assertEqual(result.calls, 0)
        self.assertFalse(result.worker_started)
        self.assertEqual(spawned, [])
        self.assertIsInstance(acquire_exclusive(canonical), FenceBusy)

    def test_missing_trusted_identity_refuses_dispatch(self):
        result = supervise(
            claimed_at=CLAIMED_AT,
            operation_id=OPERATION,
            runner_id=RUNNER,
            request_content="hello",
            standing_context="standing",
            wall_clock=lambda: CLAIMED_AT,
            fake="ok",
            prove_claim=lambda: True,
        )
        self.assertEqual(result.classification, "definite_non_send")
        self.assertEqual(result.calls, 0)
        self.assertFalse(result.worker_started)
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        fence = _fence(directory)
        stale = ProviderFenceConfig(fence.path, fence.device, fence.inode + 1)
        self.assertIsInstance(acquire_exclusive(stale), FenceUnproven)

    def test_production_helper_cannot_authorize_without_durable_proof(self):
        source = (SRC_ROOT / "provider_supervisor.py").read_text(encoding="utf-8")
        worker = (SRC_ROOT / "provider_worker.py").read_text(encoding="utf-8")
        self.assertNotIn("authorize_locked_worker", source)
        self.assertNotIn("authorize_locked_worker", worker)
        self.assertNotIn("def authorize_locked_worker", source)
        self.assertNotIn("def _write_control", source)
        self.assertEqual(source.count('{"type":"go"}'), 1)
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        fence = _fence(directory)
        _publish(fence)
        ready = directory / "ready"
        result = supervise(
            claimed_at=CLAIMED_AT,
            operation_id=OPERATION,
            runner_id=RUNNER,
            request_content="hello",
            standing_context="standing",
            wall_clock=lambda: CLAIMED_AT,
            fake=None,
            api_key="sk-test",
            ready_path=str(ready),
            fence_config=fence,
            actor_id=1,
            prove_claim=lambda: True,
        )
        self.assertEqual(result.classification, "definite_non_send")
        self.assertEqual(result.calls, 0)
        self.assertFalse(ready.exists())

    def test_explicit_config_requires_established_canonical_trust(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        canonical = _fence(directory)
        other = _directory()
        self.addCleanup(shutil.rmtree, other, True)
        explicit = _fence(other)
        spawned = []

        def refuse_spawn(*_args, **_kwargs):
            spawned.append(1)
            raise AssertionError("provider spawned")

        missing = _supervise(explicit, popen=refuse_spawn)
        self.assertEqual(missing.classification, "definite_non_send")
        self.assertEqual(missing.calls, 0)
        self.assertFalse(missing.worker_started)
        self.assertEqual(spawned, [])
        self.assertFalse(PRODUCTION_FENCE.exists())
        self.assertFalse(PRODUCTION_DATABASE.exists())

        os.environ["MAX_SMART_PROVIDER_FENCE"] = canonical.path
        with self.assertRaises(FenceSafetyError) as invalid:
            publish_shared_fence_config(explicit)
        self.assertEqual(invalid.exception.code, "fence_identity_invalid")
        self.assertIsNone(os.environ.get("MAX_SMART_PROVIDER_FENCE_DEVICE"))
        inconsistent = _supervise(explicit, popen=refuse_spawn)
        self.assertEqual(inconsistent.classification, "definite_non_send")
        self.assertEqual(inconsistent.calls, 0)
        self.assertEqual(spawned, [])
        _drop_published_fence()

        _publish(canonical)
        mismatch = _supervise(explicit, popen=refuse_spawn)
        self.assertEqual(mismatch.classification, "definite_non_send")
        self.assertEqual(mismatch.calls, 0)
        self.assertEqual(spawned, [])
        self.assertIsNone(canonical_fence_config(explicit))
        accepted = _supervise(canonical)
        self.assertEqual(accepted.classification, "success")
        self.assertEqual(accepted.calls, 1)
        self.assertFalse(PRODUCTION_FENCE.exists())

    def test_published_identity_cannot_change_before_go(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        canonical = _fence(directory)
        other = _directory()
        self.addCleanup(shutil.rmtree, other, True)
        replacement = _fence(other)
        _publish(canonical)
        ready = directory / "ready"
        process = _spawn_fake_worker(_job(
            canonical,
            time.monotonic_ns() + 5_000_000_000,
            fake="block_read",
            ready_path=str(ready),
        ))
        self.addCleanup(_close_worker, process)
        deadline = time.monotonic() + 2
        busy = None
        while time.monotonic() < deadline:
            busy = acquire_exclusive(canonical)
            if isinstance(busy, FenceBusy):
                break
            release_fence(busy)
            time.sleep(0.01)
        self.assertIsInstance(busy, FenceBusy)
        with self.assertRaises(FenceSafetyError) as changed:
            publish_shared_fence_config(replacement)
        self.assertEqual(changed.exception.code, "fence_identity_changed")
        self.assertEqual(shared_fence_config(), canonical)
        self.assertIsNone(canonical_fence_config(replacement))
        self.assertEqual(canonical_fence_config(canonical), canonical)
        process._provider_control.close()
        process._provider_control = None
        self.assertEqual(process.wait(timeout=2), 0)
        self.assertFalse(ready.exists())
        frame = json.loads(process.stdout.readline().decode("utf-8"))
        self.assertEqual(frame["kind"], "not_sent")
        self.assertEqual(frame["calls"], 0)
        held = acquire_exclusive(canonical)
        self.assertIsInstance(held, FenceHeld)
        release_fence(held)
        payload = _payload(self, _run(_TRUST_BODY, timeout=90))
        self.assertEqual(payload["publish"], "fence_identity_changed")
        self.assertEqual(payload["active"], "LocalProviderActive")
        self.assertEqual(payload["switched"], "ProviderFenceUnproven")
        self.assertTrue(payload["same"])
        self.assertEqual(payload["repair"], "fence_identity_invalid")
        self.assertEqual(payload["repaired_inode"], "nope")
        self.assertEqual(payload["still"], payload["inode"])

    def test_same_process_verification_keeps_the_recovery_lock(self):
        payload = _payload(self, _run(_LOCK_RACE_BODY, timeout=90))
        for name in ("normal", "raised", "ambiguous"):
            phase = payload[name]
            self.assertTrue(phase["matched"])
            self.assertEqual(phase["blocked"], "FenceBusy")
            self.assertEqual(phase["afterwards"], "FenceHeld")
        self.assertEqual(payload["raised"]["error"], "RuntimeError")
        self.assertEqual(payload["ambiguous"]["result"], "CommitAmbiguous")

    def test_public_api_does_not_expose_a_go_channel(self):
        import provider_supervisor

        source = (SRC_ROOT / "provider_supervisor.py").read_text(encoding="utf-8")
        self.assertFalse(hasattr(provider_supervisor, "spawn_worker"))
        self.assertNotIn("def spawn_worker", source)
        self.assertNotIn("_provider_control", source)
        self.assertEqual(source.count('{"type":"go"}'), 1)
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        fence = _fence(directory)
        _publish(fence)
        captured = []

        def popen(args, **kwargs):
            process = subprocess.Popen(args, **kwargs)
            captured.append(process)
            self.assertFalse(hasattr(process, "_provider_control"))
            return process

        result = _supervise(fence, popen=popen)
        self.assertEqual(result.classification, "success")
        self.assertEqual(result.calls, 1)
        self.assertEqual(len(captured), 1)
        self.assertFalse(hasattr(captured[0], "_provider_control"))
        self.assertFalse(hasattr(result, "_provider_control"))

    def test_caller_fake_label_cannot_authorize_a_real_worker(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        fence = _fence(directory)
        _publish(fence)
        ready = directory / "ready"
        with self.assertRaises(RuntimeError):
            _spawn_fake_worker(_job(
                fence,
                time.monotonic_ns() + 5_000_000_000,
                mode="openai",
                fake=None,
                api_key="sk-test",
                ready_path=str(ready),
            ))
        self.assertFalse(ready.exists())
        parent, child = socket.socketpair()
        self.addCleanup(parent.close)
        self.addCleanup(child.close)
        child.sendall(b'{"type":"locked","mode":"openai"}\n')

        class _OpenAIFrame:
            def __init__(self, control):
                self._provider_control = control

            def poll(self):
                return None

        self.assertFalse(_release_fake_locked_worker(
            _OpenAIFrame(parent),
            time.monotonic_ns() + 2_000_000_000,
            mode="fake",
        ))
        child.settimeout(0.2)
        with self.assertRaises(TimeoutError):
            child.recv(64)

    def test_replaced_fence_blocks_running_recovery(self):
        payload = _payload(self, _run(_IDENTITY_BODY, timeout=90))
        self.assertEqual(payload["second_root"], "definite_non_send")
        self.assertEqual(payload["second_calls"], 0)
        self.assertEqual(payload["service"], "ProviderFailure")
        self.assertEqual(payload["spawned"], 0)
        self.assertEqual(payload["propagated"], payload["identity"])
        self.assertEqual(payload["recovery"], "ProviderFenceUnproven")
        self.assertEqual(payload["status"], "RUNNING")
        self.assertEqual(payload["slot"], payload["operation"])
        self.assertEqual(payload["dispatch"], "definite_non_send")
        self.assertEqual(payload["dispatch_calls"], 0)
        self.assertEqual(payload["still"], payload["identity"][1])
        self.assertTrue(payload["holder_alive"])
        self.assertNotEqual(payload["adoption"], 0)
        self.assertEqual(payload["composition"], "DisposableComposition")

    def test_no_fence_mutation_helpers_delete_or_rename(self):
        source = (SRC_ROOT / "provider_fence.py").read_text(encoding="utf-8")
        for banned in ("os.unlink", "os.remove", "os.rename", "os.replace"):
            self.assertNotIn(banned, source)


_HOLDER = """\
import os
import sys
from provider_fence import ProviderFenceConfig, acquire_exclusive
config = ProviderFenceConfig(
    os.environ["FENCE_PATH"],
    int(os.environ["FENCE_DEVICE"]),
    int(os.environ["FENCE_INODE"]),
)
held = acquire_exclusive(config)
if type(held).__name__ != "FenceHeld":
    sys.stdout.write("fail\\n")
    sys.stdout.flush()
    sys.exit(1)
sys.stdout.write("held %s\\n" % held.inode)
sys.stdout.flush()
sys.stdin.readline()
sys.stdout.write("still %s\\n" % os.fstat(held.descriptor).st_ino)
sys.stdout.flush()
sys.stdin.readline()
"""


def _provider_workers():
    listed = subprocess.run(
        ("ps", "-ax", "-o", "pid=,command="),
        capture_output=True,
        text=True,
        check=False,
    )
    return [
        line.strip()
        for line in listed.stdout.splitlines()
        if " -m provider_worker" in line or line.strip().endswith("provider_worker")
    ]


def _run(body, *, timeout=60):
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "max_smart_db.test_settings",
            "MAX_SMART_TEST_DATABASE_PATH": str(
                Path(temporary) / "provider-fence.sqlite3"
            ),
            "PYTHONPATH": str(SRC_ROOT),
            "NOW": "1",
        }
        for name in (
            "MAX_SMART_PROVIDER_FENCE",
            "MAX_SMART_PROVIDER_FENCE_DEVICE",
            "MAX_SMART_PROVIDER_FENCE_INODE",
        ):
            environment.pop(name, None)
        return subprocess.run(
            (sys.executable, "-B", "-c", _PREAMBLE + "\n" + textwrap.dedent(body)),
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )


def _payload(testcase, result):
    testcase.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
    return json.loads(result.stdout)


_PREAMBLE = """\
import json
import os
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

database = Path(os.environ["MAX_SMART_TEST_DATABASE_PATH"])
if database.name == "max_smart.sqlite3":
    raise SystemExit("production database requested")
from max_smart_db.safety import initialize_sqlite_file
initialize_sqlite_file(database)

import django
django.setup()
from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from conversation_service import ConversationService, TurnRequest
from max_smart_db.creation import SqliteConversationRepository
from max_smart_db.models import Conversation, GlobalModelSlot, Message, Operation
from max_smart_db.repository import (
    Claimed,
    Failed,
    PrepareCommand,
    PreProviderFailureCategory,
    RetryCommand,
)
from operator_commands import OperatorCommands
from provider_fence import prepare_disposable_fence
from provider_supervisor import _durable_claim_matches, supervise

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)
OWNER = User.objects.create(username="owner")
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
PREPARED_AT = 1_000_000
LEASE_JUMP = PREPARED_AT + 720_000_000
FENCE_CONFIG = settings.MAX_SMART_PROVIDER_FENCE_CONFIG
FENCE = Path(FENCE_CONFIG.path)


class Clock:
    def __init__(self, value):
        self.value = value

    def now_us(self):
        return self.value


def repository(now=PREPARED_AT, fence_path=None):
    return SqliteConversationRepository(Clock(now), RUNNER, fence_path)


def key_for(value):
    import base64
    return base64.urlsafe_b64encode(bytes([value]) * 16).rstrip(b"=").decode()


def conversation():
    identity = uuid.uuid4()
    Conversation.objects.create(
        id=identity,
        owner_id=OWNER.id,
        role_id="max-smart",
        version=0,
        next_sequence=1,
        title=None,
        created_at_us=0,
        updated_at_us=0,
    )
    return identity


def reserve(repo, key, conversation_id):
    prepared = repo.prepare(PrepareCommand(
        actor_id=OWNER.id,
        conversation_id=conversation_id,
        expected_version=0,
        request_content="persisted original",
        idempotency_key=key_for(key),
        provider_id="openai",
        model_id="gpt-5.6-sol",
    ))
    if type(prepared).__name__ != "Prepared":
        raise RuntimeError(type(prepared).__name__)
    return prepared.reservation


def claim(repo, reservation):
    outcome = repo.claim_running(OWNER.id, reservation.operation_id)
    if type(outcome) is not Claimed:
        raise RuntimeError(type(outcome).__name__)
    return outcome.claimed_operation


def prove_for(claimed):
    def prove():
        return _durable_claim_matches(
            actor_id=OWNER.id,
            operation_id=claimed.operation_id,
            runner_id=claimed.runner_id,
            claimed_at=claimed.claimed_at,
        )
    return prove


def slot_id():
    slot = GlobalModelSlot.objects.get(pk=1)
    return None if slot.operation_id is None else str(slot.operation_id)


def status_of(operation_id):
    return Operation.objects.get(pk=operation_id).status


def messages_of(operation_id):
    return [
        {"role": row.role, "content": row.content}
        for row in Message.objects.filter(operation_id=operation_id).order_by("sequence")
    ]
"""


_RACE_BODY = """\
report = {}
repo = repository()
reserved = reserve(repo, 1, conversation())
claimed = claim(repo, reserved)
service = ConversationService(
    repo,
    lambda **_kwargs: "standing-context",
    lambda **_kwargs: "answer",
)
completed = supervise(
    claimed_at=claimed.claimed_at,
    operation_id=str(claimed.operation_id),
    runner_id=claimed.runner_id,
    request_content="persisted original",
    standing_context="standing-context",
    wall_clock=lambda: claimed.claimed_at,
    fake="ok",
    fence_config=FENCE_CONFIG,
    prove_claim=prove_for(claimed),
)
from max_smart_db.repository import Applied
applied = repo.apply_success(claimed, completed.output)
if type(applied) is not Applied:
    raise RuntimeError(type(applied).__name__)
report["go"] = {
    "classification": completed.classification,
    "calls": completed.calls,
    "messages": messages_of(claimed.operation_id),
}

stale_repo = repository()
stale_reserved = reserve(stale_repo, 2, conversation())
stale_claimed = claim(stale_repo, stale_reserved)
released = repository(LEASE_JUMP).recover_slot()
if type(released).__name__ != "MarkedOutcomeUnknown":
    raise RuntimeError(type(released).__name__)
stale = supervise(
    claimed_at=stale_claimed.claimed_at,
    operation_id=str(stale_claimed.operation_id),
    runner_id=stale_claimed.runner_id,
    request_content="persisted original",
    standing_context="standing-context",
    wall_clock=lambda: stale_claimed.claimed_at,
    fake="ok",
    fence_config=FENCE_CONFIG,
    prove_claim=prove_for(stale_claimed),
)
report["recovery_first"] = {
    "classification": stale.classification,
    "calls": stale.calls,
    "status": status_of(stale_claimed.operation_id),
    "slot": slot_id(),
}

failed_repo = repository()
failed_reservation = reserve(failed_repo, 5, conversation())
failed = failed_repo.fail_pre_provider(
    failed_reservation,
    PreProviderFailureCategory.CONTEXT_UNAVAILABLE,
)
if type(failed) is not Failed:
    raise RuntimeError(type(failed).__name__)

import socket as _socket
server = _socket.socket()
server.bind(("127.0.0.1", 0))
server.listen(1)
accepted = []

def accept_provider():
    connection, _address = server.accept()
    accepted.append(connection)

threading.Thread(target=accept_provider, daemon=True).start()
live_repo = repository()
live_reserved = reserve(live_repo, 3, conversation())
live_claimed = claim(live_repo, live_reserved)
ready = database.parent / "worker-ready"
box = {}

def run_worker():
    box["result"] = supervise(
        claimed_at=live_claimed.claimed_at,
        operation_id=str(live_claimed.operation_id),
        runner_id=live_claimed.runner_id,
        request_content="persisted original",
        standing_context="standing-context",
        wall_clock=lambda: live_claimed.claimed_at + 480_000_000 - 10_000_000,
        fake="block_read",
        socket_port=server.getsockname()[1],
        ready_path=str(ready),
        fence_config=FENCE_CONFIG,
        prove_claim=prove_for(live_claimed),
    )

worker = threading.Thread(target=run_worker)
worker.start()
deadline = time.monotonic() + 3
while time.monotonic() < deadline and not ready.exists():
    time.sleep(0.01)
if not ready.exists():
    raise RuntimeError(box.get("result"))
active = repository(LEASE_JUMP).recover_slot()
peer_code = '''
import os
import django
django.setup()
from max_smart_db.creation import SqliteConversationRepository
class Clock:
    def now_us(self):
        return int(os.environ["NOW"])
print(type(SqliteConversationRepository(
    Clock(),
    os.environ["RUNNER"],
).recover_slot()).__name__)
'''
children = [
    subprocess.Popen(
        (sys.executable, "-B", "-c", peer_code),
        env={**os.environ, "NOW": str(LEASE_JUMP), "RUNNER": RUNNER},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    for _ in range(2)
]
peers = []
for child in children:
    stdout, stderr = child.communicate(timeout=8)
    if child.returncode != 0:
        raise RuntimeError(stderr)
    peers.append(stdout.strip())
other = conversation()
prepared = repository(LEASE_JUMP).prepare(PrepareCommand(
    actor_id=OWNER.id,
    conversation_id=other,
    expected_version=0,
    request_content="persisted original",
    idempotency_key=key_for(4),
    provider_id="openai",
    model_id="gpt-5.6-sol",
))
retry = repository(LEASE_JUMP).create_retry(RetryCommand(
    actor_id=OWNER.id,
    source_operation_id=failed_reservation.operation_id,
    idempotency_key=key_for(6),
    expected_version=0,
))
operator = OperatorCommands(ConversationService(
    repository(LEASE_JUMP),
    lambda **_kwargs: "standing-context",
    lambda **_kwargs: (_ for _ in ()).throw(AssertionError("provider")),
))
active_recovery = operator.recover()
inode = FENCE.stat().st_ino
os.chmod(FENCE, 0o644)
unproven = operator.recover()
os.chmod(FENCE, 0o600)
decoy_dir = database.parent / "other-root"
decoy_dir.mkdir(mode=0o700)
os.chmod(decoy_dir, 0o700)
decoy = prepare_disposable_fence(decoy_dir.resolve() / "provider.fence")
bypassed = repository(LEASE_JUMP, decoy).recover_slot()
report["worker_first"] = {
    "recovery": type(active).__name__,
    "status": status_of(live_claimed.operation_id),
    "slot": slot_id(),
    "operation": str(live_claimed.operation_id),
    "peers": peers,
}
report["prepare"] = {
    "result": type(prepared).__name__,
    "status": status_of(live_claimed.operation_id),
    "operations": Operation.objects.filter(conversation_id=other).count(),
}
report["retry"] = {
    "result": type(retry).__name__,
    "status": status_of(live_claimed.operation_id),
}
report["operator"] = {
    "active": type(active_recovery).__name__,
    "unproven": type(unproven).__name__,
    "status": status_of(live_claimed.operation_id),
    "recreated": FENCE.stat().st_ino != inode or not FENCE.exists(),
}
report["roots"] = {
    "result": type(bypassed).__name__,
    "status": status_of(live_claimed.operation_id),
}
worker.join(timeout=12)
if worker.is_alive():
    raise RuntimeError("supervised worker did not finish")
cleared = repository(LEASE_JUMP).recover_slot()
if type(cleared).__name__ != "MarkedOutcomeUnknown":
    raise RuntimeError(type(cleared).__name__)

quiet = repository()
quiet_reserved = reserve(quiet, 7, conversation())
quiet_claimed = claim(quiet, quiet_reserved)
unknown = repository(LEASE_JUMP).recover_slot()
if type(unknown).__name__ != "MarkedOutcomeUnknown":
    raise RuntimeError(type(unknown).__name__)
from max_smart_db.repository import Acknowledged
acknowledged = quiet.acknowledge_unknown(OWNER.id, quiet_claimed.operation_id)
if type(acknowledged) is not Acknowledged:
    raise RuntimeError(type(acknowledged).__name__)
created = quiet.create_retry(RetryCommand(
    actor_id=OWNER.id,
    source_operation_id=quiet_claimed.operation_id,
    idempotency_key=key_for(8),
    expected_version=0,
))
report["acknowledged"] = {
    "warning": created.duplicate_charge_warning,
    "status": Operation.objects.get(pk=created.reservation.operation_id).status,
}
report["fence_mode"] = stat.S_IMODE(FENCE.stat().st_mode)
print(json.dumps(report))
"""


_IDENTITY_BODY = """\
import os
import subprocess
import sys
import uuid
from durable_composition import open_disposable_service
from max_smart_db.local_identity import (
    LocalIdentityProvisioned,
    provision_local_identity,
)
from provider_fence import prepare_disposable_fence, shared_fence_config
from provider_supervisor import build_service_provider, supervise

config = shared_fence_config()
if config is None or config.path != str(FENCE):
    raise RuntimeError("canonical fence was not published")
if (config.device, config.inode) != (FENCE.stat().st_dev, FENCE.stat().st_ino):
    raise RuntimeError("canonical inode does not match the provisioned file")
identity = provision_local_identity()
if type(identity) is not LocalIdentityProvisioned:
    raise RuntimeError(type(identity).__name__)
opened = open_disposable_service(
    owner_id=identity.owner_id,
    clock=Clock(PREPARED_AT),
    runner_id=RUNNER,
    fake="ok",
    load_context=lambda **_kwargs: "standing",
)
if type(opened).__name__ != "DisposableComposition":
    raise RuntimeError(type(opened).__name__)
if (
    opened.repository.provider_fence_path is not None
    or opened.repository.provider_fence_config is not None
):
    raise RuntimeError("composition selected a second fence")

repo = repository()
reserved = reserve(repo, 1, conversation())
claimed = claim(repo, reserved)
holder_script = '''
import sys
from provider_fence import acquire_exclusive, env_fence_config
config = env_fence_config()
held = acquire_exclusive(config)
if type(held).__name__ != "FenceHeld" or held.inode != config.inode:
    print("fail", flush=True)
    sys.exit(1)
print("held %s %s" % (held.device, held.inode), flush=True)
sys.stdin.readline()
import os
print(os.fstat(held.descriptor).st_ino, flush=True)
'''
holder = subprocess.Popen(
    (sys.executable, "-B", "-c", holder_script),
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
held_line = holder.stdout.readline().strip()
if not held_line.startswith("held"):
    raise RuntimeError(held_line + "\\n" + holder.stderr.read())
identity_script = '''
import django
django.setup()
from django.conf import settings
config = settings.MAX_SMART_PROVIDER_FENCE_CONFIG
print(config.device, config.inode)
'''
propagated = subprocess.run(
    (sys.executable, "-B", "-c", identity_script),
    capture_output=True,
    text=True,
)
if propagated.returncode != 0:
    raise RuntimeError(propagated.stderr)
other_dir = database.parent / "other-root"
other_dir.mkdir()
os.chmod(other_dir, 0o700)
decoy = prepare_disposable_fence(other_dir.resolve() / "provider.fence")
spawned = []

def refuse_spawn(*_args, **_kwargs):
    spawned.append(1)
    raise AssertionError("provider spawned")

second = supervise(
    claimed_at=claimed.claimed_at,
    operation_id=str(claimed.operation_id),
    runner_id=claimed.runner_id,
    request_content="persisted original",
    standing_context="standing",
    wall_clock=lambda: claimed.claimed_at,
    fake="ok",
    fence_config=decoy,
    prove_claim=lambda: True,
    popen=refuse_spawn,
)
service_result = build_service_provider(
    wall_clock=lambda: claimed.claimed_at,
    fake="ok",
    fence_config=decoy,
    popen=refuse_spawn,
)(
    actor_id=identity.owner_id,
    conversation_id=uuid.uuid4(),
    operation_id=claimed.operation_id,
    runner_id=claimed.runner_id,
    claimed_at=claimed.claimed_at,
    request_content="persisted original",
    standing_context="standing",
)
os.replace(decoy.path, config.path)
recovered = repository(LEASE_JUMP).recover_slot()
dispatched = supervise(
    claimed_at=claimed.claimed_at,
    operation_id=str(claimed.operation_id),
    runner_id=claimed.runner_id,
    request_content="persisted original",
    standing_context="standing",
    wall_clock=lambda: claimed.claimed_at,
    fake="ok",
    prove_claim=lambda: True,
    popen=refuse_spawn,
)
adoption = subprocess.run(
    (sys.executable, "-B", "-c", identity_script),
    capture_output=True,
    text=True,
)
holder_alive = holder.poll() is None
holder.stdin.write("\\n")
holder.stdin.flush()
still_line = holder.stdout.readline().strip()
holder.stdin.close()
holder.wait(timeout=2)
device_text, inode_text = propagated.stdout.split()
print(json.dumps({
    "second_root": second.classification,
    "second_calls": second.calls,
    "service": type(service_result).__name__,
    "spawned": len(spawned),
    "propagated": [int(device_text), int(inode_text)],
    "identity": [config.device, config.inode],
    "recovery": type(recovered).__name__,
    "status": status_of(claimed.operation_id),
    "slot": slot_id(),
    "operation": str(claimed.operation_id),
    "dispatch": dispatched.classification,
    "dispatch_calls": dispatched.calls,
    "still": int(still_line),
    "holder_alive": holder_alive,
    "adoption": adoption.returncode,
    "composition": type(opened).__name__,
}))
"""


_TRUST_BODY = r"""
import tempfile
from provider_fence import (
    canonical_fence_config,
    prepare_disposable_fence,
    publish_shared_fence_config,
)

other = Path(tempfile.mkdtemp(prefix="decoy-fence-")).resolve()
os.chmod(other, 0o700)
decoy = prepare_disposable_fence(other / "provider.fence")
holder_script = chr(10).join([
    "import os, sys",
    "from provider_fence import ProviderFenceConfig, acquire_exclusive",
    "config = ProviderFenceConfig(",
    "    os.environ['FENCE_PATH'],",
    "    int(os.environ['FENCE_DEVICE']),",
    "    int(os.environ['FENCE_INODE']),",
    ")",
    "held = acquire_exclusive(config)",
    "if type(held).__name__ != 'FenceHeld':",
    "    sys.stdout.write('fail' + chr(10))",
    "    sys.stdout.flush()",
    "    sys.exit(1)",
    "sys.stdout.write('held %s' % held.inode + chr(10))",
    "sys.stdout.flush()",
    "sys.stdin.readline()",
    "sys.stdout.write('still %s' % os.fstat(held.descriptor).st_ino + chr(10))",
    "sys.stdout.flush()",
])
holder = subprocess.Popen(
    [sys.executable, "-B", "-c", holder_script],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    env={
        **os.environ,
        "FENCE_PATH": FENCE_CONFIG.path,
        "FENCE_DEVICE": str(FENCE_CONFIG.device),
        "FENCE_INODE": str(FENCE_CONFIG.inode),
    },
)
held_line = holder.stdout.readline().strip()
if not held_line.startswith("held"):
    holder.kill()
    raise RuntimeError(holder.stderr.read())
try:
    publish_shared_fence_config(decoy)
    publish_code = "published"
except Exception as error:
    publish_code = getattr(error, "code", type(error).__name__)
active = repository(LEASE_JUMP).recover_slot()
switched = repository(LEASE_JUMP, decoy).recover_slot()
same = canonical_fence_config(FENCE_CONFIG)
os.environ["MAX_SMART_PROVIDER_FENCE_INODE"] = "nope"
try:
    publish_shared_fence_config(FENCE_CONFIG)
    repair = "repaired"
except Exception as error:
    repair = getattr(error, "code", type(error).__name__)
holder.stdin.write("\n")
holder.stdin.flush()
still_line = holder.stdout.readline().strip()
holder.stdin.close()
holder.wait(timeout=2)
print(json.dumps({
    "publish": publish_code,
    "active": type(active).__name__,
    "switched": type(switched).__name__,
    "same": same is not None and same.inode == FENCE_CONFIG.inode,
    "repair": repair,
    "repaired_inode": os.environ.get("MAX_SMART_PROVIDER_FENCE_INODE"),
    "inode": FENCE_CONFIG.inode,
    "still": int(still_line.split()[-1]),
}))
"""


_LOCK_RACE_BODY = r"""
from django.db import OperationalError, connection
from provider_fence import env_fence_config, fence_matches_config

config = env_fence_config()
if type(config).__name__ != "ProviderFenceConfig":
    raise RuntimeError("canonical fence missing")
probe_source = (
    "import sys\n"
    "from provider_fence import acquire_exclusive, env_fence_config, release_fence\n"
    "held = acquire_exclusive(env_fence_config())\n"
    "name = type(held).__name__\n"
    "sys.stdout.write(name)\n"
    "if name == 'FenceHeld':\n"
    "    release_fence(held)\n"
)

def probe():
    result = subprocess.run(
        [sys.executable, "-B", "-c", probe_source],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()

class Gate:
    def __init__(self, fail):
        self.fail = fail
        self.entered = threading.Event()
        self.release = threading.Event()

    def now_us(self):
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("pause was not released")
        if self.fail == "raise":
            raise RuntimeError("boom")
        if self.fail == "commit":
            def fail_commit(*_args, **_kwargs):
                raise OperationalError("injected commit failure")
            connection.commit = fail_commit
        return LEASE_JUMP

def recover(fail):
    gate = Gate(fail)
    box = {}

    def run():
        try:
            box["result"] = SqliteConversationRepository(gate, RUNNER).recover_slot()
        except Exception as error:
            box["error"] = type(error).__name__

    thread = threading.Thread(target=run)
    thread.start()
    if not gate.entered.wait(5):
        raise RuntimeError("recovery did not reach its transaction")
    matched = fence_matches_config(config)
    blocked = probe()
    gate.release.set()
    thread.join(5)
    if thread.is_alive():
        raise RuntimeError("recovery thread did not finish")
    return {
        "matched": matched,
        "blocked": blocked,
        "afterwards": probe(),
        "result": type(box["result"]).__name__ if "result" in box else None,
        "error": box.get("error"),
    }

print(json.dumps({
    "normal": recover(None),
    "raised": recover("raise"),
    "ambiguous": recover("commit"),
}))
"""


if __name__ == "__main__":
    unittest.main()
