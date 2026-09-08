#!/usr/bin/env python3
"""Deploy a verified Sub2API image without rebuilding dependent services."""

import argparse
import base64
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid


class DeploymentError(RuntimeError):
    pass


HEX64 = re.compile(r"^[0-9a-f]{64}$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
IMAGE = re.compile(r"^(?:sha256:[0-9a-f]{64}|[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[0-9a-f]{64})$")
TRANSACTION = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")
MIGRATIONS_LABEL = "io.sub2api.availability.migrations-sha256"
REVISION_LABEL = "org.opencontainers.image.revision"
FEATURE_FLAG = "GATEWAY_OPENAI_APIKEY_AVAILABILITY_ENABLED"

# Each entry requires a reviewed SQL diff, a restored-database rehearsal, and
# application rollback tests. This is not a general schema-bypass option.
# Evidence and the application-only rollback boundary: UPGRADE_0_2_1.md.
REVIEWED_ADDITIVE_TRANSITIONS = {
    # Migration 236 repairs the 0.2.2 allowlist column in place. Application
    # rollback to 0.2.2 retains this compatible column and the 236 ledger entry.
    # See UPGRADE_0_2_3.md for the restored-copy and rollback evidence.
    ("5485f368b29d05adb95a00f71801c7c23d8f48af",
     "e5e58e07acb1018cbd3a2427054eb453263d84e8b773aca907c2c73051e2a96f",
     "8fa67d477d6651a744754392a8982ea589c26ae6",
     "37008c238c0b9214fe5d414a08b023263d6694b899d704fe1c49f21a619f3bd6"): "0.2.2-to-0.2.3-allowlist-repair",
    ("aa236488351eb71e120fc2b6fb32e36b0374c918",
     "cfd6e44c66258419248741d14d9981fc734a314cab00ee95c44547f7abceef1e",
     "578785ee7fb35030b094b69624efe25670a36f5f",
     "c457086f49728b5c59eb875ccacbc57b17bf4fdcaef3288288ee2fbd5bb074ef"): "0.2.0-to-0.2.1-additive",
}

# This rename requires the guarded reverse migration before the old app starts.
# It is deliberately separate from application-only additive rollback.
REVIEWED_RENAME_TRANSITION = (
    "578785ee7fb35030b094b69624efe25670a36f5f",
    "c457086f49728b5c59eb875ccacbc57b17bf4fdcaef3288288ee2fbd5bb074ef",
    "5485f368b29d05adb95a00f71801c7c23d8f48af",
    "e5e58e07acb1018cbd3a2427054eb453263d84e8b773aca907c2c73051e2a96f",
)
RENAME_MIGRATION = "235_group_model_allowlist.sql"
RENAME_CHECKSUM = "546fd53d114f9a8c402b019af71bf4685dc1968fd5cd25d054d095a58d806fbc"
# The all-LF checkout differs only in non-code text line endings (including the
# migrations README). SQL, schema Go files and migration runner are identical.
REVIEWED_RENAME_TARGET_HASHES = {
    REVIEWED_RENAME_TRANSITION[3],
    "8667b82b352d70686d4e6a1358499064fc6d07909f2a3351fe4e4aac37d283cf",
}


def is_reviewed_rename(current, target):
    return (current["revision"], current["migrations_sha256"],
            target["revision"]) == REVIEWED_RENAME_TRANSITION[:3] and target["migrations_sha256"] in REVIEWED_RENAME_TARGET_HASHES


def validate_rename_state(state, *, rollback=False):
    if not isinstance(state, dict) or state.get("unexpected_migrations"):
        raise DeploymentError("Unreviewed database migrations; rename transition refused")
    old, new, checksum = state.get("old_column"), state.get("new_column"), state.get("checksum")
    if old is True and new is False and checksum is None:
        if not rollback and state.get("enabled_groups") != 0:
            raise DeploymentError("Enabled model lists require review before becoming request allowlists")
        return "original"
    if rollback and old is False and new is True and checksum == RENAME_CHECKSUM:
        if state.get("enabled_groups") != 0:
            raise DeploymentError("Active model allowlists require review before reverting to 0.2.1")
        return "renamed"
    raise DeploymentError("Database columns or migration checksum do not match the reviewed transition")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DeploymentError("Cannot read valid JSON: " + str(path)) from error


def manifest(value):
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise DeploymentError("Manifest schema_version must be 1")
    for key, pattern in (("image", IMAGE), ("revision", REVISION),
                         ("migrations_sha256", HEX64)):
        if not isinstance(value.get(key), str) or not pattern.fullmatch(value[key]):
            raise DeploymentError("Invalid manifest field: " + key)
    return {key: value[key] for key in ("schema_version", "image", "revision", "migrations_sha256")}


def atomic_write(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Runner:
    def __call__(self, args, *, cwd, timeout=60, check=True):
        try:
            result = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                                    timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DeploymentError("Command failed or timed out: " + args[0]) from error
        if check and result.returncode:
            # Backup output and Compose diagnostics can contain secrets.
            raise DeploymentError("Command failed (exit %d): %s" % (result.returncode, args[0]))
        return result


class Deployment:
    def __init__(self, project_dir, *, runner=None, health_timeout=120,
                 poll_interval=2, clock=time.monotonic, sleep=time.sleep):
        self.project = Path(project_dir).resolve()
        self.files = [self.project / "docker-compose.yml",
                      self.project / "docker-compose.override.yml"]
        self.override = self.project / "docker-compose.availability.yml"
        self.state = self.project / ".sub2api-maintenance"
        self.runner = runner or Runner()
        self.health_timeout = health_timeout
        self.poll_interval = poll_interval
        self.clock = clock
        self.sleep = sleep
        self.service = "sub2api"
        self.transaction_path = None

    def run(self, args, **kwargs):
        return self.runner(args, cwd=str(self.project), **kwargs)

    def compose(self, args):
        command = ["docker", "compose", "--project-directory", str(self.project)]
        for path in self.files:
            command.extend(["-f", str(path)])
        if self.override.exists():
            command.extend(["-f", str(self.override)])
        return command + args

    def validate_paths(self):
        if not self.project.is_dir():
            raise DeploymentError("Project directory does not exist")
        for path in self.files:
            if not path.is_file() or path.is_symlink():
                raise DeploymentError("Expected a regular Compose file: " + str(path))
        for path in (self.override, self.state):
            if path.is_symlink():
                raise DeploymentError("Managed paths must not be symbolic links")
        if self.override.exists():
            try:
                value = read_json(self.override)
                image_ref = value["services"][self.service]["image"]
            except (KeyError, TypeError) as error:
                raise DeploymentError("Existing availability override was not created by this tool") from error
            allowed = (self.override_value(image_ref), {"services": {self.service: {"image": image_ref}}})
            if value not in allowed or not isinstance(image_ref, str) or not IMAGE.fullmatch(image_ref):
                raise DeploymentError("Availability override has unexpected settings")

    @contextlib.contextmanager
    def lock(self):
        if os.name != "posix":
            raise DeploymentError("Apply requires Linux with fcntl locking")
        import fcntl
        self.state.mkdir(mode=0o700, exist_ok=True)
        lock_path = self.state / "deployment.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise DeploymentError("Another deployment is running") from error
            yield

    def inspect_image(self, release, *, required=True, custom=False):
        result = self.run(["docker", "image", "inspect", release["image"]], check=False)
        if result.returncode:
            if required:
                raise DeploymentError("Immutable image is not available locally")
            return None
        try:
            images = json.loads(result.stdout)
            image = images[0]
            image_id = image["Id"]
            labels = image["Config"].get("Labels") or {}
            healthcheck = image["Config"].get("Healthcheck") or {}
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise DeploymentError("Invalid Docker image inspection result") from error
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise DeploymentError("Invalid Docker image ID")
        if labels.get(REVISION_LABEL) != release["revision"]:
            raise DeploymentError("Image revision differs from release manifest")
        migration_label = labels.get(MIGRATIONS_LABEL)
        if (custom or migration_label is not None) and migration_label != release["migrations_sha256"]:
            raise DeploymentError("Image migration fingerprint differs from release manifest")
        if not healthcheck.get("Test") or healthcheck["Test"][0] == "NONE":
            raise DeploymentError("Image must provide a Docker HEALTHCHECK")
        return image_id

    def container(self, *, include_stopped=False, deadline=None):
        def bounded(args):
            remaining = 60 if deadline is None else deadline - self.clock()
            if remaining <= 0:
                raise DeploymentError("Container inspection deadline expired")
            return self.run(args, timeout=remaining)

        arguments = ["ps"] + (["--all"] if include_stopped else []) + ["-q", self.service]
        result = bounded(self.compose(arguments))
        ids = result.stdout.strip().splitlines()
        if len(ids) != 1 or not re.fullmatch(r"[0-9a-f]{12,64}", ids[0]):
            raise DeploymentError("Expected exactly one identifiable Sub2API container")
        # Read only fields needed for validation; never capture container Env.
        template = ('{"image":{{json .Image}},"running":{{json .State.Running}},'
                    '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},'
                    '"compose_files":{{json (index .Config.Labels "com.docker.compose.project.config_files")}},'
                    '"compose_working_dir":{{json (index .Config.Labels "com.docker.compose.project.working_dir")}},'
                    '"compose_service":{{json (index .Config.Labels "com.docker.compose.service")}}}')
        result = bounded(["docker", "inspect", "--format", template, ids[0]])
        try:
            value = json.loads(result.stdout)
            if not isinstance(value, dict) or not isinstance(value.get("running"), bool):
                raise ValueError("Invalid state")
        except (ValueError, TypeError) as error:
            raise DeploymentError("Invalid Docker container inspection result") from error
        value["id"] = ids[0]
        return value

    def verify_running(self, release, *, custom=False, healthy=True, allow_stopped=False):
        image_id = self.inspect_image(release, custom=custom)
        actual = self.container(include_stopped=allow_stopped)
        if actual.get("image") != image_id or (not allow_stopped and not actual["running"]):
            raise DeploymentError("Current container does not match the supplied current manifest")
        if healthy and actual.get("health") != "healthy":
            raise DeploymentError("Current Sub2API container is not healthy; investigate before upgrading")
        expected_files = self.files + ([self.override] if self.override.exists() else [])
        if (actual.get("compose_files") != ",".join(str(path) for path in expected_files) or
                actual.get("compose_working_dir") != str(self.project) or
                actual.get("compose_service") != self.service):
            raise DeploymentError("Container Compose labels do not match this deployment file set")
        diff = self.run(["docker", "diff", actual["id"]])
        for line in diff.stdout.splitlines():
            parts = line.split(maxsplit=1)
            if len(parts) == 2 and parts[1].rstrip("/") == "/app/sub2api":
                raise DeploymentError("Container binary differs from its image; reconcile the in-app update first")
        return image_id

    def configuration(self):
        result = self.run(self.compose(["config", "--format", "json"]))
        try:
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise ValueError("Expected an object")
            return value
        except (ValueError, TypeError) as error:
            raise DeploymentError("Cannot resolve the Compose configuration") from error

    def verify_configured_current(self, image_id):
        value = self.configuration()
        try:
            configured = value["services"][self.service]["image"]
        except (KeyError, TypeError) as error:
            raise DeploymentError("Cannot resolve the current Compose image") from error
        if not isinstance(configured, str) or not IMAGE.fullmatch(configured):
            raise DeploymentError("Current Compose image must already use an immutable ID or digest")
        inspected = self.run(["docker", "image", "inspect", "--format", "{{.Id}}", configured])
        if inspected.stdout.strip() != image_id:
            raise DeploymentError("Compose image differs from the current container; resolve drift first")
        return sha256(canonical(value))

    def rename_database_command(self):
        """Resolve only this Compose project's local Postgres, without passwords."""
        services = self.configuration().get("services", {})
        env = services.get(self.service, {}).get("environment", {})
        if "postgres" not in services or not isinstance(env, dict):
            raise DeploymentError("Reviewed rename requires this project's Postgres service")
        ids = self.run(self.compose(["ps", "-q", "postgres"])).stdout.strip().splitlines()
        if len(ids) != 1 or not re.fullmatch(r"[a-f0-9]{12,64}", ids[0]):
            raise DeploymentError("Expected one local Postgres container")
        template = ('{"name":{{json .Name}},"running":{{json .State.Running}},'
                    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
                    '"directory":{{json (index .Config.Labels "com.docker.compose.project.working_dir")}},'
                    '"networks":{{json .NetworkSettings.Networks}}}')
        pg = json.loads(self.run(["docker", "inspect", "--format", template, ids[0]]).stdout)
        aliases = {"postgres", str(pg.get("name", "")).lstrip("/")}
        for network in (pg.get("networks") or {}).values():
            aliases.update(network.get("Aliases") or [])
        if (pg.get("running") is not True or pg.get("service") != "postgres" or
                pg.get("directory") != str(self.project) or env.get("DATABASE_HOST") not in aliases or
                str(env.get("DATABASE_PORT", "5432")) != "5432"):
            raise DeploymentError("Database connection is outside the verified Compose deployment")
        user, database = env.get("DATABASE_USER"), env.get("DATABASE_DBNAME")
        if not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", v) for v in (user, database)):
            raise DeploymentError("Database user/name are missing or unsupported")
        return ["docker", "exec", "-e", "PGOPTIONS=-c statement_timeout=30000 -c lock_timeout=10000",
                ids[0], "psql", "-X", "-q", "-A", "-t", "-U", user, "-d", database,
                "--set=ON_ERROR_STOP=1", "--command"]

    def rename_database_state(self):
        query = """SELECT json_build_object(
            'old_column', EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='public'
                AND table_name='groups' AND column_name='models_list_config' AND data_type='jsonb'),
            'new_column', EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='public'
                AND table_name='groups' AND column_name='model_allowlist' AND data_type='jsonb'),
            'checksum', (SELECT checksum FROM schema_migrations WHERE filename='235_group_model_allowlist.sql'),
            'unexpected_migrations', (SELECT count(*) FROM schema_migrations WHERE filename >= '235_'
                AND filename <> '235_group_model_allowlist.sql'),
            'enabled_groups', (SELECT count(*) FROM groups g WHERE deleted_at IS NULL AND
                COALESCE(to_jsonb(g)->'model_allowlist',to_jsonb(g)->'models_list_config')->>'enabled'='true'))"""
        return json.loads(self.run(self.rename_database_command() + [query], timeout=40).stdout)

    def reverse_reviewed_rename(self, record):
        if not is_reviewed_rename(record["current"], record["target"]):
            return
        validate_rename_state(self.rename_database_state(), rollback=True)
        # Drain/stop only the app before touching the shared schema. The SQL also
        # takes the upstream migration lock and repeats checks atomically.
        self.run(self.compose(["stop", "--timeout", "30", self.service]), timeout=60)
        if self.run(self.compose(["ps", "--status", "running", "-q", self.service])).stdout.strip():
            raise DeploymentError("Application still running; reverse migration refused")
        query = """BEGIN;
        SELECT pg_advisory_xact_lock(694208311321144027);
        LOCK TABLE public.groups IN ACCESS EXCLUSIVE MODE;
        DO $rollback$
        DECLARE old_exists boolean; new_exists boolean; applied_checksum text;
        BEGIN
            IF EXISTS(SELECT 1 FROM schema_migrations WHERE filename >= '235_'
                      AND filename <> '235_group_model_allowlist.sql') THEN
                RAISE EXCEPTION 'Unreviewed migrations; rollback refused';
            END IF;
            SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='public'
                AND table_name='groups' AND column_name='models_list_config' AND data_type='jsonb') INTO old_exists;
            SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='public'
                AND table_name='groups' AND column_name='model_allowlist' AND data_type='jsonb') INTO new_exists;
            SELECT checksum INTO applied_checksum FROM schema_migrations WHERE filename='235_group_model_allowlist.sql';
            IF old_exists AND NOT new_exists AND applied_checksum IS NULL THEN RETURN; END IF;
            IF NOT old_exists AND new_exists AND applied_checksum = 'CHECKSUM' THEN
                IF EXISTS(SELECT 1 FROM public.groups WHERE deleted_at IS NULL AND model_allowlist->>'enabled'='true') THEN
                    RAISE EXCEPTION 'Active model allowlists require rollback review';
                END IF;
                ALTER TABLE public.groups RENAME COLUMN model_allowlist TO models_list_config;
                DELETE FROM schema_migrations WHERE filename='235_group_model_allowlist.sql' AND checksum='CHECKSUM';
            ELSE
                RAISE EXCEPTION 'Unexpected columns or checksum; rollback refused';
            END IF;
        END $rollback$;
        COMMIT;""".replace("CHECKSUM", RENAME_CHECKSUM)
        self.run(self.rename_database_command() + [query], timeout=40)
        if validate_rename_state(self.rename_database_state(), rollback=True) != "original":
            raise DeploymentError("Reverse migration did not restore the expected column")

    def backup(self):
        script = self.project / "backup.sh"
        backups = self.project / "backups"
        if not script.is_file() or script.is_symlink() or backups.is_symlink():
            raise DeploymentError("Expected a regular backup.sh and a local backups directory")
        before = set(backups.iterdir()) if backups.exists() else set()
        self.run(["bash", str(script)], timeout=1800)
        after = set(backups.iterdir()) if backups.exists() else set()
        created = [path for path in after - before if path.is_dir() and not path.is_symlink()]
        if len(created) != 1:
            raise DeploymentError("Backup must produce exactly one new snapshot directory")
        snapshot = created[0]
        sums = snapshot / "SHA256SUMS"
        if not sums.is_file() or sums.is_symlink():
            raise DeploymentError("Backup is missing SHA256SUMS")
        try:
            recorded = {}
            for line in sums.read_text(encoding="ascii").splitlines():
                checksum, filename = line.split(maxsplit=1)
                recorded[filename.lstrip("*")] = checksum
        except (OSError, ValueError) as error:
            raise DeploymentError("Invalid backup checksum file") from error
        verified = {}
        for name in ("database.dump", "runtime.tar.gz"):
            path = snapshot / name
            if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
                raise DeploymentError("Backup artifact missing or empty: " + name)
            actual = file_hash(path)
            if recorded.get(name) != actual:
                raise DeploymentError("Backup checksum mismatch: " + name)
            verified[name] = actual
        contents = snapshot / "database-contents.txt"
        if not contents.is_file() or contents.is_symlink() or contents.stat().st_size == 0:
            raise DeploymentError("Backup archive listing is missing or empty")
        return {"path": str(snapshot.resolve()), "sha256": verified}

    @staticmethod
    def override_value(image_ref):
        return {"services": {"sub2api": {"image": image_ref,
                                         "environment": {FEATURE_FLAG: "true"}}}}

    def override_bytes(self, release):
        return canonical(self.override_value(release["image"]))

    def install(self, release, record):
        atomic_write(self.override, self.override_bytes(release))
        record["target_config_sha256"] = sha256(canonical(self.configuration()))
        self.save_record(record)
        self.recreate()

    def recreate(self):
        self.run(self.compose(["up", "-d", "--no-deps", "--no-build", "--pull", "never",
                               "--force-recreate", self.service]), timeout=180)

    def restore(self, record):
        if record["compose_sha256"] != {p.name: file_hash(p) for p in self.files}:
            raise DeploymentError("Compose configuration changed; automatic restoration refused")
        if (record.get("target_config_sha256") and
                sha256(canonical(self.configuration())) != record["target_config_sha256"]):
            raise DeploymentError("Resolved target configuration changed; restoration refused")
        self.reverse_reviewed_rename(record)
        previous = record["previous_override"]
        if previous is None:
            if self.override.exists():
                self.override.unlink()
        else:
            try:
                data = base64.b64decode(previous["base64"], validate=True)
            except (ValueError, KeyError, TypeError) as error:
                raise DeploymentError("Invalid previous override in transaction") from error
            if sha256(data) != previous.get("sha256"):
                raise DeploymentError("Previous override checksum mismatch")
            atomic_write(self.override, data)
        if self.verify_configured_current(record["image_ids"]["current"]) != record["current_config_sha256"]:
            raise DeploymentError("Resolved original configuration changed; recreation refused")
        self.recreate()

    def wait_healthy(self, image_id):
        deadline = self.clock() + self.health_timeout
        while True:
            try:
                actual = self.container(deadline=deadline)
                if actual.get("image") == image_id and actual["running"] and actual.get("health") == "healthy":
                    return
            except DeploymentError:
                pass
            if self.clock() >= deadline:
                raise DeploymentError("Sub2API failed to become healthy within the configured timeout")
            self.sleep(min(self.poll_interval, max(0, deadline - self.clock())))

    def save_record(self, record):
        data = canonical(record)
        atomic_write(self.transaction_path / "record.json", data)
        atomic_write(self.transaction_path / "record.sha256", (sha256(data) + "\n").encode())

    def start_record(self, current, target, config_sha256):
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        transaction_id = stamp + "-" + uuid.uuid4().hex[:12]
        transactions = self.state / "transactions"
        transactions.mkdir(mode=0o700, exist_ok=True)
        self.transaction_path = transactions / transaction_id
        self.transaction_path.mkdir(mode=0o700)
        record = {"schema_version": 1, "id": transaction_id, "project": str(self.project),
                  "service": self.service, "status": "preparing", "current": current, "target": target,
                  "compose_sha256": {path.name: file_hash(path) for path in self.files},
                  "target_override_sha256": sha256(self.override_bytes(target)),
                  "current_config_sha256": config_sha256,
                  "schema_compatibility": self.compatible(current, target),
                  "previous_override": None}
        if self.override.exists():
            data = self.override.read_bytes()
            record["previous_override"] = {"base64": base64.b64encode(data).decode("ascii"),
                                           "sha256": sha256(data)}
        self.save_record(record)
        return record

    @staticmethod
    def compatible(current, target):
        if current["migrations_sha256"] == target["migrations_sha256"]:
            return "identical"
        transition = (current["revision"], current["migrations_sha256"],
                      target["revision"], target["migrations_sha256"])
        if transition in REVIEWED_ADDITIVE_TRANSITIONS:
            return REVIEWED_ADDITIVE_TRANSITIONS[transition]
        if is_reviewed_rename(current, target):
            return "0.2.1-to-0.2.2-guarded-column-rename"
        raise DeploymentError("Migration fingerprints differ: isolated upgrade rehearsal is required; apply refused")

    def upgrade(self, current, target, *, apply=False):
        current, target = manifest(current), manifest(target)
        self.compatible(current, target)
        self.validate_paths()
        if is_reviewed_rename(current, target):
            validate_rename_state(self.rename_database_state())
        if current["image"] == target["image"]:
            raise DeploymentError("Current and target image are identical")
        if not apply:
            current_id = self.verify_running(current)
            self.verify_configured_current(current_id)
            target_id = self.inspect_image(target, required=False, custom=True)
            return {"mode": "dry-run", "action": "upgrade", "service": self.service,
                    "current": current["image"], "target": target["image"],
                    "target_pull_required": target_id is None,
                    "schema_compatibility": self.compatible(current, target),
                    "steps": ["verify immutable images and reviewed schema compatibility", "verify new backup",
                              "write dedicated image override", "recreate sub2api only", "check health",
                              "restore previous image on failure"]}
        with self.lock():
            self.validate_paths()
            current_id = self.verify_running(current)
            config_sha256 = self.verify_configured_current(current_id)
            record = self.start_record(current, target, config_sha256)
            switched = False
            try:
                target_id = self.inspect_image(target, required=False, custom=True)
                if target_id is None:
                    if target["image"].startswith("sha256:"):
                        raise DeploymentError("Local image ID is missing; load the prepared image first")
                    self.run(["docker", "pull", target["image"]], timeout=900)
                    target_id = self.inspect_image(target, custom=True)
                record["image_ids"] = {"current": current_id, "target": target_id}
                record["backup"] = self.backup()
                # Detect changes made by operators or external upgrade tools during backup.
                self.verify_running(current)
                if record["compose_sha256"] != {p.name: file_hash(p) for p in self.files}:
                    raise DeploymentError("Compose configuration changed during preparation")
                if self.verify_configured_current(current_id) != config_sha256:
                    raise DeploymentError("Resolved configuration changed during preparation")
                if is_reviewed_rename(current, target):
                    validate_rename_state(self.rename_database_state())
                record["status"] = "switching"
                self.save_record(record)
                switched = True
                self.install(target, record)
                self.wait_healthy(target_id)
                record["status"] = "succeeded"
                self.save_record(record)
                atomic_write(self.state / "current-manifest.json", canonical(target))
                return {"mode": "apply", "status": "succeeded", "transaction": record["id"]}
            except (Exception, KeyboardInterrupt) as error:
                record["error"] = str(error) if isinstance(error, DeploymentError) else type(error).__name__
                record["status"] = "failed_before_switch"
                if switched:
                    record["status"] = "rolling_back"
                    self.save_record(record)
                    try:
                        self.restore(record)
                        self.wait_healthy(current_id)
                        record["status"] = "rolled_back"
                        atomic_write(self.state / "current-manifest.json", canonical(current))
                    except (Exception, KeyboardInterrupt) as rollback_error:
                        record["status"] = "rollback_failed"
                        record["rollback_error"] = (str(rollback_error) if isinstance(rollback_error, DeploymentError)
                                                    else type(rollback_error).__name__)
                self.save_record(record)
                raise DeploymentError("Upgrade failed; status=%s; transaction=%s" %
                                      (record["status"], record["id"])) from error

    def load_record(self, transaction_id):
        if not TRANSACTION.fullmatch(transaction_id):
            raise DeploymentError("Invalid transaction ID")
        self.transaction_path = self.state / "transactions" / transaction_id
        if self.transaction_path.is_symlink() or self.transaction_path.parent.is_symlink():
            raise DeploymentError("Transaction paths must not be symbolic links")
        record_path = self.transaction_path / "record.json"
        checksum_path = self.transaction_path / "record.sha256"
        if record_path.is_symlink() or checksum_path.is_symlink():
            raise DeploymentError("Transaction files must not be symbolic links")
        try:
            data = record_path.read_bytes()
            expected = checksum_path.read_text(encoding="ascii").strip()
        except OSError as error:
            raise DeploymentError("Cannot read transaction record") from error
        if sha256(data) != expected:
            raise DeploymentError("Transaction metadata checksum mismatch")
        record = read_json(record_path)
        if (not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("id") != transaction_id or
                record.get("project") != str(self.project) or record.get("service") != self.service):
            raise DeploymentError("Transaction metadata does not match this deployment")
        if record.get("status") != "succeeded":
            raise DeploymentError("Only a successful upgrade can be manually rolled back")
        current, target = manifest(record.get("current")), manifest(record.get("target"))
        self.compatible(current, target)
        if record.get("compose_sha256") != {p.name: file_hash(p) for p in self.files}:
            raise DeploymentError("Compose configuration changed since this upgrade")
        if (not isinstance(record.get("current_config_sha256"), str) or
                not HEX64.fullmatch(record["current_config_sha256"]) or
                sha256(canonical(self.configuration())) != record.get("target_config_sha256")):
            raise DeploymentError("Resolved configuration changed since this upgrade")
        if (not self.override.is_file() or file_hash(self.override) != record.get("target_override_sha256") or
                record["target_override_sha256"] != sha256(self.override_bytes(target))):
            raise DeploymentError("Current image override does not match this transaction")
        ids = record.get("image_ids")
        if not isinstance(ids, dict) or set(ids) != {"current", "target"}:
            raise DeploymentError("Transaction is missing verified image IDs")
        for key, release in (("current", current), ("target", target)):
            if self.inspect_image(release, custom=(key == "target")) != ids[key]:
                raise DeploymentError("Rollback image ID differs from recorded image")
        self.verify_running(target, custom=True, healthy=False, allow_stopped=True)
        if is_reviewed_rename(current, target):
            validate_rename_state(self.rename_database_state(), rollback=True)
        previous = record.get("previous_override", "missing")
        if previous is not None:
            try:
                data = base64.b64decode(previous["base64"], validate=True)
                parsed = json.loads(data)
                previous_ref = parsed["services"][self.service]["image"]
            except (ValueError, KeyError, TypeError) as error:
                raise DeploymentError("Invalid previous override metadata") from error
            allowed = (self.override_value(previous_ref), {"services": {self.service: {"image": previous_ref}}})
            if (sha256(data) != previous.get("sha256") or parsed not in allowed or
                    not isinstance(previous_ref, str) or not IMAGE.fullmatch(previous_ref)):
                raise DeploymentError("Previous override failed validation")
            inspected = self.run(["docker", "image", "inspect", "--format", "{{.Id}}", previous_ref])
            if inspected.stdout.strip() != ids["current"]:
                raise DeploymentError("Previous override image differs from the recorded rollback image")
        return record

    def rollback(self, transaction_id, *, apply=False):
        self.validate_paths()
        if not apply:
            record = self.load_record(transaction_id)
            return {"mode": "dry-run", "action": "rollback", "transaction": transaction_id,
                    "target": record["current"]["image"], "service": self.service}
        with self.lock():
            self.validate_paths()
            record = self.load_record(transaction_id)
            record["status"] = "manual_rolling_back"
            self.save_record(record)
            try:
                self.restore(record)
                self.wait_healthy(record["image_ids"]["current"])
                record["status"] = "manual_rolled_back"
                self.save_record(record)
                atomic_write(self.state / "current-manifest.json", canonical(record["current"]))
                return {"mode": "apply", "status": "manual_rolled_back", "transaction": transaction_id}
            except (Exception, KeyboardInterrupt) as error:
                record["status"] = "manual_rollback_failed"
                record["rollback_error"] = str(error) if isinstance(error, DeploymentError) else type(error).__name__
                self.save_record(record)
                raise DeploymentError("Manual rollback failed; transaction=" + transaction_id) from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("upgrade", "rollback"))
    parser.add_argument("--project-dir", default="/opt/llmrelay")
    parser.add_argument("--manifest", type=Path, help="Prepared target release manifest")
    parser.add_argument("--current-manifest", type=Path, help="Verified currently running release manifest")
    parser.add_argument("--transaction", help="Successful transaction ID to roll back")
    parser.add_argument("--apply", action="store_true", help="Perform changes; default is read-only dry-run")
    parser.add_argument("--health-timeout", type=int, default=120)
    args = parser.parse_args(argv)
    if not 10 <= args.health_timeout <= 600:
        parser.error("--health-timeout must be between 10 and 600 seconds")
    manager = Deployment(args.project_dir, health_timeout=args.health_timeout)
    previous_handlers = {}

    def interrupted(signum, frame):
        raise KeyboardInterrupt("Deployment interrupted")

    if args.apply and os.name == "posix":
        for name in ("SIGTERM", "SIGHUP"):
            signum = getattr(signal, name)
            previous_handlers[signum] = signal.signal(signum, interrupted)
    try:
        if args.action == "upgrade":
            if not args.manifest or args.transaction:
                parser.error("upgrade requires --manifest and does not accept --transaction")
            current_path = args.current_manifest or manager.state / "current-manifest.json"
            result = manager.upgrade(read_json(current_path), read_json(args.manifest), apply=args.apply)
        else:
            if not args.transaction or args.manifest or args.current_manifest:
                parser.error("rollback requires --transaction and does not accept manifests")
            result = manager.rollback(args.transaction, apply=args.apply)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except DeploymentError as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("ERROR: Interrupted before a deployment transaction started", file=sys.stderr)
        return 130
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    sys.exit(main())
