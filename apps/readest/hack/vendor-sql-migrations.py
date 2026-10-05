#!/usr/bin/env python3
import json
import os
import re
import shutil
import ssl
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = "readest/readest"
DB_PATH = "docker/volumes/db"
SCHEMA_PATH = f"{DB_PATH}/init/schema.sql"
MIGRATIONS_PATH = f"{DB_PATH}/migrations"
MIGRATION_RE = re.compile(r"^\d+_.+\.sql$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

SCRIPT_DIR = Path(__file__).resolve().parent
CHART_DIR = SCRIPT_DIR.parent
SQL_DIR = CHART_DIR / "files" / "sql"
VALUES_FILE = CHART_DIR / "values.yaml"


def read_image_tag():
    text = VALUES_FILE.read_text()
    for line_match in re.finditer(
        r"#\s*renovate:.*depName=ghcr\.io/readest/readest\s*\n\s*repository:.*\n\s*tag:\s*(\S+)",
        text,
    ):
        return line_match.group(1).strip().strip("\"'")
    print("ERROR: cannot parse readest image tag from values.yaml", file=sys.stderr)
    sys.exit(1)


def github_get(url):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "homelab-vendor", "Accept": "application/vnd.github+json"})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_raw_bytes(url):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "homelab-vendor"})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
        return resp.read()


def fetch_tree(ref):
    url = f"https://api.github.com/repos/{REPO}/git/trees/{ref}?recursive=1"
    data = github_get(url)
    if data.get("truncated"):
        print(f"ERROR: tree for {ref} is truncated, cannot enumerate all files", file=sys.stderr)
        sys.exit(1)
    return [item["path"] for item in data.get("tree", []) if item["type"] == "blob"]


def fetch_and_validate(ref, tree_path):
    url = f"https://raw.githubusercontent.com/{REPO}/{ref}/{tree_path}"
    content = fetch_raw_bytes(url)
    if not content:
        print(f"ERROR: {tree_path} is empty at {ref}", file=sys.stderr)
        sys.exit(1)
    return content


def resolve_tag(arg):
    tag = arg if arg else read_image_tag()
    if not VERSION_RE.match(tag):
        print(f"ERROR: invalid version format '{tag}', expected digits.digits.digits", file=sys.stderr)
        sys.exit(1)
    return tag


def collect_file_manifest(ref):
    all_paths = fetch_tree(ref)

    if not any(p == SCHEMA_PATH for p in all_paths):
        print(f"ERROR: schema.sql not found at {SCHEMA_PATH} in {ref}", file=sys.stderr)
        sys.exit(1)

    migration_paths = sorted(
        p for p in all_paths
        if p.startswith(f"{MIGRATIONS_PATH}/") and MIGRATION_RE.match(p.split("/")[-1])
    )
    if not migration_paths:
        print(f"ERROR: no migration files found at {MIGRATIONS_PATH} in {ref}", file=sys.stderr)
        sys.exit(1)

    basenames = [p.split("/")[-1] for p in migration_paths]
    if len(basenames) != len(set(basenames)):
        dupes = {b for b in basenames if basenames.count(b) > 1}
        print(f"ERROR: duplicate migration basenames: {dupes}", file=sys.stderr)
        sys.exit(1)

    return migration_paths


def download_all(ref, migration_paths, dest_dir):
    schema_content = fetch_and_validate(ref, SCHEMA_PATH)
    (dest_dir / "schema.sql").write_bytes(schema_content)

    for mp in migration_paths:
        bn = mp.split("/")[-1]
        content = fetch_and_validate(ref, mp)
        (dest_dir / bn).write_bytes(content)

    (dest_dir / "VERSION").write_text(ref.lstrip("v") + "\n")


def do_vendor(tag):
    ref = f"v{tag}"
    print(f"Vendoring Readest SQL migrations for {ref}...")

    migration_paths = collect_file_manifest(ref)
    print(f"Found schema.sql + {len(migration_paths)} migrations")

    staging_dir = Path(tempfile.mkdtemp(dir=SQL_DIR.parent, prefix=".sql-staging-"))
    backup_dir = Path(tempfile.mkdtemp(dir=SQL_DIR.parent, prefix=".sql-backup-"))
    backup_dir.rmdir()
    swapped = False
    try:
        download_all(ref, migration_paths, staging_dir)

        if SQL_DIR.exists():
            SQL_DIR.rename(backup_dir)
            swapped = True
        staging_dir.rename(SQL_DIR)
    except BaseException:
        if swapped and not SQL_DIR.exists() and backup_dir.exists():
            backup_dir.rename(SQL_DIR)
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        raise

    if backup_dir.exists():
        shutil.rmtree(backup_dir)

    print(f"Vendored {len(migration_paths) + 1} SQL files + VERSION ({tag}) to {SQL_DIR}")


def do_check(tag):
    ref = f"v{tag}"
    print(f"Checking vendored SQL against upstream {ref}...")

    if not SQL_DIR.exists():
        print(f"ERROR: {SQL_DIR} does not exist", file=sys.stderr)
        sys.exit(1)

    existing_version_file = SQL_DIR / "VERSION"
    if not existing_version_file.exists():
        print("ERROR: VERSION file missing", file=sys.stderr)
        sys.exit(1)

    existing_version = existing_version_file.read_text().strip()
    if existing_version != tag:
        print(f"ERROR: vendored VERSION is {existing_version}, expected {tag}", file=sys.stderr)
        sys.exit(1)

    migration_paths = collect_file_manifest(ref)

    staging_dir = Path(tempfile.mkdtemp(dir=SQL_DIR.parent, prefix=".sql-check-"))
    try:
        download_all(ref, migration_paths, staging_dir)

        mismatches = []
        for staged_file in sorted(staging_dir.iterdir()):
            existing_file = SQL_DIR / staged_file.name
            if not existing_file.exists():
                mismatches.append(f"missing: {staged_file.name}")
            elif staged_file.read_bytes() != existing_file.read_bytes():
                mismatches.append(f"changed: {staged_file.name}")

        for existing_file in SQL_DIR.iterdir():
            if existing_file.is_file() and not (staging_dir / existing_file.name).exists():
                mismatches.append(f"extra: {existing_file.name}")

        if mismatches:
            print("ERROR: vendored SQL does not match upstream:", file=sys.stderr)
            for m in mismatches:
                print(f"  {m}", file=sys.stderr)
            sys.exit(1)
    finally:
        shutil.rmtree(staging_dir)

    print(f"OK: vendored SQL matches upstream {ref} ({len(migration_paths)} migrations)")


def main():
    check_mode = False
    positional = []
    for arg in sys.argv[1:]:
        if arg == "--check":
            check_mode = True
        else:
            positional.append(arg)

    tag = resolve_tag(positional[0] if positional else None)

    if check_mode:
        do_check(tag)
    else:
        do_vendor(tag)


if __name__ == "__main__":
    main()
