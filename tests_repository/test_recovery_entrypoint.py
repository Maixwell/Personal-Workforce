"""Disposable security tests for the root-anchored recovery entrypoint."""

import array
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import recovery_entrypoint  # noqa: E402
from max_smart_db.config import PRODUCTION_DATABASE_PATH  # noqa: E402
from provider_fence import PRODUCTION_FENCE_PATH, _APPROVED_PIN_DIRECTORY  # noqa: E402
from recovery_entrypoint import CLIENT_GID, CLIENT_UID, EntrypointError  # noqa: E402


SOURCE_ROOT = PROJECT_ROOT / "src"
CLIENT_SOURCE = PROJECT_ROOT / "client" / "max-smart-recovery-client.c"
FIXED_SOURCE = Path(
    "/Users/smart/Projects/Personal Workforce/data/max_smart.sqlite3"
)
FIXED_SOCKET = "/Library/Application Support/MaxSmartRecovery/service/s"
SDK = "/Library/Developer/CommandLineTools/SDKs/MacOSX26.sdk"


def _mkdir(path, mode):
    path.mkdir(mode=mode)
    os.chmod(path, mode)
    return path


class RecoveryEntrypointTests(unittest.TestCase):
    """Same-UID fixtures validate refusal logic, not account separation."""

    def setUp(self):
        if os.geteuid() == CLIENT_UID and os.getegid() != CLIENT_GID:
            os.setgid(CLIENT_GID)
        self.root = Path(tempfile.mkdtemp(prefix="ms-entry-", dir="/private/tmp"))
        os.chmod(self.root, 0o711)
        self.addCleanup(self._cleanup_root)
        self.session = PROJECT_ROOT / "data" / "current_session.json"
        self.session_before = self.session.lstat()
        self.service = None
        self.thread = None
        self.addCleanup(self._stop)
        self._install_tree()
        self.client = self.root / "bin" / "max-smart-recovery-client"
        self._compile(self.socket_path, self.client)
        os.chmod(self.root / "bin", 0o555)
        self._patch_trust_constants()
        self.trusted_sys_path = [
            entry
            for entry in sys.path
            if recovery_entrypoint._is_within(
                Path(entry).absolute(), recovery_entrypoint.TRUSTED_PYTHON_PREFIX
            )
        ]

    def _cleanup_root(self):
        if self.root.exists():
            service = self.root / "service"
            if service.exists():
                subprocess.run(
                    ["/bin/chmod", "-N", str(service)],
                    check=False,
                    capture_output=True,
                )
            subprocess.run(
                ["/bin/chmod", "-RN", str(self.root)],
                check=False,
                capture_output=True,
            )
            subprocess.run(
                ["/bin/chmod", "-R", "u+rwX", str(self.root)],
                check=False,
                capture_output=True,
            )
        shutil.rmtree(self.root, True)
        self.assertFalse(self.root.exists())

    def _patch_trust_constants(self):
        values = {
            "INSTALL_ROOT": self.root,
            "ENTRYPOINT_PATH": self.root / "code" / "recovery_entrypoint.py",
            "CLIENT_PATH": self.client,
            "TRUSTED_INSTALL_BOUNDARY": self.root,
            "TRUSTED_OWNER_UID": os.geteuid(),
        }
        for name, value in values.items():
            patcher = mock.patch.object(recovery_entrypoint, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        source = mock.patch.object(
            recovery_entrypoint,
            "_current_entrypoint_source",
            lambda: recovery_entrypoint.ENTRYPOINT_PATH,
        )
        source.start()
        self.addCleanup(source.stop)

    def _install_tree(self):
        code = _mkdir(self.root / "code", 0o755)
        entrypoint = code / "recovery_entrypoint.py"
        entrypoint.write_bytes((SOURCE_ROOT / "recovery_entrypoint.py").read_bytes())
        os.chmod(entrypoint, 0o555)
        library = _mkdir(code / "lib", 0o755)
        package = _mkdir(library / "max_smart_db", 0o755)
        for name in recovery_entrypoint._FROZEN_SHA256:
            source = SOURCE_ROOT / name
            destination = library / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
            os.chmod(destination, 0o444)
        os.chmod(package, 0o555)
        os.chmod(library, 0o555)
        os.chmod(code, 0o555)
        for name in ("service", "store", "anchor", "key", "identity"):
            _mkdir(self.root / name, 0o700)
        metadata_dir = _mkdir(self.root / "metadata", 0o755)
        bin_dir = _mkdir(self.root / "bin", 0o700)
        self._secret(self.root / "key" / "recovery.key", b"K" * 32)
        self._secret(self.root / "identity" / "source.id", b"S" * 32)
        self._secret(self.root / "identity" / "store.id", b"T" * 32)
        metadata = metadata_dir / "source.json"
        metadata.write_text(
            '{"device":1,"format":"max-smart/recovery-source-metadata/v1",'
            '"inode":1,"owner_uid":501}',
            encoding="ascii",
        )
        os.chmod(metadata, 0o400)
        os.chmod(metadata_dir, 0o555)
        self.socket_path = self.root / "service" / "s"
        self.assertTrue(bin_dir.is_dir())

    def _secret(self, path, payload):
        path.write_bytes(payload)
        os.chmod(path, 0o400)

    def _compile(self, socket_path, destination):
        completed = subprocess.run(
            [
                "cc",
                "-isysroot",
                SDK,
                "-O2",
                "-DRECOVERY_DISPOSABLE_TEST_BUILD=1",
                f'-DRECOVERY_SOCKET_PATH="{socket_path}"',
                "-o",
                str(destination),
                str(CLIENT_SOURCE),
            ],
            check=False,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        os.chmod(destination, 0o555)

    def _start(self, **environment):
        old_environment = {name: os.environ.get(name) for name in environment}
        old_path = list(sys.path)
        os.environ.update(environment)
        sys.path[:] = self.trusted_sys_path
        try:
            self.service, self.thread = recovery_entrypoint.start_service(
                self.root, self.client
            )
        finally:
            sys.path[:] = old_path
            for name, value in old_environment.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def _stop(self):
        if self.service is not None:
            self.service.shutdown()
        if self.thread is not None:
            self.thread.join(5)
        self.service = None
        self.thread = None

    def _run_client(self, operation):
        return subprocess.run(
            [str(self.client), operation], check=False, capture_output=True
        )

    def _assert_preserved(self):
        self.assertFalse(recovery_entrypoint.OS_ACCOUNT_SEPARATION_ENFORCED)
        self.assertFalse(PRODUCTION_DATABASE_PATH.exists())
        self.assertFalse(PRODUCTION_FENCE_PATH.exists())
        self.assertFalse((PRODUCTION_FENCE_PATH.parent / "fence.trust").exists())
        self.assertFalse(_APPROVED_PIN_DIRECTORY.exists())
        current = self.session.lstat()
        self.assertEqual(current.st_ino, self.session_before.st_ino)
        self.assertEqual(current.st_size, self.session_before.st_size)
        self.assertEqual(current.st_mtime_ns, self.session_before.st_mtime_ns)

    def test_fixed_source_survives_frozen_relocation(self):
        self._start()
        helper = sys.modules["recovery_helper"]
        frozen_config = sys.modules["max_smart_db.config"]
        self.assertEqual(helper.DELEGATED_SOURCE_PATH, FIXED_SOURCE)
        self.assertEqual(
            frozen_config.PRODUCTION_DATABASE_PATH,
            self.root / "code" / "data" / "max_smart.sqlite3",
        )
        self.assertNotEqual(
            helper.DELEGATED_SOURCE_PATH, frozen_config.PRODUCTION_DATABASE_PATH
        )
        completed = self._run_client("verify")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stdout,
            b'{"code":"anchor_invalid","status":"refused"}\n',
        )
        self._assert_preserved()

    def test_entrypoint_module_and_install_ancestry_are_fail_closed(self):
        with mock.patch.object(
            recovery_entrypoint, "_current_entrypoint_source", lambda: self.root / "other"
        ):
            with self.assertRaises(EntrypointError) as substituted:
                self._start()
        self.assertEqual(substituted.exception.code, "entrypoint_untrusted")
        self.assertFalse(self.socket_path.exists())

        os.chmod(self.root / "code" / "lib", 0o775)
        with self.assertRaises(EntrypointError) as writable:
            self._start()
        self.assertEqual(writable.exception.code, "module_untrusted")
        os.chmod(self.root / "code" / "lib", 0o555)

        entrypoint = self.root / "code" / "recovery_entrypoint.py"
        saved = entrypoint.read_bytes()
        os.chmod(self.root / "code", 0o755)
        entrypoint.unlink()
        entrypoint.symlink_to(self.root / "code" / "lib" / "recovery_service.py")
        with self.assertRaises(EntrypointError) as linked:
            self._start()
        self.assertEqual(linked.exception.code, "entrypoint_untrusted")
        entrypoint.unlink()
        entrypoint.write_bytes(saved)
        os.chmod(entrypoint, 0o555)
        os.chmod(self.root / "code", 0o555)

        library = self.root / "code" / "lib"
        module = library / "recovery_service.py"
        module_bytes = module.read_bytes()
        os.chmod(library, 0o755)
        module.unlink()
        module.symlink_to(library / "provider_fence.py")
        os.chmod(library, 0o555)
        with self.assertRaises(EntrypointError) as module_link:
            self._start()
        self.assertEqual(module_link.exception.code, "module_untrusted")
        os.chmod(library, 0o755)
        module.unlink()
        module.write_bytes(module_bytes)
        os.chmod(module, 0o444)
        os.chmod(library, 0o555)

        with mock.patch.object(
            recovery_entrypoint, "TRUSTED_OWNER_UID", os.geteuid() + 1
        ):
            with self.assertRaises(EntrypointError) as owner:
                self._start()
        self.assertEqual(owner.exception.code, "installation_invalid")
        self._assert_preserved()

    def test_interpreter_ancestry_and_runtime_customization_are_refused(self):
        fake_root = _mkdir(self.root / "fake-interpreter", 0o700)
        parent = _mkdir(fake_root / "writable", 0o775)
        image = parent / "Python"
        image.write_bytes(b"not executable code")
        os.chmod(image, 0o755)
        with mock.patch.object(
            recovery_entrypoint, "TRUSTED_INTERPRETER_IMAGE", image
        ), mock.patch.object(
            recovery_entrypoint, "TRUSTED_INTERPRETER_BOUNDARY", fake_root
        ), mock.patch.object(
            recovery_entrypoint, "TRUSTED_INTERPRETER_OWNER_UID", os.geteuid()
        ), mock.patch.object(
            recovery_entrypoint, "_current_interpreter_image", lambda: image
        ):
            with self.assertRaises(EntrypointError) as ancestry:
                recovery_entrypoint._require_interpreter()
        self.assertEqual(ancestry.exception.code, "interpreter_untrusted")

        with self.assertRaises(EntrypointError) as startup:
            self._start(PYTHONSTARTUP="/private/tmp/never-executed")
        self.assertEqual(startup.exception.code, "runtime_untrusted")
        self.assertFalse(self.socket_path.exists())

        shadow = _mkdir(self.root / "shadow", 0o700)
        (shadow / "recovery_service.py").write_text("raise RuntimeError('shadow')\n")
        old = list(self.trusted_sys_path)
        self.trusted_sys_path.insert(0, str(shadow))
        try:
            with self.assertRaises(EntrypointError) as module_path:
                self._start()
        finally:
            self.trusted_sys_path[:] = old
        self.assertEqual(module_path.exception.code, "runtime_untrusted")
        self._assert_preserved()

    def test_service_and_socket_acls_are_exact(self):
        self._start()
        name = recovery_entrypoint._account_name(CLIENT_UID)
        self.assertEqual(
            recovery_entrypoint._acl_entries(self.root / "service"),
            (f"user:{name} allow search",),
        )
        self.assertEqual(
            recovery_entrypoint._acl_entries(self.socket_path),
            (f"user:{name} allow write",),
        )
        self.assertEqual(stat.S_IMODE(self.socket_path.lstat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / "service").lstat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.root.lstat().st_mode), 0o711)
        self._assert_preserved()

    def test_acl_failures_and_unexpected_entries_leave_no_listener(self):
        real_run = recovery_entrypoint.subprocess.run

        def fail_directory(argv, **kwargs):
            if argv[:2] == ["/bin/chmod", "+a"] and argv[-1] == str(
                self.root / "service"
            ):
                return subprocess.CompletedProcess(argv, 1, b"", b"denied")
            return real_run(argv, **kwargs)

        with mock.patch.object(recovery_entrypoint.subprocess, "run", fail_directory):
            with self.assertRaises(EntrypointError) as directory:
                self._start()
        self.assertEqual(directory.exception.code, "socket_acl")
        self.assertFalse(self.socket_path.exists())

        subprocess.run(
            [
                "/bin/chmod",
                "+a",
                f"user:{recovery_entrypoint._account_name(CLIENT_UID)} deny search",
                str(self.root / "service"),
            ],
            check=True,
        )
        with self.assertRaises(EntrypointError) as unexpected:
            self._start()
        self.assertEqual(unexpected.exception.code, "socket_acl")
        subprocess.run(
            ["/bin/chmod", "-N", str(self.root / "service")],
            check=True,
        )
        self.assertFalse(self.socket_path.exists())
        self._assert_preserved()

    def test_socket_acl_failure_closes_and_unlinks_listener(self):
        real_run = recovery_entrypoint.subprocess.run

        def fail_socket(argv, **kwargs):
            if argv[:2] == ["/bin/chmod", "+a"] and argv[-1] == str(
                self.socket_path
            ):
                return subprocess.CompletedProcess(argv, 1, b"", b"denied")
            return real_run(argv, **kwargs)

        with mock.patch.object(recovery_entrypoint.subprocess, "run", fail_socket):
            with self.assertRaises(EntrypointError) as failure:
                self._start()
        self.assertEqual(failure.exception.code, "socket_acl")
        self.assertFalse(self.socket_path.exists())
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with self.assertRaises(OSError):
                probe.connect(str(self.socket_path))
        finally:
            probe.close()
        self._assert_preserved()

    def test_unexpected_socket_grant_is_refused_on_disposable_inode(self):
        disposable = self.root / "service" / "acl-socket"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(disposable))
        os.chmod(disposable, 0o600)
        name = recovery_entrypoint._account_name(CLIENT_UID)
        subprocess.run(
            ["/bin/chmod", "+a", f"user:{name} allow write", str(disposable)],
            check=True,
        )
        subprocess.run(
            ["/bin/chmod", "+a", "group:staff allow write", str(disposable)],
            check=True,
        )
        try:
            with self.assertRaises(EntrypointError) as unexpected:
                recovery_entrypoint._ensure_exclusive_acl(
                    disposable,
                    kind="socket",
                    mode=0o600,
                    client_uid=CLIENT_UID,
                    permission="write",
                    allow_create=False,
                )
            self.assertEqual(unexpected.exception.code, "socket_acl")
        finally:
            listener.close()
            disposable.unlink()
        self._assert_preserved()

    def test_peer_uid_gid_and_executable_mismatch_are_refused(self):
        cases = (
            (0, CLIENT_GID, os.fsencode(self.client)),
            (CLIENT_UID, 0, os.fsencode(self.client)),
            (CLIENT_UID, CLIENT_GID, b"/usr/bin/true"),
        )
        for credentials in cases:
            self._start()
            module = sys.modules["recovery_service"]
            module._peer_credentials = lambda descriptor, value=credentials: value
            completed = self._run_client("verify")
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(
                completed.stdout,
                b'{"code":"unauthorized","status":"refused"}\n',
            )
            self._stop()
        self._assert_preserved()

    def test_installation_root_0700_is_refused_and_0711_is_required(self):
        os.chmod(self.root, 0o700)
        try:
            with self.assertRaises(EntrypointError) as denied:
                self._start()
            self.assertEqual(denied.exception.code, "traversal_denied")
            self.assertFalse(self.socket_path.exists())
        finally:
            os.chmod(self.root, 0o711)
        self._start()
        self.assertEqual(stat.S_IMODE(self.root.lstat().st_mode), 0o711)
        self._assert_preserved()

    def test_traversal_failure_after_bind_leaves_no_listener(self):
        real_prepare = recovery_entrypoint._prepare_stale_socket

        def revoke_root_search(path, service_dir):
            real_prepare(path, service_dir)
            os.chmod(self.root, 0o700)

        with mock.patch.object(
            recovery_entrypoint, "_prepare_stale_socket", revoke_root_search
        ):
            with self.assertRaises(EntrypointError) as denied:
                self._start()
        self.assertEqual(denied.exception.code, "traversal_denied")
        self.assertFalse(self.socket_path.exists())
        with self.assertRaises(OSError):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(self.socket_path))
            finally:
                probe.close()
        os.chmod(self.root, 0o711)
        self._assert_preserved()

    def test_regular_file_and_symlink_socket_paths_are_preserved(self):
        self.socket_path.write_bytes(b"preserved-regular")
        os.chmod(self.socket_path, 0o600)
        with self.assertRaises(EntrypointError) as regular:
            self._start()
        self.assertEqual(regular.exception.code, "socket_refused")
        self.assertEqual(self.socket_path.read_bytes(), b"preserved-regular")
        self.socket_path.unlink()
        target = self.root / "service" / "elsewhere"
        target.write_bytes(b"preserved-target")
        self.socket_path.symlink_to(target)
        with self.assertRaises(EntrypointError) as linked:
            self._start()
        self.assertEqual(linked.exception.code, "socket_refused")
        self.assertTrue(self.socket_path.is_symlink())
        self.assertEqual(target.read_bytes(), b"preserved-target")
        self.socket_path.unlink()
        self._assert_preserved()

    def test_live_socket_is_preserved_and_stale_socket_is_replaced(self):
        live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        live.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        live.listen(1)
        live_inode = self.socket_path.lstat().st_ino
        try:
            with self.assertRaises(EntrypointError) as busy:
                self._start()
            self.assertEqual(busy.exception.code, "socket_busy")
            self.assertEqual(self.socket_path.lstat().st_ino, live_inode)
        finally:
            live.close()
            self.socket_path.unlink()

        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        stale_inode = self.socket_path.lstat().st_ino
        stale.close()
        self._start()
        self.assertNotEqual(self.socket_path.lstat().st_ino, stale_inode)
        self._assert_preserved()

    def test_shutdown_preserves_replacement_inode(self):
        self._start()
        original = self.socket_path.lstat().st_ino
        moved = self.root / "service" / "moved-listener"
        os.rename(self.socket_path, moved)
        self.socket_path.write_bytes(b"replacement")
        os.chmod(self.socket_path, 0o600)
        self._stop()
        self.assertEqual(self.socket_path.read_bytes(), b"replacement")
        self.assertNotEqual(self.socket_path.lstat().st_ino, original)
        self.assertTrue(moved.exists())
        self._assert_preserved()

    def test_client_timeout_oversize_unexpected_and_descriptor_responses(self):
        fake = self.socket_path
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(fake))
        os.chmod(fake, 0o600)
        listener.listen(1)

        def serve(payload=None, ancillary=None, delay=0):
            connection, _ = listener.accept()
            try:
                connection.recv(64)
                if delay:
                    time.sleep(delay)
                elif ancillary is not None:
                    connection.sendmsg([payload], ancillary)
                else:
                    connection.sendall(payload)
            finally:
                connection.close()

        stalled = threading.Thread(target=serve, kwargs={"delay": 3})
        stalled.start()
        started = time.monotonic()
        timeout = self._run_client("verify")
        elapsed = time.monotonic() - started
        stalled.join(5)
        self.assertEqual(timeout.returncode, 1)
        self.assertLess(elapsed, 2.8)

        for payload in (b"X" * 200, b'{"status":"ok"}\n'):
            worker = threading.Thread(target=serve, args=(payload,))
            worker.start()
            result = self._run_client("snapshot")
            worker.join(5)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, b"")

        secret = self.root / "secret"
        secret.write_bytes(b"database-secret")
        descriptor = os.open(secret, os.O_RDONLY)
        rights = array.array("i", [descriptor])
        worker = threading.Thread(
            target=serve,
            kwargs={
                "payload": b'{"code":"failed","status":"refused"}\n',
                "ancillary": [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights.tobytes())],
            },
        )
        worker.start()
        try:
            result = self._run_client("verify")
        finally:
            os.close(descriptor)
        worker.join(5)
        listener.close()
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(b"database-secret", result.stdout)
        self._assert_preserved()

    def test_malformed_requests_and_process_local_quota(self):
        self._start()
        module = sys.modules["recovery_service"]
        module._peer_credentials = lambda descriptor: (
            CLIENT_UID,
            CLIENT_GID,
            os.fsencode(self.client),
        )
        for payload in (b'{"op":"restore"}', b'{"op":"verify","path":"x"}', b"X" * 65):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.connect(str(self.socket_path))
            probe.sendall(payload)
            probe.shutdown(socket.SHUT_WR)
            response = probe.recv(96)
            probe.close()
            self.assertEqual(
                response,
                b'{"code":"invalid_request","status":"refused"}\n',
            )
        self._stop()

        self._start()
        for _ in range(recovery_entrypoint.VERIFY_QUOTA):
            result = self._run_client("verify")
            self.assertEqual(
                result.stdout,
                b'{"code":"anchor_invalid","status":"refused"}\n',
            )
        exhausted = self._run_client("verify")
        self.assertEqual(
            exhausted.stdout,
            b'{"code":"quota_exceeded","status":"refused"}\n',
        )
        self._stop()
        self._start()
        reset = self._run_client("verify")
        self.assertEqual(
            reset.stdout,
            b'{"code":"anchor_invalid","status":"refused"}\n',
        )
        self._assert_preserved()

    def test_release_client_build_is_fixed_and_accepts_no_extra_arguments(self):
        source = CLIENT_SOURCE.read_text(encoding="utf-8")
        self.assertIn(FIXED_SOCKET, source)
        self.assertIn("SO_RCVTIMEO", source)
        binary = self.root / "release-client"
        completed = subprocess.run(
            [
                "cc",
                "-isysroot",
                SDK,
                "-O2",
                "-o",
                str(binary),
                str(CLIENT_SOURCE),
            ],
            check=False,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        forbidden = subprocess.run(
            [
                "cc",
                "-isysroot",
                SDK,
                "-DRECOVERY_SOCKET_PATH=\"/private/tmp/forbidden\"",
                "-o",
                str(self.root / "forbidden-client"),
                str(CLIENT_SOURCE),
            ],
            check=False,
            capture_output=True,
        )
        self.assertNotEqual(forbidden.returncode, 0)
        extra = subprocess.run(
            [str(self.client), "verify", "path=/private/tmp/x"],
            check=False,
            capture_output=True,
        )
        self.assertEqual(extra.returncode, 1)
        self.assertEqual(extra.stdout, b"")
        binary.unlink()
        self._assert_preserved()


if __name__ == "__main__":
    unittest.main()
