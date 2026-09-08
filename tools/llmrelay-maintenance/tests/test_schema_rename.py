import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deploy


OLD = {"revision": deploy.REVIEWED_RENAME_TRANSITION[0],
       "migrations_sha256": deploy.REVIEWED_RENAME_TRANSITION[1]}
NEW = {"revision": deploy.REVIEWED_RENAME_TRANSITION[2],
       "migrations_sha256": deploy.REVIEWED_RENAME_TRANSITION[3]}


def state(renamed=False, **changes):
    value = {"old_column": not renamed, "new_column": renamed,
             "checksum": deploy.RENAME_CHECKSUM if renamed else None,
             "unexpected_migrations": 0, "enabled_groups": 0}
    return dict(value, **changes)


class SchemaRenameTests(unittest.TestCase):
    def test_only_exact_reviewed_release_pair_is_allowed(self):
        self.assertEqual(deploy.Deployment.compatible(OLD, NEW), "0.2.1-to-0.2.2-guarded-column-rename")
        for key in ("revision", "migrations_sha256"):
            altered = dict(NEW, **{key: "0" * len(NEW[key])})
            with self.assertRaises(deploy.DeploymentError):
                deploy.Deployment.compatible(OLD, altered)
        with self.assertRaises(deploy.DeploymentError):
            deploy.Deployment.compatible(NEW, OLD)

    def test_verified_lf_checkout_uses_the_same_guarded_reverse_migration(self):
        for fingerprint in deploy.REVIEWED_RENAME_TARGET_HASHES:
            target = dict(NEW, migrations_sha256=fingerprint)
            self.assertTrue(deploy.is_reviewed_rename(OLD, target))
            self.assertEqual(deploy.Deployment.compatible(OLD, target), "0.2.1-to-0.2.2-guarded-column-rename")

    def test_forward_requires_original_schema_and_inactive_lists(self):
        self.assertEqual(deploy.validate_rename_state(state()), "original")
        for value in (state(True), state(enabled_groups=1), state(checksum="bad"),
                      state(new_column=True), state(old_column=False), state(unexpected_migrations=1)):
            with self.subTest(value=value), self.assertRaises(deploy.DeploymentError):
                deploy.validate_rename_state(value)

    def test_reverse_accepts_already_original_or_exact_applied_migration(self):
        self.assertEqual(deploy.validate_rename_state(state(), rollback=True), "original")
        self.assertEqual(deploy.validate_rename_state(state(True), rollback=True), "renamed")
        for value in (state(True, checksum="wrong"), state(True, enabled_groups=1),
                      state(True, unexpected_migrations=1), state(True, old_column=True)):
            with self.subTest(value=value), self.assertRaises(deploy.DeploymentError):
                deploy.validate_rename_state(value, rollback=True)

    def test_app_stops_before_reverse_sql_and_state_is_rechecked(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = deploy.Deployment(directory)
            manager.rename_database_state = mock.Mock(side_effect=[state(True), state()])
            manager.rename_database_command = mock.Mock(return_value=["psql-fixture"])
            manager.run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
            manager.reverse_reviewed_rename({"current": OLD, "target": NEW})
            commands = [call.args[0] for call in manager.run.call_args_list]
            self.assertIn("stop", commands[0])
            self.assertEqual(commands[0][-1], "sub2api")
            self.assertIn("running", commands[1])
            self.assertEqual(commands[2][0], "psql-fixture")
            self.assertEqual(manager.rename_database_state.call_count, 2)

    def test_invalid_state_never_stops_application(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = deploy.Deployment(directory)
            manager.rename_database_state = mock.Mock(return_value=state(True, checksum="wrong"))
            manager.run = mock.Mock()
            with self.assertRaises(deploy.DeploymentError):
                manager.reverse_reviewed_rename({"current": OLD, "target": NEW})
            manager.run.assert_not_called()

    def test_running_application_prevents_sql(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = deploy.Deployment(directory)
            manager.rename_database_state = mock.Mock(return_value=state(True))
            manager.rename_database_command = mock.Mock()
            manager.run = mock.Mock(side_effect=[subprocess.CompletedProcess([], 0, stdout=""),
                                                subprocess.CompletedProcess([], 0, stdout="still-running")])
            with self.assertRaisesRegex(deploy.DeploymentError, "still running"):
                manager.reverse_reviewed_rename({"current": OLD, "target": NEW})
            manager.rename_database_command.assert_not_called()

    def test_external_database_is_never_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = deploy.Deployment(directory)
            manager.configuration = mock.Mock(return_value={"services": {
                "postgres": {}, "sub2api": {"environment": {"DATABASE_HOST": "external.example",
                    "DATABASE_USER": "test", "DATABASE_DBNAME": "test"}}}})
            metadata = {"name": "/fixture-postgres", "running": True, "service": "postgres",
                        "directory": str(manager.project), "networks": {"test": {"Aliases": ["postgres"]}}}
            manager.run = mock.Mock(side_effect=[subprocess.CompletedProcess([], 0, stdout="a" * 64),
                subprocess.CompletedProcess([], 0, stdout=json.dumps(metadata))])
            with self.assertRaisesRegex(deploy.DeploymentError, "outside"):
                manager.rename_database_command()


if __name__ == "__main__":
    unittest.main()
