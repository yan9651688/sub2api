import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import archive_maintenance as archive
import deploy


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project, self.maintenance = self.root / "project", self.root / "maintenance"
        self.transaction = "20260905T120000Z-123456abcdef"
        self.records = self.project / ".sub2api-maintenance" / "transactions" / self.transaction
        self.records.mkdir(parents=True)
        self.patches = self.maintenance / "patches"
        self.patches.mkdir(parents=True)
        patch_data = b"synthetic non-secret patch fixture\n"
        (self.patches / "change.patch").write_bytes(patch_data)
        entries = [{"file": "change.patch", "sha256": deploy.sha256(patch_data)}]
        (self.patches / "series.json").write_bytes(deploy.canonical({"schema_version": 1, "patches": entries}))
        self.release = {"schema_version": 1, "image": "sha256:" + "a" * 64,
                        "revision": "b" * 40, "migrations_sha256": "c" * 64}
        state = self.project / ".sub2api-maintenance"
        (state / "current-manifest.json").write_bytes(deploy.canonical(self.release))
        self.release_path = self.maintenance / "release.json"
        self.release_path.write_bytes(deploy.canonical({**self.release, "patches": entries, "tests": "passed"}))
        self.record = {"schema_version": 1, "id": self.transaction, "project": str(self.project),
                       "service": "sub2api", "status": "succeeded", "current": self.release,
                       "target": self.release, "previous_override": None}
        self.write_record()
        (self.project / "docker-compose.availability.yml").write_bytes(
            deploy.canonical(deploy.Deployment.override_value(self.release["image"])))
        (self.project / ".env").write_text("TOKEN=SENSITIVE_FIXTURE_DO_NOT_ARCHIVE\n")
        (self.project / "application.log").write_text("SENSITIVE_FIXTURE_DO_NOT_ARCHIVE\n")

    def write_record(self):
        data = deploy.canonical(self.record)
        (self.records / "record.json").write_bytes(data)
        (self.records / "record.sha256").write_text(deploy.sha256(data) + "\n")

    def collect(self):
        return archive.collect(self.project, self.maintenance, self.release_path)

    def test_preview_excludes_credentials_logs_and_unrelated_files(self):
        before = sorted(str(path) for path in self.root.rglob("*"))
        members = self.collect()
        self.assertEqual(before, sorted(str(path) for path in self.root.rglob("*")))
        self.assertFalse(any(".env" in name or "application.log" in name for name in members))
        self.assertFalse(any(b"SENSITIVE_FIXTURE" in data for data in members.values()))
        self.assertIn("maintenance/release.json", members)
        self.assertIn("maintenance/patches/change.patch", members)

    def test_archive_inventory_and_checksum_match_exact_bytes(self):
        members = self.collect()
        output = self.root / "metadata.tar.gz"
        checksum = archive.write_archive(output, members)
        self.assertEqual(checksum, deploy.file_hash(output))
        with tarfile.open(output) as reader:
            self.assertEqual(sorted(reader.getnames()), sorted(members))
            for entry in reader.getmembers():
                self.assertEqual(entry.mode, 0o600)
                self.assertEqual(reader.extractfile(entry).read(), members[entry.name])
        inventory = json.loads(members["ARCHIVE_MANIFEST.json"])
        for name, entry in inventory["files"].items():
            self.assertEqual(entry["sha256"], deploy.sha256(members[name]))

    def test_validated_schema_policy_can_be_archived(self):
        self.record["schema_compatibility"] = "identical"
        self.write_record()
        self.assertTrue(self.collect())

    def test_schema_policy_rejects_unexpected_content(self):
        self.record["schema_compatibility"] = {"token": "SENSITIVE_FIXTURE"}
        self.write_record()
        with self.assertRaisesRegex(deploy.DeploymentError, "schema compatibility metadata"):
            self.collect()

    def test_archive_never_overwrites_existing_output(self):
        output = self.root / "already-exists.tar.gz"
        output.write_bytes(b"preserve")
        with self.assertRaisesRegex(deploy.DeploymentError, "already exists"):
            archive.write_archive(output, self.collect())
        self.assertEqual(output.read_bytes(), b"preserve")

    def test_transaction_checksum_mismatch_is_rejected(self):
        (self.records / "record.sha256").write_text("0" * 64)
        with self.assertRaisesRegex(deploy.DeploymentError, "Transaction checksum mismatch"):
            self.collect()

    def test_override_with_extra_environment_is_rejected(self):
        data = deploy.Deployment.override_value(self.release["image"])
        data["services"]["sub2api"]["environment"]["PASSWORD"] = "SENSITIVE_FIXTURE"
        (self.project / "docker-compose.availability.yml").write_bytes(deploy.canonical(data))
        with self.assertRaisesRegex(deploy.DeploymentError, "unexpected settings"):
            self.collect()

    def test_mismatched_release_is_rejected(self):
        value = json.loads(self.release_path.read_text())
        value["image"] = "sha256:" + "f" * 64
        self.release_path.write_bytes(deploy.canonical(value))
        with self.assertRaisesRegex(deploy.DeploymentError, "does not match"):
            self.collect()

    def test_transaction_nested_release_secret_is_rejected(self):
        self.record["current"] = {**self.release, "api_key": "SENSITIVE_FIXTURE"}
        self.write_record()
        with self.assertRaisesRegex(deploy.DeploymentError, "Unexpected transaction release fields"):
            self.collect()


if __name__ == "__main__":
    unittest.main()
