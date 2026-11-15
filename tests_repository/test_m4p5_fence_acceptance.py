"""Disposable receipt and acceptance tests. No production path is used."""

import errno
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import warnings
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
REAL_PIN_DIRECTORY = Path(
    "/Users/smart/Library/Application Support/MaxSmartTrust"
)
sys.path.insert(0, str(SRC_ROOT))

from max_smart_db.config import (  # noqa: E402
    EXECUTION_DEADLINE_US,
    FIXED_LEASE_US,
    QUEUED_RESERVATION_US,
    RECOVERY_CLOCK_SKEW_MARGIN_US,
)
import provider_fence  # noqa: E402
from provider_fence import (  # noqa: E402
    FenceBusy,
    FenceHeld,
    FenceSafetyError,
    ProviderFenceConfig,
    accept_disposable_fence_receipt,
    acquire_exclusive,
    acquire_worker_fence,
    begin_disposable_fence_provision,
    complete_disposable_fence_provision,
    env_fence_config,
    fence_still_matches,
    load_disposable_fence_acceptance,
    load_disposable_fence_pin,
    prepare_disposable_fence,
    provision_disposable_fence_pin,
    provision_disposable_fence_receipt,
    publish_loaded_fence_acceptance,
    publish_loaded_fence_pin,
    publish_shared_fence_config,
    release_fence,
)
from provider_supervisor import supervise  # noqa: E402


_FENCE_ENV = (
    "MAX_SMART_PROVIDER_FENCE",
    "MAX_SMART_PROVIDER_FENCE_DEVICE",
    "MAX_SMART_PROVIDER_FENCE_INODE",
)
_RECEIPT_VERSION = "max-smart-provider-fence-receipt-v1"
_ACCEPTANCE_VERSION = "max-smart-provider-fence-acceptance-v1"
_ACCEPTANCE_NAME = "provider-fence.acceptance"
CLAIMED_AT = 1_000_000
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
OPERATION = "11111111-1111-1111-1111-111111111111"
_FIXED_TOKEN = b"\x11" * 16


def _drop_fence_env():
    for name in _FENCE_ENV:
        os.environ.pop(name, None)


def _directory():
    directory = Path(tempfile.mkdtemp(prefix="m4p5-fence-acceptance-")).resolve()
    os.chmod(directory, 0o700)
    return directory


def _pair():
    return _directory(), _directory()


def _child_env():
    environment = {**os.environ, "PYTHONPATH": str(SRC_ROOT)}
    for name in _FENCE_ENV:
        environment.pop(name, None)
    return environment


def _finish_process(process):
    if process.poll() is None:
        process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.wait(timeout=2)
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None and not pipe.closed:
            pipe.close()


def _targets_approved_pin(path):
    try:
        text = os.fspath(path)
    except TypeError:
        return False
    if type(text) is not str or text == "":
        return False
    if "MaxSmartTrust" in Path(text).parts or text == "MaxSmartTrust":
        return True
    approved = str(REAL_PIN_DIRECTORY)
    absolute = os.path.abspath(text)
    return absolute == approved or absolute.startswith(approved + os.sep)


def _fixed_token(count):
    if count != 16:
        raise AssertionError(count)
    return _FIXED_TOKEN


def _echo(binding):
    return dict(binding)


class _ObservedLifecycle:
    """A permit lifecycle lock that reports when a second caller waits."""

    def __init__(self):
        self._lock = threading.Lock()
        self.contended = threading.Event()

    def __enter__(self):
        if self._lock.locked():
            self.contended.set()
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._lock.release()
        return False


class FenceAcceptanceTests(unittest.TestCase):
    def tearDown(self):
        _drop_fence_env()

    def test_receipt_is_emitted_only_after_successful_provisioning(self):
        fence_directory, pin_directory = self._fresh_pair()
        with self.assertRaises(FenceSafetyError) as failed:
            provision_disposable_fence_receipt(fence_directory, fence_directory)
        self.assertEqual(failed.exception.code, "pin_directory_invalid")
        self.assertFalse((fence_directory / "provider.fence").exists())
        receipt = provision_disposable_fence_receipt(
            fence_directory,
            pin_directory,
            token_bytes=_fixed_token,
        )
        parsed = self._assert_canonical_receipt(receipt.receipt_bytes)
        self.assertEqual(parsed["result"], "provisioned")
        self.assertEqual(parsed["receipt_id"], _FIXED_TOKEN.hex())
        self.assertFalse((pin_directory / _ACCEPTANCE_NAME).exists())
        self.assertIsNone(env_fence_config())

    def test_existing_pin_does_not_become_a_receipt(self):
        fence_directory, pin_directory = self._fresh_pair()
        created = provision_disposable_fence_pin(fence_directory, pin_directory)
        with self.assertRaises(FenceSafetyError) as caught:
            provision_disposable_fence_receipt(fence_directory, pin_directory)
        self.assertIn(
            caught.exception.code,
            {"fence_exists", "trust_record_exists", "pin_file_exists"},
        )
        self.assertFalse(hasattr(created, "receipt_bytes"))
        self.assertFalse((pin_directory / _ACCEPTANCE_NAME).exists())

    def test_crashed_or_unsuccessful_provision_is_ineligible(self):
        fence_directory, pin_directory = self._fresh_pair()
        process = begin_disposable_fence_provision(
            fence_directory,
            pin_directory,
            token_bytes=_fixed_token,
        )
        self.addCleanup(_finish_process, process)
        emitted = process.stdout.readline()
        self.assertIn(_RECEIPT_VERSION.encode("ascii"), emitted)
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
        with self.assertRaises(FenceSafetyError) as crashed:
            complete_disposable_fence_provision(process)
        self.assertEqual(crashed.exception.code, "provision_exit_failed")
        with self.assertRaises(FenceSafetyError) as reused:
            complete_disposable_fence_provision(process)
        self.assertEqual(reused.exception.code, "provision_exit_unproven")
        self.assertFalse((pin_directory / _ACCEPTANCE_NAME).exists())
        with self.assertRaises(FenceSafetyError) as raw:
            accept_disposable_fence_receipt(emitted, pin_directory, _echo)
        self.assertEqual(raw.exception.code, "provision_exit_unproven")

        failed_fence, failed_pin = self._fresh_pair()
        (failed_fence / "provider.fence").write_bytes(b"present\n")
        failed = begin_disposable_fence_provision(failed_fence, failed_pin)
        self.addCleanup(_finish_process, failed)
        with self.assertRaises(FenceSafetyError) as unsuccessful:
            complete_disposable_fence_provision(failed)
        self.assertEqual(unsuccessful.exception.code, "provision_exit_failed")
        self.assertFalse((failed_pin / "provider-fence.pin").exists())
        self.assertFalse((failed_pin / _ACCEPTANCE_NAME).exists())

    def test_receipt_ids_are_unique_canonical_and_injectable(self):
        first = self._capture()
        second = self._capture()
        left = json.loads(first.receipt.receipt_bytes.decode("ascii"))
        right = json.loads(second.receipt.receipt_bytes.decode("ascii"))
        self.assertNotEqual(left["receipt_id"], right["receipt_id"])
        self.assertEqual(len(left["receipt_id"]), 32)
        self.assertEqual(len(right["receipt_id"]), 32)
        injected_fence, injected_pin = self._fresh_pair()
        injected = provision_disposable_fence_receipt(
            injected_fence,
            injected_pin,
            token_bytes=_fixed_token,
        )
        parsed = self._assert_canonical_receipt(injected.receipt_bytes)
        self.assertEqual(parsed["receipt_id"], _FIXED_TOKEN.hex())
        again = provision_disposable_fence_receipt(
            *self._fresh_pair(),
            token_bytes=_fixed_token,
        )
        repeated = json.loads(again.receipt_bytes.decode("ascii"))
        self.assertEqual(repeated["receipt_id"], parsed["receipt_id"])
        self.assertNotEqual(repeated["fence_inode"], parsed["fence_inode"])

    def test_confirmation_binds_receipt_digest_and_identities(self):
        _fence, pin_directory, completion = self._accepted_inputs()
        parsed = json.loads(completion.receipt.receipt_bytes.decode("ascii"))
        seen = {}
        created = []
        real_open = os.open

        def spy(path, flags, mode=0o777, *, dir_fd=None):
            if path == _ACCEPTANCE_NAME and flags & os.O_CREAT:
                created.append(path)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        def confirm(binding):
            seen["binding"] = binding
            self.assertEqual(created, [])
            self.assertFalse((pin_directory / _ACCEPTANCE_NAME).exists())
            self.assertEqual(binding["receipt_id"], parsed["receipt_id"])
            self.assertEqual(
                binding["receipt_digest"],
                hashlib.sha256(completion.receipt.receipt_bytes).hexdigest(),
            )
            for key in (
                "fence_path",
                "fence_device",
                "fence_inode",
                "record_path",
                "record_device",
                "record_inode",
                "record_digest",
                "pin_path",
                "pin_device",
                "pin_inode",
                "pin_digest",
            ):
                self.assertEqual(binding[key], parsed[key])
            pin_status = Path(parsed["pin_path"]).stat()
            record_status = Path(parsed["record_path"]).stat()
            fence_status = Path(parsed["fence_path"]).stat()
            self.assertEqual(binding["pin_inode"], pin_status.st_ino)
            self.assertEqual(binding["record_inode"], record_status.st_ino)
            self.assertEqual(binding["fence_inode"], fence_status.st_ino)
            return dict(binding)

        with mock.patch("provider_fence.os.open", side_effect=spy):
            path = accept_disposable_fence_receipt(
                completion,
                pin_directory,
                confirm,
            )
        self.assertEqual(created, [_ACCEPTANCE_NAME])
        self.assertEqual(path, str(pin_directory / _ACCEPTANCE_NAME))
        accepted = json.loads(Path(path).read_bytes().decode("ascii"))
        self.assertEqual(accepted["receipt_digest"], seen["binding"]["receipt_digest"])
        self.assertEqual(accepted["receipt_id"], parsed["receipt_id"])
        self.assertEqual(accepted["receipt_version"], _RECEIPT_VERSION)

    def test_rejected_or_mismatched_confirmation_creates_nothing(self):
        for decision in (None, False, True, {}, lambda _binding: False):
            _fence, pin_directory, completion = self._accepted_inputs()
            with self.assertRaises(FenceSafetyError) as caught:
                accept_disposable_fence_receipt(
                    completion,
                    pin_directory,
                    decision,
                )
            self.assertEqual(
                caught.exception.code,
                "acceptance_confirmation_refused",
            )
            self.assertFalse((pin_directory / _ACCEPTANCE_NAME).exists())
        _fence, pin_directory, completion = self._accepted_inputs()

        def mismatch(binding):
            altered = dict(binding)
            altered["receipt_id"] = "ab" * 16
            return altered

        with self.assertRaises(FenceSafetyError) as mismatched:
            accept_disposable_fence_receipt(completion, pin_directory, mismatch)
        self.assertEqual(
            mismatched.exception.code,
            "acceptance_confirmation_refused",
        )
        self.assertFalse((pin_directory / _ACCEPTANCE_NAME).exists())

    def test_clean_process_exit_cannot_be_supplied_by_the_caller(self):
        _fence, pin_directory, completion = self._accepted_inputs()
        for evidence in (
            True,
            False,
            0,
            1,
            completion.receipt,
            completion.receipt.receipt_bytes,
        ):
            with self.assertRaises(FenceSafetyError) as caught:
                accept_disposable_fence_receipt(evidence, pin_directory, _echo)
            self.assertEqual(caught.exception.code, "provision_exit_unproven")
        foreign = subprocess.Popen(
            (sys.executable, "-B", "-c", "import sys; sys.exit(0)"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(_finish_process, foreign)
        foreign.wait(timeout=5)
        with self.assertRaises(FenceSafetyError) as unproven:
            complete_disposable_fence_provision(foreign)
        self.assertEqual(unproven.exception.code, "provision_exit_unproven")
        self.assertFalse((pin_directory / _ACCEPTANCE_NAME).exists())
        reused_fence, reused_pin = self._fresh_pair()
        process = begin_disposable_fence_provision(reused_fence, reused_pin)
        self.addCleanup(_finish_process, process)
        sealed = complete_disposable_fence_provision(process)
        with self.assertRaises(FenceSafetyError) as reused:
            complete_disposable_fence_provision(process)
        self.assertEqual(reused.exception.code, "provision_exit_unproven")
        self.assertTrue(
            accept_disposable_fence_receipt(sealed, reused_pin, _echo).endswith(
                _ACCEPTANCE_NAME
            )
        )

    def test_receipt_mismatch_against_each_identity_is_refused(self):
        cases = ("fence", "record", "pin")
        for target in cases:
            _fence, pin_directory, completion = self._accepted_inputs()
            parsed = json.loads(completion.receipt.receipt_bytes.decode("ascii"))
            self._replace_identity(parsed, target)
            called = {"value": False}

            def confirm(_binding):
                called["value"] = True
                return {}

            with self.assertRaises(FenceSafetyError) as caught:
                accept_disposable_fence_receipt(
                    completion,
                    pin_directory,
                    confirm,
                )
            self.assertEqual(caught.exception.code, "receipt_chain_mismatch")
            self.assertFalse(called["value"])
            self.assertFalse((pin_directory / _ACCEPTANCE_NAME).exists())

    def test_acceptance_file_is_canonical_and_bound_to_the_receipt(self):
        fence_directory, pin_directory, completion, path = self._accept()
        payload = Path(path).read_bytes()
        self.assertLessEqual(len(payload), 8192)
        self.assertEqual(payload.count(b"\n"), 1)
        self.assertTrue(payload.endswith(b"\n"))
        parsed = json.loads(payload.decode("ascii"))
        rendered = (
            json.dumps(
                parsed,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
        self.assertEqual(payload, rendered)
        self.assertEqual(parsed["version"], _ACCEPTANCE_VERSION)
        receipt = json.loads(completion.receipt.receipt_bytes.decode("ascii"))
        self.assertEqual(
            parsed["receipt_digest"],
            hashlib.sha256(completion.receipt.receipt_bytes).hexdigest(),
        )
        self.assertEqual(parsed["receipt_id"], receipt["receipt_id"])
        loaded = load_disposable_fence_acceptance(
            path,
            completion.receipt.receipt_bytes,
        )
        self.assertEqual(loaded.path, str(fence_directory / "provider.fence"))
        self.assertEqual(loaded.device, receipt["fence_device"])
        self.assertEqual(loaded.inode, receipt["fence_inode"])
        with self.assertRaises(FenceSafetyError) as missing_receipt:
            load_disposable_fence_acceptance(path, None)
        self.assertEqual(
            missing_receipt.exception.code,
            "acceptance_receipt_required",
        )
        tampered = bytearray(completion.receipt.receipt_bytes)
        tampered[-2] ^= 1
        with self.assertRaises(FenceSafetyError) as digest:
            load_disposable_fence_acceptance(path, bytes(tampered))
        self.assertIn(
            digest.exception.code,
            {"acceptance_receipt_digest", "receipt_malformed"},
        )

    def test_missing_empty_malformed_and_noncanonical_acceptance_is_refused(self):
        _fence, pin_directory, completion, path = self._accept()
        receipt = completion.receipt.receipt_bytes
        missing = str(pin_directory / "missing" / _ACCEPTANCE_NAME)
        with self.assertRaises(FenceSafetyError) as absent:
            load_disposable_fence_acceptance(
                str(pin_directory / "absent.acceptance"),
                receipt,
            )
        self.assertEqual(absent.exception.code, "acceptance_file_invalid")
        os.unlink(path)
        with self.assertRaises(FenceSafetyError) as gone:
            load_disposable_fence_acceptance(path, receipt)
        self.assertEqual(gone.exception.code, "acceptance_file_missing")
        self.assertNotEqual(missing, path)
        for payload, code in (
            (b"", "acceptance_file_malformed"),
            (b"{", "acceptance_file_malformed"),
            (b"{}\n", "acceptance_file_malformed"),
            (payload_with_space(receipt), "acceptance_file_malformed"),
            (payload_with_boolean(receipt), "acceptance_file_malformed"),
        ):
            _write_exclusive(Path(path), payload)
            with self.assertRaises(FenceSafetyError) as caught:
                load_disposable_fence_acceptance(path, receipt)
            self.assertEqual(caught.exception.code, code)
            os.unlink(path)

    def test_unsafe_acceptance_files_are_refused(self):
        _fence, pin_directory, completion, path = self._accept()
        receipt = completion.receipt.receipt_bytes
        original = Path(path).read_bytes()
        os.chmod(path, 0o644)
        with self.assertRaises(FenceSafetyError) as mode:
            load_disposable_fence_acceptance(path, receipt)
        self.assertEqual(mode.exception.code, "acceptance_file_permissions")
        os.chmod(path, 0o600)
        link = pin_directory / "acceptance-link"
        os.link(path, link)
        with self.assertRaises(FenceSafetyError) as links:
            load_disposable_fence_acceptance(path, receipt)
        self.assertEqual(links.exception.code, "acceptance_file_hard_link")
        os.unlink(link)
        os.unlink(path)
        fifo = Path(path)
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(FenceSafetyError) as pipe:
            load_disposable_fence_acceptance(path, receipt)
        self.assertEqual(pipe.exception.code, "acceptance_file_malformed")
        os.unlink(fifo)
        Path(path).symlink_to(pin_directory / "provider-fence.pin")
        with self.assertRaises(FenceSafetyError) as link_error:
            load_disposable_fence_acceptance(path, receipt)
        self.assertEqual(link_error.exception.code, "acceptance_file_symlink")
        os.unlink(path)
        _write_exclusive(Path(path), original)
        real_stat = os.stat

        def foreign_owner(path_arg, *args, **kwargs):
            current = real_stat(path_arg, *args, **kwargs)
            if os.fspath(path_arg) != _ACCEPTANCE_NAME:
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

        with mock.patch("provider_fence.os.stat", side_effect=foreign_owner):
            with self.assertRaises(FenceSafetyError) as owner:
                load_disposable_fence_acceptance(path, receipt)
        self.assertEqual(owner.exception.code, "acceptance_file_owner")

    def test_short_write_revokes_the_acceptance_in_place(self):
        _fence, pin_directory, completion = self._accepted_inputs()
        real_write = os.write
        calls = {"count": 0}

        def short_write(descriptor, payload):
            if (
                isinstance(payload, bytes)
                and _ACCEPTANCE_VERSION.encode("ascii") in payload
            ):
                calls["count"] += 1
                real_write(descriptor, payload[:1])
                return 1
            return real_write(descriptor, payload)

        with mock.patch("provider_fence.os.write", side_effect=short_write):
            with self.assertRaises(FenceSafetyError) as caught:
                accept_disposable_fence_receipt(completion, pin_directory, _echo)
        acceptance = pin_directory / _ACCEPTANCE_NAME
        self.assertEqual(calls["count"], 1)
        self.assertEqual(caught.exception.code, "acceptance_file_create_failed")
        self._assert_revoked(acceptance)
        self._assert_permit_released(completion)
        with self.assertRaises(FenceSafetyError):
            load_disposable_fence_acceptance(
                str(acceptance),
                completion.receipt.receipt_bytes,
            )

    def test_fsync_failures_revoke_the_acceptance(self):
        self._assert_fsync_revocation(file_sync=True)
        self._assert_fsync_revocation(file_sync=False)

    def test_close_failure_preserves_the_active_error(self):
        _fence, pin_directory, completion = self._accepted_inputs()
        real_fsync = os.fsync
        real_fstat = os.fstat
        real_close = os.close
        directory_inode = pin_directory.lstat().st_ino
        armed = {"value": False}
        seen = {}

        def fail_content_fsync(descriptor):
            current = real_fstat(descriptor)
            if stat.S_ISREG(current.st_mode) and _is_acceptance(descriptor):
                seen["inode"] = current.st_ino
                armed["value"] = True
                raise OSError(errno.EIO, "file fsync failed")
            return real_fsync(descriptor)

        def fail_close(descriptor):
            current = real_fstat(descriptor)
            real_close(descriptor)
            if armed["value"] and current.st_ino in (seen["inode"], directory_inode):
                raise OSError(errno.EIO, "close failed")
            return None

        with (
            mock.patch("provider_fence.os.fsync", side_effect=fail_content_fsync),
            mock.patch("provider_fence.os.close", side_effect=fail_close),
        ):
            with self.assertRaises(FenceSafetyError) as caught:
                accept_disposable_fence_receipt(completion, pin_directory, _echo)
        acceptance = pin_directory / _ACCEPTANCE_NAME
        self.assertEqual(caught.exception.code, "acceptance_file_fsync_failed")
        self.assertTrue(any(
            "close failed: errno " in note
            for note in caught.exception.__notes__
        ))
        self._assert_revoked(acceptance)

    def test_unverified_revocation_is_uncertain_and_canonical_bytes_can_remain(self):
        _fence, pin_directory, completion = self._accepted_inputs()
        real_fsync = os.fsync
        real_fstat = os.fstat

        def fail_content_fsync(descriptor):
            current = real_fstat(descriptor)
            if stat.S_ISREG(current.st_mode) and _is_acceptance(descriptor):
                raise OSError(errno.EIO, "file fsync failed")
            return real_fsync(descriptor)

        def fail_truncate(_descriptor, _length):
            raise OSError(errno.EIO, "truncate failed")

        with (
            mock.patch("provider_fence.os.fsync", side_effect=fail_content_fsync),
            mock.patch("provider_fence.os.ftruncate", side_effect=fail_truncate),
        ):
            with self.assertRaises(FenceSafetyError) as caught:
                accept_disposable_fence_receipt(completion, pin_directory, _echo)
        acceptance = pin_directory / _ACCEPTANCE_NAME
        self.assertEqual(caught.exception.code, "acceptance_completion_uncertain")
        self.assertEqual(
            caught.exception.__cause__.code,
            "acceptance_file_fsync_failed",
        )
        self.assertIn(_ACCEPTANCE_VERSION.encode("ascii"), acceptance.read_bytes())
        loaded = load_disposable_fence_acceptance(
            str(acceptance),
            completion.receipt.receipt_bytes,
        )
        receipt = json.loads(completion.receipt.receipt_bytes.decode("ascii"))
        self.assertEqual(loaded.path, receipt["fence_path"])
        self.assertEqual(loaded.inode, receipt["fence_inode"])
        before = acceptance.read_bytes()
        with self.assertRaises(FenceSafetyError) as second:
            accept_disposable_fence_receipt(completion, pin_directory, _echo)
        self.assertEqual(second.exception.code, "acceptance_file_exists")
        self.assertEqual(acceptance.read_bytes(), before)

    def test_descriptor_close_failure_does_not_claim_success(self):
        _fence, pin_directory, completion = self._accepted_inputs()
        real_close = os.close
        real_fstat = os.fstat
        armed = {"value": False}

        def fail_close(descriptor):
            current = real_fstat(descriptor)
            payload = b""
            if stat.S_ISREG(current.st_mode) and current.st_size:
                offset = os.lseek(descriptor, 0, os.SEEK_CUR)
                os.lseek(descriptor, 0, os.SEEK_SET)
                payload = os.read(descriptor, 8192)
                os.lseek(descriptor, offset, os.SEEK_SET)
            real_close(descriptor)
            if _ACCEPTANCE_VERSION.encode("ascii") in payload:
                armed["value"] = True
                raise OSError(errno.EIO, "close failed")
            return None

        with mock.patch("provider_fence.os.close", side_effect=fail_close):
            with self.assertRaises(FenceSafetyError) as caught:
                accept_disposable_fence_receipt(completion, pin_directory, _echo)
        self.assertTrue(armed["value"])
        self.assertEqual(caught.exception.code, "acceptance_completion_uncertain")
        self.assertEqual(
            caught.exception.__cause__.code,
            "acceptance_descriptor_close_failed",
        )
        acceptance = pin_directory / _ACCEPTANCE_NAME
        self.assertIn(_ACCEPTANCE_VERSION.encode("ascii"), acceptance.read_bytes())
        loaded = load_disposable_fence_acceptance(
            str(acceptance),
            completion.receipt.receipt_bytes,
        )
        self.assertEqual(
            loaded.path,
            json.loads(completion.receipt.receipt_bytes.decode("ascii"))["fence_path"],
        )

    def test_missing_or_replaced_chain_is_not_repaired(self):
        fence_directory, pin_directory, completion, path = self._accept()
        receipt = completion.receipt.receipt_bytes
        parsed = json.loads(receipt.decode("ascii"))
        record_before = Path(parsed["record_path"]).read_bytes()
        for target in ("pin", "record", "fence"):
            self._replace_identity(parsed, target)
            with self.assertRaises(FenceSafetyError) as caught:
                load_disposable_fence_acceptance(path, receipt)
            self.assertEqual(caught.exception.code, "acceptance_chain_mismatch")
            self._restore_identity(parsed, target, record_before)
        os.unlink(parsed["pin_path"])
        with self.assertRaises(FenceSafetyError) as missing:
            load_disposable_fence_acceptance(path, receipt)
        self.assertEqual(missing.exception.code, "acceptance_chain_mismatch")
        self.assertFalse((pin_directory / "provider-fence.pin").exists())
        self.assertTrue((fence_directory / "fence.trust").exists())
        self.assertFalse((pin_directory / "provider-fence.pin.repaired").exists())

    def test_second_acceptance_is_refused_without_repair(self):
        _fence, pin_directory, completion, path = self._accept()
        before = Path(path).read_bytes()
        with self.assertRaises(FenceSafetyError) as caught:
            accept_disposable_fence_receipt(completion, pin_directory, _echo)
        self.assertEqual(caught.exception.code, "acceptance_file_exists")
        self.assertEqual(Path(path).read_bytes(), before)
        self.assertEqual(
            sorted(item.name for item in pin_directory.iterdir()),
            ["provider-fence.acceptance", "provider-fence.pin"],
        )

    def test_concurrent_readers_leave_the_acceptance_unchanged(self):
        _fence, _pin, completion, path = self._accept()
        receipt = completion.receipt.receipt_bytes
        before = Path(path).stat()
        results = []

        def read_once():
            results.append(load_disposable_fence_acceptance(path, receipt))

        threads = [threading.Thread(target=read_once) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        child = subprocess.run(
            (
                sys.executable,
                "-B",
                "-c",
                _LOAD_CHILD,
                path,
                receipt.hex(),
            ),
            cwd=PROJECT_ROOT,
            env=_child_env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertTrue(all(thread.is_alive() is False for thread in threads))
        self.assertEqual(len(results), 4)
        self.assertTrue(all(item == results[0] for item in results))
        self.assertEqual(child.stdout, f"{results[0].path} {results[0].inode}")
        after = Path(path).stat()
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(after.st_size, before.st_size)

    def test_acceptance_holds_the_fence_through_confirmation_and_creation(self):
        _fence, pin_directory, completion = self._accepted_inputs()
        pin_path = str(pin_directory / "provider-fence.pin")
        real_open = os.open
        observed = {"confirm": False, "create": False}

        def confirm(binding):
            observed["confirm"] = _fence_gate_name(pin_path) == "FenceBusy"
            self.assertFalse((pin_directory / _ACCEPTANCE_NAME).exists())
            return dict(binding)

        def spy(path, flags, mode=0o777, *, dir_fd=None):
            if path == _ACCEPTANCE_NAME and flags & os.O_CREAT:
                observed["create"] = _fence_gate_name(pin_path) == "FenceBusy"
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with mock.patch("provider_fence.os.open", side_effect=spy):
            path = accept_disposable_fence_receipt(
                completion,
                pin_directory,
                confirm,
            )
        self.assertTrue(observed["confirm"])
        self.assertTrue(observed["create"])
        self.assertTrue(path.endswith(_ACCEPTANCE_NAME))
        self._assert_permit_released(completion)

    def test_busy_worker_refuses_acceptance_before_confirmation(self):
        _fence, pin_directory, completion = self._accepted_inputs()
        pin_path = str(pin_directory / "provider-fence.pin")
        publish_loaded_fence_pin(pin_path)
        holder = self._worker_hold(pin_path)
        called = {"confirm": False, "created": False}
        real_open = os.open

        def confirm(_binding):
            called["confirm"] = True
            return {}

        def spy(path, flags, mode=0o777, *, dir_fd=None):
            if path == _ACCEPTANCE_NAME and flags & os.O_CREAT:
                called["created"] = True
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with mock.patch("provider_fence.os.open", side_effect=spy):
            with self.assertRaises(FenceSafetyError) as caught:
                accept_disposable_fence_receipt(completion, pin_directory, confirm)
        self.assertEqual(caught.exception.code, "fence_busy")
        self.assertFalse(called["confirm"])
        self.assertFalse(called["created"])
        self.assertFalse((pin_directory / _ACCEPTANCE_NAME).exists())
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=2)

    def test_release_failure_preserves_the_primary_error_and_blocks_success(self):
        _fence, pin_directory, completion = self._accepted_inputs()
        real_lock = provider_fence.fcntl.lockf

        def fail_unlock(descriptor, command, *args):
            if command == provider_fence.fcntl.LOCK_UN:
                raise OSError(errno.EIO, "unlock failed")
            return real_lock(descriptor, command, *args)

        with mock.patch("provider_fence.fcntl.lockf", side_effect=fail_unlock):
            with self.assertRaises(FenceSafetyError) as released:
                accept_disposable_fence_receipt(completion, pin_directory, _echo)
        self.assertEqual(released.exception.code, "fence_release_failed")
        self.assertIsInstance(released.exception.__cause__, OSError)
        acceptance = pin_directory / _ACCEPTANCE_NAME
        self.assertIn(_ACCEPTANCE_VERSION.encode("ascii"), acceptance.read_bytes())
        self._assert_permit_released(completion)

        real_write = os.write

        def short_write(descriptor, payload):
            if (
                isinstance(payload, bytes)
                and _ACCEPTANCE_VERSION.encode("ascii") in payload
            ):
                real_write(descriptor, payload[:1])
                return 1
            return real_write(descriptor, payload)

        _fence, pin_directory, completion = self._accepted_inputs()
        with (
            mock.patch("provider_fence.os.write", side_effect=short_write),
            mock.patch("provider_fence.fcntl.lockf", side_effect=fail_unlock),
        ):
            with self.assertRaises(FenceSafetyError) as primary:
                accept_disposable_fence_receipt(completion, pin_directory, _echo)
        self.assertEqual(primary.exception.code, "acceptance_file_create_failed")
        self.assertTrue(any(
            "fence release failed: errno " in note
            for note in primary.exception.__notes__
        ))
        self._assert_revoked(pin_directory / _ACCEPTANCE_NAME)
        self._assert_permit_released(completion)

    def test_ambiguous_release_keeps_the_permit_until_the_os_lock_is_proven(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        config = prepare_disposable_fence(directory / "provider.fence")
        held = acquire_exclusive(config)
        self.assertIsInstance(held, FenceHeld)
        self.assertIsNone(release_fence(held))
        self.assertTrue(held.released)
        self.assertIsNone(provider_fence._active_held(config))
        restored = acquire_exclusive(config)
        self.assertIsInstance(restored, FenceHeld)

        descriptor = restored.descriptor
        closes = {"count": 0}
        real_close = os.close
        real_lock = provider_fence.fcntl.lockf

        def fail_unlock(fd, command, *args):
            if fd == descriptor and command == provider_fence.fcntl.LOCK_UN:
                raise OSError(errno.EIO, "unlock failed")
            return real_lock(fd, command, *args)

        def fail_close(fd):
            if fd == descriptor:
                closes["count"] += 1
                raise OSError(errno.EBADF, "close failed")
            return real_close(fd)

        with (
            mock.patch("provider_fence.fcntl.lockf", side_effect=fail_unlock),
            mock.patch("provider_fence.os.close", side_effect=fail_close),
        ):
            problem = release_fence(restored)
            self.assertEqual(problem.errno, errno.EIO)
            self.assertTrue(any(
                "descriptor close failed: errno %s" % errno.EBADF in note
                for note in problem.__notes__
            ))
            self.assertFalse(restored.released)
            self.assertTrue(restored.closure_uncertain)
            self.assertIs(provider_fence._active_held(config), restored)
            self.assertTrue(restored.mutex.locked())
            self.assertIsInstance(acquire_exclusive(config), FenceBusy)
            self.assertEqual(closes["count"], 1)
            again = release_fence(restored)
            self.assertIs(again, problem)
            self.assertEqual(closes["count"], 1)
            self.assertFalse(restored.released)
            self.assertTrue(restored.quarantined)
            self.assertTrue(restored.mutex.locked())
            self.assertIsInstance(acquire_exclusive(config), FenceBusy)

        self.assertIs(release_fence(restored), problem)
        self.assertIs(provider_fence._active_held(config), restored)
        self.assertIsInstance(acquire_exclusive(config), FenceBusy)
        os.open(config.path, os.O_RDONLY)

        other = _directory()
        self.addCleanup(shutil.rmtree, other, True)
        other_config = prepare_disposable_fence(other / "provider.fence")
        other_held = acquire_exclusive(other_config)
        self.assertIsInstance(other_held, FenceHeld)
        self.assertIsNone(release_fence(other_held))

        closed = acquire_exclusive(other_config)
        self.assertIsInstance(closed, FenceHeld)
        closed_descriptor = closed.descriptor

        def fail_only_unlock(fd, command, *args):
            if fd == closed_descriptor and command == provider_fence.fcntl.LOCK_UN:
                raise OSError(errno.EIO, "unlock failed")
            return real_lock(fd, command, *args)

        with mock.patch("provider_fence.fcntl.lockf", side_effect=fail_only_unlock):
            unlock_problem = release_fence(closed)
        self.assertEqual(unlock_problem.errno, errno.EIO)
        self.assertTrue(closed.released)
        self.assertIsNone(provider_fence._active_held(other_config))
        reacquired = acquire_exclusive(other_config)
        self.assertIsInstance(reacquired, FenceHeld)
        self.assertIsNone(release_fence(reacquired))

    def test_quarantine_never_operates_on_a_reused_descriptor(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        config = prepare_disposable_fence(directory / "provider.fence")
        held = acquire_exclusive(config)
        number = held.descriptor
        replacement = directory / "replacement"
        calls = []
        real_close = os.close
        real_lock = provider_fence.fcntl.lockf
        real_fstat = os.fstat
        real_read = os.read
        real_lseek = os.lseek

        def trace_lock(fd, command, *args):
            calls.append(("lock", fd, command))
            if fd == number and command == provider_fence.fcntl.LOCK_UN:
                raise OSError(errno.EIO, "unlock failed")
            return real_lock(fd, command, *args)

        def ambiguous_close(fd):
            calls.append(("close", fd))
            if fd == number:
                real_close(fd)
                raise OSError(errno.EINTR, "close ambiguous")
            return real_close(fd)

        def trace_fstat(fd):
            calls.append(("fstat", fd))
            return real_fstat(fd)

        def trace_read(fd, size):
            calls.append(("read", fd))
            return real_read(fd, size)

        def trace_lseek(fd, offset, whence):
            calls.append(("lseek", fd))
            return real_lseek(fd, offset, whence)

        with (
            mock.patch("provider_fence.fcntl.lockf", side_effect=trace_lock),
            mock.patch("provider_fence.os.close", side_effect=ambiguous_close),
            mock.patch("provider_fence.os.fstat", side_effect=trace_fstat),
            mock.patch("provider_fence.os.read", side_effect=trace_read),
            mock.patch("provider_fence.os.lseek", side_effect=trace_lseek),
        ):
            problem = release_fence(held)
            self.assertEqual(problem.errno, errno.EIO)
            self.assertTrue(held.quarantined)
            self.assertFalse(held.released)
            descriptor = os.open(
                replacement,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            extras = []
            while descriptor != number and len(extras) < 64:
                extras.append(descriptor)
                descriptor = os.open(replacement, os.O_RDWR | os.O_NOFOLLOW)
            self.assertEqual(descriptor, number)
            real_lock(number, provider_fence.fcntl.LOCK_EX)
            calls.clear()
            self.assertIs(release_fence(held), problem)
            self.assertFalse(fence_still_matches(held))
            self.assertEqual(calls, [])
            for extra in extras:
                real_close(extra)
        child = subprocess.run(
            (
                sys.executable,
                "-B",
                "-c",
                _LOCK_PROBE,
                str(replacement),
            ),
            cwd=PROJECT_ROOT,
            env=_child_env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), "busy")
        os.open(config.path, os.O_RDONLY)
        os.close(number)

    def test_ambiguous_mutex_release_blocks_reacquisition(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        config = prepare_disposable_fence(directory / "provider.fence")
        held = acquire_exclusive(config)
        os.open(config.path, os.O_RDONLY)
        real_mutex = held.mutex

        class ReleaseThenRaise:
            def release(self):
                real_mutex.release()
                raise RuntimeError("mutex release ambiguous")

        held.mutex = ReleaseThenRaise()
        problem = release_fence(held)
        self.assertIsInstance(problem, RuntimeError)
        self.assertTrue(held.quarantined)
        self.assertFalse(held.released)
        self.assertIs(provider_fence._active_held(config), held)
        self.assertFalse(real_mutex.locked())
        self.assertIs(release_fence(held), problem)
        self.assertIsInstance(acquire_exclusive(config), FenceBusy)

        barrier = threading.Barrier(2)
        names = []

        def attempt():
            barrier.wait()
            names.append(type(acquire_exclusive(config)).__name__)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sorted(names), ["FenceBusy", "FenceBusy"])

        other = _directory()
        self.addCleanup(shutil.rmtree, other, True)
        other_config = prepare_disposable_fence(other / "provider.fence")
        other_held = acquire_exclusive(other_config)
        self.assertIsInstance(other_held, FenceHeld)
        self.assertIsNone(release_fence(other_held))

    def test_validation_finishes_before_release_can_close(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        config = prepare_disposable_fence(directory / "provider.fence")
        held = acquire_exclusive(config)
        observed = _ObservedLifecycle()
        held.lifecycle = observed
        order = []
        acquired = []
        real_fstat = os.fstat
        real_close = os.close
        entered = threading.Event()
        acquire_finished = threading.Event()
        watching = {"fstat": True, "close": True}

        def slow_fstat(fd):
            if not watching["fstat"] or fd != held.descriptor:
                return real_fstat(fd)
            watching["fstat"] = False
            order.append("fstat")
            entered.set()
            self.assertTrue(acquire_finished.wait(5))
            self.assertTrue(observed.contended.wait(5))
            self.assertNotIn("close", order)
            order.append("fstat-done")
            return real_fstat(fd)

        def trace_close(fd):
            if watching["close"] and fd == held.descriptor:
                watching["close"] = False
                order.append("close")
            return real_close(fd)

        def acquire_while_validating():
            self.assertTrue(entered.wait(5))
            acquired.append(type(acquire_exclusive(config)).__name__)
            acquire_finished.set()

        def release_after_acquire():
            self.assertTrue(acquire_finished.wait(5))
            self.assertIsNone(release_fence(held))

        acquire_thread = threading.Thread(target=acquire_while_validating)
        release_thread = threading.Thread(target=release_after_acquire)
        with (
            mock.patch("provider_fence.os.fstat", side_effect=slow_fstat),
            mock.patch("provider_fence.os.close", side_effect=trace_close),
        ):
            acquire_thread.start()
            release_thread.start()
            self.assertTrue(fence_still_matches(held))
            acquire_thread.join(5)
            release_thread.join(5)
        self.assertFalse(acquire_thread.is_alive())
        self.assertFalse(release_thread.is_alive())
        self.assertEqual(acquired, ["FenceBusy"])
        self.assertEqual(order, ["fstat", "fstat-done", "close"])
        self.assertTrue(held.released)
        again = acquire_exclusive(config)
        self.assertIsInstance(again, FenceHeld)
        self.assertTrue(fence_still_matches(again))
        self.assertIsNone(release_fence(again))

    def test_release_rejects_new_validation_before_close_finishes(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        config = prepare_disposable_fence(directory / "provider.fence")
        other = _directory()
        self.addCleanup(shutil.rmtree, other, True)
        other_config = prepare_disposable_fence(other / "provider.fence")
        held = acquire_exclusive(config)
        other_held = acquire_exclusive(other_config)
        observed = _ObservedLifecycle()
        held.lifecycle = observed
        order = []
        matched = []
        acquired = []
        real_fstat = os.fstat
        real_read = os.read
        real_lseek = os.lseek
        real_lock = provider_fence.fcntl.lockf
        in_unlock = threading.Event()
        allow_finish = threading.Event()

        def trace_lock(fd, command, *args):
            if fd == held.descriptor and command == provider_fence.fcntl.LOCK_UN:
                order.append("unlock")
                in_unlock.set()
                self.assertTrue(allow_finish.wait(5))
            return real_lock(fd, command, *args)

        def trace_fstat(fd):
            if fd == held.descriptor:
                order.append("fstat")
            return real_fstat(fd)

        def trace_read(fd, size):
            if fd == held.descriptor:
                order.append("read")
            return real_read(fd, size)

        def trace_lseek(fd, offset, whence):
            if fd == held.descriptor:
                order.append("lseek")
            return real_lseek(fd, offset, whence)

        def release():
            self.assertIsNone(release_fence(held))

        def validate():
            matched.append(fence_still_matches(held))

        def acquire_same():
            acquired.append(type(acquire_exclusive(config)).__name__)

        with (
            mock.patch("provider_fence.fcntl.lockf", side_effect=trace_lock),
            mock.patch("provider_fence.os.fstat", side_effect=trace_fstat),
            mock.patch("provider_fence.os.read", side_effect=trace_read),
            mock.patch("provider_fence.os.lseek", side_effect=trace_lseek),
        ):
            release_thread = threading.Thread(target=release)
            release_thread.start()
            self.assertTrue(in_unlock.wait(5))
            validation = threading.Thread(target=validate)
            validation.start()
            self.assertTrue(observed.contended.wait(5))
            self.assertNotIn("fstat", order)
            self.assertNotIn("read", order)
            self.assertNotIn("lseek", order)
            acquire_thread = threading.Thread(target=acquire_same)
            acquire_thread.start()
            acquire_thread.join(5)
            self.assertFalse(acquire_thread.is_alive())
            self.assertEqual(acquired, ["FenceBusy"])
            self.assertTrue(fence_still_matches(other_held))
            allow_finish.set()
            release_thread.join(5)
            validation.join(5)
        self.assertFalse(release_thread.is_alive())
        self.assertFalse(validation.is_alive())
        self.assertEqual(matched, [False])
        self.assertNotIn("fstat", order)
        self.assertNotIn("read", order)
        self.assertNotIn("lseek", order)
        self.assertIsNone(release_fence(other_held))
        again = acquire_exclusive(config)
        self.assertIsInstance(again, FenceHeld)
        self.assertTrue(fence_still_matches(again))
        self.assertIsNone(release_fence(again))

    def test_uncertain_close_quarantines_after_successful_unlock(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        config = prepare_disposable_fence(directory / "provider.fence")
        held = acquire_exclusive(config)
        number = held.descriptor
        replacement = directory / "replacement"
        calls = []
        real_close = os.close
        real_lock = provider_fence.fcntl.lockf
        real_fstat = os.fstat
        real_read = os.read
        real_lseek = os.lseek

        def trace_lock(fd, command, *args):
            calls.append(("lock", fd, command))
            return real_lock(fd, command, *args)

        def ambiguous_close(fd):
            calls.append(("close", fd))
            if fd == number:
                real_close(fd)
                raise OSError(errno.EINTR, "close ambiguous")
            return real_close(fd)

        def trace_fstat(fd):
            calls.append(("fstat", fd))
            return real_fstat(fd)

        def trace_read(fd, size):
            calls.append(("read", fd))
            return real_read(fd, size)

        def trace_lseek(fd, offset, whence):
            calls.append(("lseek", fd))
            return real_lseek(fd, offset, whence)

        with (
            mock.patch("provider_fence.fcntl.lockf", side_effect=trace_lock),
            mock.patch("provider_fence.os.close", side_effect=ambiguous_close),
            mock.patch("provider_fence.os.fstat", side_effect=trace_fstat),
            mock.patch("provider_fence.os.read", side_effect=trace_read),
            mock.patch("provider_fence.os.lseek", side_effect=trace_lseek),
        ):
            problem = release_fence(held)
            self.assertEqual(problem.errno, errno.EINTR)
            self.assertTrue(held.closure_uncertain)
            self.assertTrue(held.quarantined)
            self.assertFalse(held.released)
            self.assertTrue(held.mutex.locked())
            descriptor = os.open(
                replacement,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            extras = []
            while descriptor != number and len(extras) < 64:
                extras.append(descriptor)
                descriptor = os.open(replacement, os.O_RDWR | os.O_NOFOLLOW)
            self.assertEqual(descriptor, number)
            real_lock(number, provider_fence.fcntl.LOCK_EX)
            calls.clear()
            self.assertFalse(fence_still_matches(held))
            self.assertIs(release_fence(held), problem)
            self.assertEqual(calls, [])
            for extra in extras:
                real_close(extra)
        child = subprocess.run(
            (
                sys.executable,
                "-B",
                "-c",
                _LOCK_PROBE,
                str(replacement),
            ),
            cwd=PROJECT_ROOT,
            env=_child_env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), "busy")
        held.mutex.release()
        self.assertIsInstance(acquire_exclusive(config), FenceBusy)
        self.assertIs(provider_fence._active_held(config), held)
        os.open(config.path, os.O_RDONLY)
        os.close(number)

    def test_concurrent_releases_perform_one_unlock_and_close(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        config = prepare_disposable_fence(directory / "provider.fence")
        held = acquire_exclusive(config)
        number = held.descriptor
        calls = []
        errors = []
        results = []
        real_lock = provider_fence.fcntl.lockf
        real_close = os.close
        barrier = threading.Barrier(2)

        def trace_lock(fd, command, *args):
            if fd == number and command == provider_fence.fcntl.LOCK_UN:
                calls.append("unlock")
            return real_lock(fd, command, *args)

        def trace_close(fd):
            if fd == number:
                calls.append("close")
            return real_close(fd)

        def worker():
            try:
                barrier.wait()
                results.append(release_fence(held))
            except Exception as error:
                errors.append(error)

        with (
            mock.patch("provider_fence.fcntl.lockf", side_effect=trace_lock),
            mock.patch("provider_fence.os.close", side_effect=trace_close),
        ):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
        self.assertEqual(errors, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted(calls), ["close", "unlock"])
        self.assertEqual(results, [None, None])
        self.assertTrue(held.released)
        again = acquire_exclusive(config)
        self.assertIsInstance(again, FenceHeld)

        failed = again
        failed_number = failed.descriptor
        failed_calls = []
        failed_results = []
        failed_errors = []
        failed_barrier = threading.Barrier(2)

        def fail_lock(fd, command, *args):
            if fd == failed_number and command == provider_fence.fcntl.LOCK_UN:
                failed_calls.append("unlock")
                raise OSError(errno.EIO, "unlock failed")
            return real_lock(fd, command, *args)

        def fail_close(fd):
            if fd == failed_number:
                failed_calls.append("close")
                raise OSError(errno.EBADF, "close failed")
            return real_close(fd)

        def failed_worker():
            try:
                failed_barrier.wait()
                failed_results.append(release_fence(failed))
            except Exception as error:
                failed_errors.append(error)

        with (
            mock.patch("provider_fence.fcntl.lockf", side_effect=fail_lock),
            mock.patch("provider_fence.os.close", side_effect=fail_close),
        ):
            failed_threads = [
                threading.Thread(target=failed_worker) for _ in range(2)
            ]
            for thread in failed_threads:
                thread.start()
            for thread in failed_threads:
                thread.join(5)
            self.assertEqual(failed_errors, [])
            self.assertFalse(any(thread.is_alive() for thread in failed_threads))
            self.assertEqual(sorted(failed_calls), ["close", "unlock"])
            self.assertEqual(len(failed_results), 2)
            self.assertIs(failed_results[0], failed_results[1])
            self.assertEqual(failed_results[0].errno, errno.EIO)
            self.assertTrue(failed.quarantined)
            self.assertFalse(failed.released)
            self.assertIs(release_fence(failed), failed_results[0])
            self.assertEqual(sorted(failed_calls), ["close", "unlock"])
            self.assertIsInstance(acquire_exclusive(config), FenceBusy)
        os.open(config.path, os.O_RDONLY)
        other = _directory()
        self.addCleanup(shutil.rmtree, other, True)
        other_config = prepare_disposable_fence(other / "provider.fence")
        other_held = acquire_exclusive(other_config)
        self.assertIsInstance(other_held, FenceHeld)
        self.assertTrue(fence_still_matches(other_held))
        self.assertIsNone(release_fence(other_held))

    def test_distinct_lifecycle_locks_do_not_deadlock(self):
        first_directory = _directory()
        second_directory = _directory()
        self.addCleanup(shutil.rmtree, first_directory, True)
        self.addCleanup(shutil.rmtree, second_directory, True)
        first = prepare_disposable_fence(first_directory / "provider.fence")
        second = prepare_disposable_fence(second_directory / "provider.fence")
        held_first = acquire_exclusive(first)
        held_second = acquire_exclusive(second)
        entered = threading.Event()
        second_released = threading.Event()
        real_fstat = os.fstat
        errors = []

        def slow_fstat(fd):
            if fd != held_first.descriptor:
                return real_fstat(fd)
            entered.set()
            self.assertTrue(second_released.wait(5))
            return real_fstat(fd)

        def release_second():
            try:
                self.assertTrue(entered.wait(5))
                self.assertIsNone(release_fence(held_second))
                second_released.set()
            except Exception as error:
                errors.append(error)
                second_released.set()

        with mock.patch("provider_fence.os.fstat", side_effect=slow_fstat):
            thread = threading.Thread(target=release_second)
            thread.start()
            self.assertTrue(fence_still_matches(held_first))
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(held_second.released)
        self.assertIsNone(release_fence(held_first))
        restored = acquire_exclusive(second)
        self.assertIsInstance(restored, FenceHeld)
        self.assertIsNone(release_fence(restored))

    def test_ambiguous_acceptance_release_stays_fail_closed(self):
        _fence, pin_directory, completion = self._accepted_inputs()
        parsed = json.loads(completion.receipt.receipt_bytes.decode("ascii"))
        config = ProviderFenceConfig(
            parsed["fence_path"],
            parsed["fence_device"],
            parsed["fence_inode"],
        )
        closes = {"count": 0}
        locked = {"fd": None}
        tracked_configs = [config]
        real_close = os.close
        real_lock = provider_fence.fcntl.lockf

        def fail_fence_unlock(fd, command, *args):
            if command == (
                provider_fence.fcntl.LOCK_EX | provider_fence.fcntl.LOCK_NB
            ):
                locked["fd"] = fd
            if fd == locked["fd"] and command == provider_fence.fcntl.LOCK_UN:
                raise OSError(errno.EIO, "unlock failed")
            return real_lock(fd, command, *args)

        def fail_fence_close(fd):
            if fd == locked["fd"]:
                closes["count"] += 1
                raise OSError(errno.EBADF, "close failed")
            return real_close(fd)

        failed_config = None
        try:
            with (
                mock.patch("provider_fence.fcntl.lockf", side_effect=fail_fence_unlock),
                mock.patch("provider_fence.os.close", side_effect=fail_fence_close),
            ):
                with self.assertRaises(FenceSafetyError) as created:
                    accept_disposable_fence_receipt(
                        completion,
                        pin_directory,
                        _echo,
                    )
                self.assertEqual(created.exception.code, "fence_release_failed")
                self.assertEqual(created.exception.__cause__.errno, errno.EIO)
                self.assertTrue(any(
                    "descriptor close failed: errno %s" % errno.EBADF in note
                    for note in created.exception.__notes__
                ))
                self.assertIsInstance(acquire_exclusive(config), FenceBusy)
                held = provider_fence._active_held(config)
                self.assertIsInstance(held, FenceHeld)
                self.assertFalse(held.released)
                self.assertTrue(held.mutex.locked())
                self.assertEqual(closes["count"], 1)
                release_fence(held)
                self.assertEqual(closes["count"], 1)

                failed_fence, failed_pin, failed_completion = self._accepted_inputs()
                failed_parsed = json.loads(
                    failed_completion.receipt.receipt_bytes.decode("ascii")
                )
                failed_config = ProviderFenceConfig(
                    failed_parsed["fence_path"],
                    failed_parsed["fence_device"],
                    failed_parsed["fence_inode"],
                )
                tracked_configs.append(failed_config)
                real_write = os.write

                def short_write(descriptor, payload):
                    if (
                        isinstance(payload, bytes)
                        and _ACCEPTANCE_VERSION.encode("ascii") in payload
                    ):
                        real_write(descriptor, payload[:1])
                        return 1
                    return real_write(descriptor, payload)

                with mock.patch("provider_fence.os.write", side_effect=short_write):
                    with self.assertRaises(FenceSafetyError) as primary:
                        accept_disposable_fence_receipt(
                            failed_completion,
                            failed_pin,
                            _echo,
                        )
                self.assertEqual(
                    primary.exception.code,
                    "acceptance_file_create_failed",
                )
                self.assertTrue(any(
                    note.startswith("fence release failed: errno ")
                    for note in primary.exception.__notes__
                ))
                self.assertTrue(any(
                    "descriptor close failed: errno %s" % errno.EBADF in note
                    for note in primary.exception.__notes__
                ))
                self.assertIsInstance(acquire_exclusive(failed_config), FenceBusy)
                self._assert_revoked(failed_pin / _ACCEPTANCE_NAME)
        finally:
            for tracked in (
                provider_fence._active_held(item)
                for item in tracked_configs
            ):
                if tracked is None or tracked.released:
                    continue
                os.open(tracked.path, os.O_RDONLY)

    def test_confirmation_requires_an_exact_dictionary(self):
        _fence, pin_directory, completion = self._accepted_inputs()
        seen = {}

        class EqualAnything:
            def __eq__(self, other):
                return True

        class BindingSubclass(dict):
            pass

        def capture(binding):
            seen["binding"] = binding
            return EqualAnything()

        with self.assertRaises(FenceSafetyError) as adversarial:
            accept_disposable_fence_receipt(completion, pin_directory, capture)
        self.assertEqual(
            adversarial.exception.code,
            "acceptance_confirmation_refused",
        )
        binding = seen["binding"]
        rejected = [
            BindingSubclass(binding),
            {**binding, "extra": "no"},
            {key: value for key, value in binding.items() if key != "receipt_digest"},
            {**binding, "fence_inode": True},
            {**binding, "pin_device": str(binding["pin_device"])},
            {**binding, "receipt_id": "ab" * 16},
        ]

        def refuse(_binding):
            raise RuntimeError("confirmation failed")

        rejected.append(refuse)
        for decision in rejected:
            with self.assertRaises(FenceSafetyError) as caught:
                accept_disposable_fence_receipt(
                    completion,
                    pin_directory,
                    decision if callable(decision) else (lambda _binding, decision=decision: decision),
                )
            self.assertEqual(
                caught.exception.code,
                "acceptance_confirmation_refused",
            )
        self.assertFalse((pin_directory / _ACCEPTANCE_NAME).exists())

        def reenter(binding):
            with self.assertRaises(FenceSafetyError) as inner:
                accept_disposable_fence_receipt(completion, pin_directory, _echo)
            self.assertEqual(inner.exception.code, "fence_busy")
            self.assertFalse((pin_directory / _ACCEPTANCE_NAME).exists())
            return dict(binding)

        path = accept_disposable_fence_receipt(completion, pin_directory, reenter)
        self.assertTrue(path.endswith(_ACCEPTANCE_NAME))

    def test_acceptance_descriptor_identity_failures_stay_fail_closed(self):
        _fence, pin_directory, completion = self._accepted_inputs()
        real_fstat = os.fstat
        seen = {}

        def substituted(descriptor):
            current = real_fstat(descriptor)
            if (
                stat.S_ISREG(current.st_mode)
                and current.st_size
                and _is_acceptance(descriptor)
            ):
                seen["inode"] = current.st_ino
                return _foreign_inode(current)
            return current

        with mock.patch("provider_fence.os.fstat", side_effect=substituted):
            with self.assertRaises(FenceSafetyError) as validation:
                accept_disposable_fence_receipt(completion, pin_directory, _echo)
        acceptance = pin_directory / _ACCEPTANCE_NAME
        self.assertEqual(validation.exception.code, "acceptance_file_permissions")
        self.assertEqual(acceptance.lstat().st_ino, seen["inode"])
        self._assert_revoked(acceptance)

        _fence, pin_directory, completion, path = self._accept()
        before = Path(path).read_bytes()
        with mock.patch("provider_fence.os.fstat", side_effect=substituted):
            with self.assertRaises(FenceSafetyError) as listed:
                load_disposable_fence_acceptance(
                    path,
                    completion.receipt.receipt_bytes,
                )
        self.assertEqual(listed.exception.code, "acceptance_file_identity")
        self.assertEqual(Path(path).read_bytes(), before)

    def test_read_only_load_succeeds_while_exclusive_use_is_busy(self):
        _fence, pin_directory, completion, path = self._accept()
        receipt = completion.receipt.receipt_bytes
        config = load_disposable_fence_acceptance(path, receipt)
        publish_loaded_fence_acceptance(path, receipt)
        holder = self._hold(str(pin_directory / "provider-fence.pin"))
        loaded = load_disposable_fence_acceptance(path, receipt)
        self.assertEqual(loaded, config)
        self.assertIsInstance(acquire_exclusive(config), FenceBusy)
        self.assertFalse(acquire_worker_fence(config))
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=2)

    def test_publication_is_set_once(self):
        _fence, _pin, completion, path = self._accept()
        receipt = completion.receipt.receipt_bytes
        other = _directory()
        self.addCleanup(shutil.rmtree, other, True)
        from provider_fence import prepare_disposable_fence

        replacement = prepare_disposable_fence(other / "provider.fence")
        publish_shared_fence_config(replacement)
        with self.assertRaises(FenceSafetyError) as caught:
            publish_loaded_fence_acceptance(path, receipt)
        self.assertEqual(caught.exception.code, "fence_identity_changed")
        self.assertEqual(env_fence_config(), replacement)
        _drop_fence_env()
        first = publish_loaded_fence_acceptance(path, receipt)
        second = publish_loaded_fence_acceptance(path, receipt)
        self.assertEqual(first, second)
        self.assertEqual(env_fence_config(), first)
        self.assertNotEqual(first, replacement)

    def test_real_production_paths_are_not_opened(self):
        real_lstat = os.lstat
        real_stat = os.stat
        real_open = os.open
        calls = {"count": 0}

        def guard_lstat(path, *args, **kwargs):
            if _targets_approved_pin(path):
                raise AssertionError(path)
            return real_lstat(path, *args, **kwargs)

        def guard_stat(path, *args, **kwargs):
            if _targets_approved_pin(path):
                raise AssertionError(path)
            return real_stat(path, *args, **kwargs)

        def guard_open(path, flags, mode=0o777, *, dir_fd=None):
            if _targets_approved_pin(path):
                raise AssertionError(path)
            calls["count"] += 0
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with (
            mock.patch("provider_fence.os.lstat", side_effect=guard_lstat),
            mock.patch("provider_fence.os.stat", side_effect=guard_stat),
            mock.patch("provider_fence.os.open", side_effect=guard_open),
        ):
            with self.assertRaises(FenceSafetyError) as provisioned:
                provision_disposable_fence_receipt(
                    REAL_PIN_DIRECTORY,
                    REAL_PIN_DIRECTORY,
                )
            self.assertEqual(provisioned.exception.code, "production_fence_refused")
            with self.assertRaises(FenceSafetyError) as loaded:
                load_disposable_fence_acceptance(
                    str(REAL_PIN_DIRECTORY / _ACCEPTANCE_NAME),
                    b"{}\n",
                )
            self.assertEqual(loaded.exception.code, "production_fence_refused")

    def test_acceptance_is_not_wired_into_production_startup(self):
        markers = (
            "provision_disposable_fence_receipt",
            "accept_disposable_fence_receipt",
            "load_disposable_fence_acceptance",
            "publish_loaded_fence_acceptance",
            "provider-fence.acceptance",
        )
        for relative in (
            "src/max_smart_db/settings.py",
            "src/max_smart_db/startup.py",
            "src/max_smart_db/bootstrap.py",
            "src/max_smart_db/test_settings.py",
            "src/durable_composition.py",
            "src/provider_supervisor.py",
            "src/application.py",
        ):
            text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            for marker in markers:
                self.assertNotIn(marker, text, relative)
        source = (SRC_ROOT / "provider_fence.py").read_text(encoding="utf-8")
        for banned in ("os.unlink", "os.remove", "os.rename", "os.replace"):
            self.assertNotIn(banned, source)

    def test_fence_claim_go_clock_and_recovery_contracts_remain(self):
        _fence, _pin, completion, path = self._accept()
        config = publish_loaded_fence_acceptance(
            path,
            completion.receipt.receipt_bytes,
        )
        self.assertEqual(QUEUED_RESERVATION_US, 60_000_000)
        self.assertEqual(EXECUTION_DEADLINE_US, 600_000_000)
        self.assertEqual(FIXED_LEASE_US, 720_000_000)
        self.assertEqual(RECOVERY_CLOCK_SKEW_MARGIN_US, 120_000_000)
        spawned = []

        def reject_clock_popen(*_args, **_kwargs):
            spawned.append("clock")
            raise AssertionError("clock gate started a worker")

        refused = supervise(
            claimed_at=CLAIMED_AT,
            operation_id=OPERATION,
            runner_id=RUNNER,
            request_content="hello",
            standing_context="standing",
            wall_clock=lambda: CLAIMED_AT,
            fake="ok",
            fence_config=config,
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
            fence_config=config,
            prove_claim=lambda: False,
        )
        self.assertEqual(proved.classification, "definite_non_send")
        self.assertEqual(proved.calls, 0)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            completed = supervise(
                claimed_at=CLAIMED_AT,
                operation_id=OPERATION,
                runner_id=RUNNER,
                request_content="hello",
                standing_context="standing",
                wall_clock=lambda: CLAIMED_AT,
                fake="ok",
                fence_config=config,
                prove_claim=lambda: True,
            )
        self.assertEqual(completed.classification, "success")
        self.assertEqual(completed.output, "answer")
        self.assertEqual(completed.calls, 1)
        self.assertFalse(any(item.category is ResourceWarning for item in caught))
        self.assertEqual(env_fence_config(), config)

    def _fresh_pair(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        return fence_directory, pin_directory

    def _capture(self, token_bytes=None):
        fence_directory, pin_directory = self._fresh_pair()
        process = begin_disposable_fence_provision(
            fence_directory,
            pin_directory,
            token_bytes=token_bytes,
        )
        self.addCleanup(_finish_process, process)
        return complete_disposable_fence_provision(process)

    def _accepted_inputs(self):
        fence_directory, pin_directory = self._fresh_pair()
        process = begin_disposable_fence_provision(fence_directory, pin_directory)
        self.addCleanup(_finish_process, process)
        completion = complete_disposable_fence_provision(process)
        return fence_directory, pin_directory, completion

    def _accept(self):
        fence_directory, pin_directory, completion = self._accepted_inputs()
        path = accept_disposable_fence_receipt(completion, pin_directory, _echo)
        return fence_directory, pin_directory, completion, path

    def _assert_canonical_receipt(self, payload):
        self.assertIsInstance(payload, bytes)
        self.assertLessEqual(len(payload), 8192)
        self.assertEqual(payload.count(b"\n"), 1)
        self.assertTrue(payload.endswith(b"\n"))
        parsed = json.loads(payload.decode("ascii"))
        rendered = (
            json.dumps(
                parsed,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
        self.assertEqual(payload, rendered)
        self.assertEqual(parsed["version"], _RECEIPT_VERSION)
        self.assertEqual(parsed["result"], "provisioned")
        self.assertEqual(len(parsed["receipt_id"]), 32)
        self.assertEqual(len(parsed["pin_digest"]), 64)
        self.assertEqual(len(parsed["record_digest"]), 64)
        self.assertEqual(Path(parsed["fence_path"]).name, "provider.fence")
        self.assertEqual(Path(parsed["record_path"]).name, "fence.trust")
        self.assertEqual(Path(parsed["pin_path"]).name, "provider-fence.pin")
        self.assertEqual(
            Path(parsed["fence_path"]).parent,
            Path(parsed["record_path"]).parent,
        )
        self.assertNotEqual(
            Path(parsed["pin_path"]).parent,
            Path(parsed["fence_path"]).parent,
        )
        self.assertEqual(
            hashlib.sha256(Path(parsed["pin_path"]).read_bytes()).hexdigest(),
            parsed["pin_digest"],
        )
        self.assertEqual(
            hashlib.sha256(Path(parsed["record_path"]).read_bytes()).hexdigest(),
            parsed["record_digest"],
        )
        return parsed

    def _replace_identity(self, parsed, target):
        if target == "fence":
            replacement = _directory() / "provider.fence"
            self.addCleanup(shutil.rmtree, replacement.parent, True)
            _write_exclusive(replacement, b"max-smart-provider-fence-v1\n")
            os.replace(replacement, parsed["fence_path"])
            return
        path = Path(parsed["record_path"] if target == "record" else parsed["pin_path"])
        path.write_bytes(path.read_bytes() + b" ")

    def _restore_identity(self, parsed, target, record_before):
        if target == "record":
            Path(parsed["record_path"]).write_bytes(record_before)
            return
        if target == "pin":
            path = Path(parsed["pin_path"])
            payload = path.read_bytes()
            if payload.endswith(b" "):
                path.write_bytes(payload[:-1])

    def _assert_revoked(self, path):
        status = path.lstat()
        self.assertTrue(stat.S_ISREG(status.st_mode))
        self.assertEqual(status.st_size, 0)
        self.assertEqual(status.st_nlink, 1)
        self.assertFalse(stat.S_ISLNK(status.st_mode))

    def _assert_fsync_revocation(self, *, file_sync):
        _fence, pin_directory, completion = self._accepted_inputs()
        real_fsync = os.fsync
        real_fstat = os.fstat
        directory_inode = pin_directory.lstat().st_ino
        seen = {}

        def fail_fsync(descriptor):
            current = real_fstat(descriptor)
            if stat.S_ISREG(current.st_mode) and _is_acceptance(descriptor):
                seen["inode"] = current.st_ino
                if file_sync:
                    raise OSError(errno.EIO, "file fsync failed")
            if (
                not file_sync
                and stat.S_ISDIR(current.st_mode)
                and current.st_ino == directory_inode
            ):
                raise OSError(errno.EIO, "directory fsync failed")
            return real_fsync(descriptor)

        with mock.patch("provider_fence.os.fsync", side_effect=fail_fsync):
            with self.assertRaises(FenceSafetyError) as caught:
                accept_disposable_fence_receipt(completion, pin_directory, _echo)
        acceptance = pin_directory / _ACCEPTANCE_NAME
        self.assertEqual(caught.exception.code, "acceptance_file_fsync_failed")
        self.assertEqual(acceptance.lstat().st_ino, seen["inode"])
        self._assert_revoked(acceptance)

    def _assert_permit_released(self, completion):
        parsed = json.loads(completion.receipt.receipt_bytes.decode("ascii"))
        gate = acquire_exclusive(ProviderFenceConfig(
            parsed["fence_path"],
            parsed["fence_device"],
            parsed["fence_inode"],
        ))
        self.assertIsInstance(gate, FenceHeld)
        self.assertIsNone(release_fence(gate))

    def _worker_hold(self, pin_path):
        config = load_disposable_fence_pin(pin_path)
        environment = _child_env()
        environment["MAX_SMART_PROVIDER_FENCE"] = config.path
        environment["MAX_SMART_PROVIDER_FENCE_DEVICE"] = str(config.device)
        environment["MAX_SMART_PROVIDER_FENCE_INODE"] = str(config.inode)
        process = subprocess.Popen(
            (sys.executable, "-B", "-c", _WORKER_HOLDER, pin_path),
            cwd=PROJECT_ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(_finish_process, process)
        line = process.stdout.readline().strip()
        if line != "held":
            error = process.stderr.read()
            _finish_process(process)
            raise AssertionError(f"{line}\n{error}")
        return process

    def _hold(self, pin_path):
        process = subprocess.Popen(
            (sys.executable, "-B", "-c", _HOLDER, pin_path),
            cwd=PROJECT_ROOT,
            env=_child_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(_finish_process, process)
        line = process.stdout.readline().strip()
        if line != "held":
            error = process.stderr.read()
            _finish_process(process)
            raise AssertionError(f"{line}\n{error}")
        return process


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


def _write_exclusive(path, payload):
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fence_gate_name(pin_path):
    child = subprocess.run(
        (sys.executable, "-B", "-c", _GATE_CHILD, pin_path),
        cwd=PROJECT_ROOT,
        env=_child_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if child.returncode != 0:
        raise AssertionError(child.stderr)
    return child.stdout.strip()


def _foreign_inode(current):
    return os.stat_result((
        current.st_mode,
        current.st_ino + 1,
        current.st_dev,
        current.st_nlink,
        current.st_uid,
        current.st_gid,
        current.st_size,
        int(current.st_atime),
        int(current.st_mtime),
        int(current.st_ctime),
    ))


def _is_acceptance(descriptor):
    offset = os.lseek(descriptor, 0, os.SEEK_CUR)
    os.lseek(descriptor, 0, os.SEEK_SET)
    payload = os.read(descriptor, 8192)
    os.lseek(descriptor, offset, os.SEEK_SET)
    return _ACCEPTANCE_VERSION.encode("ascii") in payload


def payload_with_space(receipt):
    parsed = json.loads(receipt.decode("ascii"))
    body = {
        "fence_device": parsed["fence_device"],
        "fence_inode": parsed["fence_inode"],
        "fence_path": parsed["fence_path"],
        "pin_device": parsed["pin_device"],
        "pin_digest": parsed["pin_digest"],
        "pin_inode": parsed["pin_inode"],
        "pin_path": parsed["pin_path"],
        "receipt_digest": hashlib.sha256(receipt).hexdigest(),
        "receipt_id": parsed["receipt_id"],
        "receipt_version": _RECEIPT_VERSION,
        "record_device": parsed["record_device"],
        "record_digest": parsed["record_digest"],
        "record_inode": parsed["record_inode"],
        "record_path": parsed["record_path"],
        "version": _ACCEPTANCE_VERSION,
    }
    return (json.dumps(body, indent=2) + "\n").encode("ascii")


def payload_with_boolean(receipt):
    parsed = json.loads(receipt.decode("ascii"))
    body = {
        "fence_device": parsed["fence_device"],
        "fence_inode": True,
        "fence_path": parsed["fence_path"],
        "pin_device": parsed["pin_device"],
        "pin_digest": parsed["pin_digest"],
        "pin_inode": parsed["pin_inode"],
        "pin_path": parsed["pin_path"],
        "receipt_digest": hashlib.sha256(receipt).hexdigest(),
        "receipt_id": parsed["receipt_id"],
        "receipt_version": _RECEIPT_VERSION,
        "record_device": parsed["record_device"],
        "record_digest": parsed["record_digest"],
        "record_inode": parsed["record_inode"],
        "record_path": parsed["record_path"],
        "version": _ACCEPTANCE_VERSION,
    }
    return (
        json.dumps(body, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")


_LOCK_PROBE = """\
import fcntl
import os
import sys
descriptor = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.stdout.write("busy")
else:
    sys.stdout.write("acquired")
"""


_GATE_CHILD = """\
import sys
from provider_fence import acquire_exclusive, load_disposable_fence_pin, release_fence
config = load_disposable_fence_pin(sys.argv[1])
gate = acquire_exclusive(config)
sys.stdout.write(type(gate).__name__)
if type(gate).__name__ == "FenceHeld":
    release_fence(gate)
"""


_WORKER_HOLDER = """\
import sys
from provider_fence import acquire_worker_fence, load_disposable_fence_pin
config = load_disposable_fence_pin(sys.argv[1])
if not acquire_worker_fence(config):
    sys.stdout.write("fail\\n")
    sys.stdout.flush()
    sys.exit(1)
sys.stdout.write("held\\n")
sys.stdout.flush()
sys.stdin.readline()
"""


_HOLDER = """\
import sys
from provider_fence import acquire_exclusive, load_disposable_fence_pin
config = load_disposable_fence_pin(sys.argv[1])
held = acquire_exclusive(config)
if type(held).__name__ != "FenceHeld":
    sys.stdout.write("fail\\n")
    sys.stdout.flush()
    sys.exit(1)
sys.stdout.write("held\\n")
sys.stdout.flush()
sys.stdin.readline()
"""


_LOAD_CHILD = """\
import sys
from provider_fence import load_disposable_fence_acceptance
config = load_disposable_fence_acceptance(sys.argv[1], bytes.fromhex(sys.argv[2]))
sys.stdout.write(config.path + " " + str(config.inode))
"""


if __name__ == "__main__":
    unittest.main()
