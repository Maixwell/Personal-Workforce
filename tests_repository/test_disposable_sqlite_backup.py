"""Disposable SQLite backup and restore checks.

These tests use private temporary databases only. They do not create or open
the production database, fence, trust record, pin, or acceptance file.
"""

import errno
import os
import sqlite3
import stat
import threading
import unittest
from pathlib import Path
from unittest import mock

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import disposable_sqlite_backup  # noqa: E402
from disposable_sqlite_backup import (  # noqa: E402
    DisposableBackupError,
    backup_disposable_sqlite,
    restore_disposable_sqlite,
)
from max_smart_db.config import PRODUCTION_DATABASE_PATH  # noqa: E402
from max_smart_db.safety import (  # noqa: E402
    SQLITE_INITIALIZATION_INCOMPLETE,
    SQLITE_INITIALIZATION_READY,
    SQLITE_INITIALIZATION_STATE_SUFFIX,
    SQLITE_INITIALIZATION_TEMP_SUFFIX,
    DatabaseSafetyError,
    initialize_sqlite_file,
)
from provider_fence import (  # noqa: E402
    PRODUCTION_FENCE_PATH,
    _APPROVED_PIN_DIRECTORY,
)
from provisioning_rehearsal import disposable_rehearsal_root  # noqa: E402


def _private(path):
    path.mkdir(mode=0o700)
    return path


def _rows(path):
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return connection.execute(
            "SELECT value FROM items ORDER BY value"
        ).fetchall()
    finally:
        connection.close()


class DisposableSqliteBackupTests(unittest.TestCase):
    def setUp(self):
        self.root = disposable_rehearsal_root()
        self.addCleanup(self._remove_root)
        self.session = PROJECT_ROOT / "data" / "current_session.json"
        self.session_before = self.session.lstat()
        self.source = self.root / "source.sqlite3"
        initialize_sqlite_file(self.source)
        self._seed(self.source, (1,))

    def _remove_root(self):
        import shutil

        shutil.rmtree(self.root, True)

    def _seed(self, path, values):
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS items(value INTEGER)")
            connection.executemany(
                "INSERT INTO items(value) VALUES (?)",
                [(value,) for value in values],
            )
            connection.commit()
        finally:
            connection.close()

    def _destination(self, name="copy.sqlite3"):
        return self.root / name

    def _assert_production_absent(self):
        self.assertFalse(PRODUCTION_DATABASE_PATH.exists())
        self.assertFalse(PRODUCTION_FENCE_PATH.exists())
        self.assertFalse((PRODUCTION_FENCE_PATH.parent / "fence.trust").exists())
        self.assertFalse(_APPROVED_PIN_DIRECTORY.exists())
        current = self.session.lstat()
        self.assertEqual(current.st_ino, self.session_before.st_ino)
        self.assertEqual(current.st_size, self.session_before.st_size)
        self.assertEqual(current.st_mtime_ns, self.session_before.st_mtime_ns)

    def test_wal_snapshot_includes_committed_rows_and_omits_open_transaction(self):
        connection = sqlite3.connect(self.source)
        try:
            connection.execute("INSERT INTO items(value) VALUES (2)")
            connection.commit()
            self.assertGreater(Path(f"{self.source}-wal").stat().st_size, 0)
            connection.execute("INSERT INTO items(value) VALUES (99)")
            destination = self._destination()
            published = backup_disposable_sqlite(self.source, destination)
        finally:
            connection.close()
        self.assertEqual(Path(published.path), destination)
        self.assertEqual(_rows(destination), [(1,), (2,)])
        self.assertEqual(destination.lstat().st_uid, os.geteuid())
        self.assertEqual(stat.S_IMODE(destination.lstat().st_mode), 0o600)
        restored = self._destination("restored.sqlite3")
        restore_disposable_sqlite(destination, restored)
        self.assertEqual(_rows(restored), [(1,), (2,)])
        with self.assertRaises(DisposableBackupError) as existing:
            restore_disposable_sqlite(destination, restored)
        self.assertEqual(existing.exception.code, "restore_exists")
        self.assertEqual(_rows(restored), [(1,), (2,)])
        self._assert_production_absent()

    def test_concurrent_commits_restore_as_a_contiguous_prefix(self):
        destination = self._destination()
        stop = threading.Event()

        def writer():
            connection = sqlite3.connect(self.source)
            try:
                connection.execute("PRAGMA busy_timeout=5000")
                value = 2
                while not stop.is_set():
                    connection.execute(
                        "INSERT INTO items(value) VALUES (?)",
                        (value,),
                    )
                    connection.commit()
                    value += 1
            finally:
                connection.close()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            published = backup_disposable_sqlite(self.source, destination)
        finally:
            stop.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        rows = [row[0] for row in _rows(destination)]
        self.assertGreaterEqual(rows[0], 1)
        self.assertEqual(rows, list(range(rows[0], rows[0] + len(rows))))
        self.assertEqual(destination.stat().st_ino, published.inode)
        self._assert_production_absent()

    def test_existing_destination_and_sidecar_are_not_overwritten(self):
        destination = self._destination()
        destination.write_bytes(b"keep")
        os.chmod(destination, 0o600)
        with self.assertRaises(DisposableBackupError) as existing:
            backup_disposable_sqlite(self.source, destination)
        self.assertEqual(existing.exception.code, "backup_exists")
        self.assertEqual(destination.read_bytes(), b"keep")
        destination.unlink()
        sidecar = Path(f"{destination}-wal")
        sidecar.write_bytes(b"sidecar")
        os.chmod(sidecar, 0o600)
        with self.assertRaises(DisposableBackupError) as artifact:
            backup_disposable_sqlite(self.source, destination)
        self.assertEqual(artifact.exception.code, "backup_artifact_exists")
        self.assertFalse(destination.exists())
        self.assertEqual(sidecar.read_bytes(), b"sidecar")
        self._assert_production_absent()

    def test_symlinks_permissions_and_aliases_are_refused(self):
        destination = self._destination()
        linked = self.root / "linked.sqlite3"
        linked.symlink_to(self.source)
        with self.assertRaises(DisposableBackupError) as source_link:
            backup_disposable_sqlite(linked, destination)
        self.assertEqual(source_link.exception.code, "backup_symlink")
        self.assertFalse(destination.exists())
        real = _private(self.root / "real-dest")
        alias = self.root / "alias-dest"
        alias.symlink_to(real, target_is_directory=True)
        with self.assertRaises(DisposableBackupError) as directory_link:
            backup_disposable_sqlite(self.source, alias / "copy.sqlite3")
        self.assertEqual(directory_link.exception.code, "backup_symlink")
        os.chmod(self.source, 0o644)
        try:
            with self.assertRaises(DisposableBackupError) as permissions:
                backup_disposable_sqlite(self.source, destination)
            self.assertEqual(permissions.exception.code, "backup_permissions")
        finally:
            os.chmod(self.source, 0o600)
        os.chmod(self.root, 0o755)
        try:
            with self.assertRaises(DisposableBackupError) as directory:
                backup_disposable_sqlite(self.source, destination)
            self.assertEqual(directory.exception.code, "backup_permissions")
        finally:
            os.chmod(self.root, 0o700)
        with self.assertRaises(DisposableBackupError) as relative:
            backup_disposable_sqlite(self.source, "copy.sqlite3")
        self.assertEqual(relative.exception.code, "backup_path_refused")
        with self.assertRaises(DisposableBackupError) as same:
            backup_disposable_sqlite(self.source, self.source)
        self.assertEqual(same.exception.code, "backup_same_path")
        extra = self.root / "extra.sqlite3"
        os.link(self.source, extra)
        with self.assertRaises(DisposableBackupError) as linked_file:
            backup_disposable_sqlite(self.source, destination)
        self.assertEqual(linked_file.exception.code, "backup_hard_link")
        self.assertFalse(destination.exists())
        self._assert_production_absent()

    def test_production_paths_and_cloud_aliases_are_refused(self):
        self.assertEqual(
            disposable_sqlite_backup._PIN_DIRECTORY,
            _APPROVED_PIN_DIRECTORY,
        )
        for operation, target in (
            (backup_disposable_sqlite, PRODUCTION_DATABASE_PATH),
            (backup_disposable_sqlite, PRODUCTION_FENCE_PATH),
            (
                backup_disposable_sqlite,
                _APPROVED_PIN_DIRECTORY / "provider-fence.pin",
            ),
            (restore_disposable_sqlite, PRODUCTION_DATABASE_PATH),
        ):
            with self.assertRaises(DisposableBackupError) as refused:
                operation(self.source, target)
            self.assertTrue(refused.exception.code.endswith("_path_refused"))
        with self.assertRaises(DisposableBackupError) as cloud:
            backup_disposable_sqlite(
                self.source,
                self.root / "Dropbox" / "copy.sqlite3",
            )
        self.assertEqual(cloud.exception.code, "backup_filesystem")
        self.assertFalse(PRODUCTION_DATABASE_PATH.exists())
        self._assert_production_absent()

    def test_owner_mismatch_and_unmatched_cleanup_fail_closed(self):
        class Status:
            st_uid = 0
            st_mode = stat.S_IFREG | 0o600
            st_nlink = 1
            st_size = 4

        with self.assertRaises(DisposableBackupError) as owner:
            disposable_sqlite_backup._require_private_file(Status(), "backup")
        self.assertEqual(owner.exception.code, "backup_owner")
        kept = self.root / "kept.sqlite3"
        kept.write_bytes(b"keep")
        os.chmod(kept, 0o600)
        status = kept.lstat()
        with self.assertRaises(DisposableBackupError) as cleanup:
            disposable_sqlite_backup._remove_owned_file(
                kept,
                status.st_dev,
                status.st_ino + 1,
                "backup",
            )
        self.assertEqual(cleanup.exception.code, "backup_cleanup_uncertain")
        self.assertEqual(kept.read_bytes(), b"keep")
        self._assert_production_absent()

    def test_missing_source_creates_no_destination(self):
        missing = self.root / "missing.sqlite3"
        destination = self._destination()
        with self.assertRaises(DisposableBackupError) as absent:
            backup_disposable_sqlite(missing, destination)
        self.assertEqual(absent.exception.code, "backup_source_missing")
        self.assertFalse(destination.exists())
        self._assert_production_absent()

    def test_exclusive_lock_publishes_nothing(self):
        locked = self.root / "locked.sqlite3"
        initialize_sqlite_file(locked)
        holder = sqlite3.connect(locked)
        destination = self._destination()
        try:
            holder.execute("CREATE TABLE items(value INTEGER)")
            holder.execute("INSERT INTO items(value) VALUES (1)")
            holder.commit()
            holder.execute("PRAGMA journal_mode=DELETE")
            holder.execute("BEGIN EXCLUSIVE")
            with self.assertRaises(DisposableBackupError) as busy:
                backup_disposable_sqlite(locked, destination)
            self.assertEqual(busy.exception.code, "backup_lock_contention")
        finally:
            holder.close()
        self.assertFalse(destination.exists())
        self._assert_production_absent()

    def test_corrupt_backup_is_not_restored(self):
        backup = self._destination("backup.sqlite3")
        backup_disposable_sqlite(self.source, backup)
        corrupt = self.root / "corrupt.sqlite3"
        payload = bytearray(backup.read_bytes())
        payload[len(payload) // 2] ^= 0xFF
        corrupt.write_bytes(payload)
        os.chmod(corrupt, 0o600)
        restored = self._destination("restored.sqlite3")
        with self.assertRaises(DisposableBackupError) as failed:
            restore_disposable_sqlite(corrupt, restored)
        self.assertEqual(failed.exception.code, "restore_integrity")
        self.assertFalse(restored.exists())
        self._assert_production_absent()

    def test_file_fsync_failure_publishes_nothing(self):
        real_fsync = os.fsync

        def fail_regular(descriptor):
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError(errno.EIO, "file fsync")
            return real_fsync(descriptor)

        destination = self._destination()
        with mock.patch("disposable_sqlite_backup.os.fsync", side_effect=fail_regular):
            with self.assertRaises(DisposableBackupError) as failed:
                backup_disposable_sqlite(self.source, destination)
        self.assertIn(
            failed.exception.code,
            {"backup_interrupted", "backup_cleanup_uncertain"},
        )
        self.assertFalse(destination.exists())
        self._assert_production_absent()

    def test_directory_fsync_failure_leaves_published_file_unreplaced(self):
        real_fsync = os.fsync
        failed = {"count": 0}

        def fail_directory(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                failed["count"] += 1
                raise OSError(errno.EIO, "directory fsync")
            return real_fsync(descriptor)

        destination = self._destination()
        with mock.patch(
            "disposable_sqlite_backup.os.fsync",
            side_effect=fail_directory,
        ):
            with self.assertRaises(DisposableBackupError) as uncertain:
                backup_disposable_sqlite(self.source, destination)
        self.assertEqual(uncertain.exception.code, "backup_durability_uncertain")
        self.assertGreater(failed["count"], 0)
        self.assertTrue(destination.is_file())
        self.assertEqual(_rows(destination), [(1,)])
        with self.assertRaises(DisposableBackupError) as retry:
            backup_disposable_sqlite(self.source, destination)
        self.assertEqual(retry.exception.code, "backup_exists")
        self.assertEqual(_rows(destination), [(1,)])
        self._assert_production_absent()

    def test_temp_unlink_failure_is_uncertain_and_keeps_destination(self):
        real_unlink = os.unlink

        def fail_temp(path, *args, **kwargs):
            if Path(path).name.startswith(".backup-"):
                raise OSError(errno.EIO, "unlink")
            return real_unlink(path, *args, **kwargs)

        destination = self._destination()
        with mock.patch("disposable_sqlite_backup.os.unlink", side_effect=fail_temp):
            with self.assertRaises(DisposableBackupError) as uncertain:
                backup_disposable_sqlite(self.source, destination)
        self.assertEqual(uncertain.exception.code, "backup_cleanup_uncertain")
        self.assertEqual(_rows(destination), [(1,)])
        with self.assertRaises(DisposableBackupError) as retry:
            backup_disposable_sqlite(self.source, destination)
        self.assertEqual(retry.exception.code, "backup_exists")
        self._assert_production_absent()

    def _marker(self, database):
        return Path(f"{database}{SQLITE_INITIALIZATION_STATE_SUFFIX}")

    def _write_private(self, path, payload):
        path.write_bytes(payload)
        os.chmod(path, 0o600)

    def _assert_no_temporary_names(self):
        leftovers = [
            path.name
            for path in self.root.iterdir()
            if path.name.startswith(".backup-") or path.name.startswith(".restore-")
        ]
        self.assertEqual(leftovers, [])

    def test_backup_requires_bound_ready_marker(self):
        marker = self._marker(self.source)
        before = marker.lstat()
        self.assertEqual(marker.read_bytes(), SQLITE_INITIALIZATION_READY)
        destination = self._destination()
        published = backup_disposable_sqlite(self.source, destination)
        self.assertEqual(Path(published.path), destination)
        self.assertEqual(_rows(destination), [(1,)])
        self.assertEqual(marker.read_bytes(), SQLITE_INITIALIZATION_READY)
        self.assertEqual(marker.lstat().st_ino, before.st_ino)
        self.assertFalse(self._marker(destination).exists())
        restored = self._destination("restored.sqlite3")
        restore_disposable_sqlite(destination, restored)
        self.assertEqual(_rows(restored), [(1,)])
        self._assert_production_absent()

    def test_backup_refuses_missing_incomplete_invalid_and_unbound_markers(self):
        destination = self._destination()
        marker = self._marker(self.source)
        marker.unlink()
        with self.assertRaises(DisposableBackupError) as missing:
            backup_disposable_sqlite(self.source, destination)
        self.assertEqual(missing.exception.code, "backup_marker_invalid")
        self.assertFalse(destination.exists())
        self.assertNotIn(str(self.source), str(missing.exception))

        self._write_private(marker, SQLITE_INITIALIZATION_INCOMPLETE)
        with self.assertRaises(DisposableBackupError) as incomplete:
            backup_disposable_sqlite(self.source, destination)
        self.assertEqual(incomplete.exception.code, "backup_marker_invalid")
        self.assertEqual(marker.read_bytes(), SQLITE_INITIALIZATION_INCOMPLETE)

        self._write_private(marker, b"max-smart/sqlite-init/v1 OTHER\n")
        with self.assertRaises(DisposableBackupError) as mismatched:
            backup_disposable_sqlite(self.source, destination)
        self.assertEqual(mismatched.exception.code, "backup_marker_invalid")
        self.assertEqual(marker.read_bytes(), b"max-smart/sqlite-init/v1 OTHER\n")

        self._write_private(marker, SQLITE_INITIALIZATION_READY + b" ")
        with self.assertRaises(DisposableBackupError) as extra:
            backup_disposable_sqlite(self.source, destination)
        self.assertEqual(extra.exception.code, "backup_marker_invalid")

        self._write_private(marker, SQLITE_INITIALIZATION_READY)
        temporary = Path(f"{self.source}{SQLITE_INITIALIZATION_TEMP_SUFFIX}")
        self._write_private(temporary, SQLITE_INITIALIZATION_READY)
        with self.assertRaises(DisposableBackupError) as pending:
            backup_disposable_sqlite(self.source, destination)
        self.assertEqual(pending.exception.code, "backup_marker_invalid")
        self.assertEqual(temporary.read_bytes(), SQLITE_INITIALIZATION_READY)
        self.assertFalse(destination.exists())

        other = self.root / "other.sqlite3"
        initialize_sqlite_file(other)
        self._seed(other, (7,))
        other_marker = self._marker(other)
        self.assertTrue(other_marker.is_file())
        marker.unlink()
        with self.assertRaises(DisposableBackupError) as unbound:
            backup_disposable_sqlite(self.source, destination)
        self.assertEqual(unbound.exception.code, "backup_marker_invalid")
        self.assertEqual(other_marker.read_bytes(), SQLITE_INITIALIZATION_READY)
        self.assertFalse(destination.exists())
        self._assert_no_temporary_names()
        self._assert_production_absent()

    def test_sqlite_version_errors_keep_a_distinct_code(self):
        destination = self._destination()
        cases = (
            (
                "disposable_sqlite_backup.verify_sqlite_module_version",
                "sqlite_version_unsupported",
                "SQLite 3.33.0 or newer is required.",
            ),
            (
                "disposable_sqlite_backup.verify_sqlite_connection_version",
                "sqlite_version_mismatch",
                "The SQLite module and connection versions do not match.",
            ),
        )
        for target, code, message in cases:
            with self.subTest(code=code):
                with mock.patch(
                    target,
                    side_effect=DatabaseSafetyError(code, message),
                ):
                    with self.assertRaises(DisposableBackupError) as failed:
                        backup_disposable_sqlite(self.source, destination)
                self.assertEqual(failed.exception.code, "backup_sqlite_version")
                self.assertNotIn("path_refused", failed.exception.code)
                self.assertNotIn(str(self.source), str(failed.exception))
                self.assertNotIn(str(destination), str(failed.exception))
                self.assertNotIn("/", str(failed.exception))
                self.assertFalse(destination.exists())
        self._assert_no_temporary_names()
        self._assert_production_absent()

    def _plant_during_backup(self, sibling, payload):
        real_snapshot = disposable_sqlite_backup._snapshot_database

        def plant(source_connection, target_connection):
            self._write_private(sibling, payload)
            return real_snapshot(source_connection, target_connection)

        return mock.patch("disposable_sqlite_backup._snapshot_database", plant)

    def _plant_before_link(self, sibling, payload):
        real_link = os.link

        def plant(source, destination, *args, **kwargs):
            self._write_private(sibling, payload)
            return real_link(source, destination, *args, **kwargs)

        return mock.patch("disposable_sqlite_backup.os.link", plant)

    def test_sibling_inserted_during_copy_is_not_published(self):
        backup_destination = self._destination("during-backup.sqlite3")
        self.assertEqual(
            [
                path.name
                for path in disposable_sqlite_backup._artifact_paths(
                    backup_destination
                )
            ],
            [
                "during-backup.sqlite3",
                "during-backup.sqlite3-wal",
                "during-backup.sqlite3-shm",
                "during-backup.sqlite3-journal",
                "during-backup.sqlite3.initialization-state",
                "during-backup.sqlite3.initialization-state.tmp",
            ],
        )
        backup_sibling = Path(f"{backup_destination}-shm")
        with self._plant_during_backup(backup_sibling, b"during-copy"):
            with self.assertRaises(DisposableBackupError) as backup_refused:
                backup_disposable_sqlite(self.source, backup_destination)
        self.assertEqual(backup_refused.exception.code, "backup_artifact_exists")
        self.assertFalse(backup_destination.exists())
        self.assertEqual(backup_sibling.read_bytes(), b"during-copy")

        published = self._destination("published.sqlite3")
        backup_disposable_sqlite(self.source, published)
        restore_destination = self._destination("during-restore.sqlite3")
        restore_sibling = Path(f"{restore_destination}-journal")
        with self._plant_during_backup(restore_sibling, b"during-restore"):
            with self.assertRaises(DisposableBackupError) as restore_refused:
                restore_disposable_sqlite(published, restore_destination)
        self.assertEqual(restore_refused.exception.code, "restore_artifact_exists")
        self.assertFalse(restore_destination.exists())
        self.assertEqual(restore_sibling.read_bytes(), b"during-restore")
        self._assert_no_temporary_names()
        self._assert_production_absent()

    def test_sibling_inserted_between_check_and_link_is_not_accepted(self):
        backup_destination = self._destination("race-backup.sqlite3")
        backup_sibling = Path(f"{backup_destination}{SQLITE_INITIALIZATION_STATE_SUFFIX}")
        with self._plant_before_link(backup_sibling, b"check-to-link"):
            with self.assertRaises(DisposableBackupError) as backup_refused:
                backup_disposable_sqlite(self.source, backup_destination)
        self.assertEqual(backup_refused.exception.code, "backup_artifact_exists")
        self.assertTrue(backup_destination.is_file())
        self.assertEqual(backup_sibling.read_bytes(), b"check-to-link")
        with self.assertRaises(DisposableBackupError) as backup_retry:
            backup_disposable_sqlite(self.source, backup_destination)
        self.assertEqual(backup_retry.exception.code, "backup_exists")
        self.assertEqual(backup_sibling.read_bytes(), b"check-to-link")

        published = self._destination("race-source.sqlite3")
        backup_disposable_sqlite(self.source, published)
        restore_destination = self._destination("race-restore.sqlite3")
        restore_sibling = Path(
            f"{restore_destination}{SQLITE_INITIALIZATION_TEMP_SUFFIX}"
        )
        with self._plant_before_link(restore_sibling, b"restore-race"):
            with self.assertRaises(DisposableBackupError) as restore_refused:
                restore_disposable_sqlite(published, restore_destination)
        self.assertEqual(restore_refused.exception.code, "restore_artifact_exists")
        self.assertTrue(restore_destination.is_file())
        self.assertEqual(restore_sibling.read_bytes(), b"restore-race")
        with self.assertRaises(DisposableBackupError) as restore_retry:
            restore_disposable_sqlite(published, restore_destination)
        self.assertEqual(restore_retry.exception.code, "restore_exists")
        self.assertEqual(restore_sibling.read_bytes(), b"restore-race")
        self._assert_no_temporary_names()
        self._assert_production_absent()

    def test_main_file_inserted_before_link_is_not_overwritten(self):
        destination = self._destination("occupied.sqlite3")
        real_link = os.link

        def occupy(source, target, *args, **kwargs):
            self._write_private(Path(target), b"occupant")
            return real_link(source, target, *args, **kwargs)

        with mock.patch("disposable_sqlite_backup.os.link", occupy):
            with self.assertRaises(DisposableBackupError) as refused:
                backup_disposable_sqlite(self.source, destination)
        self.assertEqual(refused.exception.code, "backup_exists")
        self.assertEqual(destination.read_bytes(), b"occupant")
        self._assert_no_temporary_names()
        self._assert_production_absent()


if __name__ == "__main__":
    unittest.main()
