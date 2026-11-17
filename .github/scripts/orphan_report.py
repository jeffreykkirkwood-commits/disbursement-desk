#!/usr/bin/env python3
"""Daily orphan report (#227): list open issues that have no parent issue.

Every issue should trace up to an initiative through GitHub's sub-issues, so an
open issue with no parent is an "orphan". Issues labeled `type: initiative` are
the top of the tree and are skipped. Pull requests are skipped too.

Report only: it writes the list to the job summary and the log, and it never
fails the run (an API error is a warning). Read-only: it changes nothing.
Uses one REST list call per 100 issues (`parent_issue_url` is set on an issue
that has a parent). Standard library only.

Env: GITHUB_TOKEN, REPO (owner/name), GITHUB_STEP_SUMMARY (optional).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

SKIP_LABEL = "type: initiative"


def orphans(issues: list[dict]) -> list[dict]:
    """Open issues (not PRs) with no parent, leaving out initiatives."""
    found = []
    for issue in issues:
        if "pull_request" in issue or issue.get("state") != "open":
            continue
        if issue.get("parent_issue_url"):
            continue
        if SKIP_LABEL in {label.get("name") for label in issue.get("labels") or []}:
            continue
        found.append(issue)
    return sorted(found, key=lambda i: i["number"])


def report(found: list[dict]) -> str:
    """Markdown for the job summary."""
    if not found:
        return "## Orphan report\n\nEvery open issue has a parent issue (initiatives skipped).\n"
    lines = [f"## Orphan report\n\n{len(found)} open issues have no parent issue "
             "(initiatives skipped). Set each one's parent in its sidebar.\n"]
    lines += [f"- [#{i['number']}]({i['html_url']}) {i['title']}" for i in found]
    return "\n".join(lines) + "\n"


def fetch_open_issues(repo: str, token: str) -> list[dict]:
    """Every open issue and PR in the repo, 100 per page."""
    items: list[dict] = []
    page = 1
    while True:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/issues?state=open&per_page=100&page={page}",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            batch = json.load(resp)
        items += batch
        if len(batch) < 100:
            return items
        page += 1


def main() -> int:
    repo = os.environ.get("REPO", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    try:
        issues = fetch_open_issues(repo, token)
    except Exception as exc:  # report only: never fail the run
        print(f"::warning::Orphan report could not read issues: {type(exc).__name__}")
        return 0
    text = report(orphans(issues))
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
