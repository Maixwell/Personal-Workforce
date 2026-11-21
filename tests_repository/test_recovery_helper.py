"""Disposable tests for the isolated recovery-helper core.

These tests use private temporary databases and a simulated boundary. They do
not create an operating-system account, install a helper, generate a
production key, or open production resources.
"""

import errno
import hashlib
import hmac
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
    OS_ENFORCED_RECOVERY_ISOLATION,
    SEAL_VISIBILITY_IS_NOT_DURABILITY,
    SIMULATED_RECOVERY_BOUNDARY,
    RecoveryHelperError,
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


if __name__ == "__main__":
    unittest.main()
