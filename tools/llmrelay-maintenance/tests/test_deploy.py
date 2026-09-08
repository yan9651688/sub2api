import base64
import contextlib
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deploy


OLD = {"schema_version": 1, "image": "example/sub2api@sha256:" + "1" * 64,
       "revision": "a" * 40, "migrations_sha256": "f" * 64}
NEW = {"schema_version": 1, "image": "example/custom@sha256:" + "2" * 64,
       "revision": "b" * 40, "migrations_sha256": "f" * 64}
OLD_ID = "sha256:" + "3" * 64
NEW_ID = "sha256:" + "4" * 64
CONTAINER_ID = "5" * 64


class FakeDocker:
    def __init__(self, project):
        self.project = project
        self.current = OLD_ID
        self.calls = []
        self.new_healthy = True
        self.old_healthy = True
        self.backup_failure = False
        self.corrupt_backup = False
        self.target_missing = False
        self.target_bad_label = False
        self.install_failure = False
        self.modified_binary = False
        self.extra_compose_file = False
        self.environment_value = "original"
        self.environment_change_during_backup = False
        self.now = 0.0
        self.running = True

    def configured(self):
        override = self.project / "docker-compose.availability.yml"
        path = override if override.exists() else self.project / "docker-compose.override.yml"
        return json.loads(path.read_text())["services"]["sub2api"]["image"]

    def backup(self):
        if self.backup_failure:
            raise deploy.DeploymentError("Command failed (exit 1): bash")
        snapshot = self.project / "backups" / "snapshot-test"
        snapshot.mkdir(parents=True)
        lines = []
        for filename in ("database.dump", "runtime.tar.gz"):
            data = b"synthetic backup fixture"
            (snapshot / filename).write_bytes(data)
            digest = "0" * 64 if self.corrupt_backup else hashlib.sha256(data).hexdigest()
            lines.append(digest + "  " + filename)
        (snapshot / "SHA256SUMS").write_text("\n".join(lines) + "\n")
        (snapshot / "database-contents.txt").write_text("synthetic archive listing\n")
        if self.environment_change_during_backup:
            self.environment_value = "changed"

    def __call__(self, args, *, cwd, timeout=60, check=True):
        self.calls.append(args)
        stdout = ""
        code = 0
        if args[:2] == ["docker", "compose"]:
            if "config" in args:
                stdout = json.dumps({"services": {"sub2api": {"image": self.configured(),
                                                              "environment": {"DATABASE_HOST": self.environment_value}}}})
            elif "ps" in args:
                stdout = CONTAINER_ID + "\n" if self.running or "--all" in args else ""
            elif "up" in args:
                self.running = True
                if self.configured() == NEW["image"]:
                    if self.install_failure:
                        raise deploy.DeploymentError("Command failed (exit 1): docker")
                    self.current = NEW_ID
                else:
                    self.current = OLD_ID
            else:
                raise AssertionError(args)
        elif args[:3] == ["docker", "image", "inspect"]:
            old = args[-1] == OLD["image"]
            if not old and self.target_missing:
                code = 1
            elif "--format" in args:
                stdout = (OLD_ID if old else NEW_ID) + "\n"
            else:
                release = OLD if old else NEW
                labels = {deploy.REVISION_LABEL: release["revision"]}
                if not old:
                    labels[deploy.MIGRATIONS_LABEL] = "0" * 64 if self.target_bad_label else release["migrations_sha256"]
                stdout = json.dumps([{"Id": OLD_ID if old else NEW_ID,
                                      "Config": {"Labels": labels,
                                                 "Healthcheck": {"Test": ["CMD", "healthcheck"]}}}])
        elif args[:2] == ["docker", "inspect"]:
            healthy = self.new_healthy if self.current == NEW_ID else self.old_healthy
            compose_files = [self.project / "docker-compose.yml", self.project / "docker-compose.override.yml"]
            if (self.project / "docker-compose.availability.yml").exists():
                compose_files.append(self.project / "docker-compose.availability.yml")
            if self.extra_compose_file:
                compose_files.append(self.project / "unexpected.yml")
            stdout = json.dumps({"image": self.current, "running": self.running,
                                 "health": "healthy" if healthy else "unhealthy",
                                 "compose_files": ",".join(str(path) for path in compose_files),
                                 "compose_working_dir": str(self.project), "compose_service": "sub2api"})
        elif args[:2] == ["docker", "diff"]:
            stdout = "C /app\nC /app/data\nC /app/sub2api\n" if self.modified_binary else "C /app\nC /app/data\n"
        elif args[:2] == ["docker", "pull"]:
            self.target_missing = False
        elif args[0] == "bash":
            self.backup()
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, code, stdout, "")

    def sleep(self, seconds):
        self.now += seconds


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / "docker-compose.yml").write_text('{"services":{"sub2api":{}}}')
        (self.project / "docker-compose.override.yml").write_bytes(
            deploy.canonical({"services": {"sub2api": {"image": OLD["image"]}}}))
        (self.project / "backup.sh").write_text("#!/bin/sh\nexit 0\n")
        self.docker = FakeDocker(self.project)
        self.manager = deploy.Deployment(self.project, runner=self.docker, health_timeout=4,
                                         poll_interval=2, clock=lambda: self.docker.now,
                                         sleep=self.docker.sleep)

        @contextlib.contextmanager
        def fake_lock():
            self.manager.state.mkdir(exist_ok=True)
            yield

        patcher = mock.patch.object(self.manager, "lock", fake_lock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def snapshot(self):
        return {str(path.relative_to(self.project)): path.read_bytes()
                for path in self.project.rglob("*") if path.is_file()}

    def records(self):
        return [json.loads(p.read_text()) for p in self.manager.state.glob("transactions/*/record.json")]

    def installs(self):
        return [args for args in self.docker.calls if args[:2] == ["docker", "compose"] and "up" in args]

    def test_dry_run_has_no_writes_or_mutating_commands(self):
        before = self.snapshot()
        with mock.patch("deploy.atomic_write", side_effect=AssertionError("Unexpected write")):
            result = self.manager.upgrade(OLD, NEW)
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.manager.state.exists())
        self.assertFalse(self.installs())
        self.assertFalse(any(args[0] == "bash" or args[:2] == ["docker", "pull"] for args in self.docker.calls))

    def test_dry_run_missing_target_reports_pull_without_pulling(self):
        self.docker.target_missing = True
        result = self.manager.upgrade(OLD, NEW)
        self.assertTrue(result["target_pull_required"])
        self.assertTrue(self.docker.target_missing)
        self.assertFalse(self.manager.state.exists())

    def test_backup_failure_never_changes_application(self):
        self.docker.backup_failure = True
        with self.assertRaisesRegex(deploy.DeploymentError, "failed_before_switch"):
            self.manager.upgrade(OLD, NEW, apply=True)
        self.assertFalse(self.installs())
        self.assertFalse(self.manager.override.exists())
        self.assertEqual(self.docker.current, OLD_ID)
        self.assertEqual(self.records()[0]["status"], "failed_before_switch")

    def test_corrupt_backup_never_changes_application(self):
        self.docker.corrupt_backup = True
        with self.assertRaisesRegex(deploy.DeploymentError, "failed_before_switch"):
            self.manager.upgrade(OLD, NEW, apply=True)
        self.assertFalse(self.installs())
        self.assertIn("checksum mismatch", self.records()[0]["error"])

    def test_success_deploys_only_sub2api_with_feature_flag(self):
        base_before = {path: path.read_bytes() for path in self.manager.files}
        result = self.manager.upgrade(OLD, NEW, apply=True)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.docker.current, NEW_ID)
        self.assertEqual(len(self.installs()), 1)
        command = self.installs()[0]
        for flag in ("--no-deps", "--no-build", "--pull", "never", "--force-recreate"):
            self.assertIn(flag, command)
        self.assertEqual(command[-1], "sub2api")
        self.assertNotIn("postgres", command)
        self.assertNotIn("redis", command)
        settings = json.loads(self.manager.override.read_text())["services"]["sub2api"]
        self.assertEqual(settings["environment"], {deploy.FEATURE_FLAG: "true"})
        self.assertEqual({path: path.read_bytes() for path in self.manager.files}, base_before)
        self.assertEqual(json.loads((self.manager.state / "current-manifest.json").read_text()), NEW)

    def test_missing_target_is_pulled_by_digest_before_backup(self):
        self.docker.target_missing = True
        self.manager.upgrade(OLD, NEW, apply=True)
        pull = next(i for i, a in enumerate(self.docker.calls) if a[:2] == ["docker", "pull"])
        backup = next(i for i, a in enumerate(self.docker.calls) if a[0] == "bash")
        self.assertLess(pull, backup)
        self.assertEqual(self.docker.calls[pull][-1], NEW["image"])

    def test_unhealthy_target_restores_exact_original_pin_and_flag(self):
        self.docker.new_healthy = False
        with self.assertRaisesRegex(deploy.DeploymentError, "status=rolled_back"):
            self.manager.upgrade(OLD, NEW, apply=True)
        self.assertEqual(self.docker.current, OLD_ID)
        self.assertEqual(len(self.installs()), 2)
        self.assertFalse(self.manager.override.exists())
        self.assertEqual(self.records()[0]["status"], "rolled_back")

    def test_install_command_failure_also_restores_old_application(self):
        self.docker.install_failure = True
        with self.assertRaisesRegex(deploy.DeploymentError, "status=rolled_back"):
            self.manager.upgrade(OLD, NEW, apply=True)
        self.assertEqual(self.docker.current, OLD_ID)
        self.assertFalse(self.manager.override.exists())

    def test_keyboard_interrupt_after_switch_restores_old_application(self):
        original = self.manager.wait_healthy

        def wait(image_id):
            if image_id == NEW_ID:
                raise KeyboardInterrupt()
            return original(image_id)

        with mock.patch.object(self.manager, "wait_healthy", side_effect=wait):
            with self.assertRaisesRegex(deploy.DeploymentError, "status=rolled_back"):
                self.manager.upgrade(OLD, NEW, apply=True)
        self.assertEqual(self.docker.current, OLD_ID)
        self.assertFalse(self.manager.override.exists())

    def test_failed_recovery_is_bounded_and_retains_evidence(self):
        original_wait = self.manager.wait_healthy

        def wait(image_id):
            self.docker.new_healthy = False
            self.docker.old_healthy = False
            return original_wait(image_id)

        with mock.patch.object(self.manager, "wait_healthy", side_effect=wait):
            with self.assertRaisesRegex(deploy.DeploymentError, "status=rollback_failed"):
                self.manager.upgrade(OLD, NEW, apply=True)
        self.assertEqual(len(self.installs()), 2)
        self.assertEqual(self.records()[0]["status"], "rollback_failed")
        self.assertIn("rollback_error", self.records()[0])

    def test_health_commands_share_the_remaining_deadline(self):
        self.docker.current = NEW_ID
        original = self.manager.run
        budgets = []

        def run(args, **kwargs):
            budgets.append(kwargs["timeout"])
            result = original(args, **kwargs)
            if "ps" in args:
                self.docker.now += 3
            return result

        with mock.patch.object(self.manager, "run", side_effect=run):
            self.manager.wait_healthy(NEW_ID)
        self.assertEqual(budgets, [4, 1])

    def test_schema_difference_rejected_before_any_io(self):
        target = dict(NEW, migrations_sha256="e" * 64)
        for apply in (False, True):
            with self.assertRaisesRegex(deploy.DeploymentError, "Migration fingerprints differ"):
                self.manager.upgrade(OLD, target, apply=apply)
        self.assertEqual(self.docker.calls, [])
        self.assertFalse(self.manager.state.exists())

    def test_mutable_image_tag_is_rejected(self):
        with self.assertRaisesRegex(deploy.DeploymentError, "Invalid manifest field: image"):
            self.manager.upgrade(OLD, dict(NEW, image="example/custom:latest"), apply=True)
        self.assertEqual(self.docker.calls, [])

    def test_target_label_mismatch_prevents_backup_and_switch(self):
        self.docker.target_bad_label = True
        with self.assertRaisesRegex(deploy.DeploymentError, "failed_before_switch"):
            self.manager.upgrade(OLD, NEW, apply=True)
        self.assertFalse(self.installs())
        self.assertFalse((self.project / "backups").exists())

    def test_current_container_mismatch_prevents_any_write(self):
        self.docker.current = NEW_ID
        with self.assertRaisesRegex(deploy.DeploymentError, "Current container does not match"):
            self.manager.upgrade(OLD, NEW)
        self.assertFalse(self.manager.state.exists())

    def test_in_app_binary_update_is_rejected_even_when_image_id_matches(self):
        self.docker.modified_binary = True
        with self.assertRaisesRegex(deploy.DeploymentError, "Container binary differs from its image"):
            self.manager.upgrade(OLD, NEW)
        self.assertFalse(self.manager.state.exists())

    def test_untracked_compose_file_is_rejected(self):
        self.docker.extra_compose_file = True
        with self.assertRaisesRegex(deploy.DeploymentError, "Container Compose labels do not match"):
            self.manager.upgrade(OLD, NEW)
        self.assertFalse(self.manager.state.exists())

    def test_environment_drift_during_backup_aborts_without_recreation(self):
        self.docker.environment_change_during_backup = True
        with self.assertRaisesRegex(deploy.DeploymentError, "failed_before_switch"):
            self.manager.upgrade(OLD, NEW, apply=True)
        self.assertFalse(self.installs())
        self.assertIn("Resolved configuration changed", self.records()[0]["error"])

    def test_current_compose_mutable_tag_is_rejected(self):
        self.manager.files[1].write_bytes(deploy.canonical({"services": {"sub2api": {"image": "example:latest"}}}))
        with self.assertRaisesRegex(deploy.DeploymentError, "Current Compose image must already"):
            self.manager.upgrade(OLD, NEW)
        self.assertFalse(self.manager.state.exists())

    def test_manual_rollback_dry_run_and_apply(self):
        result = self.manager.upgrade(OLD, NEW, apply=True)
        before = self.snapshot()
        calls_before = len(self.installs())
        preview = self.manager.rollback(result["transaction"])
        self.assertEqual(preview["mode"], "dry-run")
        self.assertEqual(before, self.snapshot())
        self.assertEqual(len(self.installs()), calls_before)
        reverted = self.manager.rollback(result["transaction"], apply=True)
        self.assertEqual(reverted["status"], "manual_rolled_back")
        self.assertEqual(self.docker.current, OLD_ID)
        self.assertFalse(self.manager.override.exists())

    def test_stopped_target_container_can_be_manually_rolled_back(self):
        result = self.manager.upgrade(OLD, NEW, apply=True)
        self.docker.running = False
        preview = self.manager.rollback(result["transaction"])
        self.assertEqual(preview["mode"], "dry-run")
        reverted = self.manager.rollback(result["transaction"], apply=True)
        self.assertEqual(reverted["status"], "manual_rolled_back")
        self.assertEqual(self.docker.current, OLD_ID)
        self.assertTrue(self.docker.running)

    def test_rollback_restores_existing_override_byte_for_byte(self):
        previous = b'{"services": {"sub2api": {"image": "' + OLD["image"].encode() + b'"}}}\n'
        self.manager.override.write_bytes(previous)
        result = self.manager.upgrade(OLD, NEW, apply=True)
        self.manager.rollback(result["transaction"], apply=True)
        self.assertEqual(self.manager.override.read_bytes(), previous)

    def test_tampered_rollback_metadata_is_rejected(self):
        result = self.manager.upgrade(OLD, NEW, apply=True)
        record_path = self.manager.transaction_path / "record.json"
        record_path.write_bytes(record_path.read_bytes() + b" ")
        before = len(self.installs())
        with self.assertRaisesRegex(deploy.DeploymentError, "metadata checksum mismatch"):
            self.manager.rollback(result["transaction"], apply=True)
        self.assertEqual(before, len(self.installs()))

    def test_rollback_rejects_cross_project_record_even_with_valid_checksum(self):
        result = self.manager.upgrade(OLD, NEW, apply=True)
        record = self.records()[0]
        record["project"] = str(self.project / "other")
        self.manager.save_record(record)
        with self.assertRaisesRegex(deploy.DeploymentError, "does not match this deployment"):
            self.manager.rollback(result["transaction"], apply=True)
        self.assertEqual(len(self.installs()), 1)

    def test_rollback_rejects_configuration_drift(self):
        result = self.manager.upgrade(OLD, NEW, apply=True)
        self.manager.files[0].write_text("changed externally")
        with self.assertRaisesRegex(deploy.DeploymentError, "Compose configuration changed"):
            self.manager.rollback(result["transaction"], apply=True)
        self.assertEqual(len(self.installs()), 1)

    def test_rollback_rejects_environment_drift_without_logging_values(self):
        result = self.manager.upgrade(OLD, NEW, apply=True)
        self.docker.environment_value = "SENSITIVE_VALUE_MUST_NOT_APPEAR"
        with self.assertRaisesRegex(deploy.DeploymentError, "Resolved configuration changed"):
            self.manager.rollback(result["transaction"], apply=True)
        self.assertEqual(len(self.installs()), 1)
        self.assertNotIn("SENSITIVE_VALUE_MUST_NOT_APPEAR", json.dumps(self.records()))

    def test_rollback_rejects_unexpected_previous_override_settings(self):
        self.manager.override.write_bytes(deploy.canonical({"services": {"sub2api": {"image": OLD["image"]}}}))
        result = self.manager.upgrade(OLD, NEW, apply=True)
        record = self.records()[0]
        data = deploy.canonical({"services": {"postgres": {"image": OLD["image"]}}})
        record["previous_override"] = {"base64": base64.b64encode(data).decode(), "sha256": deploy.sha256(data)}
        self.manager.save_record(record)
        with self.assertRaisesRegex(deploy.DeploymentError, "Invalid previous override"):
            self.manager.rollback(result["transaction"], apply=True)
        self.assertEqual(len(self.installs()), 1)

    def test_rollback_rejects_wrong_previous_image_before_writing_pin(self):
        self.manager.override.write_bytes(deploy.canonical({"services": {"sub2api": {"image": OLD["image"]}}}))
        result = self.manager.upgrade(OLD, NEW, apply=True)
        record = self.records()[0]
        data = deploy.canonical({"services": {"sub2api": {"image": NEW["image"]}}})
        record["previous_override"] = {"base64": base64.b64encode(data).decode(), "sha256": deploy.sha256(data)}
        self.manager.save_record(record)
        before = self.manager.override.read_bytes()
        with self.assertRaisesRegex(deploy.DeploymentError, "Previous override image differs"):
            self.manager.rollback(result["transaction"], apply=True)
        self.assertEqual(before, self.manager.override.read_bytes())
        self.assertEqual(len(self.installs()), 1)

    def test_transaction_path_traversal_is_rejected(self):
        with self.assertRaisesRegex(deploy.DeploymentError, "Invalid transaction ID"):
            self.manager.rollback("../../other")
        self.assertFalse(self.manager.state.exists())


if __name__ == "__main__":
    unittest.main()
