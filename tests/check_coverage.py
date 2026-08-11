"""Fail the build when any single file falls below the coverage floor.

A project total hides a hole: one thoroughly covered 1500-line script can carry
a barely-touched 300-line one well past 90% overall, and the barely-touched one
is where the next bug is. `coverage report` has no per-file threshold, so the
check lives here, and both `make coverage` and CI run this same file rather
than two thresholds that can drift apart.

    python3 tests/check_coverage.py [--min 90]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys


def measured(rcfile: str = "pyproject.toml") -> dict[str, float]:
    """{path: percent covered} straight from coverage's own JSON report.

    Shelling out to coverage rather than importing it: the number reported to a
    human by `coverage report` and the number gating the build then come from
    one implementation, and cannot disagree.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "coverage", "json", f"--rcfile={rcfile}", "-o", "-"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"check_coverage: `coverage json` failed:\n{proc.stderr.strip()}")
    report = json.loads(proc.stdout)
    return {
        path: data["summary"]["percent_covered"] for path, data in report.get("files", {}).items()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--min", type=float, default=90.0, help="floor per file (default: 90)")
    args = parser.parse_args()

    files = measured()
    if not files:
        # Almost always MP_COVER_SUBPROCESS unset: this suite runs the scripts
        # as subprocesses, which a plain coverage run cannot see at all.
        sys.exit("check_coverage: no files in the coverage report -- did the suite run?")

    worst = sorted(files.items(), key=lambda kv: kv[1])
    below = [(path, pct) for path, pct in worst if pct < args.min]

    width = max(len(path) for path in files)
    for path, pct in worst:
        mark = "FAIL" if pct < args.min else "ok"
        print(f"  {mark:<4} {path:<{width}}  {pct:5.1f}%")

    if below:
        print(
            f"\ncheck_coverage: {len(below)} file(s) below {args.min:g}%:",
            ", ".join(path for path, _ in below),
            file=sys.stderr,
        )
        return 1
    print(f"\ncheck_coverage: every file at or above {args.min:g}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
