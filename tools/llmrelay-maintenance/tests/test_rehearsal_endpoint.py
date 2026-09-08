"""The host may reach only its isolated rehearsal container's bridge address."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import rehearse


class RehearsalEndpointTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.project = "s2rehearsal-012345abcdef"
        self.container_id = "c" * 64
        self.network_id = "d" * 64
        self.network_name = self.project + "_default"
        self.marker = {"project": self.project, "directory": str(self.directory)}
        self.marker_path = self.directory / ".rehearsal.json"
        self.marker_path.write_text(json.dumps(self.marker), encoding="utf-8")
        self.ids = self.container_id
        self.container = {
            "Id": self.container_id, "State": {"Running": True},
            "Config": {"Labels": {"com.docker.compose.project": self.project,
                                   "com.docker.compose.service": "sub2api"}},
            "HostConfig": {"NetworkMode": self.network_name},
            "NetworkSettings": {"Networks": {self.network_name: {
                "NetworkID": self.network_id, "IPAddress": "172.19.0.3"}}},
        }
        self.network = {
            "Id": self.network_id, "Name": self.network_name, "Driver": "bridge", "Internal": True,
            "Labels": {"com.docker.compose.project": self.project},
            "IPAM": {"Config": [{"Subnet": "172.19.0.0/16"}]},
            "Containers": {self.container_id: {"IPv4Address": "172.19.0.3/16"}},
        }

        def run(args):
            if args == ["compose", "ps", "-q", self.container["Config"]["Labels"]["com.docker.compose.service"]]:
                return SimpleNamespace(stdout=self.ids)
            if args == ["docker", "container", "inspect", self.container_id]:
                return SimpleNamespace(stdout=json.dumps([self.container]))
            if args == ["docker", "network", "inspect", self.network_id]:
                return SimpleNamespace(stdout=json.dumps([self.network]))
            # Foreign service labels must be rejected after Compose locates it.
            if args[:3] == ["compose", "ps", "-q"]:
                return SimpleNamespace(stdout=self.ids)
            self.fail("Unexpected Docker operation: " + repr(args))

        self.manager = SimpleNamespace(project=self.directory, compose=lambda args: ["compose"] + args, run=run)

    def test_accepts_known_services_on_the_dedicated_internal_network(self):
        self.assertEqual(rehearse.endpoint(self.manager, "sub2api", 8080), "http://172.19.0.3:8080")
        self.container["Config"]["Labels"]["com.docker.compose.service"] = "fake"
        self.assertEqual(rehearse.endpoint(self.manager, "fake", 8090), "http://172.19.0.3:8090")

    def test_rejects_unknown_pair_invalid_marker_and_multiple_containers(self):
        for service, port in (("postgres", 5432), ("fake", 8080), ("sub2api", 8080.0)):
            with self.subTest(service=service, port=port), self.assertRaises(RuntimeError):
                rehearse.endpoint(self.manager, service, port)
        for changes in ({"project": "production"}, {"directory": str(self.directory / "other")}):
            self.marker_path.write_text(json.dumps({**self.marker, **changes}), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                rehearse.endpoint(self.manager, "sub2api", 8080)
        self.marker_path.write_text(json.dumps(self.marker), encoding="utf-8")
        for ids in ("", self.container_id + "\n" + "e" * 64):
            self.ids = ids
            with self.assertRaises(RuntimeError):
                rehearse.endpoint(self.manager, "sub2api", 8080)

    def test_rejects_foreign_or_stopped_container(self):
        for label in ("com.docker.compose.project", "com.docker.compose.service"):
            original = self.container["Config"]["Labels"][label]
            self.container["Config"]["Labels"][label] = "production"
            with self.assertRaises(RuntimeError):
                rehearse.endpoint(self.manager, "sub2api", 8080)
            self.container["Config"]["Labels"][label] = original
        self.container["State"]["Running"] = False
        with self.assertRaises(RuntimeError):
            rehearse.endpoint(self.manager, "sub2api", 8080)

    def test_rejects_public_host_foreign_and_multiple_networks(self):
        for key, value in (("Internal", False), ("Driver", "host"), ("Labels", {"com.docker.compose.project": "production"})):
            original = self.network[key]
            self.network[key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                rehearse.endpoint(self.manager, "sub2api", 8080)
            self.network[key] = original
        self.container["HostConfig"]["NetworkMode"] = "host"
        with self.assertRaises(RuntimeError):
            rehearse.endpoint(self.manager, "sub2api", 8080)
        self.container["HostConfig"]["NetworkMode"] = self.network_name
        self.container["NetworkSettings"]["Networks"]["second"] = {}
        with self.assertRaises(RuntimeError):
            rehearse.endpoint(self.manager, "sub2api", 8080)

    def test_rejects_external_loopback_out_of_subnet_and_unverified_membership(self):
        attachment = self.container["NetworkSettings"]["Networks"][self.network_name]
        for address in ("8.8.8.8", "127.0.0.1", "172.20.0.3", "172.19.0.0", "172.19.255.255", "::1"):
            attachment["IPAddress"] = address
            with self.subTest(address=address), self.assertRaises(RuntimeError):
                rehearse.endpoint(self.manager, "sub2api", 8080)
        attachment["IPAddress"] = "172.19.0.3"
        self.network["Containers"][self.container_id]["IPv4Address"] = "172.19.0.4/16"
        with self.assertRaises(RuntimeError):
            rehearse.endpoint(self.manager, "sub2api", 8080)

    def test_generated_project_has_no_published_ports(self):
        args = SimpleNamespace(postgres_image="postgres:fixture", redis_image="redis:fixture", fixture_image="python:fixture")
        rehearse.prepare_project(self.directory, self.project, {"image": "sha256:" + "a" * 64}, args)
        compose = json.loads((self.directory / "docker-compose.yml").read_text())
        self.assertTrue(compose["networks"]["default"]["internal"])
        for service in compose["services"].values():
            self.assertNotIn("ports", service)


if __name__ == "__main__":
    unittest.main()
