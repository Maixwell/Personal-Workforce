"""Disposable proof for fixed production fence provisioning.

These tests do not create the production database, fence, pin, trust record,
or acceptance file.
"""

import errno
import inspect
import io
import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

import production_fence_provision  # noqa: E402
import provider_fence  # noqa: E402
from production_fence_provision import (  # noqa: E402
    _decision_from_response,
    main,
    provision_approved_production_fence,
)
from provider_fence import (  # noqa: E402
    PRODUCTION_FENCE_PATH,
    FenceBusy,
    FenceHeld,
    FenceSafetyError,
    ProviderFenceConfig,
    ProvisioningCompletion,
    ProvisioningReceipt,
    _APPROVED_PIN_DIRECTORY,
        _PRODUCTION_RECEIPT_CHILD,
        _PROVISION_SEAL,
        _production_child_environment,
    _receipt_bytes,
    accept_disposable_fence_receipt,
    accept_bound_disposable_fence_receipt,
    accept_production_fence_receipt,
    acquire_exclusive,
    begin_disposable_fence_provision,
    begin_production_fence_provision,
    bind_disposable_fence_directories,
    complete_disposable_fence_provision,
    prepare_disposable_fence,
    provision_bound_disposable_fence_pin,
    provision_disposable_fence_pin,
    provision_production_fence_pin,
    release_fence,
    require_production_fence_parents,
)
from provisioning_rehearsal import disposable_rehearsal_root  # noqa: E402


def _private(path):
    path.mkdir()
    os.chmod(path, 0o700)
    return path


def _snapshot(root):
    found = {}
    if root.exists():
        for path in sorted(root.rglob("*")):
            status = path.lstat()
            found[str(path)] = (
                status.st_dev,
                status.st_ino,
                stat.S_IMODE(status.st_mode),
                path.read_bytes() if stat.S_ISREG(status.st_mode) else None,
            )
    return found


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


class ProductionFenceProvisionTests(unittest.TestCase):
    def setUp(self):
        self.root = disposable_rehearsal_root()
        self.addCleanup(self._remove_root)
        self.session = PROJECT_ROOT / "data" / "current_session.json"
        self.session_before = self.session.lstat()
        self._serial = 0

    def _remove_root(self):
        import shutil

        shutil.rmtree(self.root, True)

    def _dirs(self):
        self._serial += 1
        return (
            _private(self.root / f"fence-{self._serial}"),
            _private(self.root / f"pin-{self._serial}"),
        )

    def _assert_production_absent(self):
        self.assertFalse(PRODUCTION_FENCE_PATH.exists())
        self.assertFalse((PRODUCTION_FENCE_PATH.parent / "fence.trust").exists())
        self.assertFalse((PROJECT_ROOT / "data" / "max_smart.sqlite3").exists())
        self.assertFalse(_APPROVED_PIN_DIRECTORY.exists())
        self.assertFalse(
            (_APPROVED_PIN_DIRECTORY / "provider-fence.pin").exists()
        )
        self.assertFalse(
            (_APPROVED_PIN_DIRECTORY / "provider-fence.acceptance").exists()
        )
        current = self.session.lstat()
        self.assertEqual(current.st_ino, self.session_before.st_ino)
        self.assertEqual(current.st_size, self.session_before.st_size)
        self.assertEqual(current.st_mtime_ns, self.session_before.st_mtime_ns)

    def _completion(self):
        fence, pin = self._dirs()
        process = begin_disposable_fence_provision(fence, pin)
        completion = complete_disposable_fence_provision(process)
        return fence, pin, completion

    def test_fixed_targets_reject_path_and_environment_selection(self):
        self.assertEqual(
            list(inspect.signature(provision_production_fence_pin).parameters),
            [],
        )
        self.assertEqual(
            list(inspect.signature(begin_production_fence_provision).parameters),
            ["token_bytes"],
        )
        self.assertEqual(
            list(
                inspect.signature(
                    provision_approved_production_fence
                ).parameters
            ),
            ["confirm"],
        )
        self.assertEqual(
            PRODUCTION_FENCE_PATH,
            Path("/Users/smart/Projects/Personal Workforce/data/provider.fence"),
        )
        self.assertEqual(
            _APPROVED_PIN_DIRECTORY,
            Path("/Users/smart/Library/Application Support/MaxSmartTrust"),
        )
        self.assertEqual(
            provider_fence._TRUSTED_PRODUCTION_INTERPRETER,
            Path(
                "/Library/Developer/CommandLineTools/Library/Frameworks/"
                "Python3.framework/Versions/3.9/bin/python3.9"
            ),
        )
        self.assertNotIn("fence_directory", _PRODUCTION_RECEIPT_CHILD)
        self.assertNotIn("argv[2]", _PRODUCTION_RECEIPT_CHILD)
        child_environment = _production_child_environment()
        self.assertEqual(
            set(child_environment),
            {"PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE"},
        )
        self.assertNotIn("PYTHONPATH", child_environment)
        self.assertNotIn("PYTHONSTARTUP", child_environment)
        self.assertNotIn("MAX_SMART_PROVIDER_FENCE", child_environment)
        command = (SRC_ROOT / "production_fence_provision.py").read_text(
            encoding="utf-8"
        )
        for banned in (
            "os.environ",
            "os.getenv",
            "mkdir",
            "initialize_sqlite_file",
            "publish_shared_fence_config",
            "publish_loaded_fence",
        ):
            self.assertNotIn(banned, command)
        with self.assertRaises(TypeError):
            provision_production_fence_pin(self.root)
        with self.assertRaises(TypeError):
            begin_production_fence_provision(self.root, self.root)
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as rejected:
                main([str(self.root / "fence")])
        self.assertEqual(rejected.exception.code, 2)
        self._assert_production_absent()

    def test_missing_production_parent_creates_nothing(self):
        self._assert_production_absent()
        called = {"confirm": False}

        def confirm(binding):
            called["confirm"] = True
            return dict(binding)

        with self.assertRaises(FenceSafetyError) as missing:
            provision_approved_production_fence(confirm)
        self.assertEqual(missing.exception.code, "fence_directory_missing")
        self.assertFalse(called["confirm"])
        with self.assertRaises(FenceSafetyError) as direct:
            require_production_fence_parents()
        self.assertEqual(direct.exception.code, "fence_directory_missing")
        self._assert_production_absent()

    def test_capability_separation_and_protected_paths(self):
        fence, pin = self._dirs()
        with self.assertRaises(FenceSafetyError) as production_fence:
            prepare_disposable_fence(PRODUCTION_FENCE_PATH)
        self.assertEqual(
            production_fence.exception.code,
            "production_fence_refused",
        )
        with self.assertRaises(FenceSafetyError) as production_dir:
            bind_disposable_fence_directories(PRODUCTION_FENCE_PATH.parent, pin)
        self.assertEqual(
            production_dir.exception.code,
            "production_fence_refused",
        )
        with self.assertRaises(FenceSafetyError) as approved:
            bind_disposable_fence_directories(fence, _APPROVED_PIN_DIRECTORY)
        self.assertEqual(approved.exception.code, "production_fence_refused")
        with self.assertRaises(FenceSafetyError) as data_pin:
            provision_disposable_fence_pin(PRODUCTION_FENCE_PATH.parent, pin)
        self.assertEqual(data_pin.exception.code, "production_fence_refused")
        cloud = _private(self.root / "Dropbox")
        with self.assertRaises(FenceSafetyError) as cloud_path:
            bind_disposable_fence_directories(cloud, pin)
        self.assertEqual(cloud_path.exception.code, "fence_path_refused")
        self._assert_production_absent()

    def test_forged_production_receipt_is_refused_by_both_capabilities(self):
        _fence, pin, completion = self._completion()
        called = {"confirm": False}

        def confirm(binding):
            called["confirm"] = True
            return dict(binding)

        with self.assertRaises(FenceSafetyError) as disposable_target:
            accept_production_fence_receipt(completion, confirm)
        self.assertEqual(
            disposable_target.exception.code,
            "production_fence_refused",
        )
        self.assertFalse(called["confirm"])
        observed = {
            "fence_device": 1,
            "fence_inode": 1,
            "fence_path": str(PRODUCTION_FENCE_PATH),
            "pin_device": 1,
            "pin_digest": "ab" * 32,
            "pin_inode": 1,
            "pin_path": str(_APPROVED_PIN_DIRECTORY / "provider-fence.pin"),
            "record_device": 1,
            "record_digest": "cd" * 32,
            "record_inode": 1,
            "record_path": str(PRODUCTION_FENCE_PATH.parent / "fence.trust"),
        }
        payload = _receipt_bytes(observed, "ef" * 16)
        forged = ProvisioningCompletion(
            _PROVISION_SEAL,
            ProvisioningReceipt(_PROVISION_SEAL, payload),
        )
        with self.assertRaises(FenceSafetyError) as production_target:
            accept_disposable_fence_receipt(forged, pin, confirm)
        self.assertEqual(
            production_target.exception.code,
            "production_fence_refused",
        )
        self.assertFalse(called["confirm"])
        self.assertFalse((pin / "provider-fence.acceptance").exists())
        self._assert_production_absent()

    def test_receipt_mismatch_and_confirmation_refusal(self):
        _fence, pin, completion = self._completion()
        acceptance = pin / "provider-fence.acceptance"
        called = {"confirm": False}

        def confirm(binding):
            called["confirm"] = True
            return dict(binding)

        parsed = json.loads(completion.receipt.receipt_bytes.decode("ascii"))
        pin_path = Path(parsed["pin_path"])
        original = pin_path.read_bytes()
        pin_path.write_bytes(original[:-1] + bytes([original[-1] ^ 0x01]))
        os.chmod(pin_path, 0o600)
        with self.assertRaises(FenceSafetyError) as mismatch:
            accept_disposable_fence_receipt(completion, pin, confirm)
        self.assertEqual(mismatch.exception.code, "receipt_chain_mismatch")
        self.assertFalse(called["confirm"])
        self.assertFalse(acceptance.exists())

        replacement_fence, replacement_pin, replacement = self._completion()
        del replacement_fence

        def wrong(binding):
            decision = dict(binding)
            decision["receipt_id"] = "0" * 32
            return decision

        with self.assertRaises(FenceSafetyError) as confirmation:
            accept_disposable_fence_receipt(replacement, replacement_pin, wrong)
        self.assertEqual(
            confirmation.exception.code,
            "acceptance_confirmation_refused",
        )
        with self.assertRaises(FenceSafetyError) as not_dict:
            accept_disposable_fence_receipt(
                replacement,
                replacement_pin,
                lambda _binding: "yes",
            )
        self.assertEqual(
            not_dict.exception.code,
            "acceptance_confirmation_refused",
        )
        self.assertFalse((replacement_pin / "provider-fence.acceptance").exists())
        self._assert_production_absent()

    def test_lock_covers_verification_confirmation_and_contention(self):
        fence, pin, completion = self._completion()
        parsed = json.loads(completion.receipt.receipt_bytes.decode("ascii"))
        config = ProviderFenceConfig(
            parsed["fence_path"],
            parsed["fence_device"],
            parsed["fence_inode"],
        )
        observed = {"verify": False, "confirm": False}
        real_observe = provider_fence._observe_chain

        def wrapped(path, production=False, held=None):
            gate = acquire_exclusive(config)
            observed["verify"] = type(gate) is FenceBusy
            return real_observe(path, production=production, held=held)

        def confirm(binding):
            gate = acquire_exclusive(config)
            observed["confirm"] = type(gate) is FenceBusy
            return dict(binding)

        with mock.patch("provider_fence._observe_chain", side_effect=wrapped):
            accepted = accept_disposable_fence_receipt(completion, pin, confirm)
        self.assertTrue(observed["verify"])
        self.assertTrue(observed["confirm"])
        self.assertTrue(accepted.endswith("provider-fence.acceptance"))

        busy_fence, busy_pin, busy_completion = self._completion()
        del busy_fence
        holder = subprocess.Popen(
            (sys.executable, "-B", "-c", _HOLDER, str(busy_pin / "provider-fence.pin")),
            cwd=PROJECT_ROOT,
            env={**os.environ, "PYTHONPATH": str(SRC_ROOT)},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._finish, holder)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        called = {"confirm": False}

        def blocked(binding):
            called["confirm"] = True
            return dict(binding)

        with self.assertRaises(FenceSafetyError) as busy:
            accept_disposable_fence_receipt(busy_completion, busy_pin, blocked)
        self.assertEqual(busy.exception.code, "fence_busy")
        self.assertFalse(called["confirm"])
        self.assertFalse((busy_pin / "provider-fence.acceptance").exists())
        self._assert_production_absent()

    def test_interrupted_and_partial_artifacts_are_not_retried(self):
        fence, pin = self._dirs()
        process = begin_disposable_fence_provision(fence, pin)
        process.kill()
        process.wait(timeout=5)
        with self.assertRaises(FenceSafetyError) as interrupted:
            complete_disposable_fence_provision(process)
        self.assertEqual(interrupted.exception.code, "provision_exit_failed")
        self.assertFalse((pin / "provider-fence.acceptance").exists())
        before = _snapshot(self.root)
        if any(value[3] is not None for value in before.values()):
            with self.assertRaises(FenceSafetyError):
                provision_disposable_fence_pin(fence, pin)
            self.assertEqual(_snapshot(self.root), before)

        partial_fence, partial_pin = self._dirs()
        marker = partial_fence / "provider.fence"
        marker.write_bytes(b"partial-fence\n")
        os.chmod(marker, 0o600)
        inode = marker.lstat().st_ino
        with self.assertRaises(FenceSafetyError) as partial:
            provision_disposable_fence_pin(partial_fence, partial_pin)
        self.assertEqual(partial.exception.code, "fence_exists")
        self.assertEqual(marker.read_bytes(), b"partial-fence\n")
        self.assertEqual(marker.lstat().st_ino, inode)
        self.assertFalse((partial_pin / "provider-fence.pin").exists())
        self.assertFalse((partial_pin / "provider-fence.acceptance").exists())
        self._assert_production_absent()

    def test_uncertain_release_quarantines_without_cleanup(self):
        _fence, pin, completion = self._completion()
        captured = {}
        real_release = provider_fence.release_fence

        def wrapping(held):
            captured["held"] = held
            real_close = os.close

            def ambiguous(descriptor):
                if descriptor == held.descriptor:
                    real_close(descriptor)
                    raise OSError(errno.EINTR, "close ambiguous")
                return real_close(descriptor)

            with mock.patch("provider_fence.os.close", side_effect=ambiguous):
                return real_release(held)

        with mock.patch("provider_fence.release_fence", side_effect=wrapping):
            with self.assertRaises(FenceSafetyError) as failed:
                accept_disposable_fence_receipt(
                    completion,
                    pin,
                    lambda binding: dict(binding),
                )
        self.assertEqual(failed.exception.code, "fence_release_failed")
        held = captured["held"]
        self.assertTrue(held.closure_uncertain)
        self.assertTrue(held.quarantined)
        self.assertFalse(held.released)
        acceptance = pin / "provider-fence.acceptance"
        self.assertTrue(acceptance.is_file())
        preserved = acceptance.read_bytes()
        with mock.patch("provider_fence.os.close") as later_close:
            self.assertIs(release_fence(held), held.release_failure)
        later_close.assert_not_called()
        parsed = json.loads(completion.receipt.receipt_bytes.decode("ascii"))
        self.assertIsInstance(
            acquire_exclusive(
                ProviderFenceConfig(
                    parsed["fence_path"],
                    parsed["fence_device"],
                    parsed["fence_inode"],
                )
            ),
            FenceBusy,
        )
        with self.assertRaises(FenceSafetyError) as retry:
            accept_disposable_fence_receipt(
                completion,
                pin,
                lambda binding: dict(binding),
            )
        self.assertEqual(retry.exception.code, "fence_busy")
        self.assertEqual(acceptance.read_bytes(), preserved)
        self._assert_production_absent()

    def test_parent_identity_substitution_is_refused(self):
        fence, pin = self._dirs()
        fence_binding, pin_binding = bind_disposable_fence_directories(fence, pin)
        real_create = provider_fence._create_fence_file

        def swap(candidate):
            moved = self.root / "fence-replaced"
            fence.rename(moved)
            _private(fence)
            return real_create(candidate)

        with mock.patch(
            "provider_fence._create_fence_file",
            side_effect=swap,
        ):
            with self.assertRaises(FenceSafetyError) as replaced:
                provision_bound_disposable_fence_pin(fence_binding, pin_binding)
        self.assertEqual(replaced.exception.code, "fence_directory_identity")
        self.assertFalse((fence / "provider.fence").exists())
        self.assertFalse((self.root / "fence-replaced" / "provider.fence").exists())

        acceptance_fence, acceptance_pin, completion = self._completion()
        _other, acceptance_binding = bind_disposable_fence_directories(
            acceptance_fence,
            acceptance_pin,
        )
        real_acceptance = provider_fence._create_acceptance_file

        def swap_pin(path, payload):
            moved = self.root / "pin-replaced"
            acceptance_pin.rename(moved)
            _private(acceptance_pin)
            return real_acceptance(path, payload)

        with mock.patch(
            "provider_fence._create_acceptance_file",
            side_effect=swap_pin,
        ):
            with self.assertRaises(FenceSafetyError) as acceptance:
                accept_bound_disposable_fence_receipt(
                    acceptance_binding,
                    completion,
                    lambda binding: dict(binding),
                )
        self.assertEqual(acceptance.exception.code, "fence_directory_identity")
        self.assertFalse((acceptance_pin / "provider-fence.acceptance").exists())
        self.assertFalse(
            (self.root / "pin-replaced" / "provider-fence.acceptance").exists()
        )
        self.assertTrue((self.root / "pin-replaced" / "provider-fence.pin").is_file())
        self._assert_production_absent()

    def test_mode_symlink_and_filesystem_identity_are_refused(self):
        fence, pin = self._dirs()
        loose = self.root / "loose"
        loose.mkdir()
        os.chmod(loose, 0o755)
        with self.assertRaises(FenceSafetyError) as mode:
            bind_disposable_fence_directories(loose, pin)
        self.assertEqual(mode.exception.code, "fence_directory_permissions")
        linked = self.root / "linked"
        linked.symlink_to(fence)
        with self.assertRaises(FenceSafetyError) as symlink:
            bind_disposable_fence_directories(linked, pin)
        self.assertEqual(symlink.exception.code, "fence_symlink")
        with mock.patch("provider_fence._directory_is_local", return_value=False):
            with self.assertRaises(FenceSafetyError) as filesystem:
                bind_disposable_fence_directories(fence, pin)
        self.assertEqual(filesystem.exception.code, "fence_filesystem")
        sample = {
            "fence_device": 7,
            "fence_inode": 11,
            "fence_path": "/private/tmp/fence",
            "pin_digest": "ab" * 32,
            "pin_inode": 13,
            "pin_path": "/private/tmp/pin",
            "receipt_id": "cd" * 16,
        }
        self.assertEqual(
            _decision_from_response(
                json.dumps(sample, separators=(",", ":"), sort_keys=True)
            ),
            sample,
        )
        self.assertEqual(_decision_from_response("not-json"), {})
        self._assert_production_absent()

    def test_isolated_child_rejects_shadow_module_and_startup(self):
        shadow = _private(self.root / "shadow")
        shadow_module = shadow / "provider_fence.py"
        shadow_module.write_text(
            "import pathlib\n"
            "pathlib.Path(%s).write_text('shadow')\n"
            "raise SystemExit(91)\n" % repr(str(self.root / "shadow-ran")),
            encoding="utf-8",
        )
        startup = self.root / "startup.py"
        startup.write_text(
            "import pathlib\n"
            "pathlib.Path(%s).write_text('startup')\n" % repr(
                str(self.root / "startup-ran")
            ),
            encoding="utf-8",
        )
        hostile = {
            "PYTHONPATH": str(shadow),
            "PYTHONSTARTUP": str(startup),
            "PYTHONSAFEPATH": "0",
            "PYTHONNOUSERSITE": "0",
        }
        trusted = Path(provider_fence.__file__).resolve()
        for cwd in (shadow, SRC_ROOT):
            process = provider_fence._isolated_provider_process(
                "print(provider_fence.__file__)\n",
                [],
                cwd,
                environment=hostile,
            )
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertEqual(Path(stdout.decode().strip()).resolve(), trusted)
        self.assertFalse((self.root / "shadow-ran").exists())
        self.assertFalse((self.root / "startup-ran").exists())

        with mock.patch("provider_fence.subprocess.Popen") as popen:
            popen.return_value = mock.Mock()
            process = begin_production_fence_provision(
                token_bytes=lambda count: b"\x11" * count
            )
        provider_fence._PROVISION_LAUNCHES.pop(process, None)
        argv = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        self.assertEqual(argv[1:5], ["-I", "-S", "-B", "-c"])
        self.assertEqual(argv[6], "11" * 16)
        self.assertEqual(len(argv), 7)
        launched = Path(argv[0])
        self.assertFalse(launched.is_symlink())
        self.assertEqual(launched, provider_fence._TRUSTED_PRODUCTION_INTERPRETER)
        self.assertNotEqual(launched, Path(sys.executable).resolve())
        self.assertIn("sys.flags.no_site", argv[5])
        self.assertNotIn("sys.flags.safe_path", argv[5])
        self.assertIn(str(trusted), argv[5])
        self.assertNotIn("argv[2]", argv[5])
        self.assertEqual(kwargs["cwd"], str(SRC_ROOT))
        self.assertNotIn("PYTHONPATH", kwargs["env"])
        self.assertNotIn("PYTHONSTARTUP", kwargs["env"])
        self.assertNotIn("MAX_SMART_PROVIDER_FENCE", kwargs["env"])
        self._assert_production_absent()

    def test_interpreter_symlink_substitution_cannot_redirect_execution(self):
        real = Path(sys.executable).resolve()
        link = self.root / "python-link"
        link.symlink_to(real)
        os.chmod(link, 0o755, follow_symlinks=False)
        marker = self.root / "evil-ran"
        evil = self.root / "evil-interpreter"
        quoted = str(marker).replace("'", "'\\''")
        evil.write_text(
            "#!/bin/sh\nprintf x > '%s'\nexit 91\n" % quoted,
            encoding="utf-8",
        )
        os.chmod(evil, 0o755)
        real_popen = subprocess.Popen

        def swap(argv, **kwargs):
            link.unlink()
            link.symlink_to(evil)
            return real_popen(argv, **kwargs)

        with mock.patch.object(sys, "executable", str(link)):
            with mock.patch(
                "provider_fence.subprocess.Popen",
                side_effect=swap,
            ):
                process = provider_fence._isolated_provider_process(
                    "import sys\nprint(sys.executable)\nprint(sys.version.split()[0])\n",
                    [],
                    SRC_ROOT,
                )
        stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, stderr.decode())
        launched, version = stdout.decode().splitlines()
        self.assertEqual(
            Path(launched),
            provider_fence._TRUSTED_PRODUCTION_INTERPRETER,
        )
        self.assertTrue(version.startswith("3.9."))
        self.assertFalse(marker.exists())
        self._assert_production_absent()

    def test_source_replacement_cannot_execute_substituted_source(self):
        original = Path(provider_fence.__file__).resolve()
        copy = Path(self.root).resolve() / "provider_fence.py"
        copy.write_bytes(original.read_bytes())
        os.chmod(copy, 0o644)
        marker = self.root / "substituted-ran"
        real_popen = subprocess.Popen

        def swap(argv, **kwargs):
            copy.write_text(
                "import pathlib\n"
                "pathlib.Path(%s).write_text('substituted')\n"
                "raise SystemExit(91)\n" % repr(str(marker)),
                encoding="utf-8",
            )
            os.chmod(copy, 0o644)
            return real_popen(argv, **kwargs)

        with mock.patch.object(provider_fence, "__file__", str(copy)):
            with mock.patch(
                "provider_fence.subprocess.Popen",
                side_effect=swap,
            ):
                process = provider_fence._isolated_provider_process(
                    "print('verified-source')\n",
                    [],
                    SRC_ROOT,
                )
        stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, stderr.decode())
        self.assertEqual(stdout.decode().strip(), "verified-source")
        self.assertFalse(marker.exists())
        self._assert_production_absent()

    def test_verified_child_executes_disposable_provision(self):
        fence, pin = self._dirs()
        suffix = (
            "receipt = provider_fence.provision_disposable_fence_receipt(%r, %r)\n"
            "sys.stdout.buffer.write(receipt.receipt_bytes)\n"
        ) % (str(fence), str(pin))
        process = provider_fence._isolated_provider_process(
            suffix,
            [],
            SRC_ROOT,
        )
        stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, stderr.decode())
        parsed = json.loads(stdout.decode("ascii"))
        self.assertEqual(parsed["fence_path"], str(fence / "provider.fence"))
        self.assertTrue((fence / "provider.fence").is_file())
        self.assertTrue((fence / "fence.trust").is_file())
        self.assertTrue((pin / "provider-fence.pin").is_file())
        self._assert_production_absent()

    def test_retained_descriptor_rejects_substituted_parent(self):
        fence, pin = self._dirs()
        fence_binding, pin_binding = bind_disposable_fence_directories(
            fence,
            pin,
        )
        provision_bound_disposable_fence_pin(fence_binding, pin_binding)
        self.assertEqual(fence.lstat().st_ino, fence_binding.inode)
        self.assertEqual(pin.lstat().st_ino, pin_binding.inode)
        self.assertTrue((fence / "provider.fence").is_file())
        self.assertTrue((fence / "fence.trust").is_file())
        self.assertTrue((pin / "provider-fence.pin").is_file())
        with self.assertRaises(FenceSafetyError) as existing:
            provision_bound_disposable_fence_pin(fence_binding, pin_binding)
        self.assertEqual(existing.exception.code, "trust_record_exists")
        self.assertTrue((fence / "fence.trust").is_file())

        race_fence, race_pin = self._dirs()
        self._assert_swap_does_not_touch_replacement(
            race_fence,
            "provider.fence",
            provider_fence._RETAINED_FENCE_DIRECTORY,
            lambda: provision_bound_disposable_fence_pin(
                *bind_disposable_fence_directories(race_fence, race_pin)
            ),
        )
        trust_fence, trust_pin = self._dirs()
        self._assert_swap_does_not_touch_replacement(
            trust_fence,
            "fence.trust",
            provider_fence._RETAINED_FENCE_DIRECTORY,
            lambda: provision_bound_disposable_fence_pin(
                *bind_disposable_fence_directories(trust_fence, trust_pin)
            ),
        )
        pin_fence, pin_directory = self._dirs()
        self._assert_swap_does_not_touch_replacement(
            pin_directory,
            "provider-fence.pin",
            provider_fence._RETAINED_PIN_DIRECTORY,
            lambda: provision_bound_disposable_fence_pin(
                *bind_disposable_fence_directories(pin_fence, pin_directory)
            ),
        )

        acceptance_fence, acceptance_pin, completion = self._completion()
        _ignored, acceptance_binding = bind_disposable_fence_directories(
            acceptance_fence,
            acceptance_pin,
        )
        accepted = accept_bound_disposable_fence_receipt(
            acceptance_binding,
            completion,
            lambda binding: dict(binding),
        )
        self.assertTrue(accepted.endswith("provider-fence.acceptance"))
        second_fence, second_pin, second_completion = self._completion()
        _ignored, second_binding = bind_disposable_fence_directories(
            second_fence,
            second_pin,
        )
        self._assert_swap_does_not_touch_replacement(
            second_pin,
            "provider-fence.acceptance",
            provider_fence._RETAINED_PIN_DIRECTORY,
            lambda: accept_bound_disposable_fence_receipt(
                second_binding,
                second_completion,
                lambda decision: dict(decision),
            ),
        )
        self._assert_production_absent()

    def _assert_swap_does_not_touch_replacement(
        self,
        directory,
        name,
        retained,
        operation,
    ):
        moved = self.root / f"{directory.name}-validated"
        real_open = os.open
        state = {"done": False, "descriptor": None}

        def spy(path, flags, mode=0o777, *, dir_fd=None):
            if path == name and flags & os.O_CREAT and not state["done"]:
                state["done"] = True
                state["descriptor"] = retained.get()
                if dir_fd is None or dir_fd != state["descriptor"]:
                    raise AssertionError(name)
                directory.rename(moved)
                _private(directory)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        validated_inode = directory.lstat().st_ino
        with mock.patch("os.open", side_effect=spy):
            with self.assertRaises(FenceSafetyError) as refused:
                operation()
        self.assertEqual(refused.exception.code, "fence_directory_identity")
        self.assertTrue(state["done"])
        self.assertEqual(list(directory.iterdir()), [])
        self.assertNotEqual(directory.lstat().st_ino, validated_inode)
        self.assertEqual(moved.lstat().st_ino, validated_inode)
        self.assertTrue((moved / name).is_file())
        self.assertEqual(type(state["descriptor"]), int)

    def test_fence_directory_fsync_precedes_trust_record(self):
        fence, pin = self._dirs()
        bindings = bind_disposable_fence_directories(fence, pin)
        real_fsync = os.fsync
        directory_syncs = {"count": 0}

        def fail_directory(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                directory_syncs["count"] += 1
                raise OSError(errno.EIO, "directory fsync")
            return real_fsync(descriptor)

        with mock.patch("os.fsync", side_effect=fail_directory):
            with self.assertRaises(FenceSafetyError) as failed:
                provision_bound_disposable_fence_pin(*bindings)
        self.assertEqual(failed.exception.code, "fence_durability_uncertain")
        self.assertEqual(directory_syncs["count"], 1)
        self.assertTrue((fence / "provider.fence").is_file())
        self.assertFalse((fence / "fence.trust").exists())
        self.assertFalse((pin / "provider-fence.pin").exists())
        with self.assertRaises(FenceSafetyError) as retry:
            provision_bound_disposable_fence_pin(*bindings)
        self.assertEqual(retry.exception.code, "fence_exists")
        self.assertFalse((fence / "fence.trust").exists())
        self.assertFalse((pin / "provider-fence.pin").exists())
        self._assert_production_absent()

    def test_command_line_tools_child_suppresses_sitecustomize(self):
        process = provider_fence._isolated_provider_process(
            "import sys\n"
            "print(sys.executable)\n"
            "print(sys.version.split()[0])\n"
            "print(int(sys.flags.isolated))\n"
            "print(int(sys.flags.no_user_site))\n"
            "print(int(sys.flags.no_site))\n"
            "print(int(sys.flags.ignore_environment))\n"
            "print('sitecustomize' in sys.modules)\n"
            "print('usercustomize' in sys.modules)\n"
            "print(hasattr(sys.flags, 'safe_path'))\n"
            "error = provider_fence.FenceSafetyError('sample')\n"
            "provider_fence._note_failure(error, 'child note')\n"
            "sys.stdout.write(error.__notes__[0] + '\\n')\n",
            [],
            SRC_ROOT,
        )
        stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, stderr.decode())
        lines = stdout.decode().splitlines()
        self.assertEqual(
            Path(lines[0]),
            provider_fence._TRUSTED_PRODUCTION_INTERPRETER,
        )
        self.assertTrue(lines[1].startswith("3.9."))
        self.assertEqual(lines[2:6], ["1", "1", "1", "1"])
        self.assertEqual(lines[6:8], ["False", "False"])
        self.assertEqual(lines[8], "False")
        self.assertEqual(lines[9], "child note")
        stdlib = (
            provider_fence._TRUSTED_PRODUCTION_INTERPRETER.parent.parent
            / "lib"
            / "python3.9"
        )
        self.assertFalse((stdlib / "sitecustomize.py").exists())
        self.assertFalse((stdlib / "usercustomize.py").exists())
        self._assert_production_absent()

    def test_unapproved_interpreter_identity_is_refused(self):
        with self.assertRaises(FenceSafetyError) as stub:
            provider_fence._require_approved_interpreter(Path("/usr/bin/python3"))
        self.assertEqual(stub.exception.code, "fence_interpreter_invalid")
        with self.assertRaises(FenceSafetyError) as homebrew:
            provider_fence._require_approved_interpreter(Path(sys.executable).resolve())
        self.assertEqual(homebrew.exception.code, "fence_interpreter_invalid")
        approved = provider_fence._require_approved_interpreter(
            provider_fence._TRUSTED_PRODUCTION_INTERPRETER
        )
        self.assertEqual(approved, provider_fence._TRUSTED_PRODUCTION_INTERPRETER)
        self._assert_production_absent()

    def test_symlinked_runtime_directory_is_refused(self):
        real = _private(self.root / "real-runtime")
        link = self.root / "runtime-link"
        link.symlink_to(real, target_is_directory=True)
        binary = real / "python3.9"
        binary.write_bytes(b"x")
        os.chmod(binary, 0o755)
        with self.assertRaises(FenceSafetyError) as linked_directory:
            provider_fence._trusted_status(link, "dir")
        self.assertEqual(linked_directory.exception.code, "fence_interpreter_invalid")
        with self.assertRaises(FenceSafetyError) as linked:
            provider_fence._trusted_ancestry(link / "python3.9", "file", executable=True)
        self.assertEqual(linked.exception.code, "fence_interpreter_invalid")
        self._assert_production_absent()

    def test_writable_ancestry_and_unsafe_permissions_are_refused(self):
        tree = _private(self.root / "owned-runtime")
        binary = tree / "python3.9"
        binary.write_bytes(b"#!/bin/sh\n")
        os.chmod(binary, 0o755)
        with self.assertRaises(FenceSafetyError) as owned:
            provider_fence._trusted_ancestry(binary, "file", executable=True)
        self.assertEqual(owned.exception.code, "fence_interpreter_invalid")
        loose = _private(self.root / "loose-parent")
        os.chmod(loose, 0o777)
        loose_binary = loose / "python3.9"
        loose_binary.write_bytes(b"x")
        os.chmod(loose_binary, 0o755)
        with self.assertRaises(FenceSafetyError) as writable:
            provider_fence._trusted_ancestry(
                loose_binary,
                "file",
                executable=True,
            )
        self.assertEqual(writable.exception.code, "fence_interpreter_invalid")

        class Status:
            st_mode = stat.S_IFREG | 0o775
            st_uid = 0
            st_dev = 1
            st_ino = 2

        pretend = self.root / "pretend-runtime"
        pretend.write_bytes(b"x")
        with mock.patch("provider_fence.os.lstat", return_value=Status()):
            with mock.patch("provider_fence.os.access", return_value=False):
                with self.assertRaises(FenceSafetyError) as group_write:
                    provider_fence._trusted_status(
                        pretend,
                        "file",
                        executable=True,
                    )
        self.assertEqual(group_write.exception.code, "fence_interpreter_invalid")
        Status.st_mode = stat.S_IFREG | 0o555
        Status.st_uid = os.geteuid()
        with mock.patch("provider_fence.os.lstat", return_value=Status()):
            with mock.patch("provider_fence.os.access", return_value=False):
                with self.assertRaises(FenceSafetyError) as account_owned:
                    provider_fence._trusted_status(
                        pretend,
                        "file",
                        executable=True,
                    )
        self.assertEqual(account_owned.exception.code, "fence_interpreter_invalid")
        Status.st_mode = stat.S_IFREG | 0o755
        Status.st_uid = 0
        with mock.patch("provider_fence.os.lstat", return_value=Status()):
            with mock.patch("provider_fence.os.access", return_value=True):
                with self.assertRaises(FenceSafetyError) as access_write:
                    provider_fence._trusted_status(
                        pretend,
                        "file",
                        executable=True,
                    )
        self.assertEqual(access_write.exception.code, "fence_interpreter_invalid")
        self._assert_production_absent()

    def test_runtime_directory_identity_change_is_refused(self):
        (
            path,
            device,
            inode,
            interpreter_ancestry,
            stdlib_ancestry,
        ) = provider_fence._validate_production_runtime()
        self.assertEqual(path, provider_fence._TRUSTED_PRODUCTION_INTERPRETER)
        changed = list(stdlib_ancestry)
        entry = changed[0]
        changed[0] = (entry[0], entry[1], entry[2] + 1)
        with self.assertRaises(FenceSafetyError) as identity:
            provider_fence._recheck_production_runtime(
                path,
                device,
                inode,
                interpreter_ancestry,
                tuple(changed),
            )
        self.assertEqual(identity.exception.code, "fence_interpreter_invalid")
        self._assert_production_absent()

    def test_missing_trusted_runtime_does_not_fall_back(self):
        marker = self.root / "fallback-ran"
        evil = self.root / "evil-python"
        quoted = str(marker).replace("'", "'\\''")
        evil.write_text(
            "#!/bin/sh\nprintf x > '%s'\nexit 91\n" % quoted,
            encoding="utf-8",
        )
        os.chmod(evil, 0o755)
        missing = self.root / "bin" / "python3.9"
        with mock.patch.object(sys, "executable", str(evil)):
            with mock.patch.object(
                provider_fence,
                "_TRUSTED_PRODUCTION_INTERPRETER",
                missing,
            ):
                with mock.patch("provider_fence.subprocess.Popen") as popen:
                    with self.assertRaises(FenceSafetyError) as refused:
                        provider_fence._isolated_provider_process(
                            "print('fallback')\n",
                            [],
                            SRC_ROOT,
                        )
        self.assertEqual(refused.exception.code, "fence_interpreter_invalid")
        popen.assert_not_called()
        self.assertFalse(marker.exists())
        self._assert_production_absent()

    def test_missing_stdlib_dependency_is_refused(self):
        required = provider_fence._REQUIRED_TRUSTED_STDLIB + (
            "no-such-runtime-dependency.py",
        )
        with mock.patch.object(
            provider_fence,
            "_REQUIRED_TRUSTED_STDLIB",
            required,
        ):
            with self.assertRaises(FenceSafetyError) as missing:
                provider_fence._validate_production_runtime()
            self.assertEqual(missing.exception.code, "fence_interpreter_invalid")
            with mock.patch("provider_fence.subprocess.Popen") as popen:
                with self.assertRaises(FenceSafetyError):
                    provider_fence._isolated_provider_process(
                        "print('missing')\n",
                        [],
                        SRC_ROOT,
                    )
            popen.assert_not_called()
        self._assert_production_absent()

    def _finish(self, process):
        if process.poll() is None:
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.write("\n")
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()


if __name__ == "__main__":
    unittest.main()
