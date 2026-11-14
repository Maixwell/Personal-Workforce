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
- A disposable trust record stores only the fence path, device, and inode.
  A reader accepts that record only when the record file's own device,
  inode, and content digest match a pin captured at provisioning. The pin
  is not stored in the record. File mode 0600 does not stop the owning
  user from replacing the record, so a process that is given a new pin can
  be pointed at a substituted record.
- A disposable pin file stores that captured pin outside the fence directory.
  Loading reads the pin file and then the trust record. It does not derive
  a pin from the live fence or the live record. Mode 0600 does not stop the
  owning user from replacing the pin file, so a fully replaced pin, record,
  and fence is another chain.
- A pin is successfully provisioned only after its full write, file fsync,
  parent fsync, and descriptor closure all succeed. A failed pin is revoked
  in place by truncating that same descriptor to zero bytes. Truncation
  keeps the directory entry and inode. If that revocation cannot be
  verified, provisioning reports pin_completion_uncertain and does not
  reconstruct a pin. An empty or partial result is incomplete. Rereading
  canonical bytes does not prove that fsync succeeded. A concurrent reader
  or a process interruption can still observe an ambiguous chain. That
  residual is not production-readiness evidence. fence_record_and_pin names
  the surviving files; it does not mean provisioning completed.
"""

import ctypes
import ctypes.util
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PRODUCTION_DATA = _PROJECT_ROOT / "data"
PRODUCTION_FENCE_PATH = _PRODUCTION_DATA / "provider.fence"
_MAGIC = b"max-smart-provider-fence-v1\n"
_TRUST_VERSION = "max-smart-provider-fence-trust-v1"
_TRUST_RECORD_NAME = "fence.trust"
_FENCE_FILE_NAME = "provider.fence"
_TRUST_RECORD_MAX_BYTES = 8192
_PIN_VERSION = "max-smart-provider-fence-pin-v1"
_PIN_FILE_NAME = "provider-fence.pin"
_PIN_MAX_BYTES = 8192
_APPROVED_PIN_DIRECTORY = Path(
    "/Users/smart/Library/Application Support/MaxSmartTrust"
)
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


@dataclass(frozen=True)
class TrustRecordPin:
    """Identity of the trust record, captured outside the record file.

    This pin is the disposable anchor. It is not written into the record.
    Mode 0600 on the record does not replace it.
    """

    path: str
    device: int
    inode: int
    digest: str


@dataclass(frozen=True)
class DisposableFenceTrust:
    """One new fence and the pin for the record that names it."""

    fence: ProviderFenceConfig
    pin: TrustRecordPin


class FenceSafetyError(RuntimeError):
    """A fence trust operation failed closed. Existing files were not replaced."""

    def __init__(self, code, partial=None):
        super().__init__(code)
        self.code = code
        self.partial = partial


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


def provision_disposable_fence_trust(directory):
    """Create one private fence and a separate trust record.

    The directory must already be a private local directory. An existing
    fence or record is left untouched and is not adopted. This function
    does not publish an identity and does not delete or replace a file.
    """

    root = _trust_directory(directory)
    fence_path = root / _FENCE_FILE_NAME
    record_path = root / _TRUST_RECORD_NAME
    parent_fd, _name = _open_parent(fence_path)
    try:
        if not _directory_is_local(parent_fd):
            raise FenceSafetyError("fence_filesystem")
        fence_status = _named_status(parent_fd, _FENCE_FILE_NAME)
        record_status = _named_status(parent_fd, _TRUST_RECORD_NAME)
        if record_status is not None:
            if stat.S_ISLNK(record_status.st_mode):
                raise FenceSafetyError("trust_record_symlink")
            raise FenceSafetyError("trust_record_exists")
        if fence_status is not None:
            if stat.S_ISLNK(fence_status.st_mode):
                raise FenceSafetyError("fence_symlink")
            raise FenceSafetyError("fence_exists")
    finally:
        os.close(parent_fd)
    fence = prepare_disposable_fence(str(fence_path))
    payload = _trust_bytes(fence)
    parent_fd, name = _open_parent(record_path)
    descriptor = None
    try:
        try:
            descriptor = os.open(
                name,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise FenceSafetyError("trust_record_symlink") from error
            if error.errno == errno.EEXIST:
                raise FenceSafetyError("trust_record_exists") from error
            raise FenceSafetyError("trust_record_create_failed") from error
        os.set_inheritable(descriptor, False)
        if os.write(descriptor, payload) != len(payload):
            raise FenceSafetyError("trust_record_create_failed")
        os.fsync(descriptor)
        status = os.fstat(descriptor)
        if not _private_regular(status):
            raise FenceSafetyError("trust_record_permissions")
        if not _local_apfs(descriptor, status.st_dev):
            raise FenceSafetyError("fence_filesystem")
        os.fsync(parent_fd)
        pin = TrustRecordPin(
            str(record_path),
            status.st_dev,
            status.st_ino,
            hashlib.sha256(payload).hexdigest(),
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)
    if not _valid_pin(pin):
        raise FenceSafetyError("trust_record_create_failed")
    return DisposableFenceTrust(fence, pin)


def load_disposable_fence_trust(pin):
    """Return the fence identity stored in a pinned trust record.

    The record is read only. A missing, replaced, or rewritten record is
    refused. The fence file's current inode is never copied into the result
    ahead of the inode stored in the record.
    """

    if not _valid_pin(pin):
        raise FenceSafetyError("trust_pin_invalid")
    record = _absolute_path(pin.path)
    if record is None or str(record) != pin.path:
        raise FenceSafetyError("trust_pin_invalid")
    if record.name != _TRUST_RECORD_NAME:
        raise FenceSafetyError("trust_record_malformed")
    if _under_production_data(record) or _cloud_path(record):
        raise FenceSafetyError("production_fence_refused")
    payload = _read_trust_record(pin)
    if hashlib.sha256(payload).hexdigest() != pin.digest:
        raise FenceSafetyError("trust_record_digest")
    try:
        config = _parse_trust_record(payload)
    except FenceSafetyError as error:
        if error.code == "trust_record_malformed" and (
            payload == b"" or not payload.endswith(b"\n")
        ):
            raise FenceSafetyError("trust_record_truncated") from error
        raise
    if _under_production_data(config.path) or _cloud_path(config.path):
        raise FenceSafetyError("fence_path_refused")
    if not _record_fence_matches(config):
        raise FenceSafetyError("fence_identity")
    return config


def publish_loaded_fence_trust(pin):
    """Publish the fence identity named by a pinned record, once.

    A conflicting identity that is already published is left unchanged.
    """

    config = load_disposable_fence_trust(pin)
    publish_shared_fence_config(config)
    return config


def fence_config_from_trust(pin, explicit=None):
    """Load a pinned record and refuse a different explicit fence identity."""

    config = load_disposable_fence_trust(pin)
    if explicit is not None and not _same_config(explicit, config):
        raise FenceSafetyError("fence_identity_changed")
    return config


@dataclass(frozen=True)
class DisposableFencePin:
    """One fence, its original trust pin, and the separate pin file."""

    fence: ProviderFenceConfig
    pin: TrustRecordPin
    pin_path: str


def provision_disposable_fence_pin(fence_directory, pin_directory):
    """Create a fence, trust record, and original pin file.

    Both directories must already be separate private local directories.
    This function does not create or chmod them, does not publish an
    identity, and does not delete or replace a file. A failure after the
    fence exists leaves that partial chain in place. The partial value names
    surviving files and does not mean the pin was successfully provisioned.
    """

    fence_root = _disposable_directory(fence_directory)
    pin_root = _disposable_directory(pin_directory)
    if _directories_overlap(fence_root, pin_root):
        raise FenceSafetyError("pin_directory_invalid")
    pin_path = pin_root / _PIN_FILE_NAME
    fence_identity = _inspect_private_directory(
        fence_root,
        (
            (_TRUST_RECORD_NAME, "trust_record_symlink", "trust_record_exists"),
            (_FENCE_FILE_NAME, "fence_symlink", "fence_exists"),
        ),
    )
    pin_identity = _inspect_private_directory(
        pin_root,
        ((_PIN_FILE_NAME, "pin_file_symlink", "pin_file_exists"),),
    )
    if fence_identity == pin_identity:
        raise FenceSafetyError("pin_directory_invalid")
    try:
        trust = provision_disposable_fence_trust(str(fence_root))
    except FenceSafetyError as error:
        _reraise_partial(error, fence_root, pin_path)
    payload = _pin_bytes(trust.pin)
    try:
        _create_pin_file(pin_path, payload)
    except FenceSafetyError as error:
        _reraise_partial(error, fence_root, pin_path)
    return DisposableFencePin(trust.fence, trust.pin, str(pin_path))


def load_disposable_fence_pin(path):
    """Return the fence named by a pin file.

    The pin comes from the file bytes. The live fence inode is not copied
    into it, and this function does not create or rewrite any file.
    """

    pin = _read_pin_file(path)
    return load_disposable_fence_trust(pin)


def publish_loaded_fence_pin(path):
    """Publish the fence named by a pin file, once.

    A conflicting identity that is already published is left unchanged.
    """

    config = load_disposable_fence_pin(path)
    publish_shared_fence_config(config)
    return config


def fence_config_from_pin(path, explicit=None):
    """Load a pin file and refuse a different explicit fence identity."""

    config = load_disposable_fence_pin(path)
    if explicit is not None and not _same_config(explicit, config):
        raise FenceSafetyError("fence_identity_changed")
    return config


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


def _trust_directory(directory):
    root = _absolute_path(directory)
    if root is None or root.name == "":
        raise FenceSafetyError("fence_path_invalid")
    if _under_production_data(root):
        raise FenceSafetyError("production_fence_refused")
    if _cloud_path(root):
        raise FenceSafetyError("fence_path_refused")
    return root


def _named_status(parent_fd, name):
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise FenceSafetyError("fence_unreadable") from error


def _directory_is_local(descriptor):
    try:
        status = os.fstat(descriptor)
    except OSError as error:
        raise FenceSafetyError("fence_unreadable") from error
    return _local_apfs(descriptor, status.st_dev)


def _private_regular(status):
    return (
        stat.S_ISREG(status.st_mode)
        and not stat.S_ISLNK(status.st_mode)
        and stat.S_IMODE(status.st_mode) == 0o600
        and status.st_uid == os.geteuid()
        and status.st_nlink == 1
    )


def _record_fence_matches(config):
    """Verify the recorded fence without treating another reader as replacement.

    A busy mutex means either this process holds the fence or another reader
    is opening it. The held descriptor is authoritative in the first case.
    Readers wait only when nobody holds that fence.
    """

    if fence_matches_config(config):
        return True
    if _active_held(config) is not None:
        return False
    mutex = _mutex_for(config.device, config.inode)
    if not mutex.acquire(timeout=2):
        return False
    try:
        opened = _open_verified(config)
        if type(opened) is not tuple:
            return False
        os.close(opened[0])
        return True
    finally:
        mutex.release()


def _trust_bytes(config):
    if not _valid_config(config):
        raise FenceSafetyError("fence_identity_missing")
    body = json.dumps(
        {
            "device": config.device,
            "inode": config.inode,
            "path": config.path,
            "version": _TRUST_VERSION,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (body + "\n").encode("ascii")


def _valid_pin(pin):
    if (
        type(pin) is not TrustRecordPin
        or type(pin.path) is not str
        or not pin.path.startswith("/")
        or type(pin.device) is not int
        or type(pin.inode) is not int
        or type(pin.digest) is not str
        or pin.device <= 0
        or pin.inode <= 0
        or len(pin.digest) != 64
    ):
        return False
    return all(character in "0123456789abcdef" for character in pin.digest)


def _refuse_approved_pin(path):
    if path == _APPROVED_PIN_DIRECTORY or _APPROVED_PIN_DIRECTORY in path.parents:
        raise FenceSafetyError("production_fence_refused")


def _disposable_directory(directory):
    root = _absolute_path(directory)
    if root is None or root.name == "":
        raise FenceSafetyError("fence_path_invalid")
    _refuse_approved_pin(root)
    if _under_production_data(root):
        raise FenceSafetyError("production_fence_refused")
    if _cloud_path(root):
        raise FenceSafetyError("fence_path_refused")
    return root


def _directories_overlap(left, right):
    return left == right or left in right.parents or right in left.parents


def _inspect_private_directory(root, names):
    parent_fd, _name = _open_parent(root / names[0][0])
    try:
        if not _directory_is_local(parent_fd):
            raise FenceSafetyError("fence_filesystem")
        status = os.fstat(parent_fd)
        for name, symlink_code, exists_code in names:
            _refuse_existing_name(parent_fd, name, symlink_code, exists_code)
        return status.st_dev, status.st_ino
    finally:
        _close_all_preserving(((parent_fd, "pin parent"),))


def _refuse_existing_name(parent_fd, name, symlink_code, exists_code):
    status = _named_status(parent_fd, name)
    if status is None:
        return
    if stat.S_ISLNK(status.st_mode):
        raise FenceSafetyError(symlink_code)
    raise FenceSafetyError(exists_code)


def _chain_partial(fence_root, pin_path):
    fence_exists = _path_exists(fence_root / _FENCE_FILE_NAME)
    record_exists = _path_exists(fence_root / _TRUST_RECORD_NAME)
    pin_exists = _path_exists(pin_path)
    if fence_exists and record_exists and pin_exists:
        return "fence_record_and_pin"
    if fence_exists and record_exists:
        return "fence_and_record"
    if fence_exists:
        return "fence_only"
    return None


def _path_exists(path):
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise FenceSafetyError("fence_unreadable") from error
    return True


def _reraise_partial(error, fence_root, pin_path):
    try:
        partial = _chain_partial(fence_root, pin_path)
    except FenceSafetyError as inspection:
        error.add_note("partial inspection failed: " + inspection.code)
        raise error
    if partial is not None:
        error.partial = partial
    raise error


def _close_one(descriptor):
    if descriptor is None:
        return None
    try:
        os.close(descriptor)
    except OSError as error:
        return error
    return None


def _annotate_cleanup(primary, cleanup, label):
    primary.add_note(f"{label} close failed: errno {cleanup.errno}")


def _close_all_preserving(pairs):
    """Close every descriptor. A close error does not replace an active one."""

    failures = []
    for descriptor, label in pairs:
        cleanup = _close_one(descriptor)
        if cleanup is not None:
            failures.append((label, cleanup))
    if not failures:
        return
    current = sys.exc_info()[1]
    if current is not None:
        for label, cleanup in failures:
            _annotate_cleanup(current, cleanup, label)
        return
    raised = FenceSafetyError("pin_descriptor_close_failed")
    raised.__cause__ = failures[0][1]
    for label, cleanup in failures:
        _annotate_cleanup(raised, cleanup, label)
    raise raised


def _pin_open_error(error):
    if error.errno == errno.ELOOP:
        code = "pin_file_symlink"
    elif error.errno == errno.EEXIST:
        code = "pin_file_exists"
    else:
        code = "pin_file_create_failed"
    raised = FenceSafetyError(code)
    raised.__cause__ = error
    return raised


def _revoke_pin_descriptor(descriptor, inode):
    """Truncate the open pin and verify that same inode is empty.

    Verification uses fstat and fsync of this descriptor. It does not reread
    the file bytes and does not prove anything about a concurrent reader or
    a process that stops before this call.
    """

    if descriptor is None or type(inode) is not int:
        return OSError(errno.EBADF, "pin descriptor unavailable")
    try:
        os.ftruncate(descriptor, 0)
        current = os.fstat(descriptor)
        if current.st_ino != inode or current.st_size != 0:
            return OSError(errno.EIO, "pin revocation unverified")
        os.fsync(descriptor)
    except OSError as error:
        return error
    return None


def _revoke_or_uncertain(descriptor, inode, primary):
    problem = _revoke_pin_descriptor(descriptor, inode)
    if problem is None:
        return primary
    uncertain = FenceSafetyError("pin_completion_uncertain")
    uncertain.__cause__ = primary
    uncertain.add_note(
        "pin revocation unverified: errno %s" % getattr(problem, "errno", "unknown")
    )
    return uncertain


def _create_pin_file(pin_path, payload):
    parent_fd, name = _open_parent(pin_path)
    descriptor = None
    inode = None
    primary = None
    try:
        try:
            descriptor = os.open(
                name,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
        except OSError as error:
            primary = _pin_open_error(error)
        if descriptor is not None and primary is None:
            try:
                os.set_inheritable(descriptor, False)
                inode = os.fstat(descriptor).st_ino
                written = os.write(descriptor, payload)
                if written != len(payload):
                    raise FenceSafetyError("pin_file_create_failed")
            except FenceSafetyError as error:
                primary = _revoke_or_uncertain(descriptor, inode, error)
            except OSError as error:
                failed = FenceSafetyError("pin_file_create_failed")
                failed.__cause__ = error
                primary = _revoke_or_uncertain(descriptor, inode, failed)
        if descriptor is not None and primary is None:
            try:
                os.fsync(descriptor)
                current = os.fstat(descriptor)
                if current.st_ino != inode or not _private_regular(current):
                    raise FenceSafetyError("pin_file_permissions")
                if not _local_apfs(descriptor, current.st_dev):
                    raise FenceSafetyError("fence_filesystem")
                os.fsync(parent_fd)
            except FenceSafetyError as error:
                primary = _revoke_or_uncertain(descriptor, inode, error)
            except OSError as error:
                failed = FenceSafetyError("pin_file_fsync_failed")
                failed.__cause__ = error
                primary = _revoke_or_uncertain(descriptor, inode, failed)
        if primary is None:
            parent_error = _close_one(parent_fd)
            parent_fd = None
            if parent_error is not None:
                failed = FenceSafetyError("pin_descriptor_close_failed")
                failed.__cause__ = parent_error
                _annotate_cleanup(failed, parent_error, "pin parent")
                primary = _revoke_or_uncertain(descriptor, inode, failed)
    finally:
        file_error = _close_one(descriptor)
        parent_error = _close_one(parent_fd)
        if file_error is not None:
            if primary is None:
                failed = FenceSafetyError("pin_descriptor_close_failed")
                failed.__cause__ = file_error
                _annotate_cleanup(failed, file_error, "pin file")
                primary = _revoke_or_uncertain(descriptor, inode, failed)
            else:
                _annotate_cleanup(primary, file_error, "pin file")
        if parent_error is not None:
            if primary is None:
                failed = FenceSafetyError("pin_descriptor_close_failed")
                failed.__cause__ = parent_error
                _annotate_cleanup(failed, parent_error, "pin parent")
                primary = _revoke_or_uncertain(descriptor, inode, failed)
            else:
                _annotate_cleanup(primary, parent_error, "pin parent")
    if primary is not None:
        raise primary


def _pin_bytes(pin):
    if not _valid_pin(pin):
        raise FenceSafetyError("trust_pin_invalid")
    body = json.dumps(
        {
            "device": pin.device,
            "digest": pin.digest,
            "inode": pin.inode,
            "path": pin.path,
            "version": _PIN_VERSION,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (body + "\n").encode("ascii")


def _pin_file_path(path):
    if type(path) is not str:
        raise FenceSafetyError("pin_file_invalid")
    candidate = _absolute_path(path)
    if (
        candidate is None
        or str(candidate) != path
        or candidate.name != _PIN_FILE_NAME
    ):
        raise FenceSafetyError("pin_file_invalid")
    _refuse_approved_pin(candidate)
    if _under_production_data(candidate):
        raise FenceSafetyError("production_fence_refused")
    if _cloud_path(candidate):
        raise FenceSafetyError("fence_path_refused")
    return candidate


def _read_pin_file(path):
    candidate = _pin_file_path(path)
    parent_fd = None
    descriptor = None
    try:
        parent_fd, name = _open_parent(candidate)
        listed = _named_status(parent_fd, name)
        if listed is None:
            raise FenceSafetyError("pin_file_missing")
        if stat.S_ISLNK(listed.st_mode):
            raise FenceSafetyError("pin_file_symlink")
        if not stat.S_ISREG(listed.st_mode):
            raise FenceSafetyError("pin_file_malformed")
        if stat.S_IMODE(listed.st_mode) != 0o600:
            raise FenceSafetyError("pin_file_permissions")
        if listed.st_uid != os.geteuid():
            raise FenceSafetyError("pin_file_owner")
        if listed.st_nlink != 1:
            raise FenceSafetyError("pin_file_hard_link")
        if listed.st_size > _PIN_MAX_BYTES:
            raise FenceSafetyError("pin_file_malformed")
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise FenceSafetyError("pin_file_symlink") from error
            raise FenceSafetyError("pin_file_unreadable") from error
        os.set_inheritable(descriptor, False)
        current = os.fstat(descriptor)
        if current.st_dev != listed.st_dev or current.st_ino != listed.st_ino:
            raise FenceSafetyError("pin_file_identity")
        if not _private_regular(current):
            if current.st_uid != os.geteuid():
                raise FenceSafetyError("pin_file_owner")
            if current.st_nlink != 1:
                raise FenceSafetyError("pin_file_hard_link")
            if stat.S_IMODE(current.st_mode) != 0o600:
                raise FenceSafetyError("pin_file_permissions")
            raise FenceSafetyError("pin_file_identity")
        if not _local_apfs(descriptor, current.st_dev):
            raise FenceSafetyError("fence_filesystem")
        payload = os.read(descriptor, _PIN_MAX_BYTES + 1)
        if len(payload) != current.st_size:
            raise FenceSafetyError("pin_file_unreadable")
        return _parse_pin_file(payload)
    except FenceSafetyError:
        raise
    except OSError as error:
        raise FenceSafetyError("pin_file_unreadable") from error
    finally:
        _close_all_preserving(
            (
                (descriptor, "pin file"),
                (parent_fd, "pin parent"),
            )
        )


def _parse_pin_file(payload):
    if type(payload) is not bytes or payload == b"" or not payload.endswith(b"\n"):
        raise FenceSafetyError("pin_file_truncated")
    if len(payload) > _PIN_MAX_BYTES or payload.count(b"\n") != 1:
        raise FenceSafetyError("pin_file_malformed")
    try:
        parsed = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FenceSafetyError("pin_file_malformed") from None
    if (
        type(parsed) is not dict
        or set(parsed) != {"device", "digest", "inode", "path", "version"}
        or parsed["version"] != _PIN_VERSION
        or type(parsed["path"]) is not str
        or type(parsed["digest"]) is not str
        or type(parsed["device"]) is not int
        or type(parsed["inode"]) is not int
        or type(parsed["device"]) is bool
        or type(parsed["inode"]) is bool
    ):
        raise FenceSafetyError("pin_file_malformed")
    pin = TrustRecordPin(
        parsed["path"],
        parsed["device"],
        parsed["inode"],
        parsed["digest"],
    )
    record = _absolute_path(pin.path)
    if (
        not _valid_pin(pin)
        or record is None
        or str(record) != pin.path
        or record.name != _TRUST_RECORD_NAME
        or _pin_bytes(pin) != payload
    ):
        raise FenceSafetyError("pin_file_malformed")
    _refuse_approved_pin(record)
    if _under_production_data(record):
        raise FenceSafetyError("production_fence_refused")
    if _cloud_path(record):
        raise FenceSafetyError("fence_path_refused")
    return pin


def _read_trust_record(pin):
    record = _absolute_path(pin.path)
    parent_fd = None
    descriptor = None
    try:
        parent_fd, name = _open_parent(record)
        listed = _named_status(parent_fd, name)
        if listed is None:
            raise FenceSafetyError("trust_record_missing")
        if stat.S_ISLNK(listed.st_mode):
            raise FenceSafetyError("trust_record_symlink")
        if not stat.S_ISREG(listed.st_mode):
            raise FenceSafetyError("trust_record_malformed")
        if stat.S_IMODE(listed.st_mode) != 0o600:
            raise FenceSafetyError("trust_record_permissions")
        if listed.st_uid != os.geteuid():
            raise FenceSafetyError("trust_record_owner")
        if listed.st_nlink != 1:
            raise FenceSafetyError("trust_record_hard_link")
        if listed.st_dev != pin.device or listed.st_ino != pin.inode:
            raise FenceSafetyError("trust_record_identity")
        if listed.st_size > _TRUST_RECORD_MAX_BYTES:
            raise FenceSafetyError("trust_record_malformed")
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise FenceSafetyError("trust_record_symlink") from error
            raise FenceSafetyError("trust_record_unreadable") from error
        os.set_inheritable(descriptor, False)
        current = os.fstat(descriptor)
        if (
            current.st_dev != pin.device
            or current.st_ino != pin.inode
            or not _private_regular(current)
        ):
            if current.st_uid != os.geteuid():
                raise FenceSafetyError("trust_record_owner")
            if current.st_nlink != 1:
                raise FenceSafetyError("trust_record_hard_link")
            if stat.S_IMODE(current.st_mode) != 0o600:
                raise FenceSafetyError("trust_record_permissions")
            raise FenceSafetyError("trust_record_identity")
        if not _local_apfs(descriptor, current.st_dev):
            raise FenceSafetyError("fence_filesystem")
        payload = os.read(descriptor, _TRUST_RECORD_MAX_BYTES + 1)
        if len(payload) != current.st_size:
            raise FenceSafetyError("trust_record_unreadable")
        return payload
    except FenceSafetyError:
        raise
    except OSError as error:
        raise FenceSafetyError("trust_record_unreadable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_fd is not None:
            os.close(parent_fd)


def _parse_trust_record(payload):
    if type(payload) is not bytes or payload == b"" or not payload.endswith(b"\n"):
        raise FenceSafetyError("trust_record_truncated")
    if len(payload) > _TRUST_RECORD_MAX_BYTES or payload.count(b"\n") != 1:
        raise FenceSafetyError("trust_record_malformed")
    try:
        parsed = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FenceSafetyError("trust_record_malformed") from None
    if (
        type(parsed) is not dict
        or set(parsed) != {"device", "inode", "path", "version"}
        or parsed["version"] != _TRUST_VERSION
        or type(parsed["path"]) is not str
        or type(parsed["device"]) is not int
        or type(parsed["inode"]) is not int
        or type(parsed["device"]) is bool
        or type(parsed["inode"]) is bool
    ):
        raise FenceSafetyError("trust_record_malformed")
    config = ProviderFenceConfig(parsed["path"], parsed["device"], parsed["inode"])
    if not _valid_config(config) or _trust_bytes(config) != payload:
        raise FenceSafetyError("trust_record_malformed")
    return config


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
