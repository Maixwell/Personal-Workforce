"""Disposable rehearsal of the accepted provisioning sequence.

Resources are created only after the accepted disposable-path validator
accepts the root. Production paths, aliases, and unproven identities are
refused before any rehearsal directory, database, fence, or pin is created.
"""

import os
import secrets
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from max_smart_db.config import PRODUCTION_DATABASE_PATH, PROJECT_ROOT
from max_smart_db.safety import (
    SQLITE_INITIALIZATION_READY,
    SQLITE_INITIALIZATION_STATE_SUFFIX,
    SQLITE_INITIALIZATION_TEMP_SUFFIX,
    DatabaseSafetyError,
    initialize_sqlite_file,
    validate_test_database_path,
    verify_local_apfs,
    verify_test_database_before_connect,
)
from provider_fence import (
    FenceSafetyError,
    _APPROVED_PIN_DIRECTORY,
    accept_disposable_fence_receipt,
    begin_disposable_fence_provision,
    complete_disposable_fence_provision,
    prepare_disposable_fence,
    provision_disposable_fence_pin,
)


_ROOT_VALIDATION_CODES = {
    "filesystem_identity_unavailable": "rehearsal_root_unproven",
    "path_outside_boundary": "rehearsal_root_invalid",
    "path_symlink": "fence_symlink",
    "test_path_in_repository": "production_fence_refused",
    "test_path_is_production": "production_fence_refused",
    "test_path_not_absolute": "rehearsal_root_invalid",
}


@dataclass(frozen=True)
class InitializationRehearsal:
    """One disposable database initialized by the accepted function."""

    database_path: str
    marker_path: str
    directory_mode: int
    database_mode: int
    marker_mode: int
    journal_mode: str
    synchronous: int


@dataclass(frozen=True)
class AcceptanceRehearsal:
    """One disposable fence receipt accepted by the accepted function."""

    acceptance_path: str
    completion: object


def disposable_rehearsal_root(parent=None):
    """Create a private rehearsal root only after its parent is validated.

    The default parent is the resolved system temporary directory. Validation
    uses the accepted rehearsal-root checks and opens that directory without
    following links. The new directory is created through that open descriptor.
    """

    if parent is None:
        try:
            parent = Path(tempfile.gettempdir()).resolve()
        except OSError as error:
            raise FenceSafetyError("rehearsal_root_unproven") from error
    validated = assert_disposable_root(parent)
    observed = validated.lstat()
    directory = os.open(
        validated,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    root = None
    try:
        current = os.fstat(directory)
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (observed.st_dev, observed.st_ino)
            or current.st_uid != os.geteuid()
        ):
            raise FenceSafetyError("rehearsal_root_unproven")
        name = "max-smart-provision-rehearsal-" + secrets.token_hex(8)
        old_umask = os.umask(0o077)
        try:
            os.mkdir(name, 0o700, dir_fd=directory)
        finally:
            os.umask(old_umask)
        root = validated / name
        parent_after = os.fstat(directory)
        if (parent_after.st_dev, parent_after.st_ino) != (
            current.st_dev,
            current.st_ino,
        ):
            raise FenceSafetyError("rehearsal_root_unproven")
        return assert_disposable_root(root)
    except Exception:
        if root is not None:
            try:
                os.rmdir(name, dir_fd=directory)
            except OSError:
                pass
        raise
    finally:
        os.close(directory)


def assert_disposable_root(root):
    """Refuse a root before rehearsal creation unless accepted validation passes."""

    candidate = Path(root)
    if not candidate.is_absolute():
        raise FenceSafetyError("rehearsal_root_invalid")
    _refuse_named_protected_root(candidate)
    _assert_real_directory(candidate)
    probe = candidate / "database" / "rehearsal.sqlite3"
    try:
        resolved_probe = validate_test_database_path(probe)
    except DatabaseSafetyError as error:
        mapped = _ROOT_VALIDATION_CODES.get(error.code, "rehearsal_root_unproven")
        raise FenceSafetyError(mapped) from error
    if resolved_probe != probe:
        raise FenceSafetyError("fence_symlink")
    resolved_root = resolved_probe.parents[1]
    if resolved_root != candidate:
        raise FenceSafetyError("fence_symlink")
    _refuse_named_protected_root(resolved_root)
    try:
        verify_local_apfs(resolved_root)
    except DatabaseSafetyError as error:
        if error.code == "cloud_path":
            raise FenceSafetyError("fence_path_refused") from error
        raise FenceSafetyError("fence_filesystem") from error
    return resolved_root


def rehearsal_database_path(root):
    """Return the disposable database path inside a validated root."""

    base = assert_disposable_root(root)
    return base / "database" / "rehearsal.sqlite3"


def rehearse_database_initialization(root):
    """Initialize one disposable database with the accepted ordering."""

    database = rehearsal_database_path(root)
    database.parent.mkdir(mode=0o700, exist_ok=False)
    initialize_sqlite_file(database, production=False)
    directory = database.parent.lstat()
    database_status = database.lstat()
    marker = Path(f"{database}{SQLITE_INITIALIZATION_STATE_SUFFIX}")
    temporary = Path(f"{database}{SQLITE_INITIALIZATION_TEMP_SUFFIX}")
    if temporary.exists():
        raise DatabaseSafetyError(
            "database_initialization_artifact_exists",
            "A temporary initialization marker survived rehearsal.",
        )
    marker_status = marker.lstat()
    if marker.read_bytes() != SQLITE_INITIALIZATION_READY:
        raise DatabaseSafetyError(
            "database_initialization_failed",
            "The disposable initialization marker is not READY.",
        )
    if (
        stat.S_IMODE(directory.st_mode) != 0o700
        or stat.S_IMODE(database_status.st_mode) != 0o600
        or stat.S_IMODE(marker_status.st_mode) != 0o600
        or directory.st_uid != os.geteuid()
        or database_status.st_uid != os.geteuid()
        or marker_status.st_uid != os.geteuid()
    ):
        raise DatabaseSafetyError(
            "database_permissions",
            "Disposable initialization did not preserve private ownership.",
        )
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    try:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
    finally:
        connection.close()
    if journal_mode.lower() != "wal" or synchronous != 2:
        raise DatabaseSafetyError(
            "wal_not_enabled",
            "Disposable initialization did not retain WAL and synchronous=FULL.",
        )
    return InitializationRehearsal(
        str(database),
        str(marker),
        stat.S_IMODE(directory.st_mode),
        stat.S_IMODE(database_status.st_mode),
        stat.S_IMODE(marker_status.st_mode),
        journal_mode,
        synchronous,
    )


def refuse_production_database_initialization():
    """Ask the accepted initializer to use the production path and return its refusal."""

    try:
        initialize_sqlite_file(PRODUCTION_DATABASE_PATH, production=False)
    except DatabaseSafetyError as error:
        return error.code
    raise DatabaseSafetyError(
        "production_database_not_refused",
        "The production database path was not refused.",
    )


def refuse_production_fence(path):
    """Ask disposable fence creation to use a protected path and return its refusal."""

    try:
        prepare_disposable_fence(path)
    except FenceSafetyError as error:
        return error.code
    raise FenceSafetyError("production_fence_not_refused")


def refuse_protected_pin_provision(fence_directory, pin_directory):
    """Ask pin provisioning to use a protected directory and return its refusal."""

    try:
        provision_disposable_fence_pin(fence_directory, pin_directory)
    except FenceSafetyError as error:
        return error.code
    raise FenceSafetyError("production_fence_not_refused")


def refuse_symlink_database(path):
    """Ask the accepted test-path check to open a symlinked database path."""

    try:
        verify_test_database_before_connect(path)
    except DatabaseSafetyError as error:
        return error.code
    raise DatabaseSafetyError(
        "path_symlink_not_refused",
        "A symlinked database path was not refused.",
    )


def rehearse_fence_acceptance(root, confirm):
    """Provision and accept one disposable fence using the supplied confirmation."""

    base = assert_disposable_root(root)
    fence_directory = base / "fence"
    pin_directory = base / "pin"
    fence_directory.mkdir(mode=0o700, exist_ok=False)
    pin_directory.mkdir(mode=0o700, exist_ok=False)
    process = begin_disposable_fence_provision(fence_directory, pin_directory)
    completion = complete_disposable_fence_provision(process)
    path = accept_disposable_fence_receipt(completion, pin_directory, confirm)
    return AcceptanceRehearsal(path, completion)


def _refuse_named_protected_root(path):
    try:
        resolved = path.resolve()
    except OSError as error:
        raise FenceSafetyError("rehearsal_root_unproven") from error
    protected = []
    for item in (
        PROJECT_ROOT,
        PROJECT_ROOT / "data",
        PRODUCTION_DATABASE_PATH,
        _APPROVED_PIN_DIRECTORY,
    ):
        protected.append(Path(item))
        try:
            protected.append(Path(item).resolve())
        except OSError as error:
            raise FenceSafetyError("rehearsal_root_unproven") from error
    for item in protected:
        if (
            path == item
            or item in path.parents
            or resolved == item
            or item in resolved.parents
        ):
            raise FenceSafetyError("production_fence_refused")


def _assert_real_directory(path):
    try:
        status = path.lstat()
    except OSError as error:
        raise FenceSafetyError("rehearsal_root_unproven") from error
    if stat.S_ISLNK(status.st_mode):
        raise FenceSafetyError("fence_symlink")
    if not stat.S_ISDIR(status.st_mode):
        raise FenceSafetyError("rehearsal_root_invalid")
