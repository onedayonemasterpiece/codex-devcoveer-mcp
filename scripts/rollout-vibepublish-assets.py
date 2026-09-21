#!/usr/bin/env python3
"""Guarded single-file VibePublish assets.py rollout for a dirty recovery checkout."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import tempfile

REPO = Path("/home/dev/projects/vibepublish")
TARGET = REPO / "social_operations/assets.py"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(*args: str) -> bytes:
    return subprocess.run(
        ["/usr/bin/git", "-C", str(REPO), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--expected-current-sha256", required=True)
    parser.add_argument("--expected-new-sha256", required=True)
    args = parser.parse_args()

    if len(args.commit) != 40 or any(c not in "0123456789abcdef" for c in args.commit):
        raise SystemExit("commit must be exact 40-hex")
    for value in (args.expected_current_sha256, args.expected_new_sha256):
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise SystemExit("sha256 arguments must be exact lowercase 64-hex")

    current = TARGET.read_bytes()
    current_sha = sha256(current)
    if current_sha == args.expected_new_sha256:
        print(f"already_deployed sha256={current_sha}")
        return 0
    if current_sha != args.expected_current_sha256:
        raise SystemExit(
            f"refusing unexpected target sha256={current_sha}; "
            f"expected {args.expected_current_sha256}"
        )

    git("fetch", "--quiet", "--no-tags", "origin", args.commit)
    source = git("show", f"{args.commit}:social_operations/assets.py")
    source_sha = sha256(source)
    if source_sha != args.expected_new_sha256:
        raise SystemExit(
            f"remote source sha256 mismatch={source_sha}; expected {args.expected_new_sha256}"
        )
    compile(source.decode("utf-8"), str(TARGET), "exec")

    st = TARGET.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        raise SystemExit("target is not an owner-controlled regular file")

    fd, tmp_name = tempfile.mkstemp(prefix=".assets.py.", dir=TARGET.parent)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, stat.S_IMODE(st.st_mode))
        with os.fdopen(fd, "wb") as out:
            out.write(source)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, TARGET)
        directory_fd = os.open(TARGET.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        tmp.unlink(missing_ok=True)

    deployed_sha = sha256(TARGET.read_bytes())
    if deployed_sha != args.expected_new_sha256:
        raise SystemExit("post-write sha256 verification failed")
    print(
        f"deployed commit={args.commit} path=social_operations/assets.py "
        f"sha256={deployed_sha}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
