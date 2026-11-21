"""Root-anchored entrypoint for the accepted recovery service adapter.

The production entrypoint, frozen application modules, source metadata, and
native client are rooted in a separately installed root-owned path.  The
recovery account owns only its private key, identities, store, anchor, and
socket directory.  Executable-path pinning constrains client identity; it
does not authenticate the application that invoked the client.
"""

from __future__ import annotations

import grp
import hashlib
import json
import os
import pwd
import re
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path


OS_ACCOUNT_SEPARATION_ENFORCED = False
INSTALL_ROOT = Path("/Library/Application Support/MaxSmartRecovery")
ENTRYPOINT_PATH = INSTALL_ROOT / "code" / "recovery_entrypoint.py"
CLIENT_PATH = INSTALL_ROOT / "bin" / "max-smart-recovery-client"
TRUSTED_INTERPRETER_IMAGE = Path(
    "/Library/Developer/CommandLineTools/Library/Frameworks/"
    "Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python"
)
TRUSTED_PYTHON_PREFIX = Path(
    "/Library/Developer/CommandLineTools/Library/Frameworks/"
    "Python3.framework/Versions/3.9"
)
TRUSTED_INTERPRETER_BOUNDARY = Path("/")
TRUSTED_INTERPRETER_OWNER_UID = 0
TRUSTED_INSTALL_BOUNDARY = Path("/")
TRUSTED_OWNER_UID = 0
CLIENT_UID = 501
CLIENT_GID = 20
SNAPSHOT_QUOTA = 4
VERIFY_QUOTA = 20
METADATA_FORMAT = "max-smart/recovery-source-metadata/v1"
_SECRET_LENGTH = 32
_SOCKET_NAME = "s"
_USERNAME = re.compile(r"[A-Za-z0-9._-]+")
_ACL_LINE = re.compile(r"^\s*(\d+):\s+(.+?)\s*$")
_REFUSED_ROOTS = (
    Path("/Users/smart/Projects"),
    Path("/Users/smart/Library/Application Support/MaxSmartTrust"),
)
_FROZEN_SHA256 = {
    "recovery_service.py": "5d8ba06abd8c68527f990c57498e2791bc1cd5a135e99a302f28ab3fc6c5b2bf",
    "recovery_helper.py": "f5c7ce4d94f33d071de84f752a95ae43e7127c522c0f313f3ac8a5d9d6c27e97",
    "provider_fence.py": "8f54b98b119ded69f85c9c1f13610fc3ee9819220e00b66fe2aeaa24ea4dc32f",
    "max_smart_db/__init__.py": "ddcd59d18095127d1419166c39ac06803b3ad51789b65a3f5e4c84c8b39573a8",
    "max_smart_db/config.py": "be8dd4393ed3a9f5a189ed066bc4555ddf7a0fe2b05247c1f2371d39fb2ee5d0",
    "max_smart_db/safety.py": "251cefdc70f3b83b3b95e1e6fb8a4fe59e48f921c906cfffadd8bab685e42f4d",
}
_LOCAL_MODULES = (
    "recovery_service",
    "recovery_helper",
    "provider_fence",
    "max_smart_db",
    "max_smart_db.config",
    "max_smart_db.safety",
)


class EntrypointError(RuntimeError):
    """Startup failed closed before an ACL-verified listener was available."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def main():
    """Start only the fixed root-anchored production installation."""

    if len(sys.argv) != 1:
        raise SystemExit(1)
    service, thread = start_service(INSTALL_ROOT, CLIENT_PATH)
    try:
        thread.join()
    finally:
        service.shutdown()
        thread.join(5)


def start_service(install_root, client_path):
    """Validate the fixed installation and return its service thread."""

    configuration = load_configuration(install_root, client_path)
    from recovery_service import RecoveryService

    class _GuardedService(RecoveryService):
        def __init__(self, configuration):
            super().__init__(configuration)
            self.acl_ready = threading.Event()
            self.start_error = None

        def _bind(self):
            try:
                super()._bind()
                self.ready.clear()
                _ensure_exclusive_acl(
                    self.configuration.socket_path,
                    kind="socket",
                    mode=0o600,
                    client_uid=CLIENT_UID,
                    permission="write",
                    allow_create=True,
                )
                _require_socket_traversal(self.configuration.socket_path)
                self.ready.set()
            except Exception as error:
                self.start_error = error
                self.shutdown()
                self._unlink_socket()
                self.acl_ready.set()
                return
            self.acl_ready.set()

    service = _GuardedService(configuration)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    service.acl_ready.wait(5)
    if service.start_error is not None or not service.ready.is_set():
        service.shutdown()
        thread.join(5)
        _require_listener_absent(configuration.socket_path)
        error = service.start_error
        if isinstance(error, EntrypointError):
            raise error
        raise EntrypointError("startup_failed")
    return service, thread


def load_configuration(install_root, client_path):
    """Build adapter configuration after every mandatory trust check."""

    _require_interpreter()
    _require_runtime()
    root = _require_fixed_installation(install_root, client_path)
    code = _require_frozen_code(root / "code" / "lib")
    client = _require_client(client_path)
    key_dir = _require_private_directory(root / "key")
    identity_dir = _require_private_directory(root / "identity")
    key = _read_secret(key_dir / "recovery.key", os.geteuid())
    source_identity = _read_secret(identity_dir / "source.id", os.geteuid())
    store_identity = _read_secret(identity_dir / "store.id", os.geteuid())
    metadata = _read_metadata(root / "metadata" / "source.json")
    service_dir = _require_private_directory(root / "service")
    store = _require_private_directory(root / "store")
    anchor = _require_private_directory(root / "anchor")
    _ensure_exclusive_acl(
        service_dir,
        kind="directory",
        mode=0o700,
        client_uid=CLIENT_UID,
        permission="search",
        allow_create=True,
    )
    socket_path = service_dir / _SOCKET_NAME
    if len(os.fsencode(socket_path)) > 103:
        _fail("installation_invalid")
    _require_socket_traversal(socket_path)
    _prepare_stale_socket(socket_path, service_dir)
    _install_import_path(code)
    from recovery_service import RecoveryServiceConfiguration

    loaded = Path(sys.modules["recovery_service"].__file__).absolute()
    if loaded != (code / "recovery_service.py").absolute():
        _fail("module_untrusted")
    return RecoveryServiceConfiguration(
        socket_path=socket_path,
        store=store,
        anchor_directory=anchor,
        key=key,
        source_identity=source_identity,
        store_identity=store_identity,
        expected_owner_uid=metadata["owner_uid"],
        expected_device=metadata["device"],
        expected_inode=metadata["inode"],
        allowed_peer_uid=CLIENT_UID,
        allowed_peer_gid=CLIENT_GID,
        executable_pin=str(client),
        snapshot_quota=SNAPSHOT_QUOTA,
        verify_quota=VERIFY_QUOTA,
    )


def _require_runtime():
    flags = sys.flags
    if not (
        flags.isolated
        and flags.no_site
        and flags.ignore_environment
        and flags.no_user_site
    ):
        _fail("runtime_untrusted")
    for name in (
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSAFEPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
    ):
        if os.environ.get(name):
            _fail("runtime_untrusted")
    if "sitecustomize" in sys.modules or "usercustomize" in sys.modules:
        _fail("runtime_untrusted")
    if Path(sys.prefix).absolute() != TRUSTED_PYTHON_PREFIX:
        _fail("runtime_untrusted")
    for entry in sys.path:
        if entry == "" or entry == ".":
            _fail("runtime_untrusted")
        candidate = Path(entry).absolute()
        if not _is_within(candidate, TRUSTED_PYTHON_PREFIX):
            _fail("runtime_untrusted")
        if "site-packages" in candidate.parts:
            _fail("runtime_untrusted")
        exists = candidate.exists()
        checked = candidate if exists else candidate.parent
        kind = "file" if exists and candidate.is_file() else "directory"
        _require_trusted_chain(
            checked,
            TRUSTED_INTERPRETER_BOUNDARY,
            TRUSTED_INTERPRETER_OWNER_UID,
            leaf_kind=kind,
            code="runtime_untrusted",
        )


def _require_interpreter():
    image = _current_interpreter_image()
    if image != TRUSTED_INTERPRETER_IMAGE:
        _fail("interpreter_untrusted")
    _require_trusted_chain(
        image,
        TRUSTED_INTERPRETER_BOUNDARY,
        TRUSTED_INTERPRETER_OWNER_UID,
        leaf_kind="file",
        code="interpreter_untrusted",
    )


def _current_interpreter_image():
    import ctypes

    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidpath.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint32,
    ]
    library.proc_pidpath.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(4 * 1024)
    length = library.proc_pidpath(os.getpid(), buffer, ctypes.sizeof(buffer))
    value = buffer.value
    if length <= 0 or len(value) != length:
        _fail("interpreter_untrusted")
    return Path(os.fsdecode(value))


def _current_entrypoint_source():
    return Path(__file__).absolute()


def _require_fixed_installation(root, client_path):
    if not isinstance(root, Path) or not isinstance(client_path, Path):
        _fail("installation_invalid")
    if not root.is_absolute() or not client_path.is_absolute():
        _fail("installation_invalid")
    if root != INSTALL_ROOT or client_path != CLIENT_PATH:
        _fail("installation_refused")
    if _current_entrypoint_source() != ENTRYPOINT_PATH:
        _fail("entrypoint_untrusted")
    for refused in _REFUSED_ROOTS:
        if root == refused or refused in root.parents:
            _fail("installation_refused")
    _require_trusted_chain(
        root,
        TRUSTED_INSTALL_BOUNDARY,
        TRUSTED_OWNER_UID,
        leaf_kind="directory",
        code="installation_invalid",
    )
    _require_trusted_chain(
        ENTRYPOINT_PATH,
        TRUSTED_INSTALL_BOUNDARY,
        TRUSTED_OWNER_UID,
        leaf_kind="file",
        code="entrypoint_untrusted",
    )
    return root


def _require_frozen_code(code):
    _require_trusted_chain(
        code,
        TRUSTED_INSTALL_BOUNDARY,
        TRUSTED_OWNER_UID,
        leaf_kind="directory",
        code="module_untrusted",
    )
    expected_top = {
        "recovery_service.py",
        "recovery_helper.py",
        "provider_fence.py",
        "max_smart_db",
    }
    expected_package = {"__init__.py", "config.py", "safety.py"}
    try:
        if set(os.listdir(code)) != expected_top:
            _fail("module_untrusted")
        package = code / "max_smart_db"
        if set(os.listdir(package)) != expected_package:
            _fail("module_untrusted")
    except OSError:
        _fail("module_untrusted")
    _require_trusted_chain(
        package,
        TRUSTED_INSTALL_BOUNDARY,
        TRUSTED_OWNER_UID,
        leaf_kind="directory",
        code="module_untrusted",
    )
    for name, expected in _FROZEN_SHA256.items():
        path = code / name
        _require_trusted_chain(
            path,
            TRUSTED_INSTALL_BOUNDARY,
            TRUSTED_OWNER_UID,
            leaf_kind="file",
            code="module_untrusted",
        )
        if not _constant_equal(_hash_file(path, TRUSTED_OWNER_UID), expected):
            _fail("module_untrusted")
    return code


def _require_client(path):
    if path != CLIENT_PATH:
        _fail("client_untrusted")
    _require_trusted_chain(
        path,
        TRUSTED_INSTALL_BOUNDARY,
        TRUSTED_OWNER_UID,
        leaf_kind="file",
        code="client_untrusted",
    )
    if not os.access(path, os.X_OK):
        _fail("client_untrusted")
    return path


def _hash_file(path, owner_uid):
    before = path.lstat()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _fail("module_untrusted")
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != owner_uid
            or stat.S_IMODE(opened.st_mode) & 0o022
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            _fail("module_untrusted")
        while True:
            block = os.read(descriptor, 128 * 1024)
            if not block:
                break
            digest.update(block)
        after = path.lstat()
        if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            _fail("module_untrusted")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _read_secret(path, owner_uid):
    listed = _required_status(path, "metadata_missing", "metadata_invalid")
    if (
        stat.S_ISLNK(listed.st_mode)
        or not stat.S_ISREG(listed.st_mode)
        or listed.st_uid != owner_uid
        or stat.S_IMODE(listed.st_mode) != 0o400
        or listed.st_size != _SECRET_LENGTH
        or _acl_entries(path)
    ):
        _fail("metadata_invalid")
    data = path.read_bytes()
    if len(data) != _SECRET_LENGTH:
        _fail("metadata_invalid")
    return data


def _read_metadata(path):
    _required_status(path, "metadata_missing", "metadata_invalid")
    _require_trusted_chain(
        path,
        TRUSTED_INSTALL_BOUNDARY,
        TRUSTED_OWNER_UID,
        leaf_kind="file",
        code="metadata_invalid",
    )
    listed = path.lstat()
    if stat.S_IMODE(listed.st_mode) != 0o400 or listed.st_size > 512:
        _fail("metadata_invalid")
    try:
        payload = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail("metadata_invalid")
    expected = {"device", "format", "inode", "owner_uid"}
    if type(payload) is not dict or set(payload) != expected:
        _fail("metadata_invalid")
    if payload["format"] != METADATA_FORMAT:
        _fail("metadata_invalid")
    for name in ("device", "inode", "owner_uid"):
        value = payload[name]
        if type(value) is not int or isinstance(value, bool) or value < 0:
            _fail("metadata_invalid")
    if payload["owner_uid"] != CLIENT_UID or payload["inode"] < 1:
        _fail("metadata_invalid")
    return payload


def _require_private_directory(path):
    listed = _required_status(path, "installation_invalid", "installation_invalid")
    if (
        stat.S_ISLNK(listed.st_mode)
        or not stat.S_ISDIR(listed.st_mode)
        or listed.st_uid != os.geteuid()
        or stat.S_IMODE(listed.st_mode) != 0o700
    ):
        _fail("installation_invalid")
    return path


def _require_trusted_chain(path, boundary, owner_uid, *, leaf_kind, code):
    if not path.is_absolute() or not boundary.is_absolute() or not _is_within(path, boundary):
        _fail(code)
    relative = path.relative_to(boundary)
    current = boundary
    components = [boundary]
    for part in relative.parts:
        current = current / part
        components.append(current)
    for index, component in enumerate(components):
        try:
            listed = component.lstat()
        except OSError:
            _fail(code)
        final = index == len(components) - 1
        required_kind = leaf_kind if final else "directory"
        if (
            stat.S_ISLNK(listed.st_mode)
            or listed.st_uid != owner_uid
            or stat.S_IMODE(listed.st_mode) & 0o022
            or (required_kind == "directory" and not stat.S_ISDIR(listed.st_mode))
            or (required_kind == "file" and not stat.S_ISREG(listed.st_mode))
            or _acl_entries(component)
        ):
            _fail(code)


_DISPOSABLE_STICKY_TEMP = Path("/private/tmp")
_ACL_SUBJECT = re.compile(
    r"^(?:user:(?P<user>[A-Za-z0-9._-]+)|group:(?P<group>[A-Za-z0-9._-]+)|everyone) "
    r"(?P<action>allow|deny) (?P<rights>[a-z_,]+)$"
)


def _require_socket_traversal(socket_path):
    """Require UID 501 search across every directory above the socket."""

    service_dir = socket_path.parent
    install_root = service_dir.parent
    if service_dir.name != "service" or install_root != INSTALL_ROOT:
        _fail("traversal_denied")
    current = Path(socket_path.anchor)
    components = [current]
    for part in service_dir.relative_to(current).parts:
        current = current / part
        components.append(current)
    for component in components:
        try:
            listed = component.lstat()
        except OSError:
            _fail("traversal_denied")
        entries = _acl_entries(component)
        mode = stat.S_IMODE(listed.st_mode)
        if stat.S_ISLNK(listed.st_mode) or not stat.S_ISDIR(listed.st_mode):
            _fail("traversal_denied")
        if component == install_root:
            if listed.st_uid != TRUSTED_OWNER_UID or mode != 0o711 or entries:
                _fail("traversal_denied")
        elif component == service_dir:
            expected = (f"user:{_account_name(CLIENT_UID)} allow search",)
            if listed.st_uid != os.geteuid() or mode != 0o700 or entries != expected:
                _fail("traversal_denied")
        else:
            if listed.st_uid not in (0, TRUSTED_OWNER_UID) or entries:
                _fail("traversal_denied")
            if mode & 0o022 and (
                component != _DISPOSABLE_STICKY_TEMP or not mode & stat.S_ISVTX
            ):
                _fail("traversal_denied")
        if not _client_can_traverse(listed, entries):
            _fail("traversal_denied")


def _client_can_traverse(listed, entries):
    """Model client search without treating owner rights as client rights."""

    client_name = _account_name(CLIENT_UID)
    try:
        client_group = grp.getgrgid(CLIENT_GID).gr_name
    except KeyError:
        return False
    parsed = []
    for entry in entries:
        match = _ACL_SUBJECT.fullmatch(entry)
        if match is None:
            return False
        parsed.append((match, set(match.group("rights").split(","))))
    for match, rights in parsed:
        if match.group("action") == "deny" and rights & {"search", "execute"}:
            subject_user = match.group("user")
            subject_group = match.group("group")
            if (
                subject_user == client_name
                or subject_group == client_group
                or (subject_user is None and subject_group is None)
            ):
                return False
    mode = stat.S_IMODE(listed.st_mode)
    if mode & 0o001 or (listed.st_gid == CLIENT_GID and mode & 0o010):
        return True
    return any(
        match.group("action") == "allow"
        and match.group("user") == client_name
        and rights == {"search"}
        for match, rights in parsed
    )


def _prepare_stale_socket(path, service_dir):
    try:
        listed = path.lstat()
    except FileNotFoundError:
        return
    parent = service_dir.lstat()
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISSOCK(listed.st_mode):
        _fail("socket_refused")
    if listed.st_uid != os.geteuid() or path.parent != service_dir:
        _fail("socket_refused")
    if (path.parent.lstat().st_dev, path.parent.lstat().st_ino) != (
        parent.st_dev,
        parent.st_ino,
    ):
        _fail("socket_refused")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(os.fsdecode(os.fsencode(path)))
    except ConnectionRefusedError:
        stale = True
    except OSError:
        _fail("socket_refused")
    else:
        stale = False
    finally:
        probe.close()
    if not stale:
        _fail("socket_busy")
    current = path.lstat()
    if (
        (current.st_dev, current.st_ino) != (listed.st_dev, listed.st_ino)
        or stat.S_ISLNK(current.st_mode)
        or not stat.S_ISSOCK(current.st_mode)
        or current.st_uid != os.geteuid()
    ):
        _fail("socket_refused")
    path.unlink()


def _ensure_exclusive_acl(path, *, kind, mode, client_uid, permission, allow_create):
    expected = f"user:{_account_name(client_uid)} allow {permission}"
    listed = _require_acl_identity(path, kind, mode)
    entries = _acl_entries(path)
    if not entries and allow_create:
        identity = (listed.st_dev, listed.st_ino)
        _apply_acl(path, expected, identity, kind, mode)
        entries = _acl_entries(path)
    if entries != (expected,):
        _fail("socket_acl")
    current = _require_acl_identity(path, kind, mode)
    if (current.st_dev, current.st_ino) != (listed.st_dev, listed.st_ino):
        _fail("socket_acl")


def _apply_acl(path, entry, identity, kind, mode):
    current = _require_acl_identity(path, kind, mode)
    if (current.st_dev, current.st_ino) != identity:
        _fail("socket_acl")
    completed = subprocess.run(
        ["/bin/chmod", "+a", entry, os.fsdecode(path)],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        _fail("socket_acl")
    after = _require_acl_identity(path, kind, mode)
    if (after.st_dev, after.st_ino) != identity:
        _fail("socket_acl")


def _require_acl_identity(path, kind, mode):
    try:
        listed = path.lstat()
    except OSError:
        _fail("socket_acl")
    correct_kind = stat.S_ISDIR(listed.st_mode) if kind == "directory" else stat.S_ISSOCK(listed.st_mode)
    if (
        stat.S_ISLNK(listed.st_mode)
        or not correct_kind
        or listed.st_uid != os.geteuid()
        or stat.S_IMODE(listed.st_mode) != mode
    ):
        _fail("socket_acl")
    return listed


def _acl_entries(path):
    completed = subprocess.run(
        ["/bin/ls", "-led", os.fsdecode(path)],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        _fail("installation_invalid")
    lines = completed.stdout.decode("utf-8", "strict").splitlines()[1:]
    entries = []
    indexes = []
    for line in lines:
        match = _ACL_LINE.fullmatch(line)
        if match is None:
            if line.strip():
                _fail("installation_invalid")
            continue
        indexes.append(int(match.group(1)))
        entries.append(match.group(2))
    if indexes != list(range(len(indexes))):
        _fail("installation_invalid")
    return tuple(entries)


def _account_name(uid):
    try:
        record = pwd.getpwuid(uid)
    except KeyError:
        _fail("socket_acl")
    name = record.pw_name
    if _USERNAME.fullmatch(name) is None or pwd.getpwnam(name).pw_uid != uid:
        _fail("socket_acl")
    return name


def _install_import_path(code):
    for name in list(sys.modules):
        if name in _LOCAL_MODULES or name.startswith("max_smart_db."):
            del sys.modules[name]
    kept = []
    for entry in sys.path:
        candidate = Path(entry).absolute()
        if not _is_within(candidate, TRUSTED_PYTHON_PREFIX):
            _fail("runtime_untrusted")
        kept.append(entry)
    sys.path[:] = [str(code)] + kept
    import recovery_service  # noqa: F401

    loaded = Path(recovery_service.__file__).absolute()
    if loaded != (code / "recovery_service.py").absolute():
        _fail("module_untrusted")


def _require_listener_absent(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return
    _fail("startup_failed")


def _required_status(path, missing_code, invalid_code):
    try:
        return path.lstat()
    except FileNotFoundError:
        _fail(missing_code)
    except OSError:
        _fail(invalid_code)


def _is_within(path, root):
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _constant_equal(left, right):
    import hmac

    return len(left) == len(right) and hmac.compare_digest(left, right)


def _fail(code):
    raise EntrypointError(code)


if __name__ == "__main__":
    main()
