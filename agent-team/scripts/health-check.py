#!/usr/bin/env python3
"""
AutoTeam pipeline health check.

Hourly cron-driven script that scans all configured projects for known
"stuck pipeline" patterns and remediates them. Two layers:

  Layer 1 (deterministic): detect stuck PRs/issues and apply the same
  fixes a human would apply (label, comment, ping daemon).

  Layer 2 (LLM-driven escalation): when the same pattern recurs N times
  in the past 7 days, spawn a Claude Code session that opens a PR
  against the AutoTeam repo proposing a daemon.py fix.

Layer 2 only opens PRs — it never pushes directly. Human review remains
in the loop for daemon code changes.

Usage:
    python3 health-check.py              # normal hourly run
    python3 health-check.py --dry-run    # detect only, do not remediate
    python3 health-check.py --no-escalate  # skip Layer 2

Exits 0 always so cron does not spam mail. All output goes to the log.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ─── Paths ───────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_TEAM_DIR = SCRIPT_DIR.parent
PROJECTS_DIR = AGENT_TEAM_DIR / "projects"
LOG_DIR = AGENT_TEAM_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "health-check.log"
INCIDENT_FILE = LOG_DIR / "health-check.jsonl"
ESCALATION_STATE = LOG_DIR / "health-check-escalations.json"

# ─── Tunables ────────────────────────────────────────────
DEV_IN_PROGRESS_STALE_AFTER_HOURS = 1
ESCALATION_RECURRENCE_THRESHOLD = 3   # same pattern this many times → escalate
ESCALATION_LOOKBACK_DAYS = 7
ESCALATION_COOLDOWN_HOURS = 24        # do not re-escalate the same pattern within this window

# Where to file the credential-expiry alert. Any project's GITHUB_TOKEN can
# be used to write to this repo as long as the PAT has access to it.
AUTOTEAM_REPO = "prasinus-automation/AutoTeam"
CREDENTIAL_ALERT_TITLE = "AutoTeam credentials expired — run `claude /login` on the host"

API_BASE = "https://api.github.com"


# ─── Logging ─────────────────────────────────────────────
log = logging.getLogger("health-check")
log.setLevel(logging.INFO)
_handler = logging.FileHandler(LOG_FILE)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_handler)
# Also echo to stderr so cron mail captures errors if logging is broken
_stderr = logging.StreamHandler(sys.stderr)
_stderr.setLevel(logging.WARNING)
_stderr.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_stderr)


# ─── GitHub helpers ──────────────────────────────────────
def gh_request(method: str, url: str, token: str, body=None):
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "autoteam-health-check",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "null")
        except Exception:
            return e.code, None
    except Exception as e:
        log.error(f"GH request failed {method} {url}: {e}")
        return None, None


def gh_get(url: str, token: str):
    return gh_request("GET", url, token)


def gh_add_label(repo: str, number: int, label: str, token: str):
    return gh_request(
        "POST",
        f"{API_BASE}/repos/{repo}/issues/{number}/labels",
        token,
        {"labels": [label]},
    )


def gh_post_comment(repo: str, number: int, body: str, token: str):
    return gh_request(
        "POST",
        f"{API_BASE}/repos/{repo}/issues/{number}/comments",
        token,
        {"body": body},
    )


# ─── Project discovery ───────────────────────────────────
def parse_env_file(path: Path) -> dict:
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def discover_projects() -> list[dict]:
    projects = []
    if not PROJECTS_DIR.is_dir():
        return projects
    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        env_file = project_dir / ".env"
        if not env_file.is_file():
            continue
        env = parse_env_file(env_file)
        if not env.get("GITHUB_REPO") or not env.get("GITHUB_TOKEN"):
            continue
        projects.append({
            "name": project_dir.name,
            "repo": env["GITHUB_REPO"],
            "token": env["GITHUB_TOKEN"],
            "webhook_port": env.get("WEBHOOK_PORT"),
        })
    return projects


def daemon_health(port: str | None) -> dict | None:
    if not port:
        return None
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/", timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


# ─── Pattern detection ───────────────────────────────────
def parse_review_state(comments: list[dict]) -> dict:
    """Walk comments newest-first, return latest QA / Security / architect verdicts."""
    out = {"qa": None, "sec": None, "arch_declined": False}
    for c in reversed(comments):
        body = c.get("body", "") or ""
        if "Daemon-verified:" in body or body.startswith("⚠️ Agent ") or body.startswith("⏳ Agent "):
            continue
        header = body.split("\n", 1)[0].upper()
        if out["qa"] is None and "QA REVIEW" in header:
            if "CHANGES REQUESTED" in header or "❌" in header:
                out["qa"] = "changes"
            elif "APPROVED" in header or "✅" in header:
                out["qa"] = "approved"
        if out["sec"] is None and "SECURITY REVIEW" in header:
            if "CHANGES REQUESTED" in header or "❌" in header:
                out["sec"] = "changes"
            elif "APPROVED" in header or "✅" in header:
                out["sec"] = "approved"
        if "ARCHITECT REVIEW" in header and "NOT MERGING" in header:
            out["arch_declined"] = True
    return out


def detect_stuck_patterns(project: dict, daemon: dict | None) -> list[dict]:
    """Return a list of incidents found in this project."""
    incidents: list[dict] = []
    repo = project["repo"]
    token = project["token"]

    # PRs/issues the daemon is already tracking — do not flag these. The
    # daemon knows about them and will act when a slot frees.
    daemon_known: set[int] = set()
    daemon_active: set[int] = set()
    if daemon:
        daemon_known.update(daemon.get("pending_fix_prs") or [])
        daemon_active.update(a["issue"] for a in daemon.get("active_agents", []))
        daemon_known.update(daemon_active)

    # Open PRs
    status, prs = gh_get(f"{API_BASE}/repos/{repo}/pulls?state=open&per_page=50", token)
    if status != 200 or not isinstance(prs, list):
        log.warning(f"[{project['name']}] could not fetch PRs (status={status})")
        prs = []

    for pr in prs:
        number = pr["number"]
        labels = {l["name"] for l in pr.get("labels", [])}
        mergeable_state = pr.get("mergeable_state", "")
        branch = pr.get("head", {}).get("ref", "")

        # Skip PRs whose branch doesn't follow our convention — daemon won't
        # know what to do with them anyway.
        if not (branch.startswith("frontend/") or branch.startswith("backend/")):
            continue

        # Skip PRs the daemon is already tracking (pending or active).
        if number in daemon_known:
            continue

        # Pull comments to inspect review state
        _, comments = gh_get(f"{API_BASE}/repos/{repo}/issues/{number}/comments?per_page=100", token)
        if not isinstance(comments, list):
            comments = []
        rs = parse_review_state(comments)

        # ── Pattern 1: reviewers requested changes but no needs-fixes label
        last_negative = rs["qa"] == "changes" or rs["sec"] == "changes"
        if last_negative and "needs-fixes" not in labels and "dev-in-progress" not in labels:
            incidents.append({
                "pattern": "reviewer_changes_no_label",
                "pr": number,
                "title": pr.get("title", "")[:80],
                "remedy": "add_needs_fixes",
            })
            continue

        # ── Pattern 2: PR is dirty (merge conflicts) and no needs-fixes
        if mergeable_state == "dirty" and "needs-fixes" not in labels and "dev-in-progress" not in labels:
            incidents.append({
                "pattern": "merge_conflict_no_label",
                "pr": number,
                "title": pr.get("title", "")[:80],
                "remedy": "add_needs_fixes",
            })
            continue

        # ── Pattern 3: architect declined merge but no follow-up label
        if rs["arch_declined"] and "needs-fixes" not in labels and "dev-in-progress" not in labels:
            incidents.append({
                "pattern": "architect_declined_no_label",
                "pr": number,
                "title": pr.get("title", "")[:80],
                "remedy": "synth_changes_then_label",
            })
            continue

    # Open issues for stale dev-in-progress detection
    status, issues_and_prs = gh_get(
        f"{API_BASE}/repos/{repo}/issues?state=open&labels=dev-in-progress&per_page=50",
        token,
    )
    if isinstance(issues_and_prs, list):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=DEV_IN_PROGRESS_STALE_AFTER_HOURS)
        active_numbers = set()
        if daemon:
            active_numbers = {a["issue"] for a in daemon.get("active_agents", [])}
        # Build set of issue numbers referenced by any open PR body so we
        # can tell whether a stale dev-in-progress label is benign (the dev
        # produced a PR, the label is just leftover) vs the dev died and
        # produced nothing.
        closes_re = re.compile(r"\b(?:closes|fixes|resolves|part of)\s+#(\d+)", re.IGNORECASE)
        prs_close = set()
        for p in prs:
            body = p.get("body") or ""
            for m in closes_re.finditer(body):
                prs_close.add(int(m.group(1)))

        for item in issues_and_prs:
            if "pull_request" in item:
                continue  # only issues
            number = item["number"]
            if number in active_numbers:
                continue
            updated = item.get("updated_at", "")
            try:
                updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            except Exception:
                continue
            if updated_dt > cutoff:
                continue
            # Benign case: the dev opened a PR and the label is just stale.
            # Skip silently so we don't generate noise.
            if number in prs_close:
                continue
            # Real stuck case: dev died, no PR exists for this issue.
            incidents.append({
                "pattern": "dev_in_progress_stale_no_pr",
                "issue": number,
                "title": item.get("title", "")[:80],
                "remedy": "log_only",
            })

    # Daemon-side: pending fixes not draining
    if daemon:
        pending = daemon.get("pending_fix_prs") or []
        active_devs = sum(1 for a in daemon.get("active_agents", []) if a["role"] in ("frontend-dev", "backend-dev"))
        if pending and active_devs == 0:
            incidents.append({
                "pattern": "pending_fixes_not_draining",
                "pending_count": len(pending),
                "pending": pending,
                "remedy": "log_only",
            })

    return incidents


# ─── Remediation ─────────────────────────────────────────
SYNTH_CHANGES_REQUESTED_BODY = (
    "## QA Review — CHANGES REQUESTED\n\n"
    "Re-opening review (auto-detected by health-check.py): the architect "
    "previously declined to merge this PR but no `needs-fixes` label was "
    "applied, so it was sitting orphaned. See the architect's most recent "
    "comment on this PR for the specific issues that need to be addressed."
)


def remediate(project: dict, incident: dict, dry_run: bool) -> bool:
    repo = project["repo"]
    token = project["token"]
    remedy = incident["remedy"]
    label = "[DRY-RUN] " if dry_run else ""

    if remedy == "log_only":
        log.info(f"{label}[{project['name']}] {incident['pattern']} — log only, no auto-fix")
        return False

    if remedy == "add_needs_fixes":
        pr_number = incident["pr"]
        log.info(f"{label}[{project['name']}] {incident['pattern']} on PR #{pr_number} — adding needs-fixes")
        if dry_run:
            return True
        gh_add_label(repo, pr_number, "needs-fixes", token)
        return True

    if remedy == "synth_changes_then_label":
        pr_number = incident["pr"]
        log.info(f"{label}[{project['name']}] {incident['pattern']} on PR #{pr_number} — posting synth comment + needs-fixes")
        if dry_run:
            return True
        gh_post_comment(repo, pr_number, SYNTH_CHANGES_REQUESTED_BODY, token)
        gh_add_label(repo, pr_number, "needs-fixes", token)
        return True

    log.warning(f"[{project['name']}] unknown remedy: {remedy}")
    return False


# ─── Incident persistence ────────────────────────────────
def append_incidents(project: dict, incidents: list[dict]) -> None:
    if not incidents:
        return
    now = datetime.now(timezone.utc).isoformat()
    with INCIDENT_FILE.open("a") as f:
        for inc in incidents:
            f.write(json.dumps({
                "ts": now,
                "project": project["name"],
                "repo": project["repo"],
                **inc,
            }) + "\n")


def load_recent_incidents() -> list[dict]:
    if not INCIDENT_FILE.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=ESCALATION_LOOKBACK_DAYS)
    out = []
    for line in INCIDENT_FILE.read_text().splitlines():
        try:
            row = json.loads(line)
            ts = datetime.fromisoformat(row["ts"])
            if ts >= cutoff:
                out.append(row)
        except Exception:
            continue
    return out


def load_escalation_state() -> dict:
    if ESCALATION_STATE.exists():
        try:
            return json.loads(ESCALATION_STATE.read_text())
        except Exception:
            return {}
    return {}


def save_escalation_state(state: dict) -> None:
    ESCALATION_STATE.write_text(json.dumps(state, indent=2))


# ─── Layer 2: LLM escalation ─────────────────────────────
ESCALATION_PROMPT = """\
You are debugging a recurring bug in the AutoTeam pipeline daemon.

The hourly health check has been auto-remediating the same stuck pattern \
multiple times in the past week. This means the daemon code in \
`agent-team/daemon/daemon.py` has a real gap that needs to be fixed at \
the source so the health check stops needing to clean up after it.

## Recurring pattern

Pattern name: `{pattern}`
Occurrences in past {days} days: {count}
Affected projects: {projects}

Recent incidents (most recent {sample_size} of {count} total):
```json
{sample_json}
```

## What the health check does

For this pattern, the health check applies this remedy:
{remedy_description}

## Your task

1. Read `agent-team/daemon/daemon.py` and identify the code path that \
should have prevented this stuck state from occurring in the first place.
2. Read `agent-team/scripts/health-check.py` to see exactly what the \
health check is detecting and how it remediates — your daemon fix should \
make those patterns impossible (or rare) at the source.
3. Write a focused, minimal fix to `daemon.py`. Do not refactor unrelated \
code. Do not add features.
4. Verify your fix by re-reading the relevant function. Trace through \
the failure mode mentally to confirm it is closed.
5. Commit on a new branch named `health-check/fix-{pattern}-{timestamp}` \
with a clear commit message that includes the pattern name and a brief \
explanation of the fix.
6. Push the branch and open a PR against `main` with `gh pr create`. The PR \
title should start with `fix(daemon):` and the body should include:
   - The pattern name
   - How many times it recurred
   - Root cause analysis (one or two paragraphs)
   - What the fix does
   - Any caveats / things the human reviewer should double-check

Do NOT merge the PR. The human reviews and merges. If the fix is not \
obvious or you cannot trace the root cause cleanly, open the PR anyway \
with `[NEEDS HUMAN]` in the title and explain what you found.
"""


REMEDY_DESCRIPTIONS = {
    "add_needs_fixes": (
        "Adds the `needs-fixes` label to the PR. This makes the daemon "
        "re-spawn the dev agent to address review feedback. The fact "
        "that this is needed means the daemon should have applied the "
        "label itself when the review came in but did not."
    ),
    "synth_changes_then_label": (
        "Posts a synthesizing 'QA Review — CHANGES REQUESTED' comment "
        "(so `_latest_reviews_approved` returns False) and then adds "
        "`needs-fixes`. The comment is needed because the daemon's fix-"
        "dispatch path skips PRs whose latest QA/Security reviews are "
        "approvals — so when only the architect declined the merge, the "
        "PR is orphaned with no path to fix-loop."
    ),
    "log_only": (
        "Logs only — no auto-fix. The pattern indicates a deeper failure "
        "that needs human eyes."
    ),
}


def detect_recurring_patterns() -> list[dict]:
    """Group recent incidents by pattern. Return patterns above threshold."""
    incidents = load_recent_incidents()
    by_pattern: dict[str, list[dict]] = defaultdict(list)
    for inc in incidents:
        by_pattern[inc["pattern"]].append(inc)
    out = []
    for pattern, rows in by_pattern.items():
        if len(rows) >= ESCALATION_RECURRENCE_THRESHOLD:
            out.append({
                "pattern": pattern,
                "count": len(rows),
                "projects": sorted({r["project"] for r in rows}),
                "rows": rows,
            })
    return out


def should_escalate(pattern: str, escalations: dict) -> bool:
    last = escalations.get(pattern, {}).get("last_escalated_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except Exception:
        return True
    return datetime.now(timezone.utc) - last_dt > timedelta(hours=ESCALATION_COOLDOWN_HOURS)


def escalate_to_claude(pattern_info: dict, escalations: dict, dry_run: bool) -> None:
    pattern = pattern_info["pattern"]
    count = pattern_info["count"]
    projects = pattern_info["projects"]
    rows = pattern_info["rows"]
    sample = rows[-5:]

    prompt = ESCALATION_PROMPT.format(
        pattern=pattern,
        days=ESCALATION_LOOKBACK_DAYS,
        count=count,
        projects=", ".join(projects),
        sample_size=len(sample),
        sample_json=json.dumps(sample, indent=2),
        remedy_description=REMEDY_DESCRIPTIONS.get(rows[-1].get("remedy"), "(unknown)"),
        timestamp=int(time.time()),
    )

    log.warning(
        f"Escalating recurring pattern '{pattern}' "
        f"({count} occurrences in {ESCALATION_LOOKBACK_DAYS}d, projects: {projects})"
    )

    if dry_run:
        log.info(f"[DRY-RUN] Would spawn Claude Code with prompt ({len(prompt)} chars)")
        return

    # Run Claude Code in the AutoTeam repo. We do NOT use a worktree —
    # working directly on the main repo lets the agent commit + push.
    # Claude is invoked in non-interactive mode and will use the gh CLI
    # already configured on the host.
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    cmd = [claude_bin, "--print", "--permission-mode", "acceptEdits", prompt]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(AGENT_TEAM_DIR.parent),  # AutoTeam repo root
            capture_output=True,
            text=True,
            timeout=1800,  # 30 min
        )
        log.info(f"Claude exit={proc.returncode}")
        if proc.stdout:
            log.info(f"Claude stdout (first 2000 chars):\n{proc.stdout[:2000]}")
        if proc.stderr:
            log.warning(f"Claude stderr (first 2000 chars):\n{proc.stderr[:2000]}")
    except FileNotFoundError:
        log.error(f"Claude binary not found: {claude_bin}")
        return
    except subprocess.TimeoutExpired:
        log.error("Claude escalation timed out after 30min")
        return
    except Exception as e:
        log.error(f"Claude escalation failed: {e}")
        return

    escalations[pattern] = {
        "last_escalated_at": datetime.now(timezone.utc).isoformat(),
        "occurrences_at_escalation": count,
    }
    save_escalation_state(escalations)


# ─── Credential expiry alerting ──────────────────────────
def check_credentials(projects: list[dict], dry_run: bool) -> None:
    """Read each daemon's /health credential block. If any daemon reports
    expired credentials, file a single GitHub issue in the AutoTeam repo so
    the human gets a notification. Idempotent: only files the issue if one
    isn't already open with the same title."""
    expired_projects: list[tuple[str, dict]] = []
    healthy_count = 0
    for project in projects:
        daemon = daemon_health(project["webhook_port"])
        if daemon is None:
            continue
        creds = daemon.get("credentials") or {}
        if creds.get("mode") == "api_key":
            healthy_count += 1
            continue
        if creds.get("expired"):
            expired_projects.append((project["name"], creds))
        else:
            healthy_count += 1

    if not expired_projects:
        log.info(f"Credentials healthy across {healthy_count} daemon(s)")
        return

    log.warning(f"CREDENTIAL EXPIRY: {len(expired_projects)} daemon(s) report expired credentials")
    for name, creds in expired_projects:
        log.warning(f"  {name}: {creds.get('reason') or 'expired'}")

    if dry_run:
        log.info("[DRY-RUN] Would file/refresh AutoTeam credential alert issue")
        return

    # Pick any project's token — we just need a PAT with access to the
    # AutoTeam repo, and the org-wide PAT is shared across projects.
    token = projects[0]["token"]

    # Look for an already-open alert issue. We match by exact title to keep
    # this dead simple.
    status, items = gh_get(
        f"{API_BASE}/repos/{AUTOTEAM_REPO}/issues?state=open&per_page=50",
        token,
    )
    existing = None
    if isinstance(items, list):
        for it in items:
            if "pull_request" in it:
                continue
            if it.get("title") == CREDENTIAL_ALERT_TITLE:
                existing = it
                break

    body_lines = [
        "The hourly health check detected that one or more AutoTeam daemons are running with expired Claude credentials.",
        "",
        "**To fix:** on the host machine, run an interactive Claude Code session and execute `/login`. The daemons will pick up the new credentials automatically — no daemon restart needed (the directory mount stays in sync with the host file).",
        "",
        "## Affected daemons",
        "",
    ]
    for name, creds in expired_projects:
        reason = creds.get("reason") or f"expired at {creds.get('expires_at', 'unknown')}"
        body_lines.append(f"- **{name}**: {reason}")
    body_lines.append("")
    body_lines.append(f"_Detected at {datetime.now(timezone.utc).isoformat()} by `health-check.py`._")
    body_lines.append("")
    body_lines.append("This issue will close itself automatically once credentials are refreshed and the next health-check run sees them as valid.")
    body = "\n".join(body_lines)

    if existing:
        # Refresh the body so it always reflects the current detection time
        # and the latest list of affected daemons. Don't spam comments —
        # one issue, refreshed in place.
        gh_request(
            "PATCH",
            f"{API_BASE}/repos/{AUTOTEAM_REPO}/issues/{existing['number']}",
            token,
            {"body": body},
        )
        log.warning(f"Refreshed credential alert issue #{existing['number']} on {AUTOTEAM_REPO}")
    else:
        status, created = gh_request(
            "POST",
            f"{API_BASE}/repos/{AUTOTEAM_REPO}/issues",
            token,
            {"title": CREDENTIAL_ALERT_TITLE, "body": body},
        )
        if isinstance(created, dict) and "number" in created:
            log.warning(f"Filed credential alert issue #{created['number']} on {AUTOTEAM_REPO}: {created.get('html_url')}")
        else:
            log.error(f"Failed to file credential alert issue: status={status} resp={created}")


def maybe_close_credential_alert(projects: list[dict], dry_run: bool) -> None:
    """If credentials are healthy and there's an open alert issue, close it."""
    # Only close if EVERY daemon we can reach reports healthy credentials.
    any_expired = False
    any_seen = False
    for project in projects:
        daemon = daemon_health(project["webhook_port"])
        if daemon is None:
            continue
        creds = daemon.get("credentials") or {}
        if creds.get("mode") == "none":
            continue
        any_seen = True
        if creds.get("expired"):
            any_expired = True
            break
    if any_expired or not any_seen:
        return

    token = projects[0]["token"]
    status, items = gh_get(
        f"{API_BASE}/repos/{AUTOTEAM_REPO}/issues?state=open&per_page=50",
        token,
    )
    if not isinstance(items, list):
        return
    for it in items:
        if "pull_request" in it:
            continue
        if it.get("title") != CREDENTIAL_ALERT_TITLE:
            continue
        if dry_run:
            log.info(f"[DRY-RUN] Would close credential alert issue #{it['number']}")
            return
        gh_request(
            "POST",
            f"{API_BASE}/repos/{AUTOTEAM_REPO}/issues/{it['number']}/comments",
            token,
            {"body": "Credentials are healthy across all daemons again. Auto-closing."},
        )
        gh_request(
            "PATCH",
            f"{API_BASE}/repos/{AUTOTEAM_REPO}/issues/{it['number']}",
            token,
            {"state": "closed", "state_reason": "completed"},
        )
        log.info(f"Closed credential alert issue #{it['number']}")
        return


# ─── Main ────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Detect and log only, do not remediate or escalate")
    parser.add_argument("--no-escalate", action="store_true",
                        help="Run Layer 1 only, skip Layer 2 LLM escalation")
    args = parser.parse_args()

    log.info("─── health-check run started ───")
    projects = discover_projects()
    log.info(f"Discovered {len(projects)} project(s)")

    # ── Credential expiry check (cheap, runs first) ────
    if projects:
        check_credentials(projects, args.dry_run)
        maybe_close_credential_alert(projects, args.dry_run)

    total_incidents = 0
    total_remediated = 0

    for project in projects:
        daemon = daemon_health(project["webhook_port"])
        if daemon is None:
            log.info(f"[{project['name']}] daemon unreachable, skipping")
            continue

        incidents = detect_stuck_patterns(project, daemon)
        if not incidents:
            log.info(f"[{project['name']}] healthy")
            continue

        log.warning(f"[{project['name']}] {len(incidents)} stuck pattern(s) found")
        append_incidents(project, incidents)
        total_incidents += len(incidents)

        for inc in incidents:
            if remediate(project, inc, args.dry_run):
                total_remediated += 1

    log.info(f"Layer 1 done: {total_incidents} incident(s), {total_remediated} remediated")

    # ── Layer 2: escalation ────────────────────────────
    if not args.no_escalate:
        recurring = detect_recurring_patterns()
        if recurring:
            escalations = load_escalation_state()
            for r in recurring:
                if should_escalate(r["pattern"], escalations):
                    escalate_to_claude(r, escalations, args.dry_run)
                else:
                    log.info(f"Pattern '{r['pattern']}' in cooldown, skipping escalation")
        else:
            log.info("No recurring patterns above escalation threshold")

    log.info("─── health-check run finished ───\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
