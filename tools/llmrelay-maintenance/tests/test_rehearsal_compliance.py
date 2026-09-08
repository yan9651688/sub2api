"""Synthetic setup data is writable only in a generated rehearsal database."""

import copy
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import rehearse


class RehearsalComplianceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.project = "s2rehearsal-012345abcdef"
        self.container_id = "c" * 64
        self.volume_name = self.project + "_postgres-data"
        self.source = "/var/lib/docker/volumes/" + self.volume_name + "/_data"
        rehearse.prepare_project(self.directory, self.project, {"image": "sha256:" + "a" * 64},
                                  SimpleNamespace(postgres_image="pg:test", redis_image="redis:test", fixture_image="python:test"))
        self.container = {
            "Id": self.container_id, "State": {"Running": True},
            "Config": {"Labels": {"com.docker.compose.project": self.project,
                                   "com.docker.compose.service": "postgres"},
                       "Env": ["POSTGRES_DB=rehearsal", "POSTGRES_USER=rehearsal", "PGDATA=/var/lib/postgresql/data/pgdata"]},
            "Mounts": [{"Destination": "/var/lib/postgresql/data", "Type": "volume", "RW": True,
                        "Name": self.volume_name, "Source": self.source}],
        }
        self.volume = {"Name": self.volume_name, "Driver": "local", "Options": None, "Mountpoint": self.source,
                       "Labels": {"com.docker.compose.project": self.project, "com.docker.compose.volume": "postgres-data"}}
        self.calls = []

        def run(args):
            self.calls.append(args)
            if args == ["compose", "ps", "-q", "postgres"]:
                return SimpleNamespace(stdout=self.container_id)
            if args == ["docker", "container", "inspect", self.container_id]:
                return SimpleNamespace(stdout=json.dumps([self.container]))
            if args == ["docker", "volume", "inspect", self.volume_name]:
                return SimpleNamespace(stdout=json.dumps([self.volume]))
            if args[:3] == ["docker", "exec", self.container_id]:
                return SimpleNamespace(stdout="fixture-initialized\n")
            self.fail("Unexpected Docker operation: " + repr(args))

        self.manager = SimpleNamespace(project=self.directory, compose=lambda args: ["compose"] + args, run=run)

    def initialize(self):
        with patch.object(rehearse, "request", side_effect=[
            {"version": "v2026.06.10", "required": True},
            {"version": "v2026.06.10", "required": False},
        ]) as api:
            result = rehearse.initialize_compliance_fixture(self.manager, "http://172.19.0.3:8080", "fixture-token")
            self.assertEqual([call.args[1] for call in api.call_args_list], ["/api/v1/admin/compliance"] * 2)
            self.assertTrue(all("payload" not in call.kwargs for call in api.call_args_list))
            return result

    def test_writes_only_version_and_synthetic_marker_with_single_admin_guards(self):
        self.assertEqual(self.initialize(), {"version": "v2026.06.10", "test_fixture": True})
        writes = [call for call in self.calls if call[:2] == ["docker", "exec"]]
        self.assertEqual(len(writes), 1)
        command = writes[0]
        self.assertEqual(command[:4], ["docker", "exec", self.container_id, "psql"])
        self.assertIn("--set=ON_ERROR_STOP=1", command)
        self.assertEqual(command[command.index("-h") + 1], "/var/run/postgresql")
        self.assertEqual(command[command.index("-d") + 1], "rehearsal")
        self.assertEqual(command[command.index("-U") + 1], "rehearsal")
        sql = command[-1]
        payload = json.loads(re.search(r"id::text, '(\{[^']+\})', NOW\(\)", sql).group(1))
        self.assertEqual(payload, {"version": "v2026.06.10", "test_fixture": True})
        self.assertIn("INSERT INTO settings (key, value, updated_at)", sql)
        self.assertIn("email = 'rehearsal@example.test' AND role = 'admin'", sql)
        self.assertIn("SELECT count(*) INTO affected", sql)
        self.assertEqual(sql.count("IF affected <> 1"), 2)
        self.assertIn("ON CONFLICT (key) DO UPDATE", sql)
        self.assertIn("current_database() <> 'rehearsal' OR current_user <> 'rehearsal'", sql)
        self.assertNotIn("accepted_at", sql)
        self.assertNotIn("created_at", sql)

    def test_rejects_wrong_container_database_and_mount_before_sql(self):
        original = copy.deepcopy(self.container)
        mutations = [
            lambda c: c["Config"]["Labels"].update({"com.docker.compose.project": "production"}),
            lambda c: c["Config"]["Labels"].update({"com.docker.compose.service": "sub2api"}),
            lambda c: c["Config"].update(Env=["POSTGRES_DB=production", "POSTGRES_USER=rehearsal"]),
            lambda c: c["Config"]["Env"].append("POSTGRES_USER=production"),
            lambda c: c["Mounts"][0].update(Type="bind"),
            lambda c: c["Mounts"][0].update(Name="production_postgres-data"),
        ]
        for mutate in mutations:
            self.container = copy.deepcopy(original)
            mutate(self.container)
            with self.assertRaises(RuntimeError):
                self.initialize()
        self.assertFalse(any(call[:2] == ["docker", "exec"] for call in self.calls))

    def test_rejects_foreign_or_external_volume_before_sql(self):
        original = copy.deepcopy(self.volume)
        for updates in ({"Labels": {"com.docker.compose.project": "production"}},
                        {"Options": {"type": "nfs", "device": ":/production"}}, {"Driver": "external"}):
            self.volume = {**copy.deepcopy(original), **updates}
            with self.assertRaises(RuntimeError):
                self.initialize()
        self.assertFalse(any(call[:2] == ["docker", "exec"] for call in self.calls))

    def test_rejects_wrong_marker_and_external_compose_volume(self):
        marker = self.directory / ".rehearsal.json"
        marker.write_text(json.dumps({"project": "production", "directory": str(self.directory)}))
        with self.assertRaises(RuntimeError):
            self.initialize()
        marker.write_text(json.dumps({"project": self.project, "directory": str(self.directory)}))
        compose_path = self.directory / "docker-compose.yml"
        compose = json.loads(compose_path.read_text())
        compose["volumes"]["postgres-data"] = {"external": True}
        compose_path.write_text(json.dumps(compose))
        with self.assertRaises(RuntimeError):
            self.initialize()
        self.assertFalse(any(call[:2] == ["docker", "exec"] for call in self.calls))

    def test_rejects_untrusted_version_before_any_docker_command(self):
        with patch.object(rehearse, "request", return_value={"version": "v'; DROP TABLE users;--"}):
            with self.assertRaises(RuntimeError):
                rehearse.initialize_compliance_fixture(self.manager, "http://fixture", "fixture-token")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
