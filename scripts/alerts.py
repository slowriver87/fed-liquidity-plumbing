#!/usr/bin/env python3
"""Open, comment on and close the alert issues listed by scripts/update.py (--alerts file).

Kinds:
- new: a new issue each time (color change, policy change, reserves move).
- sticky: one open issue per title. on=true: comment on the open issue, or open one; on=false: comment
  and close the open issue if there is one (SOFR/EFFR above IORB, standing repo usage).

All alert issues carry the label `alert` and are assigned to the repository owner (read from
GITHUB_REPOSITORY_OWNER at run time; being assigned subscribes the owner to the issue and its comments).
Uses the GitHub REST API with GITHUB_TOKEN and GITHUB_REPOSITORY.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

API = "https://api.github.com"
LABEL = "alert"


class GitHub:
    def __init__(self, repo: str, token: str, owner: str = "", session=None):
        import requests
        self.repo, self.owner, self.s = repo, owner, session or requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28"})

    def _req(self, method: str, path: str, **kw):
        r = self.s.request(method, f"{API}/repos/{self.repo}{path}", timeout=30, **kw)
        if r.status_code >= 400 and not (method == "POST" and path == "/labels" and r.status_code == 422):
            raise RuntimeError(f"GitHub API {method} {path}: HTTP {r.status_code}")
        return r.json() if r.content else None

    def ensure_label(self):
        self._req("POST", "/labels", json={"name": LABEL, "color": "b60205", "description": "Automatic data alert"})

    def open_issues(self) -> list[dict]:
        out, page = [], 1
        while True:
            batch = self._req("GET", "/issues", params={"state": "open", "labels": LABEL, "per_page": 100, "page": page})
            out += [i for i in batch if "pull_request" not in i]
            if len(batch) < 100:
                return out
            page += 1

    def create(self, title: str, body: str):
        data = {"title": title, "body": body, "labels": [LABEL]}
        if self.owner:
            data["assignees"] = [self.owner]
        return self._req("POST", "/issues", json=data)

    def assign(self, number: int):
        if self.owner:
            self._req("POST", f"/issues/{number}/assignees", json={"assignees": [self.owner]})

    def comment(self, number: int, body: str):
        return self._req("POST", f"/issues/{number}/comments", json={"body": body})

    def close(self, number: int):
        return self._req("PATCH", f"/issues/{number}", json={"state": "closed", "state_reason": "completed"})


def apply(alerts: list[dict], gh: GitHub, run_url: str = "") -> list[str]:
    """-> what was done, one line per alert."""
    done = []
    tail = f"\n\nRun: {run_url}" if run_url else ""
    gh.ensure_label()
    open_by_title = {}
    for i in gh.open_issues():
        open_by_title.setdefault(i["title"], i["number"])
    for a in alerts:
        body = a["body"] + tail
        if a["kind"] == "new":
            gh.create(a["title"], body)
            done.append(f"opened: {a['title']}")
            continue
        num = open_by_title.get(a["title"])
        if a["on"]:
            if num:
                gh.assign(num)
                gh.comment(num, body)
                done.append(f"commented #{num}: {a['title']}")
            else:
                created = gh.create(a["title"], body)
                open_by_title[a["title"]] = created["number"] if created else None
                done.append(f"opened: {a['title']}")
        elif num:
            gh.comment(num, "Back under the threshold. " + body)
            gh.close(num)
            done.append(f"closed #{num}: {a['title']}")
    return done


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("alerts", type=Path)
    a = ap.parse_args(argv)
    if not a.alerts.exists():
        print("No alerts.")
        return 0
    alerts = json.loads(a.alerts.read_text(encoding="utf-8"))
    repo, token = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", "")
    if not repo or not token:
        print("GITHUB_REPOSITORY and GITHUB_TOKEN must be set.", file=sys.stderr)
        return 1
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_url = f"{server}/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}" if os.environ.get("GITHUB_RUN_ID") else ""
    gh = GitHub(repo, token, os.environ.get("GITHUB_REPOSITORY_OWNER", ""))
    for line in apply(alerts, gh, run_url):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
