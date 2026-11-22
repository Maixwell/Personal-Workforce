"""Disposable SQLite backup and restore.

The online backup API copies a consistent snapshot, including committed WAL
pages. Publication uses a private temporary file, an exclusive link, and
file and directory fsync. An existing destination is never replaced.

sqlite3.connect addresses the source and temporary files by path. Identity
is rechecked immediately before each connection. A substitution inside that
remaining interval cannot be removed on this platform.

Publication rechecks the destination main file and its WAL, shared-memory,
journal, and initialization-marker siblings immediately before os.link, then
rechecks the published inode and those siblings before success is returned.
The checks and os.link are not one atomic directory operation. A sibling
created by the same account after the last check can still accompany a
successful return. A sibling present at either check prevents success and is
left untouched. os.link still refuses an existing main name.

This module does not create production resources and does not initialize,
migrate, or open the live production database.
"""

import errno
import os
import secrets
import sqlite3
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from max_smart_db.config import (
    PRODUCTION_DATABASE_PATH,
    PROJECT_ROOT,
    SQLITE_BUSY_TIMEOUT_MS,
    SQLITE_BUSY_TIMEOUT_SECONDS,
)
from max_smart_db.safety import (
    SQLITE_SIDECAR_SUFFIXES,
    DatabaseSafetyError,
    _check_ready_initialization_marker,
    validate_test_database_path,
    verify_local_apfs,
    verify_sqlite_connection_version,
    verify_sqlite_module_version,
)
from provider_fence import PRODUCTION_FENCE_PATH, _APPROVED_PIN_DIRECTORY


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


class DisposableBackupError(RuntimeError):
    """A disposable backup or restore failed closed."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DisposableDatabaseCopy:
    """Identity of one published disposable database file."""

    path: str
    device: int
    inode: int
    size: int


def backup_disposable_sqlite(source, destination):
    """Copy one disposable database to a fresh destination.

    The destination file, its sidecars, and its initialization markers must
    not already exist. The published file is a consistent main database.
    """

    return _copy_disposable_database(
        source,
        destination,
        "backup",
        enable_wal=False,
    )


def restore_disposable_sqlite(backup, destination):
    """Restore one disposable backup into a fresh destination.

    The destination is accepted only after its integrity check succeeds.
    """

    return _copy_disposable_database(
        backup,
        destination,
        "restore",
        enable_wal=True,
    )


def _fail(operation, code):
    raise DisposableBackupError(f"{operation}_{code}")


def _map_safety(error, operation):
    filesystem = {
        "cloud_path",
        "filesystem_not_local_apfs",
        "unsupported_filesystem_platform",
        "filesystem_check_failed",
    }
    if error.code in _SQLITE_VERSION_CODES:
        _fail(operation, "sqlite_version")
    if error.code == "database_initialization_incomplete":
        _fail(operation, "marker_invalid")
    if error.code == "path_symlink":
        _fail(operation, "symlink")
    if error.code in filesystem:
        _fail(operation, "filesystem")
    _fail(operation, "path_refused")


def _raise_sqlite(error, operation):
    text = str(error).lower()
    if "locked" in text or "busy" in text:
        _fail(operation, "lock_contention")
    _fail(operation, "interrupted")


def _refused_path(path):
    if path in _REFUSED_FILES:
        return True
    return any(
        path == directory or directory in path.parents
        for directory in _REFUSED_DIRECTORIES
    )


def _candidate(path, operation, *, must_exist):
    if type(path) is not str and not isinstance(path, Path):
        _fail(operation, "path_refused")
    candidate = Path(path)
    if not candidate.is_absolute() or _refused_path(candidate):
        _fail(operation, "path_refused")
    if any(part in _CLOUD_PATH_COMPONENTS for part in candidate.parts):
        _fail(operation, "filesystem")
    try:
        validated = validate_test_database_path(candidate)
        verify_local_apfs(candidate.parent if candidate.parent != candidate else candidate)
    except DatabaseSafetyError as error:
        _map_safety(error, operation)
    if validated != candidate:
        _fail(operation, "symlink")
    try:
        listed = candidate.lstat()
    except FileNotFoundError:
        listed = None
    except OSError as error:
        _fail(operation, "identity")
    if listed is None:
        if must_exist:
            _fail(operation, "source_missing")
        return candidate, None
    if stat.S_ISLNK(listed.st_mode):
        _fail(operation, "symlink")
    if not stat.S_ISREG(listed.st_mode):
        _fail(operation, "source_invalid")
    return candidate, listed


def _require_private_directory(directory, operation):
    try:
        listed = directory.lstat()
    except OSError as error:
        _fail(operation, "path_refused")
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISDIR(listed.st_mode):
        _fail(operation, "symlink" if stat.S_ISLNK(listed.st_mode) else "path_refused")
    if listed.st_uid != os.geteuid():
        _fail(operation, "owner")
    if stat.S_IMODE(listed.st_mode) != 0o700:
        _fail(operation, "permissions")
    return listed


def _require_private_file(status, operation):
    if status.st_uid != os.geteuid():
        _fail(operation, "owner")
    if stat.S_IMODE(status.st_mode) != 0o600:
        _fail(operation, "permissions")
    if status.st_nlink != 1:
        _fail(operation, "hard_link")
    if status.st_size <= 0:
        _fail(operation, "source_invalid")


def _artifact_paths(path):
    paths = [path]
    text = str(path)
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        paths.append(Path(f"{text}{suffix}"))
    paths.append(Path(f"{text}.initialization-state"))
    paths.append(Path(f"{text}.initialization-state.tmp"))
    return tuple(paths)


def _status_or_none(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise OSError(error.errno, "identity unavailable") from error


def _require_source(path, operation):
    candidate, listed = _candidate(path, operation, must_exist=True)
    _require_private_directory(candidate.parent, operation)
    _require_private_file(listed, operation)
    for artifact in _artifact_paths(candidate)[1:]:
        status = _status_or_none(artifact)
        if status is None:
            continue
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            _fail(operation, "symlink" if stat.S_ISLNK(status.st_mode) else "source_invalid")
        if status.st_uid != os.geteuid() or stat.S_IMODE(status.st_mode) != 0o600:
            _fail(operation, "owner" if status.st_uid != os.geteuid() else "permissions")
        if status.st_nlink != 1:
            _fail(operation, "hard_link")
    if operation == "backup":
        _require_ready_marker(candidate, operation)
    return candidate, listed


def _require_ready_marker(path, operation):
    """Require the accepted READY marker without repairing it."""

    try:
        _check_ready_initialization_marker(path)
    except DatabaseSafetyError as error:
        _map_safety(error, operation)


def _require_fresh_destination(path, operation):
    candidate, listed = _candidate(path, operation, must_exist=False)
    parent = _require_private_directory(candidate.parent, operation)
    _require_destination_unpublished(candidate, operation, listed)
    return candidate, parent


def _require_destination_unpublished(destination, operation, listed=None):
    """Refuse a destination whose main file or sibling artifacts exist.

    This is a path check, not an atomic reservation. Publication calls it
    again immediately before ``os.link`` and checks siblings again after the
    link, before success is returned.
    """

    if listed is None:
        listed = _status_or_none(destination)
    if listed is not None:
        _fail(operation, "exists")
    for artifact in _artifact_paths(destination)[1:]:
        if _status_or_none(artifact) is not None:
            _fail(operation, "artifact_exists")


def _require_published_siblings_clear(destination, operation):
    """Refuse success when a sibling appeared beside the published file.

    Unexpected siblings are left untouched.
    """

    for artifact in _artifact_paths(destination)[1:]:
        if _status_or_none(artifact) is not None:
            _fail(operation, "artifact_exists")


def _same_identity(left, right):
    """Compare the stable file identity, not its changing size or link count."""

    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_uid == right.st_uid
        and stat.S_IMODE(left.st_mode) == stat.S_IMODE(right.st_mode)
        and stat.S_ISREG(left.st_mode)
        and stat.S_ISREG(right.st_mode)
        and not stat.S_ISLNK(left.st_mode)
        and not stat.S_ISLNK(right.st_mode)
    )


def _recheck(path, expected, operation):
    try:
        current = path.lstat()
    except OSError as error:
        _fail(operation, "identity")
    if not _same_identity(current, expected):
        _fail(operation, "identity")
    return current


def _open_verified(path, expected, operation):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        _fail(operation, "symlink" if error.errno == errno.ELOOP else "identity")
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != expected.st_dev
            or current.st_ino != expected.st_ino
            or current.st_uid != expected.st_uid
            or stat.S_IMODE(current.st_mode) != stat.S_IMODE(expected.st_mode)
        ):
            _fail(operation, "identity")
    finally:
        os.close(descriptor)
    return _recheck(path, expected, operation)


def _connect(path, operation, *, readonly):
    query = "mode=ro" if readonly else "mode=rw"
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?{query}",
            uri=True,
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
        )
    except sqlite3.OperationalError as error:
        _raise_sqlite(error, operation)
    except sqlite3.DatabaseError as error:
        _fail(operation, "integrity")
    try:
        connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        if readonly:
            connection.execute("PRAGMA query_only=ON")
    except sqlite3.OperationalError as error:
        connection.close()
        _raise_sqlite(error, operation)
    except sqlite3.DatabaseError as error:
        connection.close()
        _fail(operation, "integrity")
    return connection


def _snapshot_database(source_connection, target_connection):
    """Copy one consistent snapshot. Tests use this boundary to plant races."""

    source_connection.backup(target_connection, pages=1, sleep=0.0)


def _require_integrity(connection, operation):
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        quick = connection.execute("PRAGMA quick_check").fetchall()
    except sqlite3.OperationalError as error:
        _raise_sqlite(error, operation)
    except sqlite3.DatabaseError as error:
        _fail(operation, "integrity")
    if integrity != [("ok",)] or quick != [("ok",)]:
        _fail(operation, "integrity")


def _create_temp(parent, label, operation):
    name = f".{label}-{secrets.token_hex(16)}.sqlite3"
    path = parent / name
    if _refused_path(path):
        _fail(operation, "path_refused")
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    old_umask = os.umask(0o077)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        _fail(operation, "exists")
    except OSError as error:
        _fail(operation, "symlink" if error.errno == errno.ELOOP else "interrupted")
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
            _fail(operation, "identity")
    finally:
        os.close(descriptor)
    listed = _recheck(path, path.lstat(), operation)
    return path, listed


def _remove_owned_file(path, device, inode, operation):
    try:
        listed = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        _fail(operation, "cleanup_uncertain")
    if (
        stat.S_ISLNK(listed.st_mode)
        or not stat.S_ISREG(listed.st_mode)
        or listed.st_dev != device
        or listed.st_ino != inode
        or listed.st_uid != os.geteuid()
    ):
        _fail(operation, "cleanup_uncertain")
    try:
        os.unlink(path)
    except OSError as error:
        _fail(operation, "cleanup_uncertain")


def _fsync_file(path, expected, operation):
    current = _open_verified(path, expected, operation)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        observed = os.fstat(descriptor)
        if observed.st_dev != current.st_dev or observed.st_ino != current.st_ino:
            _fail(operation, "identity")
        os.fsync(descriptor)
    except OSError as error:
        _fail(operation, "interrupted")
    finally:
        os.close(descriptor)
    return current


def _fsync_directory(directory, expected, operation, *, uncertain):
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | os.O_NOFOLLOW
        | os.O_CLOEXEC
    )
    try:
        descriptor = os.open(directory, flags)
    except OSError as error:
        _fail(operation, "durability_uncertain" if uncertain else "identity")
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(current.st_mode)
            or current.st_dev != expected.st_dev
            or current.st_ino != expected.st_ino
            or current.st_uid != os.geteuid()
            or stat.S_IMODE(current.st_mode) != 0o700
        ):
            _fail(operation, "durability_uncertain" if uncertain else "identity")
        os.fsync(descriptor)
    except DisposableBackupError:
        raise
    except OSError as error:
        _fail(operation, "durability_uncertain" if uncertain else "interrupted")
    finally:
        os.close(descriptor)


def _prepare_restore(connection, operation):
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA legacy_alter_table=OFF")
        journal = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA recursive_triggers=ON")
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.OperationalError as error:
        _raise_sqlite(error, operation)
    except sqlite3.DatabaseError as error:
        _fail(operation, "integrity")
    if (
        not isinstance(journal, tuple)
        or str(journal[0]).lower() != "wal"
        or not isinstance(checkpoint, tuple)
        or len(checkpoint) != 3
        or checkpoint[0] != 0
    ):
        _fail(operation, "interrupted")
    _require_integrity(connection, operation)


def _clear_unpublished_sidecars(path, operation):
    text = str(path)
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        sidecar = Path(f"{text}{suffix}")
        status = _status_or_none(sidecar)
        if status is None:
            continue
        if (
            stat.S_ISLNK(status.st_mode)
            or not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.geteuid()
            or status.st_nlink != 1
        ):
            _fail(operation, "cleanup_uncertain")
        if status.st_size != 0:
            _fail(operation, "interrupted")
        _remove_owned_file(sidecar, status.st_dev, status.st_ino, operation)


def _copy_disposable_database(source, destination, operation, *, enable_wal):
    if (
        isinstance(source, (str, Path))
        and isinstance(destination, (str, Path))
        and Path(source).is_absolute()
        and Path(source) == Path(destination)
    ):
        _fail(operation, "same_path")
    source_path, source_status = _require_source(source, operation)
    destination_path, parent_status = _require_fresh_destination(
        destination,
        operation,
    )
    try:
        module_version = verify_sqlite_module_version()
    except DatabaseSafetyError as error:
        _map_safety(error, operation)
    source_status = _open_verified(source_path, source_status, operation)
    temporary_path = None
    temporary_status = None
    published = False
    source_connection = None
    target_connection = None
    try:
        temporary_path, temporary_status = _create_temp(
            destination_path.parent,
            operation,
            operation,
        )
        source_status = _recheck(source_path, source_status, operation)
        if operation == "backup":
            _require_ready_marker(source_path, operation)
        source_connection = _connect(source_path, operation, readonly=True)
        try:
            verify_sqlite_connection_version(source_connection, module_version)
            _require_integrity(source_connection, operation)
        except DisposableBackupError:
            raise
        target_status = _recheck(temporary_path, temporary_status, operation)
        target_connection = _connect(temporary_path, operation, readonly=False)
        try:
            verify_sqlite_connection_version(target_connection, module_version)
            _snapshot_database(source_connection, target_connection)
            if enable_wal:
                _prepare_restore(target_connection, operation)
            _require_integrity(target_connection, operation)
        except sqlite3.OperationalError as error:
            _raise_sqlite(error, operation)
        except sqlite3.DatabaseError as error:
            _fail(operation, "integrity")
        finally:
            target_connection.close()
            target_connection = None
        source_connection.close()
        source_connection = None
        if enable_wal:
            _clear_unpublished_sidecars(temporary_path, operation)
        elif any(
            _status_or_none(Path(f"{temporary_path}{suffix}")) is not None
            for suffix in SQLITE_SIDECAR_SUFFIXES
        ):
            _fail(operation, "interrupted")
        temporary_status = _fsync_file(temporary_path, target_status, operation)
        if os.chmod not in os.supports_follow_symlinks:
            _fail(operation, "permissions")
        os.chmod(temporary_path, 0o600, follow_symlinks=False)
        temporary_status = _recheck(temporary_path, temporary_status, operation)
        if stat.S_IMODE(temporary_status.st_mode) != 0o600:
            _fail(operation, "permissions")
        _require_destination_unpublished(destination_path, operation)
        try:
            os.link(temporary_path, destination_path)
        except FileExistsError as error:
            _fail(operation, "exists")
        except OSError as error:
            _fail(operation, "interrupted")
        published = True
        published_status = _recheck(destination_path, temporary_status, operation)
        _require_published_siblings_clear(destination_path, operation)
        _fsync_directory(
            destination_path.parent,
            parent_status,
            operation,
            uncertain=True,
        )
        _remove_owned_file(
            temporary_path,
            temporary_status.st_dev,
            temporary_status.st_ino,
            operation,
        )
        temporary_path = None
        _fsync_directory(
            destination_path.parent,
            parent_status,
            operation,
            uncertain=True,
        )
        final = _recheck(destination_path, published_status, operation)
        if final.st_nlink != 1:
            _fail(operation, "cleanup_uncertain")
        _require_published_siblings_clear(destination_path, operation)
        return DisposableDatabaseCopy(
            str(destination_path),
            final.st_dev,
            final.st_ino,
            final.st_size,
        )
    except DisposableBackupError:
        raise
    except sqlite3.OperationalError as error:
        _raise_sqlite(error, operation)
    except sqlite3.DatabaseError as error:
        _fail(operation, "integrity")
    except DatabaseSafetyError as error:
        _map_safety(error, operation)
    except BaseException as error:
        failure = DisposableBackupError(f"{operation}_interrupted")
        failure.__cause__ = error
        raise failure
    finally:
        for connection in (target_connection, source_connection):
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
        if temporary_path is not None and temporary_status is not None:
            active = sys.exc_info()[1]
            try:
                _clear_failed_sidecars(temporary_path, operation)
                _remove_owned_file(
                    temporary_path,
                    temporary_status.st_dev,
                    temporary_status.st_ino,
                    operation,
                )
            except DisposableBackupError as cleanup:
                if active is not None:
                    cleanup.__cause__ = active
                raise cleanup


def _clear_failed_sidecars(path, operation):
    text = str(path)
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        sidecar = Path(f"{text}{suffix}")
        status = _status_or_none(sidecar)
        if status is None:
            continue
        _remove_owned_file(sidecar, status.st_dev, status.st_ino, operation)
