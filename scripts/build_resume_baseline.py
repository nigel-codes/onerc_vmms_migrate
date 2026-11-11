"""Reproduce the non-executable fingerprint baseline for a released receiver.

Run from the repository root. No site settings, captures or credentials are read.
"""

import gzip
import hashlib
from pathlib import Path, PurePosixPath
import subprocess

COMMIT = "6a3d44e"
VERSION = "0.7.1"

if __name__ == "__main__":
	commit = subprocess.check_output(["git", "rev-parse", COMMIT], text=True).strip()
	names = subprocess.check_output(
		["git", "ls-tree", "-r", "--name-only", commit, "onerc_vmms_migrate"], text=True
	).splitlines()
	files = sorted(
		(
			name
			for name in names
			if PurePosixPath(name).suffix in (".py", ".json") and "tests" not in PurePosixPath(name).parts
		),
		key=lambda name: PurePosixPath(name).parts,
	)
	raw = b"".join(subprocess.check_output(["git", "show", commit + ":" + name]) for name in files)
	target = Path("onerc_vmms_migrate/resume_baselines") / (VERSION + "-code.gz")
	target.parent.mkdir(exist_ok=True)
	target.write_bytes(gzip.compress(raw, mtime=0))
	print(
		{
			"version": VERSION,
			"commit": commit,
			"files": len(files),
			"bytes": len(raw),
			"sha256": hashlib.sha256(raw).hexdigest(),
			"artifact": str(target),
		}
	)
