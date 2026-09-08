#!/usr/bin/env python3
"""Back up only a generated rehearsal project; no production path defaults."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import uuid


def main():
    project = Path(sys.argv[1]).resolve()
    marker = json.loads((project / ".rehearsal.json").read_text())
    assert marker["project"].startswith("s2rehearsal-")
    assert marker["directory"] == str(project)
    command = ["docker", "compose", "--project-directory", str(project)]
    for name in ("docker-compose.yml", "docker-compose.override.yml", "docker-compose.availability.yml"):
        if (project / name).exists():
            command += ["-f", str(project / name)]
    snapshot = project / "backups" / uuid.uuid4().hex
    snapshot.mkdir(parents=True, mode=0o700)
    with (snapshot / "database.dump").open("wb") as stream:
        subprocess.run(command + ["exec", "-T", "postgres", "pg_dump", "-U", "rehearsal", "-d", "rehearsal", "-Fc"],
                       stdout=stream, stderr=subprocess.PIPE, check=True, timeout=120)
    with (snapshot / "database.dump").open("rb") as source, (snapshot / "database-contents.txt").open("wb") as listing:
        subprocess.run(command + ["exec", "-T", "postgres", "pg_restore", "--list"],
                       stdin=source, stdout=listing, stderr=subprocess.PIPE, check=True, timeout=120)
    with tarfile.open(snapshot / "runtime.tar.gz", "w:gz") as archive:
        archive.add(project / "data", arcname="data")
    (snapshot / "SHA256SUMS").write_text("".join(
        hashlib.sha256((snapshot / name).read_bytes()).hexdigest() + "  " + name + "\n"
        for name in ("database.dump", "runtime.tar.gz")))


if __name__ == "__main__":
    main()
