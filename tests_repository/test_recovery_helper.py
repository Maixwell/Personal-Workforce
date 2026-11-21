"""Disposable tests for the isolated recovery-helper core.

These tests use private temporary databases and a simulated boundary. They do
not create an operating-system account, install a helper, generate a
production key, or open production resources.
"""

import errno
import hashlib
import hmac
import inspect
import json
import os
import shutil
import sqlite3
import stat
import unittest
from pathlib import Path
from unittest import mock

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import recovery_helper  # noqa: E402
from max_smart_db.config import PRODUCTION_DATABASE_PATH  # noqa: E402
from max_smart_db.safety import (  # noqa: E402
    DatabaseSafetyError,
    initialize_sqlite_file,
)
from provider_fence import (  # noqa: E402
    PRODUCTION_FENCE_PATH,
    _APPROVED_PIN_DIRECTORY,
)
from provisioning_rehearsal import disposable_rehearsal_root  # noqa: E402
from recovery_helper import (  # noqa: E402
    DELEGATED_SOURCE_PATH,
    OS_ENFORCED_RECOVERY_ISOLATION,
    SEAL_VISIBILITY_IS_NOT_DURABILITY,
    SIMULATED_RECOVERY_BOUNDARY,
    RecoveryHelperError,
    create_delegated_recovery_snapshot,
    create_recovery_snapshot,
    restore_recovery_point,
    verify_recovery_point,
)


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


class RecoveryHelperTests(unittest.TestCase):
    def setUp(self):
        self.root = disposable_rehearsal_root()
        self.addCleanup(self._remove_root)
        self.session = PROJECT_ROOT / "data" / "current_session.json"
        self.session_before = self.session.lstat()
        self.source = self.root / "source.sqlite3"
        initialize_sqlite_file(self.source)
        self._seed(self.source, (1,))
        self.store = _private(self.root / "store")
        self.key = os.urandom(32)
        self.source_identity = os.urandom(32)
        self.store_identity = os.urandom(32)

    def _remove_root(self):
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

    def _quarantine(self, name="quarantine"):
        return _private(self.root / name)

    def _assert_production_absent(self):
        self.assertFalse(PRODUCTION_DATABASE_PATH.exists())
        self.assertFalse(PRODUCTION_FENCE_PATH.exists())
        self.assertFalse((PRODUCTION_FENCE_PATH.parent / "fence.trust").exists())
        self.assertFalse(_APPROVED_PIN_DIRECTORY.exists())
        current = self.session.lstat()
        self.assertEqual(current.st_ino, self.session_before.st_ino)
        self.assertEqual(current.st_size, self.session_before.st_size)
        self.assertEqual(current.st_mtime_ns, self.session_before.st_mtime_ns)

    def _snapshot(self, minimum_generation=None):
        return create_recovery_snapshot(
            self.source,
            self.store,
            self.key,
            source_identity=self.source_identity,
            store_identity=self.store_identity,
            minimum_generation=minimum_generation,
        )

    def _verify(self, store=None, key=None, **kwargs):
        values = {
            "source_identity": self.source_identity,
            "store_identity": self.store_identity,
        }
        values.update(kwargs)
        return verify_recovery_point(
            self.store if store is None else store,
            self.key if key is None else key,
            **values,
        )

    def _restore(self, quarantine, **kwargs):
        values = {
            "source_identity": self.source_identity,
            "store_identity": self.store_identity,
        }
        values.update(kwargs)
        return restore_recovery_point(
            self.store,
            quarantine,
            self.key,
            **values,
        )

    def _fail_directory_fsync(self, number):
        real_fsync = os.fsync
        seen = {"count": 0}

        def fail_directory(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                seen["count"] += 1
                if seen["count"] == number:
                    raise OSError(errno.EIO, "directory fsync")
            return real_fsync(descriptor)

        return fail_directory

    def test_simulated_boundary_is_not_os_enforced_isolation(self):
        self.assertEqual(SIMULATED_RECOVERY_BOUNDARY, "simulated")
        self.assertFalse(OS_ENFORCED_RECOVERY_ISOLATION)
        source = Path(recovery_helper.__file__).read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("os.system", source)
        self.assertNotIn("os.exec", source)
        with self.assertRaises(RecoveryHelperError) as refused:
            create_recovery_snapshot(
                self.source,
                self.store,
                self.key,
                source_identity=self.source_identity,
                store_identity=self.store_identity,
                boundary="os_enforced",
            )
        self.assertEqual(refused.exception.code, "recovery_boundary_unproven")
        self.assertEqual(list(self.store.iterdir()), [])
        self.assertNotIn(str(self.store), str(refused.exception))
        self._assert_production_absent()

    def test_snapshot_authenticates_committed_wal_rows_and_restores_quarantine(self):
        connection = sqlite3.connect(self.source)
        try:
            connection.execute("INSERT INTO items(value) VALUES (2)")
            connection.commit()
            self.assertGreater(Path(f"{self.source}-wal").stat().st_size, 0)
            connection.execute("INSERT INTO items(value) VALUES (99)")
            point = self._snapshot()
        finally:
            connection.close()
        self.assertEqual(point.generation, 1)
        self.assertEqual(point.boundary, "simulated")
        verified = self._verify()
        self.assertEqual(verified, point)
        database = self.store / "generations" / "000001" / "database.sqlite3"
        self.assertEqual(_rows(database), [(1,), (2,)])
        self.assertFalse(Path(f"{database}-wal").exists())
        self.assertFalse(Path(f"{database}-shm").exists())
        self.assertFalse(Path(f"{database}-journal").exists())
        with database.open("rb") as handle:
            self.assertEqual(handle.read(20)[18], 1)
        quarantine = self._quarantine()
        restored = self._restore(quarantine)
        self.assertEqual(restored, point)
        self.assertEqual(_rows(quarantine / "database.sqlite3"), [(1,), (2,)])
        with self.assertRaises(RecoveryHelperError) as existing:
            self._restore(quarantine)
        self.assertEqual(existing.exception.code, "recovery_quarantine_refused")
        self.assertEqual(_rows(quarantine / "database.sqlite3"), [(1,), (2,)])
        self._assert_production_absent()

    def test_generation_chain_and_rollback_are_enforced(self):
        first = self._snapshot()
        self._seed(self.source, (2,))
        second = self._snapshot(minimum_generation=1)
        self.assertEqual(second.generation, 2)
        self.assertNotEqual(second.database_sha256, first.database_sha256)
        manifest = self.store / "generations" / "000002" / "manifest.json"
        payload = json.loads(manifest.read_text(encoding="ascii"))
        self.assertEqual(payload["previous_manifest_sha256"], first.manifest_sha256)
        hidden = self.store / "generations" / "000003"
        hidden.mkdir(mode=0o700)
        with self.assertRaises(RecoveryHelperError) as rollback:
            self._verify(minimum_generation=2)
        self.assertEqual(rollback.exception.code, "recovery_rollback")
        hidden.rmdir()
        preserved = {
            name: (self.store / name).read_bytes()
            for name in (
                "head.json",
                "head.mac",
                "acceptance.json",
                "acceptance.mac",
            )
        }
        self._seed(self.source, (3,))
        third = self._snapshot(minimum_generation=2)
        self.assertEqual(third.generation, 3)
        for name, payload in preserved.items():
            (self.store / name).write_bytes(payload)
        with self.assertRaises(RecoveryHelperError) as rewound:
            self._verify(minimum_generation=3)
        self.assertEqual(rewound.exception.code, "recovery_rollback")
        self._assert_production_absent()

    def test_complete_store_replay_requires_an_external_anchor(self):
        self._snapshot()
        preserved = self.root / "preserved-store"
        shutil.copytree(self.store, preserved)
        self._seed(self.source, (2,))
        self._snapshot(minimum_generation=1)
        shutil.rmtree(self.store)
        shutil.copytree(preserved, self.store)
        rolled = self._verify()
        self.assertEqual(rolled.generation, 1)
        self.assertEqual(rolled.boundary, "simulated")
        with self.assertRaises(RecoveryHelperError) as detected:
            self._verify(minimum_generation=2)
        self.assertEqual(detected.exception.code, "recovery_rollback")
        self._assert_production_absent()

    def _generation_file(self, name):
        return self.store / "generations" / "000001" / name

    def test_tampered_content_mac_and_manifest_are_rejected(self):
        point = self._snapshot()
        database = self._generation_file("database.sqlite3")
        payload = bytearray(database.read_bytes())
        payload[len(payload) // 2] ^= 0xFF
        database.write_bytes(payload)
        os.chmod(database, 0o600)
        with self.assertRaises(RecoveryHelperError) as content:
            self._verify()
        self.assertEqual(content.exception.code, "recovery_content_mismatch")
        self._snapshot_replacement()
        manifest = self._generation_file("manifest.json")
        body = bytearray(manifest.read_bytes())
        body[-2] ^= 0x01
        manifest.write_bytes(body)
        os.chmod(manifest, 0o600)
        with self.assertRaises(RecoveryHelperError) as manifest_error:
            self._verify()
        self.assertEqual(manifest_error.exception.code, "recovery_mac_invalid")
        self._snapshot_replacement()
        encoded = b"{not-json"
        digest = hmac.new(self.key, encoded, hashlib.sha256).hexdigest().encode(
            "ascii"
        )
        manifest = self._generation_file("manifest.json")
        mac = self._generation_file("manifest.mac")
        manifest.write_bytes(encoded)
        mac.write_bytes(digest)
        os.chmod(manifest, 0o600)
        os.chmod(mac, 0o600)
        with self.assertRaises(RecoveryHelperError) as malformed:
            self._verify()
        self.assertEqual(malformed.exception.code, "recovery_manifest_invalid")
        self._snapshot_replacement()
        mac = self._generation_file("manifest.mac")
        mac.write_bytes(b"0" * 64)
        os.chmod(mac, 0o600)
        with self.assertRaises(RecoveryHelperError) as mac_error:
            self._verify()
        self.assertEqual(mac_error.exception.code, "recovery_mac_invalid")
        self.assertNotIn(self.key.hex(), str(mac_error.exception))
        self._snapshot_replacement()
        manifest = self._generation_file("manifest.json")
        mac = self._generation_file("manifest.mac")
        body = json.loads(manifest.read_text(encoding="ascii"))
        body["previous_manifest_sha256"] = "a" * 64
        encoded = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        digest = hmac.new(self.key, encoded, hashlib.sha256).hexdigest().encode(
            "ascii"
        )
        manifest.write_bytes(encoded)
        mac.write_bytes(digest)
        os.chmod(manifest, 0o600)
        os.chmod(mac, 0o600)
        with self.assertRaises(RecoveryHelperError) as chain:
            self._verify()
        self.assertEqual(chain.exception.code, "recovery_store_inconsistent")
        self.assertEqual(point.generation, 1)
        self._assert_production_absent()

    def _snapshot_replacement(self):
        shutil.rmtree(self.store)
        self.store = _private(self.root / "store")
        return self._snapshot()

    def test_sidecars_and_malformed_manifests_are_rejected_without_deletion(self):
        self._snapshot()
        database = self.store / "generations" / "000001" / "database.sqlite3"
        sidecar = Path(f"{database}-wal")
        sidecar.write_bytes(b"unexpected")
        os.chmod(sidecar, 0o600)
        with self.assertRaises(RecoveryHelperError) as rejected:
            self._verify()
        self.assertEqual(rejected.exception.code, "recovery_sidecar_rejected")
        self.assertEqual(sidecar.read_bytes(), b"unexpected")
        sidecar.unlink()
        manifest = self.store / "generations" / "000001" / "manifest.json"
        manifest.write_bytes(b"{not-json")
        os.chmod(manifest, 0o600)
        with self.assertRaises(RecoveryHelperError) as malformed:
            self._verify()
        self.assertEqual(malformed.exception.code, "recovery_mac_invalid")
        self._assert_production_absent()

    def test_key_unavailability_publishes_nothing(self):
        for key in (None, b"", b"short", "a" * 32, bytearray(32)):
            with self.subTest(key=type(key).__name__):
                with self.assertRaises(RecoveryHelperError) as unavailable:
                    create_recovery_snapshot(
                        self.source,
                        self.store,
                        key,
                        source_identity=self.source_identity,
                        store_identity=self.store_identity,
                    )
                self.assertEqual(
                    unavailable.exception.code,
                    "recovery_key_unavailable",
                )
        self.assertEqual(list(self.store.iterdir()), [])
        point = self._snapshot()
        with self.assertRaises(RecoveryHelperError) as wrong:
            self._verify(key=b"\x01" * 32)
        self.assertEqual(wrong.exception.code, "recovery_mac_invalid")
        self.assertEqual(
            self._verify().generation,
            point.generation,
        )
        self._assert_production_absent()

    def test_production_paths_permissions_and_cloud_aliases_are_refused(self):
        for target in (
            PRODUCTION_DATABASE_PATH,
            PRODUCTION_FENCE_PATH,
            _APPROVED_PIN_DIRECTORY / "provider-fence.pin",
        ):
            with self.assertRaises(RecoveryHelperError) as refused:
                create_recovery_snapshot(
                    target,
                    self.store,
                    self.key,
                    source_identity=self.source_identity,
                    store_identity=self.store_identity,
                )
            self.assertEqual(refused.exception.code, "recovery_path_refused")
        with self.assertRaises(RecoveryHelperError) as cloud:
            create_recovery_snapshot(
                self.source,
                self.root / "Dropbox" / "store",
                self.key,
                source_identity=self.source_identity,
                store_identity=self.store_identity,
            )
        self.assertEqual(cloud.exception.code, "recovery_filesystem")
        os.chmod(self.source, 0o644)
        try:
            with self.assertRaises(RecoveryHelperError) as permissions:
                self._snapshot()
            self.assertEqual(permissions.exception.code, "recovery_permissions")
        finally:
            os.chmod(self.source, 0o600)
        self.assertFalse(PRODUCTION_DATABASE_PATH.exists())
        self._assert_production_absent()

    def test_file_fsync_failure_leaves_store_empty(self):
        real_fsync = os.fsync

        def fail_regular(descriptor):
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError(errno.EIO, "file fsync")
            return real_fsync(descriptor)

        with mock.patch("recovery_helper.os.fsync", side_effect=fail_regular):
            with self.assertRaises(RecoveryHelperError) as failed:
                self._snapshot()
        self.assertEqual(failed.exception.code, "recovery_interrupted")
        self.assertFalse((self.store / "head.json").exists())
        self.assertFalse((self.store / "acceptance.json").exists())
        with self.assertRaises(RecoveryHelperError) as refused:
            self._verify()
        self.assertNotEqual(refused.exception.code, "recovery_unconfirmed")
        self._assert_production_absent()

    def test_directory_fsync_failure_does_not_advance_head(self):
        real_fsync = os.fsync

        def fail_directory(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError(errno.EIO, "directory fsync")
            return real_fsync(descriptor)

        with mock.patch("recovery_helper.os.fsync", side_effect=fail_directory):
            with self.assertRaises(RecoveryHelperError) as uncertain:
                self._snapshot()
        self.assertEqual(uncertain.exception.code, "recovery_durability_uncertain")
        self.assertFalse((self.store / "head.json").exists())
        with self.assertRaises(RecoveryHelperError) as inconsistent:
            self._verify()
        self.assertEqual(
            inconsistent.exception.code,
            "recovery_store_inconsistent",
        )
        with self.assertRaises(RecoveryHelperError) as retry:
            self._snapshot()
        self.assertIn(
            retry.exception.code,
            {"recovery_store_inconsistent", "recovery_rollback"},
        )
        self._assert_production_absent()

    def test_exclusive_lock_publishes_nothing(self):
        locked = self.root / "locked.sqlite3"
        initialize_sqlite_file(locked)
        holder = sqlite3.connect(locked)
        try:
            holder.execute("CREATE TABLE items(value INTEGER)")
            holder.execute("INSERT INTO items(value) VALUES (1)")
            holder.commit()
            holder.execute("PRAGMA journal_mode=DELETE")
            holder.execute("BEGIN EXCLUSIVE")
            with self.assertRaises(RecoveryHelperError) as busy:
                create_recovery_snapshot(
                    locked,
                    self.store,
                    self.key,
                    source_identity=self.source_identity,
                    store_identity=self.store_identity,
                )
            self.assertEqual(busy.exception.code, "recovery_lock_contention")
        finally:
            holder.close()
        self.assertFalse((self.store / "head.json").exists())
        self.assertFalse((self.store / "acceptance.json").exists())
        self._assert_production_absent()

    def test_marker_and_sqlite_version_failures_publish_nothing(self):
        with mock.patch(
            "recovery_helper.verify_sqlite_module_version",
            side_effect=DatabaseSafetyError(
                "sqlite_version_unsupported",
                "SQLite 3.33.0 or newer is required.",
            ),
        ):
            with self.assertRaises(RecoveryHelperError) as version:
                self._snapshot()
        self.assertEqual(version.exception.code, "recovery_sqlite_version")
        self.assertNotIn("path_refused", version.exception.code)
        self.assertNotIn("/", str(version.exception))
        self.assertFalse((self.store / "acceptance.json").exists())
        marker = Path(f"{self.source}.initialization-state")
        marker.unlink()
        with self.assertRaises(RecoveryHelperError) as missing:
            self._snapshot()
        self.assertEqual(missing.exception.code, "recovery_marker_invalid")
        self.assertFalse((self.store / "acceptance.json").exists())
        self.assertNotIn(str(self.source), str(missing.exception))
        self._assert_production_absent()

    def test_generation_parent_fsync_precedes_head_publication(self):
        real_fsync = os.fsync
        observed = []

        def record(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                current = os.fstat(descriptor)
                observed.append((current.st_dev, current.st_ino))
                parent = self.store / "generations"
                if parent.exists():
                    listed = parent.lstat()
                    if (listed.st_dev, listed.st_ino) == (
                        current.st_dev,
                        current.st_ino,
                    ):
                        self.assertFalse((self.store / "head.json").exists())
                        observed.append("parent-before-head")
            return real_fsync(descriptor)

        with mock.patch("recovery_helper.os.fsync", side_effect=record):
            self._snapshot()
        self.assertIn("parent-before-head", observed)
        with mock.patch(
            "recovery_helper.os.fsync",
            side_effect=self._fail_directory_fsync(2),
        ):
            with self.assertRaises(RecoveryHelperError) as failed:
                create_recovery_snapshot(
                    self.source,
                    _private(self.root / "parent-fail"),
                    self.key,
                    source_identity=self.source_identity,
                    store_identity=self.store_identity,
                )
        self.assertEqual(failed.exception.code, "recovery_durability_uncertain")
        failed_store = self.root / "parent-fail"
        self.assertFalse((failed_store / "head.json").exists())
        self.assertFalse((failed_store / "acceptance.json").exists())
        with self.assertRaises(RecoveryHelperError) as reopened:
            self._verify(store=failed_store)
        self.assertEqual(reopened.exception.code, "recovery_store_inconsistent")
        self._assert_production_absent()

    def test_publication_interruptions_are_not_accepted_after_reopening(self):
        real_replace = os.replace
        calls = {"count": 0}

        def fail_second_replace(source, destination):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError(errno.EIO, "replace")
            return real_replace(source, destination)

        with mock.patch("recovery_helper.os.replace", side_effect=fail_second_replace):
            with self.assertRaises(RecoveryHelperError) as between:
                self._snapshot()
        self.assertEqual(between.exception.code, "recovery_durability_uncertain")
        self.assertFalse((self.store / "acceptance.json").exists())
        with self.assertRaises(RecoveryHelperError) as between_reopen:
            self._verify()
        self.assertIn(
            between_reopen.exception.code,
            {"recovery_store_inconsistent", "recovery_contradictory"},
        )
        shutil.rmtree(self.store)
        self.store = _private(self.root / "store")
        with mock.patch(
            "recovery_helper.os.fsync",
            side_effect=self._fail_directory_fsync(3),
        ):
            with self.assertRaises(RecoveryHelperError) as published:
                self._snapshot()
        self.assertEqual(published.exception.code, "recovery_durability_uncertain")
        self.assertTrue((self.store / "head.json").is_file())
        self.assertFalse((self.store / "acceptance.json").exists())
        with self.assertRaises(RecoveryHelperError) as unconfirmed:
            self._verify()
        self.assertEqual(unconfirmed.exception.code, "recovery_unconfirmed")
        with self.assertRaises(RecoveryHelperError) as retry:
            self._snapshot()
        self.assertEqual(retry.exception.code, "recovery_unconfirmed")
        self._assert_production_absent()

    def test_seal_directory_fsync_failure_remains_uncertain(self):
        with mock.patch(
            "recovery_helper.os.fsync",
            side_effect=self._fail_directory_fsync(4),
        ):
            with self.assertRaises(RecoveryHelperError) as uncertain:
                self._snapshot()
        self.assertEqual(uncertain.exception.code, "recovery_durability_uncertain")
        self.assertTrue((self.store / "acceptance.json").is_file())
        self.assertTrue((self.store / "acceptance.uncertain").is_file())
        with self.assertRaises(RecoveryHelperError) as reopened:
            self._verify()
        self.assertEqual(reopened.exception.code, "recovery_durability_uncertain")
        self.assertFalse(hasattr(recovery_helper, "repair_recovery_store"))
        self._assert_production_absent()

    def test_visible_seal_without_directory_fsync_is_not_durability_proof(self):
        self.assertTrue(SEAL_VISIBILITY_IS_NOT_DURABILITY)
        real_fsync = os.fsync
        seen = {"count": 0}

        def crash_before_seal_fsync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                seen["count"] += 1
                if seen["count"] == 4:
                    raise SystemExit(1)
            return real_fsync(descriptor)

        with mock.patch(
            "recovery_helper.os.fsync",
            side_effect=crash_before_seal_fsync,
        ):
            with self.assertRaises(SystemExit):
                self._snapshot()
        self.assertTrue((self.store / "acceptance.json").is_file())
        self.assertFalse((self.store / "acceptance.uncertain").exists())
        point = self._verify()
        self.assertEqual(point.generation, 1)
        self.assertEqual(point.boundary, "simulated")
        self._assert_production_absent()

    def test_identities_reject_transplant_and_missing_fields(self):
        point = self._snapshot()
        copied = self.root / "copied-store"
        shutil.copytree(self.store, copied)
        with self.assertRaises(RecoveryHelperError) as transplanted:
            self._verify(store=copied, store_identity=os.urandom(32))
        self.assertEqual(transplanted.exception.code, "recovery_identity_mismatch")
        self.assertEqual(self._verify(store=copied).generation, point.generation)
        with self.assertRaises(RecoveryHelperError) as other_source:
            self._verify(source_identity=os.urandom(32))
        self.assertEqual(other_source.exception.code, "recovery_identity_mismatch")
        manifest = self._generation_file("manifest.json")
        mac = self._generation_file("manifest.mac")
        body = json.loads(manifest.read_text(encoding="ascii"))
        del body["source_identity"]
        del body["store_identity"]
        encoded = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        mac.write_bytes(
            hmac.new(self.key, encoded, hashlib.sha256).hexdigest().encode("ascii")
        )
        manifest.write_bytes(encoded)
        os.chmod(manifest, 0o600)
        os.chmod(mac, 0o600)
        with self.assertRaises(RecoveryHelperError) as missing:
            self._verify()
        self.assertEqual(missing.exception.code, "recovery_manifest_invalid")
        self._assert_production_absent()


    def _source_digest(self, source):
        digest = {}
        candidates = [source, Path(f"{source}.initialization-state")]
        for suffix in ("-wal", "-shm", "-journal", ".initialization-state.tmp"):
            candidates.append(Path(f"{source}{suffix}"))
        for candidate in candidates:
            if candidate.exists() and not candidate.is_symlink():
                digest[candidate.name] = hashlib.sha256(candidate.read_bytes()).hexdigest()
            elif candidate.is_symlink():
                digest[candidate.name] = "symlink"
        return digest

    def _delegated_values(self, source, **overrides):
        listed = source.lstat()
        values = {
            "source_identity": self.source_identity,
            "store_identity": self.store_identity,
            "expected_owner_uid": listed.st_uid,
            "expected_device": listed.st_dev,
            "expected_inode": listed.st_ino,
        }
        values.update(overrides)
        return values

    def _as_recovery_reader(self, source):
        return (
            mock.patch("recovery_helper.DELEGATED_SOURCE_PATH", Path(source)),
            mock.patch(
                "recovery_helper._delegated_runtime_uid",
                return_value=source.lstat().st_uid + 1,
            ),
            mock.patch(
                "recovery_helper.os.open",
                side_effect=self._read_only_source_open(source),
            ),
        )

    def _source_artifacts(self, source):
        return (
            Path(source),
            Path(f"{source}.initialization-state"),
            Path(f"{source}-wal"),
            Path(f"{source}-shm"),
        )

    def _read_only_source_open(self, source, writable=()):
        real_open = os.open
        probe = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        protected = {path for path in self._source_artifacts(source) if path.exists()}
        writable = {Path(path) for path in writable}

        def guarded(path, flags, *args, **kwargs):
            candidate = Path(path)
            if candidate in protected and flags == probe:
                if candidate in writable:
                    return real_open(path, flags, *args, **kwargs)
                raise PermissionError(errno.EACCES, "read-only delegated source")
            return real_open(path, flags, *args, **kwargs)

        return guarded

    def _delegated_snapshot(self, source=None, **overrides):
        source = self.source if source is None else source
        with mock.patch("recovery_helper.DELEGATED_SOURCE_PATH", Path(source)), mock.patch(
            "recovery_helper._delegated_runtime_uid",
            return_value=source.lstat().st_uid + 1,
        ), mock.patch(
            "recovery_helper.os.open",
            side_effect=self._read_only_source_open(source),
        ):
            return create_delegated_recovery_snapshot(
                self.store,
                self.key,
                **self._delegated_values(source, **overrides),
            )

    def test_delete_mode_snapshot_is_one_consistent_file(self):
        point = self._snapshot()
        database = self.store / "generations" / "000001" / "database.sqlite3"
        with database.open("rb") as handle:
            self.assertEqual(handle.read(20)[18], 1)
        self.assertFalse(list(database.parent.glob("database.sqlite3-*")))
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        try:
            self.assertEqual(
                connection.execute("PRAGMA journal_mode").fetchone()[0],
                "delete",
            )
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0],
                "ok",
            )
        finally:
            connection.close()
        self.assertEqual(self._verify(), point)
        self._assert_production_absent()

    def test_nonempty_snapshot_sidecar_is_refused_without_deletion(self):
        real = recovery_helper._snapshot_database
        planted = []

        def plant(*args, **kwargs):
            result = real(*args, **kwargs)
            sidecar = Path(f"{args[2]}-shm")
            sidecar.write_bytes(b"planted-sidecar")
            os.chmod(sidecar, 0o600)
            planted.append(sidecar)
            return result

        with mock.patch("recovery_helper._snapshot_database", side_effect=plant):
            with self.assertRaises(RecoveryHelperError) as refused:
                self._snapshot()
        self.assertEqual(refused.exception.code, "recovery_interrupted")
        self.assertEqual(planted[0].read_bytes(), b"planted-sidecar")
        self.assertFalse((self.store / "head.json").exists())
        self._assert_production_absent()

    def test_delegated_snapshot_reads_only_the_fixed_source(self):
        parameters = inspect.signature(create_delegated_recovery_snapshot).parameters
        self.assertNotIn("source", parameters)
        self.assertEqual(DELEGATED_SOURCE_PATH, PRODUCTION_DATABASE_PATH)
        self.assertFalse(PRODUCTION_DATABASE_PATH.exists())
        with self.assertRaises(RecoveryHelperError) as missing:
            create_delegated_recovery_snapshot(
                self.store,
                self.key,
                source_identity=self.source_identity,
                store_identity=self.store_identity,
                expected_owner_uid=os.geteuid(),
                expected_device=1,
                expected_inode=1,
            )
        self.assertEqual(missing.exception.code, "recovery_source_missing")
        self.assertFalse(PRODUCTION_DATABASE_PATH.exists())
        self.assertFalse((self.store / "head.json").exists())
        with mock.patch(
            "recovery_helper.DELEGATED_SOURCE_PATH",
            Path("relative.sqlite3"),
        ):
            with self.assertRaises(RecoveryHelperError) as relative:
                create_delegated_recovery_snapshot(
                    self.store,
                    self.key,
                    **self._delegated_values(self.source),
                )
        self.assertEqual(relative.exception.code, "recovery_path_refused")
        connection = sqlite3.connect(self.source)
        try:
            connection.execute("INSERT INTO items(value) VALUES (2)")
            connection.commit()
            connection.execute("INSERT INTO items(value) VALUES (99)")
            before = self._source_digest(self.source)
            point = self._delegated_snapshot()
            self.assertEqual(self._source_digest(self.source), before)
        finally:
            connection.close()
        self.assertEqual(point.generation, 1)
        database = self.store / "generations" / "000001" / "database.sqlite3"
        self.assertEqual(_rows(database), [(1,), (2,)])
        with database.open("rb") as handle:
            self.assertEqual(handle.read(20)[18], 1)
        self.assertFalse(list(database.parent.glob("database.sqlite3-*")))
        self.assertEqual(self._verify(), point)
        self._assert_production_absent()

    def test_delegated_identity_and_write_capability_are_refused(self):
        listed = self.source.lstat()
        before = self._source_digest(self.source)
        with mock.patch("recovery_helper.DELEGATED_SOURCE_PATH", self.source):
            with self.assertRaises(RecoveryHelperError) as owned:
                create_delegated_recovery_snapshot(
                    self.store,
                    self.key,
                    **self._delegated_values(self.source),
                )
            self.assertEqual(owned.exception.code, "recovery_owner")
            with self.assertRaises(RecoveryHelperError) as wrong_owner:
                create_delegated_recovery_snapshot(
                    self.store,
                    self.key,
                    **self._delegated_values(
                        self.source,
                        expected_owner_uid=listed.st_uid + 1,
                    ),
                )
            self.assertEqual(wrong_owner.exception.code, "recovery_owner")
            for name, value in (
                ("expected_device", listed.st_dev + 1),
                ("expected_inode", listed.st_ino + 1),
            ):
                with self.assertRaises(RecoveryHelperError) as mismatch:
                    create_delegated_recovery_snapshot(
                        self.store,
                        self.key,
                        **self._delegated_values(self.source, **{name: value}),
                    )
                self.assertEqual(mismatch.exception.code, "recovery_identity")
        with mock.patch("recovery_helper.DELEGATED_SOURCE_PATH", self.source), mock.patch(
            "recovery_helper._delegated_runtime_uid",
            return_value=listed.st_uid + 1,
        ):
            with self.assertRaises(RecoveryHelperError) as writable:
                create_delegated_recovery_snapshot(
                    self.store,
                    self.key,
                    **self._delegated_values(self.source),
                )
        self.assertEqual(writable.exception.code, "recovery_permissions")
        for bad in (None, True, -1, 1.5, "501"):
            with self.assertRaises(RecoveryHelperError) as invalid:
                create_delegated_recovery_snapshot(
                    self.store,
                    self.key,
                    **self._delegated_values(self.source, expected_owner_uid=bad),
                )
            self.assertEqual(invalid.exception.code, "recovery_identity_invalid")
        with self.assertRaises(TypeError):
            create_delegated_recovery_snapshot(
                self.store,
                self.key,
                source_identity=self.source_identity,
                store_identity=self.store_identity,
            )
        self.assertEqual(self._source_digest(self.source), before)
        self.assertFalse((self.store / "head.json").exists())
        self._assert_production_absent()

    def test_delegated_source_structure_failures_leave_the_source_in_place(self):
        listed = self.source.lstat()
        link = self.root / "source-hard-link.sqlite3"
        os.link(self.source, link)
        try:
            with mock.patch("recovery_helper.DELEGATED_SOURCE_PATH", self.source):
                with self.assertRaises(RecoveryHelperError) as linked:
                    create_delegated_recovery_snapshot(
                        self.store,
                        self.key,
                        **self._delegated_values(self.source),
                    )
            self.assertEqual(linked.exception.code, "recovery_identity")
            self.assertEqual(self.source.lstat().st_nlink, 2)
        finally:
            link.unlink()
        alias = self.root / "source-symlink.sqlite3"
        alias.symlink_to(self.source)
        with mock.patch("recovery_helper.DELEGATED_SOURCE_PATH", alias):
            with self.assertRaises(RecoveryHelperError) as symlink:
                create_delegated_recovery_snapshot(
                    self.store,
                    self.key,
                    **self._delegated_values(self.source, expected_inode=alias.lstat().st_ino),
                )
        self.assertEqual(symlink.exception.code, "recovery_symlink")
        self.assertTrue(alias.is_symlink())
        os.chmod(self.source, 0o644)
        try:
            with mock.patch("recovery_helper.DELEGATED_SOURCE_PATH", self.source):
                with self.assertRaises(RecoveryHelperError) as mode:
                    create_delegated_recovery_snapshot(
                        self.store,
                        self.key,
                        **self._delegated_values(self.source),
                    )
            self.assertEqual(mode.exception.code, "recovery_permissions")
        finally:
            os.chmod(self.source, 0o600)
        marker = Path(f"{self.source}.initialization-state")
        original = marker.read_bytes()
        marker.write_bytes(b"max-smart/sqlite-init/v1 OTHER\n")
        os.chmod(marker, 0o600)
        try:
            patches = self._as_recovery_reader(self.source)
            with patches[0], patches[1], patches[2]:
                with self.assertRaises(RecoveryHelperError) as mismatched:
                    create_delegated_recovery_snapshot(
                        self.store,
                        self.key,
                        **self._delegated_values(self.source),
                    )
            self.assertEqual(mismatched.exception.code, "recovery_marker_invalid")
        finally:
            marker.write_bytes(original)
            os.chmod(marker, 0o600)
        marker.rename(self.root / "saved-marker")
        try:
            patches = self._as_recovery_reader(self.source)
            with patches[0], patches[1], patches[2]:
                with self.assertRaises(RecoveryHelperError) as absent:
                    create_delegated_recovery_snapshot(
                        self.store,
                        self.key,
                        **self._delegated_values(self.source),
                    )
            self.assertEqual(absent.exception.code, "recovery_marker_invalid")
        finally:
            (self.root / "saved-marker").rename(marker)
            os.chmod(marker, 0o600)
        journal = Path(f"{self.source}-journal")
        journal.write_bytes(b"planted-journal")
        os.chmod(journal, 0o600)
        try:
            patches = self._as_recovery_reader(self.source)
            with patches[0], patches[1], patches[2]:
                with self.assertRaises(RecoveryHelperError) as planted:
                    create_delegated_recovery_snapshot(
                        self.store,
                        self.key,
                        **self._delegated_values(self.source),
                    )
            self.assertEqual(planted.exception.code, "recovery_sidecar_rejected")
            self.assertEqual(journal.read_bytes(), b"planted-journal")
        finally:
            journal.unlink()
        wal = Path(f"{self.source}-wal")
        shm = Path(f"{self.source}-shm")
        created_wal = False
        if not wal.exists():
            wal.write_bytes(b"")
            os.chmod(wal, 0o600)
            created_wal = True
        removed_shm = shm.exists()
        shm_bytes = shm.read_bytes() if removed_shm else None
        if removed_shm:
            shm.unlink()
        try:
            patches = self._as_recovery_reader(self.source)
            with patches[0], patches[1], patches[2]:
                with self.assertRaises(RecoveryHelperError) as missing_shm:
                    create_delegated_recovery_snapshot(
                        self.store,
                        self.key,
                        **self._delegated_values(self.source),
                    )
            self.assertEqual(missing_shm.exception.code, "recovery_sidecar_rejected")
            self.assertFalse(shm.exists())
        finally:
            if created_wal and wal.exists():
                wal.unlink()
            if removed_shm and shm_bytes is not None and not shm.exists():
                shm.write_bytes(shm_bytes)
                os.chmod(shm, 0o600)
        shm = Path(f"{self.source}-shm")
        if shm.exists():
            os.chmod(shm, 0o644)
            try:
                patches = self._as_recovery_reader(self.source)
                with patches[0], patches[1], patches[2]:
                    with self.assertRaises(RecoveryHelperError) as unsafe_shm:
                        create_delegated_recovery_snapshot(
                            self.store,
                            self.key,
                            **self._delegated_values(self.source),
                        )
                self.assertEqual(unsafe_shm.exception.code, "recovery_sidecar_rejected")
            finally:
                os.chmod(shm, 0o600)
        self.assertFalse((self.store / "head.json").exists())
        self.assertEqual(self.source.lstat().st_ino, listed.st_ino)
        self._assert_production_absent()



    def test_delegated_write_denial_covers_every_existing_artifact(self):
        artifacts = {
            "main": self.source,
            "marker": Path(f"{self.source}.initialization-state"),
            "wal": Path(f"{self.source}-wal"),
            "shm": Path(f"{self.source}-shm"),
        }
        for path in (artifacts["wal"], artifacts["shm"]):
            if not path.exists():
                path.write_bytes(b"")
                os.chmod(path, 0o600)
        for name, allowed in artifacts.items():
            with self.subTest(artifact=name):
                with mock.patch(
                    "recovery_helper.DELEGATED_SOURCE_PATH",
                    self.source,
                ), mock.patch(
                    "recovery_helper._delegated_runtime_uid",
                    return_value=self.source.lstat().st_uid + 1,
                ), mock.patch(
                    "recovery_helper.os.open",
                    side_effect=self._read_only_source_open(
                        self.source,
                        writable=(allowed,),
                    ),
                ), mock.patch(
                    "recovery_helper.sqlite3.connect",
                    side_effect=AssertionError("sqlite opened"),
                ):
                    with self.assertRaises(RecoveryHelperError) as refused:
                        create_delegated_recovery_snapshot(
                            self.store,
                            self.key,
                            **self._delegated_values(self.source),
                        )
                self.assertEqual(refused.exception.code, "recovery_permissions")
                self.assertFalse((self.store / "head.json").exists())
        self._assert_production_absent()

    def test_delegated_source_replacement_and_mutation_are_refused(self):
        real = recovery_helper._snapshot_database
        holder = sqlite3.connect(self.source)
        holder.execute("BEGIN")
        holder.execute("INSERT INTO items(value) VALUES (99)")
        self.addCleanup(holder.close)

        def replace_inode(*args, **kwargs):
            result = real(*args, **kwargs)
            replacement = self.root / "replacement.sqlite3"
            replacement.write_bytes(self.source.read_bytes())
            os.chmod(replacement, 0o600)
            os.replace(replacement, self.source)
            return result

        with mock.patch(
            "recovery_helper._snapshot_database",
            side_effect=replace_inode,
        ):
            with self.assertRaises(RecoveryHelperError) as replaced:
                self._delegated_snapshot()
        self.assertEqual(replaced.exception.code, "recovery_identity")
        self.assertTrue(self.source.exists())
        self.assertFalse((self.store / "head.json").exists())
        shutil.rmtree(self.store)
        self.store = _private(self.root / "store")
        self.source = self.root / "source.sqlite3"
        if not self.source.exists():
            self.fail("replacement removed the delegated source")

        def mutate_content(*args, **kwargs):
            result = real(*args, **kwargs)
            descriptor = os.open(
                self.source,
                os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW,
            )
            try:
                os.write(descriptor, b"x")
            finally:
                os.close(descriptor)
            return result

        with mock.patch(
            "recovery_helper._snapshot_database",
            side_effect=mutate_content,
        ):
            with self.assertRaises(RecoveryHelperError) as mutated:
                self._delegated_snapshot()
        self.assertEqual(mutated.exception.code, "recovery_identity")
        self.assertFalse((self.store / "head.json").exists())
        self.assertTrue(self.source.read_bytes().endswith(b"x"))
        self._assert_production_absent()



if __name__ == "__main__":
    unittest.main()
