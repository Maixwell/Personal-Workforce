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
- A provisioning receipt is emitted only after that successful return and a
  fresh descriptor read-back. It becomes eligible for acceptance only when
  the provisioning process that emitted it is observed to exit successfully.
  The operator must then confirm that exact receipt before an acceptance
  file can be created. Receipt retention is audit evidence, not activation.
"""

import ctypes
import ctypes.util
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
import weakref
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
_RECEIPT_VERSION = "max-smart-provider-fence-receipt-v1"
_ACCEPTANCE_VERSION = "max-smart-provider-fence-acceptance-v1"
_ACCEPTANCE_FILE_NAME = "provider-fence.acceptance"
_RECEIPT_MAX_BYTES = 8192
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
    """An exclusive lock held by this process. Closing it releases the lock.

    ``lifecycle`` serializes descriptor validation with release. It is not the
    per-inode acquisition mutex. Release retires the descriptor under that
    lock before unlock or close, and an uncertain close quarantines the permit
    for the life of the process.
    """

    __slots__ = (
        "path",
        "descriptor",
        "device",
        "inode",
        "mutex",
        "lifecycle",
        "released",
        "release_started",
        "closure_uncertain",
        "quarantined",
        "release_failure",
    )

    def __init__(self, path, descriptor, device, inode, mutex):
        self.path = path
        self.descriptor = descriptor
        self.device = device
        self.inode = inode
        self.mutex = mutex
        self.lifecycle = threading.Lock()
        self.released = False
        self.release_started = False
        self.closure_uncertain = False
        self.quarantined = False
        self.release_failure = None


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


_PROVISION_SEAL = object()
_PROVISION_LAUNCHES = weakref.WeakKeyDictionary()
_RECEIPT_KEYS = {
    "fence_device",
    "fence_inode",
    "fence_path",
    "pin_device",
    "pin_digest",
    "pin_inode",
    "pin_path",
    "receipt_id",
    "record_device",
    "record_digest",
    "record_inode",
    "record_path",
    "result",
    "version",
}
_ACCEPTANCE_KEYS = {
    "fence_device",
    "fence_inode",
    "fence_path",
    "pin_device",
    "pin_digest",
    "pin_inode",
    "pin_path",
    "receipt_digest",
    "receipt_id",
    "receipt_version",
    "record_device",
    "record_digest",
    "record_inode",
    "record_path",
    "version",
}
_RECEIPT_CHILD = """\
import sys
from provider_fence import provision_disposable_fence_receipt
fence_directory, pin_directory, receipt_id = sys.argv[1:]
def token_bytes(count):
    raw = bytes.fromhex(receipt_id)
    if count != 16 or len(raw) != 16:
        raise SystemExit(2)
    return raw
receipt = provision_disposable_fence_receipt(
    fence_directory,
    pin_directory,
    token_bytes=token_bytes,
)
sys.stdout.buffer.write(receipt.receipt_bytes)
sys.stdout.buffer.flush()
if sys.stdin.buffer.read(1) != b"\\n":
    raise SystemExit(3)
"""


class ProvisioningReceipt:
    """Canonical receipt bytes emitted after one successful provisioning."""

    __slots__ = ("receipt_bytes",)

    def __init__(self, seal, payload):
        if seal is not _PROVISION_SEAL:
            raise FenceSafetyError("provision_exit_unproven")
        _parse_receipt(payload)
        self.receipt_bytes = payload


class ProvisioningCompletion:
    """Receipt whose provisioning process was observed to exit successfully."""

    __slots__ = ("receipt",)

    def __init__(self, seal, receipt):
        if (
            seal is not _PROVISION_SEAL
            or type(receipt) is not ProvisioningReceipt
        ):
            raise FenceSafetyError("provision_exit_unproven")
        self.receipt = receipt


def provision_disposable_fence_receipt(
    fence_directory,
    pin_directory,
    *,
    token_bytes=None,
):
    """Return a receipt only after successful provisioning and read-back.

    The returned receipt is not evidence that this process later exited
    successfully and cannot by itself authorize acceptance.
    """

    created = provision_disposable_fence_pin(fence_directory, pin_directory)
    observed = _observe_chain(created.pin_path)
    if not _observation_matches_created(observed, created):
        raise FenceSafetyError("receipt_observation_failed")
    receipt_id = _new_receipt_id(token_bytes)
    payload = _receipt_bytes(observed, receipt_id)
    return ProvisioningReceipt(_PROVISION_SEAL, payload)


def begin_disposable_fence_provision(
    fence_directory,
    pin_directory,
    *,
    token_bytes=None,
):
    """Start provisioning in a child process and return that process."""

    receipt_id = _new_receipt_id(token_bytes)
    environment = os.environ.copy()
    root = str(Path(__file__).resolve().parent)
    current = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = (
        root + os.pathsep + current if current else root
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-c",
            _RECEIPT_CHILD,
            os.fspath(fence_directory),
            os.fspath(pin_directory),
            receipt_id,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    _PROVISION_LAUNCHES[process] = receipt_id
    return process


def complete_disposable_fence_provision(process):
    """Seal a receipt only after the started child exits with status zero."""

    if type(process) is not subprocess.Popen or process not in _PROVISION_LAUNCHES:
        raise FenceSafetyError("provision_exit_unproven")
    expected_id = _PROVISION_LAUNCHES.pop(process)
    try:
        if process.poll() is None:
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.write(b"\n")
                    process.stdin.close()
                except OSError:
                    pass
            process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        _drain_provision_pipes(process)
        raise FenceSafetyError("provision_exit_unproven") from None
    if process.returncode != 0:
        _drain_provision_pipes(process)
        raise FenceSafetyError("provision_exit_failed")
    payload = b"" if process.stdout is None else process.stdout.read()
    _drain_provision_pipes(process)
    parsed = _parse_receipt(payload)
    if parsed["receipt_id"] != expected_id:
        raise FenceSafetyError("receipt_observation_failed")
    if _observe_chain(parsed["pin_path"]) != _observation_from_receipt(parsed):
        raise FenceSafetyError("receipt_observation_failed")
    return ProvisioningCompletion(
        _PROVISION_SEAL,
        ProvisioningReceipt(_PROVISION_SEAL, payload),
    )


def accept_disposable_fence_receipt(completion, acceptance_directory, confirm):
    """Create one acceptance file after explicit confirmation of a receipt.

    Confirmation is the authorization event. The acceptance path is not
    opened for creation until confirmation matches the verified receipt.
    """

    if type(completion) is not ProvisioningCompletion:
        raise FenceSafetyError("provision_exit_unproven")
    parsed = _parse_receipt(completion.receipt.receipt_bytes)
    try:
        observed = _observe_chain(parsed["pin_path"])
    except FenceSafetyError as error:
        if error.code != "receipt_observation_failed":
            raise
        raise FenceSafetyError("receipt_chain_mismatch") from error
    if observed != _observation_from_receipt(parsed):
        raise FenceSafetyError("receipt_chain_mismatch")
    loaded = load_disposable_fence_pin(parsed["pin_path"])
    if not _same_config(loaded, _fence_from_receipt(parsed)):
        raise FenceSafetyError("receipt_chain_mismatch")
    gate = acquire_exclusive(loaded)
    if type(gate) is not FenceHeld:
        if type(gate) is FenceBusy:
            raise FenceSafetyError("fence_busy")
        code = gate.code if type(gate) is FenceUnproven else "fence_unproven"
        raise FenceSafetyError(code)
    primary = None
    result = None
    try:
        if not callable(confirm):
            raise FenceSafetyError("acceptance_confirmation_refused")
        expected = _confirmation_binding(parsed)
        try:
            decision = confirm(dict(expected))
        except FenceSafetyError:
            raise
        except Exception as error:
            refused = FenceSafetyError("acceptance_confirmation_refused")
            refused.__cause__ = error
            raise refused
        if not _exact_confirmation(decision, expected):
            raise FenceSafetyError("acceptance_confirmation_refused")
        payload = _acceptance_bytes(parsed)
        path = _acceptance_candidate(acceptance_directory, parsed["pin_path"])
        _create_acceptance_file(path, payload)
        result = str(path)
    except FenceSafetyError as error:
        primary = error
    finally:
        try:
            release_problem = release_fence(gate)
        except Exception as error:
            release_problem = error
        if release_problem is not None:
            if primary is None and sys.exc_info()[1] is None:
                primary = FenceSafetyError("fence_release_failed")
                primary.__cause__ = release_problem
                _attach_release_failure(primary, release_problem)
            elif primary is not None:
                _attach_release_failure(primary, release_problem)
    if primary is not None:
        raise primary
    return result


def load_disposable_fence_acceptance(path, receipt_bytes):
    """Load an accepted chain when the retained receipt binds the file.

    The retained receipt is required. This function does not reconstruct a
    receipt from the live files and does not acquire the exclusive fence.
    """

    if type(receipt_bytes) is not bytes:
        raise FenceSafetyError("acceptance_receipt_required")
    payload = _read_acceptance_file(path)
    accepted = _parse_acceptance(payload)
    if hashlib.sha256(receipt_bytes).hexdigest() != accepted["receipt_digest"]:
        raise FenceSafetyError("acceptance_receipt_digest")
    parsed = _parse_receipt(receipt_bytes)
    if not _acceptance_matches_receipt(accepted, parsed):
        raise FenceSafetyError("acceptance_receipt_mismatch")
    try:
        observed = _observe_chain(parsed["pin_path"])
    except FenceSafetyError as error:
        if error.code != "receipt_observation_failed":
            raise
        raise FenceSafetyError("acceptance_chain_mismatch") from error
    if observed != _observation_from_receipt(parsed):
        raise FenceSafetyError("acceptance_chain_mismatch")
    loaded = load_disposable_fence_pin(parsed["pin_path"])
    if not _same_config(loaded, _fence_from_receipt(parsed)):
        raise FenceSafetyError("acceptance_chain_mismatch")
    return loaded


def publish_loaded_fence_acceptance(path, receipt_bytes):
    """Publish one accepted fence identity through the set-once contract."""

    config = load_disposable_fence_acceptance(path, receipt_bytes)
    publish_shared_fence_config(config)
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
    if _unreleased_held(config.device, config.inode) is not None:
        mutex.release()
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
    """Unlock and close one held fence under its lifecycle lock.

    Confirmed descriptor closure proves that the OS lock ended, including
    when unlock itself failed. A close that raises quarantines the permit
    permanently, including after a successful unlock: the numerical
    descriptor is never used again and the identity stays unavailable in
    this process. The per-inode mutex is released only after confirmed
    closure and a mutex release that returns normally.

    Lock order:
    - Acquire the per-inode mutex before the held-registry guard.
    - Never acquire a lifecycle lock while the registry guard or the
      mutex-table guard is held. Those guards are dropped before validation.
    - Never acquire a per-inode mutex while a lifecycle lock is held.
      Release only releases the mutex it already owns.
    - Registry removal may take the registry guard while the lifecycle
      lock is held. The two guards are not nested the other way.
    The lifecycle lock is not held across confirmation, process waits, or
    database work.
    """

    if type(held) is not FenceHeld:
        return None
    with held.lifecycle:
        if held.released:
            return None
        if held.release_started:
            return held.release_failure
        held.release_started = True
        try:
            return _release_retired_descriptor(held)
        finally:
            if not held.released:
                held.quarantined = True
                if held.release_failure is None:
                    active = sys.exc_info()[1]
                    held.release_failure = (
                        active
                        if active is not None
                        else RuntimeError(
                            "fence release did not prove descriptor closure"
                        )
                    )


def _release_retired_descriptor(held):
    """Finish release after the descriptor has been retired.

    The caller holds ``held.lifecycle`` and has set ``release_started``.
    """

    problem = None
    try:
        fcntl.lockf(held.descriptor, fcntl.LOCK_UN)
    except OSError as error:
        problem = error
    try:
        os.close(held.descriptor)
    except OSError as error:
        held.closure_uncertain = True
        if problem is None:
            problem = error
        else:
            problem.add_note(
                "descriptor close failed: errno %s" % error.errno
            )
        held.quarantined = True
        held.release_failure = problem
        return problem
    try:
        held.mutex.release()
    except RuntimeError as error:
        if problem is None:
            problem = error
        else:
            problem.add_note("mutex release failed: %s" % error)
        held.quarantined = True
        held.release_failure = problem
        return problem
    held.released = True
    _forget_held(held)
    return problem


def _attach_release_failure(primary, problem):
    primary.add_note(
        "fence release failed: errno %s" % getattr(problem, "errno", "unknown")
    )
    for note in getattr(problem, "__notes__", ()):
        primary.add_note(note)


def fence_still_matches(held):
    """Return whether the held descriptor and path still name that inode.

    Descriptor reads hold the permit lifecycle lock, so a release that has
    retired the descriptor cannot close or reuse it during this check. The
    path walk uses directory descriptors, not the held descriptor, and runs
    after that lock is dropped.
    """

    if type(held) is not FenceHeld:
        return False
    with held.lifecycle:
        if (
            held.released
            or held.release_started
            or held.closure_uncertain
            or held.quarantined
        ):
            return False
        try:
            current = os.fstat(held.descriptor)
            content_ok = _content_matches(held.descriptor)
        except OSError:
            return False
        descriptor_matches = (
            content_ok
            and stat.S_ISREG(current.st_mode)
            and stat.S_IMODE(current.st_mode) == 0o600
            and current.st_uid == os.geteuid()
            and current.st_nlink == 1
            and current.st_dev == held.device
            and current.st_ino == held.inode
        )
        path = held.path
        device = held.device
        inode = held.inode
    if not descriptor_matches:
        return False
    return _path_names(path, device, inode)


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


def _revoke_or_uncertain(
    descriptor,
    inode,
    primary,
    uncertain_code="pin_completion_uncertain",
):
    problem = _revoke_pin_descriptor(descriptor, inode)
    if problem is None:
        return primary
    uncertain = FenceSafetyError(uncertain_code)
    uncertain.__cause__ = primary
    uncertain.add_note(
        "revocation unverified: errno %s" % getattr(problem, "errno", "unknown")
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


def _new_receipt_id(token_bytes):
    producer = secrets.token_bytes if token_bytes is None else token_bytes
    try:
        raw = producer(16)
    except Exception as error:
        raise FenceSafetyError("receipt_id_invalid") from error
    if type(raw) is not bytes or len(raw) != 16:
        raise FenceSafetyError("receipt_id_invalid")
    return raw.hex()


def _receipt_bytes(observed, receipt_id):
    return _canonical_document(
        {
            "fence_device": observed["fence_device"],
            "fence_inode": observed["fence_inode"],
            "fence_path": observed["fence_path"],
            "pin_device": observed["pin_device"],
            "pin_digest": observed["pin_digest"],
            "pin_inode": observed["pin_inode"],
            "pin_path": observed["pin_path"],
            "receipt_id": receipt_id,
            "record_device": observed["record_device"],
            "record_digest": observed["record_digest"],
            "record_inode": observed["record_inode"],
            "record_path": observed["record_path"],
            "result": "provisioned",
            "version": _RECEIPT_VERSION,
        },
        "receipt_malformed",
    )


def _acceptance_bytes(parsed):
    return _canonical_document(
        {
            "fence_device": parsed["fence_device"],
            "fence_inode": parsed["fence_inode"],
            "fence_path": parsed["fence_path"],
            "pin_device": parsed["pin_device"],
            "pin_digest": parsed["pin_digest"],
            "pin_inode": parsed["pin_inode"],
            "pin_path": parsed["pin_path"],
            "receipt_digest": hashlib.sha256(
                _canonical_document(parsed, "receipt_malformed")
            ).hexdigest(),
            "receipt_id": parsed["receipt_id"],
            "receipt_version": _RECEIPT_VERSION,
            "record_device": parsed["record_device"],
            "record_digest": parsed["record_digest"],
            "record_inode": parsed["record_inode"],
            "record_path": parsed["record_path"],
            "version": _ACCEPTANCE_VERSION,
        },
        "acceptance_file_malformed",
    )


def _exact_confirmation(decision, expected):
    if type(decision) is not dict or set(decision) != set(expected):
        return False
    for key, value in expected.items():
        actual = decision[key]
        if type(actual) is not type(value) or actual != value:
            return False
    return True


def _confirmation_binding(parsed):
    receipt_bytes = _canonical_document(parsed, "receipt_malformed")
    return {
        "fence_device": parsed["fence_device"],
        "fence_inode": parsed["fence_inode"],
        "fence_path": parsed["fence_path"],
        "pin_device": parsed["pin_device"],
        "pin_digest": parsed["pin_digest"],
        "pin_inode": parsed["pin_inode"],
        "pin_path": parsed["pin_path"],
        "receipt_digest": hashlib.sha256(receipt_bytes).hexdigest(),
        "receipt_id": parsed["receipt_id"],
        "record_device": parsed["record_device"],
        "record_digest": parsed["record_digest"],
        "record_inode": parsed["record_inode"],
        "record_path": parsed["record_path"],
    }


def _canonical_document(mapping, code):
    body = json.dumps(
        mapping,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    payload = (body + "\n").encode("ascii")
    if len(payload) > _RECEIPT_MAX_BYTES:
        raise FenceSafetyError(code)
    return payload


def _parse_receipt(payload):
    parsed = _parse_canonical_object(payload, _RECEIPT_KEYS, "receipt_malformed")
    if (
        parsed["version"] != _RECEIPT_VERSION
        or parsed["result"] != "provisioned"
        or not _lowercase_hex(parsed["receipt_id"], 32)
        or not _lowercase_hex(parsed["record_digest"], 64)
        or not _lowercase_hex(parsed["pin_digest"], 64)
    ):
        raise FenceSafetyError("receipt_malformed")
    _require_identity_fields(parsed, "receipt_malformed")
    if _canonical_document(parsed, "receipt_malformed") != payload:
        raise FenceSafetyError("receipt_malformed")
    return parsed


def _parse_acceptance(payload):
    parsed = _parse_canonical_object(
        payload,
        _ACCEPTANCE_KEYS,
        "acceptance_file_malformed",
    )
    if (
        parsed["version"] != _ACCEPTANCE_VERSION
        or parsed["receipt_version"] != _RECEIPT_VERSION
        or not _lowercase_hex(parsed["receipt_id"], 32)
        or not _lowercase_hex(parsed["receipt_digest"], 64)
        or not _lowercase_hex(parsed["record_digest"], 64)
        or not _lowercase_hex(parsed["pin_digest"], 64)
    ):
        raise FenceSafetyError("acceptance_file_malformed")
    _require_identity_fields(parsed, "acceptance_file_malformed")
    if _canonical_document(parsed, "acceptance_file_malformed") != payload:
        raise FenceSafetyError("acceptance_file_malformed")
    return parsed


def _parse_canonical_object(payload, keys, code):
    if (
        type(payload) is not bytes
        or payload == b""
        or not payload.endswith(b"\n")
        or payload.count(b"\n") != 1
        or len(payload) > _RECEIPT_MAX_BYTES
    ):
        raise FenceSafetyError(code)
    try:
        parsed = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FenceSafetyError(code) from None
    if type(parsed) is not dict or set(parsed) != keys:
        raise FenceSafetyError(code)
    return parsed


def _require_identity_fields(parsed, code):
    for name in (
        "fence_device",
        "fence_inode",
        "record_device",
        "record_inode",
        "pin_device",
        "pin_inode",
    ):
        if type(parsed[name]) is not int or type(parsed[name]) is bool or parsed[name] <= 0:
            raise FenceSafetyError(code)
    for name in ("fence_path", "record_path", "pin_path"):
        if type(parsed[name]) is not str:
            raise FenceSafetyError(code)
    fence = _chain_path(parsed["fence_path"], _FENCE_FILE_NAME, code)
    record = _chain_path(parsed["record_path"], _TRUST_RECORD_NAME, code)
    pin = _chain_path(parsed["pin_path"], _PIN_FILE_NAME, code)
    if fence.parent != record.parent or pin.parent == fence.parent:
        raise FenceSafetyError(code)


def _chain_path(path, name, code):
    candidate = _absolute_path(path)
    if candidate is None or str(candidate) != path or candidate.name != name:
        raise FenceSafetyError(code)
    _refuse_approved_pin(candidate)
    if _under_production_data(candidate) or _cloud_path(candidate):
        raise FenceSafetyError("production_fence_refused")
    return candidate


def _lowercase_hex(value, size):
    return (
        type(value) is str
        and len(value) == size
        and all(character in "0123456789abcdef" for character in value)
    )


def _observation_from_receipt(parsed):
    return {
        "fence_device": parsed["fence_device"],
        "fence_inode": parsed["fence_inode"],
        "fence_path": parsed["fence_path"],
        "pin_device": parsed["pin_device"],
        "pin_digest": parsed["pin_digest"],
        "pin_inode": parsed["pin_inode"],
        "pin_path": parsed["pin_path"],
        "record_device": parsed["record_device"],
        "record_digest": parsed["record_digest"],
        "record_inode": parsed["record_inode"],
        "record_path": parsed["record_path"],
    }


def _observation_matches_created(observed, created):
    return (
        observed["fence_path"] == created.fence.path
        and observed["fence_device"] == created.fence.device
        and observed["fence_inode"] == created.fence.inode
        and observed["record_path"] == created.pin.path
        and observed["record_device"] == created.pin.device
        and observed["record_inode"] == created.pin.inode
        and observed["record_digest"] == created.pin.digest
        and observed["pin_path"] == created.pin_path
    )


def _fence_from_receipt(parsed):
    return ProviderFenceConfig(
        parsed["fence_path"],
        parsed["fence_device"],
        parsed["fence_inode"],
    )


def _acceptance_matches_receipt(accepted, parsed):
    if (
        accepted["receipt_id"] != parsed["receipt_id"]
        or accepted["receipt_version"] != parsed["version"]
    ):
        return False
    for key in _observation_from_receipt(parsed):
        if accepted[key] != parsed[key]:
            return False
    return True


def _observe_chain(pin_path):
    pin_payload, pin_device, pin_inode = _read_chain_file(
        pin_path,
        _PIN_MAX_BYTES,
    )
    try:
        pin = _parse_pin_file(pin_payload)
    except FenceSafetyError as error:
        raise FenceSafetyError("receipt_observation_failed") from error
    record_payload, record_device, record_inode = _read_chain_file(
        pin.path,
        _TRUST_RECORD_MAX_BYTES,
    )
    if (
        record_device != pin.device
        or record_inode != pin.inode
        or hashlib.sha256(record_payload).hexdigest() != pin.digest
    ):
        raise FenceSafetyError("receipt_observation_failed")
    try:
        record = _parse_trust_record(record_payload)
    except FenceSafetyError as error:
        raise FenceSafetyError("receipt_observation_failed") from error
    fence_payload, fence_device, fence_inode = _read_chain_file(
        record.path,
        len(_MAGIC),
    )
    if (
        fence_payload != _MAGIC
        or fence_device != record.device
        or fence_inode != record.inode
    ):
        raise FenceSafetyError("receipt_observation_failed")
    return {
        "fence_device": fence_device,
        "fence_inode": fence_inode,
        "fence_path": record.path,
        "pin_device": pin_device,
        "pin_digest": hashlib.sha256(pin_payload).hexdigest(),
        "pin_inode": pin_inode,
        "pin_path": pin_path if type(pin_path) is str else str(pin_path),
        "record_device": record_device,
        "record_digest": pin.digest,
        "record_inode": record_inode,
        "record_path": pin.path,
    }


def _read_chain_file(path, max_bytes):
    if type(path) is not str:
        path = os.fspath(path)
    candidate = _absolute_path(path)
    if candidate is None or str(candidate) != path:
        raise FenceSafetyError("receipt_observation_failed")
    _refuse_approved_pin(candidate)
    parent_fd, name = _open_parent(candidate)
    descriptor = None
    try:
        listed = _named_status(parent_fd, name)
        if listed is None or not _private_regular(listed):
            raise FenceSafetyError("receipt_observation_failed")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        os.set_inheritable(descriptor, False)
        current = os.fstat(descriptor)
        if (
            current.st_dev != listed.st_dev
            or current.st_ino != listed.st_ino
            or not _local_apfs(descriptor, current.st_dev)
        ):
            raise FenceSafetyError("receipt_observation_failed")
        payload = os.read(descriptor, max_bytes + 1)
        if len(payload) != current.st_size or len(payload) > max_bytes:
            raise FenceSafetyError("receipt_observation_failed")
        return payload, current.st_dev, current.st_ino
    finally:
        _close_all_preserving(
            (
                (descriptor, "chain file"),
                (parent_fd, "chain parent"),
            )
        )


def _acceptance_candidate(directory, pin_path):
    if type(directory) is not str and not isinstance(directory, Path):
        raise FenceSafetyError("acceptance_directory_invalid")
    text = os.fspath(directory)
    candidate = _absolute_path(text)
    pin = _absolute_path(pin_path)
    if (
        candidate is None
        or pin is None
        or str(candidate) != text
        or candidate != pin.parent
    ):
        raise FenceSafetyError("acceptance_directory_invalid")
    path = candidate / _ACCEPTANCE_FILE_NAME
    _refuse_approved_pin(path)
    if _under_production_data(path) or _cloud_path(path):
        raise FenceSafetyError("production_fence_refused")
    return path


def _create_acceptance_file(path, payload):
    parent_fd, name = _open_parent(path)
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
            primary = _acceptance_open_error(error)
        if descriptor is not None and primary is None:
            try:
                os.set_inheritable(descriptor, False)
                inode = os.fstat(descriptor).st_ino
                written = os.write(descriptor, payload)
                if written != len(payload):
                    raise FenceSafetyError("acceptance_file_create_failed")
            except FenceSafetyError as error:
                primary = _revoke_or_uncertain(
                    descriptor,
                    inode,
                    error,
                    "acceptance_completion_uncertain",
                )
            except OSError as error:
                failed = FenceSafetyError("acceptance_file_create_failed")
                failed.__cause__ = error
                primary = _revoke_or_uncertain(
                    descriptor,
                    inode,
                    failed,
                    "acceptance_completion_uncertain",
                )
        if descriptor is not None and primary is None:
            try:
                os.fsync(descriptor)
                current = os.fstat(descriptor)
                if current.st_ino != inode or not _private_regular(current):
                    raise FenceSafetyError("acceptance_file_permissions")
                if not _local_apfs(descriptor, current.st_dev):
                    raise FenceSafetyError("fence_filesystem")
                os.fsync(parent_fd)
            except FenceSafetyError as error:
                primary = _revoke_or_uncertain(
                    descriptor,
                    inode,
                    error,
                    "acceptance_completion_uncertain",
                )
            except OSError as error:
                failed = FenceSafetyError("acceptance_file_fsync_failed")
                failed.__cause__ = error
                primary = _revoke_or_uncertain(
                    descriptor,
                    inode,
                    failed,
                    "acceptance_completion_uncertain",
                )
        if primary is None:
            parent_error = _close_one(parent_fd)
            parent_fd = None
            if parent_error is not None:
                failed = FenceSafetyError("acceptance_descriptor_close_failed")
                failed.__cause__ = parent_error
                _annotate_cleanup(failed, parent_error, "acceptance parent")
                primary = _revoke_or_uncertain(
                    descriptor,
                    inode,
                    failed,
                    "acceptance_completion_uncertain",
                )
    finally:
        file_error = _close_one(descriptor)
        parent_error = _close_one(parent_fd)
        if file_error is not None:
            if primary is None:
                failed = FenceSafetyError("acceptance_descriptor_close_failed")
                failed.__cause__ = file_error
                _annotate_cleanup(failed, file_error, "acceptance file")
                primary = _revoke_or_uncertain(
                    descriptor,
                    inode,
                    failed,
                    "acceptance_completion_uncertain",
                )
            else:
                _annotate_cleanup(primary, file_error, "acceptance file")
        if parent_error is not None:
            if primary is None:
                failed = FenceSafetyError("acceptance_descriptor_close_failed")
                failed.__cause__ = parent_error
                _annotate_cleanup(failed, parent_error, "acceptance parent")
                primary = _revoke_or_uncertain(
                    descriptor,
                    inode,
                    failed,
                    "acceptance_completion_uncertain",
                )
            else:
                _annotate_cleanup(primary, parent_error, "acceptance parent")
    if primary is not None:
        raise primary


def _acceptance_open_error(error):
    if error.errno == errno.ELOOP:
        code = "acceptance_file_symlink"
    elif error.errno == errno.EEXIST:
        code = "acceptance_file_exists"
    else:
        code = "acceptance_file_create_failed"
    raised = FenceSafetyError(code)
    raised.__cause__ = error
    return raised


def _read_acceptance_file(path):
    if type(path) is not str:
        raise FenceSafetyError("acceptance_file_invalid")
    candidate = _absolute_path(path)
    if (
        candidate is None
        or str(candidate) != path
        or candidate.name != _ACCEPTANCE_FILE_NAME
    ):
        raise FenceSafetyError("acceptance_file_invalid")
    _refuse_approved_pin(candidate)
    if _under_production_data(candidate) or _cloud_path(candidate):
        raise FenceSafetyError("production_fence_refused")
    parent_fd, name = _open_parent(candidate)
    descriptor = None
    try:
        listed = _named_status(parent_fd, name)
        if listed is None:
            raise FenceSafetyError("acceptance_file_missing")
        if stat.S_ISLNK(listed.st_mode):
            raise FenceSafetyError("acceptance_file_symlink")
        if not stat.S_ISREG(listed.st_mode):
            raise FenceSafetyError("acceptance_file_malformed")
        if stat.S_IMODE(listed.st_mode) != 0o600:
            raise FenceSafetyError("acceptance_file_permissions")
        if listed.st_uid != os.geteuid():
            raise FenceSafetyError("acceptance_file_owner")
        if listed.st_nlink != 1:
            raise FenceSafetyError("acceptance_file_hard_link")
        if listed.st_size > _RECEIPT_MAX_BYTES:
            raise FenceSafetyError("acceptance_file_malformed")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        os.set_inheritable(descriptor, False)
        current = os.fstat(descriptor)
        if current.st_dev != listed.st_dev or current.st_ino != listed.st_ino:
            raise FenceSafetyError("acceptance_file_identity")
        if not _private_regular(current):
            raise FenceSafetyError("acceptance_file_permissions")
        if not _local_apfs(descriptor, current.st_dev):
            raise FenceSafetyError("fence_filesystem")
        payload = os.read(descriptor, _RECEIPT_MAX_BYTES + 1)
        if len(payload) != current.st_size:
            raise FenceSafetyError("acceptance_file_unreadable")
        return payload
    except FenceSafetyError:
        raise
    except OSError as error:
        raise FenceSafetyError("acceptance_file_unreadable") from error
    finally:
        _close_all_preserving(
            (
                (descriptor, "acceptance file"),
                (parent_fd, "acceptance parent"),
            )
        )


def _drain_provision_pipes(process):
    for pipe in (process.stdout, process.stderr):
        if pipe is not None and not pipe.closed:
            try:
                pipe.read()
            except OSError:
                pass
            try:
                pipe.close()
            except OSError:
                pass
    if process.stdin is not None and not process.stdin.closed:
        try:
            process.stdin.close()
        except OSError:
            pass


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


def _unreleased_held(device, inode):
    """Return a permit that still blocks acquisition of this inode.

    The caller holds the per-inode mutex. That mutex is the synchronization
    boundary: only one same-process acquisition of this identity can be
    between this check and permit registration. A quarantined or otherwise
    unreleased permit remains a barrier even when its mutex is available.
    """

    with _HELD_GUARD:
        held = _HELD_BY_IDENTITY.get((device, inode))
    if held is None or held.released:
        return None
    return held


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
