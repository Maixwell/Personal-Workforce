"""One fixed, host-local fence for the active provider-capable worker.

Operational contract:

- One canonical fence configuration is shared by the worker, the supervisor,
  and every recovery caller. It carries the trusted path and the expected
  device and inode. A different path or a replacement inode is not a new
  lock domain.
- The expected device and inode are recorded when the disposable fence is
  provisioned. Ordinary operation never reads a new trusted identity out of
  a replacement file, and it never deletes, renames, or recreates the fence.
- Every open walks the path from the filesystem root with directory
  descriptors and no-follow flags. A symlink in any component, including the
  final file, is unverifiable.
- The provider-capable worker holds the exclusive POSIX lock until that
  process exits. The supervisor is not the lock owner.
- A busy fence blocks recovery and slot release. Explicit recovery reports
  that the local provider is active. PREPARE and retry creation report the
  model as busy.
- An unverifiable fence blocks recovery and provider dispatch. Missing,
  replaced, unreadable, linked, or permission-invalid files are not treated
  as an idle worker and are not recreated.
- Host remediation requires a separately approved operator action and proof.
  This module has no force-unlock command.
- A host reboot releases local locks. It does not prove that a remote
  provider call was cancelled. Duplicate-charge risk remains after an
  uncertain remote outcome.
"""

import ctypes
import ctypes.util
import errno
import fcntl
import os
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PRODUCTION_DATA = _PROJECT_ROOT / "data"
PRODUCTION_FENCE_PATH = _PRODUCTION_DATA / "provider.fence"
_MAGIC = b"max-smart-provider-fence-v1\n"
_ENV_PATH = "MAX_SMART_PROVIDER_FENCE"
_ENV_DEVICE = "MAX_SMART_PROVIDER_FENCE_DEVICE"
_ENV_INODE = "MAX_SMART_PROVIDER_FENCE_INODE"
_CLOUD_PATH_COMPONENTS = {
    "Dropbox",
    "Google Drive",
    "iCloud Drive",
    "Mobile Documents",
    "OneDrive",
}
_LOCAL_DISK_DEVICE = re.compile(r"disk[0-9]+(s[0-9]+)+")
_MUTEX_GUARD = threading.Lock()
_INODE_MUTEXES = {}
_HELD_GUARD = threading.Lock()
_HELD_BY_IDENTITY = {}
_LOCAL_DEVICES = {}
_WORKER_HELD = None
_INVALID = object()


@dataclass(frozen=True)
class ProviderFenceConfig:
    """Trusted path and device/inode captured outside the fence file."""

    path: str
    device: int
    inode: int


class FenceSafetyError(RuntimeError):
    """A disposable fence could not be prepared. The file was not replaced."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class FenceBusy:
    """Another provider-capable process holds the exclusive fence."""


class FenceUnproven:
    """The fence resource cannot be verified. It must not be treated as idle."""

    def __init__(self, code):
        self.code = code


class FenceHeld:
    """An exclusive lock held by this process. Closing it releases the lock."""

    __slots__ = ("path", "descriptor", "device", "inode", "mutex", "released")

    def __init__(self, path, descriptor, device, inode, mutex):
        self.path = path
        self.descriptor = descriptor
        self.device = device
        self.inode = inode
        self.mutex = mutex
        self.released = False


def prepare_disposable_fence(path):
    """Create one private disposable fence and return its trusted identity.

    The returned device and inode are the identity every later participant
    must be given. An existing file is left untouched and is not adopted.
    This function does not delete or replace a file.
    """

    candidate = _absolute_path(path)
    if candidate is None:
        raise FenceSafetyError("fence_path_invalid")
    if candidate.name == "max_smart.sqlite3":
        raise FenceSafetyError("production_database_refused")
    if _under_production_data(candidate):
        raise FenceSafetyError("production_fence_refused")
    if _cloud_path(candidate):
        raise FenceSafetyError("fence_path_refused")
    parent_fd, name = _open_parent(candidate)
    descriptor = None
    try:
        try:
            listed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            listed = None
        except OSError as error:
            raise FenceSafetyError("fence_create_failed") from error
        if listed is not None:
            if stat.S_ISLNK(listed.st_mode):
                raise FenceSafetyError("fence_symlink")
            raise FenceSafetyError("fence_exists")
        try:
            descriptor = os.open(
                name,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise FenceSafetyError("fence_symlink") from error
            if error.errno == errno.EEXIST:
                raise FenceSafetyError("fence_exists") from error
            raise FenceSafetyError("fence_create_failed") from error
        if os.write(descriptor, _MAGIC) != len(_MAGIC):
            raise FenceSafetyError("fence_create_failed")
        os.fsync(descriptor)
        file_status = os.fstat(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)
    config = ProviderFenceConfig(
        str(candidate),
        file_status.st_dev,
        file_status.st_ino,
    )
    gate = acquire_exclusive(config)
    if type(gate) is not FenceHeld:
        code = gate.code if type(gate) is FenceUnproven else "fence_busy"
        raise FenceSafetyError(code)
    release_fence(gate)
    return config


def publish_shared_fence_config(config):
    """Bootstrap one canonical identity, or repeat that exact identity.

    A different, partial, or corrupt published value is left unchanged.
    Ordinary execution never clears or repairs trust through this function.
    """

    if not _valid_config(config):
        raise FenceSafetyError("fence_identity_missing")
    published = env_fence_config()
    if published is _INVALID:
        raise FenceSafetyError("fence_identity_invalid")
    if published is not None and not _same_config(published, config):
        raise FenceSafetyError("fence_identity_changed")
    configured = _settings_config()
    if configured is _INVALID:
        raise FenceSafetyError("fence_identity_invalid")
    if configured is not None and not _same_config(configured, config):
        raise FenceSafetyError("fence_identity_changed")
    if published is not None:
        return
    os.environ[_ENV_PATH] = config.path
    os.environ[_ENV_DEVICE] = str(config.device)
    os.environ[_ENV_INODE] = str(config.inode)


def env_fence_config():
    """Return the published identity, None when absent, or invalid when broken."""

    path = os.environ.get(_ENV_PATH)
    device = os.environ.get(_ENV_DEVICE)
    inode = os.environ.get(_ENV_INODE)
    if path is None and device is None and inode is None:
        return None
    if type(path) is not str or device is None or inode is None:
        return _INVALID
    try:
        config = ProviderFenceConfig(path, int(device), int(inode))
    except (TypeError, ValueError):
        return _INVALID
    if str(config.device) != device or str(config.inode) != inode:
        return _INVALID
    if not _valid_config(config):
        return _INVALID
    return config


def shared_fence_config():
    """Return the one canonical config, or None when it is missing or split."""

    configured = _settings_config()
    published = env_fence_config()
    if configured is _INVALID or published is _INVALID:
        return None
    if configured is not None and published is not None:
        if not _same_config(configured, published):
            return None
        return configured
    if configured is not None:
        return configured
    return published


def canonical_fence_config(explicit):
    """Accept an explicit config only when it is the established canonical one.

    Missing or inconsistent canonical trust does not make the explicit config
    authoritative. Callers with no explicit config should read
    shared_fence_config directly.
    """

    shared = shared_fence_config()
    if not _valid_config(shared) or not _same_config(explicit, shared):
        return None
    return shared


def fence_matches_config(config):
    """Return whether the path still names the trusted device and inode.

    A thread that already holds this inode keeps the per-inode mutex for the
    whole descriptor lifetime. This function does not open another descriptor
    while that mutex is held, because closing one would drop the process lock.
    """

    if not _valid_config(config):
        return False
    mutex = _mutex_for(config.device, config.inode)
    if not mutex.acquire(blocking=False):
        held = _active_held(config)
        return held is not None and fence_still_matches(held)
    try:
        opened = _open_verified(config)
        if type(opened) is not tuple:
            return False
        os.close(opened[0])
        return True
    finally:
        mutex.release()


def acquire_exclusive(config):
    """Take one inode mutex and a non-blocking exclusive fcntl lock.

    The caller must release a FenceHeld. A busy or unverifiable result holds
    nothing. This function never creates, deletes, or replaces the file.
    The trusted device and inode come from config, not from the file body.
    Classic fcntl locks are process-scoped, so the mutex stops a second user
    in this process from treating the same expected inode as newly acquired.
    """

    if not _valid_config(config):
        return FenceUnproven("fence_identity_missing")
    mutex = _mutex_for(config.device, config.inode)
    if not mutex.acquire(blocking=False):
        return FenceBusy()
    opened = _open_verified(config)
    if type(opened) is not tuple:
        mutex.release()
        return opened
    descriptor, _file_status = opened
    try:
        fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        mutex.release()
        return FenceBusy()
    except OSError as error:
        os.close(descriptor)
        mutex.release()
        if error.errno in (errno.EAGAIN, errno.EACCES):
            return FenceBusy()
        return FenceUnproven("fence_lock_failed")
    try:
        os.set_inheritable(descriptor, False)
        held = FenceHeld(
            config.path,
            descriptor,
            config.device,
            config.inode,
            mutex,
        )
        _register_held(held)
        return held
    except Exception:
        os.close(descriptor)
        mutex.release()
        return FenceUnproven("fence_lock_failed")


def acquire_worker_fence(config):
    """Hold the fence until this provider-capable process exits.

    There is no release helper. Process exit drops the POSIX lock. A second
    acquisition in the same process fails closed. A published canonical
    identity that differs from config also fails closed.
    """

    global _WORKER_HELD
    if _WORKER_HELD is not None:
        return False
    published = env_fence_config()
    if not _valid_config(published) or not _same_config(config, published):
        return False
    gate = acquire_exclusive(config)
    if type(gate) is not FenceHeld:
        return False
    _WORKER_HELD = gate
    return True


def release_fence(held):
    """Unlock and close one held fence. The file stays in place."""

    if type(held) is not FenceHeld or held.released:
        return
    held.released = True
    _forget_held(held)
    try:
        fcntl.lockf(held.descriptor, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(held.descriptor)
    except OSError:
        pass
    held.mutex.release()


def fence_still_matches(held):
    """Return whether the held descriptor and path still name that inode.

    The path walk does not open the fence file, so the caller's lock stays.
    """

    if type(held) is not FenceHeld or held.released:
        return False
    try:
        current = os.fstat(held.descriptor)
        content_ok = _content_matches(held.descriptor)
    except OSError:
        return False
    if not (
        content_ok
        and stat.S_ISREG(current.st_mode)
        and stat.S_IMODE(current.st_mode) == 0o600
        and current.st_uid == os.geteuid()
        and current.st_nlink == 1
        and current.st_dev == held.device
        and current.st_ino == held.inode
    ):
        return False
    return _path_names(held.path, held.device, held.inode)


def _open_verified(config):
    if not _valid_config(config):
        return FenceUnproven("fence_identity_missing")
    candidate = _absolute_path(config.path)
    if candidate is None or str(candidate) != config.path:
        return FenceUnproven("fence_path_invalid")
    if (
        _cloud_path(candidate)
        or _under_production_data(candidate)
        or candidate.name == "max_smart.sqlite3"
    ):
        return FenceUnproven("fence_path_refused")
    try:
        parent_fd, name = _open_parent(candidate)
    except FenceSafetyError as error:
        return FenceUnproven(error.code)
    descriptor = None
    try:
        try:
            listed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return FenceUnproven("fence_missing")
        except OSError:
            return FenceUnproven("fence_unreadable")
        if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
            return FenceUnproven("fence_not_regular")
        if stat.S_IMODE(listed.st_mode) != 0o600:
            return FenceUnproven("fence_permissions")
        if listed.st_uid != os.geteuid():
            return FenceUnproven("fence_owner")
        if listed.st_nlink != 1:
            return FenceUnproven("fence_hard_link")
        if listed.st_dev != config.device or listed.st_ino != config.inode:
            return FenceUnproven("fence_identity")
        try:
            descriptor = os.open(
                name,
                os.O_RDWR | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        except OSError as error:
            if error.errno == errno.ELOOP:
                return FenceUnproven("fence_symlink")
            return FenceUnproven("fence_unreadable")
        current = os.fstat(descriptor)
        if (
            current.st_dev != listed.st_dev
            or current.st_ino != listed.st_ino
            or current.st_dev != config.device
            or current.st_ino != config.inode
            or not stat.S_ISREG(current.st_mode)
            or stat.S_IMODE(current.st_mode) != 0o600
            or current.st_uid != os.geteuid()
            or current.st_nlink != 1
        ):
            os.close(descriptor)
            descriptor = None
            return FenceUnproven("fence_identity")
        if not _content_matches(descriptor):
            os.close(descriptor)
            descriptor = None
            return FenceUnproven("fence_corrupt")
        if not _local_apfs(descriptor, current.st_dev):
            os.close(descriptor)
            descriptor = None
            return FenceUnproven("fence_filesystem")
        os.set_inheritable(descriptor, False)
        opened = descriptor
        descriptor = None
        return opened, current
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        return FenceUnproven("fence_unreadable")
    finally:
        os.close(parent_fd)


def _path_names(path, device, inode):
    candidate = _absolute_path(path)
    if candidate is None or str(candidate) != path:
        return False
    try:
        parent_fd, name = _open_parent(candidate)
    except FenceSafetyError:
        return False
    try:
        listed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    finally:
        os.close(parent_fd)
    return (
        stat.S_ISREG(listed.st_mode)
        and not stat.S_ISLNK(listed.st_mode)
        and listed.st_dev == device
        and listed.st_ino == inode
        and listed.st_nlink == 1
        and stat.S_IMODE(listed.st_mode) == 0o600
        and listed.st_uid == os.geteuid()
    )


def _open_parent(path):
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise FenceSafetyError("fence_platform")
    directory = None
    try:
        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        for part in path.parent.parts[1:]:
            child = _open_directory_component(directory, part)
            os.close(directory)
            directory = child
        _require_private_directory_fd(directory)
        opened = directory
        directory = None
        return opened, path.name
    except Exception:
        if directory is not None:
            os.close(directory)
        raise


def _open_directory_component(parent_fd, name):
    if name in ("", ".", ".."):
        raise FenceSafetyError("fence_directory_invalid")
    try:
        return os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY,
            dir_fd=parent_fd,
        )
    except OSError as error:
        if error.errno == errno.ELOOP or _component_is_symlink(parent_fd, name):
            raise FenceSafetyError("fence_symlink") from error
        if error.errno == errno.ENOENT:
            raise FenceSafetyError("fence_directory_missing") from error
        raise FenceSafetyError("fence_directory_invalid") from error


def _component_is_symlink(parent_fd, name):
    try:
        status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISLNK(status.st_mode)


def _require_private_directory_fd(descriptor):
    status = os.fstat(descriptor)
    if not stat.S_ISDIR(status.st_mode):
        raise FenceSafetyError("fence_directory_invalid")
    if stat.S_IMODE(status.st_mode) != 0o700:
        raise FenceSafetyError("fence_directory_permissions")
    if status.st_uid != os.geteuid():
        raise FenceSafetyError("fence_directory_owner")


def _content_matches(descriptor):
    os.lseek(descriptor, 0, os.SEEK_SET)
    payload = os.read(descriptor, len(_MAGIC) + 1)
    return payload == _MAGIC


def _valid_config(config):
    return (
        type(config) is ProviderFenceConfig
        and type(config.path) is str
        and config.path.startswith("/")
        and type(config.device) is int
        and type(config.inode) is int
        and config.device > 0
        and config.inode > 0
    )


def _same_config(left, right):
    return (
        _valid_config(left)
        and _valid_config(right)
        and left.path == right.path
        and left.device == right.device
        and left.inode == right.inode
    )


def _absolute_path(path):
    if type(path) is not str and not isinstance(path, Path):
        return None
    text = os.fspath(path)
    if type(text) is not str or text == "" or "\x00" in text:
        return None
    if not os.path.isabs(text):
        return None
    return Path(os.path.abspath(text))


def _cloud_path(path):
    return any(part in _CLOUD_PATH_COMPONENTS for part in Path(path).parts)


def _under_production_data(path):
    try:
        resolved = Path(path).resolve()
        data = _PRODUCTION_DATA.resolve()
    except OSError:
        return True
    return resolved == data or data in resolved.parents


def _settings_config():
    try:
        from django.conf import settings

        if not settings.configured:
            return None
    except Exception:
        return None
    if not hasattr(settings, "MAX_SMART_PROVIDER_FENCE_CONFIG"):
        return None
    config = settings.MAX_SMART_PROVIDER_FENCE_CONFIG
    if config is None:
        return None
    if not _valid_config(config):
        return _INVALID
    return config


def _register_held(held):
    with _HELD_GUARD:
        _HELD_BY_IDENTITY[(held.device, held.inode)] = held


def _forget_held(held):
    with _HELD_GUARD:
        key = (held.device, held.inode)
        if _HELD_BY_IDENTITY.get(key) is held:
            del _HELD_BY_IDENTITY[key]


def _active_held(config):
    with _HELD_GUARD:
        held = _HELD_BY_IDENTITY.get((config.device, config.inode))
    if (
        held is None
        or held.released
        or held.path != config.path
        or held.device != config.device
        or held.inode != config.inode
    ):
        return None
    return held


def _mutex_for(device, inode):
    with _MUTEX_GUARD:
        key = (device, inode)
        mutex = _INODE_MUTEXES.get(key)
        if mutex is None:
            mutex = threading.Lock()
            _INODE_MUTEXES[key] = mutex
        return mutex


class _StatFS(ctypes.Structure):
    _fields_ = (
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
    )


_LIBC = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_LIBC.fstatfs.argtypes = (ctypes.c_int, ctypes.POINTER(_StatFS))
_LIBC.fstatfs.restype = ctypes.c_int


def _local_apfs(descriptor, device):
    if device in _LOCAL_DEVICES:
        return _LOCAL_DEVICES[device]
    proven = _probe_local_apfs(descriptor)
    _LOCAL_DEVICES[device] = proven
    return proven


def _probe_local_apfs(descriptor):
    if os.uname().sysname != "Darwin":
        return False
    info = _StatFS()
    if _LIBC.fstatfs(descriptor, ctypes.byref(info)) != 0:
        return False
    if info.f_fstypename.split(b"\x00", 1)[0] != b"apfs":
        return False
    source = info.f_mntfromname.split(b"\x00", 1)[0]
    prefix = b"/dev/"
    if not source.startswith(prefix):
        return False
    try:
        device = source[len(prefix):].decode("ascii")
    except UnicodeDecodeError:
        return False
    return _LOCAL_DISK_DEVICE.fullmatch(device) is not None
