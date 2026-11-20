"""Disposable provisioning rehearsal. No production resource is created."""

import errno
import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from max_smart_db.config import PRODUCTION_DATABASE_PATH  # noqa: E402
from max_smart_db.safety import (  # noqa: E402
    SQLITE_INITIALIZATION_INCOMPLETE,
    SQLITE_INITIALIZATION_STATE_SUFFIX,
    SQLITE_INITIALIZATION_TEMP_SUFFIX,
    DatabaseSafetyError,
    initialize_sqlite_file,
    validate_test_database_path,
)
from provider_fence import (  # noqa: E402
    PRODUCTION_FENCE_PATH,
    FenceBusy,
    FenceHeld,
    FenceSafetyError,
    ProviderFenceConfig,
    _APPROVED_PIN_DIRECTORY,
    accept_disposable_fence_receipt,
    acquire_exclusive,
    begin_disposable_fence_provision,
    complete_disposable_fence_provision,
    provision_disposable_fence_pin,
    release_fence,
)
import provisioning_rehearsal  # noqa: E402
from provisioning_rehearsal import (  # noqa: E402
    assert_disposable_root,
    disposable_rehearsal_root,
    refuse_production_database_initialization,
    refuse_production_fence,
    refuse_protected_pin_provision,
    refuse_symlink_database,
    rehearsal_database_path,
    rehearse_database_initialization,
    rehearse_fence_acceptance,
)


def _child_env():
    return {**os.environ, "PYTHONPATH": str(SRC_ROOT)}


def _finish(process):
    if process.poll() is None:
        process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.wait(timeout=2)
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None and not pipe.closed:
            pipe.close()


_HOLDER = """\
import sys
from provider_fence import acquire_exclusive, load_disposable_fence_pin
config = load_disposable_fence_pin(sys.argv[1])
held = acquire_exclusive(config)
if type(held).__name__ != "FenceHeld":
    sys.stdout.write("fail\\n")
    sys.stdout.flush()
    sys.exit(1)
sys.stdout.write("held\\n")
sys.stdout.flush()
sys.stdin.readline()
"""


class _Interrupted(BaseException):
    """Test-only interruption that must not be converted into success."""


class ProvisioningRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.root = disposable_rehearsal_root()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_initialization_order_wal_and_private_modes(self):
        database = rehearsal_database_path(self.root)
        created = []
        real_open = os.open

        def spy(path, flags, mode=0o777, **kwargs):
            if flags & os.O_CREAT:
                created.append(Path(path).name)
            return real_open(path, flags, mode, **kwargs)

        with mock.patch("max_smart_db.safety.os.open", side_effect=spy):
            result = rehearse_database_initialization(self.root)
        marker = Path(result.marker_path).name
        database_name = Path(result.database_path).name
        self.assertLess(created.index(marker), created.index(database_name))
        self.assertEqual(result.journal_mode.lower(), "wal")
        self.assertEqual(result.synchronous, 2)
        self.assertEqual(result.directory_mode, 0o700)
        self.assertEqual(result.database_mode, 0o600)
        self.assertEqual(result.marker_mode, 0o600)
        self.assertFalse(
            Path(f"{result.database_path}{SQLITE_INITIALIZATION_TEMP_SUFFIX}").exists()
        )

    def test_existing_database_marker_and_partial_marker_are_refused(self):
        database = rehearsal_database_path(self.root)
        database.parent.mkdir(mode=0o700)
        database.touch(mode=0o600)
        with self.assertRaises(DatabaseSafetyError) as existing:
            initialize_sqlite_file(database, production=False)
        self.assertEqual(
            existing.exception.code,
            "database_initialization_artifact_exists",
        )
        database.unlink()
        validated = validate_test_database_path(database)
        self.assertEqual(validated, database)
        self.assertTrue(validated.is_relative_to(self.root))
        marker = Path(f"{validated}{SQLITE_INITIALIZATION_STATE_SUFFIX}")
        descriptor = os.open(
            marker,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(descriptor, SQLITE_INITIALIZATION_INCOMPLETE)
        finally:
            os.close(descriptor)
        with self.assertRaises(DatabaseSafetyError) as partial:
            initialize_sqlite_file(database, production=False)
        self.assertEqual(
            partial.exception.code,
            "database_initialization_artifact_exists",
        )
        self.assertEqual(marker.read_bytes(), SQLITE_INITIALIZATION_INCOMPLETE)
        self.assertFalse(database.exists())
        marker.unlink()
        temporary = Path(f"{database}{SQLITE_INITIALIZATION_TEMP_SUFFIX}")
        temporary.touch(mode=0o600)
        with self.assertRaises(DatabaseSafetyError) as temporary_marker:
            initialize_sqlite_file(database, production=False)
        self.assertEqual(
            temporary_marker.exception.code,
            "database_initialization_artifact_exists",
        )

    def test_wrong_mode_owner_and_symlink_are_refused(self):
        database = rehearsal_database_path(self.root)
        database.parent.mkdir(mode=0o700)
        os.chmod(database.parent, 0o755)
        with self.assertRaises(DatabaseSafetyError) as mode:
            initialize_sqlite_file(database, production=False)
        self.assertEqual(mode.exception.code, "data_directory_permissions")
        self.assertFalse(database.exists())
        os.chmod(database.parent, 0o700)
        with mock.patch("max_smart_db.safety.os.geteuid", return_value=1):
            with self.assertRaises(DatabaseSafetyError) as owner:
                initialize_sqlite_file(database, production=False)
        self.assertEqual(owner.exception.code, "data_directory_owner")
        self.assertFalse(
            Path(f"{database}{SQLITE_INITIALIZATION_STATE_SUFFIX}").exists()
        )
        linked = self.root / "linked"
        linked.mkdir(mode=0o700)
        alias = self.root / "alias"
        alias.symlink_to(linked, target_is_directory=True)
        self.assertEqual(
            refuse_symlink_database(alias / "rehearsal.sqlite3"),
            "path_symlink",
        )
        self.assertFalse((linked / "rehearsal.sqlite3").exists())

    def test_interrupted_initialization_stays_incomplete(self):
        database = rehearsal_database_path(self.root)

        def fail_replace(source, destination):
            raise OSError(errno.EIO, "initialization interrupted")

        with mock.patch("max_smart_db.safety.os.replace", side_effect=fail_replace):
            with self.assertRaises(DatabaseSafetyError) as interrupted:
                initialize_sqlite_file(database, production=False)
        self.assertEqual(
            interrupted.exception.code,
            "database_initialization_failed",
        )
        marker = Path(f"{database}{SQLITE_INITIALIZATION_STATE_SUFFIX}")
        self.assertEqual(marker.read_bytes(), SQLITE_INITIALIZATION_INCOMPLETE)
        with self.assertRaises(DatabaseSafetyError) as retry:
            initialize_sqlite_file(database, production=False)
        self.assertEqual(
            retry.exception.code,
            "database_initialization_artifact_exists",
        )
        self.assertEqual(marker.read_bytes(), SQLITE_INITIALIZATION_INCOMPLETE)

    def test_production_paths_and_repository_roots_are_refused(self):
        before = {
            path: _metadata(path)
            for path in (
                PRODUCTION_DATABASE_PATH,
                PRODUCTION_FENCE_PATH,
                _APPROVED_PIN_DIRECTORY,
            )
        }
        self.assertEqual(
            refuse_production_database_initialization(),
            "test_path_in_repository",
        )
        self.assertEqual(
            refuse_production_fence(PRODUCTION_FENCE_PATH),
            "production_fence_refused",
        )
        disposable = self.root / "unused-pin"
        disposable.mkdir(mode=0o700)
        self.assertEqual(
            refuse_protected_pin_provision(
                PRODUCTION_DATABASE_PATH.parent,
                disposable,
            ),
            "production_fence_refused",
        )
        self.assertEqual(
            refuse_protected_pin_provision(
                disposable,
                _APPROVED_PIN_DIRECTORY,
            ),
            "production_fence_refused",
        )
        for path in (PROJECT_ROOT, PROJECT_ROOT / "data", _APPROVED_PIN_DIRECTORY):
            with self.assertRaises(FenceSafetyError) as refused:
                assert_disposable_root(path)
            self.assertEqual(refused.exception.code, "production_fence_refused")
        after = {
            path: _metadata(path)
            for path in (
                PRODUCTION_DATABASE_PATH,
                PRODUCTION_FENCE_PATH,
                _APPROVED_PIN_DIRECTORY,
            )
        }
        self.assertEqual(before, after)
        self.assertFalse((disposable / "provider.fence").exists())
        self.assertFalse((disposable / "provider-fence.pin").exists())

    def test_acceptance_binds_confirmation_and_holds_one_permit(self):
        observed = {"busy": False}

        def confirm(binding):
            gate = acquire_exclusive(
                ProviderFenceConfig(
                    binding["fence_path"],
                    binding["fence_device"],
                    binding["fence_inode"],
                )
            )
            observed["busy"] = type(gate) is FenceBusy
            return dict(binding)

        result = rehearse_fence_acceptance(self.root, confirm)
        self.assertTrue(observed["busy"])
        parsed = json.loads(result.completion.receipt.receipt_bytes.decode("ascii"))
        self.assertEqual(
            Path(result.acceptance_path).name,
            "provider-fence.acceptance",
        )
        self.assertEqual(
            stat.S_IMODE(Path(result.acceptance_path).lstat().st_mode),
            0o600,
        )
        gate = acquire_exclusive(
            ProviderFenceConfig(
                parsed["fence_path"],
                parsed["fence_device"],
                parsed["fence_inode"],
            )
        )
        self.assertIsInstance(gate, FenceHeld)
        self.assertIsNone(release_fence(gate))

    def test_wrong_confirmation_and_receipt_create_no_acceptance(self):
        fence = self.root / "fence"
        pin = self.root / "pin"
        fence.mkdir(mode=0o700)
        pin.mkdir(mode=0o700)
        process = begin_disposable_fence_provision(fence, pin)
        completion = complete_disposable_fence_provision(process)
        acceptance = pin / "provider-fence.acceptance"

        def wrong(binding):
            decision = dict(binding)
            decision["receipt_id"] = "0" * 32
            return decision

        with self.assertRaises(FenceSafetyError) as confirmation:
            accept_disposable_fence_receipt(completion, pin, wrong)
        self.assertEqual(
            confirmation.exception.code,
            "acceptance_confirmation_refused",
        )
        self.assertFalse(acceptance.exists())
        with self.assertRaises(FenceSafetyError) as not_dict:
            accept_disposable_fence_receipt(completion, pin, lambda _binding: "yes")
        self.assertEqual(
            not_dict.exception.code,
            "acceptance_confirmation_refused",
        )
        parsed = json.loads(completion.receipt.receipt_bytes.decode("ascii"))
        pin_path = Path(parsed["pin_path"])
        original = pin_path.read_bytes()
        pin_path.write_bytes(original[:-1] + bytes([original[-1] ^ 0x01]))
        os.chmod(pin_path, 0o600)
        called = {"confirm": False}

        def confirm(binding):
            called["confirm"] = True
            return dict(binding)

        with self.assertRaises(FenceSafetyError) as receipt:
            accept_disposable_fence_receipt(completion, pin, confirm)
        self.assertEqual(receipt.exception.code, "receipt_chain_mismatch")
        self.assertFalse(called["confirm"])
        self.assertFalse(acceptance.exists())

    def test_lock_contention_refuses_before_confirmation(self):
        fence = self.root / "fence"
        pin = self.root / "pin"
        fence.mkdir(mode=0o700)
        pin.mkdir(mode=0o700)
        process = begin_disposable_fence_provision(fence, pin)
        completion = complete_disposable_fence_provision(process)
        holder = subprocess.Popen(
            (
                sys.executable,
                "-B",
                "-c",
                _HOLDER,
                str(pin / "provider-fence.pin"),
            ),
            cwd=PROJECT_ROOT,
            env=_child_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(_finish, holder)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        called = {"confirm": False}

        def confirm(binding):
            called["confirm"] = True
            return dict(binding)

        with self.assertRaises(FenceSafetyError) as busy:
            accept_disposable_fence_receipt(completion, pin, confirm)
        self.assertEqual(busy.exception.code, "fence_busy")
        self.assertFalse(called["confirm"])
        self.assertFalse((pin / "provider-fence.acceptance").exists())

    def test_uncertain_release_quarantines_the_permit(self):
        fence = self.root / "fence"
        pin = self.root / "pin"
        fence.mkdir(mode=0o700)
        pin.mkdir(mode=0o700)
        provision_disposable_fence_pin(fence, pin)
        from provider_fence import load_disposable_fence_pin

        config = load_disposable_fence_pin(str(pin / "provider-fence.pin"))
        held = acquire_exclusive(config)
        self.assertIsInstance(held, FenceHeld)
        real_close = os.close

        def ambiguous_close(descriptor):
            if descriptor == held.descriptor:
                real_close(descriptor)
                raise OSError(errno.EINTR, "close ambiguous")
            return real_close(descriptor)

        with mock.patch("provider_fence.os.close", side_effect=ambiguous_close):
            problem = release_fence(held)
        self.assertEqual(problem.errno, errno.EINTR)
        self.assertTrue(held.closure_uncertain)
        self.assertTrue(held.quarantined)
        self.assertFalse(held.released)
        self.assertTrue(held.mutex.locked())
        with mock.patch("provider_fence.os.close") as later_close:
            with mock.patch("provider_fence.fcntl.lockf") as later_lock:
                self.assertIs(release_fence(held), problem)
        later_close.assert_not_called()
        later_lock.assert_not_called()
        self.assertIsInstance(acquire_exclusive(config), FenceBusy)

    def test_interrupted_provision_and_acceptance_fail_closed(self):
        fence = self.root / "fence"
        pin = self.root / "pin"
        fence.mkdir(mode=0o700)
        pin.mkdir(mode=0o700)
        process = begin_disposable_fence_provision(fence, pin)
        process.kill()
        process.wait(timeout=5)
        with self.assertRaises(FenceSafetyError) as killed:
            complete_disposable_fence_provision(process)
        self.assertEqual(killed.exception.code, "provision_exit_failed")
        self.assertFalse((pin / "provider-fence.acceptance").exists())

        replacement_fence = self.root / "fence-retry"
        replacement_pin = self.root / "pin-retry"
        replacement_fence.mkdir(mode=0o700)
        replacement_pin.mkdir(mode=0o700)
        process = begin_disposable_fence_provision(replacement_fence, replacement_pin)
        completion = complete_disposable_fence_provision(process)

        def interrupt(_binding):
            raise _Interrupted()

        with self.assertRaises(_Interrupted):
            accept_disposable_fence_receipt(completion, replacement_pin, interrupt)
        self.assertFalse((replacement_pin / "provider-fence.acceptance").exists())
        parsed = json.loads(completion.receipt.receipt_bytes.decode("ascii"))
        gate = acquire_exclusive(
            ProviderFenceConfig(
                parsed["fence_path"],
                parsed["fence_device"],
                parsed["fence_inode"],
            )
        )
        self.assertIsInstance(gate, FenceHeld)
        self.assertIsNone(release_fence(gate))

    def test_wrong_fence_mode_owner_and_symlink_create_nothing(self):
        fence = self.root / "fence"
        pin = self.root / "pin"
        fence.mkdir(mode=0o700)
        pin.mkdir(mode=0o700)
        os.chmod(fence, 0o755)
        with self.assertRaises(FenceSafetyError) as mode:
            provision_disposable_fence_pin(fence, pin)
        self.assertEqual(mode.exception.code, "fence_directory_permissions")
        self.assertFalse((fence / "provider.fence").exists())
        os.chmod(fence, 0o700)
        with mock.patch("provider_fence.os.geteuid", return_value=1):
            with self.assertRaises(FenceSafetyError) as owner:
                provision_disposable_fence_pin(fence, pin)
        self.assertEqual(owner.exception.code, "fence_directory_owner")
        self.assertFalse((fence / "provider.fence").exists())
        (fence / "provider.fence").symlink_to(fence / "absent")
        with self.assertRaises(FenceSafetyError) as link:
            provision_disposable_fence_pin(fence, pin)
        self.assertEqual(link.exception.code, "fence_symlink")
        self.assertFalse((fence / "fence.trust").exists())
        self.assertFalse((pin / "provider-fence.pin").exists())

    def test_production_sqlite_path_cannot_create_initialization_marker(self):
        marker = Path(
            f"{PRODUCTION_DATABASE_PATH}{SQLITE_INITIALIZATION_STATE_SUFFIX}"
        )
        before = _metadata(marker)
        self.assertFalse(hasattr(provisioning_rehearsal, "incomplete_marker"))
        with self.assertRaises(DatabaseSafetyError) as validation:
            validate_test_database_path(PRODUCTION_DATABASE_PATH)
        self.assertEqual(validation.exception.code, "test_path_in_repository")
        with self.assertRaises(FenceSafetyError) as rehearsal:
            rehearse_database_initialization(PRODUCTION_DATABASE_PATH.parent)
        self.assertEqual(rehearsal.exception.code, "production_fence_refused")
        self.assertEqual(_metadata(marker), before)
        self.assertFalse(marker.exists())
        self.assertFalse((PROJECT_ROOT / "database").exists())

    def test_symlink_alias_and_inode_substitutions_are_refused(self):
        real = self.root / "real"
        real.mkdir(mode=0o700)
        parent_link = self.root / "parent-link"
        parent_link.symlink_to(real, target_is_directory=True)
        child = parent_link / "child"
        child.mkdir(mode=0o700)
        with self.assertRaises(FenceSafetyError) as parent:
            rehearse_fence_acceptance(child, lambda binding: dict(binding))
        self.assertEqual(parent.exception.code, "fence_symlink")
        self.assertFalse((real / "child" / "fence").exists())
        self.assertFalse((real / "child" / "pin").exists())

        repository_alias = self.root / "repository-alias"
        repository_alias.symlink_to(PROJECT_ROOT, target_is_directory=True)
        with self.assertRaises(FenceSafetyError) as repository:
            rehearse_database_initialization(repository_alias)
        self.assertEqual(repository.exception.code, "production_fence_refused")
        self.assertFalse((PROJECT_ROOT / "database").exists())

        self.assertTrue(str(self.root).startswith("/private/"))
        alternate = Path("/var") / self.root.relative_to("/private/var")
        with self.assertRaises(FenceSafetyError) as substituted:
            rehearse_database_initialization(alternate)
        self.assertEqual(substituted.exception.code, "fence_symlink")
        self.assertFalse((self.root / "database").exists())

        cloud = self.root / "Dropbox"
        cloud.mkdir(mode=0o700)
        with self.assertRaises(FenceSafetyError) as cloud_root:
            rehearse_fence_acceptance(cloud, lambda binding: dict(binding))
        self.assertEqual(cloud_root.exception.code, "fence_path_refused")
        self.assertFalse((cloud / "fence").exists())

        payload = self.root / "payload"
        payload.write_bytes(b"disposable")
        linked = self.root / "payload-link"
        os.link(payload, linked)
        self.assertEqual(payload.stat().st_ino, linked.stat().st_ino)
        self.assertGreater(linked.stat().st_nlink, 1)
        with self.assertRaises(FenceSafetyError) as hard_link:
            rehearse_fence_acceptance(linked, lambda binding: dict(binding))
        self.assertEqual(hard_link.exception.code, "rehearsal_root_invalid")
        self.assertFalse((self.root / "fence").exists())

    def test_repository_inode_alias_and_unproven_identity_fail_closed(self):
        project_status = PROJECT_ROOT.stat()
        real_stat = Path.stat

        def alias_stat(path, *args, **kwargs):
            if os.path.realpath(path) == os.path.realpath(self.root):
                return project_status
            return real_stat(path, *args, **kwargs)

        with mock.patch("max_smart_db.safety.Path.stat", alias_stat):
            with self.assertRaises(FenceSafetyError) as aliased:
                rehearse_fence_acceptance(self.root, lambda binding: dict(binding))
        self.assertEqual(aliased.exception.code, "production_fence_refused")
        self.assertFalse((self.root / "fence").exists())
        self.assertFalse((self.root / "pin").exists())

        with mock.patch(
            "max_smart_db.safety.Path.resolve",
            side_effect=OSError("identity unavailable"),
        ):
            with self.assertRaises(FenceSafetyError) as unproven:
                assert_disposable_root(self.root)
        self.assertEqual(unproven.exception.code, "rehearsal_root_unproven")
        self.assertFalse((self.root / "database").exists())

    def test_unsafe_temporary_parent_creates_no_directory(self):
        before = _rehearsal_names(PROJECT_ROOT)
        created = []
        real_mkdir = os.mkdir

        def spy(path, mode=0o777, *, dir_fd=None):
            created.append(os.fspath(path))
            return real_mkdir(path, mode, dir_fd=dir_fd)

        with mock.patch("provisioning_rehearsal.os.mkdir", side_effect=spy):
            with self.assertRaises(FenceSafetyError) as repository:
                disposable_rehearsal_root(PROJECT_ROOT)
        self.assertEqual(repository.exception.code, "production_fence_refused")
        self.assertEqual(created, [])
        self.assertEqual(_rehearsal_names(PROJECT_ROOT), before)

        link = self.root / "unsafe-parent"
        link.symlink_to(self.root, target_is_directory=True)
        with mock.patch("provisioning_rehearsal.os.mkdir", side_effect=spy):
            with self.assertRaises(FenceSafetyError) as symlink:
                disposable_rehearsal_root(link)
        self.assertEqual(symlink.exception.code, "fence_symlink")
        self.assertEqual(created, [])
        self.assertFalse(any(path.name.startswith("max-smart-provision-rehearsal-") for path in self.root.iterdir()))


def _rehearsal_names(path):
    return sorted(
        child.name
        for child in Path(path).iterdir()
        if child.name.startswith("max-smart-provision-rehearsal-")
    )


def _metadata(path):
    try:
        status = Path(path).lstat()
    except FileNotFoundError:
        return None
    return (status.st_dev, status.st_ino, status.st_mode, status.st_uid, status.st_size)


if __name__ == "__main__":
    unittest.main()
