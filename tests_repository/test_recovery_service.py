"""Disposable tests for the recovery service adapter.

These tests use one macOS account and temporary files. Peer mismatches are
configuration mismatches, not operating-system account separation.
"""

import array
import errno
import json
import os
import shutil
import socket
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import recovery_helper  # noqa: E402
import recovery_service  # noqa: E402
from max_smart_db.config import PRODUCTION_DATABASE_PATH  # noqa: E402
from max_smart_db.safety import initialize_sqlite_file  # noqa: E402
from provider_fence import (  # noqa: E402
    PRODUCTION_FENCE_PATH,
    _APPROVED_PIN_DIRECTORY,
)
from provisioning_rehearsal import disposable_rehearsal_root  # noqa: E402
from recovery_helper import RecoveryHelperError  # noqa: E402
from recovery_service import (  # noqa: E402
    MACOS_ACCOUNT_SEPARATION_ENFORCED,
    SIMULATED_SERVICE_BOUNDARY,
    SNAPSHOT_REQUEST,
    VERIFY_REQUEST,
    RecoveryService,
    RecoveryServiceConfiguration,
    RecoveryServiceError,
    current_process_executable,
)


def _private(path):
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


class RecoveryServiceTests(unittest.TestCase):
    def setUp(self):
        self.root = disposable_rehearsal_root()
        self.addCleanup(lambda: shutil.rmtree(self.root, True))
        self.socket_root = Path(tempfile.mkdtemp(prefix="ms-", dir="/private/tmp"))
        os.chmod(self.socket_root, 0o700)
        self.addCleanup(lambda: shutil.rmtree(self.socket_root, True))
        self.session = PROJECT_ROOT / "data" / "current_session.json"
        self.session_before = self.session.lstat()
        self.source = self.root / "source.sqlite3"
        initialize_sqlite_file(self.source)
        self.holder = sqlite3.connect(self.source)
        self.addCleanup(self.holder.close)
        self.holder.execute("CREATE TABLE items(value TEXT)")
        self.holder.execute(
            "INSERT INTO items(value) VALUES ('committed-marker')"
        )
        self.holder.commit()
        self.holder.execute(
            "INSERT INTO items(value) VALUES ('uncommitted-marker')"
        )
        self.store = _private(self.root / "store")
        self.anchor = _private(self.root / "anchor")
        self.socket_path = self.socket_root / "s"
        self.service = None
        self.thread = None
        self.addCleanup(self._stop)
        listed = self.source.lstat()
        self.patches = [
            mock.patch("recovery_helper.DELEGATED_SOURCE_PATH", self.source),
            mock.patch(
                "recovery_helper._delegated_runtime_uid",
                return_value=listed.st_uid + 1,
            ),
            mock.patch(
                "recovery_helper.os.open",
                side_effect=self._read_only_open(),
            ),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _read_only_open(self):
        real_open = os.open
        probe = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC

        def guarded(path, flags, *args, **kwargs):
            candidate = Path(path)
            protected = (
                self.source,
                Path(f"{self.source}.initialization-state"),
                Path(f"{self.source}-wal"),
                Path(f"{self.source}-shm"),
            )
            if flags == probe and candidate in protected and candidate.exists():
                raise PermissionError(errno.EACCES, "read-only delegated source")
            return real_open(path, flags, *args, **kwargs)

        return guarded

    def _configuration(self, **overrides):
        listed = self.source.lstat()
        values = {
            "socket_path": self.socket_path,
            "store": self.store,
            "anchor_directory": self.anchor,
            "key": os.urandom(32),
            "source_identity": os.urandom(32),
            "store_identity": os.urandom(32),
            "expected_owner_uid": listed.st_uid,
            "expected_device": listed.st_dev,
            "expected_inode": listed.st_ino,
            "allowed_peer_uid": os.geteuid(),
            "allowed_peer_gid": os.getegid(),
            "executable_pin": current_process_executable(),
            "snapshot_quota": 4,
            "verify_quota": 4,
        }
        values.update(overrides)
        return RecoveryServiceConfiguration(**values)

    def _start(self, **overrides):
        self.service = RecoveryService(self._configuration(**overrides))
        self.thread = threading.Thread(target=self.service.serve_forever)
        self.thread.start()
        self.assertTrue(self.service.ready.wait(2))

    def _stop(self):
        if self.service is not None:
            self.service.shutdown()
        if self.thread is not None:
            self.thread.join(5)

    def _exchange(self, payload, ancillary=None):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if hasattr(socket, "SO_NOSIGPIPE"):
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_NOSIGPIPE, 1)
        connection.settimeout(30)
        try:
            connection.connect(str(self.socket_path))
            if ancillary is None:
                connection.sendall(payload)
            else:
                connection.sendmsg([payload], ancillary)
            connection.shutdown(socket.SHUT_WR)
            chunks = []
            while sum(len(chunk) for chunk in chunks) <= 256:
                chunk = connection.recv(256)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            connection.close()
        raw = b"".join(chunks)
        self.assertLessEqual(len(raw), 96)
        response = json.loads(raw.decode("ascii"))
        self.assertNotIn(b"committed-marker", raw)
        self.assertNotIn(b"uncommitted-marker", raw)
        self.assertNotIn(str(self.store).encode(), raw)
        self.assertNotIn(str(self.source).encode(), raw)
        return response

    def _assert_preserved(self):
        self.assertFalse(PRODUCTION_DATABASE_PATH.exists())
        self.assertFalse(PRODUCTION_FENCE_PATH.exists())
        self.assertFalse((PRODUCTION_FENCE_PATH.parent / "fence.trust").exists())
        self.assertFalse(_APPROVED_PIN_DIRECTORY.exists())
        current = self.session.lstat()
        self.assertEqual(current.st_ino, self.session_before.st_ino)
        self.assertEqual(current.st_size, self.session_before.st_size)
        self.assertEqual(current.st_mtime_ns, self.session_before.st_mtime_ns)

    def test_simulated_checks_are_not_account_separation(self):
        self.assertFalse(MACOS_ACCOUNT_SEPARATION_ENFORCED)
        self.assertEqual(SIMULATED_SERVICE_BOUNDARY, "simulated")
        source = Path(recovery_service.__file__).read_text(encoding="utf-8")
        self.assertNotIn("restore_recovery_point", source)
        self.assertNotIn("subprocess", source)
        self.assertNotIn("os.system", source)
        self._assert_preserved()

    def test_request_grammar_rejects_caller_supplied_values(self):
        self._start()
        rejected = (
            b'{"op":"snapshot","path":"/tmp/x"}',
            b'{"op":"verify","key":"secret"}',
            b'{"op":"snapshot","source_identity":"aa"}',
            b'{"op":"verify","store_identity":"bb"}',
            b'{"op":"snapshot","generation":2}',
            b'{"op":"verify","filename":"database.sqlite3"}',
            b'{"op":"restore"}',
            b'{"op":"sign","payload":"abc"}',
            b'{"op": "snapshot"}',
            b'{"op":"verify"}{"op":"snapshot"}',
            b'{"op":"snapshot","op":"verify"}',
            b"",
            b"x" * 65,
        )
        for payload in rejected:
            with self.subTest(payload=payload):
                response = self._exchange(payload)
                self.assertEqual(set(response), {"code", "status"})
                self.assertEqual(response["status"], "refused")
                self.assertEqual(response["code"], "invalid_request")
        self.assertFalse((self.store / "head.json").exists())
        self.assertEqual(list(self.anchor.iterdir()), [])
        self._assert_preserved()

    def test_received_file_descriptors_are_discarded(self):
        self._start()
        secret = self.root / "secret"
        secret.write_bytes(b"top-secret-backup")
        os.chmod(secret, 0o600)
        descriptor = os.open(secret, os.O_RDONLY)
        try:
            rights = array.array("i", [descriptor])
            response = self._exchange(
                VERIFY_REQUEST,
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights.tobytes())],
            )
        finally:
            os.close(descriptor)
        self.assertEqual(response["code"], "invalid_request")
        self.assertNotIn("top-secret-backup", json.dumps(response))
        self.assertFalse((self.store / "head.json").exists())
        self._assert_preserved()

    def test_mismatched_peer_uid_is_unauthorized(self):
        self._start(allowed_peer_uid=os.geteuid() + 1)
        response = self._exchange(SNAPSHOT_REQUEST)
        self.assertEqual(response["code"], "unauthorized")
        self.assertFalse((self.store / "head.json").exists())
        self._assert_preserved()

    def test_mismatched_executable_pin_is_unauthorized(self):
        self._start(executable_pin="/usr/bin/true")
        response = self._exchange(VERIFY_REQUEST)
        self.assertEqual(response["code"], "unauthorized")
        self.assertFalse((self.store / "head.json").exists())
        self._assert_preserved()

    def test_snapshot_and_verify_quotas_are_independent(self):
        self._start(snapshot_quota=1, verify_quota=1)
        first = self._exchange(SNAPSHOT_REQUEST)
        self.assertEqual(first, {"generation": 1, "status": "ok"})
        blocked = self._exchange(SNAPSHOT_REQUEST)
        self.assertEqual(blocked["code"], "quota_exceeded")
        verified = self._exchange(VERIFY_REQUEST)
        self.assertEqual(verified, {"generation": 1, "status": "ok"})
        blocked_verify = self._exchange(VERIFY_REQUEST)
        self.assertEqual(blocked_verify["code"], "quota_exceeded")
        self.assertFalse((self.store / "generations" / "000002").exists())
        self._assert_preserved()

    def test_successful_responses_exclude_backup_bytes(self):
        self._start()
        snapshot = self._exchange(SNAPSHOT_REQUEST)
        verify = self._exchange(VERIFY_REQUEST)
        self.assertEqual(snapshot, {"generation": 1, "status": "ok"})
        self.assertEqual(verify, {"generation": 1, "status": "ok"})
        database = self.store / "generations" / "000001" / "database.sqlite3"
        self.assertNotIn(database.read_bytes()[:32], json.dumps(snapshot).encode())
        self.assertEqual(list(database.parent.glob("database.sqlite3-*")), [])
        self._assert_preserved()

    def test_regressed_store_is_refused_without_moving_the_anchor(self):
        self._start()
        self.assertEqual(self._exchange(SNAPSHOT_REQUEST)["generation"], 1)
        saved = self.root / "store-generation-1"
        shutil.copytree(self.store, saved)
        self.assertEqual(self._exchange(SNAPSHOT_REQUEST)["generation"], 2)
        shutil.rmtree(self.store)
        shutil.copytree(saved, self.store)
        response = self._exchange(VERIFY_REQUEST)
        self.assertEqual(response["code"], "rollback")
        self.assertFalse((self.store / "generations" / "000002").exists())
        anchor = json.loads((self.anchor / "accepted.json").read_text("ascii"))
        self.assertEqual(anchor["generation"], 2)
        self._assert_preserved()

    def test_interrupted_anchor_update_fails_closed(self):
        self._start()

        def fail_snapshot(*args, **kwargs):
            raise RecoveryHelperError("recovery_permissions")

        with mock.patch(
            "recovery_service.create_delegated_recovery_snapshot",
            side_effect=fail_snapshot,
        ):
            failed = self._exchange(SNAPSHOT_REQUEST)
        self.assertEqual(failed["code"], "failed")
        follow_up = self._exchange(VERIFY_REQUEST)
        self.assertEqual(follow_up["code"], "anchor_pending")
        self.assertFalse((self.store / "head.json").exists())
        self.assertTrue((self.anchor / "pending.json").exists())
        self._assert_preserved()

    def test_uncertain_directory_sync_fails_closed(self):
        self._start()
        self.assertEqual(self._exchange(SNAPSHOT_REQUEST)["generation"], 1)
        real_fsync = recovery_service.os.fsync

        def fail_directory(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError(errno.EIO, "directory fsync")
            return real_fsync(descriptor)

        with mock.patch("recovery_service.os.fsync", side_effect=fail_directory):
            uncertain = self._exchange(SNAPSHOT_REQUEST)
        self.assertEqual(uncertain["code"], "anchor_uncertain")
        follow_up = self._exchange(VERIFY_REQUEST)
        self.assertEqual(follow_up["code"], "anchor_uncertain")
        self.assertFalse((self.store / "generations" / "000002").exists())
        self._assert_preserved()

    def test_contradictory_anchor_file_is_refused(self):
        self._start()
        self.assertEqual(self._exchange(SNAPSHOT_REQUEST)["generation"], 1)
        extra = self.anchor / "extra"
        extra.write_bytes(b"planted")
        os.chmod(extra, 0o600)
        response = self._exchange(VERIFY_REQUEST)
        self.assertEqual(response["code"], "anchor_contradictory")
        self.assertEqual(extra.read_bytes(), b"planted")
        self._assert_preserved()

    def test_shutdown_removes_the_listening_socket(self):
        self._start()
        self.service.shutdown()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertFalse(self.socket_path.exists())
        with self.assertRaises(OSError):
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                connection.connect(str(self.socket_path))
            finally:
                connection.close()
        self.service = None
        self.thread = None
        self._assert_preserved()

    def test_production_configuration_is_refused(self):
        with self.assertRaises(RecoveryServiceError) as refused:
            self._configuration(store=PROJECT_ROOT / "data")
        self.assertEqual(refused.exception.code, "configuration_invalid")
        self.assertFalse(PRODUCTION_DATABASE_PATH.exists())
        self._assert_preserved()


if __name__ == "__main__":
    unittest.main()
