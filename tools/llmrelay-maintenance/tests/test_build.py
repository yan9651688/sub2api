import hashlib
import contextlib
import io
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

import build


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def schema(self):
        for name in ("backend/migrations/001_init.sql", "backend/ent/schema/account.go",
                     "backend/internal/repository/migrations_runner.go"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name, encoding="utf-8")

    def test_schema_hash_detects_content_names_and_execution_changes(self):
        self.schema()
        original = build.migration_hash(self.root)
        runner = self.root / "backend/internal/repository/migrations_runner.go"
        runner.write_text("changed")
        self.assertNotEqual(original, build.migration_hash(self.root))
        runner.write_text("backend/internal/repository/migrations_runner.go")
        sql = self.root / "backend/migrations/001_init.sql"
        sql.rename(sql.with_name("002_init.sql"))
        self.assertNotEqual(original, build.migration_hash(self.root))

    def test_schema_hash_ignores_tests(self):
        self.schema()
        original = build.migration_hash(self.root)
        (self.root / "backend/migrations/example_test.go").write_text("test")
        self.assertEqual(original, build.migration_hash(self.root))

    def test_patch_checksum_mismatch_stops_before_git(self):
        folder = self.root / "patches"
        folder.mkdir()
        (folder / "fix.patch").write_bytes(b"modified")
        (folder / "series.json").write_text(json.dumps({"schema_version": 1, "patches": [
            {"file": "fix.patch", "sha256": hashlib.sha256(b"original").hexdigest()}]}))
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            build.patch_set(self.root)

    def test_existing_worktree_is_never_reused(self):
        with patch.object(build, "run") as run:
            with self.assertRaisesRegex(ValueError, "must not exist"):
                build.prepare(self.root, "a" * 40, self.root, "go", self.root / "out")
            run.assert_not_called()

    def test_floating_revision_is_rejected(self):
        with patch.object(build, "run") as run:
            with self.assertRaisesRegex(ValueError, "exact"):
                build.prepare(self.root, "latest", self.root / "new", "go", self.root / "out")
            run.assert_not_called()

    def fake_git(self, args, cwd=None, capture=False):
        if "rev-parse" in args:
            return "a" * 40
        if "worktree" in args:
            (Path(args[-2]) / "backend").mkdir(parents=True)
        return None

    def test_second_patch_conflict_prevents_tests_and_image(self):
        patches = [self.root / "one.patch", self.root / "two.patch"]
        calls = []

        def git(args, cwd=None, capture=False):
            calls.append(args)
            if "--check" in args and args[-1] == patches[1]:
                raise subprocess.CalledProcessError(1, args)
            return self.fake_git(args, cwd, capture)

        with patch.object(build, "patch_set", return_value=({"patches": []}, patches)), \
                patch.object(build, "migration_hash", return_value="b" * 64), \
                patch.object(build, "run", side_effect=git), \
                patch.object(build.subprocess, "run") as process:
            with self.assertRaises(subprocess.CalledProcessError):
                build.prepare(self.root, "a" * 40, self.root / "checkout", "go", self.root / "out")
            process.assert_not_called()
            self.assertFalse(any(args[:2] == ["git", "apply"] and "--check" not in args
                                 and args[-1] == patches[1] for args in calls))

    def test_failed_go_tests_never_build_or_emit_release(self):
        output = self.root / "out"
        argv = ["build.py", "--source", str(self.root), "--revision", "a" * 40,
                "--worktree", str(self.root / "checkout"), "--output", str(output),
                "--image-tag", "local/sub2api:test"]
        with patch.object(build, "patch_set", return_value=({"patches": []}, [])), \
                patch.object(build, "migration_hash", return_value="b" * 64), \
                patch.object(build, "run", side_effect=self.fake_git), \
                patch.object(build.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)), \
                patch.object(build, "build_image") as image, patch("sys.argv", argv):
            self.assertEqual(1, build.main())
            image.assert_not_called()
        self.assertTrue((output / "go-test.log").exists())
        self.assertFalse((output / "release.json").exists())
        self.assertFalse((output / "test-report.json").exists())

    def image_fixture(self):
        version_file = self.root / "backend/cmd/server/VERSION"
        version_file.parent.mkdir(parents=True)
        version_file.write_text("0.1.185\n")
        output = self.root / "out"
        output.mkdir()
        report = {"revision": "a" * 40, "migrations_sha256": "b" * 64, "patches": []}
        return output, report

    def test_explicit_version_overrides_source_and_matches_release(self):
        output, report = self.image_fixture()
        with patch.object(build.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as process, \
                patch.object(build, "run", return_value="sha256:" + "c" * 64):
            result = build.build_image(self.root, "local/sub2api:test", report, output,
                                       version="0.2.0-availability.1")
        self.assertIn("VERSION=0.2.0-availability.1", process.call_args.args[0])
        self.assertEqual(result["version"], "0.2.0-availability.1")
        self.assertEqual(json.loads((output / "release.json").read_text())["version"], result["version"])
        self.assertEqual((self.root / "backend/cmd/server/VERSION").read_text(), "0.1.185\n")

    def test_omitted_version_requires_explicit_custom_release_name(self):
        with patch.object(build.subprocess, "run") as process:
            with self.assertRaisesRegex(ValueError, "explicit --version"):
                build.build_image(self.root, "local/sub2api:test", {}, self.root)
        process.assert_not_called()

    def test_invalid_versions_are_rejected_before_docker(self):
        output, report = self.image_fixture()
        for version in ("", "latest", "v0.2.0", "0.2.0 extra", "0.2.0\n", "0.2.0;echo",
                        "0.2.0$HOME", "0.2.0$(id)", "0.2.0/extra"):
            with self.subTest(version=version), patch.object(build.subprocess, "run") as process:
                with self.assertRaisesRegex(ValueError, "Version must start"):
                    build.build_image(self.root, "local/sub2api:test", report, output, version=version)
                process.assert_not_called()
        self.assertFalse((output / "release.json").exists())

    def test_cli_version_requires_image_tag_before_preparation(self):
        argv = ["build.py", "--source", str(self.root), "--revision", "a" * 40,
                "--worktree", str(self.root / "checkout"), "--output", str(self.root / "out"),
                "--version", "0.2.0-availability.1"]
        with patch("sys.argv", argv), patch.object(build, "prepare") as prepare, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                build.main()
        self.assertEqual(stopped.exception.code, 2)
        prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
