#!/usr/bin/env python3
"""Verify artifact integrity of a benchmark run directory.

Usage::

    python hack/verify_artifacts.py <run-dir> [--report]

By default verifies the benchmark stage (outputs_manifest.json with 3 files).
With --report, verifies the report stage (report_outputs_manifest.json with 2 files).

Exits 0 on success, 1 on verification failure, 2 on usage error.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running from the project root without installation.
_project_root = Path(__file__).resolve().parent.parent
_src = _project_root / "src"
if _src.is_dir():
    sys.path.insert(0, str(_src))

from energy_baseline.artifact_integrity import (  # noqa: E402
    BENCHMARK_MANIFEST_NAME,
    BENCHMARK_REQUIRED_FILES,
    REPORT_MANIFEST_NAME,
    REPORT_REQUIRED_FILES,
    ArtifactIntegrityError,
    verify_run_outputs,
)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    if len(args) < 1 or len(args) > 2:
        print(f"Usage: {sys.argv[0]} <run-dir> [--report]", file=sys.stderr)
        return 2

    run_dir = Path(args[0]).expanduser()
    report_stage = len(args) == 2 and args[1] == "--report"

    if len(args) == 2 and args[1] != "--report":
        print(f"Unknown option: {args[1]}", file=sys.stderr)
        print(f"Usage: {sys.argv[0]} <run-dir> [--report]", file=sys.stderr)
        return 2

    if not run_dir.is_dir():
        print(f"Error: {run_dir} is not a directory", file=sys.stderr)
        return 2

    if report_stage:
        required_files = REPORT_REQUIRED_FILES
        manifest_name = REPORT_MANIFEST_NAME
        stage = "report"
    else:
        required_files = BENCHMARK_REQUIRED_FILES
        manifest_name = BENCHMARK_MANIFEST_NAME
        stage = "benchmark"

    try:
        result = verify_run_outputs(run_dir, required_files, manifest_name)
    except ArtifactIntegrityError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    if result.ok:
        print(f"OK: {stage} stage — {len(result.files)} artifact(s) verified in {result.run_dir}")
        for fv in result.files:
            print(f"  {fv.filename}: sha256={fv.actual_sha256[:12]}  bytes={fv.actual_bytes}")
        if result.unexpected_files:
            print(f"  (unexpected files: {', '.join(result.unexpected_files)})")
        return 0

    print(f"FAIL: {stage} stage verification failed for {result.run_dir}", file=sys.stderr)
    if result.missing_required:
        print(f"  missing: {', '.join(result.missing_required)}", file=sys.stderr)
    if result.hash_mismatches:
        print(f"  hash drift: {', '.join(result.hash_mismatches)}", file=sys.stderr)
    if result.size_mismatches:
        print(f"  size drift: {', '.join(result.size_mismatches)}", file=sys.stderr)

    # Print detail for each file
    for fv in result.files:
        status = "OK" if fv.ok else "FAIL"
        print(f"  [{status}] {fv.filename}", file=sys.stderr)
        if not fv.exists:
            print("    missing", file=sys.stderr)
        else:
            if not fv.hash_ok:
                print(
                    f"    hash: expected={fv.expected_sha256[:12]}  "
                    f"actual={fv.actual_sha256[:12] if fv.actual_sha256 else 'N/A'}",
                    file=sys.stderr,
                )
            if not fv.size_ok:
                print(
                    f"    size: expected={fv.expected_bytes}  actual={fv.actual_bytes}",
                    file=sys.stderr,
                )

    return 1


if __name__ == "__main__":
    sys.exit(main())
