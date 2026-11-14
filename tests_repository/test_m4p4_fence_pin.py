"""Disposable pin-file tests for independent fence identity."""

import errno
import hashlib
import json
import os
import shutil
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
PRODUCTION_DATABASE = PROJECT_ROOT / "data" / "max_smart.sqlite3"
PRODUCTION_FENCE = PROJECT_ROOT / "data" / "provider.fence"
PRODUCTION_RECORD = PROJECT_ROOT / "data" / "fence.trust"
REAL_PIN_DIRECTORY = Path(
    "/Users/smart/Library/Application Support/MaxSmartTrust"
)
REAL_PIN = REAL_PIN_DIRECTORY / "provider-fence.pin"
SESSION_FILE = PROJECT_ROOT / "data" / "current_session.json"
sys.path.insert(0, str(SRC_ROOT))

from max_smart_db.config import (  # noqa: E402
    EXECUTION_DEADLINE_US,
    FIXED_LEASE_US,
    RECOVERY_CLOCK_SKEW_MARGIN_US,
)
import provider_fence  # noqa: E402
from provider_fence import (  # noqa: E402
    FenceBusy,
    FenceSafetyError,
    ProviderFenceConfig,
    acquire_exclusive,
    env_fence_config,
    fence_config_from_pin,
    load_disposable_fence_pin,
    load_disposable_fence_trust,
    prepare_disposable_fence,
    provision_disposable_fence_pin,
    publish_loaded_fence_pin,
    publish_shared_fence_config,
)
from provider_supervisor import supervise  # noqa: E402


_FENCE_ENV = (
    "MAX_SMART_PROVIDER_FENCE",
    "MAX_SMART_PROVIDER_FENCE_DEVICE",
    "MAX_SMART_PROVIDER_FENCE_INODE",
)
_PIN_VERSION = "max-smart-provider-fence-pin-v1"
_TRUST_VERSION = "max-smart-provider-fence-trust-v1"
CLAIMED_AT = 1_000_000
RUNNER = "AAAAAAAAAAAAAAAAAAAAAA"
OPERATION = "11111111-1111-1111-1111-111111111111"


def _targets_approved_pin(path):
    try:
        text = os.fspath(path)
    except TypeError:
        return False
    if type(text) is not str or text == "":
        return False
    if text == "MaxSmartTrust":
        return True
    approved = str(REAL_PIN_DIRECTORY)
    absolute = os.path.abspath(text)
    return absolute == approved or absolute.startswith(approved + os.sep)


def _is_pin_descriptor(descriptor, status):
    if status.st_size <= 0:
        return False
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        preview = os.read(descriptor, min(status.st_size, 8192))
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)
    return b"max-smart-provider-fence-pin-v1" in preview


def _drop_fence_env():
    for name in _FENCE_ENV:
        os.environ.pop(name, None)


def _directory():
    directory = Path(tempfile.mkdtemp(prefix="m4p4-fence-pin-")).resolve()
    os.chmod(directory, 0o700)
    return directory


def _pair():
    return _directory(), _directory()


def _child_env():
    environment = {**os.environ, "PYTHONPATH": str(SRC_ROOT)}
    for name in _FENCE_ENV:
        environment.pop(name, None)
    return environment


def _run_child(script, pin_path, *, timeout=30):
    return subprocess.run(
        (sys.executable, "-B", "-c", script, pin_path),
        cwd=PROJECT_ROOT,
        env=_child_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


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


def _trust_payload(path, device, inode):
    return (
        json.dumps(
            {
                "device": device,
                "inode": inode,
                "path": path,
                "version": _TRUST_VERSION,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _pin_payload(record_path, device, inode, digest):
    return (
        json.dumps(
            {
                "device": device,
                "digest": digest,
                "inode": inode,
                "path": record_path,
                "version": _PIN_VERSION,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


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


class FencePinTests(unittest.TestCase):
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
        for path, label in (
            (PRODUCTION_DATABASE, "production database"),
            (PRODUCTION_FENCE, "production fence"),
            (PRODUCTION_RECORD, "production trust record"),
        ):
            if path.exists():
                raise AssertionError(f"{label} created")

    def test_provision_writes_the_original_pin_outside_the_fence_directory(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        created = provision_disposable_fence_pin(fence_directory, pin_directory)
        pin_file = Path(created.pin_path)
        record = Path(created.pin.path)
        pin_status = pin_file.lstat()
        payload = pin_file.read_bytes()
        self.assertEqual(pin_file.name, "provider-fence.pin")
        self.assertEqual(pin_file.parent, pin_directory)
        self.assertNotEqual(pin_directory, fence_directory)
        self.assertEqual(created.fence.path, str(fence_directory / "provider.fence"))
        self.assertEqual(created.pin.path, str(fence_directory / "fence.trust"))
        self.assertNotEqual(created.pin.inode, created.fence.inode)
        self.assertNotEqual(pin_status.st_ino, created.pin.inode)
        self.assertEqual(stat.S_IMODE(pin_directory.lstat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(pin_status.st_mode), 0o600)
        self.assertEqual(pin_status.st_nlink, 1)
        self.assertEqual(pin_status.st_uid, os.geteuid())
        self.assertFalse(stat.S_ISLNK(pin_status.st_mode))
        self.assertEqual(payload, _pin_payload(
            created.pin.path,
            created.pin.device,
            created.pin.inode,
            created.pin.digest,
        ))
        self.assertNotEqual(hashlib.sha256(payload).hexdigest(), created.pin.digest)
        self.assertNotIn(created.pin.digest.encode("ascii"), record.read_bytes())
        self.assertIsNone(env_fence_config())
        loaded = load_disposable_fence_pin(created.pin_path)
        self.assertEqual(loaded, created.fence)
        self.assertNotEqual(loaded.inode, pin_status.st_ino)

    def test_same_and_nested_directories_are_refused(self):
        directory = _directory()
        self.addCleanup(shutil.rmtree, directory, True)
        nested = directory / "pin"
        nested.mkdir(mode=0o700)
        with self.assertRaises(FenceSafetyError) as same:
            provision_disposable_fence_pin(directory, directory)
        self.assertEqual(same.exception.code, "pin_directory_invalid")
        self.assertIsNone(same.exception.partial)
        with self.assertRaises(FenceSafetyError) as child:
            provision_disposable_fence_pin(directory, nested)
        self.assertEqual(child.exception.code, "pin_directory_invalid")
        self.assertFalse((directory / "provider.fence").exists())
        self.assertFalse((directory / "fence.trust").exists())
        self.assertFalse((nested / "provider-fence.pin").exists())

    def test_parents_are_not_created_or_repaired(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        missing = fence_directory / "missing"
        with self.assertRaises(FenceSafetyError) as absent:
            provision_disposable_fence_pin(missing, pin_directory)
        self.assertEqual(absent.exception.code, "fence_directory_missing")
        self.assertFalse(missing.exists())
        os.chmod(pin_directory, 0o755)
        with self.assertRaises(FenceSafetyError) as loose:
            provision_disposable_fence_pin(fence_directory, pin_directory)
        self.assertEqual(loose.exception.code, "fence_directory_permissions")
        self.assertEqual(stat.S_IMODE(pin_directory.lstat().st_mode), 0o755)
        self.assertFalse((fence_directory / "provider.fence").exists())

    def test_existing_files_are_not_adopted(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        _write_exclusive(pin_directory / "provider-fence.pin", b"already\n")
        with self.assertRaises(FenceSafetyError) as existing_pin:
            provision_disposable_fence_pin(fence_directory, pin_directory)
        self.assertEqual(existing_pin.exception.code, "pin_file_exists")
        self.assertIsNone(existing_pin.exception.partial)
        self.assertFalse((fence_directory / "provider.fence").exists())
        self.assertEqual(
            (pin_directory / "provider-fence.pin").read_bytes(),
            b"already\n",
        )
        prepare_disposable_fence(fence_directory / "provider.fence")
        before = (fence_directory / "provider.fence").read_bytes()
        fresh_pin = _directory()
        self.addCleanup(shutil.rmtree, fresh_pin, True)
        with self.assertRaises(FenceSafetyError) as existing_fence:
            provision_disposable_fence_pin(fence_directory, fresh_pin)
        self.assertEqual(existing_fence.exception.code, "fence_exists")
        self.assertEqual((fence_directory / "provider.fence").read_bytes(), before)
        self.assertFalse((fence_directory / "fence.trust").exists())

    def test_production_and_real_pin_paths_are_refused(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        with self.assertRaises(FenceSafetyError) as production:
            provision_disposable_fence_pin(PROJECT_ROOT / "data", pin_directory)
        self.assertEqual(production.exception.code, "production_fence_refused")
        real_open_parent = provider_fence._open_parent
        real_open = os.open
        real_lstat = os.lstat
        real_stat = os.stat

        def guard(real):
            def wrapped(path, *args, **kwargs):
                if _targets_approved_pin(path):
                    raise AssertionError("approved pin path was traversed")
                return real(path, *args, **kwargs)

            return wrapped

        with (
            mock.patch("provider_fence._open_parent", side_effect=guard(real_open_parent)),
            mock.patch("provider_fence.os.open", side_effect=guard(real_open)),
            mock.patch("provider_fence.os.lstat", side_effect=guard(real_lstat)),
            mock.patch("provider_fence.os.stat", side_effect=guard(real_stat)),
        ):
            with self.assertRaises(FenceSafetyError) as real_pin:
                provision_disposable_fence_pin(
                    fence_directory,
                    REAL_PIN_DIRECTORY,
                )
            self.assertEqual(real_pin.exception.code, "production_fence_refused")
            with self.assertRaises(FenceSafetyError) as real_root:
                provision_disposable_fence_pin(
                    REAL_PIN_DIRECTORY,
                    pin_directory,
                )
            self.assertEqual(real_root.exception.code, "production_fence_refused")
            with self.assertRaises(FenceSafetyError) as real_file:
                load_disposable_fence_pin(str(REAL_PIN))
            self.assertEqual(real_file.exception.code, "production_fence_refused")
        with self.assertRaises(FenceSafetyError) as cloud:
            provision_disposable_fence_pin(
                "/tmp/Dropbox/max-smart-pin",
                pin_directory,
            )
        self.assertEqual(cloud.exception.code, "fence_path_refused")
        self.assertFalse(PRODUCTION_FENCE.exists())
        self.assertFalse(PRODUCTION_RECORD.exists())
        self.assertFalse((fence_directory / "provider.fence").exists())
        source = Path(__file__).read_text(encoding="utf-8")
        self.assertNotIn("REAL_PIN_DIRECTORY" + ".exists", source)
        self.assertNotIn("REAL_PIN" + ".exists", source)

    def test_partial_record_failure_is_left_in_place(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        real_open = os.open

        def fail_record(path, flags, mode=0o777, *, dir_fd=None):
            if path == "fence.trust" and flags & os.O_CREAT:
                raise OSError(errno.EIO, "record create failed")
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with mock.patch("provider_fence.os.open", side_effect=fail_record):
            with self.assertRaises(FenceSafetyError) as caught:
                provision_disposable_fence_pin(fence_directory, pin_directory)
        self.assertEqual(caught.exception.code, "trust_record_create_failed")
        self.assertEqual(caught.exception.partial, "fence_only")
        self.assertTrue((fence_directory / "provider.fence").is_file())
        self.assertFalse((fence_directory / "fence.trust").exists())
        self.assertFalse((pin_directory / "provider-fence.pin").exists())
        with self.assertRaises(FenceSafetyError) as retry:
            provision_disposable_fence_pin(fence_directory, pin_directory)
        self.assertEqual(retry.exception.code, "fence_exists")
        self.assertIsNone(retry.exception.partial)
        self.assertFalse((fence_directory / "fence.trust").exists())

    def test_partial_pin_failure_is_left_in_place(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        real_write = os.write

        def fail_pin(descriptor, payload):
            if (
                isinstance(payload, bytes)
                and b"max-smart-provider-fence-pin-v1" in payload
            ):
                raise OSError(errno.EIO, "pin write failed")
            return real_write(descriptor, payload)

        with mock.patch("provider_fence.os.write", side_effect=fail_pin):
            with self.assertRaises(FenceSafetyError) as caught:
                provision_disposable_fence_pin(fence_directory, pin_directory)
        self.assertEqual(caught.exception.code, "pin_file_create_failed")
        self.assertEqual(caught.exception.partial, "fence_record_and_pin")
        record = (fence_directory / "fence.trust").read_bytes()
        pin_file = pin_directory / "provider-fence.pin"
        self.assertTrue(pin_file.is_file())
        self.assertEqual(pin_file.read_bytes(), b"")
        with self.assertRaises(FenceSafetyError) as retry:
            provision_disposable_fence_pin(fence_directory, pin_directory)
        self.assertEqual(retry.exception.code, "trust_record_exists")
        self.assertEqual((fence_directory / "fence.trust").read_bytes(), record)
        self.assertEqual(pin_file.read_bytes(), b"")

    def test_independent_processes_load_the_same_pin_file(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        created = provision_disposable_fence_pin(fence_directory, pin_directory)
        before = Path(created.pin_path).read_bytes()
        first = _run_child(_AGREE_CHILD, created.pin_path)
        second = _run_child(_AGREE_CHILD, created.pin_path)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        left = json.loads(first.stdout)
        right = json.loads(second.stdout)
        self.assertEqual(left, right)
        self.assertEqual(left["path"], created.fence.path)
        self.assertEqual(left["device"], created.fence.device)
        self.assertEqual(left["inode"], created.fence.inode)
        self.assertTrue(left["same"])
        self.assertFalse(left["inherited"])
        self.assertEqual(left["env_inode"], str(created.fence.inode))
        self.assertEqual(Path(created.pin_path).read_bytes(), before)
        self.assertIsNone(env_fence_config())

    def test_publish_is_set_once_and_explicit_root_cannot_override(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        created = provision_disposable_fence_pin(fence_directory, pin_directory)
        other = _directory()
        self.addCleanup(shutil.rmtree, other, True)
        decoy = prepare_disposable_fence(other / "provider.fence")
        first = publish_loaded_fence_pin(created.pin_path)
        second = publish_loaded_fence_pin(created.pin_path)
        self.assertEqual(first, second)
        self.assertEqual(first, created.fence)
        with self.assertRaises(FenceSafetyError) as explicit:
            fence_config_from_pin(created.pin_path, decoy)
        self.assertEqual(explicit.exception.code, "fence_identity_changed")
        self.assertEqual(env_fence_config(), created.fence)
        _drop_fence_env()
        publish_shared_fence_config(decoy)
        with self.assertRaises(FenceSafetyError) as conflict:
            publish_loaded_fence_pin(created.pin_path)
        self.assertEqual(conflict.exception.code, "fence_identity_changed")
        self.assertEqual(env_fence_config(), decoy)

    def test_malformed_pin_files_fail_closed(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        created = provision_disposable_fence_pin(fence_directory, pin_directory)
        original = Path(created.pin_path).read_bytes()
        samples = {
            "truncated": (b"{", "pin_file_truncated"),
            "empty": (b"", "pin_file_truncated"),
            "missing newline": (original[:-1], "pin_file_truncated"),
            "extra key": (
                original.replace(b"}", b',"extra":1}'),
                "pin_file_malformed",
            ),
            "wrong version": (
                original.replace(
                    b"max-smart-provider-fence-pin-v1",
                    b"max-smart-provider-fence-pin-v2",
                ),
                "pin_file_malformed",
            ),
            "spaced json": (original.replace(b":", b": "), "pin_file_malformed"),
            "boolean device": (
                original.replace(b'"device":', b'"device":true,'),
                "pin_file_malformed",
            ),
            "zero device": (
                _pin_payload(created.pin.path, 0, created.pin.inode, created.pin.digest),
                "pin_file_malformed",
            ),
            "negative inode": (
                _pin_payload(
                    created.pin.path,
                    created.pin.device,
                    -1,
                    created.pin.digest,
                ),
                "pin_file_malformed",
            ),
            "uppercase digest": (
                _pin_payload(
                    created.pin.path,
                    created.pin.device,
                    created.pin.inode,
                    created.pin.digest.upper(),
                ),
                "pin_file_malformed",
            ),
            "short digest": (
                _pin_payload(
                    created.pin.path,
                    created.pin.device,
                    created.pin.inode,
                    "ab",
                ),
                "pin_file_malformed",
            ),
            "relative record path": (
                _pin_payload(
                    "fence.trust",
                    created.pin.device,
                    created.pin.inode,
                    created.pin.digest,
                ),
                "pin_file_malformed",
            ),
            "wrong record name": (
                _pin_payload(
                    str(fence_directory / "provider.fence"),
                    created.pin.device,
                    created.pin.inode,
                    created.pin.digest,
                ),
                "pin_file_malformed",
            ),
            "wrong digest": (
                _pin_payload(
                    created.pin.path,
                    created.pin.device,
                    created.pin.inode,
                    "0" * 64,
                ),
                "trust_record_digest",
            ),
            "boolean inode": (
                _pin_payload(
                    created.pin.path,
                    created.pin.device,
                    created.pin.inode,
                    created.pin.digest,
                ).replace(
                    b'"inode":' + str(created.pin.inode).encode("ascii"),
                    b'"inode":true',
                ),
                "pin_file_malformed",
            ),
        }
        # The boolean sample above is not valid JSON after a naive replace.
        samples["boolean device"] = (
            _pin_payload(
                created.pin.path,
                created.pin.device,
                created.pin.inode,
                created.pin.digest,
            ).replace(b'"device":' + str(created.pin.device).encode("ascii"), b'"device":true'),
            "pin_file_malformed",
        )
        samples["float device"] = (
            _pin_payload(
                created.pin.path,
                created.pin.device,
                created.pin.inode,
                created.pin.digest,
            ).replace(
                b'"device":' + str(created.pin.device).encode("ascii"),
                b'"device":1.5',
            ),
            "pin_file_malformed",
        )
        for name, (payload, code) in samples.items():
            with self.subTest(name=name):
                self._replace_pin(created.pin_path, payload)
                with self.assertRaises(FenceSafetyError) as caught:
                    load_disposable_fence_pin(created.pin_path)
                self.assertEqual(caught.exception.code, code)
                self.assertIsNone(env_fence_config())
        self._replace_pin(created.pin_path, b"x" * 8193)
        with self.assertRaises(FenceSafetyError) as oversized:
            load_disposable_fence_pin(created.pin_path)
        self.assertEqual(oversized.exception.code, "pin_file_malformed")
        missing = pin_directory / "absent" / "provider-fence.pin"
        with self.assertRaises(FenceSafetyError) as missing_parent:
            load_disposable_fence_pin(str(missing))
        self.assertIn(
            missing_parent.exception.code,
            {"fence_directory_missing", "fence_symlink", "pin_file_missing"},
        )
        absent = str(pin_directory / "provider-fence.pin")
        self._replace_pin(created.pin_path, original)
        os.unlink(created.pin_path)
        with self.assertRaises(FenceSafetyError) as missing_file:
            load_disposable_fence_pin(absent)
        self.assertEqual(missing_file.exception.code, "pin_file_missing")
        self.assertFalse(Path(absent).exists())

    def test_symlink_owner_mode_and_hard_link_are_refused(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        created = provision_disposable_fence_pin(fence_directory, pin_directory)
        original = Path(created.pin_path).read_bytes()
        linked = pin_directory / "linked-parent"
        linked.symlink_to(pin_directory, target_is_directory=True)
        with self.assertRaises(FenceSafetyError) as ancestor:
            load_disposable_fence_pin(str(linked / "provider-fence.pin"))
        self.assertEqual(ancestor.exception.code, "fence_symlink")
        moved = pin_directory / "provider-fence.saved"
        os.replace(created.pin_path, moved)
        final = Path(created.pin_path)
        final.symlink_to(moved)
        with self.assertRaises(FenceSafetyError) as symlink:
            load_disposable_fence_pin(str(final))
        self.assertEqual(symlink.exception.code, "pin_file_symlink")
        final.unlink()
        os.replace(moved, final)
        self.assertEqual(final.read_bytes(), original)
        os.chmod(final, 0o644)
        with self.assertRaises(FenceSafetyError) as permissions:
            load_disposable_fence_pin(created.pin_path)
        self.assertEqual(permissions.exception.code, "pin_file_permissions")
        os.chmod(final, 0o600)
        extra = pin_directory / "extra-link"
        os.link(final, extra)
        self.addCleanup(extra.unlink, missing_ok=True)
        with self.assertRaises(FenceSafetyError) as linked_file:
            load_disposable_fence_pin(created.pin_path)
        self.assertEqual(linked_file.exception.code, "pin_file_hard_link")
        extra.unlink()
        real_fstat = os.fstat
        pin_inode = final.lstat().st_ino

        def changed(descriptor):
            current = real_fstat(descriptor)
            if current.st_ino != pin_inode:
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
            with self.assertRaises(FenceSafetyError) as owner:
                load_disposable_fence_pin(created.pin_path)
        self.assertEqual(owner.exception.code, "pin_file_owner")
        self.assertEqual(final.read_bytes(), original)

    def test_pin_replacement_between_stat_and_open_is_refused(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        created = provision_disposable_fence_pin(fence_directory, pin_directory)
        original = Path(created.pin_path).read_bytes()
        decoy = pin_directory / "decoy-pin"
        _write_exclusive(decoy, b"replacement\n")
        real_stat = os.stat
        swapped = {"done": False}

        def swap_after_stat(path, *args, **kwargs):
            current = real_stat(path, *args, **kwargs)
            if path == "provider-fence.pin" and not swapped["done"]:
                swapped["done"] = True
                os.replace(decoy, created.pin_path)
            return current

        with mock.patch("provider_fence.os.stat", side_effect=swap_after_stat):
            with self.assertRaises(FenceSafetyError) as caught:
                load_disposable_fence_pin(created.pin_path)
        self.assertEqual(caught.exception.code, "pin_file_identity")
        self.assertTrue(swapped["done"])
        self.assertEqual(Path(created.pin_path).read_bytes(), b"replacement\n")
        self.assertNotEqual(Path(created.pin_path).read_bytes(), original)
        self.assertIsNone(env_fence_config())

    def test_fence_replacement_keeps_the_original_pin(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        created = provision_disposable_fence_pin(fence_directory, pin_directory)
        other = _directory()
        self.addCleanup(shutil.rmtree, other, True)
        replacement = prepare_disposable_fence(other / "provider.fence")
        pin_before = Path(created.pin_path).read_bytes()
        record_before = Path(created.pin.path).read_bytes()
        os.replace(replacement.path, created.fence.path)
        with self.assertRaises(FenceSafetyError) as caught:
            load_disposable_fence_pin(created.pin_path)
        self.assertEqual(caught.exception.code, "fence_identity")
        self.assertEqual(Path(created.pin_path).read_bytes(), pin_before)
        self.assertEqual(Path(created.pin.path).read_bytes(), record_before)
        self.assertNotEqual(Path(created.fence.path).stat().st_ino, created.fence.inode)

    def test_record_rewrite_and_replacement_keep_the_original_pin(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        created = provision_disposable_fence_pin(fence_directory, pin_directory)
        pin_before = Path(created.pin_path).read_bytes()
        rewritten = _trust_payload(
            created.fence.path,
            created.fence.device,
            created.fence.inode + 1,
        )
        descriptor = os.open(
            created.pin.path,
            os.O_RDWR | os.O_NOFOLLOW,
        )
        try:
            os.ftruncate(descriptor, 0)
            os.write(descriptor, rewritten)
            os.fsync(descriptor)
            rewritten_inode = os.fstat(descriptor).st_ino
        finally:
            os.close(descriptor)
        self.assertEqual(rewritten_inode, created.pin.inode)
        with self.assertRaises(FenceSafetyError) as digest:
            load_disposable_fence_pin(created.pin_path)
        self.assertEqual(digest.exception.code, "trust_record_digest")
        self.assertEqual(Path(created.pin_path).read_bytes(), pin_before)
        other_fence, other_pin = _pair()
        self.addCleanup(shutil.rmtree, other_fence, True)
        self.addCleanup(shutil.rmtree, other_pin, True)
        forged = provision_disposable_fence_pin(other_fence, other_pin)
        os.replace(forged.fence.path, created.fence.path)
        os.replace(forged.pin.path, created.pin.path)
        with self.assertRaises(FenceSafetyError) as replaced:
            load_disposable_fence_pin(created.pin_path)
        self.assertEqual(replaced.exception.code, "trust_record_identity")
        self.assertEqual(Path(created.pin_path).read_bytes(), pin_before)

    def test_same_user_replaced_chain_is_a_new_pin_file(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        original = provision_disposable_fence_pin(fence_directory, pin_directory)
        other_fence, other_pin = _pair()
        self.addCleanup(shutil.rmtree, other_fence, True)
        self.addCleanup(shutil.rmtree, other_pin, True)
        forged = provision_disposable_fence_pin(other_fence, other_pin)
        os.replace(forged.fence.path, original.fence.path)
        live = Path(original.fence.path).stat()
        record_payload = _trust_payload(
            original.fence.path,
            live.st_dev,
            live.st_ino,
        )
        record_file = other_fence / "rewritten.trust"
        _write_exclusive(record_file, record_payload)
        os.replace(record_file, original.pin.path)
        record_status = Path(original.pin.path).stat()
        pin_payload = _pin_payload(
            original.pin.path,
            record_status.st_dev,
            record_status.st_ino,
            hashlib.sha256(record_payload).hexdigest(),
        )
        pin_file = other_pin / "rewritten.pin"
        _write_exclusive(pin_file, pin_payload)
        os.replace(pin_file, original.pin_path)
        with self.assertRaises(FenceSafetyError) as old_pin:
            load_disposable_fence_trust(original.pin)
        self.assertEqual(old_pin.exception.code, "trust_record_identity")
        loaded = load_disposable_fence_pin(original.pin_path)
        self.assertEqual(loaded.path, original.fence.path)
        self.assertEqual(loaded.device, live.st_dev)
        self.assertEqual(loaded.inode, live.st_ino)
        self.assertNotEqual(loaded.inode, original.fence.inode)

    def test_concurrent_readers_do_not_modify_the_pin(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        created = provision_disposable_fence_pin(fence_directory, pin_directory)
        before = Path(created.pin_path).stat()
        record_before = Path(created.pin.path).stat()
        names = sorted(path.name for path in pin_directory.iterdir())
        results = []

        def read_once():
            results.append(load_disposable_fence_pin(created.pin_path))

        threads = [threading.Thread(target=read_once) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        processes = [
            _run_child(_READ_CHILD, created.pin_path),
            _run_child(_READ_CHILD, created.pin_path),
        ]
        self.assertTrue(all(thread.is_alive() is False for thread in threads))
        self.assertEqual(results, [created.fence] * 4)
        self.assertTrue(all(process.returncode == 0 for process in processes))
        after = Path(created.pin_path).stat()
        record_after = Path(created.pin.path).stat()
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(record_after.st_ino, record_before.st_ino)
        self.assertEqual(record_after.st_mtime_ns, record_before.st_mtime_ns)
        self.assertEqual(
            sorted(path.name for path in pin_directory.iterdir()),
            names,
        )

    def test_failed_pin_blocks_provider_dispatch(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        created = provision_disposable_fence_pin(fence_directory, pin_directory)
        other = _directory()
        self.addCleanup(shutil.rmtree, other, True)
        replacement = prepare_disposable_fence(other / "provider.fence")
        os.replace(replacement.path, created.fence.path)
        spawned = {"count": 0}

        def popen(*_args, **_kwargs):
            spawned["count"] += 1
            raise AssertionError("provider started")

        with self.assertRaises(FenceSafetyError) as caught:
            load_disposable_fence_pin(created.pin_path)
        self.assertEqual(caught.exception.code, "fence_identity")
        live = Path(created.fence.path).stat()
        bogus = ProviderFenceConfig(
            created.fence.path,
            live.st_dev,
            live.st_ino,
        )
        result = supervise(
            claimed_at=CLAIMED_AT,
            operation_id=OPERATION,
            runner_id=RUNNER,
            request_content="hello",
            standing_context="standing",
            wall_clock=lambda: CLAIMED_AT,
            fake="ok",
            fence_config=bogus,
            prove_claim=lambda: True,
            popen=popen,
        )
        self.assertEqual(result.classification, "definite_non_send")
        self.assertEqual(result.calls, 0)
        self.assertEqual(spawned["count"], 0)

    def test_fence_serialization_claim_proof_and_clock_gate_remain(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        created = provision_disposable_fence_pin(fence_directory, pin_directory)
        self.assertEqual(EXECUTION_DEADLINE_US, 600_000_000)
        self.assertEqual(FIXED_LEASE_US, 720_000_000)
        self.assertEqual(RECOVERY_CLOCK_SKEW_MARGIN_US, 120_000_000)
        holder = self._hold(created.pin_path)
        busy = acquire_exclusive(created.fence)
        self.assertIsInstance(busy, FenceBusy)
        holder.stdin.write("\n\n")
        holder.stdin.flush()
        holder.wait(timeout=2)
        publish_loaded_fence_pin(created.pin_path)
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
            fence_config=created.fence,
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
            fence_config=created.fence,
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
                fence_config=created.fence,
                prove_claim=lambda: True,
            )
        self.assertEqual(completed.classification, "success")
        self.assertEqual(completed.output, "answer")
        self.assertEqual(completed.calls, 1)
        self.assertFalse(any(item.category is ResourceWarning for item in caught))

    def test_loader_is_not_wired_into_production_startup(self):
        markers = (
            "provision_disposable_fence_pin",
            "load_disposable_fence_pin",
            "publish_loaded_fence_pin",
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

    def test_product_source_does_not_delete_or_replace_files(self):
        source = (SRC_ROOT / "provider_fence.py").read_text(encoding="utf-8")
        for banned in ("os.unlink", "os.remove", "os.rename", "os.replace"):
            self.assertNotIn(banned, source)

    def test_short_write_revokes_the_pin_in_place(self):
        fence_directory, pin_directory = self._fresh_pair()
        calls = {"count": 0}
        real_write = os.write

        def short_write(descriptor, payload):
            if (
                isinstance(payload, bytes)
                and b"max-smart-provider-fence-pin-v1" in payload
            ):
                calls["count"] += 1
                real_write(descriptor, payload[:1])
                return 1
            return real_write(descriptor, payload)

        with mock.patch("provider_fence.os.write", side_effect=short_write):
            with self.assertRaises(FenceSafetyError) as caught:
                provision_disposable_fence_pin(fence_directory, pin_directory)
        pin_file = pin_directory / "provider-fence.pin"
        record = (fence_directory / "fence.trust").read_bytes()
        self.assertEqual(calls["count"], 1)
        self.assertEqual(caught.exception.code, "pin_file_create_failed")
        self.assertEqual(caught.exception.partial, "fence_record_and_pin")
        self._assert_revoked(pin_file)
        self._assert_loader_refuses(str(pin_file), "pin_file_truncated")
        self._assert_reprovision_refuses(fence_directory, pin_directory, record)

    def test_file_fsync_failure_revokes_and_blocks_loading(self):
        fence_directory, pin_directory = self._fresh_pair()
        seen = {}
        real_fsync = os.fsync
        real_fstat = os.fstat

        def fail_content_fsync(descriptor):
            current = real_fstat(descriptor)
            if stat.S_ISREG(current.st_mode) and _is_pin_descriptor(descriptor, current):
                seen["inode"] = current.st_ino
                raise OSError(errno.EIO, "file fsync failed")
            return real_fsync(descriptor)

        with mock.patch("provider_fence.os.fsync", side_effect=fail_content_fsync):
            with self.assertRaises(FenceSafetyError) as caught:
                provision_disposable_fence_pin(fence_directory, pin_directory)
        pin_file = pin_directory / "provider-fence.pin"
        record = (fence_directory / "fence.trust").read_bytes()
        self.assertEqual(caught.exception.code, "pin_file_fsync_failed")
        self.assertEqual(caught.exception.partial, "fence_record_and_pin")
        self.assertEqual(pin_file.lstat().st_ino, seen["inode"])
        self._assert_revoked(pin_file)
        self._assert_loader_refuses(str(pin_file), "pin_file_truncated")
        child = _run_child(_LOAD_CODE_CHILD, str(pin_file))
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout, "pin_file_truncated")
        self._assert_reprovision_refuses(fence_directory, pin_directory, record)

    def test_directory_fsync_failure_revokes_the_same_inode(self):
        fence_directory, pin_directory = self._fresh_pair()
        seen = {}
        pin_inode = pin_directory.lstat().st_ino
        real_fsync = os.fsync
        real_fstat = os.fstat

        def fail_directory_fsync(descriptor):
            current = real_fstat(descriptor)
            if stat.S_ISREG(current.st_mode) and _is_pin_descriptor(descriptor, current):
                seen["inode"] = current.st_ino
            if stat.S_ISDIR(current.st_mode) and current.st_ino == pin_inode:
                raise OSError(errno.EIO, "directory fsync failed")
            return real_fsync(descriptor)

        with mock.patch("provider_fence.os.fsync", side_effect=fail_directory_fsync):
            with self.assertRaises(FenceSafetyError) as caught:
                provision_disposable_fence_pin(fence_directory, pin_directory)
        pin_file = pin_directory / "provider-fence.pin"
        record = (fence_directory / "fence.trust").read_bytes()
        self.assertEqual(caught.exception.code, "pin_file_fsync_failed")
        self.assertEqual(caught.exception.partial, "fence_record_and_pin")
        self.assertEqual(pin_file.lstat().st_ino, seen["inode"])
        self._assert_revoked(pin_file)
        self._assert_loader_refuses(str(pin_file), "pin_file_truncated")
        self._assert_reprovision_refuses(fence_directory, pin_directory, record)

    def test_unverified_revocation_is_pin_completion_uncertain(self):
        fence_directory, pin_directory = self._fresh_pair()
        real_fsync = os.fsync
        real_fstat = os.fstat

        def fail_content_fsync(descriptor):
            current = real_fstat(descriptor)
            if stat.S_ISREG(current.st_mode) and _is_pin_descriptor(descriptor, current):
                raise OSError(errno.EIO, "file fsync failed")
            return real_fsync(descriptor)

        def fail_truncate(_descriptor, _length):
            raise OSError(errno.EIO, "truncate failed")

        with (
            mock.patch("provider_fence.os.fsync", side_effect=fail_content_fsync),
            mock.patch("provider_fence.os.ftruncate", side_effect=fail_truncate),
        ):
            with self.assertRaises(FenceSafetyError) as caught:
                provision_disposable_fence_pin(fence_directory, pin_directory)
        pin_file = pin_directory / "provider-fence.pin"
        record = (fence_directory / "fence.trust").read_bytes()
        self.assertEqual(caught.exception.code, "pin_completion_uncertain")
        self.assertEqual(caught.exception.__cause__.code, "pin_file_fsync_failed")
        self.assertEqual(caught.exception.partial, "fence_record_and_pin")
        self.assertIn(b"max-smart-provider-fence-pin-v1", pin_file.read_bytes())
        loaded = load_disposable_fence_pin(str(pin_file))
        self.assertEqual(loaded.path, str(fence_directory / "provider.fence"))
        self.assertIsNone(env_fence_config())
        before = pin_file.read_bytes()
        self._assert_reprovision_refuses(fence_directory, pin_directory, record)
        self.assertEqual(pin_file.read_bytes(), before)

    def test_close_failure_preserves_the_active_safety_error(self):
        fence_directory, pin_directory = self._fresh_pair()
        real_fsync = os.fsync
        real_fstat = os.fstat
        real_close = os.close
        directory_inode = pin_directory.lstat().st_ino
        armed = {"value": False}
        seen = {}

        def fail_content_fsync(descriptor):
            current = real_fstat(descriptor)
            if stat.S_ISREG(current.st_mode) and _is_pin_descriptor(descriptor, current):
                seen["inode"] = current.st_ino
                armed["value"] = True
                raise OSError(errno.EIO, "file fsync failed")
            return real_fsync(descriptor)

        def fail_close(descriptor):
            current = real_fstat(descriptor)
            real_close(descriptor)
            if armed["value"] and current.st_ino in (seen["inode"], directory_inode):
                raise OSError(errno.EIO, "close failed")

        with (
            mock.patch("provider_fence.os.fsync", side_effect=fail_content_fsync),
            mock.patch("provider_fence.os.close", side_effect=fail_close),
        ):
            with self.assertRaises(FenceSafetyError) as caught:
                provision_disposable_fence_pin(fence_directory, pin_directory)
        pin_file = pin_directory / "provider-fence.pin"
        self.assertEqual(caught.exception.code, "pin_file_fsync_failed")
        self.assertTrue(any(
            "close failed: errno " in note
            for note in caught.exception.__notes__
        ))
        self._assert_revoked(pin_file)
        self._assert_loader_refuses(str(pin_file), "pin_file_truncated")

    def test_partial_inspection_failure_preserves_the_primary_error(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        real_open = os.open
        real_lstat = os.lstat

        def fail_record(path, flags, mode=0o777, *, dir_fd=None):
            if path == "fence.trust" and flags & os.O_CREAT:
                raise OSError(errno.EIO, "record create failed")
            return real_open(path, flags, mode, dir_fd=dir_fd)

        def fail_inspect(path, *args, **kwargs):
            text = os.fspath(path)
            if text.endswith(("provider.fence", "fence.trust", "provider-fence.pin")):
                raise OSError(errno.EIO, "inspect failed")
            return real_lstat(path, *args, **kwargs)

        with (
            mock.patch("provider_fence.os.open", side_effect=fail_record),
            mock.patch("provider_fence.os.lstat", side_effect=fail_inspect),
        ):
            with self.assertRaises(FenceSafetyError) as caught:
                provision_disposable_fence_pin(fence_directory, pin_directory)
        self.assertEqual(caught.exception.code, "trust_record_create_failed")
        self.assertTrue(any(
            "partial inspection failed: fence_unreadable" in note
            for note in caught.exception.__notes__
        ))
        self.assertIsNone(env_fence_config())

    def test_non_regular_pin_parent_owner_and_filesystem_are_refused(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        fifo_directory = _directory()
        self.addCleanup(shutil.rmtree, fifo_directory, True)
        fifo = fifo_directory / "provider-fence.pin"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(FenceSafetyError) as fifo_error:
            load_disposable_fence_pin(str(fifo))
        self.assertEqual(fifo_error.exception.code, "pin_file_malformed")
        pin_inode = pin_directory.lstat().st_ino
        real_fstat = os.fstat

        def changed(descriptor):
            current = real_fstat(descriptor)
            if current.st_ino != pin_inode:
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
            with self.assertRaises(FenceSafetyError) as owner:
                provision_disposable_fence_pin(fence_directory, pin_directory)
        self.assertEqual(owner.exception.code, "fence_directory_owner")
        self.assertFalse((fence_directory / "provider.fence").exists())
        with mock.patch("provider_fence._directory_is_local", return_value=False):
            with self.assertRaises(FenceSafetyError) as filesystem:
                provision_disposable_fence_pin(fence_directory, pin_directory)
        self.assertEqual(filesystem.exception.code, "fence_filesystem")
        self.assertFalse((fence_directory / "provider.fence").exists())
        self.assertFalse((pin_directory / "provider-fence.pin").exists())

    def _fresh_pair(self):
        fence_directory, pin_directory = _pair()
        self.addCleanup(shutil.rmtree, fence_directory, True)
        self.addCleanup(shutil.rmtree, pin_directory, True)
        return fence_directory, pin_directory

    def _assert_revoked(self, pin_file):
        status = pin_file.lstat()
        self.assertTrue(stat.S_ISREG(status.st_mode))
        self.assertEqual(status.st_size, 0)
        self.assertEqual(status.st_nlink, 1)
        self.assertFalse(stat.S_ISLNK(status.st_mode))

    def _assert_loader_refuses(self, pin_path, code):
        with self.assertRaises(FenceSafetyError) as caught:
            load_disposable_fence_pin(pin_path)
        self.assertEqual(caught.exception.code, code)
        self.assertIsNone(env_fence_config())

    def _assert_reprovision_refuses(self, fence_directory, pin_directory, record):
        with self.assertRaises(FenceSafetyError) as retry:
            provision_disposable_fence_pin(fence_directory, pin_directory)
        self.assertEqual(retry.exception.code, "trust_record_exists")
        self.assertEqual(Path(fence_directory, "fence.trust").read_bytes(), record)

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
        self.addCleanup(lambda: _finish_process(process))
        line = process.stdout.readline().strip()
        if line != "held":
            error = process.stderr.read()
            _finish_process(process)
            raise AssertionError(f"{line}\n{error}")
        return process

    def _replace_pin(self, pin_path, payload):
        temporary = Path(pin_path).with_name("provider-fence.pin.tmp")
        _write_exclusive(temporary, payload)
        os.replace(temporary, pin_path)


_AGREE_CHILD = """\
import json
import os
import sys
from provider_fence import publish_loaded_fence_pin
inherited = any(
    name in os.environ
    for name in (
        "MAX_SMART_PROVIDER_FENCE",
        "MAX_SMART_PROVIDER_FENCE_DEVICE",
        "MAX_SMART_PROVIDER_FENCE_INODE",
    )
)
config = publish_loaded_fence_pin(sys.argv[1])
again = publish_loaded_fence_pin(sys.argv[1])
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
from provider_fence import load_disposable_fence_pin
load_disposable_fence_pin(sys.argv[1])
sys.stdout.write("ok\\n")
"""


_LOAD_CODE_CHILD = """\
import sys
from provider_fence import FenceSafetyError, load_disposable_fence_pin
try:
    load_disposable_fence_pin(sys.argv[1])
except FenceSafetyError as error:
    sys.stdout.write(error.code)
else:
    sys.stdout.write("loaded")
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


if __name__ == "__main__":
    unittest.main()
