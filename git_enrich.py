"""Git evidence enrichment: author filtering, branch matching, bounded diffs,
and review signals.

The upstream pipeline clusters commits by PR number and a 2-hour time window.
That leaves three gaps this module fills, using only the local repository:

* author filtering - commits authored by someone else must not be credited to
  the person whose sessions are being analyzed
* branch matching - commits that landed together on one branch are one work
  unit even when they span more than two hours
* diff evidence - file-level numstat plus a bounded patch, instead of only a
  subject line
* review evidence - review-shaped activity found in the session transcripts
  (subagent review dispatches, `gh pr review`, explicit review prompts)

Everything here is local `git`; no network, no GitHub API.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

MAX_DIFF_CHARS = 6_000
MAX_DIFF_FILES = 12
REVIEW_PATTERNS = [
    re.compile(r"\bgh\s+pr\s+review\b", re.I),
    re.compile(r"\bcode\s*review\b", re.I),
    re.compile(r"\breview(ed|ing)?\s+(the\s+)?(diff|patch|pr|change)", re.I),
    re.compile(r"\bself[- ]review\b", re.I),
]


def git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def is_repo(path: Path) -> bool:
    return (path / ".git").exists() or bool(git(path, "rev-parse", "--git-dir").strip())


def default_author(repo: Path) -> str | None:
    email = git(repo, "config", "user.email").strip()
    return email or None


def commit_branches(repo: Path, sha: str) -> list[str]:
    """Branches that contain the commit, local and remote-tracking."""
    out = git(repo, "branch", "-a", "--contains", sha, "--format=%(refname:short)")
    branches = [line.strip() for line in out.splitlines() if line.strip()]
    return sorted({b.replace("remotes/", "") for b in branches})


def collect_commits(repo: Path, days: int, author: str | None = None) -> tuple[list[dict], dict]:
    span = f"--since={days}.days"
    raw = git(repo, "log", span, "--all", "--date=iso-strict", "--pretty=format:%H%x1f%ad%x1f%ae%x1f%an%x1f%s%x1e")
    commits: list[dict] = []
    skipped_authors: dict[str, int] = {}
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x1f")
        if len(parts) < 5:
            continue
        sha, at, email, name, subject = parts[0], parts[1], parts[2], parts[3], parts[4]
        if author and email.lower() != author.lower():
            skipped_authors[email] = skipped_authors.get(email, 0) + 1
            continue
        numstat = git(repo, "show", "--format=", "--numstat", sha)
        insertions = deletions = 0
        files: list[str] = []
        for line in numstat.splitlines():
            bits = line.split("\t")
            if len(bits) < 3:
                continue
            try:
                insertions += int(bits[0])
                deletions += int(bits[1])
            except ValueError:
                pass
            files.append(bits[2])
        commits.append(
            {
                "sha": sha,
                "at": at,
                "author_name": name,
                "author_email": email,
                "subject": subject,
                "insertions": insertions,
                "deletions": deletions,
                "changed_files": files[:40],
                "changed_file_count": len(files),
                "branches": commit_branches(repo, sha),
            }
        )
    meta = {
        "commits": len(commits),
        "author_filter": author,
        "skipped_by_author": skipped_authors,
    }
    return commits, meta


def diff_for_commit(repo: Path, sha: str) -> str:
    patch = git(repo, "show", "--format=", "--unified=1", sha)
    if len(patch) > MAX_DIFF_CHARS:
        patch = patch[:MAX_DIFF_CHARS] + "\n...[truncated]"
    return patch


def attach_diffs(repo: Path, groups: list[dict], limit: int = 3) -> None:
    for group in groups:
        patches = []
        for sha in group.get("commit_shas", [])[:limit]:
            patch = diff_for_commit(repo, sha)
            if patch.strip():
                patches.append(f"--- {sha[:10]} ---\n{patch}")
        if patches:
            group["diff_excerpt"] = "\n".join(patches)
            group["diff_truncated"] = len(group["commit_shas"]) > limit


def group_commits(commits: list[dict], pr_numbers: list[int], branch_window_hours: int = 24) -> list[dict]:
    """PR first, then same-branch window, then the 2-hour cluster, then singles."""
    ordered = sorted(commits, key=lambda c: c["at"])
    groups: list[dict] = []
    used: set[str] = set()

    def make(kind: str, title: str, members: list[dict], pr_number: int | None = None, confidence: float = 0.5):
        shas = [c["sha"] for c in members]
        branches = sorted({b for c in members for b in c.get("branches", [])})
        return {
            "id": f"{kind}:{shas[0][:12]}",
            "group_type": kind,
            "title": title,
            "pr_number": pr_number,
            "commit_shas": shas,
            "earliest_commit_at": members[0]["at"],
            "latest_commit_at": members[-1]["at"],
            "branches": branches,
            "insertions": sum(c.get("insertions", 0) for c in members),
            "deletions": sum(c.get("deletions", 0) for c in members),
            "link_confidence": confidence,
        }

    for number in sorted(set(pr_numbers)):
        members = [
            c for c in ordered if c["sha"] not in used and re.search(rf"#{number}\b", c.get("subject", ""))
        ]
        if members:
            groups.append(make("pr", f"PR #{number}", members, number, 0.95))
            used.update(c["sha"] for c in members)

    window = timedelta(hours=branch_window_hours)
    by_branch: dict[str, list[dict]] = {}
    for commit in ordered:
        if commit["sha"] in used:
            continue
        for branch in commit.get("branches") or [f"<detached:{commit['sha'][:8]}>"]:
            by_branch.setdefault(branch, []).append(commit)

    for branch, members in by_branch.items():
        run: list[dict] = []
        for commit in members:
            if run and datetime.fromisoformat(commit["at"]) - datetime.fromisoformat(run[-1]["at"]) > window:
                if len(run) > 1:
                    groups.append(make("branch_match", f"{len(run)} commits on {branch}", run, None, 0.75))
                    used.update(c["sha"] for c in run)
                run = []
            if commit["sha"] not in used:
                run.append(commit)
        if len(run) > 1:
            groups.append(make("branch_match", f"{len(run)} commits on {branch}", run, None, 0.75))
            used.update(c["sha"] for c in run)

    remaining = [c for c in ordered if c["sha"] not in used]
    run = []
    for commit in remaining:
        if run and datetime.fromisoformat(commit["at"]) - datetime.fromisoformat(run[-1]["at"]) > timedelta(hours=2):
            if len(run) > 1:
                groups.append(make("commit_cluster", f"{len(run)} commits: {run[0]['subject'][:60]}", run, None, 0.5))
                used.update(c["sha"] for c in run)
            run = []
        run.append(commit)
    if len(run) > 1:
        groups.append(make("commit_cluster", f"{len(run)} commits: {run[0]['subject'][:60]}", run, None, 0.5))
        used.update(c["sha"] for c in run)

    for commit in ordered:
        if commit["sha"] not in used:
            groups.append(make("single_commit", commit["subject"][:100], [commit], None, 0.4))
    return groups


def review_signals(evidence: dict) -> dict[str, list[str]]:
    """Review-shaped activity per session, from the transcript evidence only."""
    out: dict[str, list[str]] = {}
    for session in evidence.get("sessions") or []:
        hits: list[str] = []
        for event in session.get("events") or []:
            text = ""
            if isinstance(event, dict):
                text = " ".join(
                    str(event.get(key) or "") for key in ("text", "content", "summary", "command")
                )
            elif isinstance(event, str):
                text = event
            for pattern in REVIEW_PATTERNS:
                match = pattern.search(text)
                if match:
                    hits.append(match.group(0)[:40])
                    break
        if hits:
            out[str(session.get("id"))] = hits[:10]
    return out


def enrich(project: Path, evidence: dict, days: int, author: str | None = None) -> dict:
    if not is_repo(project):
        return {"available": False, "reason": "not a git repository"}
    author = author or default_author(project)
    commits, meta = collect_commits(project, days, author)
    pr_numbers: list[int] = []
    for session in evidence.get("sessions") or []:
        for event in session.get("events") or []:
            blob = json.dumps(event, ensure_ascii=False) if isinstance(event, dict) else str(event)
            pr_numbers.extend(int(n) for n in re.findall(r"\bgh\s+pr\s+(?:view|checkout|diff|merge|review|comment|close)\s+(\d+)\b", blob))
    groups = group_commits(commits, pr_numbers)
    attach_diffs(project, groups)
    reviews = review_signals(evidence)
    return {
        "available": True,
        "repo": str(project),
        "author": author,
        **meta,
        "branches": sorted({b for c in commits for b in c.get("branches", [])}),
        "pr_numbers": sorted(set(pr_numbers)),
        "groups": groups,
        "review_signals": reviews,
    }


def write(enriched: dict, outdir: Path) -> Path:
    path = outdir / "git-enriched.json"
    path.write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
