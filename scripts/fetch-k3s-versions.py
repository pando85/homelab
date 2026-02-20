#!/usr/bin/env python3
"""Fetch relevant k3s versions from GitHub releases."""

import json
import sys
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional

GITHUB_API = "https://api.github.com/repos/k3s-io/k3s/releases"


def fetch_releases(per_page: int = 100) -> list[dict]:
    """Fetch k3s releases from GitHub API."""
    url = f"{GITHUB_API}?per_page={per_page}"
    headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "k3s-version-fetcher"}

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        data = response.read().decode("utf-8")
        return json.loads(data)


def parse_version(tag: str) -> tuple[int, ...]:
    """Parse version string into tuple for comparison."""
    tag = tag.lstrip("v")
    parts = tag.split("+")
    version_parts = parts[0].split(".")
    try:
        return tuple(int(p) for p in version_parts)
    except ValueError:
        return (0,)


def get_latest_stable(releases: list[dict]) -> Optional[dict]:
    """Get the latest stable release (non-rc, non-testing)."""
    for release in releases:
        tag = release["tag_name"]
        if "rc" not in tag.lower() and "test" not in tag.lower():
            if not release.get("prerelease", False):
                return release
    return None


def get_latest_rc(releases: list[dict]) -> Optional[dict]:
    """Get the latest release candidate."""
    for release in releases:
        tag = release["tag_name"]
        if "rc" in tag.lower():
            return release
    return None


def get_versions_by_channel(releases: list[dict]) -> dict[str, list[str]]:
    """Group versions by major.minor channel."""
    channels: dict[str, list[str]] = {}

    for release in releases:
        tag = release["tag_name"]
        if release.get("prerelease", False) and "rc" not in tag.lower():
            continue

        tag = tag.lstrip("v")
        parts = tag.split(".")
        if len(parts) >= 2:
            try:
                major = int(parts[0])
                minor = int(parts[1].split("+")[0])
                channel = f"v{major}.{minor}"
                if channel not in channels:
                    channels[channel] = []
                channels[channel].append(tag)
            except ValueError:
                continue

    return channels


def format_date(date_str: str) -> str:
    """Format ISO date string to readable format."""
    dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    return dt.strftime("%Y-%m-%d")


def main():
    print("Fetching k3s releases...")
    try:
        releases = fetch_releases()
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"Error fetching releases: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(releases)} releases\n")

    latest_stable = get_latest_stable(releases)
    if latest_stable:
        print(f"Latest Stable: {latest_stable['tag_name']} ({format_date(latest_stable['published_at'])})")

    latest_rc = get_latest_rc(releases)
    if latest_rc:
        print(f"Latest RC:     {latest_rc['tag_name']} ({format_date(latest_rc['published_at'])})")

    print("\n" + "=" * 60)
    print("Latest version per channel:")
    print("=" * 60)

    channels = get_versions_by_channel(releases)
    for channel in sorted(channels.keys(), key=parse_version, reverse=True)[:10]:
        versions = channels[channel]
        latest = versions[0]
        print(f"  {channel:<10} -> {latest}")

    print("\n" + "=" * 60)
    print("All stable versions (last 15):")
    print("=" * 60)

    stable_count = 0
    for release in releases:
        tag = release["tag_name"]
        if "rc" not in tag.lower() and not release.get("prerelease", False):
            print(f"  {tag:<20} {format_date(release['published_at'])}")
            stable_count += 1
            if stable_count >= 15:
                break


if __name__ == "__main__":
    main()
