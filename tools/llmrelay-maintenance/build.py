#!/usr/bin/env python3
"""Apply the independent patch set, test it, and optionally build a local image."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
PACKAGES = ("./internal/config", "./internal/service", "./internal/handler",
            "./internal/handler/admin", "./internal/repository")


def run(args, cwd=None, capture=False):
    result = subprocess.run([str(x) for x in args], cwd=cwd, check=True,
                            stdout=subprocess.PIPE if capture else None,
                            encoding="utf-8" if capture else None)
    return result.stdout.strip() if capture else None


def migration_hash(source: Path) -> str:
    """Hash names and bytes, including schema and migration execution code."""
    paths = []
    for directory in ("backend/migrations", "backend/ent/schema"):
        folder = source / directory
        if not folder.is_dir():
            raise ValueError(f"Missing schema directory: {directory}")
        paths.extend(p for p in folder.rglob("*") if p.is_file()
                     and not p.name.endswith("_test.go"))
    runner = source / "backend/internal/repository/migrations_runner.go"
    if not runner.is_file():
        raise ValueError("Missing migration runner")
    paths.append(runner)
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda p: p.relative_to(source).as_posix()):
        name = path.relative_to(source).as_posix().encode("utf-8")
        data = path.read_bytes()
        digest.update(name + b"\0" + str(len(data)).encode() + b"\0" + data)
    return digest.hexdigest()


def patch_set(root: Path = ROOT):
    manifest = json.loads((root / "patches/series.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported patch manifest")
    patches = []
    for entry in manifest["patches"]:
        path = (root / "patches" / entry["file"]).resolve()
        if path.parent != (root / "patches").resolve() or path.suffix != ".patch":
            raise ValueError("Invalid patch path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"Patch checksum mismatch: {path.name}")
        patches.append(path)
    if not patches:
        raise ValueError("Empty patch set")
    return manifest, patches


def prepare(source, revision, worktree, go, log_dir):
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Use an exact 40-character upstream commit, not latest or a tag")
    if worktree.exists():
        raise ValueError("Worktree destination must not exist; failed runs are preserved")
    manifest, patches = patch_set()
    resolved = run(["git", "rev-parse", "--verify", revision + "^{commit}"], source, True)
    if resolved != revision:
        raise ValueError("Upstream revision did not resolve exactly")
    # Git creates a separate checkout. No changes to the source checkout or branch.
    run(["git", "-c", "core.autocrlf=false", "worktree", "add", "--detach", worktree, revision], source)
    before = migration_hash(worktree)
    for patch in patches:
        run(["git", "apply", "--check", "--whitespace=error", patch], worktree)
        run(["git", "apply", "--whitespace=error", patch], worktree)
    after = migration_hash(worktree)
    if after != before:
        raise ValueError("Availability patches must not modify the database schema")
    run(["git", "diff", "--check"], worktree)
    log_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(go), "test", "-count=1", "-timeout=10m", *PACKAGES]
    test_env = dict(os.environ, CGO_ENABLED="0")
    with (log_dir / "go-test.log").open("wb") as log:
        completed = subprocess.run(cmd, cwd=worktree / "backend", env=test_env,
                                   stdout=log, stderr=subprocess.STDOUT)
    if completed.returncode:
        raise RuntimeError(f"Go regression tests failed; inspect {log_dir / 'go-test.log'}")
    return {"schema_version": 1, "revision": revision, "migrations_sha256": after,
            "patches": manifest["patches"], "tests": "passed", "test_command": cmd,
            "tested_at": datetime.now(timezone.utc).isoformat()}


def validate_version(version):
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9._+-]*", version):
        raise ValueError("Version must start with MAJOR.MINOR.PATCH and contain only letters, digits, . _ + -")
    return version


def build_image(worktree, tag, report, output, version=None):
    if not re.fullmatch(r"[a-z0-9][a-z0-9./_-]*:[A-Za-z0-9_][A-Za-z0-9_.-]*", tag):
        raise ValueError("Use an explicit local image name:tag")
    if version is None:
        raise ValueError("Pass an explicit --version, e.g. 0.2.3-llmrelay.1")
    version = validate_version(version)
    patch_hash = hashlib.sha256(json.dumps(report["patches"], sort_keys=True).encode()).hexdigest()
    cmd = ["docker", "build", "--tag", tag,
           "--label", "org.opencontainers.image.revision=" + report["revision"],
           "--label", "io.sub2api.availability.migrations-sha256=" + report["migrations_sha256"],
           "--label", "io.sub2api.availability.patch-sha256=" + patch_hash,
           "--build-arg", "VERSION=" + version,
           "--build-arg", "COMMIT=" + report["revision"] + "+llmrelay", "."]
    with (output / "docker-build.log").open("wb") as log:
        completed = subprocess.run(cmd, cwd=worktree, stdout=log, stderr=subprocess.STDOUT)
    if completed.returncode:
        raise RuntimeError(f"Image build failed; inspect {output / 'docker-build.log'}")
    image = run(["docker", "image", "inspect", "--format", "{{.Id}}", tag], capture=True)
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("Docker did not return an immutable image ID")
    result = {**report, "image": image, "tag": tag, "version": version}
    (output / "release.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Local upstream Git repository")
    parser.add_argument("--revision", required=True, help="Exact upstream SHA already fetched locally")
    parser.add_argument("--worktree", type=Path, required=True, help="New isolated directory")
    parser.add_argument("--output", type=Path, required=True, help="New build report directory")
    parser.add_argument("--go", default="go")
    parser.add_argument("--image-tag", help="Also build the image with Docker; no push or deployment")
    parser.add_argument("--version", help="Explicit complete image version, e.g. 0.2.3-llmrelay.1; requires --image-tag")
    args = parser.parse_args()
    if args.version is not None:
        if not args.image_tag:
            parser.error("--version requires --image-tag")
        try:
            validate_version(args.version)
        except ValueError as exc:
            parser.error(str(exc))
    output = args.output.resolve()
    if output.exists():
        parser.error("Output directory must not exist")
    try:
        report = prepare(args.source.resolve(), args.revision, args.worktree.resolve(), args.go, output)
        (output / "test-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if args.image_tag:
            build_image(args.worktree.resolve(), args.image_tag, report, output, version=args.version)
        print(f"Tests passed. Reports: {output}")
        if not args.image_tag:
            print("No image was built. Re-run with a fresh worktree/output and --image-tag on a Docker host.")
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Build stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
