#!/usr/bin/env python3
"""Fetch GitHub stars and sync them into data/github-stars.json and README."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "github-stars.json"
README_PATH = ROOT / "README.md"
GITHUB_URL_RE = re.compile(r"https://github\.com/([^/\s]+)/([^/\s)]+?)(?:\.git)?(?:/)?$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Fetch and show pending changes without writing files.")
    mode.add_argument("--write", action="store_true", help="Fetch and write updates to data and markdown files.")
    return parser.parse_args()


def load_records() -> list[dict[str, Any]]:
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def dump_records(records: list[dict[str, Any]]) -> str:
    return json.dumps(records, ensure_ascii=False, indent=2) + "\n"


def extract_repo(url: str) -> str | None:
    match = GITHUB_URL_RE.match(url.strip())
    if not match:
        return None
    owner, repo = match.groups()
    return f"{owner}/{repo}"


def scan_readme_repos() -> set[str]:
    content = README_PATH.read_text(encoding="utf-8")
    repos: set[str] = set()
    for url in re.findall(r"https://github\.com/[^\s)]+", content):
        repo = extract_repo(url)
        if repo:
            repos.add(repo.lower())
    return repos


def fetch_repo(repo: str, token: str | None) -> dict[str, Any]:
    request = Request(
        f"https://api.github.com/repos/{repo}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "llm-agent-learning-resources-star-tracker",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def format_stars(stars: int) -> str:
    if stars >= 1000:
        value = stars / 1000
        if value.is_integer():
            return f"{int(value)}k"
        return f"{value:.1f}k"
    return str(stars)


def format_delta(delta: int | None) -> str:
    if delta is None:
        return "首次记录"
    if delta > 0:
        return f"+{delta}"
    return str(delta)


def update_history(history: list[dict[str, Any]], date_str: str, stars: int) -> list[dict[str, Any]]:
    new_history = deepcopy(history)
    if new_history and new_history[-1]["date"] == date_str:
        new_history[-1]["stars"] = stars
    else:
        new_history.append({"date": date_str, "stars": stars})
    return new_history


def replace_stars_in_table_row(line: str, record: dict[str, Any]) -> str:
    stars_pattern = re.compile(r"⭐ Stars：[^<|]+")
    replacement = f"⭐ Stars：{format_stars(record['stars'])}"

    if stars_pattern.search(line):
        return stars_pattern.sub(replacement, line, count=1)

    link_pattern = re.compile(rf"(\[{re.escape(record['name'])}\]\([^)]+\))")
    if not link_pattern.search(line):
        raise ValueError(f"Could not find resource link for {record['name']}")

    return link_pattern.sub(rf"\1<br>{replacement}", line, count=1)


def update_markdown_for_location(record: dict[str, Any], location: dict[str, str]) -> bool:
    path = ROOT / location["file"]
    content = path.read_text(encoding="utf-8").splitlines()
    row_pattern = re.compile(rf"\[{re.escape(location['heading'])}\]\([^)]+\)")

    for idx, line in enumerate(content):
        if row_pattern.search(line):
            updated_line = replace_stars_in_table_row(line, record)
            if updated_line == line:
                return False
            content[idx] = updated_line
            path.write_text("\n".join(content) + "\n", encoding="utf-8")
            return True

    raise ValueError(f"Table row for {location['heading']} not found in {location['file']}")


def sync_markdown(records: list[dict[str, Any]]) -> list[str]:
    updated_files: set[str] = set()
    for record in records:
        for location in record.get("locations", []):
            changed = update_markdown_for_location(record, location)
            if changed:
                updated_files.add(location["file"])
    return sorted(updated_files)


def main() -> int:
    args = parse_args()
    today = datetime.now(timezone.utc).date().isoformat()
    token = os.getenv("GITHUB_TOKEN")
    records = load_records()
    original_records = deepcopy(records)

    declared_repos = set()
    for record in records:
        declared_repos.add(record["repo"].lower())
        for alias in record.get("aliases", []):
            declared_repos.add(alias.lower())
    scanned_repos = scan_readme_repos()
    missing_repos = sorted(scanned_repos - declared_repos)
    if missing_repos:
        print("Missing GitHub repos in data/github-stars.json:", file=sys.stderr)
        for repo in missing_repos:
            print(f"  - {repo}", file=sys.stderr)
        return 1

    errors: list[str] = []
    for record in records:
        try:
            payload = fetch_repo(record["repo"], token)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            errors.append(f"{record['repo']}: HTTP {exc.code} {detail}")
            continue
        except URLError as exc:
            errors.append(f"{record['repo']}: {exc.reason}")
            continue

        old_stars = record.get("stars")
        new_stars = int(payload["stargazers_count"])
        canonical_repo = payload["full_name"]
        canonical_url = payload["html_url"]

        record["repo"] = canonical_repo
        record["url"] = canonical_url
        record["stars"] = new_stars
        record["delta"] = None if old_stars is None else new_stars - int(old_stars)
        record["last_updated"] = today
        record["history"] = update_history(record.get("history", []), today, new_stars)

    if errors:
        print("Failed to refresh GitHub stars:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    data_changed = dump_records(records) != dump_records(original_records)
    markdown_plan: list[str] = []
    for record in records:
        for location in record.get("locations", []):
            markdown_plan.append(f"{location['file']} -> {location['heading']}")

    print(f"Fetched {len(records)} GitHub repositories.")
    for record in records:
        print(
            f"- {record['repo']}: {record['stars']} stars "
            f"(delta {format_delta(record['delta'])}, updated {record['last_updated']})"
        )

    if args.check:
        print("Markdown sync targets:")
        for target in markdown_plan:
            print(f"  - {target}")
        print(f"data/github-stars.json changed: {'yes' if data_changed else 'no'}")
        print("Check mode finished without writing files.")
        return 0

    DATA_PATH.write_text(dump_records(records), encoding="utf-8")
    updated_files = sync_markdown(records)
    print("Updated markdown files:")
    for path in updated_files:
        print(f"  - {path}")
    print("Write mode finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
