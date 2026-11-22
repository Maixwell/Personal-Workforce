"""Fail-closed filesystem and Git safeguards for the SQLite database."""

from __future__ import annotations

import contextvars
import os
import plistlib
import re
import sqlite3
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import (
    PRODUCTION_DATABASE_PATH,
    PROJECT_ROOT,
    SQLITE_BUSY_TIMEOUT_MS,
    SQLITE_BUSY_TIMEOUT_SECONDS,
)


_PROTECTED_GIT_PATHS = (
    "data/max_smart.sqlite3",
    "data/max_smart.sqlite3-wal",
    "data/max_smart.sqlite3-shm",
    "data/max_smart.sqlite3.initialization-state",
    "data/max_smart.sqlite3.initialization-state.tmp",
    "data/backups/preflight.sqlite3",
    "data/.backup-preflight",
    "data/.restore-preflight",
    "data/.migration-rehearsal-preflight",
)
_PROTECTED_TRACKED_PATTERNS = (
    "data/*.sqlite3*",
    "data/*-wal",
    "data/*-shm",
    "data/backups",
    "data/.backup-*",
    "data/.restore-*",
    "data/.migration-rehearsal-*",
)
_CLOUD_PATH_COMPONENTS = {
    "Dropbox",
    "Google Drive",
    "iCloud Drive",
    "Mobile Documents",
    "OneDrive",
}
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
SQLITE_INITIALIZATION_STATE_SUFFIX = ".initialization-state"
SQLITE_INITIALIZATION_TEMP_SUFFIX = ".initialization-state.tmp"
SQLITE_INITIALIZATION_INCOMPLETE = (
    b"max-smart/sqlite-init/v1 INCOMPLETE\n"
)
SQLITE_INITIALIZATION_READY = b"max-smart/sqlite-init/v1 READY\n"
_LOCAL_DISK_DEVICE = re.compile(r"disk[0-9]+(s[0-9]+)+")
MINIMUM_SQLITE_VERSION = (3, 33, 0)
_INITIALIZATION_CAPABILITY_TOKEN = object()
_ACTIVE_INITIALIZATION_CAPABILITY = contextvars.ContextVar(
    "max_smart_active_initialization_capability",
    default=None,
)


@dataclass(frozen=True)
class _InitializationCapability:
    """One validated, descriptor-bound initialization target."""

    database_path: Path
    parent_descriptor: int
    parent_device: int
    parent_inode: int
    policy: str
    token: object


class DatabaseSafetyError(RuntimeError):
    """A redacted, actionable database-foundation failure."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def verify_sqlite_module_version():
    """Return the approved three-part SQLite module version or fail closed."""
    version = sqlite3.sqlite_version_info
    if (
        not isinstance(version, tuple)
        or len(version) != 3
        or any(type(component) is not int for component in version)
    ):
        raise DatabaseSafetyError(
            "sqlite_module_version_invalid",
            "The SQLite module version is invalid.",
        )
    if version < MINIMUM_SQLITE_VERSION:
        raise DatabaseSafetyError(
            "sqlite_version_unsupported",
            "SQLite 3.33.0 or newer is required.",
        )
    return version


def verify_sqlite_connection_version(connection, module_version):
    """Verify the first connection query reports the approved module version."""
    row = connection.execute("SELECT sqlite_version()").fetchone()
    if not isinstance(row, tuple) or len(row) != 1 or not isinstance(row[0], str):
        raise DatabaseSafetyError(
            "sqlite_connection_version_invalid",
            "The SQLite connection version is invalid.",
        )
    components = row[0].split(".")
    if (
        len(components) != 3
        or any(
            not component
            or any(character not in "0123456789" for character in component)
            for component in components
        )
    ):
        raise DatabaseSafetyError(
            "sqlite_connection_version_invalid",
            "The SQLite connection version is invalid.",
        )
    connection_version = tuple(int(component) for component in components)
    if connection_version < MINIMUM_SQLITE_VERSION:
        raise DatabaseSafetyError(
            "sqlite_version_unsupported",
            "SQLite 3.33.0 or newer is required.",
        )
    if connection_version != module_version:
        raise DatabaseSafetyError(
            "sqlite_version_mismatch",
            "The SQLite module and connection versions do not match.",
        )
    return connection_version


def close_owned_resource(resource, primary_exception):
    """Close one owned resource without replacing an existing failure."""
    try:
        resource.close()
    except BaseException as cleanup_error:
        if primary_exception.__cause__ is None:
            primary_exception.__cause__ = cleanup_error
        else:
            primary_exception.add_note(
                "Owned-resource cleanup also failed "
                f"({type(cleanup_error).__name__}; details redacted)."
            )


def verify_required_sqlite_pragmas(connection):
    """Read back all approved SQLite settings in their required order."""
    expected_pragmas = (
        ("foreign_keys", 1),
        ("journal_mode", "wal"),
        ("synchronous", 2),
        ("busy_timeout", SQLITE_BUSY_TIMEOUT_MS),
        ("recursive_triggers", 1),
    )
    for pragma, expected in expected_pragmas:
        row = connection.execute(f"PRAGMA {pragma}").fetchone()
        if not isinstance(row, tuple) or len(row) != 1:
            raise DatabaseSafetyError(
                f"{pragma}_invalid",
                "A required SQLite connection safeguard is not active.",
            )
        actual = row[0]
        if pragma == "journal_mode":
            actual = str(actual).lower()
        if actual != expected:
            raise DatabaseSafetyError(
                f"{pragma}_invalid",
                "A required SQLite connection safeguard is not active.",
            )


def _git_environment():
    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("GIT_")
    }


def _run_git(arguments, project_root):
    try:
        return subprocess.run(
            ("git", *arguments),
            cwd=project_root,
            env=_git_environment(),
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DatabaseSafetyError(
            "git_unavailable",
            "Database Git protection could not be verified.",
        ) from error


def verify_git_protection(project_root=PROJECT_ROOT):
    """Require all database artifacts to be ignored and none to be tracked."""
    project_root = Path(project_root)
    unignored_path_found = False
    for relative_path in _PROTECTED_GIT_PATHS:
        result = _run_git(
            ("check-ignore", "--quiet", "--", relative_path),
            project_root,
        )
        if result.returncode == 1:
            unignored_path_found = True
        elif result.returncode != 0:
            raise DatabaseSafetyError(
                "git_ignore_check_failed",
                "Database Git protection could not be verified.",
            )

    tracked = _run_git(
        ("ls-files", "-z", "--", *_PROTECTED_TRACKED_PATTERNS),
        project_root,
    )
    if tracked.returncode != 0:
        raise DatabaseSafetyError(
            "git_tracked_check_failed",
            "Tracked database artifacts could not be checked.",
        )
    if tracked.stdout:
        raise DatabaseSafetyError(
            "git_path_tracked",
            "A database artifact is already tracked by Git.",
        )
    if unignored_path_found:
        raise DatabaseSafetyError(
            "git_path_not_ignored",
            "A database artifact path is not safely ignored by Git.",
        )


def _assert_no_symlink(path, boundary):
    path = Path(path)
    boundary = Path(boundary)
    try:
        relative = path.relative_to(boundary)
    except ValueError as error:
        raise DatabaseSafetyError(
            "path_outside_boundary",
            "The database path is outside its approved boundary.",
        ) from error

    paths = [boundary]
    current = boundary
    for part in relative.parts:
        current /= part
        paths.append(current)
    for current in paths:
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise DatabaseSafetyError(
                "path_symlink",
                "The database path must not contain symbolic links.",
            )


def _lstat_or_none(path):
    try:
        return Path(path).lstat()
    except FileNotFoundError:
        return None


def _filesystem_identity(path, *, required):
    """Return a filesystem object identity, failing closed on unsafe errors."""
    try:
        status = Path(path).stat()
    except FileNotFoundError as error:
        if not required:
            return None
        raise DatabaseSafetyError(
            "filesystem_identity_unavailable",
            "Filesystem identity could not be established safely.",
        ) from error
    except OSError as error:
        raise DatabaseSafetyError(
            "filesystem_identity_unavailable",
            "Filesystem identity could not be established safely.",
        ) from error
    return status.st_dev, status.st_ino


def _assert_not_within_project_identity(path, project_identity):
    """Reject every existing ancestor that is the project root by identity."""
    current = Path(path)
    while True:
        if _filesystem_identity(current, required=False) == project_identity:
            raise DatabaseSafetyError(
                "test_path_in_repository",
                "Test databases must be outside the project repository.",
            )
        if current == current.parent:
            break
        current = current.parent


def _check_private_directory(directory):
    status = _lstat_or_none(directory)
    if (
        status is None
        or stat.S_ISLNK(status.st_mode)
        or not stat.S_ISDIR(status.st_mode)
    ):
        raise DatabaseSafetyError(
            "data_directory_invalid",
            "The database directory must be a real directory.",
        )
    if stat.S_IMODE(status.st_mode) != 0o700:
        raise DatabaseSafetyError(
            "data_directory_permissions",
            "The database directory permissions must be 0700.",
        )
    if status.st_uid != os.geteuid():
        raise DatabaseSafetyError(
            "data_directory_owner",
            "The database directory must be owned by the current user.",
        )


def _check_private_file(path, *, required):
    status = _lstat_or_none(path)
    if status is None:
        if required:
            raise DatabaseSafetyError(
                "database_missing",
                "The database does not exist; it is never created implicitly.",
            )
        return
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        raise DatabaseSafetyError(
            "database_artifact_invalid",
            "SQLite artifacts must be regular files, not symbolic links.",
        )
    if stat.S_IMODE(status.st_mode) != 0o600:
        raise DatabaseSafetyError(
            "database_permissions",
            "SQLite artifact permissions must be 0600.",
        )
    if status.st_uid != os.geteuid():
        raise DatabaseSafetyError(
            "database_owner",
            "SQLite artifacts must be owned by the current user.",
        )
    if status.st_nlink != 1:
        raise DatabaseSafetyError(
            "database_hard_link",
            "SQLite artifacts must not have additional hard links.",
        )


def _check_sqlite_artifacts(database_path, *, require_database):
    database_path = Path(database_path)
    _check_private_file(database_path, required=require_database)
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        _check_private_file(Path(f"{database_path}{suffix}"), required=False)
    if _lstat_or_none(database_path) is not None:
        _check_ready_initialization_marker(database_path)
    else:
        for path in _initialization_marker_paths(database_path):
            if _lstat_or_none(path) is not None:
                raise _initialization_marker_error()


def _initialization_state_path(database_path):
    return Path(f"{database_path}{SQLITE_INITIALIZATION_STATE_SUFFIX}")


def _initialization_temp_path(database_path):
    return Path(f"{database_path}{SQLITE_INITIALIZATION_TEMP_SUFFIX}")


def _initialization_marker_paths(database_path):
    return (
        _initialization_state_path(database_path),
        _initialization_temp_path(database_path),
    )


def _initialization_marker_error():
    return DatabaseSafetyError(
        "database_initialization_incomplete",
        "The database initialization state is incomplete.",
    )


def _read_private_marker(path):
    path = Path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise _initialization_marker_error() from error
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_IMODE(status.st_mode) != 0o600
            or status.st_uid != os.geteuid()
            or status.st_nlink != 1
        ):
            raise _initialization_marker_error()
        maximum = max(
            len(SQLITE_INITIALIZATION_INCOMPLETE),
            len(SQLITE_INITIALIZATION_READY),
        )
        contents = os.read(descriptor, maximum + 1)
        if os.read(descriptor, 1):
            raise _initialization_marker_error()
        return contents
    except OSError as error:
        raise _initialization_marker_error() from error
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            raise _initialization_marker_error() from error


def _check_marker_exact(path, expected):
    if _read_private_marker(path) != expected:
        raise _initialization_marker_error()


def _check_ready_initialization_marker(database_path):
    state_path = _initialization_state_path(database_path)
    temporary_path = _initialization_temp_path(database_path)
    if _lstat_or_none(temporary_path) is not None:
        raise _initialization_marker_error()
    _check_marker_exact(state_path, SQLITE_INITIALIZATION_READY)


def validate_production_database_path(
    database_path=PRODUCTION_DATABASE_PATH,
    project_root=PROJECT_ROOT,
):
    """Require the fixed production path and private existing artifacts."""
    database_path = Path(database_path)
    project_root = Path(project_root)
    expected = project_root / "data" / "max_smart.sqlite3"
    if database_path != expected:
        raise DatabaseSafetyError(
            "production_path_mismatch",
            "The production database path is not the approved fixed path.",
        )
    _assert_no_symlink(database_path, project_root)
    if _lstat_or_none(database_path.parent) is not None:
        _check_private_directory(database_path.parent)
    _check_sqlite_artifacts(database_path, require_database=False)


def validate_test_database_path(database_path, project_root=PROJECT_ROOT):
    """Require an absolute test path outside every alias of the repository."""
    database_path = Path(database_path)
    if not database_path.is_absolute():
        raise DatabaseSafetyError(
            "test_path_not_absolute",
            "The test database path must be absolute.",
        )
    try:
        project_root = Path(project_root).resolve(strict=True)
        resolved = database_path.resolve(strict=False)
    except OSError as error:
        raise DatabaseSafetyError(
            "filesystem_identity_unavailable",
            "Filesystem identity could not be established safely.",
        ) from error
    if resolved == project_root or resolved.is_relative_to(project_root):
        raise DatabaseSafetyError(
            "test_path_in_repository",
            "Test databases must be outside the project repository.",
        )

    project_identity = _filesystem_identity(project_root, required=True)
    _assert_not_within_project_identity(database_path, project_identity)
    _assert_not_within_project_identity(resolved, project_identity)

    database_identity = _filesystem_identity(database_path, required=False)
    production_identity = _filesystem_identity(
        PRODUCTION_DATABASE_PATH,
        required=False,
    )
    if (
        database_identity is not None
        and production_identity is not None
        and database_identity == production_identity
    ):
        raise DatabaseSafetyError(
            "test_path_is_production",
            "The test database must not be the production database.",
        )

    _assert_no_symlink(resolved, resolved.anchor)
    return resolved


def verify_production_database_before_connect(
    database_path=PRODUCTION_DATABASE_PATH,
    project_root=PROJECT_ROOT,
):
    """Check an existing production database before SQLite opens it."""
    validate_production_database_path(database_path, project_root)
    _check_private_directory(Path(database_path).parent)
    _check_sqlite_artifacts(database_path, require_database=True)


def verify_test_database_before_connect(database_path):
    """Check an existing out-of-repository test database before opening it."""
    database_path = Path(database_path)
    resolved = validate_test_database_path(database_path)
    if resolved != database_path:
        raise DatabaseSafetyError(
            "path_symlink",
            "The database path must not contain symbolic links.",
        )
    _check_private_directory(resolved.parent)
    _check_sqlite_artifacts(resolved, require_database=True)


def _filesystem_check_failed(error=None):
    failure = DatabaseSafetyError(
        "filesystem_check_failed",
        "The production database filesystem could not be verified.",
    )
    if error is not None:
        failure.__cause__ = error
    return failure


def verify_local_apfs(path=PROJECT_ROOT):
    """Require a local APFS disk volume on macOS without exposing details."""
    if any(component in _CLOUD_PATH_COMPONENTS for component in Path(path).parts):
        raise DatabaseSafetyError(
            "cloud_path",
            "The database must not use a cloud-synchronized path.",
        )
    if os.uname().sysname != "Darwin":
        raise DatabaseSafetyError(
            "unsupported_filesystem_platform",
            "Production database filesystem verification requires macOS.",
        )
    try:
        device = subprocess.run(
            ("/usr/bin/stat", "-f", "%Sd", str(path)),
            capture_output=True,
            check=False,
            timeout=5,
        )
        if device.returncode != 0:
            raise _filesystem_check_failed()
        device_name = device.stdout.decode("utf-8", errors="strict").strip()
        if not _LOCAL_DISK_DEVICE.fullmatch(device_name):
            raise DatabaseSafetyError(
                "filesystem_not_local_apfs",
                "The production database requires a local APFS filesystem.",
            )
        result = subprocess.run(
            ("/usr/sbin/diskutil", "info", "-plist", device_name),
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as error:
        raise _filesystem_check_failed(error) from error
    if result.returncode != 0:
        raise _filesystem_check_failed()
    try:
        details = plistlib.loads(result.stdout)
    except (plistlib.InvalidFileException, ValueError) as error:
        raise _filesystem_check_failed(error) from error
    if not isinstance(details, dict):
        raise _filesystem_check_failed()
    filesystem_type = details.get("FilesystemType")
    device_node = details.get("DeviceNode")
    if not isinstance(filesystem_type, str) or not isinstance(device_node, str):
        raise _filesystem_check_failed()
    if filesystem_type.lower() != "apfs" or device_node != f"/dev/{device_name}":
        raise DatabaseSafetyError(
            "filesystem_not_local_apfs",
            "The production database requires a local APFS filesystem.",
        )


def _chmod_no_follow(path, mode):
    if os.chmod not in os.supports_follow_symlinks:
        raise DatabaseSafetyError(
            "chmod_nofollow_unsupported",
            "Permissions cannot be set safely on this platform.",
        )
    try:
        os.chmod(path, mode, follow_symlinks=False)
    except NotImplementedError as error:
        raise DatabaseSafetyError(
            "chmod_nofollow_unsupported",
            "Permissions cannot be set safely on this platform.",
        ) from error


def prepare_production_database_directory(
    database_path=PRODUCTION_DATABASE_PATH,
    project_root=PROJECT_ROOT,
):
    """Create or harden only the approved directory; never create the DB."""
    validate_production_database_path(database_path, project_root)
    verify_local_apfs(project_root)
    verify_git_protection(project_root)

    data_directory = Path(database_path).parent
    old_umask = os.umask(0o077)
    try:
        data_directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    finally:
        os.umask(old_umask)
    status = data_directory.lstat()
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise DatabaseSafetyError(
            "data_directory_invalid",
            "The database directory must be a real directory.",
        )
    _chmod_no_follow(data_directory, 0o700)
    _check_private_directory(data_directory)
    verify_local_apfs(data_directory)


def verify_production_database_environment(
    database_path=PRODUCTION_DATABASE_PATH,
    project_root=PROJECT_ROOT,
):
    """Run read-only production path, filesystem, permission, and Git checks."""
    validate_production_database_path(database_path, project_root)
    data_directory = Path(database_path).parent
    if _lstat_or_none(data_directory) is None:
        verify_local_apfs(project_root)
    else:
        verify_local_apfs(data_directory)
    verify_git_protection(project_root)


def secure_sqlite_artifacts(database_path):
    """Set private modes on freshly initialized files, never following links."""
    database_path = Path(database_path)
    for path in (
        database_path,
        *(Path(f"{database_path}{suffix}") for suffix in SQLITE_SIDECAR_SUFFIXES),
    ):
        status = _lstat_or_none(path)
        if status is None:
            continue
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise DatabaseSafetyError(
                "database_artifact_invalid",
                "SQLite artifacts must be regular files, not symbolic links.",
            )
        _chmod_no_follow(path, 0o600)
    _check_private_file(database_path, required=True)
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        _check_private_file(Path(f"{database_path}{suffix}"), required=False)


def _refuse_existing_initialization_artifacts(database_path):
    database_path = Path(database_path)
    for path in (
        database_path,
        *(Path(f"{database_path}{suffix}") for suffix in SQLITE_SIDECAR_SUFFIXES),
        *_initialization_marker_paths(database_path),
    ):
        if _lstat_or_none(path) is not None:
            raise DatabaseSafetyError(
                "database_initialization_artifact_exists",
                "Database initialization requires unused artifact paths.",
            )


def _create_sqlite_file_exclusively(database_path):
    try:
        descriptor = os.open(
            database_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as error:
        raise DatabaseSafetyError(
            "database_initialization_artifact_exists",
            "Database initialization requires unused artifact paths.",
        ) from error
    except OSError as error:
        raise DatabaseSafetyError(
            "database_initialization_failed",
            "The database file could not be initialized safely.",
        ) from error
    try:
        os.close(descriptor)
    except OSError as error:
        raise DatabaseSafetyError(
            "database_initialization_failed",
            "The database file could not be initialized safely.",
        ) from error


def _write_all(descriptor, contents):
    view = memoryview(contents)
    while view:
        try:
            written = os.write(descriptor, view)
        except OSError as error:
            raise DatabaseSafetyError(
                "database_initialization_failed",
                "The database file could not be initialized safely.",
            ) from error
        if written <= 0:
            raise DatabaseSafetyError(
                "database_initialization_failed",
                "The database file could not be initialized safely.",
            )
        view = view[written:]


def _create_marker_exclusively(path, contents):
    """Write and sync a new marker, then return its descriptor identity."""

    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as error:
        raise DatabaseSafetyError(
            "database_initialization_artifact_exists",
            "Database initialization requires unused artifact paths.",
        ) from error
    except OSError as error:
        raise DatabaseSafetyError(
            "database_initialization_failed",
            "The database file could not be initialized safely.",
        ) from error
    primary_exception = None
    try:
        _write_all(descriptor, contents)
        os.fsync(descriptor)
        return os.fstat(descriptor)
    except BaseException as error:
        primary_exception = error
        raise
    finally:
        try:
            os.close(descriptor)
        except OSError as cleanup_error:
            if primary_exception is None:
                raise DatabaseSafetyError(
                    "database_initialization_failed",
                    "The database file could not be initialized safely.",
                ) from cleanup_error
            if primary_exception.__cause__ is None:
                primary_exception.__cause__ = cleanup_error
            else:
                primary_exception.add_note(
                    "Owned-resource cleanup also failed "
                    f"({type(cleanup_error).__name__}; details redacted)."
                )


def _directory_identity_matches(status, expected_device, expected_inode):
    return (
        stat.S_ISDIR(status.st_mode)
        and not stat.S_ISLNK(status.st_mode)
        and stat.S_IMODE(status.st_mode) == 0o700
        and status.st_uid == os.geteuid()
        and (status.st_dev, status.st_ino) == (expected_device, expected_inode)
    )


def _fsync_retained_parent(capability, directory):
    """Fsync the retained parent, then prove the path still names it."""

    if Path(directory) != capability.database_path.parent:
        raise DatabaseSafetyError(
            "initialization_identity_changed",
            "The initialization target identity changed.",
        )
    try:
        held = os.fstat(capability.parent_descriptor)
    except OSError as error:
        raise DatabaseSafetyError(
            "initialization_identity_changed",
            "The initialization target identity changed.",
        ) from error
    if not _directory_identity_matches(
        held,
        capability.parent_device,
        capability.parent_inode,
    ):
        raise DatabaseSafetyError(
            "initialization_identity_changed",
            "The initialization target identity changed.",
        )
    try:
        os.fsync(capability.parent_descriptor)
    except OSError as error:
        raise DatabaseSafetyError(
            "database_initialization_failed",
            "The database directory could not be synchronized safely.",
        ) from error
    try:
        named = Path(directory).lstat()
    except OSError as error:
        raise DatabaseSafetyError(
            "initialization_identity_changed",
            "The initialization target identity changed.",
        ) from error
    if not _directory_identity_matches(named, held.st_dev, held.st_ino):
        raise DatabaseSafetyError(
            "initialization_identity_changed",
            "The initialization target identity changed.",
        )


def _fsync_directory(directory):
    capability = _ACTIVE_INITIALIZATION_CAPABILITY.get()
    if capability is not None:
        _fsync_retained_parent(capability, directory)
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(directory, flags)
    except OSError as error:
        raise DatabaseSafetyError(
            "database_initialization_failed",
            "The database directory could not be synchronized safely.",
        ) from error
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise DatabaseSafetyError(
            "database_initialization_failed",
            "The database directory could not be synchronized safely.",
        ) from error
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            raise DatabaseSafetyError(
                "database_initialization_failed",
                "The database directory could not be synchronized safely.",
            ) from error


def _check_initialization_candidate(database_path, *, production):
    database_path = Path(database_path)
    if production:
        expected = PROJECT_ROOT / "data" / "max_smart.sqlite3"
        if database_path != expected:
            raise DatabaseSafetyError(
                "production_path_mismatch",
                "The production database path is not the approved fixed path.",
            )
        _assert_no_symlink(database_path, PROJECT_ROOT)
    else:
        resolved = validate_test_database_path(database_path)
        if resolved != database_path:
            raise DatabaseSafetyError(
                "path_symlink",
                "The database path must not contain symbolic links.",
            )
    _check_initialized_artifacts(database_path)


def _check_initialized_artifacts(database_path):
    """Verify initialized artifacts without selecting their path policy."""

    database_path = Path(database_path)
    _check_private_directory(database_path.parent)
    _check_private_file(database_path, required=True)
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        _check_private_file(Path(f"{database_path}{suffix}"), required=False)
    _check_marker_exact(
        _initialization_state_path(database_path),
        SQLITE_INITIALIZATION_INCOMPLETE,
    )
    if _lstat_or_none(_initialization_temp_path(database_path)) is not None:
        raise _initialization_marker_error()


def _open_initialization_capability(database_path, policy):
    """Bind an already validated target to its current parent identity."""

    parent = Path(database_path).parent
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(parent, flags)
    except OSError as error:
        raise DatabaseSafetyError(
            "initialization_capability_unavailable",
            "The initialization target identity could not be bound safely.",
        ) from error
    try:
        status = os.fstat(descriptor)
        named = parent.lstat()
        if (
            not stat.S_ISDIR(status.st_mode)
            or stat.S_IMODE(status.st_mode) != 0o700
            or status.st_uid != os.geteuid()
            or stat.S_ISLNK(named.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (status.st_dev, status.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise DatabaseSafetyError(
                "initialization_capability_invalid",
                "The initialization target identity is unsafe.",
            )
        return _InitializationCapability(
            database_path=Path(database_path),
            parent_descriptor=descriptor,
            parent_device=status.st_dev,
            parent_inode=status.st_ino,
            policy=policy,
            token=_INITIALIZATION_CAPABILITY_TOKEN,
        )
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _disposable_production_initialization_capability(database_path):
    """Return a production-semantics capability for an external test path.

    This private test boundary cannot designate production or any repository
    alias. It performs the real APFS and Git checks and does not create the
    parent directory or any database artifact.
    """

    database_path = Path(database_path)
    resolved = validate_test_database_path(database_path)
    if resolved != database_path:
        raise DatabaseSafetyError(
            "path_symlink",
            "The database path must not contain symbolic links.",
        )
    _check_private_directory(database_path.parent)
    verify_local_apfs(database_path.parent)
    verify_git_protection(PROJECT_ROOT)
    return _open_initialization_capability(
        database_path,
        "disposable_production",
    )


def _production_initialization_capability(database_path):
    """Return the fixed production capability; no alternate root is accepted."""

    database_path = Path(database_path)
    expected = PROJECT_ROOT / "data" / "max_smart.sqlite3"
    if database_path != PRODUCTION_DATABASE_PATH or database_path != expected:
        raise DatabaseSafetyError(
            "production_path_mismatch",
            "The production database path is not the approved fixed path.",
        )
    _assert_no_symlink(database_path, PROJECT_ROOT)
    _check_private_directory(database_path.parent)
    verify_local_apfs(database_path.parent)
    verify_git_protection(PROJECT_ROOT)
    return _open_initialization_capability(database_path, "production")


def _test_initialization_capability(database_path):
    """Bind the accepted ordinary disposable initialization path."""

    database_path = Path(database_path)
    resolved = validate_test_database_path(database_path)
    if resolved != database_path:
        raise DatabaseSafetyError(
            "path_symlink",
            "The database path must not contain symbolic links.",
        )
    _check_private_directory(database_path.parent)
    return _open_initialization_capability(database_path, "test")


def _revalidate_initialization_capability(capability):
    """Prove the capability and its named parent still identify one directory."""

    if (
        type(capability) is not _InitializationCapability
        or capability.token is not _INITIALIZATION_CAPABILITY_TOKEN
        or capability.policy
        not in {"production", "disposable_production", "test"}
    ):
        raise DatabaseSafetyError(
            "initialization_capability_invalid",
            "The initialization target capability is invalid.",
        )
    database_path = capability.database_path
    try:
        held = os.fstat(capability.parent_descriptor)
        named = database_path.parent.lstat()
    except OSError as error:
        raise DatabaseSafetyError(
            "initialization_identity_changed",
            "The initialization target identity changed.",
        ) from error
    expected = (capability.parent_device, capability.parent_inode)
    if (
        not stat.S_ISDIR(held.st_mode)
        or stat.S_IMODE(held.st_mode) != 0o700
        or held.st_uid != os.geteuid()
        or stat.S_ISLNK(named.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or stat.S_IMODE(named.st_mode) != 0o700
        or named.st_uid != os.geteuid()
        or (held.st_dev, held.st_ino) != expected
        or (named.st_dev, named.st_ino) != expected
    ):
        raise DatabaseSafetyError(
            "initialization_identity_changed",
            "The initialization target identity changed.",
        )
    if capability.policy == "production":
        expected_path = PROJECT_ROOT / "data" / "max_smart.sqlite3"
        if (
            database_path != PRODUCTION_DATABASE_PATH
            or database_path != expected_path
        ):
            raise DatabaseSafetyError(
                "production_path_mismatch",
                "The production database path is not the approved fixed path.",
            )
        _assert_no_symlink(database_path, PROJECT_ROOT)
        verify_local_apfs(database_path.parent)
        verify_git_protection(PROJECT_ROOT)
    else:
        resolved = validate_test_database_path(database_path)
        if resolved != database_path:
            raise DatabaseSafetyError(
                "path_symlink",
                "The database path must not contain symbolic links.",
            )
        if capability.policy == "disposable_production":
            verify_local_apfs(database_path.parent)
            verify_git_protection(PROJECT_ROOT)
    return database_path


def _close_initialization_capability(capability, primary_exception):
    try:
        os.close(capability.parent_descriptor)
    except OSError as cleanup_error:
        if primary_exception is None:
            raise DatabaseSafetyError(
                "initialization_capability_close_failed",
                "The initialization target capability could not be closed safely.",
            ) from cleanup_error
        if primary_exception.__cause__ is None:
            primary_exception.__cause__ = cleanup_error
        else:
            primary_exception.add_note(
                "Initialization capability cleanup also failed "
                f"({type(cleanup_error).__name__}; details redacted)."
            )


def _initialize_sqlite_capability(capability):
    """Initialize one capability-bound target using the accepted protocol.

    sqlite3.connect and os.replace address files by path. Parent identity is
    revalidated immediately before each of those calls. A replacement in the
    remaining path-based interval cannot be eliminated because SQLite cannot
    accept the retained parent descriptor.
    """

    primary_exception = None
    capability_token = _ACTIVE_INITIALIZATION_CAPABILITY.set(capability)
    try:
        module_version = verify_sqlite_module_version()
        database_path = _revalidate_initialization_capability(capability)
        _refuse_existing_initialization_artifacts(database_path)
        state_path = _initialization_state_path(database_path)
        temporary_path = _initialization_temp_path(database_path)
        old_umask = os.umask(0o077)
        try:
            database_path = _revalidate_initialization_capability(capability)
            _create_marker_exclusively(
                state_path,
                SQLITE_INITIALIZATION_INCOMPLETE,
            )
            _fsync_directory(database_path.parent)
            database_path = _revalidate_initialization_capability(capability)
            _create_sqlite_file_exclusively(database_path)
            if any(
                _lstat_or_none(Path(f"{database_path}{suffix}")) is not None
                for suffix in SQLITE_SIDECAR_SUFFIXES
            ):
                raise DatabaseSafetyError(
                    "database_initialization_artifact_exists",
                    "Database initialization requires unused artifact paths.",
                )
            database_path = _revalidate_initialization_capability(capability)
            connection = sqlite3.connect(
                f"{database_path.as_uri()}?mode=rw",
                timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
                uri=True,
            )
            try:
                verify_sqlite_connection_version(connection, module_version)
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA legacy_alter_table=OFF")
                journal_mode = connection.execute(
                    "PRAGMA journal_mode=WAL"
                ).fetchone()[0]
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute(
                    f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}"
                )
                connection.execute("PRAGMA recursive_triggers=ON")
                if journal_mode.lower() != "wal":
                    raise DatabaseSafetyError(
                        "wal_not_enabled",
                        "SQLite WAL mode could not be enabled.",
                    )
                verify_required_sqlite_pragmas(connection)
            except BaseException as connection_error:
                close_owned_resource(connection, connection_error)
                raise
            else:
                connection.close()
            database_path = _revalidate_initialization_capability(capability)
            secure_sqlite_artifacts(database_path)
            _check_initialized_artifacts(database_path)
            database_path = _revalidate_initialization_capability(capability)
            _create_marker_exclusively(
                temporary_path,
                SQLITE_INITIALIZATION_READY,
            )
            _check_marker_exact(
                temporary_path,
                SQLITE_INITIALIZATION_READY,
            )
            _fsync_directory(database_path.parent)
        finally:
            os.umask(old_umask)
        database_path = _revalidate_initialization_capability(capability)
        try:
            os.replace(temporary_path, state_path)
        except OSError as error:
            raise DatabaseSafetyError(
                "database_initialization_failed",
                "The database initialization state could not be committed.",
            ) from error
        try:
            _fsync_directory(database_path.parent)
        except BaseException as error:
            raise DatabaseSafetyError(
                "database_initialization_commit_uncertain",
                "Database initialization committed but durability is uncertain.",
            ) from error
    except BaseException as error:
        primary_exception = error
        raise
    finally:
        _ACTIVE_INITIALIZATION_CAPABILITY.reset(capability_token)
        _close_initialization_capability(capability, primary_exception)


def initialize_sqlite_file(database_path, *, production=False):
    """Explicitly initialize WAL/pragmas; callers authorize file creation."""

    database_path = Path(database_path)
    if production:
        prepare_production_database_directory(database_path)
        capability = _production_initialization_capability(database_path)
    else:
        database_path = validate_test_database_path(database_path)
        database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_private_directory(database_path.parent)
        capability = _test_initialization_capability(database_path)
    return _initialize_sqlite_capability(capability)
