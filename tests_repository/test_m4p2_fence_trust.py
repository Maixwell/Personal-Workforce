"""Disposable trust-record tests for independent fence identity."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
import warnings
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
PRODUCTION_DATABASE = PROJECT_ROOT / "data" / "max_smart.sqlite3"
PRODUCTION_FENCE = PROJECT_ROOT / "data" / "provider.fence"
SESSION_FILE = PROJECT_ROOT / "data" / "current_session.json"
sys.path.insert(0, str(SRC_ROOT))

from max_smart_db.config import (  # noqa: E402
    EXECUTION_DEADLINE_US,
    FIXED_LEASE_US,
    RECOVERY_CLOCK_SKEW_MARGIN_US,
)
from provider_fence import (  # noqa: E402
    FenceBusy,
    FenceSafetyError,
    FenceUnproven,
    ProviderFenceConfig,
    TrustRecordPin,
    acquire_exclusive,
    env_fence_config,
    load_disposable_fence_trust,
    fence_config_from_trust,
    prepare_disposable_fence,
    provision_disposable_fence_trust,
    publish_loaded_fence_trust,
    publish_shared_fence_config,
    release_fence,
)
from provider_supervisor import supervise  # noqa: E402


_FENCE_ENV = (
    "MAX_SMART_PROVIDER_FENCE",
    "MAX_SMART_PROVIDER_FENCE_DEVICE",
    "MAX_SMART_PROVIDER_FENCE_INODE",
)
CLAIMED_AT = 1_000_000
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
OPERATION = "11111111-1111-1111-1111-111111111111"
SECRET = "sk-test-trust-record-secret"


def _drop_fence_env():
    for name in _FENCE_ENV:
        os.environ.pop(name, None)


def _directory():
    directory = Path(tempfile.mkdtemp(prefix="m4p2-fence-trust-")).resolve()
    os.chmod(directory, 0o700)
    return directory


def _pin_args(pin):
    return (pin.path, str(pin.device), str(pin.inode), pin.digest)


def _child_env(**extra):
    environment = {**os.environ, "PYTHONPATH": str(SRC_ROOT)}
    for name in _FENCE_ENV:
        environment.pop(name, None)
    environment.update(extra)
    return environment


def _run_child(script, pin, *, timeout=30):
    return subprocess.run(
        (sys.executable, "-B", "-c", script, *_pin_args(pin)),
        cwd=PROJECT_ROOT,
        env=_child_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _record_bytes(pin):
    return Path(pin.path).read_bytes()


def _raw_pin(directory, payload):
    path = directory / "fence.trust"
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
        status = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    return TrustRecordPin(
        str(path),
        status.st_dev,
        status.st_ino,
        hashlib.sha256(payload).hexdigest(),
    )


class FenceTrustTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session_stat = SESSION_FILE.stat()

    def tearDown(self):
        _drop_fence_env()

    @classmethod
    def tearDownClass(cls):
        _drop_fence_env()
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

    def test_provision_writes_a_private_record_outside_the_fence(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        trust = provision_disposable_fence_trust(directory)
        record = Path(trust.pin.path)
        status = record.lstat()
        directory_status = directory.lstat()
        payload = record.read_bytes()
        self.assertEqual(trust.fence.path, str(directory / "provider.fence"))
        self.assertNotEqual(trust.pin.inode, trust.fence.inode)
        self.assertEqual(stat.S_IMODE(directory_status.st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(status.st_mode), 0o600)
        self.assertEqual(status.st_nlink, 1)
        self.assertEqual(status.st_uid, os.geteuid())
        self.assertFalse(stat.S_ISLNK(status.st_mode))
        self.assertEqual(hashlib.sha256(payload).hexdigest(), trust.pin.digest)
        self.assertNotIn(trust.pin.digest.encode("ascii"), payload)
        self.assertNotIn(SECRET.encode("ascii"), payload)
        self.assertNotIn(b"api_key", payload)
        self.assertIsNone(env_fence_config())
        loaded = load_disposable_fence_trust(trust.pin)
        self.assertEqual(loaded, trust.fence)
        self.assertNotEqual(loaded.inode, trust.pin.inode)

    def test_existing_record_is_not_reinitialized(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        _raw_pin(directory, b"already\n")
        with self.assertRaises(FenceSafetyError) as caught:
            provision_disposable_fence_trust(directory)
        self.assertEqual(caught.exception.code, "trust_record_exists")
        self.assertFalse((directory / "provider.fence").exists())
        self.assertEqual((directory / "fence.trust").read_bytes(), b"already\n")

    def test_production_and_cloud_paths_are_refused(self):
        with self.assertRaises(FenceSafetyError) as production:
            provision_disposable_fence_trust(PROJECT_ROOT / "data")
        self.assertEqual(production.exception.code, "production_fence_refused")
        self.assertFalse(PRODUCTION_FENCE.exists())
        with self.assertRaises(FenceSafetyError) as cloud:
            provision_disposable_fence_trust("/tmp/Dropbox/max-smart-trust")
        self.assertEqual(cloud.exception.code, "fence_path_refused")

    def test_independent_processes_agree_without_inherited_fence_env(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        trust = provision_disposable_fence_trust(directory)
        before = _record_bytes(trust.pin)
        first = _run_child(_AGREE_CHILD, trust.pin)
        second = _run_child(_AGREE_CHILD, trust.pin)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        left = json.loads(first.stdout)
        right = json.loads(second.stdout)
        self.assertEqual(left, right)
        self.assertEqual(left["path"], trust.fence.path)
        self.assertEqual(left["device"], trust.fence.device)
        self.assertEqual(left["inode"], trust.fence.inode)
        self.assertTrue(left["same"])
        self.assertEqual(left["env_inode"], str(trust.fence.inode))
        self.assertFalse(left["inherited"])
        self.assertEqual(_record_bytes(trust.pin), before)
        self.assertIsNone(env_fence_config())

    def test_replacement_fence_is_refused_and_the_record_stays(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        trust = provision_disposable_fence_trust(directory)
        other = _directory()
        self.addCleanup(shutil.rmtree, other, True)
        replacement = provision_disposable_fence_trust(other)
        before = _record_bytes(trust.pin)
        holder = self._hold(trust.pin)
        os.replace(
            replacement.fence.path,
            trust.fence.path,
        )
        refused = _run_child(_REFUSE_CHILD, trust.pin)
        self.assertEqual(refused.returncode, 0, refused.stderr)
        payload = json.loads(refused.stdout)
        self.assertEqual(payload["code"], "fence_identity")
        self.assertEqual(payload["dispatch"], "definite_non_send")
        self.assertEqual(payload["spawned"], 0)
        self.assertEqual(_record_bytes(trust.pin), before)
        self.assertNotEqual(
            Path(trust.fence.path).stat().st_ino,
            trust.fence.inode,
        )
        holder.stdin.write("\n")
        holder.stdin.flush()
        still = holder.stdout.readline().strip()
        self.assertEqual(still, f"still {trust.fence.inode}")
        self.assertIsNone(holder.poll())
        blocked = acquire_exclusive(trust.fence)
        self.assertIsInstance(blocked, FenceUnproven)
        self.assertEqual(blocked.code, "fence_identity")

    def test_record_replacement_is_refused_while_the_worker_holds_the_fence(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        trust = provision_disposable_fence_trust(directory)
        original = _record_bytes(trust.pin)
        holder = self._hold(trust.pin)
        record = Path(trust.pin.path)
        descriptor = os.open(record, os.O_WRONLY | os.O_NOFOLLOW)
        try:
            os.ftruncate(descriptor, 0)
            os.write(descriptor, b'{"rewritten":true}\n')
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        holder.stdin.write("check\n")
        holder.stdin.flush()
        outcome = holder.stdout.readline().strip()
        self.assertEqual(outcome, "refused trust_record_digest")
        holder.stdin.write("\n")
        holder.stdin.flush()
        self.assertEqual(
            holder.stdout.readline().strip(),
            f"still {trust.fence.inode}",
        )
        self.assertNotEqual(record.read_bytes(), original)
        self.assertEqual(record.stat().st_ino, trust.pin.inode)
        with self.assertRaises(FenceSafetyError) as caught:
            load_disposable_fence_trust(trust.pin)
        self.assertEqual(caught.exception.code, "trust_record_digest")

    def test_same_owner_needs_the_original_pin_after_replacing_both_files(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        original = provision_disposable_fence_trust(directory)
        other = _directory()
        self.addCleanup(shutil.rmtree, other, True)
        forged = provision_disposable_fence_trust(other)
        os.replace(forged.fence.path, original.fence.path)
        os.replace(forged.pin.path, original.pin.path)
        with self.assertRaises(FenceSafetyError) as old_pin:
            load_disposable_fence_trust(original.pin)
        self.assertEqual(old_pin.exception.code, "trust_record_identity")
        live = Path(original.fence.path).stat()
        payload = (json.dumps(
            {
                "device": live.st_dev,
                "inode": live.st_ino,
                "path": original.fence.path,
                "version": "max-smart-provider-fence-trust-v1",
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ) + "\n").encode("ascii")
        rewritten = other / "rewritten.trust"
        descriptor = os.open(
            rewritten,
            os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(rewritten, original.pin.path)
        moved = TrustRecordPin(
            original.pin.path,
            Path(original.pin.path).stat().st_dev,
            Path(original.pin.path).stat().st_ino,
            hashlib.sha256(payload).hexdigest(),
        )
        loaded = load_disposable_fence_trust(moved)
        self.assertEqual(loaded.path, original.fence.path)
        self.assertEqual(loaded.inode, forged.fence.inode)
        publish_shared_fence_config(original.fence)
        with self.assertRaises(FenceSafetyError) as conflict:
            publish_loaded_fence_trust(moved)
        self.assertEqual(conflict.exception.code, "fence_identity_changed")
        self.assertEqual(
            os.environ["MAX_SMART_PROVIDER_FENCE_INODE"],
            str(original.fence.inode),
        )

    def test_publish_is_set_once_and_an_explicit_root_cannot_override_it(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        trust = provision_disposable_fence_trust(directory)
        other = _directory()
        self.addCleanup(shutil.rmtree, other, True)
        decoy = prepare_disposable_fence(other / "provider.fence")
        first = publish_loaded_fence_trust(trust.pin)
        second = publish_loaded_fence_trust(trust.pin)
        self.assertEqual(first, second)
        self.assertEqual(first, trust.fence)
        with self.assertRaises(FenceSafetyError) as explicit:
            fence_config_from_trust(trust.pin, decoy)
        self.assertEqual(explicit.exception.code, "fence_identity_changed")
        self.assertEqual(env_fence_config(), trust.fence)
        _drop_fence_env()
        publish_shared_fence_config(decoy)
        with self.assertRaises(FenceSafetyError) as conflict:
            publish_loaded_fence_trust(trust.pin)
        self.assertEqual(conflict.exception.code, "fence_identity_changed")
        self.assertEqual(env_fence_config(), decoy)

    def test_malformed_missing_and_unsafe_records_fail_closed(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        trust = provision_disposable_fence_trust(directory)
        missing = TrustRecordPin(
            str(directory / "missing" / "fence.trust"),
            trust.pin.device,
            trust.pin.inode,
            trust.pin.digest,
        )
        with self.assertRaises(FenceSafetyError) as missing_error:
            load_disposable_fence_trust(missing)
        self.assertIn(
            missing_error.exception.code,
            {"trust_record_missing", "fence_directory_missing", "fence_symlink"},
        )
        absent = _directory()
        self.addCleanup(shutil.rmtree, absent, True)
        with self.assertRaises(FenceSafetyError) as absent_error:
            load_disposable_fence_trust(TrustRecordPin(
                str(absent / "fence.trust"),
                1,
                1,
                "a" * 64,
            ))
        self.assertEqual(absent_error.exception.code, "trust_record_missing")
        shifted = TrustRecordPin(
            trust.pin.path,
            trust.pin.device,
            trust.pin.inode + 1,
            trust.pin.digest,
        )
        with self.assertRaises(FenceSafetyError) as shifted_error:
            load_disposable_fence_trust(shifted)
        self.assertEqual(shifted_error.exception.code, "trust_record_identity")

        bad = _directory()
        self.addCleanup(shutil.rmtree, bad, True)
        truncated = _raw_pin(bad, b"{")
        with self.assertRaises(FenceSafetyError) as truncated_error:
            load_disposable_fence_trust(truncated)
        self.assertEqual(truncated_error.exception.code, "trust_record_truncated")
        extra = _directory()
        self.addCleanup(shutil.rmtree, extra, True)
        unexpected = _raw_pin(extra, b'{"device":1,"extra":1,"inode":1,"path":"/x","version":"no"}\n')
        with self.assertRaises(FenceSafetyError) as extra_error:
            load_disposable_fence_trust(unexpected)
        self.assertEqual(extra_error.exception.code, "trust_record_malformed")

        os.chmod(trust.pin.path, 0o644)
        try:
            with self.assertRaises(FenceSafetyError) as mode_error:
                load_disposable_fence_trust(trust.pin)
            self.assertEqual(mode_error.exception.code, "trust_record_permissions")
        finally:
            os.chmod(trust.pin.path, 0o600)
        link = directory / "extra-link"
        os.link(trust.pin.path, link)
        self.addCleanup(lambda: link.unlink(missing_ok=True))
        with self.assertRaises(FenceSafetyError) as link_error:
            load_disposable_fence_trust(trust.pin)
        self.assertEqual(link_error.exception.code, "trust_record_hard_link")
        link.unlink()

        alias_root = _directory()
        self.addCleanup(shutil.rmtree, alias_root, True)
        alias = alias_root / "alias"
        alias.symlink_to(directory, target_is_directory=True)
        through_alias = TrustRecordPin(
            str(alias / "fence.trust"),
            trust.pin.device,
            trust.pin.inode,
            trust.pin.digest,
        )
        with self.assertRaises(FenceSafetyError) as alias_error:
            load_disposable_fence_trust(through_alias)
        self.assertEqual(alias_error.exception.code, "fence_symlink")

        os.rename(trust.pin.path, directory / "record.real")
        linked = directory / "fence.trust"
        linked.symlink_to(directory / "record.real")
        through_file = TrustRecordPin(
            str(linked),
            trust.pin.device,
            trust.pin.inode,
            trust.pin.digest,
        )
        with self.assertRaises(FenceSafetyError) as file_link:
            load_disposable_fence_trust(through_file)
        self.assertEqual(file_link.exception.code, "trust_record_symlink")

    def test_opened_record_owner_is_rechecked(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        trust = provision_disposable_fence_trust(directory)
        real_fstat = os.fstat

        def changed(descriptor):
            current = real_fstat(descriptor)
            if current.st_ino != trust.pin.inode:
                return current
            return os.stat_result((
                current.st_mode,
                current.st_ino,
                current.st_dev,
                current.st_nlink,
                current.st_uid + 1,
                current.st_gid,
                current.st_size,
                int(current.st_atime),
                int(current.st_mtime),
                int(current.st_ctime),
            ))

        with mock.patch("provider_fence.os.fstat", side_effect=changed):
            with self.assertRaises(FenceSafetyError) as caught:
                load_disposable_fence_trust(trust.pin)
        self.assertEqual(caught.exception.code, "trust_record_owner")

    def test_concurrent_readers_do_not_create_or_modify_the_record(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        trust = provision_disposable_fence_trust(directory)
        before = Path(trust.pin.path).stat()
        names = sorted(path.name for path in directory.iterdir())
        results = []

        def read_once():
            results.append(load_disposable_fence_trust(trust.pin))

        threads = [threading.Thread(target=read_once) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        processes = [
            _run_child(_READ_CHILD, trust.pin)
            for _ in range(2)
        ]
        self.assertEqual(results, [trust.fence] * 4)
        self.assertTrue(all(process.returncode == 0 for process in processes))
        after = Path(trust.pin.path).stat()
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(
            sorted(path.name for path in directory.iterdir()),
            names,
        )

    def test_cross_process_lock_and_clock_gate_still_bind_dispatch(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        trust = provision_disposable_fence_trust(directory)
        self.assertEqual(EXECUTION_DEADLINE_US, 600_000_000)
        self.assertEqual(FIXED_LEASE_US, 720_000_000)
        self.assertEqual(RECOVERY_CLOCK_SKEW_MARGIN_US, 120_000_000)
        holder = self._hold(trust.pin)
        busy = acquire_exclusive(trust.fence)
        self.assertIsInstance(busy, FenceBusy)
        holder.stdin.write("\n\n")
        holder.stdin.flush()
        holder.wait(timeout=2)
        publish_loaded_fence_trust(trust.pin)
        spawned = []

        def reject_clock_popen(*_args, **_kwargs):
            spawned.append("clock")
            raise AssertionError("clock gate started a worker")

        class ScriptedClock:
            def __init__(self, values, method):
                self.values = list(values)
                self.method = method

            def _next(self):
                value = self.values.pop(0)
                if isinstance(value, BaseException):
                    raise value
                return value

            def now_us(self):
                if self.method != "wall":
                    raise RuntimeError("wrong clock")
                return self._next()

            def now_ns(self):
                if self.method != "monotonic":
                    raise RuntimeError("wrong clock")
                return self._next()

        refused = supervise(
            claimed_at=CLAIMED_AT,
            operation_id=OPERATION,
            runner_id=RUNNER,
            request_content="hello",
            standing_context="standing",
            wall_clock=lambda: CLAIMED_AT,
            fake="ok",
            fence_config=trust.fence,
            prove_claim=lambda: True,
            popen=reject_clock_popen,
            safety_wall_clock=ScriptedClock((0, 120_000_000), "wall"),
            safety_monotonic_clock=ScriptedClock((0, 0), "monotonic"),
        )
        self.assertEqual(refused.classification, "definite_non_send")
        self.assertEqual(refused.calls, 0)
        self.assertEqual(spawned, [])
        proved = supervise(
            claimed_at=CLAIMED_AT,
            operation_id=OPERATION,
            runner_id=RUNNER,
            request_content="hello",
            standing_context="standing",
            wall_clock=lambda: CLAIMED_AT,
            fake="ok",
            fence_config=trust.fence,
            prove_claim=lambda: False,
        )
        self.assertEqual(proved.classification, "definite_non_send")
        self.assertEqual(proved.calls, 0)
        self.assertNotEqual(proved.output, "answer")

    def test_locked_replacement_withholds_go_without_rewriting_the_record(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        trust = provision_disposable_fence_trust(directory)
        other = _directory()
        self.addCleanup(shutil.rmtree, other, True)
        replacement = prepare_disposable_fence(other / "provider.fence")
        before = _record_bytes(trust.pin)
        publish_loaded_fence_trust(trust.pin)

        def replace_before_go():
            os.replace(replacement.path, trust.fence.path)
            return True

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = supervise(
                claimed_at=CLAIMED_AT,
                operation_id=OPERATION,
                runner_id=RUNNER,
                request_content="hello",
                standing_context="standing",
                wall_clock=lambda: CLAIMED_AT,
                fake="ok",
                fence_config=trust.fence,
                prove_claim=replace_before_go,
            )
        self.assertEqual(result.classification, "definite_non_send")
        self.assertEqual(result.calls, 0)
        self.assertFalse(result.worker_started is True and result.output == "answer")
        self.assertEqual(_record_bytes(trust.pin), before)
        self.assertFalse(any(item.category is ResourceWarning for item in caught))

    def test_one_fake_provider_uses_the_loaded_identity(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        trust = provision_disposable_fence_trust(directory)
        publish_loaded_fence_trust(trust.pin)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = supervise(
                claimed_at=CLAIMED_AT,
                operation_id=OPERATION,
                runner_id=RUNNER,
                request_content="hello",
                standing_context="standing",
                wall_clock=lambda: CLAIMED_AT,
                fake="ok",
                fence_config=trust.fence,
                prove_claim=lambda: True,
            )
        self.assertEqual(result.classification, "success")
        self.assertEqual(result.output, "answer")
        self.assertEqual(result.calls, 1)
        self.assertTrue(result.worker_started)
        self.assertFalse(any(item.category is ResourceWarning for item in caught))

    def test_replaced_fence_blocks_recovery_prepare_and_retry(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        trust = provision_disposable_fence_trust(directory)
        other = _directory()
        self.addCleanup(shutil.rmtree, other, True)
        replacement = provision_disposable_fence_trust(other)
        before = _record_bytes(trust.pin)
        database = directory / "m4p2-trust.sqlite3"
        process = subprocess.Popen(
            (
                sys.executable,
                "-B",
                "-c",
                textwrap.dedent(_RECOVERY_CHILD),
                *_pin_args(trust.pin),
            ),
            cwd=PROJECT_ROOT,
            env=_child_env(
                DJANGO_SETTINGS_MODULE="max_smart_db.test_settings",
                MAX_SMART_TEST_DATABASE_PATH=str(database),
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: _finish_process(process))
        ready = process.stdout.readline().strip()
        self.assertEqual(ready, "ready", process.stderr.read() if ready != "ready" else "")
        os.replace(replacement.fence.path, trust.fence.path)
        process.stdin.write("\n")
        process.stdin.flush()
        process.stdin.close()
        stdout, stderr = process.communicate(timeout=90)
        self.assertEqual(process.returncode, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["recovery"], "ProviderFenceUnproven")
        self.assertEqual(payload["prepare"], "ProviderFenceUnproven")
        self.assertEqual(payload["retry"], "ProviderFenceUnproven")
        self.assertEqual(payload["dispatch"], "definite_non_send")
        self.assertEqual(payload["spawned"], 0)
        self.assertEqual(payload["status"], "RUNNING")
        self.assertEqual(payload["operations"], payload["before"])
        self.assertEqual(payload["record"], before.decode("ascii"))
        self.assertNotEqual(payload["live_inode"], trust.fence.inode)

    def _hold(self, pin):
        process = subprocess.Popen(
            (sys.executable, "-B", "-c", _HOLDER, *_pin_args(pin)),
            cwd=PROJECT_ROOT,
            env=_child_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: _finish_process(process))
        line = process.stdout.readline().strip()
        if line != "held":
            error = process.stderr.read()
            _finish_process(process)
            raise AssertionError(f"{line}\n{error}")
        return process


def _finish_process(process):
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
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None and not pipe.closed:
            pipe.close()


_AGREE_CHILD = """\
import json
import os
import sys
from provider_fence import TrustRecordPin, publish_loaded_fence_trust
inherited = any(
    name in os.environ
    for name in (
        "MAX_SMART_PROVIDER_FENCE",
        "MAX_SMART_PROVIDER_FENCE_DEVICE",
        "MAX_SMART_PROVIDER_FENCE_INODE",
    )
)
pin = TrustRecordPin(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
config = publish_loaded_fence_trust(pin)
again = publish_loaded_fence_trust(pin)
json.dump({
    "path": config.path,
    "device": config.device,
    "inode": config.inode,
    "same": again == config,
    "env_inode": os.environ.get("MAX_SMART_PROVIDER_FENCE_INODE"),
    "inherited": inherited,
}, sys.stdout)
"""


_READ_CHILD = """\
import sys
from provider_fence import TrustRecordPin, load_disposable_fence_trust
pin = TrustRecordPin(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
load_disposable_fence_trust(pin)
sys.stdout.write("ok\\n")
"""


_REFUSE_CHILD = """\
import json
import sys
from provider_fence import FenceSafetyError, TrustRecordPin, load_disposable_fence_trust
from provider_supervisor import supervise
pin = TrustRecordPin(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
spawned = {"count": 0}

def popen(*_args, **_kwargs):
    spawned["count"] += 1
    raise AssertionError("provider started")

try:
    load_disposable_fence_trust(pin)
    code = "loaded"
except FenceSafetyError as error:
    code = error.code
from pathlib import Path
fence_path = str(Path(pin.path).with_name("provider.fence"))
listed = __import__("os").stat(fence_path)
from provider_fence import ProviderFenceConfig
bogus = ProviderFenceConfig(fence_path, listed.st_dev, listed.st_ino)
result = supervise(
    claimed_at=1_000_000,
    operation_id="11111111-1111-1111-1111-111111111111",
    runner_id="AAAAAAAAAAAAAAAAAAAAAA",
    request_content="hello",
    standing_context="standing",
    wall_clock=lambda: 1_000_000,
    fake="ok",
    fence_config=bogus,
    prove_claim=lambda: True,
    popen=popen,
)
json.dump({
    "code": code,
    "spawned": spawned["count"],
    "dispatch": result.classification,
}, sys.stdout)
"""


_HOLDER = """\
import sys
from provider_fence import (
    FenceSafetyError,
    TrustRecordPin,
    acquire_exclusive,
    load_disposable_fence_trust,
)
pin = TrustRecordPin(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
config = load_disposable_fence_trust(pin)
held = acquire_exclusive(config)
if type(held).__name__ != "FenceHeld":
    sys.stdout.write("fail\\n")
    sys.stdout.flush()
    sys.exit(1)
sys.stdout.write("held\\n")
sys.stdout.flush()
line = sys.stdin.readline()
if line.startswith("check"):
    try:
        load_disposable_fence_trust(pin)
        sys.stdout.write("loaded\\n")
    except FenceSafetyError as error:
        sys.stdout.write("refused %s\\n" % error.code)
    sys.stdout.flush()
    sys.stdin.readline()
import os
sys.stdout.write("still %s\\n" % os.fstat(held.descriptor).st_ino)
sys.stdout.flush()
sys.stdin.readline()
"""


_RECOVERY_CHILD = """\
import json
import os
import sys
import uuid
from pathlib import Path

from provider_fence import ProviderFenceConfig, TrustRecordPin, publish_loaded_fence_trust

pin = TrustRecordPin(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
if any(
    name in os.environ
    for name in (
        "MAX_SMART_PROVIDER_FENCE",
        "MAX_SMART_PROVIDER_FENCE_DEVICE",
        "MAX_SMART_PROVIDER_FENCE_INODE",
    )
):
    raise SystemExit("inherited fence identity")
config = publish_loaded_fence_trust(pin)
database = Path(os.environ["MAX_SMART_TEST_DATABASE_PATH"])
if database.name == "max_smart.sqlite3":
    raise SystemExit("production database requested")
from max_smart_db.safety import initialize_sqlite_file
initialize_sqlite_file(database)
import django
django.setup()
from django.contrib.auth.models import User
from django.db.migrations.executor import MigrationExecutor
from django.db import connection
from max_smart_db.creation import SqliteConversationRepository
from max_smart_db.models import Conversation, Operation
from max_smart_db.repository import PrepareCommand, RetryCommand
from provider_supervisor import supervise

MigrationExecutor(connection).migrate(
    MigrationExecutor(connection).loader.graph.leaf_nodes()
)
owner = User.objects.create(username="owner")
runner = "AAAAAAAAAAAAAAAAAAAAAA"

class Clock:
    def now_us(self):
        return 1_000_000

repository = SqliteConversationRepository(Clock(), runner)
conversation_id = uuid.uuid4()
Conversation.objects.create(
    id=conversation_id,
    owner_id=owner.id,
    role_id="max-smart",
    version=0,
    next_sequence=1,
    title=None,
    created_at_us=0,
    updated_at_us=0,
)
import base64
key = base64.urlsafe_b64encode(bytes([7]) * 16).rstrip(b"=").decode()
prepared = repository.prepare(PrepareCommand(
    actor_id=owner.id,
    conversation_id=conversation_id,
    expected_version=0,
    request_content="persisted original",
    idempotency_key=key,
    provider_id="openai",
    model_id="gpt-5.6-sol",
))
if type(prepared).__name__ != "Prepared":
    raise SystemExit(type(prepared).__name__)
claimed = repository.claim_running(owner.id, prepared.reservation.operation_id)
if type(claimed).__name__ != "Claimed":
    raise SystemExit(type(claimed).__name__)
sys.stdout.write("ready\\n")
sys.stdout.flush()
sys.stdin.readline()
before = Operation.objects.count()
recovery = repository.recover_slot()
second = uuid.uuid4()
Conversation.objects.create(
    id=second,
    owner_id=owner.id,
    role_id="max-smart",
    version=0,
    next_sequence=1,
    title=None,
    created_at_us=0,
    updated_at_us=0,
)
prepare = repository.prepare(PrepareCommand(
    actor_id=owner.id,
    conversation_id=second,
    expected_version=0,
    request_content="persisted original",
    idempotency_key=base64.urlsafe_b64encode(bytes([8]) * 16).rstrip(b"=").decode(),
    provider_id="openai",
    model_id="gpt-5.6-sol",
))
retry = repository.create_retry(RetryCommand(
    actor_id=owner.id,
    source_operation_id=prepared.reservation.operation_id,
    idempotency_key=base64.urlsafe_b64encode(bytes([9]) * 16).rstrip(b"=").decode(),
    expected_version=0,
))
spawned = {"count": 0}

def popen(*_args, **_kwargs):
    spawned["count"] += 1
    raise AssertionError("provider started")

live = os.stat(config.path)
bogus = ProviderFenceConfig(config.path, live.st_dev, live.st_ino)
dispatch = supervise(
    claimed_at=claimed.claimed_operation.claimed_at,
    operation_id=str(claimed.claimed_operation.operation_id),
    runner_id=runner,
    request_content="persisted original",
    standing_context="standing",
    wall_clock=lambda: 1_000_000,
    fake="ok",
    fence_config=bogus,
    prove_claim=lambda: True,
    popen=popen,
)
json.dump({
    "recovery": type(recovery).__name__,
    "prepare": type(prepare).__name__,
    "retry": type(retry).__name__,
    "dispatch": dispatch.classification,
    "spawned": spawned["count"],
    "status": Operation.objects.get(pk=prepared.reservation.operation_id).status,
    "before": before,
    "operations": Operation.objects.count(),
    "record": Path(pin.path).read_text(encoding="ascii"),
    "live_inode": live.st_ino,
}, sys.stdout)
"""


if __name__ == "__main__":
    unittest.main()
