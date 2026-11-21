"""Isolated recovery-helper core.

This module creates SQLite-consistent snapshots, authenticates each snapshot
and its manifest, and restores only into a fresh quarantine directory. It
does not create accounts, install services, or generate production keys.
The simulated API refuses production paths. The delegated API reads only the
fixed DELEGATED_SOURCE_PATH constant, and only through a read-only SQLite
connection.

SIMULATED_RECOVERY_BOUNDARY is a test separation only. The process that
supplies the key can forge a MAC, and it can replace an entire store with an
older internally consistent copy. OS_ENFORCED_RECOVERY_ISOLATION is false.
Passing any other boundary fails closed before publication. A caller-supplied
minimum generation detects replay of an older store; this module does not
persist that anchor, because a counter stored beside the snapshots can be
rolled back with them.

Source and store identities are explicit caller-supplied values. They are
authenticated in every manifest, head, and acceptance seal. Verification
compares those independent values with the seal; it does not adopt identities
from the manifest, a path, or an inode.

A generation is accepted only after its head is published, the store
directory fsync returns success, and an acceptance seal is written.
Visibility of a head without that seal is not acceptance. A failed store
fsync after the seal is visible is recorded as uncertainty when that failure
returns. A crash after the seal is visible and before its directory fsync
leaves no such record: SEAL_VISIBILITY_IS_NOT_DURABILITY records that this
filesystem cannot prove that case. The resulting visible seal is not proof
of durable publication.

sqlite3.connect still addresses files by path. Identity is rechecked
immediately before each connection. A substitution inside that remaining
interval cannot be removed on this platform.
"""

import errno
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path

from max_smart_db.config import (
    PRODUCTION_DATABASE_PATH,
    PROJECT_ROOT,
    SQLITE_BUSY_TIMEOUT_MS,
    SQLITE_BUSY_TIMEOUT_SECONDS,
)
from max_smart_db.safety import (
    SQLITE_INITIALIZATION_READY,
    SQLITE_INITIALIZATION_STATE_SUFFIX,
    SQLITE_INITIALIZATION_TEMP_SUFFIX,
    SQLITE_SIDECAR_SUFFIXES,
    DatabaseSafetyError,
    _check_ready_initialization_marker,
    validate_test_database_path,
    verify_local_apfs,
    verify_sqlite_connection_version,
    verify_sqlite_module_version,
)
from provider_fence import PRODUCTION_FENCE_PATH, _APPROVED_PIN_DIRECTORY


SIMULATED_RECOVERY_BOUNDARY = "simulated"
OS_ENFORCED_RECOVERY_ISOLATION = False
SEAL_VISIBILITY_IS_NOT_DURABILITY = True
DELEGATED_SOURCE_PATH = Path(
    "/Users/smart/Projects/Personal Workforce/data/max_smart.sqlite3"
)
_MANIFEST_FORMAT = "max-smart/recovery-manifest/v1"
_HEAD_FORMAT = "max-smart/recovery-head/v1"
_ACCEPTANCE_FORMAT = "max-smart/recovery-acceptance/v1"
_UNCERTAINTY_FORMAT = "max-smart/recovery-uncertainty/v1"
_MAC_ALGORITHM = "hmac-sha256"
_PREVIOUS_NONE = "none"
_SIDECAR_POLICY = "none"
_KEY_LENGTH = 32
_GENERATION_LIMIT = 999999
_MANIFEST_LIMIT = 8192
_HASH_LENGTH = 64
_DATABASE_NAME = "database.sqlite3"
_MANIFEST_NAME = "manifest.json"
_MANIFEST_MAC_NAME = "manifest.mac"
_HEAD_NAME = "head.json"
_HEAD_MAC_NAME = "head.mac"
_ACCEPTANCE_NAME = "acceptance.json"
_ACCEPTANCE_MAC_NAME = "acceptance.mac"
_UNCERTAIN_NAME = "acceptance.uncertain"
_UNCERTAIN_MAC_NAME = "acceptance.uncertain.mac"
_GENERATIONS_NAME = "generations"
_GENERATION_FILES = frozenset(
    {_DATABASE_NAME, _MANIFEST_NAME, _MANIFEST_MAC_NAME}
)
_STORE_FILES = frozenset(
    {
        _GENERATIONS_NAME,
        _HEAD_NAME,
        _HEAD_MAC_NAME,
        _ACCEPTANCE_NAME,
        _ACCEPTANCE_MAC_NAME,
        _UNCERTAIN_NAME,
        _UNCERTAIN_MAC_NAME,
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "boundary",
        "database_sha256",
        "database_size",
        "format",
        "generation",
        "mac_algorithm",
        "previous_manifest_sha256",
        "sidecar_policy",
        "source_identity",
        "sqlite_version",
        "store_identity",
    }
)
_HEAD_FIELDS = frozenset(
    {
        "boundary",
        "format",
        "generation",
        "mac_algorithm",
        "manifest_sha256",
        "source_identity",
        "store_identity",
    }
)
_ACCEPTANCE_FIELDS = frozenset(
    {
        "boundary",
        "format",
        "generation",
        "head_sha256",
        "mac_algorithm",
        "manifest_sha256",
        "source_identity",
        "store_identity",
    }
)
_UNCERTAINTY_FIELDS = frozenset(
    {
        "boundary",
        "format",
        "generation",
        "mac_algorithm",
        "manifest_sha256",
        "reason",
        "source_identity",
        "store_identity",
    }
)
_PIN_DIRECTORY = _APPROVED_PIN_DIRECTORY
_REFUSED_DIRECTORIES = (
    PROJECT_ROOT,
    PROJECT_ROOT / "data",
    PRODUCTION_DATABASE_PATH.parent,
    PRODUCTION_FENCE_PATH.parent,
    _PIN_DIRECTORY,
)
_REFUSED_FILES = (
    PRODUCTION_DATABASE_PATH,
    PRODUCTION_FENCE_PATH,
    PROJECT_ROOT / "data" / "current_session.json",
    PROJECT_ROOT / "data" / "fence.trust",
    _PIN_DIRECTORY / "provider-fence.pin",
    _PIN_DIRECTORY / "provider-fence.acceptance",
)
_CLOUD_PATH_COMPONENTS = {
    "Dropbox",
    "Google Drive",
    "iCloud Drive",
    "Mobile Documents",
    "OneDrive",
}
_SQLITE_VERSION_CODES = {
    "sqlite_module_version_invalid",
    "sqlite_version_unsupported",
    "sqlite_connection_version_invalid",
    "sqlite_version_mismatch",
}


class RecoveryHelperError(RuntimeError):
    """A recovery operation failed closed."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RecoveryPoint:
    """Authenticated identity of one simulated recovery snapshot."""

    generation: int
    database_sha256: str
    manifest_sha256: str
    database_size: int
    boundary: str
    source_identity: str
    store_identity: str


def create_recovery_snapshot(
    source,
    store,
    key,
    *,
    source_identity,
    store_identity,
    boundary=SIMULATED_RECOVERY_BOUNDARY,
    minimum_generation=None,
):
    """Append one authenticated snapshot to a simulated recovery store."""

    _require_boundary(boundary)
    key = _require_key(key)
    source_hex = _identity_hex(source_identity)
    store_hex = _identity_hex(store_identity)
    source_path, source_status = _require_source(source)
    return _publish_snapshot(
        source_path,
        source_status,
        store,
        key,
        source_hex,
        store_hex,
        minimum_generation,
    )


def create_delegated_recovery_snapshot(
    store,
    key,
    *,
    source_identity,
    store_identity,
    expected_owner_uid,
    expected_device,
    expected_inode,
    boundary=SIMULATED_RECOVERY_BOUNDARY,
    minimum_generation=None,
):
    """Snapshot the fixed delegated source into an authenticated store.

    The source path is DELEGATED_SOURCE_PATH. Callers supply the expected
    owner, device, and inode from the trusted service, not from an
    application socket. The simulated API remains the only path that accepts
    a caller-supplied source, and that API continues to refuse production paths.
    """

    _require_boundary(boundary)
    key = _require_key(key)
    source_hex = _identity_hex(source_identity)
    store_hex = _identity_hex(store_identity)
    owner = _require_expected_integer(expected_owner_uid)
    device = _require_expected_integer(expected_device)
    inode = _require_expected_integer(expected_inode)
    source_path, source_status, source_inventory = _require_delegated_source(
        owner,
        device,
        inode,
    )
    return _publish_snapshot(
        source_path,
        source_status,
        store,
        key,
        source_hex,
        store_hex,
        minimum_generation,
        source_owner=owner,
        source_device=device,
        source_inode=inode,
        source_inventory=source_inventory,
    )


def _publish_snapshot(
    source_path,
    source_status,
    store,
    key,
    source_hex,
    store_hex,
    minimum_generation,
    *,
    source_owner=None,
    source_device=None,
    source_inode=None,
    source_inventory=None,
):
    """Publish one already validated source through the accepted store protocol."""

    store_path = _require_directory(store)
    if source_path == store_path:
        _fail("path_refused")
    current = _load_store(
        store_path,
        key,
        minimum_generation,
        source_hex,
        store_hex,
    )
    generation = 1 if current is None else current.generation + 1
    if generation > _GENERATION_LIMIT:
        _fail("store_inconsistent")
    if (
        current is not None
        and minimum_generation is not None
        and current.generation < minimum_generation
    ):
        _fail("rollback")
    if current is None and minimum_generation not in (None,):
        _require_generation_number(minimum_generation)
        _fail("rollback")
    previous = (
        _PREVIOUS_NONE if current is None else current.manifest_sha256
    )
    generation_dir = _generation_dir(store_path, generation)
    if _status_or_none(generation_dir) is not None:
        _fail("store_inconsistent")
    _ensure_generations(store_path)
    _mkdir_private(generation_dir)
    temporary_path = None
    temporary_status = None
    try:
        temporary_path, temporary_status = _create_temp(generation_dir)
        module_version = _snapshot_database(
            source_path,
            source_status,
            temporary_path,
            temporary_status,
            source_owner=source_owner,
            source_device=source_device,
            source_inode=source_inode,
            source_inventory=source_inventory,
        )
        if source_inventory is not None:
            _require_delegated_inventory(source_path, source_inventory)
        _clear_empty_temp_sidecars(temporary_path)
        temporary_status = _fsync_file(
            temporary_path,
            temporary_status,
            uncertain=False,
        )
        digest, size = _hash_file(temporary_path)
        database_path = generation_dir / _DATABASE_NAME
        _link_exclusive(temporary_path, database_path)
        _remove_owned_file(temporary_path, temporary_status)
        temporary_path = None
        linked = _hash_file(database_path)
        if linked != (digest, size):
            _fail("identity")
        _refuse_sidecars(database_path)
        body = _manifest_body(
            generation=generation,
            previous=previous,
            digest=digest,
            size=size,
            sqlite_version=".".join(str(part) for part in module_version),
            source_identity=source_hex,
            store_identity=store_hex,
        )
        _publish_pair(
            generation_dir,
            _MANIFEST_NAME,
            _MANIFEST_MAC_NAME,
            body,
            key,
        )
        # Directory fsync 1: generation contents. Directory fsync 2: parent
        # entry. Head publication is refused when either fails.
        _fsync_directory(generation_dir, uncertain=True)
        _fsync_directory(store_path / _GENERATIONS_NAME, uncertain=True)
        manifest_sha256 = _publish_head(
            store_path,
            key,
            generation,
            body,
            current,
            source_hex,
            store_hex,
        )
        # Directory fsync 3: both head replacements are now visible.
        # A failure here leaves the head unpublished for acceptance.
        _fsync_directory(store_path, uncertain=True)
        _publish_acceptance(
            store_path,
            key,
            generation,
            manifest_sha256,
            source_hex,
            store_hex,
        )
        try:
            # Directory fsync 4: the acceptance seal is already visible.
            _fsync_directory(store_path, uncertain=True)
        except RecoveryHelperError as error:
            if error.code == "recovery_durability_uncertain":
                _mark_acceptance_uncertain(
                    store_path,
                    key,
                    generation,
                    manifest_sha256,
                    source_hex,
                    store_hex,
                )
            raise
        confirmed = _load_store(
            store_path,
            key,
            generation,
            source_hex,
            store_hex,
        )
        if confirmed.generation != generation:
            _fail("identity")
        return confirmed
    except RecoveryHelperError:
        raise
    finally:
        if temporary_path is not None:
            _cleanup_temp(temporary_path, temporary_status)


def verify_recovery_point(
    store,
    key,
    *,
    source_identity,
    store_identity,
    boundary=SIMULATED_RECOVERY_BOUNDARY,
    minimum_generation=None,
):
    """Verify the accepted head and its complete generation chain."""

    _require_boundary(boundary)
    key = _require_key(key)
    source_hex = _identity_hex(source_identity)
    store_hex = _identity_hex(store_identity)
    store_path = _require_directory(store)
    point = _load_store(
        store_path,
        key,
        minimum_generation,
        source_hex,
        store_hex,
    )
    if point is None:
        _fail("manifest_invalid")
    return point


def restore_recovery_point(
    store,
    quarantine,
    key,
    *,
    source_identity,
    store_identity,
    boundary=SIMULATED_RECOVERY_BOUNDARY,
    minimum_generation=None,
):
    """Copy the verified head snapshot into a fresh empty quarantine."""

    _require_boundary(boundary)
    key = _require_key(key)
    source_hex = _identity_hex(source_identity)
    store_hex = _identity_hex(store_identity)
    store_path = _require_directory(store)
    quarantine_path = _require_directory(quarantine)
    if quarantine_path == store_path:
        _fail("quarantine_refused")
    point = _load_store(
        store_path,
        key,
        minimum_generation,
        source_hex,
        store_hex,
    )
    if point is None:
        _fail("manifest_invalid")
    if _directory_entries(quarantine_path):
        _fail("quarantine_refused")
    source = _generation_dir(store_path, point.generation) / _DATABASE_NAME
    temporary_path = None
    temporary_status = None
    published = False
    try:
        temporary_path, temporary_status = _create_temp(quarantine_path)
        _copy_verified_file(
            source,
            temporary_path,
            point.database_sha256,
            point.database_size,
        )
        temporary_status = _fsync_file(
            temporary_path,
            temporary_status,
            uncertain=False,
        )
        _require_integrity_file(temporary_path, temporary_status)
        _refuse_sidecars(temporary_path)
        destination = quarantine_path / _DATABASE_NAME
        _refuse_destination_clear(destination)
        _link_exclusive(temporary_path, destination)
        _remove_owned_file(temporary_path, temporary_status)
        temporary_path = None
        published = True
        digest, size = _hash_file(destination)
        if (digest, size) != (point.database_sha256, point.database_size):
            _fail("identity")
        _refuse_sidecars(destination)
        _fsync_directory(quarantine_path, uncertain=True)
        final = destination.lstat()
        if final.st_nlink != 1 or stat.S_IMODE(final.st_mode) != 0o600:
            _fail("identity")
        return point
    except RecoveryHelperError:
        raise
    finally:
        if not published and temporary_path is not None:
            _cleanup_temp(temporary_path, temporary_status)


def _fail(code):
    raise RecoveryHelperError(f"recovery_{code}") from None


def _require_boundary(boundary):
    if boundary != SIMULATED_RECOVERY_BOUNDARY or OS_ENFORCED_RECOVERY_ISOLATION:
        _fail("boundary_unproven")


def _require_key(key):
    if type(key) is not bytes or len(key) != _KEY_LENGTH:
        _fail("key_unavailable")
    return key


def _require_generation_number(value):
    if type(value) is not int or isinstance(value, bool):
        _fail("manifest_invalid")
    if value < 1 or value > _GENERATION_LIMIT:
        _fail("manifest_invalid")
    return value


def _map_safety(error):
    if error.code in _SQLITE_VERSION_CODES:
        _fail("sqlite_version")
    if error.code == "database_initialization_incomplete":
        _fail("marker_invalid")
    if error.code == "path_symlink":
        _fail("symlink")
    if error.code in {
        "cloud_path",
        "filesystem_not_local_apfs",
        "unsupported_filesystem_platform",
        "filesystem_check_failed",
    }:
        _fail("filesystem")
    if error.code in {
        "filesystem_identity_unavailable",
    }:
        _fail("identity")
    _fail("path_refused")


def _refused_path(path):
    if path in _REFUSED_FILES:
        return True
    return any(
        path == directory or directory in path.parents
        for directory in _REFUSED_DIRECTORIES
    )


def _candidate(path, *, must_exist, directory):
    if type(path) is not str and not isinstance(path, Path):
        _fail("path_refused")
    candidate = Path(path)
    if not candidate.is_absolute() or _refused_path(candidate):
        _fail("path_refused")
    if any(part in _CLOUD_PATH_COMPONENTS for part in candidate.parts):
        _fail("filesystem")
    try:
        validated = validate_test_database_path(candidate)
        verify_local_apfs(
            candidate.parent if candidate.parent != candidate else candidate
        )
    except DatabaseSafetyError as error:
        _map_safety(error)
    if validated != candidate:
        _fail("symlink")
    try:
        listed = candidate.lstat()
    except FileNotFoundError:
        listed = None
    except OSError:
        _fail("identity")
    if listed is None:
        if must_exist:
            _fail("source_missing" if not directory else "path_refused")
        return candidate, None
    if stat.S_ISLNK(listed.st_mode):
        _fail("symlink")
    if directory and not stat.S_ISDIR(listed.st_mode):
        _fail("path_refused")
    if not directory and not stat.S_ISREG(listed.st_mode):
        _fail("source_invalid")
    return candidate, listed


def _require_source(path):
    candidate, listed = _candidate(path, must_exist=True, directory=False)
    _require_private_directory(candidate.parent)
    _require_private_file(listed)
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        status = _status_or_none(Path(f"{candidate}{suffix}"))
        if status is None:
            continue
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            _fail("sidecar_rejected")
        if status.st_uid != os.geteuid() or stat.S_IMODE(status.st_mode) != 0o600:
            _fail("permissions")
        if status.st_nlink != 1:
            _fail("identity")
    try:
        _check_ready_initialization_marker(candidate)
    except DatabaseSafetyError as error:
        _map_safety(error)
    return candidate, listed


def _delegated_runtime_uid():
    """Return the uid that must not own the delegated source."""

    return os.geteuid()


def _require_expected_integer(value):
    if type(value) is not int or isinstance(value, bool) or value < 0:
        _fail("identity_invalid")
    return value


def _require_delegated_source(owner, device, inode):
    candidate, listed = _delegated_fixed_path()
    if stat.S_ISLNK(listed.st_mode):
        _fail("symlink")
    if not stat.S_ISREG(listed.st_mode) or listed.st_size <= 0:
        _fail("source_invalid")
    if stat.S_IMODE(listed.st_mode) != 0o600:
        _fail("permissions")
    if listed.st_nlink != 1:
        _fail("identity")
    if listed.st_uid != owner:
        _fail("owner")
    if listed.st_dev != device or listed.st_ino != inode:
        _fail("identity")
    if listed.st_uid == _delegated_runtime_uid():
        _fail("owner")
    _refuse_source_write(candidate)
    _check_delegated_marker(candidate, owner)
    _require_delegated_sidecars(candidate, owner)
    current = candidate.lstat()
    if (
        current.st_dev != device
        or current.st_ino != inode
        or current.st_uid != owner
        or stat.S_IMODE(current.st_mode) != 0o600
        or current.st_nlink != 1
    ):
        _fail("identity")
    inventory = [
        _seal_delegated_artifact(candidate, owner),
        _seal_delegated_artifact(
            Path(f"{candidate}{SQLITE_INITIALIZATION_STATE_SUFFIX}"),
            owner,
        ),
    ]
    for suffix in ("-wal", "-shm"):
        artifact = Path(f"{candidate}{suffix}")
        if _status_or_none(artifact) is not None:
            inventory.append(_seal_delegated_artifact(artifact, owner))
    return candidate, current, tuple(inventory)


def _delegated_fixed_path():
    path = DELEGATED_SOURCE_PATH
    if not isinstance(path, Path) or not path.is_absolute():
        _fail("path_refused")
    if any(part in _CLOUD_PATH_COMPONENTS for part in path.parts):
        _fail("filesystem")
    current = Path(path.anchor)
    parts = path.relative_to(path.anchor).parts
    listed = None
    for index, part in enumerate(parts):
        current = current / part
        leaf = index == len(parts) - 1
        try:
            listed = current.lstat()
        except FileNotFoundError:
            _fail("source_missing" if leaf else "path_refused")
        except OSError:
            _fail("identity")
        if stat.S_ISLNK(listed.st_mode):
            _fail("symlink")
        if leaf and not stat.S_ISREG(listed.st_mode):
            _fail("source_invalid")
        if not leaf and not stat.S_ISDIR(listed.st_mode):
            _fail("path_refused")
    return path, listed


def _refuse_source_write(path):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EPERM):
            return
        _fail("permissions")
    try:
        os.close(descriptor)
    except OSError:
        _fail("permissions")
    _fail("permissions")


def _check_delegated_marker(database, owner):
    temporary = Path(f"{database}{SQLITE_INITIALIZATION_TEMP_SUFFIX}")
    if _status_or_none(temporary) is not None:
        _fail("marker_invalid")
    marker = Path(f"{database}{SQLITE_INITIALIZATION_STATE_SUFFIX}")
    try:
        descriptor = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        _fail("marker_invalid")
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_ISLNK(status.st_mode)
            or stat.S_IMODE(status.st_mode) != 0o600
            or status.st_uid != owner
            or status.st_nlink != 1
        ):
            _fail("marker_invalid")
        contents = os.read(descriptor, len(SQLITE_INITIALIZATION_READY) + 1)
        if contents != SQLITE_INITIALIZATION_READY:
            _fail("marker_invalid")
    except OSError:
        _fail("marker_invalid")
    finally:
        try:
            os.close(descriptor)
        except OSError:
            _fail("marker_invalid")


def _require_delegated_sidecars(database, owner):
    wal = _status_or_none(Path(f"{database}-wal"))
    shm = _status_or_none(Path(f"{database}-shm"))
    journal = _status_or_none(Path(f"{database}-journal"))
    if journal is not None or (wal is not None and shm is None):
        _fail("sidecar_rejected")
    for status in (wal, shm):
        if status is None:
            continue
        if (
            stat.S_ISLNK(status.st_mode)
            or not stat.S_ISREG(status.st_mode)
            or status.st_uid != owner
            or stat.S_IMODE(status.st_mode) != 0o600
            or status.st_nlink != 1
        ):
            _fail("sidecar_rejected")


def _seal_delegated_artifact(path, owner):
    """Prove one source artifact is immutable before SQLite opens it."""

    listed = path.lstat()
    _refuse_source_write(path)
    current = path.lstat()
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or current.st_dev != listed.st_dev
        or current.st_ino != listed.st_ino
        or current.st_uid != owner
        or stat.S_IMODE(current.st_mode) != 0o600
        or current.st_nlink != 1
        or current.st_size != listed.st_size
    ):
        _fail("identity")
    return (
        path.name,
        current.st_dev,
        current.st_ino,
        current.st_uid,
        stat.S_IMODE(current.st_mode),
        current.st_nlink,
        current.st_size,
        _digest_delegated_artifact(path, current, owner),
    )


def _digest_delegated_artifact(path, listed, owner):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        _fail("identity")
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != listed.st_dev
            or current.st_ino != listed.st_ino
            or current.st_uid != owner
            or stat.S_IMODE(current.st_mode) != 0o600
            or current.st_nlink != 1
            or current.st_size != listed.st_size
        ):
            _fail("identity")
        hasher = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
            total += len(chunk)
        if total != current.st_size:
            _fail("identity")
        return hasher.hexdigest()
    except OSError:
        _fail("identity")
    finally:
        try:
            os.close(descriptor)
        except OSError:
            _fail("identity")


def _require_delegated_inventory(database, inventory):
    actual = _delegated_dependency_names(database)
    expected = {item[0] for item in inventory}
    if actual != expected:
        _fail("sidecar_rejected")
    parent = database.parent
    for name, device, inode, owner, mode, links, size, digest in inventory:
        path = parent / name
        try:
            listed = path.lstat()
        except OSError:
            _fail("identity")
        if stat.S_ISLNK(listed.st_mode):
            _fail("symlink")
        if (
            not stat.S_ISREG(listed.st_mode)
            or listed.st_dev != device
            or listed.st_ino != inode
            or listed.st_uid != owner
            or stat.S_IMODE(listed.st_mode) != mode
            or listed.st_nlink != links
            or listed.st_size != size
        ):
            _fail("identity")
        if _digest_delegated_artifact(path, listed, owner) != digest:
            _fail("identity")


def _delegated_dependency_names(path):
    names = {
        path.name,
        f"{path.name}{SQLITE_INITIALIZATION_STATE_SUFFIX}",
        f"{path.name}{SQLITE_INITIALIZATION_TEMP_SUFFIX}",
    }
    names.update(f"{path.name}{suffix}" for suffix in SQLITE_SIDECAR_SUFFIXES)
    return {
        name
        for name in names
        if _status_or_none(path.parent / name) is not None
    }


def _recheck_delegated_source(path, expected, owner, device, inode):
    current = _recheck(path, expected)
    if (
        current.st_uid != owner
        or current.st_dev != device
        or current.st_ino != inode
        or current.st_nlink != 1
    ):
        _fail("identity")
    return current


def _require_directory(path):
    candidate, listed = _candidate(path, must_exist=True, directory=True)
    _require_private_directory_status(listed)
    return candidate


def _require_private_directory(path):
    try:
        listed = path.lstat()
    except OSError:
        _fail("path_refused")
    _require_private_directory_status(listed)
    return listed


def _require_private_directory_status(listed):
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISDIR(listed.st_mode):
        _fail("symlink" if stat.S_ISLNK(listed.st_mode) else "path_refused")
    if listed.st_uid != os.geteuid():
        _fail("owner")
    if stat.S_IMODE(listed.st_mode) != 0o700:
        _fail("permissions")


def _require_private_file(listed):
    if listed.st_uid != os.geteuid():
        _fail("owner")
    if stat.S_IMODE(listed.st_mode) != 0o600:
        _fail("permissions")
    if listed.st_nlink != 1:
        _fail("identity")
    if listed.st_size <= 0:
        _fail("source_invalid")


def _status_or_none(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        _fail("identity")


def _generation_dir(store, generation):
    return store / _GENERATIONS_NAME / f"{generation:06d}"


def _ensure_generations(store):
    path = store / _GENERATIONS_NAME
    status = _status_or_none(path)
    if status is None:
        _mkdir_private(path)
        return True
    _require_private_directory_status(status)
    return False


def _mkdir_private(path):
    old_umask = os.umask(0o077)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        _fail("exists")
    except OSError:
        _fail("interrupted")
    finally:
        os.umask(old_umask)
    _require_private_directory(path)


def _create_temp(parent):
    path = parent / f".snapshot-{secrets.token_hex(16)}.sqlite3"
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    old_umask = os.umask(0o077)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        _fail("exists")
    except OSError:
        _fail("interrupted")
    finally:
        os.umask(old_umask)
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or current.st_uid != os.geteuid()
            or stat.S_IMODE(current.st_mode) != 0o600
        ):
            _fail("identity")
    finally:
        os.close(descriptor)
    return path, path.lstat()


def _snapshot_database(
    source,
    source_status,
    temporary,
    temporary_status,
    *,
    source_owner=None,
    source_device=None,
    source_inode=None,
    source_inventory=None,
):
    try:
        module_version = verify_sqlite_module_version()
    except DatabaseSafetyError as error:
        _map_safety(error)
    source_status = _recheck(source, source_status)
    if source_owner is None:
        try:
            _check_ready_initialization_marker(source)
        except DatabaseSafetyError as error:
            _map_safety(error)
    else:
        _recheck_delegated_source(
            source,
            source_status,
            source_owner,
            source_device,
            source_inode,
        )
        _check_delegated_marker(source, source_owner)
    source_connection = _connect(source, readonly=True)
    target_connection = None
    try:
        try:
            verify_sqlite_connection_version(source_connection, module_version)
        except DatabaseSafetyError as error:
            _map_safety(error)
        except sqlite3.OperationalError as error:
            _raise_sqlite(error)
        _require_integrity(source_connection)
        temporary_status = _recheck(temporary, temporary_status)
        target_connection = _connect(temporary, readonly=False)
        try:
            verify_sqlite_connection_version(target_connection, module_version)
        except DatabaseSafetyError as error:
            _map_safety(error)
        except sqlite3.OperationalError as error:
            _raise_sqlite(error)
        try:
            _require_synchronous_full(target_connection)
            source_connection.backup(target_connection, pages=1, sleep=0.0)
            _require_pragma_text(
                target_connection,
                "PRAGMA locking_mode=EXCLUSIVE",
                "exclusive",
            )
            _require_pragma_text(
                target_connection,
                "PRAGMA journal_mode=DELETE",
                "delete",
            )
            _require_pragma_text(
                target_connection,
                "PRAGMA locking_mode=NORMAL",
                "normal",
            )
            _require_integrity(target_connection)
        except sqlite3.OperationalError as error:
            _raise_sqlite(error)
        except sqlite3.DatabaseError:
            _fail("integrity")
    finally:
        for connection in (target_connection, source_connection):
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
    _recheck(temporary, temporary_status)
    if source_owner is not None:
        _recheck_delegated_source(
            source,
            source_status,
            source_owner,
            source_device,
            source_inode,
        )
        _require_delegated_inventory(source, source_inventory)
    return module_version


def _require_synchronous_full(connection):
    try:
        connection.execute("PRAGMA synchronous=FULL")
        row = connection.execute("PRAGMA synchronous").fetchone()
    except sqlite3.OperationalError as error:
        _raise_sqlite(error)
    if not isinstance(row, tuple) or len(row) != 1 or row[0] != 2:
        _fail("interrupted")


def _require_pragma_text(connection, statement, expected):
    try:
        row = connection.execute(statement).fetchone()
    except sqlite3.OperationalError as error:
        _raise_sqlite(error)
    if (
        not isinstance(row, tuple)
        or len(row) != 1
        or str(row[0]).lower() != expected
    ):
        _fail("interrupted")


def _connect(path, *, readonly):
    query = "mode=ro" if readonly else "mode=rw"
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?{query}",
            uri=True,
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
        )
    except sqlite3.OperationalError as error:
        _raise_sqlite(error)
    except sqlite3.DatabaseError:
        _fail("integrity")
    try:
        connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        if readonly:
            connection.execute("PRAGMA query_only=ON")
    except sqlite3.OperationalError as error:
        connection.close()
        _raise_sqlite(error)
    except sqlite3.DatabaseError:
        connection.close()
        _fail("integrity")
    return connection


def _require_integrity(connection):
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        quick = connection.execute("PRAGMA quick_check").fetchall()
    except sqlite3.OperationalError as error:
        _raise_sqlite(error)
    except sqlite3.DatabaseError:
        _fail("integrity")
    if integrity != [("ok",)] or quick != [("ok",)]:
        _fail("integrity")


def _require_integrity_file(path, status):
    _recheck(path, status)
    connection = _connect(path, readonly=True)
    try:
        _require_integrity(connection)
    finally:
        try:
            connection.close()
        except sqlite3.Error:
            pass


def _raise_sqlite(error):
    text = str(error).lower()
    if "locked" in text or "busy" in text:
        _fail("lock_contention")
    _fail("interrupted")


def _clear_empty_temp_sidecars(path):
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        sidecar = Path(f"{path}{suffix}")
        status = _status_or_none(sidecar)
        if status is None:
            continue
        if (
            stat.S_ISLNK(status.st_mode)
            or not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.geteuid()
            or status.st_nlink != 1
            or status.st_size != 0
        ):
            _fail("interrupted")
        _remove_owned_file(sidecar, status)


def _refuse_sidecars(database):
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        if _status_or_none(Path(f"{database}{suffix}")) is not None:
            _fail("sidecar_rejected")


def _refuse_destination_clear(destination):
    if _status_or_none(destination) is not None:
        _fail("exists")
    _refuse_sidecars(destination)


def _recheck(path, expected):
    try:
        current = path.lstat()
    except OSError:
        _fail("identity")
    if (
        current.st_dev != expected.st_dev
        or current.st_ino != expected.st_ino
        or current.st_uid != expected.st_uid
        or stat.S_IMODE(current.st_mode) != stat.S_IMODE(expected.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
    ):
        _fail("identity")
    return current


def _fsync_file(path, expected, *, uncertain):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        current = os.fstat(descriptor)
        if (
            current.st_dev != expected.st_dev
            or current.st_ino != expected.st_ino
            or not stat.S_ISREG(current.st_mode)
        ):
            _fail("identity")
        try:
            os.fsync(descriptor)
        except OSError:
            _fail("durability_uncertain" if uncertain else "interrupted")
    finally:
        os.close(descriptor)
    return _recheck(path, expected)


def _fsync_directory(path, *, uncertain):
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | os.O_NOFOLLOW
        | os.O_CLOEXEC
    )
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _fail("durability_uncertain" if uncertain else "interrupted")
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISDIR(current.st_mode) or current.st_uid != os.geteuid():
            _fail("identity")
        try:
            os.fsync(descriptor)
        except OSError:
            _fail("durability_uncertain" if uncertain else "interrupted")
    finally:
        os.close(descriptor)


def _hash_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_ISLNK(status.st_mode)
            or status.st_uid != os.geteuid()
            or stat.S_IMODE(status.st_mode) != 0o600
            or status.st_nlink != 1
        ):
            _fail("identity")
        hasher = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_dev != status.st_dev
            or after.st_ino != status.st_ino
            or after.st_size != status.st_size
            or total != status.st_size
        ):
            _fail("identity")
        return hasher.hexdigest(), total
    finally:
        os.close(descriptor)


def _copy_verified_file(source, destination, digest, size):
    source_descriptor = os.open(
        source,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    destination_descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        source_status = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(source_status.st_mode)
            or source_status.st_uid != os.geteuid()
            or stat.S_IMODE(source_status.st_mode) != 0o600
            or source_status.st_size != size
        ):
            _fail("identity")
        hasher = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
            total += len(chunk)
            written = 0
            while written < len(chunk):
                try:
                    count = os.write(destination_descriptor, chunk[written:])
                except OSError:
                    _fail("interrupted")
                if count <= 0:
                    _fail("interrupted")
                written += count
        if hasher.hexdigest() != digest or total != size:
            _fail("content_mismatch")
    finally:
        os.close(source_descriptor)
        os.close(destination_descriptor)


def _link_exclusive(source, destination):
    if _status_or_none(destination) is not None:
        _fail("exists")
    _refuse_sidecars(destination)
    try:
        os.link(source, destination)
    except FileExistsError:
        _fail("exists")
    except OSError:
        _fail("interrupted")


def _canonical(payload):
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    except (TypeError, UnicodeEncodeError, ValueError):
        _fail("manifest_invalid")
    return encoded


def _manifest_body(
    *,
    generation,
    previous,
    digest,
    size,
    sqlite_version,
    source_identity,
    store_identity,
):
    return _canonical(
        {
            "boundary": SIMULATED_RECOVERY_BOUNDARY,
            "database_sha256": digest,
            "database_size": size,
            "format": _MANIFEST_FORMAT,
            "generation": generation,
            "mac_algorithm": _MAC_ALGORITHM,
            "previous_manifest_sha256": previous,
            "sidecar_policy": _SIDECAR_POLICY,
            "source_identity": source_identity,
            "sqlite_version": sqlite_version,
            "store_identity": store_identity,
        }
    )


def _head_body(generation, manifest_sha256, source_identity, store_identity):
    return _canonical(
        {
            "boundary": SIMULATED_RECOVERY_BOUNDARY,
            "format": _HEAD_FORMAT,
            "generation": generation,
            "mac_algorithm": _MAC_ALGORITHM,
            "manifest_sha256": manifest_sha256,
            "source_identity": source_identity,
            "store_identity": store_identity,
        }
    )


def _acceptance_body(
    generation,
    manifest_sha256,
    head_sha256,
    source_identity,
    store_identity,
):
    return _canonical(
        {
            "boundary": SIMULATED_RECOVERY_BOUNDARY,
            "format": _ACCEPTANCE_FORMAT,
            "generation": generation,
            "head_sha256": head_sha256,
            "mac_algorithm": _MAC_ALGORITHM,
            "manifest_sha256": manifest_sha256,
            "source_identity": source_identity,
            "store_identity": store_identity,
        }
    )


def _mac(key, payload):
    return hmac.new(key, payload, hashlib.sha256).digest()


def _publish_pair(parent, body_name, mac_name, body, key):
    body_path = parent / body_name
    mac_path = parent / mac_name
    _write_exclusive(body_path, body)
    _write_exclusive(mac_path, _mac(key, body).hex().encode("ascii"))
    _fsync_file(body_path, body_path.lstat(), uncertain=False)
    _fsync_file(mac_path, mac_path.lstat(), uncertain=False)


def _write_exclusive(path, payload):
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    old_umask = os.umask(0o077)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        _fail("exists")
    except OSError:
        _fail("interrupted")
    finally:
        os.umask(old_umask)
    try:
        remaining = payload
        while remaining:
            try:
                count = os.write(descriptor, remaining)
            except OSError:
                _fail("interrupted")
            if count <= 0:
                _fail("interrupted")
            remaining = remaining[count:]
        os.fsync(descriptor)
    except RecoveryHelperError:
        raise
    except OSError:
        _fail("interrupted")
    finally:
        os.close(descriptor)


def _published_head_generation(store, key, source_identity, store_identity):
    body = store / _HEAD_NAME
    mac = store / _HEAD_MAC_NAME
    if _status_or_none(body) is None and _status_or_none(mac) is None:
        return None
    if _status_or_none(body) is None or _status_or_none(mac) is None:
        _fail("contradictory")
    payload = _read_authenticated_json(body, mac, key, _HEAD_FIELDS, _HEAD_FORMAT)
    _require_bound_identity(payload["source_identity"], source_identity)
    _require_bound_identity(payload["store_identity"], store_identity)
    return _require_generation_number(payload["generation"])


def _publish_head(
    store,
    key,
    generation,
    manifest_body,
    current,
    source_identity,
    store_identity,
):
    observed = _published_head_generation(store, key, source_identity, store_identity)
    if current is None and observed is not None:
        _fail("interrupted")
    if current is not None and observed != current.generation:
        _fail("interrupted")
    manifest_sha256 = hashlib.sha256(manifest_body).hexdigest()
    body = _head_body(
        generation,
        manifest_sha256,
        source_identity,
        store_identity,
    )
    pending_body = store / f".head-{generation:06d}.json"
    pending_mac = store / f".head-{generation:06d}.mac"
    _write_exclusive(pending_body, body)
    _write_exclusive(pending_mac, _mac(key, body).hex().encode("ascii"))
    _fsync_file(pending_body, pending_body.lstat(), uncertain=False)
    _fsync_file(pending_mac, pending_mac.lstat(), uncertain=False)
    if _published_head_generation(store, key, source_identity, store_identity) != observed:
        _fail("interrupted")
    head_body = store / _HEAD_NAME
    head_mac = store / _HEAD_MAC_NAME
    try:
        os.replace(pending_body, head_body)
    except OSError:
        _fail("durability_uncertain")
    try:
        os.replace(pending_mac, head_mac)
    except OSError:
        _fail("durability_uncertain")
    return manifest_sha256


def _publish_acceptance(
    store,
    key,
    generation,
    manifest_sha256,
    source_identity,
    store_identity,
):
    head_bytes = _read_limited(store / _HEAD_NAME, _MANIFEST_LIMIT)
    body = _acceptance_body(
        generation,
        manifest_sha256,
        hashlib.sha256(head_bytes).hexdigest(),
        source_identity,
        store_identity,
    )
    pending_body = store / f".acceptance-{generation:06d}.json"
    pending_mac = store / f".acceptance-{generation:06d}.mac"
    _write_exclusive(pending_body, body)
    _write_exclusive(pending_mac, _mac(key, body).hex().encode("ascii"))
    _fsync_file(pending_body, pending_body.lstat(), uncertain=False)
    _fsync_file(pending_mac, pending_mac.lstat(), uncertain=False)
    try:
        os.replace(pending_body, store / _ACCEPTANCE_NAME)
        os.replace(pending_mac, store / _ACCEPTANCE_MAC_NAME)
    except OSError:
        _fail("durability_uncertain")


def _mark_acceptance_uncertain(
    store,
    key,
    generation,
    manifest_sha256,
    source_identity,
    store_identity,
):
    body = _canonical(
        {
            "boundary": SIMULATED_RECOVERY_BOUNDARY,
            "format": _UNCERTAINTY_FORMAT,
            "generation": generation,
            "mac_algorithm": _MAC_ALGORITHM,
            "manifest_sha256": manifest_sha256,
            "reason": "seal_directory_fsync",
            "source_identity": source_identity,
            "store_identity": store_identity,
        }
    )
    try:
        _publish_pair(store, _UNCERTAIN_NAME, _UNCERTAIN_MAC_NAME, body, key)
    except RecoveryHelperError:
        return


def _load_store(store, key, minimum_generation, source_identity, store_identity):
    entries = set(_directory_entries(store))
    unexpected = entries - _STORE_FILES
    if unexpected:
        _classify_unexpected(unexpected)
    _refuse_uncertainty(store, key, entries, source_identity, store_identity)
    generations = store / _GENERATIONS_NAME
    head_complete = _HEAD_NAME in entries and _HEAD_MAC_NAME in entries
    head_partial = (_HEAD_NAME in entries) ^ (_HEAD_MAC_NAME in entries)
    acceptance_complete = (
        _ACCEPTANCE_NAME in entries and _ACCEPTANCE_MAC_NAME in entries
    )
    acceptance_partial = (_ACCEPTANCE_NAME in entries) ^ (
        _ACCEPTANCE_MAC_NAME in entries
    )
    has_generations = _GENERATIONS_NAME in entries
    if head_partial or acceptance_partial:
        _fail("contradictory")
    if (
        not has_generations
        and not head_complete
        and not acceptance_complete
    ):
        _require_optional_anchor(0, minimum_generation)
        return None
    if head_complete and not acceptance_complete:
        _fail("unconfirmed")
    if acceptance_complete and not head_complete:
        _fail("contradictory")
    if not has_generations or not head_complete or not acceptance_complete:
        _fail("store_inconsistent")
    _require_private_directory(generations)
    generation_names = _directory_entries(generations)
    head_bytes, head = _read_authenticated_bytes(
        store / _HEAD_NAME,
        store / _HEAD_MAC_NAME,
        key,
        _HEAD_FIELDS,
        _HEAD_FORMAT,
    )
    acceptance = _read_authenticated_json(
        store / _ACCEPTANCE_NAME,
        store / _ACCEPTANCE_MAC_NAME,
        key,
        _ACCEPTANCE_FIELDS,
        _ACCEPTANCE_FORMAT,
    )
    generation = _require_generation_number(head["generation"])
    if _require_generation_number(acceptance["generation"]) != generation:
        _fail("contradictory")
    if acceptance["head_sha256"] != hashlib.sha256(head_bytes).hexdigest():
        _fail("contradictory")
    if acceptance["manifest_sha256"] != head["manifest_sha256"]:
        _fail("contradictory")
    for payload in (head, acceptance):
        if payload["boundary"] != SIMULATED_RECOVERY_BOUNDARY:
            _fail("boundary_unproven")
        if payload["mac_algorithm"] != _MAC_ALGORITHM:
            _fail("manifest_invalid")
        _require_bound_identity(payload["source_identity"], source_identity)
        _require_bound_identity(payload["store_identity"], store_identity)
    if head["boundary"] != SIMULATED_RECOVERY_BOUNDARY:
        _fail("boundary_unproven")
    if head["mac_algorithm"] != _MAC_ALGORITHM:
        _fail("manifest_invalid")
    present = []
    for name in generation_names:
        if len(name) != 6 or not name.isdigit() or name != f"{int(name):06d}":
            _fail("store_inconsistent")
        present.append(int(name))
    present.sort()
    if max(present, default=0) > generation:
        _fail("rollback")
    if present != list(range(1, generation + 1)):
        _fail("store_inconsistent")
    manifest_hash = None
    digest = None
    size = None
    for number in range(1, generation + 1):
        directory = _generation_dir(store, number)
        _require_private_directory(directory)
        names = set(_directory_entries(directory))
        extra = names - _GENERATION_FILES
        if extra:
            _classify_unexpected(extra)
        if names != _GENERATION_FILES:
            _fail("store_inconsistent")
        database = directory / _DATABASE_NAME
        _refuse_sidecars(database)
        body_bytes, payload = _read_authenticated_bytes(
            directory / _MANIFEST_NAME,
            directory / _MANIFEST_MAC_NAME,
            key,
            _MANIFEST_FIELDS,
            _MANIFEST_FORMAT,
        )
        body_hash = hashlib.sha256(body_bytes).hexdigest()
        expected_previous = _PREVIOUS_NONE if number == 1 else manifest_hash
        if payload["previous_manifest_sha256"] != expected_previous:
            _fail("store_inconsistent")
        if payload["generation"] != number:
            _fail("store_inconsistent")
        if payload["boundary"] != SIMULATED_RECOVERY_BOUNDARY:
            _fail("boundary_unproven")
        if (
            payload["mac_algorithm"] != _MAC_ALGORITHM
            or payload["sidecar_policy"] != _SIDECAR_POLICY
        ):
            _fail("manifest_invalid")
        _require_bound_identity(payload["source_identity"], source_identity)
        _require_bound_identity(payload["store_identity"], store_identity)
        _require_hash(payload["database_sha256"])
        if (
            type(payload["database_size"]) is not int
            or isinstance(payload["database_size"], bool)
            or payload["database_size"] < 1
        ):
            _fail("manifest_invalid")
        if not _version_text(payload["sqlite_version"]):
            _fail("manifest_invalid")
        observed_digest, observed_size = _hash_file(database)
        if (
            observed_digest != payload["database_sha256"]
            or observed_size != payload["database_size"]
        ):
            _fail("content_mismatch")
        manifest_hash = body_hash
        digest = observed_digest
        size = observed_size
    if head["manifest_sha256"] != manifest_hash:
        _fail("store_inconsistent")
    _require_optional_anchor(generation, minimum_generation)
    return RecoveryPoint(
        generation=generation,
        database_sha256=digest,
        manifest_sha256=manifest_hash,
        database_size=size,
        boundary=SIMULATED_RECOVERY_BOUNDARY,
        source_identity=source_identity,
        store_identity=store_identity,
    )


def _identity_hex(value):
    if type(value) is not bytes or len(value) != _KEY_LENGTH:
        _fail("identity_invalid")
    return value.hex()


def _require_bound_identity(presented, expected):
    if (
        type(presented) is not str
        or len(presented) != len(expected)
        or not hmac.compare_digest(presented, expected)
    ):
        _fail("identity_mismatch")


def _refuse_uncertainty(store, key, entries, source_identity, store_identity):
    present = _UNCERTAIN_NAME in entries or _UNCERTAIN_MAC_NAME in entries
    if not present:
        return
    if _UNCERTAIN_NAME not in entries or _UNCERTAIN_MAC_NAME not in entries:
        _fail("contradictory")
    payload = _read_authenticated_json(
        store / _UNCERTAIN_NAME,
        store / _UNCERTAIN_MAC_NAME,
        key,
        _UNCERTAINTY_FIELDS,
        _UNCERTAINTY_FORMAT,
    )
    if payload.get("reason") != "seal_directory_fsync":
        _fail("manifest_invalid")
    _require_bound_identity(payload["source_identity"], source_identity)
    _require_bound_identity(payload["store_identity"], store_identity)
    _fail("durability_uncertain")


def _require_optional_anchor(current, minimum_generation):
    if minimum_generation is None:
        return
    minimum = _require_generation_number(minimum_generation)
    if current < minimum:
        _fail("rollback")


def _directory_entries(path):
    try:
        return tuple(entry.name for entry in os.scandir(path))
    except OSError:
        _fail("identity")


def _classify_unexpected(names):
    for name in names:
        if name.endswith(SQLITE_SIDECAR_SUFFIXES):
            _fail("sidecar_rejected")
    if names:
        _fail("store_inconsistent")


def _read_authenticated_json(path, mac_path, key, fields, format_name):
    _body, payload = _read_authenticated_bytes(
        path,
        mac_path,
        key,
        fields,
        format_name,
    )
    return payload


def _read_authenticated_bytes(path, mac_path, key, fields, format_name):
    body = _read_limited(path, _MANIFEST_LIMIT)
    mac_text = _read_limited(mac_path, _HASH_LENGTH)
    try:
        mac_text.decode("ascii")
    except UnicodeDecodeError:
        _fail("mac_invalid")
    if len(mac_text) != _HASH_LENGTH or any(
        character not in b"0123456789abcdef" for character in mac_text
    ):
        _fail("mac_invalid")
    try:
        presented = bytes.fromhex(mac_text.decode("ascii"))
    except ValueError:
        _fail("mac_invalid")
    if not hmac.compare_digest(_mac(key, body), presented):
        _fail("mac_invalid")
    try:
        payload = json.loads(body.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("manifest_invalid")
    if type(payload) is not dict or set(payload) != fields:
        _fail("manifest_invalid")
    if _canonical(payload) != body:
        _fail("manifest_invalid")
    if payload.get("format") != format_name:
        _fail("manifest_invalid")
    return body, payload


def _read_limited(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_ISLNK(status.st_mode)
            or status.st_uid != os.geteuid()
            or stat.S_IMODE(status.st_mode) != 0o600
            or status.st_nlink != 1
            or status.st_size > limit
            or status.st_size < 1
        ):
            _fail("manifest_invalid")
        payload = os.read(descriptor, limit + 1)
        if len(payload) != status.st_size:
            _fail("manifest_invalid")
        return payload
    finally:
        os.close(descriptor)


def _require_hash(value):
    if type(value) is not str or len(value) != _HASH_LENGTH:
        _fail("manifest_invalid")
    if any(character not in "0123456789abcdef" for character in value):
        _fail("manifest_invalid")


def _version_text(value):
    if type(value) is not str:
        return False
    parts = value.split(".")
    return len(parts) == 3 and all(part.isdigit() and part == str(int(part)) for part in parts)


def _cleanup_temp(path, status):
    if status is None:
        return
    try:
        _remove_owned_file(path, status)
    except RecoveryHelperError as error:
        if error.code != "recovery_cleanup_uncertain":
            raise
        raise


def _remove_owned_file(path, status):
    try:
        listed = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        _fail("cleanup_uncertain")
    if (
        stat.S_ISLNK(listed.st_mode)
        or not stat.S_ISREG(listed.st_mode)
        or listed.st_dev != status.st_dev
        or listed.st_ino != status.st_ino
        or listed.st_uid != os.geteuid()
    ):
        _fail("cleanup_uncertain")
    try:
        os.unlink(path)
    except OSError:
        _fail("cleanup_uncertain")


def _remove_empty_owned_directory(path):
    try:
        listed = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        _fail("cleanup_uncertain")
    if (
        stat.S_ISLNK(listed.st_mode)
        or not stat.S_ISDIR(listed.st_mode)
        or listed.st_uid != os.geteuid()
    ):
        _fail("cleanup_uncertain")
    try:
        if any(os.scandir(path)):
            return
        os.rmdir(path)
    except OSError:
        _fail("cleanup_uncertain")
