#!/usr/bin/env python3
"""Exercise immutable images and deploy.py in a new, isolated Linux Compose project.

Requires loaded current/target images. Creates no connections to production data
or external model providers. Only fixture image pulls use the host's network.
"""

import argparse
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import deploy


HERE = Path(__file__).resolve().parent
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class NoFrontendRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Rehearsal assets must come from this isolated application instance.
        return None


FRONTEND_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoFrontendRedirect())


class FrontendReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []
        self.has_app = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.has_app = self.has_app or attrs.get("id") == "app"
        if tag == "script" and attrs.get("src"):
            self.references.append(attrs["src"])
        if tag == "link" and set((attrs.get("rel") or "").lower().split()) & {"stylesheet", "modulepreload", "preload"}:
            if attrs.get("href"):
                self.references.append(attrs["href"])
        if tag == "base":
            raise RuntimeError("Unexpected base URL in rehearsal frontend")


def frontend_path(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise RuntimeError("Invalid frontend asset path")
    parts = urllib.parse.urlsplit(value)
    if parts.scheme or parts.netloc or value.startswith("//"):
        raise RuntimeError("Frontend assets must use local paths")
    path = urllib.parse.unquote(parts.path).lstrip("/")
    if not path or any(part in ("", ".", "..") for part in path.split("/")) or "\\" in path or "\x00" in path:
        raise RuntimeError("Invalid frontend asset path")
    return path


def load_frontend_manifest(path):
    data = path.read_bytes()
    value = json.loads(data)
    if not isinstance(value, dict) or not isinstance(value.get("files"), list):
        raise RuntimeError("Frontend manifest must contain a files list")
    files = {}
    for entry in value["files"]:
        if not isinstance(entry, dict):
            raise RuntimeError("Invalid frontend manifest entry")
        name = entry.get("path")
        if not isinstance(name, str) or name.startswith("/") or "?" in name or "#" in name or "%" in name:
            raise RuntimeError("Frontend manifest paths must be relative to dist")
        if name.startswith("dist/"):
            name = name[5:]
        name = frontend_path(name)
        if (name in files or not isinstance(entry.get("sha256"), str) or
                not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) or
                type(entry.get("size")) is not int or entry["size"] < 0):
            raise RuntimeError("Invalid or duplicate frontend manifest entry")
        files[name] = {"sha256": entry["sha256"], "size": entry["size"]}
    if "index.html" not in files:
        raise RuntimeError("Frontend manifest is missing index.html")
    return {"sha256": hashlib.sha256(data).hexdigest(), "files": files}


def frontend_get(base, path, limit):
    req = urllib.request.Request(base + path, headers={"Accept-Encoding": "identity"})
    try:
        with FRONTEND_HTTP.open(req, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError("Frontend did not return HTTP 200: " + path)
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise RuntimeError("Frontend ignored identity encoding: " + path)
            body = response.read(limit + 1)
            content_type = response.headers.get_content_type()
    except urllib.error.HTTPError as error:
        raise RuntimeError("Frontend returned HTTP " + str(error.code) + ": " + path) from None
    if len(body) > limit:
        raise RuntimeError("Frontend response exceeds expected size: " + path)
    return body, content_type


def frontend_smoke(base, manifest=None):
    body, content_type = frontend_get(base, "/", 2 * 1024 * 1024)
    if content_type != "text/html" or b"frontend not embedded" in body.lower():
        raise RuntimeError("Rehearsal image did not serve embedded frontend HTML")
    parser = FrontendReferences()
    parser.feed(body.decode("utf-8"))
    paths = sorted({frontend_path(value) for value in parser.references})
    paths = [path for path in paths if path.endswith((".js", ".css"))]
    if not parser.has_app or not any(path.endswith(".js") for path in paths) or not any(path.endswith(".css") for path in paths):
        raise RuntimeError("Rehearsal frontend is missing its app, JavaScript or stylesheet")
    if manifest:
        paths = sorted(set(paths) | {name for name in manifest["files"]
                                    if name.startswith("assets/AvailabilityMonitorView-")
                                    and name.endswith((".js", ".css"))})
    assets = []
    for path in paths:
        expected = manifest["files"].get(path) if manifest else None
        if manifest and expected is None:
            raise RuntimeError("Frontend references an asset missing from manifest: " + path)
        content, _ = frontend_get(base, "/" + urllib.parse.quote(path, safe="/"),
                                  expected["size"] if expected else 16 * 1024 * 1024)
        actual = {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        if expected and actual != expected:
            raise RuntimeError("Embedded frontend asset differs from manifest: " + path)
        assets.append(dict(actual, path=path))
    # The server injects runtime settings/CSP nonces into index.html, so compare
    # immutable assets rather than the dynamic homepage's complete bytes.
    return {"homepage_status": 200, "embedded": True, "assets": assets,
            "manifest_sha256": manifest["sha256"] if manifest else None,
            "asset_hashes_verified": manifest is not None}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def command(args, cwd=None, timeout=120, check=True):
    result = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout, text=True)
    if check and result.returncode:
        raise RuntimeError("Command failed: " + args[0] + " (exit " + str(result.returncode) + ")")
    return result


def request(base, path, payload=None, token=None, raw=False):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, headers=headers,
                                 data=None if payload is None else json.dumps(payload).encode())
    try:
        with HTTP.open(req, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        # Do not print the response body or credentials on an API failure.
        raise RuntimeError("Fixture API " + path + " returned HTTP " + str(error.code)) from None
    if raw:
        return body.decode()
    value = json.loads(body)
    if "code" in value and value["code"] not in (0, 200):
        raise RuntimeError("Fixture API " + path + " returned an application error")
    return value.get("data", value)


def rehearsal_project_name(manager):
    marker_path = manager.project / ".rehearsal.json"
    if not marker_path.is_file() or marker_path.is_symlink():
        raise RuntimeError("Rehearsal project marker is missing or unsafe")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        project = marker["project"]
        if (not isinstance(project, str) or not re.fullmatch(r"s2rehearsal-[0-9a-f]{12}", project) or
                marker["directory"] != str(manager.project.resolve())):
            raise RuntimeError("Rehearsal project marker does not match this directory")
        return project
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Invalid rehearsal project marker") from error


def endpoint(manager, service, port):
    """Resolve only this rehearsal's private bridge IP; never publish a port."""
    if type(port) is not int or (service, port) not in (("sub2api", 8080), ("fake", 8090)):
        raise RuntimeError("Unknown rehearsal service/port pair")
    project = rehearsal_project_name(manager)
    try:
        ids = manager.run(manager.compose(["ps", "-q", service])).stdout.split()
        if len(ids) != 1 or not re.fullmatch(r"[0-9a-f]{64}", ids[0]):
            raise RuntimeError("Expected exactly one running rehearsal service container")
        inspected = json.loads(manager.run(["docker", "container", "inspect", ids[0]]).stdout)
        if len(inspected) != 1:
            raise RuntimeError("Unexpected rehearsal container inspection")
        container = inspected[0]
        labels = container["Config"]["Labels"]
        if (container["Id"] != ids[0] or container["State"]["Running"] is not True or
                labels.get("com.docker.compose.project") != project or
                labels.get("com.docker.compose.service") != service):
            raise RuntimeError("Container does not belong to the requested rehearsal service")
        networks = container["NetworkSettings"]["Networks"]
        if not isinstance(networks, dict) or len(networks) != 1:
            raise RuntimeError("Rehearsal container must have exactly one isolated network")
        name, attachment = next(iter(networks.items()))
        network_id = attachment["NetworkID"]
        if (name != project + "_default" or not re.fullmatch(r"[0-9a-f]{64}", network_id) or
                container["HostConfig"]["NetworkMode"] not in (name, network_id)):
            raise RuntimeError("Container is not using its dedicated rehearsal network")
        inspected_networks = json.loads(manager.run(["docker", "network", "inspect", network_id]).stdout)
        if len(inspected_networks) != 1:
            raise RuntimeError("Unexpected rehearsal network inspection")
        network = inspected_networks[0]
        if (network["Id"] != network_id or network["Name"] != name or
                network["Driver"] != "bridge" or network["Internal"] is not True or
                network["Labels"].get("com.docker.compose.project") != project):
            raise RuntimeError("Rehearsal network must be a dedicated internal bridge")
        address = ipaddress.IPv4Address(attachment["IPAddress"])
        if (not address.is_private or address.is_loopback or address.is_link_local or
                address.is_multicast or address.is_unspecified or address.is_reserved):
            raise RuntimeError("Rehearsal address is not a private container IPv4 address")
        subnets = [ipaddress.ip_network(entry["Subnet"], strict=False)
                   for entry in network["IPAM"]["Config"] if entry.get("Subnet")]
        if not any(subnet.version == 4 and address in subnet and
                   address not in (subnet.network_address, subnet.broadcast_address) for subnet in subnets):
            raise RuntimeError("Rehearsal address is outside its network IPv4 subnet")
        member = network["Containers"][ids[0]]
        if ipaddress.IPv4Interface(member["IPv4Address"]).ip != address:
            raise RuntimeError("Rehearsal network membership does not match the container address")
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as error:
        raise RuntimeError("Invalid rehearsal endpoint metadata") from error
    return "http://" + str(address) + ":" + str(port)


def rehearsal_postgres_container(manager):
    """Allow fixture writes only to the generated project's own local volume."""
    project = rehearsal_project_name(manager)
    compose_path = manager.project / "docker-compose.yml"
    if not compose_path.is_file() or compose_path.is_symlink():
        raise RuntimeError("Rehearsal Compose file is missing or unsafe")
    try:
        compose = json.loads(compose_path.read_text(encoding="utf-8"))
        if compose["name"] != project or compose["volumes"]["postgres-data"] != {}:
            raise RuntimeError("Rehearsal database volume must be newly managed by this project")
        ids = manager.run(manager.compose(["ps", "-q", "postgres"])).stdout.split()
        if len(ids) != 1 or not re.fullmatch(r"[0-9a-f]{64}", ids[0]):
            raise RuntimeError("Expected exactly one rehearsal PostgreSQL container")
        inspected = json.loads(manager.run(["docker", "container", "inspect", ids[0]]).stdout)
        if len(inspected) != 1:
            raise RuntimeError("Unexpected rehearsal PostgreSQL inspection")
        container = inspected[0]
        labels = container["Config"]["Labels"]
        if (container["Id"] != ids[0] or container["State"]["Running"] is not True or
                labels.get("com.docker.compose.project") != project or
                labels.get("com.docker.compose.service") != "postgres"):
            raise RuntimeError("PostgreSQL container does not belong to this rehearsal")
        environment = container["Config"]["Env"]
        for key, expected in (("POSTGRES_DB", "rehearsal"), ("POSTGRES_USER", "rehearsal"),
                              ("PGDATA", "/var/lib/postgresql/data/pgdata")):
            values = [value.split("=", 1)[1] for value in environment if value.startswith(key + "=")]
            if values != [expected]:
                raise RuntimeError("PostgreSQL environment does not match the fixture database")
        mounts = [mount for mount in container["Mounts"]
                  if mount["Destination"] == "/var/lib/postgresql/data" or
                  mount["Destination"].startswith("/var/lib/postgresql/data/")]
        volume_name = project + "_postgres-data"
        if (len(mounts) != 1 or mounts[0]["Destination"] != "/var/lib/postgresql/data" or
                mounts[0]["Type"] != "volume" or mounts[0].get("Name") != volume_name or
                mounts[0]["RW"] is not True):
            raise RuntimeError("PostgreSQL is not using this rehearsal's dedicated data volume")
        volumes = json.loads(manager.run(["docker", "volume", "inspect", volume_name]).stdout)
        if len(volumes) != 1:
            raise RuntimeError("Unexpected rehearsal volume inspection")
        volume = volumes[0]
        if (volume["Name"] != volume_name or volume["Driver"] != "local" or volume.get("Options") or
                volume["Labels"].get("com.docker.compose.project") != project or
                volume["Labels"].get("com.docker.compose.volume") != "postgres-data" or
                volume["Mountpoint"] != mounts[0]["Source"]):
            raise RuntimeError("PostgreSQL volume is external or belongs to another project")
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as error:
        raise RuntimeError("Invalid rehearsal PostgreSQL metadata") from error
    return ids[0]


def initialize_compliance_fixture(manager, base, token):
    status = request(base, "/api/v1/admin/compliance", token=token)
    version = status.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", version):
        raise RuntimeError("Invalid compliance version for rehearsal fixture")
    container_id = rehearsal_postgres_container(manager)
    # This is synthetic initialization for a throwaway test administrator. Do
    # not invoke the human acceptance API or fabricate acceptance/audit fields.
    value = json.dumps({"version": version, "test_fixture": True}, separators=(",", ":"))
    sql = """DO $fixture$
DECLARE affected integer;
BEGIN
  IF current_database() <> 'rehearsal' OR current_user <> 'rehearsal' THEN
    RAISE EXCEPTION 'Wrong rehearsal database or user';
  END IF;
  SELECT count(*) INTO affected FROM users
    WHERE email = 'rehearsal@example.test' AND role = 'admin' AND deleted_at IS NULL;
  IF affected <> 1 THEN RAISE EXCEPTION 'Expected exactly one fixture administrator'; END IF;
  INSERT INTO settings (key, value, updated_at)
    SELECT 'admin_compliance_acknowledgement:' || id::text, '""" + value + """', NOW()
    FROM users WHERE email = 'rehearsal@example.test' AND role = 'admin' AND deleted_at IS NULL
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at;
  GET DIAGNOSTICS affected = ROW_COUNT;
  IF affected <> 1 THEN RAISE EXCEPTION 'Unexpected fixture settings row count'; END IF;
END;
$fixture$;
SELECT 'fixture-initialized';
"""
    result = manager.run(["docker", "exec", container_id, "psql", "-X", "-q", "-A", "-t",
                          "-h", "/var/run/postgresql", "-U", "rehearsal", "-d", "rehearsal",
                          "--set=ON_ERROR_STOP=1", "--command", sql])
    if result.stdout.strip() != "fixture-initialized":
        raise RuntimeError("Rehearsal fixture initialization was not confirmed")
    verified = request(base, "/api/v1/admin/compliance", token=token)
    if verified.get("version") != version or verified.get("required") is not False:
        raise RuntimeError("Rehearsal fixture initialization did not clear the setup gate")
    return {"version": version, "test_fixture": True}


def wait_api(manager):
    base = endpoint(manager, "sub2api", 8080)
    deadline = time.monotonic() + 120
    while True:
        try:
            request(base, "/health")
            return base
        except (OSError, ValueError, RuntimeError):
            if time.monotonic() > deadline:
                raise RuntimeError("Rehearsal API did not become ready")
            time.sleep(2)


def prepare_project(directory, project, current, args):
    password = secrets.token_urlsafe(24)
    (directory / "data").mkdir(mode=0o700)
    environment = {
        "AUTO_SETUP": "true", "RUN_MODE": "standard", "SERVER_HOST": "0.0.0.0", "SERVER_PORT": "8080",
        "DATABASE_HOST": "postgres", "DATABASE_PORT": "5432", "DATABASE_USER": "rehearsal",
        "DATABASE_PASSWORD": password, "DATABASE_DBNAME": "rehearsal", "DATABASE_SSLMODE": "disable",
        "DATABASE_MAX_OPEN_CONNS": "8", "DATABASE_MAX_IDLE_CONNS": "2",
        "REDIS_HOST": "redis", "REDIS_PORT": "6379", "REDIS_POOL_SIZE": "16", "REDIS_MIN_IDLE_CONNS": "1",
        "ADMIN_EMAIL": "rehearsal@example.test", "ADMIN_PASSWORD": password,
        "JWT_SECRET": secrets.token_hex(32), "TOTP_ENCRYPTION_KEY": secrets.token_hex(32),
        "SECURITY_URL_ALLOWLIST_ENABLED": "false", "SECURITY_URL_ALLOWLIST_ALLOW_PRIVATE_HOSTS": "true",
        "SECURITY_URL_ALLOWLIST_ALLOW_INSECURE_HTTP": "true", deploy.FEATURE_FLAG: "false",
        # Pick the highest-priority fixture first; production keeps its own scheduler settings.
        "GATEWAY_OPENAI_WS_LB_TOP_K": "1",
        "PRICING_REMOTE_URL": "http://fake:8090/pricing.json", "PRICING_HASH_URL": "http://fake:8090/pricing.sha256",
    }
    compose = {"name": project, "services": {
        "sub2api": {"image": current["image"], "restart": "no", "mem_limit": "640m", "cpus": "1.0",
                    "volumes": ["./data:/app/data"], "environment": environment,
                    "healthcheck": {"interval": "2s", "timeout": "2s", "start_period": "5s", "retries": 3},
                    "depends_on": {"postgres": {"condition": "service_healthy"}, "redis": {"condition": "service_healthy"},
                                   "fake": {"condition": "service_started"}}},
        "postgres": {"image": args.postgres_image, "mem_limit": "192m", "cpus": "0.5",
                     "command": ["postgres", "-c", "shared_buffers=32MB", "-c", "max_connections=20", "-c", "work_mem=2MB"],
                     "environment": {"POSTGRES_USER": "rehearsal", "POSTGRES_PASSWORD": password,
                                     "POSTGRES_DB": "rehearsal", "PGDATA": "/var/lib/postgresql/data/pgdata"},
                     "volumes": ["postgres-data:/var/lib/postgresql/data"],
                     "healthcheck": {"test": ["CMD-SHELL", "pg_isready -U rehearsal -d rehearsal"], "interval": "2s", "timeout": "2s", "retries": 30}},
        "redis": {"image": args.redis_image, "mem_limit": "64m", "cpus": "0.25",
                  "command": ["redis-server", "--maxmemory", "32mb", "--maxmemory-policy", "allkeys-lru"],
                  "healthcheck": {"test": ["CMD", "redis-cli", "ping"], "interval": "2s", "timeout": "2s", "retries": 30}},
        "fake": {"image": args.fixture_image, "mem_limit": "64m", "cpus": "0.25",
                 "volumes": [str(HERE / "fixtures") + ":/fixture:ro"],
                 "command": ["python", "-u", "/fixture/rehearsal_upstream.py"]}},
        "volumes": {"postgres-data": {}}, "networks": {"default": {"internal": True}}}
    write_json(directory / "docker-compose.yml", compose)
    write_json(directory / "docker-compose.override.yml", {"services": {}})
    write_json(directory / ".rehearsal.json", {"project": project, "directory": str(directory)})
    backup = "#!/bin/sh\nset -eu\nexec python3 " + shlex.quote(str(HERE / "fixtures/rehearsal_backup.py")) + " " + shlex.quote(str(directory)) + "\n"
    (directory / "backup.sh").write_text(backup, encoding="utf-8")
    (directory / "backup.sh").chmod(0o700)
    return password


def assert_flag(manager, expected):
    value = manager.run(manager.compose(["exec", "-T", "sub2api", "printenv", deploy.FEATURE_FLAG])).stdout.strip()
    if value != expected:
        raise RuntimeError("Rehearsal feature flag differs from expected state")


def cleanup_project(manager, images):
    """Keep cleanup failures separate so the original result is always saved."""
    failures = []
    try:
        result = manager.run(manager.compose(["down", "--volumes", "--remove-orphans"]), check=False, timeout=120)
        if result.returncode:
            failures.append("compose down: exit " + str(result.returncode))
    except Exception as error:
        failures.append("compose down: " + type(error).__name__)
    for image in images:
        try:
            result = command(["docker", "image", "rm", image], check=False)
            if result.returncode:
                failures.append("image removal: exit " + str(result.returncode))
        except Exception as error:
            failures.append("image removal: " + type(error).__name__)
    return failures


def routing_smoke(manager, password):
    base = wait_api(manager)
    token = request(base, "/api/v1/auth/login", {"email": "rehearsal@example.test", "password": password})["access_token"]
    compliance_fixture = initialize_compliance_fixture(manager, base, token)
    # Standard mode preserves per-case group isolation. Fund only the synthetic
    # user in this freshly verified rehearsal project for the local fake calls.
    user = request(base, "/api/v1/auth/me", token=token)
    if user.get("email") != "rehearsal@example.test" or user.get("role") != "admin":
        raise RuntimeError("Unexpected rehearsal administrator")
    request(base, "/api/v1/admin/users/" + str(user["id"]) + "/balance",
            {"balance": 10, "operation": "set", "notes": "Isolated rehearsal fixture"}, token)
    # The admin version endpoint also checks GitHub. Read the running image's
    # version directly so this isolated test has no external update dependency.
    version_result = manager.run(manager.compose(["exec", "-T", "sub2api", "/app/sub2api", "-version"]))
    version = (version_result.stdout + version_result.stderr).strip()
    if "Sub2API " not in version:
        raise RuntimeError("Running rehearsal binary did not report its version")
    fake = endpoint(manager, "fake", 8090)
    results = []
    for passthrough in (False, True):
        for failure in ("http", "sse"):
            case = ("passthrough" if passthrough else "native") + "-" + failure
            group = request(base, "/api/v1/admin/groups", {"name": case, "platform": "openai", "rate_multiplier": 1,
                            "subscription_type": "standard", "is_exclusive": False}, token)
            group_id = group["id"]
            for index, side in enumerate(("a", "b")):
                request(base, "/api/v1/admin/accounts", {
                    "name": case + "-" + side, "platform": "openai", "type": "apikey", "priority": index + 1,
                    "concurrency": 4, "group_ids": [group_id], "rate_multiplier": 1,
                    "upstream_billing_probe_enabled": False,
                    "credentials": {"api_key": "sk-fixture-" + side, "base_url": "http://fake:8090/" + case + "/" + side},
                    "extra": {"openai_passthrough": passthrough}}, token)
            key = request(base, "/api/v1/keys", {"name": case, "group_id": group_id}, token)["key"]
            elapsed = []
            for sequence in ("first", "next"):
                start = time.monotonic()
                stream = request(base, "/v1/responses", {"model": "gpt-5.4", "input": "Rehearsal only",
                                 "stream": True, "prompt_cache_key": case + "-" + sequence}, key, raw=True)
                elapsed.append(round(time.monotonic() - start, 3))
                if "B rehearsal answer" not in stream or '"type":"response.failed"' in stream.replace(" ", ""):
                    raise RuntimeError("Routing smoke failed: " + case)
            calls = request(fake, "/calls").get(case)
            if calls != ["a", "b", "b"]:
                raise RuntimeError("Expected A, B, B upstream attempts: " + case + "; observed=" + repr(calls))
            results.append({"case": case, "upstream_attempts": calls, "request_seconds": elapsed})
    monitoring = monitoring_smoke(base, token)
    return {"version": version, "cases": results, "compliance_fixture": compliance_fixture,
            "monitoring": monitoring}


def monitoring_smoke(base, token):
    """Read real, asynchronously saved events from the isolated routing cases."""
    events = ("openai.upstream_failover_switching", "openai_model_transient_state")
    deadline = time.monotonic() + 30
    observed = {}
    while True:
        for event in events:
            query = urllib.parse.urlencode({"event": event, "time_range": "1h", "page_size": 100})
            result = request(base, "/api/v1/admin/ops/system-logs?" + query, token=token)
            if any(row.get("message") != event for row in result["items"]):
                raise RuntimeError("Monitoring exact event filter included unrelated logs")
            observed[event] = result
        if all(observed[event]["total"] >= 4 for event in events):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Routing events were not saved for monitoring")
        time.sleep(1)
    failed = observed[events[0]]["items"][0]
    query = urllib.parse.urlencode({"request_id": failed["request_id"], "time_range": "1h", "page_size": 100})
    trace = request(base, "/api/v1/admin/ops/system-logs?" + query, token=token)
    if any(row.get("request_id") != failed["request_id"] for row in trace["items"]):
        raise RuntimeError("Monitoring request trace included a different request")
    final = next((row for row in trace["items"] if row.get("message") == "http request completed"), None)
    if not final or final.get("extra", {}).get("status_code") != 200 or final.get("account_id") == failed.get("account_id"):
        raise RuntimeError("Monitoring did not correlate the final response with the replacement account")
    missing = request(base, "/api/v1/admin/ops/system-logs?" + urllib.parse.urlencode({
        "event": "openai.upstream_failover_switching-unrelated", "time_range": "1h"}), token=token)
    if missing["total"] != 0 or missing["items"]:
        raise RuntimeError("Monitoring event filter unexpectedly used partial matching")
    overview = request(base, "/api/v1/admin/ops/dashboard/overview?platform=openai&time_range=1h&mode=raw", token=token)
    errors = request(base, "/api/v1/admin/ops/request-errors?platform=openai&time_range=1h&view=all&page_size=20", token=token)
    if "ttft" not in overview or "items" not in errors or "total" not in errors:
        raise RuntimeError("Monitoring overview or errors response is incomplete")
    req = urllib.request.Request(base + "/api/v1/admin/ops/system-logs")
    try:
        with HTTP.open(req, timeout=15):
            raise RuntimeError("Monitoring admin data was accessible without authentication")
    except urllib.error.HTTPError as error:
        if error.code not in (401, 403):
            raise RuntimeError("Unexpected unauthenticated monitoring response") from None
    return {"exact_events": {event: observed[event]["total"] for event in events},
            "request_trace_correlated": True, "final_http_status": 200,
            "replacement_account_differs": True, "unrelated_event_empty": True,
            "overview_and_errors_readable": True, "anonymous_access_denied": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--frontend-manifest", type=Path,
                        help="Verified dist manifest with relative path, sha256 and size for every file")
    parser.add_argument("--current-manifest", type=Path, default=HERE / "manifests/official-0.2.0.json")
    parser.add_argument("--directory", type=Path, help="Must not already exist; default is a new /tmp directory")
    parser.add_argument("--fixture-image", default="python:3.13-alpine")
    parser.add_argument("--postgres-image", default="postgres:18-alpine")
    parser.add_argument("--redis-image", default="redis:8-alpine")
    parser.add_argument("--keep", action="store_true", help="Keep only this rehearsal's containers/volume for inspection")
    args = parser.parse_args(argv)
    if sys.platform != "linux":
        parser.error("Real rehearsal requires Linux Docker; no production fallback exists")
    os.umask(0o077)
    current = deploy.manifest(deploy.read_json(args.current_manifest))
    target = deploy.manifest(deploy.read_json(args.manifest))
    frontend_manifest = load_frontend_manifest(args.frontend_manifest) if args.frontend_manifest else None
    deploy.Deployment.compatible(current, target)
    project = "s2rehearsal-" + uuid.uuid4().hex[:12]
    if args.directory:
        directory = args.directory.resolve()
        directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    else:
        directory = Path(tempfile.mkdtemp(prefix=project + "-"))
    manager = deploy.Deployment(directory, health_timeout=120, poll_interval=2)
    report = {"project": project, "directory": str(directory), "status": "running"}
    derived_image = None
    alias = project + ":healthy"
    alias_created = False
    started = False
    try:
        manager.inspect_image(current)
        target_id = manager.inspect_image(target, custom=True)
        for image in (args.postgres_image, args.redis_image, args.fixture_image):
            if command(["docker", "image", "inspect", image], check=False).returncode:
                command(["docker", "pull", image], timeout=600)
        password = prepare_project(directory, project, current, args)
        started = True
        manager.run(manager.compose(["up", "-d", "--pull", "never"]), timeout=180)
        manager.wait_healthy(manager.inspect_image(current))
        wait_api(manager)
        dependencies = manager.run(manager.compose(["ps", "-q", "postgres", "redis"])).stdout.splitlines()
        report["initial_health"] = "healthy"
        assert_flag(manager, "false")
        report["upgrade"] = manager.upgrade(current, target, apply=True)
        assert_flag(manager, "true")
        report["frontend"] = frontend_smoke(wait_api(manager), frontend_manifest)
        report["routing"] = routing_smoke(manager, password)
        report["manual_rollback"] = manager.rollback(report["upgrade"]["transaction"], apply=True)
        assert_flag(manager, "false")
        # Derive a uniquely labelled fixture image. The application is unchanged;
        # only HEALTHCHECK is made permanently unhealthy to exercise restoration.
        build_dir = directory / "unhealthy-fixture"
        build_dir.mkdir()
        command(["docker", "tag", target_id, alias])
        alias_created = True
        (build_dir / "Dockerfile").write_text("FROM " + alias + "\nLABEL io.sub2api.rehearsal=" + project + "\nHEALTHCHECK --interval=1s --timeout=1s --start-period=0s --retries=1 CMD exit 1\n")
        iid = build_dir / "image.id"
        command(["docker", "build", "--pull=false", "--network=none", "--iidfile", str(iid), str(build_dir)], timeout=180)
        derived_image = iid.read_text().strip()
        failed_target = dict(target, image=derived_image)
        manager.health_timeout = 30
        try:
            manager.upgrade(current, failed_target, apply=True)
        except deploy.DeploymentError:
            record = deploy.read_json(manager.transaction_path / "record.json")
            if record["status"] != "rolled_back":
                raise
            report["automatic_rollback"] = {"status": record["status"], "transaction": record["id"]}
        else:
            raise RuntimeError("Unhealthy fixture unexpectedly passed deployment health checks")
        manager.verify_running(current)
        assert_flag(manager, "false")
        if manager.run(manager.compose(["ps", "-q", "postgres", "redis"])).stdout.splitlines() != dependencies:
            raise RuntimeError("Deployment recreated a dependency container")
        report["dependencies_preserved"] = True
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = str(error) if isinstance(error, (RuntimeError, deploy.DeploymentError)) else type(error).__name__
    finally:
        if started and not args.keep:
            # This command only targets the freshly generated project name/files.
            images = ([derived_image] if derived_image else []) + ([alias] if alias_created else [])
            cleanup_failures = cleanup_project(manager, images)
            report["cleanup"] = "failed" if cleanup_failures else "completed"
            if cleanup_failures:
                report["cleanup_errors"] = cleanup_failures
                report["status"] = "failed"
        else:
            report["cleanup"] = "kept" if started else "not-started"
        write_json(directory / "rehearsal-result.json", report)
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
