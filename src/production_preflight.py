"""Read-only production provisioning preflight.

The report inspects metadata and Git ignore state. It does not create,
open, migrate, repair, or delete a database, fence, pin, acceptance file,
Terminal session, or Keychain item.
"""

import os
import pwd
import stat
from dataclasses import dataclass
from pathlib import Path

from max_smart_db.config import PRODUCTION_DATABASE_PATH, PROJECT_ROOT
from max_smart_db.safety import (
    SQLITE_INITIALIZATION_STATE_SUFFIX,
    SQLITE_INITIALIZATION_TEMP_SUFFIX,
    DatabaseSafetyError,
    verify_git_protection,
    verify_local_apfs,
)
from provider_fence import _APPROVED_PIN_DIRECTORY


@dataclass(frozen=True)
class ProductionPreflight:
    """Fail-closed report. A path conflict cannot be repaired here."""

    passed: bool
    effective_uid: int
    effective_user: str
    process_home: str
    project_root: str
    project_home: str
    pin_directory: str
    pin_home: str
    account_path_conflict: bool
    filesystem_code: str
    git_code: str
    data_directory_state: str
    database_state: str
    marker_state: str
    temporary_marker_state: str
    repository_owner: int | None
    repository_mode: int | None
    repository_symlink: bool
    path_identity: str


def run_production_preflight(
    project_root=PROJECT_ROOT,
    database_path=PRODUCTION_DATABASE_PATH,
    pin_directory=_APPROVED_PIN_DIRECTORY,
):
    """Evaluate the accepted requirements without mutating the filesystem."""

    project_root = Path(project_root)
    database_path = Path(database_path)
    pin_directory = Path(pin_directory)
    identity = pwd.getpwuid(os.geteuid())
    project_home = _account_home(project_root)
    pin_home = _account_home(pin_directory)
    conflict = (
        project_home != pin_home
        or identity.pw_dir != pin_home
        or project_home != identity.pw_dir
    )
    filesystem_code = _code(lambda: verify_local_apfs(project_root), "local_apfs")
    git_code = _code(lambda: verify_git_protection(project_root), "ignored")
    (
        repository_owner,
        repository_mode,
        repository_symlink,
        path_identity,
    ) = _repository_metadata(project_root)
    data_directory = database_path.parent
    data_state = _directory_state(data_directory)
    database_state = _file_state(database_path)
    marker_state = _file_state(
        Path(f"{database_path}{SQLITE_INITIALIZATION_STATE_SUFFIX}")
    )
    temporary_state = _file_state(
        Path(f"{database_path}{SQLITE_INITIALIZATION_TEMP_SUFFIX}")
    )
    unsafe_existing = any(
        state.startswith("unsafe_")
        for state in (data_state, database_state, marker_state, temporary_state)
    )
    return ProductionPreflight(
        passed=(
            not conflict
            and filesystem_code == "local_apfs"
            and git_code == "ignored"
            and not unsafe_existing
            and data_state == "private_directory"
            and database_state == "private_file"
            and marker_state == "private_file"
            and temporary_state == "absent"
            and path_identity == "established"
            and not repository_symlink
        ),
        effective_uid=identity.pw_uid,
        effective_user=identity.pw_name,
        process_home=identity.pw_dir,
        project_root=str(project_root),
        project_home=project_home,
        pin_directory=str(pin_directory),
        pin_home=pin_home,
        account_path_conflict=conflict,
        filesystem_code=filesystem_code,
        git_code=git_code,
        data_directory_state=data_state,
        database_state=database_state,
        marker_state=marker_state,
        temporary_marker_state=temporary_state,
        repository_owner=repository_owner,
        repository_mode=repository_mode,
        repository_symlink=repository_symlink,
        path_identity=path_identity,
    )


def _account_home(path):
    parts = Path(path).parts
    if len(parts) >= 3 and parts[1] == "Users":
        return str(Path("/", "Users", parts[2]))
    return ""


def _code(check, success):
    try:
        check()
    except DatabaseSafetyError as error:
        return error.code
    return success


def _directory_state(path):
    status = _lstat(path)
    if status is None:
        return "absent"
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        return "unsafe_not_directory"
    if stat.S_IMODE(status.st_mode) != 0o700 or status.st_uid != os.geteuid():
        return "unsafe_directory_permissions"
    return "private_directory"


def _file_state(path):
    status = _lstat(path)
    if status is None:
        return "absent"
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        return "unsafe_not_file"
    if (
        stat.S_IMODE(status.st_mode) != 0o600
        or status.st_uid != os.geteuid()
        or status.st_nlink != 1
    ):
        return "unsafe_file_permissions"
    return "private_file"


def _lstat(path):
    try:
        return Path(path).lstat()
    except FileNotFoundError:
        return None


def _repository_metadata(path):
    """Return ownership, mode, symlink state, and path-identity result."""

    try:
        status = Path(path).lstat()
    except OSError:
        return None, None, False, "filesystem_identity_unavailable"
    symlink = stat.S_ISLNK(status.st_mode)
    mode = stat.S_IMODE(status.st_mode)
    owner = status.st_uid
    if symlink:
        return owner, mode, True, "path_symlink"
    if not stat.S_ISDIR(status.st_mode):
        return owner, mode, False, "unsafe_not_directory"
    if owner != os.geteuid():
        return owner, mode, False, "repository_owner"
    if mode & 0o022:
        return owner, mode, False, "repository_permissions"
    return owner, mode, False, "established"
