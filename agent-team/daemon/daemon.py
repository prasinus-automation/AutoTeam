#!/usr/bin/env python3
"""
Agent Team Daemon

Two modes:
  1. WEBHOOK (preferred): GitHub sends events instantly to a local HTTP server
     exposed via cloudflared tunnel. Sub-second response time.
  2. POLLING (fallback): Polls GitHub API every 30s. No tunnel needed.

Set MODE=webhook or MODE=poll in .env

Events:
  - Issue labeled 'architect'      → Architect agent
  - Issue labeled 'frontend-dev'   → Frontend Developer agent
  - Issue labeled 'backend-dev'    → Backend Developer agent
  - PR opened/synchronize          → QA + Security agents (both)
  - PR labeled 'needs-fixes'       → Re-spawn dev (based on branch prefix)
  - PR review approved             → Check if both QA+Security approved → Architect merges
"""

import os
import re
import sys
import json
import time
import hmac
import hashlib
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

import docker
import requests

# ─── Config ──────────────────────────────────────────────

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ["GITHUB_REPO"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_CREDENTIALS_PATH = os.environ.get("CLAUDE_CREDENTIALS_PATH", "")  # path to .credentials.json

MODE = os.environ.get("MODE", "webhook")  # "webhook" or "poll"
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
MAX_FRONTEND_AGENTS = int(os.environ.get("MAX_FRONTEND_AGENTS", "2"))
MAX_BACKEND_AGENTS = int(os.environ.get("MAX_BACKEND_AGENTS", "2"))
MAX_FIX_ITERATIONS = int(os.environ.get("MAX_FIX_ITERATIONS", "3"))
MAX_TRANSIENT_RETRIES = int(os.environ.get("MAX_TRANSIENT_RETRIES", "5"))
TRANSIENT_BACKOFF_BASE = int(os.environ.get("TRANSIENT_BACKOFF_BASE", "60"))  # seconds
LOG_DIR = os.environ.get("LOG_DIR", "/logs")
REPO_CACHE_VOLUME = os.environ.get("REPO_CACHE_VOLUME", "agent-team_repo-cache")
PROJECT_NAME = os.environ.get("COMPOSE_PROJECT_NAME", "agent-team")
MEMORY_VOLUME = f"{PROJECT_NAME}_agent-memory"
MAX_TOTAL_AGENTS = int(os.environ.get("MAX_TOTAL_AGENTS", "3"))
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "9876"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

RESOURCE_PROFILES = {
    "architect":    {"mem_limit": "4g", "cpus": 2.0},
    "frontend-dev": {"mem_limit": "6g", "cpus": 3.0},
    "backend-dev":  {"mem_limit": "6g", "cpus": 3.0},
    "qa":           {"mem_limit": "6g", "cpus": 3.0},
    "security":     {"mem_limit": "6g", "cpus": 3.0},
}

# ─── Logging ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("daemon")

# ─── GitHub API ──────────────────────────────────────────

API = "https://api.github.com"
HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}


def gh_get(path, params=None):
    r = requests.get(f"{API}{path}", headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()


def gh_get_issues(label, state="open"):
    return gh_get(f"/repos/{GITHUB_REPO}/issues", params={
        "labels": label, "state": state, "sort": "created", "direction": "asc",
    })


def gh_get_prs(state="open"):
    return gh_get(f"/repos/{GITHUB_REPO}/pulls", params={
        "state": state, "sort": "created", "direction": "asc",
    })


def gh_get_pr(pr_number):
    return gh_get(f"/repos/{GITHUB_REPO}/pulls/{pr_number}")


def gh_remove_label(issue_number, label):
    try:
        requests.delete(
            f"{API}/repos/{GITHUB_REPO}/issues/{issue_number}/labels/{label}",
            headers=HEADERS,
        )
    except Exception:
        pass


def gh_add_label(issue_number, label):
    requests.post(
        f"{API}/repos/{GITHUB_REPO}/issues/{issue_number}/labels",
        headers=HEADERS,
        json={"labels": [label]},
    )


def gh_comment(issue_number, body):
    requests.post(
        f"{API}/repos/{GITHUB_REPO}/issues/{issue_number}/comments",
        headers=HEADERS,
        json={"body": body},
    )


def gh_issue_has_open_pr(issue_number):
    """Check if an issue already has an open PR (by body reference or branch name)."""
    try:
        for pr in gh_get_prs("open"):
            pr_body = pr.get("body", "") or ""
            branch = pr.get("head", {}).get("ref", "")
            # Check PR body for "Closes #N", "#N", etc.
            if f"#{issue_number}" in pr_body:
                return True
            # Check branch name for the issue number (e.g., frontend/5-slug)
            import re
            if re.search(rf'(?:^|/)(?:{issue_number})\b', branch):
                return True
    except Exception:
        pass
    return False


def gh_get_pr_reviews(pr_number):
    """Get all reviews on a PR."""
    return gh_get(f"/repos/{GITHUB_REPO}/pulls/{pr_number}/reviews")


def gh_get_review_comments(pr_number):
    """Get inline review comments on a PR."""
    return gh_get(f"/repos/{GITHUB_REPO}/pulls/{pr_number}/comments")


def gh_get_review_feedback(pr_number):
    """Bundle all review feedback (CHANGES_REQUESTED reviews + inline comments) for a dev."""
    reviews = gh_get_pr_reviews(pr_number)
    comments = gh_get_review_comments(pr_number)

    feedback = []

    # Collect review-level feedback
    for review in reviews:
        if review.get("state") == "CHANGES_REQUESTED":
            feedback.append({
                "type": "review",
                "reviewer": review.get("user", {}).get("login", "unknown"),
                "body": review.get("body", ""),
            })

    # Collect inline comments
    for comment in comments:
        feedback.append({
            "type": "inline_comment",
            "reviewer": comment.get("user", {}).get("login", "unknown"),
            "path": comment.get("path", ""),
            "line": comment.get("line") or comment.get("original_line"),
            "body": comment.get("body", ""),
        })

    return feedback


def gh_check_both_approved(pr_number):
    """Check if both QA and Security have approved the PR (latest review from each).

    Returns True if the latest review from a QA agent AND the latest review from
    a Security agent are both APPROVED. Identifies reviewers by review body markers.
    """
    reviews = gh_get_pr_reviews(pr_number)

    qa_approved = False
    security_approved = False

    # Walk reviews in order (oldest first) — last one per type wins
    for review in reviews:
        body = review.get("body", "")
        state = review.get("state", "")

        if "QA Review:" in body:
            qa_approved = (state == "APPROVED")
        elif "Security Review:" in body:
            security_approved = (state == "APPROVED")

    return qa_approved and security_approved


# ─── State ───────────────────────────────────────────────

class DaemonState:
    def __init__(self):
        self.processed = set()
        self.active_containers = {}
        self.frontend_dev_count = 0
        self.backend_dev_count = 0
        self.fix_iterations = {}  # pr_number -> iteration count
        self.retry_counts = {}  # "role-number" -> transient retry count
        self.retry_backoff_until = {}  # "role-number" -> datetime to wait until
        self.dev_queue = []  # list of (role, issue) waiting for a slot
        self.lock = threading.Lock()

    def already_handled(self, key):
        if key in self.processed:
            return True
        self.processed.add(key)
        return False

    def clear_handled(self, key):
        """Allow an event key to be re-processed (used for iterative review loops)."""
        self.processed.discard(key)


state = DaemonState()
docker_client = docker.from_env()


# ─── Container Management ────────────────────────────────

def spawn_agent(role, task_context, issue_or_pr_number):
    with state.lock:
        # Prevent duplicate agents for the same role + issue/PR
        for info in state.active_containers.values():
            if info["role"] == role and info["number"] == issue_or_pr_number:
                log.debug(f"Skipping {role} for #{issue_or_pr_number} — already running")
                return None
        # Respect transient error backoff
        retry_key = f"{role}-{issue_or_pr_number}"
        backoff_until = state.retry_backoff_until.get(retry_key)
        if backoff_until and datetime.now(timezone.utc) < backoff_until:
            remaining = int((backoff_until - datetime.now(timezone.utc)).total_seconds())
            log.debug(f"Skipping {role} for #{issue_or_pr_number} — backoff ({remaining}s remaining)")
            return None
        # Global concurrency limit (QA/Security are lightweight, exempt them;
        # architect merges are quick and critical, exempt them too)
        if role not in ("qa", "security", "architect") and len(state.active_containers) >= MAX_TOTAL_AGENTS:
            log.info(f"Max agents ({MAX_TOTAL_AGENTS}) reached — deferring {role} for #{issue_or_pr_number}")
            return None

    profile = RESOURCE_PROFILES[role]
    container_name = f"{PROJECT_NAME}-{role}-{issue_or_pr_number}-{int(time.time())}"

    # Write task file inside daemon container
    task_dir = Path(f"/tmp/agent-tasks/{container_name}")
    task_dir.mkdir(parents=True, exist_ok=True)
    task_file = task_dir / "task.json"
    task_file.write_text(json.dumps(task_context, indent=2))

    # Host path for volume mount (daemon's /tmp/agent-tasks maps to host /tmp/{PROJECT_NAME}-tasks)
    host_task_dir = f"/tmp/{PROJECT_NAME}-tasks/{container_name}"

    log.info(f"Spawning {role} for #{issue_or_pr_number}")

    try:
        env = {
            "GITHUB_TOKEN": GITHUB_TOKEN,
            "GITHUB_REPO": GITHUB_REPO,
            "AGENT_ROLE": role,
        }
        if ANTHROPIC_API_KEY:
            env["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY

        volumes = {
            REPO_CACHE_VOLUME: {"bind": "/repo", "mode": "ro"},
            host_task_dir: {"bind": "/tmp/task", "mode": "ro"},
            MEMORY_VOLUME: {"bind": "/memory", "mode": "rw"},
        }
        # Mount Claude subscription credentials if provided (instead of API key)
        if CLAUDE_CREDENTIALS_PATH and os.path.exists(CLAUDE_CREDENTIALS_PATH):
            volumes[CLAUDE_CREDENTIALS_PATH] = {
                "bind": "/root/.claude/.credentials.json", "mode": "ro",
            }

        # Increment dev count before spawning to prevent race conditions
        with state.lock:
            if role == "frontend-dev":
                state.frontend_dev_count += 1
            elif role == "backend-dev":
                state.backend_dev_count += 1

        try:
            container = docker_client.containers.run(
                image=f"agent-{role}:latest",
                name=container_name,
                command=["/tmp/task/task.json"],
                environment=env,
                volumes=volumes,
                mem_limit=profile["mem_limit"],
                nano_cpus=int(profile["cpus"] * 1e9),
                detach=True,
                auto_remove=False,
                network_mode="host",
            )
        except Exception:
            # Roll back count if container failed to start
            with state.lock:
                if role == "frontend-dev":
                    state.frontend_dev_count -= 1
                elif role == "backend-dev":
                    state.backend_dev_count -= 1
            raise

        with state.lock:
            state.active_containers[container.id] = {
                "role": role, "number": issue_or_pr_number,
                "container": container, "name": container_name,
                "started": datetime.now(timezone.utc),
            }

        log.info(f"✓ {container_name} started ({container.short_id})")
        threading.Thread(target=monitor_container, args=(container.id,), daemon=True).start()
        return container

    except Exception as e:
        log.error(f"✗ Failed: {e}")
        return None


def monitor_container(container_id):
    try:
        info = state.active_containers.get(container_id)
        if not info:
            return

        container = info["container"]
        result = container.wait()
        exit_code = result.get("StatusCode", -1)

        try:
            logs = container.logs(tail=100).decode("utf-8", errors="replace")
            Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
            (Path(LOG_DIR) / f"{info['name']}.log").write_text(logs)
        except Exception:
            pass

        if exit_code == 0:
            log.info(f"✓ {info['name']} completed")
            # Clear retry state on success
            retry_key = f"{info['role']}-{info['number']}"
            with state.lock:
                state.retry_counts.pop(retry_key, None)
                state.retry_backoff_until.pop(retry_key, None)
            # Agents post their own review comments directly — no need for
            # daemon-verified duplicates (they spam the PR and break pagination).
        else:
            log.warning(f"✗ {info['name']} exited ({exit_code})")

            # Check if it's a transient error (rate limit, auth expiry)
            is_transient = False
            try:
                logs_text = container.logs(tail=50).decode("utf-8", errors="replace")
                is_transient = any(s in logs_text.lower() for s in [
                    "rate limit", "usage limit", "too many requests",
                    "429", "overloaded", "capacity",
                    "authentication_error", "401", "invalid authentication",
                ])
            except Exception:
                pass

            if is_transient:
                retry_key = f"{info['role']}-{info['number']}"
                with state.lock:
                    retries = state.retry_counts.get(retry_key, 0) + 1
                    state.retry_counts[retry_key] = retries

                if retries > MAX_TRANSIENT_RETRIES:
                    log.warning(f"✗ {info['name']} exceeded max transient retries ({MAX_TRANSIENT_RETRIES})")
                    gh_comment(info["number"],
                               f"⚠️ Agent `{info['role']}` failed after {MAX_TRANSIENT_RETRIES} retries "
                               f"(rate limit / transient error). Requires manual intervention.")
                else:
                    # Exponential backoff: 60s, 120s, 240s, 480s, 960s
                    backoff = TRANSIENT_BACKOFF_BASE * (2 ** (retries - 1))
                    backoff_until = datetime.now(timezone.utc) + timedelta(seconds=backoff)
                    with state.lock:
                        state.retry_backoff_until[retry_key] = backoff_until

                    state.clear_handled(f"{info['role']}-{info['number']}")
                    if info["role"] in ("frontend-dev", "backend-dev"):
                        gh_remove_label(info["number"], "dev-in-progress")
                        gh_add_label(info["number"], info["role"])
                    elif info["role"] == "architect":
                        gh_remove_label(info["number"], "architect-in-progress")
                        gh_add_label(info["number"], "architect")

                    if retries == 1:
                        gh_comment(info["number"],
                                   f"⏳ Agent `{info['role']}` hit a transient error. "
                                   f"Will retry in {backoff}s (attempt {retries}/{MAX_TRANSIENT_RETRIES}).")
                    log.info(f"↻ {info['name']} transient error — retry {retries}/{MAX_TRANSIENT_RETRIES} "
                             f"in {backoff}s")
            else:
                gh_comment(info["number"],
                           f"⚠️ Agent `{info['role']}` errored (exit {exit_code}). Check daemon logs.")

        try:
            container.remove()
        except Exception:
            pass

        is_dev = info["role"] in ("frontend-dev", "backend-dev")
        with state.lock:
            state.active_containers.pop(container_id, None)
            if info["role"] == "frontend-dev":
                state.frontend_dev_count -= 1
            elif info["role"] == "backend-dev":
                state.backend_dev_count -= 1

        if is_dev:
            drain_queue()

    except Exception as e:
        log.error(f"Monitor error: {e}")
        with state.lock:
            info = state.active_containers.pop(container_id, None)
            if info:
                is_dev = info["role"] in ("frontend-dev", "backend-dev")
                if info["role"] == "frontend-dev":
                    state.frontend_dev_count -= 1
                elif info["role"] == "backend-dev":
                    state.backend_dev_count -= 1
        if info and is_dev:
            drain_queue()


# ─── Event Dispatch ──────────────────────────────────────

def dispatch_architect(issue):
    number = issue["number"]
    key = f"architect-{number}"
    if state.already_handled(key):
        return

    log.info(f"Architect issue: #{number} — {issue['title']}")

    result = spawn_agent("architect", {
        "action": "plan_feature",
        "issue_number": number,
        "issue_title": issue["title"],
        "issue_body": issue.get("body", ""),
        "issue_url": issue.get("html_url", ""),
    }, number)
    if result is None:
        state.clear_handled(key)
        return
    gh_remove_label(number, "architect")
    gh_add_label(number, "architect-in-progress")


def dispatch_frontend_dev(issue):
    number = issue["number"]
    key = f"frontend-dev-{number}"
    if state.already_handled(key):
        return

    with state.lock:
        if state.frontend_dev_count >= MAX_FRONTEND_AGENTS:
            # Queue it — will be picked up when a slot frees
            already_queued = any(r == "frontend-dev" and i["number"] == number for r, i in state.dev_queue)
            if not already_queued:
                state.dev_queue.append(("frontend-dev", issue))
                log.info(f"Queued: #{number} — {issue['title']} (frontend-dev, {len(state.dev_queue)} in queue)")
            state.processed.discard(key)
            return

    log.info(f"Frontend dev issue: #{number} — {issue['title']}")
    gh_remove_label(number, "frontend-dev")
    gh_add_label(number, "dev-in-progress")

    spawn_agent("frontend-dev", {
        "action": "implement_issue",
        "issue_number": number,
        "issue_title": issue["title"],
        "issue_body": issue.get("body", ""),
        "issue_url": issue.get("html_url", ""),
    }, number)


def dispatch_backend_dev(issue):
    number = issue["number"]
    key = f"backend-dev-{number}"
    if state.already_handled(key):
        return

    with state.lock:
        if state.backend_dev_count >= MAX_BACKEND_AGENTS:
            already_queued = any(r == "backend-dev" and i["number"] == number for r, i in state.dev_queue)
            if not already_queued:
                state.dev_queue.append(("backend-dev", issue))
                log.info(f"Queued: #{number} — {issue['title']} (backend-dev, {len(state.dev_queue)} in queue)")
            state.processed.discard(key)
            return

    log.info(f"Backend dev issue: #{number} — {issue['title']}")
    gh_remove_label(number, "backend-dev")
    gh_add_label(number, "dev-in-progress")

    spawn_agent("backend-dev", {
        "action": "implement_issue",
        "issue_number": number,
        "issue_title": issue["title"],
        "issue_body": issue.get("body", ""),
        "issue_url": issue.get("html_url", ""),
    }, number)


def drain_queue():
    """Try to dispatch queued issues now that a slot may be free."""
    with state.lock:
        queue_copy = list(state.dev_queue)

    for role, issue in queue_copy:
        if role == "frontend-dev":
            dispatch_frontend_dev(issue)
        elif role == "backend-dev":
            dispatch_backend_dev(issue)
        # If it dispatched successfully, remove from queue
        with state.lock:
            key = f"{role}-{issue['number']}"
            if key in state.processed:
                state.dev_queue = [(r, i) for r, i in state.dev_queue if not (r == role and i["number"] == issue["number"])]


def dispatch_dependents(pr):
    """When a PR is merged, find issues that depended on it and trigger them."""
    pr_number = pr["number"]
    pr_body = pr.get("body", "") or ""

    # Find which issue this PR closed (e.g. "Closes #2", "Fixes #2", "Part of #1")
    import re
    closed_issues = set(int(n) for n in re.findall(
        r'(?:closes|fixes|resolves|part of)\s+#(\d+)', pr_body, re.IGNORECASE
    ))
    if not closed_issues:
        return

    log.info(f"PR #{pr_number} merged, closed issues: {closed_issues}")

    # Scan open issues for ones that depend on the closed issues
    try:
        resp = gh_get(f"/repos/{GITHUB_REPO}/issues", params={"state": "open", "per_page": 100})
        if not isinstance(resp, list):
            return

        for issue in resp:
            if issue.get("pull_request"):
                continue  # skip PRs
            body = issue.get("body", "") or ""
            labels = [l["name"] for l in issue.get("labels", [])]

            # Check if this issue depends on any of the closed issues
            depends_on = set(int(n) for n in re.findall(
                r'(?:depends on|blocked by|after)\s+#(\d+)', body, re.IGNORECASE
            ))
            if not depends_on.intersection(closed_issues):
                continue

            # Check if all dependencies are now resolved (closed)
            all_resolved = True
            for dep in depends_on:
                try:
                    dep_issue = gh_get(f"/repos/{GITHUB_REPO}/issues/{dep}")
                    if dep_issue.get("state") != "closed":
                        all_resolved = False
                        break
                except Exception:
                    all_resolved = False
                    break

            if not all_resolved:
                log.info(f"Issue #{issue['number']} still has unresolved dependencies")
                continue

            # Trigger the appropriate dev agent
            issue_number = issue["number"]
            if "frontend-dev" in labels:
                log.info(f"Unblocked: #{issue_number} — dispatching frontend-dev")
                dispatch_frontend_dev(issue)
            elif "backend-dev" in labels:
                log.info(f"Unblocked: #{issue_number} — dispatching backend-dev")
                dispatch_backend_dev(issue)
            else:
                log.info(f"Unblocked: #{issue_number} — no dev label, skipping")

    except Exception as e:
        log.error(f"Error checking dependents: {e}")


def dispatch_qa(pr):
    number = pr["number"]
    key = f"qa-{number}"
    if state.already_handled(key):
        return
    branch = pr.get("head", {}).get("ref", "")
    if branch.startswith("docs/"):
        return
    result = spawn_agent("qa", {
        "action": "review_pr",
        "pr_number": number,
        "pr_title": pr["title"],
        "pr_body": pr.get("body", ""),
        "pr_url": pr.get("html_url", ""),
        "pr_branch": branch,
    }, number)
    if result:
        log.info(f"QA review: PR #{number} — {pr['title']}")
    else:
        state.clear_handled(key)


def dispatch_security(pr):
    number = pr["number"]
    key = f"security-{number}"
    if state.already_handled(key):
        return
    branch = pr.get("head", {}).get("ref", "")
    if branch.startswith("docs/"):
        return
    result = spawn_agent("security", {
        "action": "review_pr",
        "pr_number": number,
        "pr_title": pr["title"],
        "pr_body": pr.get("body", ""),
        "pr_url": pr.get("html_url", ""),
        "pr_branch": branch,
    }, number)
    if result:
        log.info(f"Security review: PR #{number} — {pr['title']}")
    else:
        state.clear_handled(key)


def _latest_reviews_approved(pr_number):
    """Check if the latest QA and Security comments are approvals (not changes requested).
    Returns True if both have reviewed and both approved, meaning no fixes are needed."""
    try:
        resp = requests.get(
            f"{API}/repos/{GITHUB_REPO}/issues/{pr_number}/comments",
            headers=HEADERS,
            params={"per_page": 100},
        )
        if resp.status_code != 200:
            return False
        # Follow pagination to last page for heavily-commented PRs
        link_header = resp.headers.get("Link", "")
        if 'rel="last"' in link_header:
            last_match = re.search(r'<([^>]+)>;\s*rel="last"', link_header)
            if last_match:
                last_resp = requests.get(last_match.group(1), headers=HEADERS)
                if last_resp.status_code == 200:
                    comments = last_resp.json()
                else:
                    comments = resp.json()
            else:
                comments = resp.json()
        else:
            comments = resp.json()

        last_qa_approved = False
        last_sec_approved = False
        has_qa = False
        has_sec = False
        for c in reversed(comments):
            body = c.get("body", "")
            if "Daemon-verified:" in body or "Agent `" in body:
                continue
            # Only check the first line (header) for verdict to avoid false positives
            # from ✅ checkmarks in the body of CHANGES REQUESTED reviews
            header = body.split("\n")[0] if body else ""
            if "QA Review" in header and not has_qa:
                has_qa = True
                if "CHANGES REQUESTED" in header.upper():
                    last_qa_approved = False
                else:
                    last_qa_approved = "APPROVED" in header.upper() or "✅" in header
            if "Security Review" in header and not has_sec:
                has_sec = True
                if "CHANGES REQUESTED" in header.upper():
                    last_sec_approved = False
                else:
                    last_sec_approved = "APPROVED" in header.upper() or "✅" in header

        return has_qa and last_qa_approved and has_sec and last_sec_approved
    except Exception:
        return False


def dispatch_needs_fixes(pr):
    """Re-spawn the right dev agent with review feedback context."""
    number = pr["number"]
    branch = pr.get("head", {}).get("ref", "")

    # Determine dev type from branch prefix
    if branch.startswith("frontend/"):
        dev_role = "frontend-dev"
    elif branch.startswith("backend/"):
        dev_role = "backend-dev"
    else:
        log.warning(f"PR #{number} branch '{branch}' has no frontend/ or backend/ prefix — skipping")
        return

    # If the latest reviews are both approvals, the fix is already applied — skip
    if _latest_reviews_approved(number):
        log.info(f"PR #{number} — latest reviews are approvals, skipping fix dispatch")
        gh_remove_label(number, "needs-fixes")
        return

    # Check iteration limit — count existing fix commits in PR comments to survive restarts
    try:
        comments = requests.get(
            f"{API}/repos/{GITHUB_REPO}/issues/{number}/comments",
            headers=HEADERS,
        ).json()
        fix_count = sum(1 for c in comments if isinstance(c, dict) and "address review feedback" in c.get("body", "").lower())
        # Also count "needs fixes" dispatch logs
        with state.lock:
            mem_iterations = state.fix_iterations.get(number, 0)
        iterations = max(fix_count, mem_iterations) + 1
        state.fix_iterations[number] = iterations
    except Exception:
        with state.lock:
            iterations = state.fix_iterations.get(number, 0) + 1
            state.fix_iterations[number] = iterations

    if iterations > MAX_FIX_ITERATIONS:
        # Only log once per restart, and mark as handled so we stop retrying
        key = f"max-fix-{number}"
        if state.already_handled(key):
            return
        log.warning(f"PR #{number} hit max fix iterations ({MAX_FIX_ITERATIONS})")
        gh_comment(number,
                   f"⚠️ This PR has gone through {MAX_FIX_ITERATIONS} review/fix cycles. "
                   f"Requesting human intervention — please review and guide the next steps.")
        return

    log.info(f"Needs fixes: PR #{number} (iteration {iterations}) — spawning {dev_role}")

    # Remove needs-fixes label so it can be re-applied after next review
    gh_remove_label(number, "needs-fixes")

    # Fetch review feedback to pass to the dev
    feedback = gh_get_review_feedback(number)

    spawn_agent(dev_role, {
        "action": "fix_review_feedback",
        "pr_number": number,
        "pr_title": pr["title"],
        "pr_body": pr.get("body", ""),
        "pr_url": pr.get("html_url", ""),
        "pr_branch": branch,
        "fix_iteration": iterations,
        "review_feedback": feedback,
    }, number)


def dispatch_architect_merge(pr):
    """Spawn architect to merge an approved PR."""
    number = pr["number"]
    key = f"merge-{number}"
    if state.already_handled(key):
        return

    log.info(f"Both approved: PR #{number} — spawning Architect to merge")
    result = spawn_agent("architect", {
        "action": "merge_approved_pr",
        "pr_number": number,
        "pr_title": pr["title"],
        "pr_body": pr.get("body", ""),
        "pr_url": pr.get("html_url", ""),
        "pr_branch": pr.get("head", {}).get("ref", ""),
    }, number)
    if result is None:
        # Spawn failed — clear handled state so it retries on next poll
        state.clear_handled(key)


def _check_both_approved(pr_number, pr_api_url, latest_comment):
    """Check PR comments for both QA and Security approval, then merge."""
    try:
        # Fetch all comments on the PR
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/issues/{pr_number}/comments",
            headers={"Authorization": f"token {GITHUB_TOKEN}",
                     "Accept": "application/vnd.github.v3+json"},
        )
        comments = resp.json() if resp.status_code == 200 else []
        all_text = " ".join(c.get("body", "") for c in comments)

        has_qa = "QA Review" in all_text or "QA review" in all_text
        has_security = "Security Review" in all_text or "Security review" in all_text
        qa_approved = has_qa and ("APPROVED" in all_text.upper() or "✅" in all_text)
        security_approved = has_security and any(
            ("Security" in c.get("body", "") and ("APPROVED" in c.get("body", "").upper() or "✅" in c.get("body", "")))
            for c in comments
        )

        if qa_approved and security_approved:
            # Fetch full PR object for dispatch
            pr_resp = requests.get(
                pr_api_url,
                headers={"Authorization": f"token {GITHUB_TOKEN}",
                         "Accept": "application/vnd.github.v3+json"},
            )
            if pr_resp.status_code == 200:
                dispatch_architect_merge(pr_resp.json())
            else:
                log.warning(f"Could not fetch PR #{pr_number}: HTTP {pr_resp.status_code}")
        else:
            log.info(f"PR #{pr_number} — QA approved: {qa_approved}, Security approved: {security_approved}")
    except Exception as e:
        log.error(f"Error checking approvals for PR #{pr_number}: {e}")


def _check_both_approved_from_review(pr):
    """Fallback: check via formal review (works if different tokens are used)."""
    number = pr["number"]
    pr_url = pr.get("url", f"https://api.github.com/repos/{GITHUB_REPO}/pulls/{number}")
    _check_both_approved(number, pr_url, "")


# ═══════════════════════════════════════════════════════════
#  MODE 1: WEBHOOK SERVER (instant, preferred)
# ═══════════════════════════════════════════════════════════

class WebhookHandler(BaseHTTPRequestHandler):
    """Receives GitHub webhook POST requests."""

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Verify HMAC signature if secret is configured
        if WEBHOOK_SECRET:
            signature = self.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(
                WEBHOOK_SECRET.encode(), body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                log.warning("Invalid webhook signature — rejected")
                self.send_response(403)
                self.end_headers()
                return

        event = self.headers.get("X-GitHub-Event", "")
        payload = json.loads(body)

        # Respond 200 immediately — process async
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

        threading.Thread(
            target=handle_webhook_event,
            args=(event, payload),
            daemon=True,
        ).start()

    def do_GET(self):
        """Health check endpoint."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        with state.lock:
            active = [{"role": v["role"], "issue": v["number"]}
                      for v in state.active_containers.values()]
        self.wfile.write(json.dumps({
            "status": "ok",
            "mode": MODE,
            "repo": GITHUB_REPO,
            "active_agents": active,
            "frontend_dev_slots": f"{state.frontend_dev_count}/{MAX_FRONTEND_AGENTS}",
            "backend_dev_slots": f"{state.backend_dev_count}/{MAX_BACKEND_AGENTS}",
        }).encode())

    def log_message(self, format, *args):
        pass  # Suppress default HTTP request logs


def handle_webhook_event(event, payload):
    """Route a GitHub webhook event to the right agent."""
    action = payload.get("action", "")
    repo = payload.get("repository", {}).get("full_name", "")

    log.info(f"Webhook: {event}.{action} from {repo}")

    # ── Issue labeled ─────────────────────────────────
    if event == "issues" and action == "labeled":
        issue = payload.get("issue", {})
        label_name = payload.get("label", {}).get("name", "")

        if label_name == "architect":
            dispatch_architect(issue)
        elif label_name == "frontend-dev":
            dispatch_frontend_dev(issue)
        elif label_name == "backend-dev":
            dispatch_backend_dev(issue)

    # ── PR merged → unblock dependent issues ───────────
    elif event == "pull_request" and action == "closed":
        pr = payload.get("pull_request", {})
        if pr.get("merged"):
            dispatch_dependents(pr)

    # ── PR opened or updated ──────────────────────────
    # Run QA and Security in parallel (agents share a token so formal
    # PR approvals don't work — we track approval via comments instead)
    elif event == "pull_request" and action in ("opened", "reopened", "synchronize"):
        pr = payload.get("pull_request", {})
        if not pr.get("draft"):
            # Clear previous review state so QA/Security re-run on new code
            state.clear_handled(f"qa-{pr['number']}")
            state.clear_handled(f"security-{pr['number']}")
            dispatch_qa(pr)
            dispatch_security(pr)

    # ── PR labeled (needs-fixes) ──────────────────────
    elif event == "pull_request" and action == "labeled":
        pr = payload.get("pull_request", {})
        label_name = payload.get("label", {}).get("name", "")
        if label_name == "needs-fixes":
            dispatch_needs_fixes(pr)

    # ── PR comment with approval keyword ──────────────
    # Agents can't submit formal reviews (same token as PR author),
    # so we detect approval from comment text instead.
    elif event == "issue_comment" and action == "created":
        comment_body = payload.get("comment", {}).get("body", "")
        issue = payload.get("issue", {})
        # Only care about PR comments (issues with pull_request key)
        if issue.get("pull_request"):
            # Skip daemon-posted comments to avoid self-triggering loops
            if "Daemon-verified:" in comment_body or "Agent `" in comment_body:
                return
            pr_number = issue["number"]
            pr_url = issue["pull_request"].get("url", "")
            # Check the header line only to avoid false positives from ✅ in body
            comment_header = comment_body.split("\n")[0] if comment_body else ""
            if "CHANGES REQUESTED" in comment_header.upper() or "❌" in comment_header:
                # QA or Security requested changes — add label and trigger fix flow
                log.info(f"Changes requested on PR #{pr_number} via comment")
                gh_add_label(pr_number, "needs-fixes")
                try:
                    pr_resp = requests.get(
                        pr_url,
                        headers={"Authorization": f"token {GITHUB_TOKEN}",
                                 "Accept": "application/vnd.github.v3+json"},
                    )
                    if pr_resp.status_code == 200:
                        dispatch_needs_fixes(pr_resp.json())
                except Exception as e:
                    log.error(f"Error triggering fixes for PR #{pr_number}: {e}")
            elif "APPROVED" in comment_header.upper() or "✅" in comment_header:
                if pr_url:
                    _check_both_approved(pr_number, pr_url, comment_body)

    # ── PR review submitted (fallback) ────────────────
    elif event == "pull_request_review" and action == "submitted":
        review = payload.get("review", {})
        pr = payload.get("pull_request", {})
        if review.get("state") == "approved":
            _check_both_approved_from_review(pr)

    # ── Ping (setup confirmation) ─────────────────────
    elif event == "ping":
        log.info(f"Webhook connected: {repo}")


def webhook_retry_loop():
    """Background loop for webhook mode: retries agents after transient error backoff,
    and runs a periodic safety-net poll to recover stuck issues."""
    poll_counter = 0
    while True:
        time.sleep(60)
        poll_counter += 1
        try:
            # Check for expired backoffs
            has_expired = False
            with state.lock:
                now = datetime.now(timezone.utc)
                for key, until in list(state.retry_backoff_until.items()):
                    if now >= until:
                        has_expired = True
                        break

            # Run poll if backoff expired OR every 5 minutes as a safety net
            if has_expired or poll_counter % 5 == 0:
                if has_expired:
                    log.info("Retry loop: backoff expired, running poll to recover")
                poll_github()
        except Exception as e:
            log.debug(f"Retry loop: {e}")


def run_webhook_server():
    server = HTTPServer(("0.0.0.0", WEBHOOK_PORT), WebhookHandler)
    log.info(f"Webhook server on port {WEBHOOK_PORT}")
    log.info(f"")
    log.info(f"   SETUP: Add webhook in GitHub repo settings")
    log.info(f"   URL: http://<tunnel>:{WEBHOOK_PORT}")
    log.info(f"   Content type: application/json")
    log.info(f"   Secret: (match WEBHOOK_SECRET in .env)")
    log.info(f"   Events: Issues, Pull requests, Pull request reviews")
    log.info(f"")
    log.info(f"   Health check: GET http://localhost:{WEBHOOK_PORT}/")
    server.serve_forever()


# ═══════════════════════════════════════════════════════════
#  MODE 2: POLLING (fallback — no tunnel needed)
# ═══════════════════════════════════════════════════════════

def poll_github():
    """Poll GitHub for work. Uses GitHub state as source of truth.
    spawn_agent's duplicate check prevents double-spawning."""

    # Issues ready to go
    for issue in gh_get_issues("architect"):
        number = issue["number"]
        # spawn_agent checks for duplicates, so just try to spawn
        result = spawn_agent("architect", {
            "action": "plan_feature",
            "issue_number": number,
            "issue_title": issue["title"],
            "issue_body": issue.get("body", ""),
            "issue_url": issue.get("html_url", ""),
        }, number)
        if result:
            log.info(f"Architect issue: #{number} — {issue['title']}")
            gh_remove_label(number, "architect")
            gh_add_label(number, "architect-in-progress")

    for issue in gh_get_issues("frontend-dev"):
        dispatch_frontend_dev(issue)
    for issue in gh_get_issues("backend-dev"):
        dispatch_backend_dev(issue)

    # Recover labeled issues that were handled but have no agent running and no PR.
    # This catches cases where an agent failed before swapping labels (e.g., rate limit
    # on first attempt, container crash before label change).
    with state.lock:
        active_numbers = {v["number"] for v in state.active_containers.values()}
    for label, dispatch_fn in [("frontend-dev", dispatch_frontend_dev), ("backend-dev", dispatch_backend_dev)]:
        for issue in gh_get_issues(label):
            number = issue["number"]
            key = f"{label}-{number}"
            if key in state.processed and number not in active_numbers and not gh_issue_has_open_pr(number):
                log.info(f"Recovering stuck #{number} — clearing handled state for {label}")
                state.clear_handled(key)
                dispatch_fn(issue)

    # Stuck architect-in-progress issues (no agent running)
    with state.lock:
        active_numbers = {v["number"] for v in state.active_containers.values()}
    for issue in gh_get_issues("architect-in-progress"):
        if issue.get("state") == "closed" or issue["number"] in active_numbers:
            continue
        log.info(f"Recovering stuck architect #{issue['number']}")
        gh_remove_label(issue["number"], "architect-in-progress")
        gh_add_label(issue["number"], "architect")

    # Stuck dev-in-progress issues (no agent running, no open PR)
    with state.lock:
        active_numbers = {v["number"] for v in state.active_containers.values()}
    for issue in gh_get_issues("dev-in-progress"):
        if issue.get("state") == "closed" or issue["number"] in active_numbers:
            continue
        if gh_issue_has_open_pr(issue["number"]):
            continue
        body = issue.get("body", "") or ""
        other_labels = [l["name"] for l in issue.get("labels", []) if l["name"] != "dev-in-progress"]
        if "backend" in " ".join(other_labels).lower() or "backend" in body.lower():
            dev_label = "backend-dev"
        else:
            dev_label = "frontend-dev"
        log.info(f"Recovering stuck #{issue['number']} → {dev_label}")
        gh_remove_label(issue["number"], "dev-in-progress")
        gh_add_label(issue["number"], dev_label)

    # Open PRs — check review state and take action
    for pr in gh_get_prs("open"):
        if pr.get("draft"):
            continue
        pr_number = pr["number"]
        try:
            # Fetch the LAST page of comments (most recent) to find latest review state.
            # GitHub Issues Comments API always returns chronologically; use Link header
            # to jump to the last page so we see recent reviews on heavily-commented PRs.
            resp = requests.get(
                f"{API}/repos/{GITHUB_REPO}/issues/{pr_number}/comments",
                headers=HEADERS,
                params={"per_page": 100},
            )
            if resp.status_code != 200:
                continue
            link_header = resp.headers.get("Link", "")
            if 'rel="last"' in link_header:
                last_match = re.search(r'<([^>]+)>;\s*rel="last"', link_header)
                if last_match:
                    last_resp = requests.get(last_match.group(1), headers=HEADERS)
                    if last_resp.status_code == 200:
                        comments = last_resp.json()
                    else:
                        comments = resp.json()
                else:
                    comments = resp.json()
            else:
                comments = resp.json()

            # Parse the LATEST state from comments (not just any comment)
            # Look at comments in reverse to find the most recent review
            last_qa_approved = False
            last_sec_approved = False
            last_changes_requested = False
            has_qa = False
            has_sec = False
            for c in reversed(comments):
                body = c.get("body", "")
                # Skip daemon-generated comments — only trust agent-posted reviews
                if "Daemon-verified:" in body or "Agent `" in body:
                    continue
                # Only check the first line (header) for verdict to avoid false positives
                # from ✅ checkmarks in the body of CHANGES REQUESTED reviews
                header = body.split("\n")[0] if body else ""
                if "QA Review" in header and not has_qa:
                    has_qa = True
                    if "CHANGES REQUESTED" in header.upper():
                        last_changes_requested = True
                    else:
                        last_qa_approved = "APPROVED" in header.upper() or "✅" in header
                if "Security Review" in header and not has_sec:
                    has_sec = True
                    if "CHANGES REQUESTED" in header.upper():
                        last_changes_requested = True
                    else:
                        last_sec_approved = "APPROVED" in header.upper() or "✅" in header

            if last_qa_approved and last_sec_approved and not last_changes_requested:
                # Both approved — merge
                result = spawn_agent("architect", {
                    "action": "merge_approved_pr",
                    "pr_number": pr_number,
                    "pr_title": pr["title"],
                    "pr_body": pr.get("body", ""),
                    "pr_url": pr.get("html_url", ""),
                    "pr_branch": pr.get("head", {}).get("ref", ""),
                }, pr_number)
                if result:
                    log.info(f"Both approved: PR #{pr_number} — merging")
            elif last_changes_requested:
                dispatch_needs_fixes(pr)
            else:
                # Dispatch whichever reviewers haven't run yet
                # Clear handled state so dispatch_qa/dispatch_security can proceed
                if not has_qa:
                    state.clear_handled(f"qa-{pr_number}")
                    dispatch_qa(pr)
                if not has_sec:
                    state.clear_handled(f"security-{pr_number}")
                    dispatch_security(pr)
        except Exception as e:
            log.debug(f"Poll PR #{pr_number}: {e}")


def run_poller():
    log.info(f"Polling every {POLL_INTERVAL}s")
    poll_count = 0
    while True:
        try:
            poll_github()
            poll_count += 1
            if poll_count % 5 == 0:
                status_line()
        except requests.exceptions.RequestException as e:
            log.warning(f"GitHub API error: {e}")
        except Exception as e:
            log.error(f"Error: {e}", exc_info=True)
        time.sleep(POLL_INTERVAL)


# ─── Repo Cache ──────────────────────────────────────────

def update_repo_cache():
    try:
        docker_client.containers.run(
            image="agent-base:latest",
            entrypoint="bash",
            command=["-c", """
                cd /repo
                if [ -d .git ]; then
                    git fetch origin main --quiet
                    git reset --hard origin/main --quiet
                else
                    git clone https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPO}.git .
                fi
            """],
            environment={"GITHUB_TOKEN": GITHUB_TOKEN, "GITHUB_REPO": GITHUB_REPO},
            volumes={REPO_CACHE_VOLUME: {"bind": "/repo", "mode": "rw"}},
            remove=True,
        )
    except Exception as e:
        log.error(f"Repo cache update failed: {e}")


def cleanup_memory():
    """Trim agent memory to prevent unbounded growth."""
    try:
        docker_client.containers.run(
            image="agent-base:latest",
            entrypoint="bash",
            command=["-c", """
                # Trim agent logs to last 200 lines each
                for f in /memory/agents/*/log.md; do
                    [ -f "$f" ] || continue
                    lines=$(wc -l < "$f")
                    if [ "$lines" -gt 200 ]; then
                        tail -200 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
                    fi
                done
                # Delete read inbox messages older than 7 days
                find /memory/inbox/*/read/ -name "*.md" -mtime +7 -delete 2>/dev/null
                # Delete issue notes for issues not active in 30 days
                find /memory/issues/ -name "notes.md" -mtime +30 -delete 2>/dev/null
                find /memory/issues/ -type d -empty -delete 2>/dev/null
            """],
            volumes={MEMORY_VOLUME: {"bind": "/memory", "mode": "rw"}},
            remove=True,
        )
    except Exception as e:
        log.debug(f"Memory cleanup: {e}")


def repo_cache_loop():
    while True:
        time.sleep(300)
        update_repo_cache()
        cleanup_memory()
        # Safety net: kill any containers over the global limit
        try:
            agent_containers = [
                c for c in docker_client.containers.list(filters={"name": f"{PROJECT_NAME}-"})
                if not c.name.endswith("-daemon") and not c.name.endswith("-tunnel")
            ]
            if len(agent_containers) > MAX_TOTAL_AGENTS:
                log.warning(f"Safety net: {len(agent_containers)} agents running (limit {MAX_TOTAL_AGENTS}) — stopping excess")
                # Sort by creation time, keep newest, stop oldest extras
                agent_containers.sort(key=lambda c: c.attrs.get("Created", ""), reverse=True)
                for c in agent_containers[MAX_TOTAL_AGENTS:]:
                    log.warning(f"Stopping excess container: {c.name}")
                    c.stop(timeout=10)
        except Exception as e:
            log.debug(f"Safety net check: {e}")


    # retry_loop removed — poll_github handles everything directly


# ─── Status ──────────────────────────────────────────────

def status_line():
    with state.lock:
        active = [f"{v['role']}(#{v['number']})" for v in state.active_containers.values()]
    if active:
        log.info(f"Active: {', '.join(active)} | "
                 f"Frontend: {state.frontend_dev_count}/{MAX_FRONTEND_AGENTS} | "
                 f"Backend: {state.backend_dev_count}/{MAX_BACKEND_AGENTS}")
    else:
        log.info(f"Idle — watching {GITHUB_REPO}")


# ─── Main ────────────────────────────────────────────────

def main():
    log.info("═══════════════════════════════════════")
    log.info(f"  Agent Team Daemon")
    log.info(f"  Mode: {MODE}")
    log.info(f"  Repo: {GITHUB_REPO}")
    log.info(f"  Frontend devs: {MAX_FRONTEND_AGENTS}")
    log.info(f"  Backend devs: {MAX_BACKEND_AGENTS}")
    log.info(f"  Max fix iterations: {MAX_FIX_ITERATIONS}")
    log.info("═══════════════════════════════════════")

    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)

    # Clean up orphaned containers from previous daemon runs
    log.info("Checking for orphaned containers...")
    try:
        for container in docker_client.containers.list(all=True, filters={"name": f"{PROJECT_NAME}-"}):
            name = container.name
            # Skip daemon and tunnel containers
            if name.endswith("-daemon") or name.endswith("-tunnel"):
                continue
            if container.status == "running":
                log.info(f"Stopping orphaned container: {name}")
                container.stop(timeout=10)
            try:
                container.remove(force=True)
            except Exception:
                pass
        log.info("✓ Cleanup complete")
    except Exception as e:
        log.warning(f"Cleanup failed: {e}")

    log.info("Setting up repo cache...")
    update_repo_cache()
    log.info("✓ Repo cache ready")

    log.info("Setting up agent memory volume...")
    try:
        docker_client.containers.run(
            image="agent-base:latest",
            entrypoint="bash",
            command=["-c",
                "mkdir -p /memory/agents/{architect,frontend-dev,backend-dev,qa,security} "
                "/memory/inbox/{architect,frontend-dev,backend-dev,qa,security} "
                "/memory/issues"
            ],
            volumes={MEMORY_VOLUME: {"bind": "/memory", "mode": "rw"}},
            remove=True,
        )
        log.info("✓ Agent memory ready")
    except Exception as e:
        log.warning(f"Memory setup failed: {e}")

    threading.Thread(target=repo_cache_loop, daemon=True).start()

    # Initial scan — same as a normal poll cycle
    log.info("Initial scan...")
    try:
        poll_github()
        log.info("✓ Scan complete")
    except Exception as e:
        log.warning(f"Initial scan failed: {e}", exc_info=True)

    if MODE == "webhook":
        threading.Thread(target=webhook_retry_loop, daemon=True).start()
        run_webhook_server()
    else:
        run_poller()


if __name__ == "__main__":
    main()
