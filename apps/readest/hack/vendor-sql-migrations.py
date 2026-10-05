#!/usr/bin/env python3
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_URL = "https://github.com/readest/readest.git"
DB_SUBPATH = "docker/volumes/db"
MIGRATION_RE = re.compile(r"^\d+_.+\.sql$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

SCRIPT_DIR = Path(__file__).resolve().parent
CHART_DIR = SCRIPT_DIR.parent
SQL_DIR = CHART_DIR / "files" / "sql"
VALUES_FILE = CHART_DIR / "values.yaml"


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def read_image_tag():
    text = VALUES_FILE.read_text()
    for m in re.finditer(
        r"#\s*renovate:.*depName=ghcr\.io/readest/readest\s*\n\s*repository:.*\n\s*tag:\s*(\S+)",
        text,
    ):
        return m.group(1).strip().strip("\"'")
    fail("cannot parse readest image tag from values.yaml")


def resolve_tag(arg):
    tag = arg if arg else read_image_tag()
    if not VERSION_RE.match(tag):
        fail(f"invalid version format '{tag}', expected digits.digits.digits")
    return tag


def run_git(args, env):
    subprocess.run(args, check=True, env=env, capture_output=True, text=True)


def sparse_checkout(tag, repo_dir):
    ref = tag if tag.startswith("v") else f"v{tag}"
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ADVICE="0")
    base = ["git", "clone", "--quiet", "--depth", "1", "--sparse",
            "--branch", ref, REPO_URL, str(repo_dir)]
    attempts = [base[:3] + ["--filter=blob:none"] + base[3:], base]
    last = None
    for cmd in attempts:
        try:
            run_git(cmd, env)
            last = None
            break
        except subprocess.CalledProcessError as e:
            last = e
            shutil.rmtree(repo_dir, ignore_errors=True)
    if last is not None:
        fail(f"git clone of {ref} failed: {last.stderr.strip() or last.stdout.strip()}")
    try:
        run_git(["git", "-C", str(repo_dir), "sparse-checkout", "set", DB_SUBPATH], env)
    except subprocess.CalledProcessError as e:
        fail(f"git sparse-checkout failed: {e.stderr.strip() or e.stdout.strip()}")
    return repo_dir / DB_SUBPATH


def collect_sql(tag):
    files = {}
    tmp = Path(tempfile.mkdtemp(prefix="readest-src-"))
    try:
        db = sparse_checkout(tag, tmp / "repo")
        schema = db / "init" / "schema.sql"
        if not schema.is_file():
            fail(f"schema.sql not found at {DB_SUBPATH}/init/schema.sql in v{tag}")
        data = schema.read_bytes()
        if not data:
            fail("schema.sql is empty")
        files["schema.sql"] = data

        mig_dir = db / "migrations"
        if not mig_dir.is_dir():
            fail(f"migrations dir not found at {DB_SUBPATH}/migrations in v{tag}")
        migs = sorted(p for p in mig_dir.iterdir() if p.is_file() and MIGRATION_RE.match(p.name))
        if not migs:
            fail(f"no migration files found at {DB_SUBPATH}/migrations in v{tag}")
        for p in migs:
            if p.name in files:
                fail(f"duplicate migration basename: {p.name}")
            d = p.read_bytes()
            if not d:
                fail(f"{p.name} is empty")
            files[p.name] = d
        return files
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def do_vendor(tag):
    print(f"Vendoring Readest SQL migrations for v{tag}...")
    files = collect_sql(tag)
    print(f"Found schema.sql + {len(files) - 1} migrations")

    SQL_DIR.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=SQL_DIR.parent, prefix=".sql-staging-"))
    backup = Path(tempfile.mkdtemp(dir=SQL_DIR.parent, prefix=".sql-backup-"))
    backup.rmdir()
    swapped = False
    try:
        for name, data in files.items():
            (staging / name).write_bytes(data)
        (staging / "VERSION").write_text(tag + "\n")
        os.chmod(staging, 0o755)
        if SQL_DIR.exists():
            SQL_DIR.rename(backup)
            swapped = True
        staging.rename(SQL_DIR)
    except BaseException:
        if swapped and not SQL_DIR.exists() and backup.exists():
            backup.rename(SQL_DIR)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        raise
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    print(f"Vendored {len(files)} SQL files + VERSION ({tag}) to {SQL_DIR}")


def do_check(tag):
    print(f"Checking vendored SQL against upstream v{tag}...")
    if not SQL_DIR.exists():
        fail(f"{SQL_DIR} does not exist")
    vf = SQL_DIR / "VERSION"
    if not vf.exists():
        fail("VERSION file missing")
    existing_version = vf.read_text().strip()
    if existing_version != tag:
        fail(f"vendored VERSION is {existing_version}, expected {tag}")

    files = collect_sql(tag)
    expected = set(files) | {"VERSION"}
    existing = {p.name for p in SQL_DIR.iterdir() if p.is_file()}
    mismatches = []
    for name, data in files.items():
        cur = SQL_DIR / name
        if not cur.exists():
            mismatches.append(f"missing: {name}")
        elif cur.read_bytes() != data:
            mismatches.append(f"changed: {name}")
    for name in existing - expected:
        mismatches.append(f"extra: {name}")
    if mismatches:
        print("ERROR: vendored SQL does not match upstream:", file=sys.stderr)
        for m in sorted(mismatches):
            print(f"  {m}", file=sys.stderr)
        sys.exit(1)
    print(f"OK: vendored SQL matches upstream v{tag} ({len(files) - 1} migrations)")


def main():
    check = "--check" in sys.argv[1:]
    pos = [a for a in sys.argv[1:] if a != "--check"]
    tag = resolve_tag(pos[0] if pos else None)
    do_check(tag) if check else do_vendor(tag)


if __name__ == "__main__":
    main()
