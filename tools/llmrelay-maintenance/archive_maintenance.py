#!/usr/bin/env python3
"""Archive only deployment metadata and verified patches; never deploy anything."""

import argparse
import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile

import build
import deploy


MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_INPUT_BYTES = 100 * 1024 * 1024
RECORD_FIELDS = {
    "schema_version", "id", "project", "service", "status", "current", "target",
    "compose_sha256", "target_override_sha256", "current_config_sha256", "previous_override",
    "image_ids", "backup", "target_config_sha256", "error", "rollback_error", "schema_compatibility",
}


def regular_bytes(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise deploy.DeploymentError("Expected a small regular file: " + str(path))
    return path.read_bytes()


def checked_json(path):
    data = regular_bytes(path)
    try:
        value = json.loads(data)
    except ValueError as error:
        raise deploy.DeploymentError("Invalid metadata JSON: " + str(path)) from error
    if not isinstance(value, dict):
        raise deploy.DeploymentError("Expected a metadata object: " + str(path))
    return data, value


def check_override(data):
    try:
        value = json.loads(data)
        image = value["services"]["sub2api"]["image"]
        expected = {"services": {"sub2api": {"image": image}}}
        enhanced = deploy.Deployment.override_value(image)
        if value not in (expected, enhanced) or not deploy.IMAGE.fullmatch(image):
            raise ValueError("Unexpected override settings")
    except (ValueError, TypeError, KeyError) as error:
        raise deploy.DeploymentError("Override may contain unexpected settings; archive refused") from error


@contextlib.contextmanager
def shared_lock(state):
    lock_path = state / "deployment.lock"
    if os.name != "posix" or not lock_path.exists():
        yield
        return
    if lock_path.is_symlink():
        raise deploy.DeploymentError("Deployment lock must not be a symbolic link")
    import fcntl
    with lock_path.open("rb") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise deploy.DeploymentError("Deployment is still running; archive after completion") from error
        yield


def collect(project, maintenance, release_path):
    project, maintenance = Path(project).resolve(), Path(maintenance).resolve()
    state = project / ".sub2api-maintenance"
    transactions = state / "transactions"
    patches_dir = maintenance / "patches"
    for path in (state, transactions, patches_dir):
        if path.is_symlink() or not path.is_dir():
            raise deploy.DeploymentError("Expected a local directory: " + str(path))
    members = {}
    override = project / "docker-compose.availability.yml"
    if override.exists():
        data = regular_bytes(override)
        check_override(data)
        members["deployment/docker-compose.availability.yml"] = data
    current_path = state / "current-manifest.json"
    current_data, current = checked_json(current_path)
    current = deploy.manifest(current)
    if json.loads(current_data) != current:
        raise deploy.DeploymentError("Current manifest has unexpected fields")
    members["deployment/.sub2api-maintenance/current-manifest.json"] = current_data

    series_data, patch_manifest = checked_json(patches_dir / "series.json")
    if set(patch_manifest) - {"schema_version", "upstream", "upstream_revision", "base_revision", "patches"}:
        raise deploy.DeploymentError("Patch series contains unexpected metadata fields")
    if not isinstance(patch_manifest.get("patches"), list):
        raise deploy.DeploymentError("Invalid patch series")
    for entry in patch_manifest["patches"]:
        if (not isinstance(entry, dict) or set(entry) != {"file", "sha256"} or
                not isinstance(entry["file"], str) or Path(entry["file"]).name != entry["file"]):
            raise deploy.DeploymentError("Invalid patch entry")
        regular_bytes(patches_dir / entry["file"])
    patch_manifest, patch_paths = build.patch_set(maintenance)
    members["maintenance/patches/series.json"] = series_data
    for path in patch_paths:
        if path.is_symlink():
            raise deploy.DeploymentError("Patch file must not be a symbolic link")
        members["maintenance/patches/" + path.name] = regular_bytes(path)

    _, release = checked_json(Path(release_path))
    normalized_release = deploy.manifest(release)
    if normalized_release != current:
        raise deploy.DeploymentError("Release manifest does not match the currently recorded image")
    if release.get("patches") != patch_manifest["patches"] or release.get("tests") != "passed":
        raise deploy.DeploymentError("Release must record successful tests for this exact patch series")
    # Only retain release fields needed to identify and reproduce the deployment.
    normalized_release.update({"patches": release["patches"], "tests": "passed"})
    for key in ("version", "tag", "tested_at"):
        if key in release:
            if not isinstance(release[key], str):
                raise deploy.DeploymentError("Invalid release field: " + key)
            normalized_release[key] = release[key]
    members["maintenance/release.json"] = deploy.canonical(normalized_release)

    count = 0
    for folder in sorted(transactions.iterdir()):
        if folder.is_symlink() or not folder.is_dir() or not deploy.TRANSACTION.fullmatch(folder.name):
            raise deploy.DeploymentError("Unexpected transaction path: " + str(folder))
        data, record = checked_json(folder / "record.json")
        checksum = regular_bytes(folder / "record.sha256")
        if deploy.sha256(data) != checksum.decode("ascii").strip():
            raise deploy.DeploymentError("Transaction checksum mismatch: " + folder.name)
        if (set(record) - RECORD_FIELDS or record.get("id") != folder.name or
                record.get("project") != str(project) or record.get("service") != "sub2api"):
            raise deploy.DeploymentError("Unexpected transaction metadata: " + folder.name)
        for key in ("current", "target"):
            if deploy.manifest(record.get(key)) != record[key]:
                raise deploy.DeploymentError("Unexpected transaction release fields: " + folder.name)
        if ("schema_compatibility" in record and
                record["schema_compatibility"] != deploy.Deployment.compatible(record["current"], record["target"])):
            raise deploy.DeploymentError("Unexpected schema compatibility metadata: " + folder.name)
        if "backup" in record:
            backup = record["backup"]
            if (not isinstance(backup, dict) or set(backup) != {"path", "sha256"} or
                    not isinstance(backup["path"], str) or not isinstance(backup["sha256"], dict) or
                    set(backup["sha256"]) != {"database.dump", "runtime.tar.gz"} or
                    any(not isinstance(value, str) or not deploy.HEX64.fullmatch(value)
                        for value in backup["sha256"].values())):
                raise deploy.DeploymentError("Unexpected transaction backup metadata: " + folder.name)
        previous = record.get("previous_override")
        if previous is not None:
            import base64
            try:
                if not isinstance(previous, dict) or set(previous) != {"base64", "sha256"}:
                    raise ValueError("Unexpected previous override fields")
                previous_data = base64.b64decode(previous["base64"], validate=True)
                if deploy.sha256(previous_data) != previous["sha256"]:
                    raise ValueError("Checksum mismatch")
                check_override(previous_data)
            except (KeyError, ValueError, TypeError) as error:
                raise deploy.DeploymentError("Invalid previous override: " + folder.name) from error
        prefix = "deployment/.sub2api-maintenance/transactions/" + folder.name + "/"
        members[prefix + "record.json"] = data
        members[prefix + "record.sha256"] = checksum
        count += 1
    if not count:
        raise deploy.DeploymentError("No deployment transaction was found")
    if sum(len(data) for data in members.values()) > MAX_ARCHIVE_INPUT_BYTES:
        raise deploy.DeploymentError("Metadata exceeds archive size limit")
    inventory = {"schema_version": 1,
                 "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 "files": {name: {"sha256": deploy.sha256(data), "bytes": len(data)}
                           for name, data in sorted(members.items())}}
    members["ARCHIVE_MANIFEST.json"] = deploy.canonical(inventory)
    return members


def write_archive(output, members):
    output = Path(output).absolute()
    checksum_path = output.with_name(output.name + ".sha256")
    if output.exists() or output.is_symlink() or checksum_path.exists() or checksum_path.is_symlink():
        raise deploy.DeploymentError("Archive or checksum destination already exists")
    if not output.parent.is_dir():
        raise deploy.DeploymentError("Archive parent directory must already exist")
    descriptor, temporary = tempfile.mkstemp(prefix=".maintenance-", dir=output.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                for name, data in sorted(members.items()):
                    entry = tarfile.TarInfo(name)
                    entry.size, entry.mode, entry.mtime = len(data), 0o600, 0
                    archive.addfile(entry, io.BytesIO(data))
            stream.flush()
            os.fsync(stream.fileno())
        # Hard-link publication refuses to overwrite a concurrently created file.
        os.link(temporary, output)
        checksum = deploy.file_hash(output)
        with checksum_path.open("x", encoding="ascii") as stream:
            stream.write(checksum + "  " + output.name + "\n")
        os.chmod(checksum_path, 0o600)
        return checksum
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=Path("/opt/llmrelay"))
    parser.add_argument("--maintenance-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Write a new tar.gz; omission is read-only preview")
    args = parser.parse_args(argv)
    try:
        with shared_lock(args.project_dir.resolve() / ".sub2api-maintenance"):
            members = collect(args.project_dir, args.maintenance_dir, args.release)
            if args.output:
                checksum = write_archive(args.output, members)
                print(json.dumps({"archive": str(args.output.absolute()), "sha256": checksum,
                                  "files": len(members)}, indent=2))
            else:
                print(json.dumps({"mode": "dry-run", "files": sorted(members),
                                  "bytes": sum(len(data) for data in members.values())}, indent=2))
        return 0
    except (deploy.DeploymentError, OSError, ValueError) as error:
        print("Archive stopped: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
