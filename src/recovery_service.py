"""Disposable recovery service adapter.

The adapter exposes snapshot and verify on one Unix socket. Requests are
exact byte strings, and every path, key, identity, generation, and filename
comes from the in-process configuration. Restore, signing, and database
bytes are not part of the socket grammar.

MACOS_ACCOUNT_SEPARATION_ENFORCED is false. Peer uid, gid, and executable
checks are real kernel queries, but a disposable test runs them inside one
macOS account. They do not create a recovery account, change ACLs, or prove
that the anchor directory is outside the store writer's control.
"""

from __future__ import annotations

import array
import ctypes
import ctypes.util
import errno
import hashlib
import hmac
import json
import os
import socket
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

from max_smart_db.config import PRODUCTION_DATABASE_PATH, PROJECT_ROOT
from provider_fence import PRODUCTION_FENCE_PATH, _APPROVED_PIN_DIRECTORY
from recovery_helper import (
    RecoveryHelperError,
    create_delegated_recovery_snapshot,
    verify_recovery_point,
)


SIMULATED_SERVICE_BOUNDARY = "simulated"
MACOS_ACCOUNT_SEPARATION_ENFORCED = False
ANCHOR_FORMAT = "max-smart/recovery-service-anchor/v1"
SNAPSHOT_REQUEST = b'{"op":"snapshot"}'
VERIFY_REQUEST = b'{"op":"verify"}'
_REQUESTS = {
    SNAPSHOT_REQUEST: "snapshot",
    VERIFY_REQUEST: "verify",
    SNAPSHOT_REQUEST + b"\n": "snapshot",
    VERIFY_REQUEST + b"\n": "verify",
}
_REQUEST_LIMIT = 64
_RESPONSE_LIMIT = 96
_ANCHOR_LIMIT = 512
_GENERATION_LIMIT = 999999
_SOCKET_PATH_LIMIT = 103
_PUBLIC_CODES = frozenset(
    {
        "anchor_contradictory",
        "anchor_invalid",
        "anchor_pending",
        "anchor_uncertain",
        "failed",
        "invalid_request",
        "quota_exceeded",
        "rollback",
        "unauthorized",
    }
)
_ACCEPTED_BODY = "accepted.json"
_ACCEPTED_MAC = "accepted.mac"
_PENDING_BODY = "pending.json"
_PENDING_MAC = "pending.mac"
_UNCERTAIN_BODY = "uncertain.json"
_UNCERTAIN_MAC = "uncertain.mac"
_ANCHOR_NAMES = frozenset(
    {
        _ACCEPTED_BODY,
        _ACCEPTED_MAC,
        _PENDING_BODY,
        _PENDING_MAC,
        _UNCERTAIN_BODY,
        _UNCERTAIN_MAC,
    }
)
_CLOUD_COMPONENTS = frozenset(
    {
        "Dropbox",
        "Google Drive",
        "iCloud Drive",
        "Mobile Documents",
        "OneDrive",
    }
)
_PROTECTED_DIRECTORIES = (
    PROJECT_ROOT,
    PROJECT_ROOT / "data",
    PRODUCTION_DATABASE_PATH.parent,
    PRODUCTION_FENCE_PATH.parent,
    _APPROVED_PIN_DIRECTORY,
)
_PROTECTED_FILES = (
    PRODUCTION_DATABASE_PATH,
    PRODUCTION_FENCE_PATH,
    PROJECT_ROOT / "data" / "current_session.json",
    PROJECT_ROOT / "data" / "fence.trust",
    _APPROVED_PIN_DIRECTORY / "provider-fence.pin",
    _APPROVED_PIN_DIRECTORY / "provider-fence.acceptance",
)
_SOL_LOCAL = 0
_LOCAL_PEERPID = 2


class RecoveryServiceError(RuntimeError):
    """A service operation failed closed."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RecoveryServiceConfiguration:
    """Trusted values supplied by the service, never by a socket request."""

    socket_path: Path
    store: Path
    anchor_directory: Path
    key: bytes
    source_identity: bytes
    store_identity: bytes
    expected_owner_uid: int
    expected_device: int
    expected_inode: int
    allowed_peer_uid: int
    allowed_peer_gid: int
    executable_pin: str
    snapshot_quota: int
    verify_quota: int

    def __post_init__(self):
        paths = (self.socket_path, self.store, self.anchor_directory)
        if any(not isinstance(path, Path) or not path.is_absolute() for path in paths):
            _configuration_invalid()
        if any(_cloud(path) or _protected(path) for path in paths):
            _configuration_invalid()
        if len(os.fsencode(self.socket_path)) > _SOCKET_PATH_LIMIT:
            _configuration_invalid()
        if (
            _overlaps(self.socket_path, self.store)
            or _overlaps(self.socket_path, self.anchor_directory)
            or _overlaps(self.store, self.anchor_directory)
        ):
            _configuration_invalid()
        _require_private_directory(self.socket_path.parent)
        _require_private_directory(self.store)
        _require_private_directory(self.anchor_directory)
        if os.listdir(self.anchor_directory):
            _configuration_invalid()
        if type(self.key) is not bytes or len(self.key) != 32:
            _configuration_invalid()
        if type(self.source_identity) is not bytes or len(self.source_identity) != 32:
            _configuration_invalid()
        if type(self.store_identity) is not bytes or len(self.store_identity) != 32:
            _configuration_invalid()
        for value in (
            self.expected_owner_uid,
            self.expected_device,
            self.expected_inode,
            self.allowed_peer_uid,
            self.allowed_peer_gid,
        ):
            if type(value) is not int or isinstance(value, bool) or value < 0:
                _configuration_invalid()
        if type(self.executable_pin) is not str or not self.executable_pin.startswith("/"):
            _configuration_invalid()
        if "\x00" in self.executable_pin:
            _configuration_invalid()
        for quota in (self.snapshot_quota, self.verify_quota):
            if type(quota) is not int or isinstance(quota, bool) or not 1 <= quota <= 100:
                _configuration_invalid()


class RecoveryService:
    """Serve the two-command grammar until shutdown."""

    def __init__(self, configuration):
        if MACOS_ACCOUNT_SEPARATION_ENFORCED or SIMULATED_SERVICE_BOUNDARY != "simulated":
            raise RecoveryServiceError("configuration_invalid")
        if type(configuration) is not RecoveryServiceConfiguration:
            raise RecoveryServiceError("configuration_invalid")
        self.configuration = configuration
        self._anchor = _AnchorLedger(configuration)
        self._snapshot_count = 0
        self._verify_count = 0
        self._listen_socket = None
        self._socket_identity = None
        self._stop = threading.Event()
        self.ready = threading.Event()

    def serve_forever(self):
        """Bind the configured socket and handle one request at a time."""

        self._bind()
        try:
            while not self._stop.is_set():
                listen = self._listen_socket
                if listen is None:
                    break
                try:
                    connection, _address = listen.accept()
                except OSError:
                    break
                if self._stop.is_set():
                    connection.close()
                    break
                try:
                    self._handle(connection)
                finally:
                    connection.close()
        finally:
            self._unlink_socket()

    def shutdown(self):
        """Stop accepting and remove the socket if it is still ours."""

        self._stop.set()
        listen = self._listen_socket
        self._listen_socket = None
        if listen is not None:
            try:
                listen.close()
            except OSError:
                pass

    def _bind(self):
        path = self.configuration.socket_path
        if _status(path) is not None:
            raise RecoveryServiceError("configuration_invalid")
        listen = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listen.bind(os.fsdecode(os.fsencode(path)))
            os.chmod(path, 0o600)
            listed = path.lstat()
            if (
                not stat.S_ISSOCK(listed.st_mode)
                or stat.S_ISLNK(listed.st_mode)
                or listed.st_uid != os.geteuid()
                or stat.S_IMODE(listed.st_mode) != 0o600
            ):
                raise RecoveryServiceError("configuration_invalid")
            listen.listen(1)
        except OSError:
            listen.close()
            self._socket_identity = None
            self._unlink_socket()
            raise RecoveryServiceError("configuration_invalid")
        self._listen_socket = listen
        self._socket_identity = (listed.st_dev, listed.st_ino)
        self.ready.set()

    def _unlink_socket(self):
        identity = self._socket_identity
        path = self.configuration.socket_path
        if identity is None:
            return
        try:
            listed = path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            return
        if stat.S_ISLNK(listed.st_mode):
            return
        if (listed.st_dev, listed.st_ino) != identity:
            return
        try:
            path.unlink()
        except OSError:
            return

    def _handle(self, connection):
        connection.settimeout(2)
        authorized = self._peer_is_authorized(connection)
        try:
            operation = self._read_request(connection) if authorized else None
            if not authorized:
                try:
                    self._read_request(connection)
                except RecoveryServiceError:
                    pass
                self._send(connection, _failure("unauthorized"))
                return
            self._consume(operation)
            if operation == "snapshot":
                generation = self._snapshot()
            else:
                generation = self._verify()
        except RecoveryServiceError as error:
            self._send(connection, _failure(_public_code(error.code)))
            return
        except RecoveryHelperError as error:
            code = "rollback" if error.code == "recovery_rollback" else "failed"
            self._send(connection, _failure(code))
            return
        except Exception:
            self._send(connection, _failure("failed"))
            return
        self._send(connection, _success(generation))

    def _peer_is_authorized(self, connection):
        try:
            uid, gid, executable = _peer_credentials(connection.fileno())
        except OSError:
            return False
        configured = self.configuration
        if uid != configured.allowed_peer_uid or gid != configured.allowed_peer_gid:
            return False
        expected = os.fsencode(configured.executable_pin)
        return len(executable) == len(expected) and hmac.compare_digest(executable, expected)

    def _read_request(self, connection):
        chunks = []
        received = 0
        while received <= _REQUEST_LIMIT:
            try:
                data, ancillary, flags, _address = connection.recvmsg(
                    _REQUEST_LIMIT + 1,
                    1024,
                )
            except socket.timeout as error:
                raise RecoveryServiceError("invalid_request") from error
            except OSError as error:
                raise RecoveryServiceError("invalid_request") from error
            _discard_received_descriptors(ancillary)
            if ancillary or flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC):
                raise RecoveryServiceError("invalid_request")
            if not data:
                break
            chunks.append(data)
            received += len(data)
        payload = b"".join(chunks)
        if len(payload) > _REQUEST_LIMIT:
            raise RecoveryServiceError("invalid_request")
        try:
            return _REQUESTS[payload]
        except KeyError as error:
            raise RecoveryServiceError("invalid_request") from error

    def _consume(self, operation):
        if operation == "snapshot":
            if self._snapshot_count >= self.configuration.snapshot_quota:
                raise RecoveryServiceError("quota_exceeded")
            self._snapshot_count += 1
            return
        if self._verify_count >= self.configuration.verify_quota:
            raise RecoveryServiceError("quota_exceeded")
        self._verify_count += 1

    def _snapshot(self):
        accepted = self._anchor.require_ready()
        expected = accepted + 1
        if expected > _GENERATION_LIMIT:
            raise RecoveryServiceError("failed")
        self._anchor.begin(expected)
        point = create_delegated_recovery_snapshot(
            self.configuration.store,
            self.configuration.key,
            source_identity=self.configuration.source_identity,
            store_identity=self.configuration.store_identity,
            expected_owner_uid=self.configuration.expected_owner_uid,
            expected_device=self.configuration.expected_device,
            expected_inode=self.configuration.expected_inode,
            minimum_generation=None if accepted == 0 else accepted,
        )
        if point.generation != expected:
            raise RecoveryServiceError("anchor_contradictory")
        self._anchor.commit(expected)
        return expected

    def _verify(self):
        accepted = self._anchor.require_ready()
        if accepted == 0:
            raise RecoveryServiceError("anchor_invalid")
        point = verify_recovery_point(
            self.configuration.store,
            self.configuration.key,
            source_identity=self.configuration.source_identity,
            store_identity=self.configuration.store_identity,
            minimum_generation=accepted,
        )
        if point.generation != accepted:
            raise RecoveryServiceError("anchor_contradictory")
        return accepted

    def _send(self, connection, payload):
        try:
            connection.sendall(payload)
        except OSError:
            return


class _AnchorLedger:
    """Authenticated generation floor stored outside the snapshot store."""

    def __init__(self, configuration):
        self.path = configuration.anchor_directory
        self.key = configuration.key
        self.source_identity = configuration.source_identity.hex()
        self.store_identity = configuration.store_identity.hex()

    def require_ready(self):
        names = self._names()
        if not names:
            return 0
        if not names <= _ANCHOR_NAMES:
            raise RecoveryServiceError("anchor_contradictory")
        if _UNCERTAIN_BODY in names or _UNCERTAIN_MAC in names:
            self._require_pair(_UNCERTAIN_BODY, _UNCERTAIN_MAC, "uncertain")
            raise RecoveryServiceError("anchor_uncertain")
        pending = _PENDING_BODY in names or _PENDING_MAC in names
        accepted = _ACCEPTED_BODY in names or _ACCEPTED_MAC in names
        if pending:
            pending_generation = self._require_pair(
                _PENDING_BODY,
                _PENDING_MAC,
                "pending",
            )
            if accepted:
                accepted_generation = self._require_pair(
                    _ACCEPTED_BODY,
                    _ACCEPTED_MAC,
                    "accepted",
                )
                if accepted_generation != pending_generation:
                    raise RecoveryServiceError("anchor_contradictory")
            raise RecoveryServiceError("anchor_pending")
        if not accepted:
            raise RecoveryServiceError("anchor_contradictory")
        return self._require_pair(_ACCEPTED_BODY, _ACCEPTED_MAC, "accepted")

    def begin(self, generation):
        if self.require_ready() != generation - 1:
            raise RecoveryServiceError("anchor_contradictory")
        self._write_pair(_PENDING_BODY, _PENDING_MAC, generation, "pending")
        self._fsync_directory(record_uncertainty=True, generation=generation)

    def commit(self, generation):
        body = self._body(generation, "accepted")
        mac = _mac(self.key, body)
        body_temp = self.path / "accepted.json.tmp"
        mac_temp = self.path / "accepted.mac.tmp"
        self._write_exclusive(body_temp, body)
        self._write_exclusive(mac_temp, mac)
        os.replace(body_temp, self.path / _ACCEPTED_BODY)
        os.replace(mac_temp, self.path / _ACCEPTED_MAC)
        self._fsync_directory(record_uncertainty=True, generation=generation)
        self._unlink(_PENDING_BODY)
        self._unlink(_PENDING_MAC)
        self._fsync_directory(record_uncertainty=True, generation=generation)

    def _require_pair(self, body_name, mac_name, state):
        if (body_name in self._names()) ^ (mac_name in self._names()):
            raise RecoveryServiceError("anchor_contradictory")
        body = self._read_file(self.path / body_name)
        presented = self._read_file(self.path / mac_name)
        if not _mac_matches(self.key, body, presented):
            raise RecoveryServiceError("anchor_invalid")
        try:
            payload = json.loads(body.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RecoveryServiceError("anchor_invalid") from error
        if type(payload) is not dict or _canonical(payload) != body:
            raise RecoveryServiceError("anchor_invalid")
        expected = {
            "format",
            "generation",
            "source_identity",
            "state",
            "store_identity",
        }
        if set(payload) != expected or payload["format"] != ANCHOR_FORMAT:
            raise RecoveryServiceError("anchor_invalid")
        if payload["state"] != state:
            raise RecoveryServiceError("anchor_contradictory")
        generation = payload["generation"]
        if type(generation) is not int or isinstance(generation, bool):
            raise RecoveryServiceError("anchor_invalid")
        if generation < 1 or generation > _GENERATION_LIMIT:
            raise RecoveryServiceError("anchor_invalid")
        if not _same_text(payload["source_identity"], self.source_identity):
            raise RecoveryServiceError("anchor_invalid")
        if not _same_text(payload["store_identity"], self.store_identity):
            raise RecoveryServiceError("anchor_invalid")
        return generation

    def _write_pair(self, body_name, mac_name, generation, state):
        body = self._body(generation, state)
        self._write_exclusive(self.path / body_name, body)
        self._write_exclusive(self.path / mac_name, _mac(self.key, body))

    def _body(self, generation, state):
        return _canonical(
            {
                "format": ANCHOR_FORMAT,
                "generation": generation,
                "source_identity": self.source_identity,
                "state": state,
                "store_identity": self.store_identity,
            }
        )

    def _write_exclusive(self, path, payload):
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as error:
            raise RecoveryServiceError("anchor_contradictory") from error
        try:
            listed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(listed.st_mode)
                or listed.st_nlink != 1
                or listed.st_uid != os.geteuid()
                or stat.S_IMODE(listed.st_mode) != 0o600
            ):
                raise RecoveryServiceError("anchor_invalid")
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise RecoveryServiceError("anchor_uncertain")
                view = view[written:]
            os.fsync(descriptor)
        except RecoveryServiceError:
            raise
        except OSError as error:
            raise RecoveryServiceError("anchor_uncertain") from error
        finally:
            os.close(descriptor)

    def _read_file(self, path):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as error:
            raise RecoveryServiceError("anchor_invalid") from error
        try:
            listed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(listed.st_mode)
                or stat.S_ISLNK(listed.st_mode)
                or listed.st_uid != os.geteuid()
                or stat.S_IMODE(listed.st_mode) != 0o600
                or listed.st_nlink != 1
                or listed.st_size < 1
                or listed.st_size > _ANCHOR_LIMIT
            ):
                raise RecoveryServiceError("anchor_invalid")
            payload = os.read(descriptor, _ANCHOR_LIMIT + 1)
            if len(payload) != listed.st_size:
                raise RecoveryServiceError("anchor_invalid")
            return payload
        finally:
            os.close(descriptor)

    def _names(self):
        self._require_directory()
        try:
            return {entry.name for entry in os.scandir(self.path)}
        except OSError as error:
            raise RecoveryServiceError("anchor_invalid") from error

    def _require_directory(self):
        try:
            listed = self.path.lstat()
        except OSError as error:
            raise RecoveryServiceError("anchor_invalid") from error
        if (
            stat.S_ISLNK(listed.st_mode)
            or not stat.S_ISDIR(listed.st_mode)
            or listed.st_uid != os.geteuid()
            or stat.S_IMODE(listed.st_mode) != 0o700
        ):
            raise RecoveryServiceError("anchor_invalid")

    def _fsync_directory(self, *, record_uncertainty, generation):
        try:
            descriptor = os.open(
                self.path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
        except OSError as error:
            raise RecoveryServiceError("anchor_uncertain") from error
        try:
            os.fsync(descriptor)
        except OSError as error:
            if record_uncertainty:
                try:
                    self._write_pair(
                        _UNCERTAIN_BODY,
                        _UNCERTAIN_MAC,
                        generation,
                        "uncertain",
                    )
                except RecoveryServiceError:
                    pass
            raise RecoveryServiceError("anchor_uncertain") from error
        finally:
            os.close(descriptor)

    def _unlink(self, name):
        try:
            os.unlink(self.path / name)
        except OSError as error:
            raise RecoveryServiceError("anchor_pending") from error


def current_process_executable():
    """Return this process path for a disposable executable pin."""

    return os.fsdecode(_process_executable(os.getpid()))


def _peer_credentials(descriptor):
    library = _libc()
    uid = ctypes.c_uint32()
    gid = ctypes.c_uint32()
    if library.getpeereid(descriptor, ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise OSError(ctypes.get_errno(), "getpeereid")
    pid = ctypes.c_int()
    length = ctypes.c_uint32(ctypes.sizeof(pid))
    result = library.getsockopt(
        descriptor,
        _SOL_LOCAL,
        _LOCAL_PEERPID,
        ctypes.byref(pid),
        ctypes.byref(length),
    )
    if result != 0 or pid.value <= 0:
        raise OSError(ctypes.get_errno(), "LOCAL_PEERPID")
    return uid.value, gid.value, _process_executable(pid.value)


def _process_executable(pid):
    library = _libproc()
    buffer = ctypes.create_string_buffer(4 * 1024)
    length = library.proc_pidpath(pid, buffer, ctypes.sizeof(buffer))
    path = buffer.value
    if length <= 0 or not path or len(path) >= ctypes.sizeof(buffer) - 1:
        raise OSError(ctypes.get_errno() or errno.EOVERFLOW, "proc_pidpath")
    if length != len(path):
        raise OSError(errno.EINVAL, "proc_pidpath")
    return path


def _libc():
    library = getattr(_libc, "cached", None)
    if library is not None:
        return library
    library = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    library.getpeereid.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    library.getpeereid.restype = ctypes.c_int
    library.getsockopt.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    library.getsockopt.restype = ctypes.c_int
    _libc.cached = library
    return library


def _libproc():
    library = getattr(_libproc, "cached", None)
    if library is not None:
        return library
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidpath.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint32,
    ]
    library.proc_pidpath.restype = ctypes.c_int
    _libproc.cached = library
    return library


def _discard_received_descriptors(ancillary):
    for _level, kind, data in ancillary:
        if kind != socket.SCM_RIGHTS:
            continue
        size = array.array("i").itemsize
        rights = array.array("i")
        rights.frombytes(data[: len(data) - (len(data) % size)])
        for descriptor in rights:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _canonical(payload):
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    except (TypeError, UnicodeEncodeError, ValueError) as error:
        raise RecoveryServiceError("anchor_invalid") from error


def _mac(key, payload):
    return hmac.new(key, payload, hashlib.sha256).hexdigest().encode("ascii")


def _mac_matches(key, payload, presented):
    expected = _mac(key, payload)
    return len(presented) == len(expected) and hmac.compare_digest(presented, expected)


def _same_text(presented, expected):
    if type(presented) is not str or len(presented) != len(expected):
        return False
    return hmac.compare_digest(presented, expected)


def _success(generation):
    return _bounded_response({"generation": generation, "status": "ok"})


def _failure(code):
    return _bounded_response({"code": _public_code(code), "status": "refused"})


def _public_code(code):
    if code in _PUBLIC_CODES:
        return code
    return "failed"


def _bounded_response(payload):
    encoded = _canonical(payload) + b"\n"
    if len(encoded) > _RESPONSE_LIMIT:
        return b'{"code":"failed","status":"refused"}\n'
    return encoded


def _configuration_invalid():
    raise RecoveryServiceError("configuration_invalid")


def _require_private_directory(path):
    current = path
    parts = path.parts
    for index in range(1, len(parts)):
        current = Path(*parts[: index + 1])
        try:
            listed = current.lstat()
        except OSError:
            _configuration_invalid()
        if stat.S_ISLNK(listed.st_mode):
            _configuration_invalid()
        if index != len(parts) - 1 and not stat.S_ISDIR(listed.st_mode):
            _configuration_invalid()
    try:
        listed = path.lstat()
    except OSError:
        _configuration_invalid()
    if (
        stat.S_ISLNK(listed.st_mode)
        or not stat.S_ISDIR(listed.st_mode)
        or listed.st_uid != os.geteuid()
        or stat.S_IMODE(listed.st_mode) != 0o700
    ):
        _configuration_invalid()


def _protected(path):
    if path in _PROTECTED_FILES:
        return True
    return any(_overlaps(path, directory) for directory in _PROTECTED_DIRECTORIES)


def _overlaps(path, root):
    if path == root:
        return True
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _cloud(path):
    return any(part in _CLOUD_COMPONENTS for part in path.parts)


def _status(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        _configuration_invalid()
