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
MAX_FULLSTACK_AGENTS = int(os.environ.get("MAX_FULLSTACK_AGENTS", "1"))
# Spend cap (USD) per UTC day. 0 = no cap. Once exceeded, new spawns are
# refused until the next day rollover. In-flight agents are unaffected.
DAILY_BUDGET_USD = float(os.environ.get("DAILY_BUDGET_USD", "0"))
# Circuit breaker for "agent ran successfully but produced no PR" loops.
# After this many such runs in a row for a given issue, the daemon stops
# dispatching and adds a `needs-attention` label for human triage.
NO_PR_RUN_LIMIT = int(os.environ.get("NO_PR_RUN_LIMIT", "2"))
MAX_FIX_ITERATIONS = int(os.environ.get("MAX_FIX_ITERATIONS", "3"))
MAX_TRANSIENT_RETRIES = int(os.environ.get("MAX_TRANSIENT_RETRIES", "5"))
TRANSIENT_BACKOFF_BASE = int(os.environ.get("TRANSIENT_BACKOFF_BASE", "60"))  # seconds
TRANSIENT_BACKOFF_MAX = int(os.environ.get("TRANSIENT_BACKOFF_MAX", "1800"))  # 30 min cap
AUTH_ERROR_BACKOFF_BASE = int(os.environ.get("AUTH_ERROR_BACKOFF_BASE", "300"))  # 5 min — auth blips usually need more time
LOG_DIR = os.environ.get("LOG_DIR", "/logs")
REPO_CACHE_VOLUME = os.environ.get("REPO_CACHE_VOLUME", "agent-team_repo-cache")
PROJECT_NAME = os.environ.get("COMPOSE_PROJECT_NAME", "agent-team")
MEMORY_VOLUME = f"{PROJECT_NAME}_agent-memory"
MAX_TOTAL_AGENTS = int(os.environ.get("MAX_TOTAL_AGENTS", "3"))
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "9876"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
# How often (in minutes) the background loop sweeps `blocked` issues so a
# missed unblock event (daemon down, in-memory state collision, etc.) is
# eventually self-healed without operator intervention. The startup scan
# always runs once regardless of this value. 0 disables the periodic sweep.
BLOCKED_SWEEP_INTERVAL_MIN = int(os.environ.get("BLOCKED_SWEEP_INTERVAL_MIN", "30"))

RESOURCE_PROFILES = {
    "architect":         {"mem_limit": "4g", "cpus": 2.0},
    "architect-merger":  {"mem_limit": "4g", "cpus": 2.0},
    "frontend-dev":      {"mem_limit": "6g", "cpus": 3.0},
    "backend-dev":       {"mem_limit": "6g", "cpus": 3.0},
    "fullstack-dev":     {"mem_limit": "6g", "cpus": 3.0},
    "qa":                {"mem_limit": "6g", "cpus": 3.0},
    "security":          {"mem_limit": "6g", "cpus": 3.0},
}

# Roles that count against MAX_TOTAL_AGENTS and per-role caps. Centralizing
# this tuple means adding a new dev role is a one-line change here plus the
# matching dispatch_<role>_dev / state counter wiring.
DEV_ROLES = ("frontend-dev", "backend-dev", "fullstack-dev")

# ─── Logging ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("daemon")


# ─── Credentials ─────────────────────────────────────────

def _credentials_status():
    """Inspect the Claude OAuth credentials file the daemon is mounting and
    report whether it is valid, expiring soon, or fully expired. Reads the
    file fresh on each call so that token refreshes on the host are picked
    up immediately. Returns a dict suitable for the /health endpoint."""
    if ANTHROPIC_API_KEY:
        return {"mode": "api_key", "expired": False}
    if not CLAUDE_CREDENTIALS_PATH:
        return {"mode": "none", "expired": True, "reason": "no credentials configured"}
    if not os.path.exists(CLAUDE_CREDENTIALS_PATH):
        return {"mode": "oauth", "expired": True, "reason": "credentials file missing"}
    try:
        # Re-open by path each call so that an inode-rotated file is read
        # fresh, not via a stale fd.
        with open(CLAUDE_CREDENTIALS_PATH) as f:
            data = json.load(f)
        oauth = data.get("claudeAiOauth") or {}
        expires_at_ms = oauth.get("expiresAt")
        if not expires_at_ms:
            return {"mode": "oauth", "expired": True, "reason": "expiresAt missing"}
        expires_at = datetime.fromtimestamp(expires_at_ms / 1000, timezone.utc)
        now = datetime.now(timezone.utc)
        remaining_seconds = int((expires_at - now).total_seconds())
        return {
            "mode": "oauth",
            "expired": remaining_seconds <= 0,
            "expires_at": expires_at.isoformat(),
            "remaining_seconds": remaining_seconds,
            # Claude Code refreshes the access token automatically using the
            # refresh token in the same file. The refresh token outlives the
            # access token by weeks. We treat the access token being expired
            # as a soft signal — the real failure is when the refresh token
            # also dies, which surfaces as auth errors at agent runtime.
            "has_refresh_token": bool(oauth.get("refreshToken")),
        }
    except Exception as e:
        return {"mode": "oauth", "expired": True, "reason": f"read error: {e}"}


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
    """Check if an issue already has an open PR.

    Body match requires a closing keyword (`Closes/Fixes/Resolves/Part of #N`)
    — what our dev prompts require in every PR. A bare substring check
    (`f"#{N}" in pr_body`) false-positives on `#1050` for issue `#105` and
    on casual cross-references like "see #105 for context", which silently
    suppresses the dev-in-progress recovery scan below and leaves issues
    stuck under `dev-in-progress` indefinitely.
    """
    body_re = re.compile(
        rf"\b(?:closes|fixes|resolves|part of)\s+#{issue_number}\b",
        re.IGNORECASE,
    )
    branch_re = re.compile(rf"(?:^|/){issue_number}\b")
    try:
        for pr in gh_get_prs("open"):
            pr_body = pr.get("body", "") or ""
            branch = pr.get("head", {}).get("ref", "")
            if body_re.search(pr_body):
                return True
            if branch_re.search(branch):
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
    """Bundle all review feedback for a dev.

    Collects from three sources:
    1. Formal PR reviews with CHANGES_REQUESTED state
    2. Inline PR review comments
    3. Issue comments containing "CHANGES REQUESTED" from QA/Security agents
       (agents share a token so they post as issue comments, not formal reviews)

    Only returns feedback that is NEWER than the last dev fix comment, so devs
    don't re-address already-fixed issues."""
    reviews = gh_get_pr_reviews(pr_number)
    pr_comments = gh_get_review_comments(pr_number)

    feedback = []

    # Collect formal review-level feedback
    for review in reviews:
        if review.get("state") == "CHANGES_REQUESTED":
            feedback.append({
                "type": "review",
                "reviewer": review.get("user", {}).get("login", "unknown"),
                "body": review.get("body", ""),
            })

    # Collect inline comments
    for comment in pr_comments:
        feedback.append({
            "type": "inline_comment",
            "reviewer": comment.get("user", {}).get("login", "unknown"),
            "path": comment.get("path", ""),
            "line": comment.get("line") or comment.get("original_line"),
            "body": comment.get("body", ""),
        })

    # Collect QA/Security agent issue comments with changes requested.
    # Only include comments newer than the last dev fix comment.
    try:
        issue_comments = requests.get(
            f"{API}/repos/{GITHUB_REPO}/issues/{pr_number}/comments",
            headers=HEADERS,
            params={"per_page": 100},
        ).json()
        if isinstance(issue_comments, list):
            # Find the timestamp of the last dev fix comment
            last_fix_time = None
            for c in issue_comments:
                body = c.get("body", "")
                if body.startswith("## Fix iteration") or body.startswith("## Review feedback addressed"):
                    last_fix_time = c.get("created_at")

            for c in issue_comments:
                body = c.get("body", "")
                created = c.get("created_at", "")
                # Skip comments older than the last fix
                if last_fix_time and created <= last_fix_time:
                    continue
                # Only include QA/Security review comments that request changes
                if "CHANGES REQUESTED" in body and ("QA Review" in body or "Security Review" in body):
                    feedback.append({
                        "type": "review_comment",
                        "reviewer": "qa" if "QA Review" in body else "security",
                        "body": body,
                    })
    except Exception as e:
        log.debug(f"Failed to fetch issue comments for PR #{pr_number}: {e}")

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


# ─── Dependency / PR-close link parsing ──────────────────
#
# Two related concerns, both expressed in GitHub Markdown bodies:
#
#   * Issue→issue dependency:  "Depends on #N" / "Blocked by #N" / "After #N"
#     in an issue body declares that #N must close before this issue can
#     proceed. Devs add the `blocked` label when they see one of these and
#     the daemon strips it via _try_unblock_issue when all deps close.
#
#   * PR→issue closure:        "Closes #N" / "Fixes #N" / "Resolves #N" /
#     "Part of #N" / "Implements #N" / "Completes #N" / etc. in a PR body
#     declares that merging the PR resolves issue #N. dispatch_dependents
#     uses this to route unblocked dependents to architect re-triage.
#
# Both forms accept three ref shapes (case-insensitive):
#
#   1. Bare:        #N
#   2. Full URL:    https://github.com/<owner>/<repo>/issues/N
#   3. Owner/repo:  <owner>/<repo>#N
#
# Only same-repo refs count — cross-repo references are silently dropped
# because the daemon has no auth into other repos and dependents are
# intentionally scoped to this repo.
#
# All four call sites (_try_unblock_issue, _unblock_dependents_of,
# dispatch_dependents, _derive_dev_role_for_pr) MUST go through the parser
# functions below — duplicating the regex inline was the original source of
# issue #38 (keywords drifting between sites).

_DEP_KEYWORDS = r'(?:depends on|blocked by|after)'

# PR-close keyword set. GitHub's built-in close-on-merge handles a narrower
# subset (closes/fixes/resolves + plurals), but PR authors in this repo also
# use 'implements', 'completes', 'part of', and 'closed-by'. Broadening
# unblock detection makes the system forgiving of variants while the canonical
# form for new PRs remains "Closes #N" (see dev prompts).
_PR_CLOSE_KEYWORDS = (
    r'(?:'
    r'closes|closed|close|'
    r'fixes|fixed|fix|'
    r'resolves|resolved|resolve|'
    r'part of|implements|completes|closed-by'
    r')'
)

DEP_BARE_RE = re.compile(rf'\b{_DEP_KEYWORDS}\s+#(\d+)\b', re.IGNORECASE)
PR_CLOSE_BARE_RE = re.compile(rf'\b{_PR_CLOSE_KEYWORDS}\s+#(\d+)\b', re.IGNORECASE)

# URL and owner/repo regex variants — built from GITHUB_REPO so they only
# match same-repo refs. Cross-repo refs are intentionally never parsed.
_GH_REPO_ESC = re.escape(GITHUB_REPO)

DEP_URL_RE = re.compile(
    rf'\b{_DEP_KEYWORDS}\s+https?://github\.com/{_GH_REPO_ESC}/issues/(\d+)\b',
    re.IGNORECASE,
)
DEP_OWNER_REPO_RE = re.compile(
    rf'\b{_DEP_KEYWORDS}\s+{_GH_REPO_ESC}#(\d+)\b',
    re.IGNORECASE,
)
PR_CLOSE_URL_RE = re.compile(
    rf'\b{_PR_CLOSE_KEYWORDS}\s+https?://github\.com/{_GH_REPO_ESC}/issues/(\d+)\b',
    re.IGNORECASE,
)
PR_CLOSE_OWNER_REPO_RE = re.compile(
    rf'\b{_PR_CLOSE_KEYWORDS}\s+{_GH_REPO_ESC}#(\d+)\b',
    re.IGNORECASE,
)

# Branch-name fallback for PRs: frontend/N-..., backend/N-..., fullstack/N-...
# infer N as the closed issue when the body yields nothing. The dev
# branch-naming convention is documented in AGENTS.md.
BRANCH_ISSUE_NUM_RE = re.compile(r'^(?:frontend|backend|fullstack)/(\d+)-')


def _parse_dep_refs(body):
    """Parse an issue body for dependency refs (Depends on / Blocked by /
    After). Returns the set of same-repo issue numbers referenced.
    Cross-repo refs are silently dropped."""
    if not body:
        return set()
    nums = set()
    for rx in (DEP_BARE_RE, DEP_URL_RE, DEP_OWNER_REPO_RE):
        nums.update(int(n) for n in rx.findall(body))
    return nums


def _parse_pr_close_refs_from_body(body):
    """Parse a PR body for close-keyword refs (Closes / Fixes / Resolves /
    Part of / Implements / Completes / Closed-by / ...). Returns the set of
    same-repo issue numbers. Cross-repo refs are silently dropped."""
    if not body:
        return set()
    nums = set()
    for rx in (PR_CLOSE_BARE_RE, PR_CLOSE_URL_RE, PR_CLOSE_OWNER_REPO_RE):
        nums.update(int(n) for n in rx.findall(body))
    return nums


def _parse_issue_num_from_branch(branch):
    """Return the issue number embedded in a role-prefixed branch name
    (frontend/12-foo, backend/7-bar, fullstack/3-baz), or None."""
    if not branch:
        return None
    m = BRANCH_ISSUE_NUM_RE.match(branch)
    return int(m.group(1)) if m else None


def _fetch_github_linked_issues(pr_number):
    """Best-effort fallback: ask the GitHub GraphQL API which issues a PR is
    set to close. This catches PRs that were linked via GitHub's UI sidebar
    rather than a body keyword. Returns a set of same-repo issue numbers
    (cross-repo silently dropped), or an empty set on any failure — this
    path must NEVER raise into the unblock pipeline."""
    try:
        owner, repo = GITHUB_REPO.split("/", 1)
    except ValueError:
        return set()
    query = (
        "query($owner: String!, $repo: String!, $number: Int!) {"
        " repository(owner: $owner, name: $repo) {"
        "  pullRequest(number: $number) {"
        "   closingIssuesReferences(first: 50) {"
        "    nodes { number repository { nameWithOwner } }"
        "   }"
        "  }"
        " }"
        "}"
    )
    try:
        resp = requests.post(
            f"{API}/graphql",
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json",
            },
            json={"query": query, "variables": {
                "owner": owner, "repo": repo, "number": pr_number,
            }},
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning(
                f"linked-issues fallback: GraphQL returned "
                f"{resp.status_code} for PR #{pr_number}"
            )
            return set()
        data = resp.json()
        nodes = ((data.get("data") or {})
                 .get("repository", {})
                 .get("pullRequest", {})
                 .get("closingIssuesReferences", {})
                 .get("nodes") or [])
        same_repo = f"{owner}/{repo}".lower()
        nums = set()
        for node in nodes:
            if not isinstance(node, dict):
                continue
            n_repo = (node.get("repository") or {}).get("nameWithOwner", "")
            if n_repo.lower() != same_repo:
                continue
            try:
                nums.add(int(node["number"]))
            except (KeyError, TypeError, ValueError):
                continue
        return nums
    except Exception as e:
        log.warning(
            f"linked-issues fallback: GraphQL call failed "
            f"for PR #{pr_number}: {e}"
        )
        return set()


def _parse_pr_close_refs(pr):
    """Resolve the set of same-repo issue numbers a PR closes.

    Tries, in order:
      1. PR body close-keyword refs (bare #N, URL form, owner/repo#N).
      2. Branch-name fallback (frontend/N-..., backend/N-..., fullstack/N-...).
      3. Best-effort GitHub linked-issues lookup (GraphQL
         `closingIssuesReferences`).

    Returns a set of ints (possibly empty). The branch-name and GraphQL
    fallbacks log explicitly so the source of the inference is auditable."""
    body = pr.get("body", "") or ""
    nums = _parse_pr_close_refs_from_body(body)
    if nums:
        return nums

    branch = (pr.get("head") or {}).get("ref", "") or ""
    branch_num = _parse_issue_num_from_branch(branch)
    if branch_num is not None:
        log.info(
            f"PR #{pr.get('number')}: no body close-link; inferring closed "
            f"issue from branch name '{branch}' → #{branch_num}"
        )
        return {branch_num}

    pr_number = pr.get("number")
    if pr_number is not None:
        linked = _fetch_github_linked_issues(pr_number)
        if linked:
            log.info(
                f"PR #{pr_number}: no body close-link or branch hint; "
                f"GitHub linked-issues sidebar reports {sorted(linked)}"
            )
            return linked

    return set()


# ─── State ───────────────────────────────────────────────

class DaemonState:
    def __init__(self):
        self.processed = set()
        self.active_containers = {}
        self.frontend_dev_count = 0
        self.backend_dev_count = 0
        self.fullstack_dev_count = 0
        self.fix_iterations = {}  # pr_number -> iteration count
        self.retry_counts = {}  # "role-number" -> transient retry count
        self.retry_backoff_until = {}  # "role-number" -> datetime to wait until
        self.dev_queue = []  # list of (role, issue) waiting for a slot
        self.pending_fix_prs = set()  # PR numbers whose fix dispatch was deferred
        self.usage_totals = {  # accumulated across all agent runs since daemon start
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "runs": 0,
        }
        self.usage_by_role = {}  # role -> totals dict (same shape as above)
        # Per-issue count of successful agent runs that produced no PR. Used
        # as a circuit breaker for the case where an agent declines work
        # (e.g., a misroute) over and over; the daemon would otherwise loop
        # because exit 0 looks like progress while the GitHub state is still
        # "needs work." Threshold is NO_PR_RUN_LIMIT.
        self.no_pr_runs = {}  # issue_number -> count
        # Daily cost rollup; resets when current UTC date changes.
        self.daily_cost_usd = 0.0
        self.daily_cost_date = datetime.now(timezone.utc).date()
        # Pause flag — when set, spawn_agent returns None without spawning.
        # Webhooks are still received and parsed; in-flight agents complete.
        self.paused = False
        self.paused_reason = ""
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


def _adjust_dev_count(role, delta):
    """Bump the per-role dev counter by delta. Caller must hold state.lock."""
    if role == "frontend-dev":
        state.frontend_dev_count += delta
    elif role == "backend-dev":
        state.backend_dev_count += delta
    elif role == "fullstack-dev":
        state.fullstack_dev_count += delta


def _dev_role_at_capacity(role):
    """Return True if the per-role cap for `role` is full. Caller must hold lock."""
    if role == "frontend-dev":
        return state.frontend_dev_count >= MAX_FRONTEND_AGENTS
    if role == "backend-dev":
        return state.backend_dev_count >= MAX_BACKEND_AGENTS
    if role == "fullstack-dev":
        return state.fullstack_dev_count >= MAX_FULLSTACK_AGENTS
    return False


# ─── Container Management ────────────────────────────────

def spawn_agent(role, task_context, issue_or_pr_number, extras=None):
    with state.lock:
        # Pause flag — refuse new spawns. In-flight agents continue.
        if state.paused:
            log.info(f"Paused — skipping {role} for #{issue_or_pr_number} ({state.paused_reason or 'no reason set'})")
            return None
        # Daily budget check. Reset the daily counter if the UTC date rolled
        # over since the last record was written.
        if DAILY_BUDGET_USD > 0:
            today = datetime.now(timezone.utc).date()
            if state.daily_cost_date != today:
                state.daily_cost_date = today
                state.daily_cost_usd = 0.0
            if state.daily_cost_usd >= DAILY_BUDGET_USD:
                log.warning(
                    f"Daily budget reached (${state.daily_cost_usd:.2f} / "
                    f"${DAILY_BUDGET_USD:.2f}) — refusing {role} for #{issue_or_pr_number}"
                )
                return None
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
        # Global concurrency limit applies to dev agents only.
        # QA/security/architect/architect-merger are exempt and are also not
        # counted against the cap — otherwise an active reviewer would block
        # dev spawns even when no devs are running.
        if role not in ("qa", "security", "architect", "architect-merger"):
            dev_count = sum(
                1 for v in state.active_containers.values()
                if v.get("role") in DEV_ROLES
            )
            if dev_count >= MAX_TOTAL_AGENTS:
                log.info(f"Max dev agents ({MAX_TOTAL_AGENTS}) reached — deferring {role} for #{issue_or_pr_number}")
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
        # Mount Claude subscription credentials directory (not the file).
        # Claude Code refreshes the credentials file via atomic rename, which
        # changes the inode. A bind mount of the file itself is pinned to the
        # original inode forever, so refreshed tokens never reach agents and
        # the daemon ends up serving expired credentials until restart. Mount
        # the parent directory instead and let the entrypoint copy the file
        # into place at startup — directory mounts always see fresh inodes.
        if CLAUDE_CREDENTIALS_PATH and os.path.exists(CLAUDE_CREDENTIALS_PATH):
            cred_dir = os.path.dirname(CLAUDE_CREDENTIALS_PATH)
            volumes[cred_dir] = {"bind": "/host-claude", "mode": "ro"}

        # Increment dev count before spawning to prevent race conditions
        with state.lock:
            _adjust_dev_count(role, +1)

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
            with state.lock:
                _adjust_dev_count(role, -1)
            raise

        with state.lock:
            info = {
                "role": role, "number": issue_or_pr_number,
                "container": container, "name": container_name,
                "started": datetime.now(timezone.utc),
                "action": task_context.get("action"),
            }
            if extras:
                info.update(extras)
            state.active_containers[container.id] = info

        log.info(f"✓ {container_name} started ({container.short_id})")
        threading.Thread(target=monitor_container, args=(container.id,), daemon=True).start()
        return container

    except Exception as e:
        log.error(f"✗ Failed: {e}")
        return None


def monitor_container(container_id):
    # Cleanup is unified in `finally` so the active_containers dict and the
    # per-role counts can never drift apart. The only path that decrements
    # a count is the `pop` in finally — guarantees exactly-once cleanup
    # regardless of where in the body an exception lands.
    info = state.active_containers.get(container_id)
    if not info:
        return

    try:
        container = info["container"]
        try:
            result = container.wait()
            exit_code = result.get("StatusCode", -1)
        except Exception as e:
            log.error(f"Monitor wait failed for {info['name']}: {e}")
            exit_code = -1

        try:
            logs = container.logs(tail=100).decode("utf-8", errors="replace")
            Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
            (Path(LOG_DIR) / f"{info['name']}.log").write_text(logs)
        except Exception:
            pass

        if exit_code == 0:
            _handle_agent_success(info)
        else:
            _handle_agent_failure(info, container, exit_code)

        try:
            container.remove()
        except Exception:
            pass

    except Exception as e:
        log.error(f"Monitor error for {info.get('name', container_id)}: {e}", exc_info=True)
    finally:
        is_dev = info["role"] in DEV_ROLES
        with state.lock:
            popped = state.active_containers.pop(container_id, None)
            if popped is not None:
                _adjust_dev_count(info["role"], -1)
        if is_dev:
            drain_queue()


def _extract_usage(logs_text):
    """Parse the `USAGE_JSON: {...}` marker line emitted by agent-entrypoint
    and return the decoded dict, or None if the marker is missing/malformed."""
    for line in logs_text.splitlines():
        if line.startswith("USAGE_JSON: "):
            try:
                return json.loads(line[len("USAGE_JSON: "):])
            except Exception:
                return None
    return None


def _record_usage(info, marker):
    """Append a run to /logs/usage.jsonl and update in-memory totals."""
    usage = (marker.get("usage") or {})
    cost = marker.get("total_cost_usd") or 0.0
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "role": info["role"],
        "number": info["number"],
        "action": info.get("action"),
        "input_tokens": usage.get("input_tokens", 0),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cost_usd": cost,
        "duration_ms": marker.get("duration_ms"),
        "num_turns": marker.get("num_turns"),
    }
    try:
        Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
        with (Path(LOG_DIR) / "usage.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        log.warning(f"usage.jsonl write failed: {e}")

    with state.lock:
        for key in ("input_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens", "output_tokens"):
            state.usage_totals[key] += record[key]
        state.usage_totals["cost_usd"] += cost
        state.usage_totals["runs"] += 1
        per_role = state.usage_by_role.setdefault(info["role"], {
            "input_tokens": 0, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0, "output_tokens": 0,
            "cost_usd": 0.0, "runs": 0,
        })
        for key in ("input_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens", "output_tokens"):
            per_role[key] += record[key]
        per_role["cost_usd"] += cost
        per_role["runs"] += 1

        # Daily rollup. If the UTC date rolled over since the last record,
        # reset to today's accumulation rather than carrying yesterday's total.
        today = datetime.now(timezone.utc).date()
        if state.daily_cost_date != today:
            state.daily_cost_date = today
            state.daily_cost_usd = 0.0
        state.daily_cost_usd += cost


def _handle_agent_success(info):
    log.info(f"✓ {info['name']} completed")
    retry_key = f"{info['role']}-{info['number']}"
    with state.lock:
        state.retry_counts.pop(retry_key, None)
        state.retry_backoff_until.pop(retry_key, None)

    # Architect post-run cleanup. Two failure modes the prompt-side bash
    # can't guard against:
    #   1. The architect container crashes / OOMs before its trailing
    #      `gh issue edit --remove-label architect-in-progress` step runs,
    #      leaving the issue label-stuck under `architect-in-progress`.
    #   2. The `architect-retriage-{N}` handled key is never cleared after
    #      a successful re-triage run, so a *second* unblock event for the
    #      same issue within one daemon lifetime is silently dropped.
    # Both are idempotent: removing a label that isn't present is a no-op,
    # and clearing an unset handled key is a no-op. Run them unconditionally
    # for any architect-role completion regardless of `action` so a crash
    # before action-specific branches still cleans up.
    if info["role"] == "architect":
        gh_remove_label(info["number"], "architect-in-progress")
        state.clear_handled(f"architect-retriage-{info['number']}")

    # Pull token usage from the entrypoint's USAGE_JSON marker line.
    try:
        logs_text = info["container"].logs(tail=20).decode("utf-8", errors="replace")
        marker = _extract_usage(logs_text)
        if marker:
            _record_usage(info, marker)
    except Exception as e:
        log.warning(f"usage extract failed for {info['name']}: {e}")

    # Circuit breaker: if a dev agent exits 0 without producing a PR AND
    # without self-correcting the issue's labels, count it. After
    # NO_PR_RUN_LIMIT such runs, escalate to `needs-attention` for human
    # triage. The self-correction check exists so that an agent that
    # correctly re-routed the issue (added a different dev role, `blocked`,
    # or `needs-attention`) doesn't get overridden by the breaker — that
    # would undo the very fix we want the agent to make.
    if info["role"] in DEV_ROLES and info.get("action") == "implement_issue":
        number = info["number"]
        try:
            has_pr = gh_issue_has_open_pr(number)
        except Exception:
            has_pr = False

        # Re-fetch issue labels AFTER the agent ran so we see any self-
        # correction it applied (relabel to another dev role, blocked, etc.)
        agent_acted_on_labels = False
        try:
            r = requests.get(
                f"{API}/repos/{GITHUB_REPO}/issues/{number}",
                headers=HEADERS, timeout=5,
            )
            if r.status_code == 200:
                current = {l["name"] for l in (r.json().get("labels") or [])}
                # If the issue carries a *different* dev role (re-route) or a
                # human-triage label, the agent took meaningful action.
                other_dev_roles = {r2 for r2 in DEV_ROLES if r2 != info["role"]}
                agent_acted_on_labels = bool(
                    (current & other_dev_roles)
                    or "blocked" in current
                    or "needs-attention" in current
                )
        except Exception as e:
            log.debug(f"label re-fetch for #{number}: {e}")

        if has_pr or agent_acted_on_labels:
            with state.lock:
                state.no_pr_runs.pop(number, None)
        else:
            with state.lock:
                count = state.no_pr_runs.get(number, 0) + 1
                state.no_pr_runs[number] = count
            if count >= NO_PR_RUN_LIMIT:
                log.warning(
                    f"#{number} hit no-PR run limit ({count} successful runs, no PR) "
                    f"— escalating to needs-attention"
                )
                # Strip workflow labels so neither the role-label scan nor the
                # dev-in-progress recovery scan can re-pick it up. Add
                # needs-attention so a human notices.
                for label in DEV_ROLES + ("dev-in-progress",):
                    gh_remove_label(number, label)
                gh_add_label(number, "needs-attention")
                gh_comment(number,
                           f"⚠️ Agent ran successfully {count} times without producing a PR "
                           f"or re-routing the issue. Stopping the dispatch loop. See prior "
                           f"agent comments for the reason — usually a missing self-correction. "
                           f"Re-label manually once resolved.")
            else:
                # Below the breaker threshold: restore the role label, drop
                # `dev-in-progress`, and clear handled state so the next poll
                # / webhook can re-dispatch. Without this, the issue sits
                # with `dev-in-progress` and no active agent — the
                # `dev_in_progress_stale_no_pr` pattern the health check has
                # been logging. Mirrors the non-transient failure path.
                gh_remove_label(number, "dev-in-progress")
                gh_add_label(number, info["role"])
                state.clear_handled(f"{info['role']}-{number}")

    # Architect-merger post-run handling. Three outcomes possible:
    #   1. PR merged — happy path, nothing to do.
    #   2. PR head SHA changed (merger pushed a commit, e.g. a conflict
    #      resolution): treat as "awaiting re-review." The push triggers a
    #      synchronize webhook that re-dispatches QA → Security on the
    #      post-merge tree. Don't flip needs-fixes — the dev did nothing
    #      wrong; the merger just brought the branch in line with main.
    #   3. PR head unchanged AND not merged: merger declined for arch
    #      reasons. Flip to needs-fixes (existing behavior).
    if info["role"] == "architect-merger":
        pr_number = info["number"]
        pre_head = info.get("pre_run_head_sha")
        try:
            r = requests.get(
                f"{API}/repos/{GITHUB_REPO}/pulls/{pr_number}",
                headers=HEADERS,
            )
            if r.status_code == 200:
                pr_data = r.json()
                if not pr_data.get("merged"):
                    current_head = (pr_data.get("head") or {}).get("sha")
                    head_changed = bool(pre_head and current_head and pre_head != current_head)
                    if head_changed:
                        log.info(
                            f"Architect-merger pushed new commit on PR #{pr_number} "
                            f"({pre_head[:7]}→{current_head[:7]}) — awaiting re-review on the post-merge tree"
                        )
                        # Clear merge handled so the merger can re-fire after
                        # the synchronize-driven QA → Security cycle approves.
                        state.clear_handled(f"merge-{pr_number}")
                    else:
                        log.warning(f"Architect declined to merge PR #{pr_number} — flipping to needs-fixes")
                        # Post a synthesizing CHANGES REQUESTED comment so
                        # _latest_reviews_approved() returns False on the next
                        # dispatch — otherwise the daemon would skip the fix
                        # because QA/Security previously approved.
                        gh_comment(pr_number,
                                   "## QA Review — CHANGES REQUESTED\n\n"
                                   "Re-opening review after the architect declined the merge. "
                                   "See the architect's most recent comment on this PR for the "
                                   "specific issues that need to be addressed before this can land.")
                        gh_add_label(pr_number, "needs-fixes")
                        state.clear_handled(f"merge-{pr_number}")
        except Exception as e:
            log.error(f"Architect-decline check for PR #{pr_number}: {e}")


def _classify_failure(logs_text):
    """Return 'auth', 'rate_limit', or None (= not a transient failure).

    Prefers the Anthropic SDK's "Error code: NNN" pattern, which is the
    most reliable signal. Falls back to keyword matching for errors that
    don't surface that line (CLI-level wrapping, network errors, etc.).
    Rate-limit signals take precedence over auth signals — a 429 response
    from an OAuth endpoint can carry auth-ish words in the body without
    being an auth issue."""
    sdk_match = re.search(r"error code:\s*(\d{3})", logs_text)
    if sdk_match:
        code = sdk_match.group(1)
        if code == "401":
            return "auth"
        if code == "429":
            return "rate_limit"
        if code in ("500", "502", "503", "504", "529"):
            return "rate_limit"  # upstream 5xx — same backoff treatment

    # No word boundaries on these — they appear as substrings of error type
    # names like "overloaded_error" / "rate_limit_error" where \b would miss.
    # Each token is specific enough to not false-positive on unrelated logs.
    # Timeouts share rate-limit treatment because both are upstream-transient
    # and benefit from the same exponential backoff — they're not real
    # failures of the agent's reasoning.
    if re.search(r"(rate.?limit|usage.?limit|too many requests|overloaded|apitimeouterror|api_timeout|read timed out|connection timed out|request timed out)", logs_text):
        return "rate_limit"
    if re.search(r"(authentication_error|invalid_api_key|oauth.?token.?expired|invalid authentication|unauthorized)", logs_text):
        return "auth"
    return None


def _handle_agent_failure(info, container, exit_code):
    log.warning(f"✗ {info['name']} exited ({exit_code})")

    failure_kind = None
    try:
        logs_text = container.logs(tail=50).decode("utf-8", errors="replace").lower()
        failure_kind = _classify_failure(logs_text)
    except Exception:
        pass

    is_auth_error = failure_kind == "auth"
    is_transient = failure_kind is not None
    is_fix_cycle = info.get("action") == "fix_review_feedback"

    # A fix-cycle dispatch that failed before doing real work shouldn't burn
    # an iteration. dispatch_needs_fixes pre-incremented state.fix_iterations
    # at dispatch time; roll it back here regardless of whether the failure
    # was transient. Comment-counting (the other counter source) only counts
    # actual "address review feedback" comments, so rolling back the in-mem
    # counter is the only thing that needs reverting.
    if is_fix_cycle:
        with state.lock:
            cur = state.fix_iterations.get(info["number"], 0)
            if cur > 0:
                state.fix_iterations[info["number"]] = cur - 1

    if not is_transient:
        # Non-transient fix-cycle failure: re-add needs-fixes so the PR
        # doesn't get stranded label-less. Polling / next webhook can retry.
        if is_fix_cycle:
            gh_add_label(info["number"], "needs-fixes")
        # Non-transient architect-merger failure: clear merge-{n} handled
        # state so the next approval event or poll cycle can re-dispatch.
        # Without this, an unclassified failure (e.g. claude --print
        # exiting silently on stale credentials) strands the PR forever
        # because `already_handled('merge-N')` keeps returning True.
        if info["role"] == "architect-merger":
            state.clear_handled(f"merge-{info['number']}")
        # Same shape for architect (planner): clear architect-{n} so a
        # later poll / re-label can retry. Also clear the re-triage handled
        # key — otherwise a subsequent unblock event for this same issue
        # (within one daemon lifetime) is silently dropped because
        # `state.already_handled('architect-retriage-N')` keeps returning
        # True. The clear is idempotent if the failed run wasn't a re-triage.
        elif info["role"] == "architect":
            state.clear_handled(f"architect-{info['number']}")
            state.clear_handled(f"architect-retriage-{info['number']}")
        # Same shape for QA and security: if an unclassified failure
        # (e.g. Anthropic API timeout that didn't match _classify_failure)
        # leaves the PR with an "errored" comment but no real review,
        # clear the handled flag so reconcile_pr / next synchronize webhook
        # can re-dispatch. _classify_pr_reviews already filters the error
        # placeholder comment, so the next attempt won't see it as a verdict.
        elif info["role"] in ("qa", "security"):
            state.clear_handled(f"{info['role']}-{info['number']}")
        # Dev `implement_issue` failure: restore the role label (so the next
        # poll re-dispatches), drop `dev-in-progress` (so the issue isn't
        # stranded looking active), and clear handled state (so _dispatch_dev
        # doesn't short-circuit). Without this the issue sits with
        # `dev-in-progress` forever — the `dev_in_progress_stale_no_pr`
        # pattern the hourly health check has been logging. The transient
        # retry path below already does this exact dance; the non-transient
        # path was the missed case.
        elif info["role"] in DEV_ROLES and not is_fix_cycle:
            gh_remove_label(info["number"], "dev-in-progress")
            gh_add_label(info["number"], info["role"])
            state.clear_handled(f"{info['role']}-{info['number']}")
        gh_comment(info["number"],
                   f"⚠️ Agent `{info['role']}` errored (exit {exit_code}). Check daemon logs.")
        return

    retry_key = f"{info['role']}-{info['number']}"
    with state.lock:
        retries = state.retry_counts.get(retry_key, 0) + 1
        state.retry_counts[retry_key] = retries

    # Auth errors are Anthropic-side transient issues (brief token revocations,
    # upstream 401s). Retry indefinitely with a longer initial backoff — capped
    # at TRANSIENT_BACKOFF_MAX. Non-auth transients (real rate limits) still
    # give up after MAX_TRANSIENT_RETRIES.
    if is_auth_error:
        backoff = min(AUTH_ERROR_BACKOFF_BASE * (2 ** (retries - 1)), TRANSIENT_BACKOFF_MAX)
        give_up = False
    else:
        backoff = min(TRANSIENT_BACKOFF_BASE * (2 ** (retries - 1)), TRANSIENT_BACKOFF_MAX)
        give_up = retries > MAX_TRANSIENT_RETRIES

    backoff_until = datetime.now(timezone.utc) + timedelta(seconds=backoff)
    with state.lock:
        state.retry_backoff_until[retry_key] = backoff_until

    if give_up:
        # Still set backoff so recovery poll doesn't tight-loop.
        log.warning(f"✗ {info['name']} exceeded max transient retries ({MAX_TRANSIENT_RETRIES}) — cooldown {backoff}s")
        gh_comment(info["number"],
                   f"⚠️ Agent `{info['role']}` failed after {MAX_TRANSIENT_RETRIES} retries "
                   f"(rate limit / transient error). Requires manual intervention. "
                   f"Daemon will cool down for {backoff//60}m before trying again.")
        return

    state.clear_handled(f"{info['role']}-{info['number']}")
    if is_fix_cycle:
        # Re-add needs-fixes so the recovery poll / next webhook re-dispatches
        # the dev once backoff expires. Without this the PR ends up label-less
        # and orphaned, since dispatch_needs_fixes removed the label at dispatch.
        gh_add_label(info["number"], "needs-fixes")
    elif info["role"] in DEV_ROLES:
        gh_remove_label(info["number"], "dev-in-progress")
        gh_add_label(info["number"], info["role"])
    elif info["role"] == "architect":
        # Clear the re-triage handled key regardless of action so the
        # recovery sweep can re-dispatch as `re_triage_unblocked` on the
        # next poll. Keeping it set would mask the retry behind the
        # `already_handled` check until the daemon restarts. This is
        # idempotent if the failed run was a `plan_feature`.
        state.clear_handled(f"architect-retriage-{info['number']}")
        if info.get("action") == "re_triage_unblocked":
            # Leave `architect-in-progress` set: the stuck-architect
            # recovery sweep (which is now re-triage-aware) will detect the
            # unblock candidate and re-dispatch via
            # `dispatch_architect_retriage`. If we re-labeled to `architect`
            # here, the next poll would fire `plan_feature` and lose the
            # re-triage context (including `closed_dependencies`).
            pass
        else:
            gh_remove_label(info["number"], "architect-in-progress")
            gh_add_label(info["number"], "architect")

    kind = "auth" if is_auth_error else "transient"
    if retries == 1:
        gh_comment(info["number"],
                   f"⏳ Agent `{info['role']}` hit a {kind} error. Will retry in {backoff}s.")
    log.info(f"↻ {info['name']} {kind} error — retry {retries} in {backoff}s")


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


def dispatch_architect_retriage(issue, closed_deps):
    """Re-triage an issue whose blocking deps just resolved. The architect
    decides whether the issue is still relevant against current `main` and
    either closes it as stale, edits the body, or re-labels with a dev role."""
    number = issue["number"]
    key = f"architect-retriage-{number}"
    if state.already_handled(key):
        return

    log.info(f"Architect re-triage: #{number} — {issue['title']} (deps closed: {closed_deps})")

    result = spawn_agent("architect", {
        "action": "re_triage_unblocked",
        "issue_number": number,
        "issue_title": issue["title"],
        "issue_body": issue.get("body", ""),
        "issue_url": issue.get("html_url", ""),
        "closed_dependencies": closed_deps,
    }, number)
    if result is None:
        state.clear_handled(key)
        return
    gh_add_label(number, "architect-in-progress")


def _dispatch_dev(role, issue):
    """Generic dev dispatcher used by frontend / backend / fullstack roles.
    Queues if at per-role capacity; otherwise removes the role label, adds
    dev-in-progress, and spawns the agent."""
    number = issue["number"]
    key = f"{role}-{number}"

    # Manual triage labels — daemon stays out of the way.
    issue_labels = {l["name"] for l in issue.get("labels", [])}
    if "blocked" in issue_labels or "needs-attention" in issue_labels:
        return

    with state.lock:
        already_queued = any(r == role and i["number"] == number for r, i in state.dev_queue)
        if already_queued:
            return

    if state.already_handled(key):
        return

    with state.lock:
        if _dev_role_at_capacity(role):
            already_queued = any(r == role and i["number"] == number for r, i in state.dev_queue)
            if not already_queued:
                state.dev_queue.append((role, issue))
                log.info(f"Queued: #{number} — {issue['title']} ({role}, {len(state.dev_queue)} in queue)")
            state.processed.discard(key)
            return

    log.info(f"{role} issue: #{number} — {issue['title']}")

    # Spawn FIRST, swap labels only after the container actually starts.
    # spawn_agent has several legitimate early-return paths (paused, daily
    # budget hit, duplicate agent already running for this issue, active
    # retry backoff, MAX_TOTAL_AGENTS race with a concurrent spawn, docker
    # run exception). If any fire after we've already swapped role label →
    # `dev-in-progress`, the issue is stranded with `dev-in-progress` and
    # no active agent — the `dev_in_progress_stale_no_pr` pattern the
    # hourly health check has been logging. Mirrors `dispatch_architect`.
    result = spawn_agent(role, {
        "action": "implement_issue",
        "issue_number": number,
        "issue_title": issue["title"],
        "issue_body": issue.get("body", ""),
        "issue_url": issue.get("html_url", ""),
    }, number)
    if result is None:
        state.clear_handled(key)
        return
    gh_remove_label(number, role)
    gh_add_label(number, "dev-in-progress")


def dispatch_frontend_dev(issue):
    _dispatch_dev("frontend-dev", issue)


def dispatch_backend_dev(issue):
    _dispatch_dev("backend-dev", issue)


def dispatch_fullstack_dev(issue):
    _dispatch_dev("fullstack-dev", issue)


def drain_queue():
    """Try to dispatch pending PR fixes and queued issues now that a slot may be free.

    PRIORITY: pending fixes always run before new issues. Finishing existing
    PRs is more valuable than starting new ones — they unblock the architect
    from merging downstream work, they have already absorbed review costs,
    and old branches accumulate merge conflicts the longer they sit. Starting
    new issues while fixes pile up creates compounding work-in-progress."""
    with state.lock:
        queue_copy = list(state.dev_queue)
        pending_copy = list(state.pending_fix_prs)

    # Pending fixes first. dispatch_needs_fixes is idempotent — if devs are
    # still saturated when we get here, it'll just re-park the PR.
    for pr_number in pending_copy:
        try:
            r = requests.get(f"{API}/repos/{GITHUB_REPO}/pulls/{pr_number}", headers=HEADERS)
            if r.status_code == 200:
                dispatch_needs_fixes(r.json())
        except Exception as e:
            log.debug(f"drain pending fix #{pr_number}: {e}")

    # Then new issues from the queue. If a pending fix grabbed the slot,
    # these will all defer back into the queue, which is what we want.
    # Remove from queue BEFORE dispatching — _dispatch_dev has an
    # already_queued early-return that would block it otherwise.
    for role, issue in queue_copy:
        with state.lock:
            state.dev_queue = [(r, i) for r, i in state.dev_queue if not (r == role and i["number"] == issue["number"])]
            state.processed.discard(f"{role}-{issue['number']}")
        if role in DEV_ROLES:
            _dispatch_dev(role, issue)


def _is_unblock_candidate(issue):
    """Return ``(True, sorted_closed_deps)`` if ``issue`` declares at least
    one ``Depends on #N`` / ``Blocked by #N`` / ``After #N`` reference AND
    every referenced #N is closed. Otherwise ``(False, [])``.

    Shared by `_try_unblock_issue`, `sweep_blocked_issues`, and the
    stuck-architect recovery sweep so we never disagree on what "unblock
    candidate" means. Network failures fetching deps are treated as
    "not a candidate" to stay conservative — better to skip one cycle than
    spuriously unblock on a flaky API call.

    Dep parsing goes through the module-level `_parse_dep_refs` helper per
    the AGENTS.md "Dependency parsing — regex contract" — all four call
    sites must share that helper (centralized in #47) so all ref shapes
    (bare `#N`, full URL, `owner/repo#N`; same-repo only) are recognized
    uniformly. Do not inline a regex here."""
    body = issue.get("body", "") or ""
    depends_on = _parse_dep_refs(body)
    if not depends_on:
        return False, []

    for dep in depends_on:
        try:
            dep_issue = gh_get(f"/repos/{GITHUB_REPO}/issues/{dep}")
            if not isinstance(dep_issue, dict) or dep_issue.get("state") != "closed":
                return False, []
        except Exception:
            return False, []

    return True, sorted(depends_on)


def _try_unblock_issue(issue):
    """If `issue` declares deps and they're all closed, strip the `blocked`
    label and dispatch architect re-triage. Returns True if unblocked.

    Safe to call multiple times: if `blocked` has already been stripped AND
    `architect-in-progress` is already set, this is a no-op (a prior call
    already kicked off the architect). Otherwise the dispatch is itself
    guarded by `state.already_handled('architect-retriage-N')`."""
    labels = [l["name"] for l in issue.get("labels", [])]
    issue_number = issue["number"]

    is_candidate, closed_deps = _is_unblock_candidate(issue)
    if not is_candidate:
        return False

    # Re-entry guard: if a prior call already stripped `blocked` and the
    # architect is currently working the issue, don't post a duplicate
    # comment or re-dispatch. `_handle_agent_success` clears the handled
    # key on completion, so a *next* unblock event after the architect
    # finishes will still fire.
    if "blocked" not in labels and "architect-in-progress" in labels:
        return False

    if "blocked" in labels:
        gh_remove_label(issue_number, "blocked")
        gh_comment(issue_number, "Unblocked — all referenced dependencies are closed. Routing to architect for staleness check.")
        refreshed = gh_get(f"/repos/{GITHUB_REPO}/issues/{issue_number}")
        if isinstance(refreshed, dict):
            issue = refreshed

    log.info(f"Unblocked: #{issue_number} — dispatching architect re-triage")
    dispatch_architect_retriage(issue, closed_deps)
    return True


def _unblock_dependents_of(closed_issue_numbers):
    """Scan open issues for ones that depend on any of `closed_issue_numbers`.
    If all of their declared deps are now closed, route them to architect re-triage."""
    if not closed_issue_numbers:
        return
    try:
        resp = gh_get(f"/repos/{GITHUB_REPO}/issues", params={"state": "open", "per_page": 100})
        if not isinstance(resp, list):
            return

        for issue in resp:
            if issue.get("pull_request"):
                continue  # skip PRs
            body = issue.get("body", "") or ""

            depends_on = _parse_dep_refs(body)
            if not depends_on.intersection(closed_issue_numbers):
                continue

            _try_unblock_issue(issue)

    except Exception as e:
        log.error(f"Error checking dependents: {e}")


def sweep_blocked_issues():
    """Scan all open issues with the `blocked` label and try to unblock each.

    Three outcomes per issue:
      - Deps declared AND all closed → strip `blocked` and dispatch architect
        re-triage. Counts as ``unblocked``.
      - No deps declared in the body (operator edited the body but the label
        stuck) → strip `blocked` and post an explanatory comment, but DON'T
        auto-dispatch — the next webhook / poll re-routes by label normally.
        Counts as ``label_cleared``.
      - Deps declared but at least one still open → leave the issue alone.
        Counts as ``skipped``.

    Runs on daemon start, on a configurable periodic cadence
    (``BLOCKED_SWEEP_INTERVAL_MIN``), and on demand via ``POST /sweep-blocked``."""
    log.info("Sweeping `blocked` issues for retroactive unblock")
    unblocked = 0
    label_cleared = 0
    skipped = 0
    try:
        resp = gh_get(f"/repos/{GITHUB_REPO}/issues",
                      params={"state": "open", "labels": "blocked", "per_page": 100})
        if not isinstance(resp, list):
            log.error(f"sweep: unexpected response listing blocked issues: {resp}")
            return {"unblocked": 0, "label_cleared": 0, "skipped": 0, "error": "list failed"}

        for issue in resp:
            if issue.get("pull_request"):
                continue
            number = issue["number"]
            body = issue.get("body", "") or ""

            # Case 1: deps declared. Use the shared candidate check.
            # Goes through `_parse_dep_refs` per the AGENTS.md "Dependency
            # parsing — regex contract" so this new call site recognizes
            # the same ref shapes as the rest of the daemon (bare `#N`,
            # full URL, `owner/repo#N`; same-repo only).
            depends_on = _parse_dep_refs(body)
            if depends_on:
                if _try_unblock_issue(issue):
                    unblocked += 1
                else:
                    skipped += 1
                continue

            # Case 2: body declares no deps but the label is stuck. This
            # happens when an operator (or the architect during re-triage)
            # edits the body to remove `Depends on #N` but doesn't strip
            # `blocked` — `_try_unblock_issue` returns False silently in
            # that case, stranding the issue. Clear the label and post a
            # human-readable note. Don't dispatch — let the next webhook /
            # poll re-route by whatever role label the issue carries.
            log.info(f"sweep: #{number} — `blocked` set but body declares no deps; clearing label")
            gh_remove_label(number, "blocked")
            gh_comment(number,
                       "Body no longer declares dependencies; clearing `blocked`. "
                       "If this was wrong, add the label back.")
            label_cleared += 1

    except Exception as e:
        log.error(f"sweep error: {e}")
        return {"unblocked": unblocked, "label_cleared": label_cleared,
                "skipped": skipped, "error": str(e)}

    log.info(f"Sweep complete: {unblocked} unblocked, {label_cleared} label-cleared, {skipped} skipped")
    return {"unblocked": unblocked, "label_cleared": label_cleared, "skipped": skipped}


def dispatch_dependents(pr):
    """When a PR is merged, find issues that depended on it and trigger them.

    Detection sources, in priority order (see _parse_pr_close_refs):
      1. PR body close-keyword refs — bare `#N`, full URL, `owner/repo#N`.
      2. Branch-name fallback — `<role>/<N>-...`.
      3. Best-effort GraphQL `closingIssuesReferences` lookup.

    A miss here means dependents stay `blocked` forever, so the layered
    fallback is intentional (see #38 / #45)."""
    pr_number = pr["number"]
    closed_issues = _parse_pr_close_refs(pr)
    if not closed_issues:
        log.info(
            f"PR #{pr_number} merged but no closed-issue link detected "
            f"(body, branch, or GitHub sidebar). Skipping dependent unblock."
        )
        return

    log.info(f"PR #{pr_number} merged, closed issues: {sorted(closed_issues)}")
    _unblock_dependents_of(closed_issues)


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


_BRANCH_PREFIX_ROLE = {
    "frontend/": "frontend-dev",
    "backend/": "backend-dev",
    "fullstack/": "fullstack-dev",
}


def _derive_dev_role_for_pr(pr):
    """Determine which dev role owns a PR.

    Primary signal is the branch prefix (frontend/, backend/, fullstack/).
    When the branch was named off-convention (e.g. feat/foo), fall back to
    the PR's own dev-role labels, then to the linked issue's labels parsed
    from the PR body ("Closes #N" / "Fixes #N" / "Resolves #N" / "Part of #N").
    Returns the role string or None if nothing matched.
    """
    branch = pr.get("head", {}).get("ref", "") or ""
    for prefix, role in _BRANCH_PREFIX_ROLE.items():
        if branch.startswith(prefix):
            return role

    pr_labels = {l["name"] for l in pr.get("labels", []) if isinstance(l, dict)}
    for role in DEV_ROLES:
        if role in pr_labels:
            return role

    body = pr.get("body", "") or ""
    # Body-only parser here (not _parse_pr_close_refs): the branch / GraphQL
    # fallbacks live one level up and we don't want a re-spawn path triggering
    # extra GraphQL calls during webhook handling.
    linked = _parse_pr_close_refs_from_body(body)
    for issue_number in linked:
        try:
            issue = gh_get(f"/repos/{GITHUB_REPO}/issues/{issue_number}")
        except Exception:
            continue
        issue_labels = {l["name"] for l in issue.get("labels", []) if isinstance(l, dict)}
        for role in DEV_ROLES:
            if role in issue_labels:
                return role
    return None


def dispatch_needs_fixes(pr):
    """Re-spawn the right dev agent with review feedback context."""
    number = pr["number"]
    branch = pr.get("head", {}).get("ref", "")

    dev_role = _derive_dev_role_for_pr(pr)
    if dev_role is None:
        key = f"unknown-role-{number}"
        if not state.already_handled(key):
            log.warning(
                f"PR #{number} branch '{branch}' has no role prefix and no role label "
                f"on PR or linked issue — flagging for human triage"
            )
            gh_comment(number,
                       "⚠️ Cannot determine which dev role owns this PR. "
                       "Branch is not prefixed with `frontend/`, `backend/`, or `fullstack/`, "
                       "and no dev-role label was found on the PR or linked issue. "
                       "Add a `frontend-dev`, `backend-dev`, or `fullstack-dev` label "
                       "to this PR (or rename the branch) so the fix loop can resume.")
            gh_remove_label(number, "needs-fixes")
            gh_add_label(number, "needs-attention")
        return

    # If the latest reviews are both approvals, the fix is already applied — skip
    if _latest_reviews_approved(number):
        log.info(f"PR #{number} — latest reviews are approvals, skipping fix dispatch")
        gh_remove_label(number, "needs-fixes")
        with state.lock:
            state.pending_fix_prs.discard(number)
        return

    # Pre-check capacity. If devs are saturated, park the PR in pending_fix_prs
    # and bail out WITHOUT incrementing iteration or removing the needs-fixes
    # label — drain_queue will retry once a dev slot frees up.
    with state.lock:
        dev_count = sum(
            1 for v in state.active_containers.values()
            if v.get("role") in DEV_ROLES
        )
        if dev_count >= MAX_TOTAL_AGENTS:
            if number not in state.pending_fix_prs:
                state.pending_fix_prs.add(number)
                log.info(f"PR #{number} fix deferred — devs saturated ({dev_count}/{MAX_TOTAL_AGENTS}), parked in pending")
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

    # Fetch review feedback to pass to the dev
    feedback = gh_get_review_feedback(number)

    # If there's no new feedback since the last fix, don't waste an iteration.
    # The dev already addressed everything — either reviewers haven't re-run yet,
    # or the fix was accepted. Remove the label and let the next synchronize
    # event trigger fresh reviews.
    if not feedback:
        log.info(f"PR #{number} — no new review feedback since last fix, skipping iteration {iterations}")
        gh_remove_label(number, "needs-fixes")
        with state.lock:
            state.fix_iterations[number] = iterations - 1  # don't count this
            state.pending_fix_prs.discard(number)
        return

    log.info(f"Needs fixes: PR #{number} (iteration {iterations}) — spawning {dev_role}")

    # Remove needs-fixes label so it can be re-applied after next review
    gh_remove_label(number, "needs-fixes")

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
    with state.lock:
        state.pending_fix_prs.discard(number)


def dispatch_architect_merge(pr):
    """Spawn architect-merger (separate role from the planner) to merge an
    approved PR. Splitting planning and merging keeps each prompt tight to
    its job and limits the blast radius of a misbehaving planner."""
    number = pr["number"]
    key = f"merge-{number}"
    if state.already_handled(key):
        return

    # Capture the PR's head SHA at spawn time so _handle_agent_success can
    # detect whether the merger pushed a new commit (e.g., a conflict
    # resolution) — that case shouldn't be treated as "merger declined."
    pre_run_head_sha = pr.get("head", {}).get("sha")
    log.info(f"Both approved: PR #{number} — spawning architect-merger")
    result = spawn_agent("architect-merger", {
        "action": "merge_approved_pr",
        "pr_number": number,
        "pr_title": pr["title"],
        "pr_body": pr.get("body", ""),
        "pr_url": pr.get("html_url", ""),
        "pr_branch": pr.get("head", {}).get("ref", ""),
    }, number, extras={"pre_run_head_sha": pre_run_head_sha})
    if result is None:
        state.clear_handled(key)


def _classify_pr_reviews(comments):
    """Walk comments newest-first and return (qa_state, sec_state) where each
    is 'approved', 'changes', or None.

    Stale-verdict invalidation: a `changes` verdict is treated as None if the
    OTHER reviewer issued an `approved` verdict more recently. The reasoning:
    if QA approved at T2 on the latest code, a Security `changes` from T1 was
    written against older code — the dev has since pushed a fix that QA has
    re-reviewed and signed off on, so Security needs to re-run, not the dev.
    Without this, a stale changes-requested verdict pins the PR in needs-fixes
    forever despite the dev having already addressed everything."""
    # First pass: find the latest verdict + timestamp for each reviewer.
    # Match both "QA Review" and "QA Re-review" (some agents stylize the
    # follow-up review with a hyphen). The regexes below allow optional
    # "Re-" / "Re " prefixes so a stylized header still classifies.
    qa_state = qa_ts = None
    sec_state = sec_ts = None
    qa_pat = re.compile(r"\bQA\s+(?:RE-?\s*)?REVIEW\b")
    sec_pat = re.compile(r"\bSECURITY\s+(?:RE-?\s*)?REVIEW\b")
    for c in reversed(comments):
        body = c.get("body", "") or ""
        if body.startswith("⚠️ Agent ") or body.startswith("⏳ Agent ") or "Daemon-verified:" in body:
            continue
        header = body.split("\n")[0]
        upper = header.upper()
        ts = c.get("created_at", "")
        if qa_state is None and qa_pat.search(upper):
            if "CHANGES REQUESTED" in upper:
                qa_state, qa_ts = "changes", ts
            elif "APPROVED" in upper or "✅" in header:
                qa_state, qa_ts = "approved", ts
        if sec_state is None and sec_pat.search(upper):
            if "CHANGES REQUESTED" in upper:
                sec_state, sec_ts = "changes", ts
            elif "APPROVED" in upper or "✅" in header:
                sec_state, sec_ts = "approved", ts
        if qa_state is not None and sec_state is not None:
            break

    # Stale invalidation: a changes-requested verdict against older code is
    # superseded by a more recent approval from the other reviewer.
    if qa_state == "changes" and sec_state == "approved" and qa_ts and sec_ts and sec_ts > qa_ts:
        qa_state = None
    if sec_state == "changes" and qa_state == "approved" and qa_ts and sec_ts and qa_ts > sec_ts:
        sec_state = None

    return qa_state, sec_state


def _fetch_pr_comments_last_page(pr_number):
    """Fetch the LAST page of issue comments. The Issues Comments API returns
    chronologically and the most recent reviews live on the last page when a
    PR has many comments, so paginate to the tail rather than reading page 1."""
    resp = requests.get(
        f"{API}/repos/{GITHUB_REPO}/issues/{pr_number}/comments",
        headers=HEADERS,
        params={"per_page": 100},
    )
    if resp.status_code != 200:
        return []
    link_header = resp.headers.get("Link", "")
    if 'rel="last"' in link_header:
        m = re.search(r'<([^>]+)>;\s*rel="last"', link_header)
        if m:
            last_resp = requests.get(m.group(1), headers=HEADERS)
            if last_resp.status_code == 200:
                data = last_resp.json()
                return data if isinstance(data, list) else []
    data = resp.json()
    return data if isinstance(data, list) else []


def _find_last_review_and_fix(comments):
    """Walk comments and return (last_changes_requested_time, last_fix_time)
    for deciding whether a dev fix was already posted after the last review."""
    last_review_time = None
    last_fix_time = None
    for c in comments:
        body = c.get("body", "") or ""
        header = body.split("\n")[0]
        if ("QA Review" in header or "Security Review" in header) and "CHANGES REQUESTED" in header.upper():
            last_review_time = c.get("created_at", "")
        if body.startswith("## Fix iteration") or body.startswith("## Review feedback addressed"):
            last_fix_time = c.get("created_at", "")
    return last_review_time, last_fix_time


def reconcile_pr(pr):
    """Decide what to do with an open PR based on its latest review state.
    The single source of truth for both `_on_review_approved` (event-driven)
    and `poll_github`'s recovery scan (periodic). Sequential review flow:
    QA → Security → architect-merge."""
    if pr.get("draft"):
        return
    pr_number = pr["number"]

    try:
        comments = _fetch_pr_comments_last_page(pr_number)
    except Exception as e:
        log.debug(f"Reconcile PR #{pr_number}: {e}")
        return

    qa_state, sec_state = _classify_pr_reviews(comments)

    if qa_state == "approved" and sec_state == "approved":
        dispatch_architect_merge(pr)
        return

    if qa_state == "changes" or sec_state == "changes":
        last_review_time, last_fix_time = _find_last_review_and_fix(comments)
        if last_fix_time and last_review_time and last_fix_time > last_review_time:
            log.info(f"PR #{pr_number} — fix exists after last review, re-triggering QA")
            state.clear_handled(f"qa-{pr_number}")
            state.clear_handled(f"security-{pr_number}")
            dispatch_qa(pr)
        else:
            dispatch_needs_fixes(pr)
        return

    if qa_state is None:
        state.clear_handled(f"qa-{pr_number}")
        dispatch_qa(pr)
    elif qa_state == "approved" and sec_state is None:
        state.clear_handled(f"security-{pr_number}")
        dispatch_security(pr)


def _on_review_approved(pr_number, pr_api_url):
    """An approval landed — reconcile the PR. Thin wrapper that fetches the
    full PR object so reconcile_pr has everything it needs."""
    try:
        r = requests.get(pr_api_url, headers=HEADERS)
        if r.status_code != 200:
            log.warning(f"Could not fetch PR #{pr_number}: HTTP {r.status_code}")
            return
        reconcile_pr(r.json())
    except Exception as e:
        log.error(f"Reconcile after approval for PR #{pr_number}: {e}")


# ═══════════════════════════════════════════════════════════
#  MODE 1: WEBHOOK SERVER (instant, preferred)
# ═══════════════════════════════════════════════════════════

class WebhookHandler(BaseHTTPRequestHandler):
    """Receives GitHub webhook POST requests."""

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # /pause and /resume are local control endpoints — protected by HMAC
        # like webhooks, so the same WEBHOOK_SECRET is the shared credential.
        # They flip state.paused so spawn_agent refuses new work.
        if self.path in ("/pause", "/resume"):
            signature = self.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(
                WEBHOOK_SECRET.encode(), body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                self.send_response(403)
                self.end_headers()
                return
            try:
                payload = json.loads(body) if body else {}
            except Exception:
                payload = {}
            with state.lock:
                state.paused = (self.path == "/pause")
                state.paused_reason = payload.get("reason", "") if state.paused else ""
            log.info(f"Daemon {'paused' if state.paused else 'resumed'} via HTTP")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"paused": state.paused}).encode())
            return

        # /sweep-blocked is a retroactive-cleanup endpoint: scans every open
        # issue with the `blocked` label and routes the resolved ones through
        # architect re-triage. HMAC-protected with the same WEBHOOK_SECRET.
        if self.path == "/sweep-blocked":
            signature = self.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(
                WEBHOOK_SECRET.encode(), body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                self.send_response(403)
                self.end_headers()
                return
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"started": true}')
            threading.Thread(target=sweep_blocked_issues, daemon=True).start()
            return

        # WEBHOOK_SECRET is required at startup in webhook mode (see main),
        # so signature verification is unconditional here.
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
            queue = [{"role": r, "issue": i["number"], "title": i.get("title", "")}
                     for r, i in state.dev_queue]
            pending_fixes = sorted(state.pending_fix_prs)
            usage_totals = dict(state.usage_totals)
            usage_by_role = {k: dict(v) for k, v in state.usage_by_role.items()}
            paused = state.paused
            paused_reason = state.paused_reason
            daily_cost = state.daily_cost_usd
            daily_date = state.daily_cost_date.isoformat()
            now = datetime.now(timezone.utc)
            retries = {
                k: {
                    "attempts": v,
                    "backoff_until": (
                        state.retry_backoff_until[k].isoformat()
                        if k in state.retry_backoff_until else None
                    ),
                    "backoff_remaining_s": (
                        max(0, int((state.retry_backoff_until[k] - now).total_seconds()))
                        if k in state.retry_backoff_until else None
                    ),
                }
                for k, v in state.retry_counts.items()
            }
        self.wfile.write(json.dumps({
            "status": "ok",
            "paused": paused,
            "paused_reason": paused_reason,
            "mode": MODE,
            "repo": GITHUB_REPO,
            "active_agents": active,
            "frontend_dev_slots": f"{state.frontend_dev_count}/{MAX_FRONTEND_AGENTS}",
            "backend_dev_slots": f"{state.backend_dev_count}/{MAX_BACKEND_AGENTS}",
            "fullstack_dev_slots": f"{state.fullstack_dev_count}/{MAX_FULLSTACK_AGENTS}",
            "dev_queue_length": len(queue),
            "dev_queue": queue,
            "pending_fix_prs": pending_fixes,
            "max_total_agents": MAX_TOTAL_AGENTS,
            "credentials": _credentials_status(),
            "retries": retries,
            "budget": {
                "daily_limit_usd": DAILY_BUDGET_USD,
                "daily_spent_usd": round(daily_cost, 4),
                "daily_date_utc": daily_date,
                "exceeded": (DAILY_BUDGET_USD > 0 and daily_cost >= DAILY_BUDGET_USD),
            },
            "usage": {
                "since_start": usage_totals,
                "by_role": usage_by_role,
            },
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
        elif label_name in DEV_ROLES:
            _dispatch_dev(label_name, issue)

    # ── PR merged → unblock dependent issues ───────────
    elif event == "pull_request" and action == "closed":
        pr = payload.get("pull_request", {})
        if pr.get("merged"):
            dispatch_dependents(pr)

    # ── Issue closed (manual close, not via PR) → unblock dependents ──
    elif event == "issues" and action == "closed":
        issue = payload.get("issue", {})
        issue_number = issue.get("number")
        if issue_number:
            _unblock_dependents_of({issue_number})

    # ── PR opened or updated ──────────────────────────
    # Reviews run sequentially: QA first; Security runs only after QA
    # approves. Agents share a token so formal PR approvals don't work —
    # approval is detected from comment headers instead.
    elif event == "pull_request" and action in ("opened", "reopened", "synchronize"):
        pr = payload.get("pull_request", {})
        if not pr.get("draft"):
            state.clear_handled(f"qa-{pr['number']}")
            state.clear_handled(f"security-{pr['number']}")
            dispatch_qa(pr)

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
                    _on_review_approved(pr_number, pr_url)

    # ── PR review submitted (fallback) ────────────────
    elif event == "pull_request_review" and action == "submitted":
        review = payload.get("review", {})
        pr = payload.get("pull_request", {})
        if review.get("state") == "approved":
            number = pr["number"]
            pr_url = pr.get("url", f"{API}/repos/{GITHUB_REPO}/pulls/{number}")
            _on_review_approved(number, pr_url)

    # ── Ping (setup confirmation) ─────────────────────
    elif event == "ping":
        log.info(f"Webhook connected: {repo}")


def webhook_retry_loop():
    """Background loop for webhook mode: retries agents after transient error backoff,
    runs a periodic safety-net poll to recover stuck issues, and periodically
    sweeps `blocked` issues so a missed unblock event is eventually self-healed."""
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

            # Periodic blocked-sweep. The loop ticks every 60s, so
            # `poll_counter % BLOCKED_SWEEP_INTERVAL_MIN == 0` gives a sweep
            # every N minutes. 0 disables it.
            if BLOCKED_SWEEP_INTERVAL_MIN > 0 and poll_counter % BLOCKED_SWEEP_INTERVAL_MIN == 0:
                log.info(f"Periodic blocked-sweep (every {BLOCKED_SWEEP_INTERVAL_MIN}m)")
                sweep_blocked_issues()
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
    spawn_agent's duplicate check prevents double-spawning.

    PRIORITY: open PRs awaiting fixes are dispatched BEFORE new issues.
    Finishing existing PRs is more valuable than starting new ones — see
    drain_queue() for the full rationale. This ordering also matters at
    daemon startup, since the recovery scan goes through this same path."""

    # Architect runs first — exempt from dev cap, quick, and unblocks merges
    for issue in gh_get_issues("architect"):
        number = issue["number"]
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

    # PRs labeled needs-fixes BEFORE new issues. dispatch_needs_fixes will
    # park them in pending_fix_prs if devs are saturated, where drain_queue
    # will pick them up first when a slot frees.
    for pr in gh_get_prs("open"):
        if pr.get("draft"):
            continue
        labels = {l["name"] for l in pr.get("labels", [])}
        if "needs-fixes" in labels:
            dispatch_needs_fixes(pr)

    for label in DEV_ROLES:
        for issue in gh_get_issues(label):
            _dispatch_dev(label, issue)

    # Recover labeled issues that were handled but have no agent running and no PR.
    # This catches cases where an agent failed before swapping labels (e.g., rate limit
    # on first attempt, container crash before label change).
    # Skip issues already queued or pending — they'll be dispatched when a slot frees.
    # Skip issues with an active retry backoff — otherwise we tight-loop dispatching
    # agents that fail fast on 401/rate-limit, each failure re-triggering recovery.
    # Re-read state fresh since earlier dispatch calls may have mutated it.
    for label in DEV_ROLES:
        for issue in gh_get_issues(label):
            number = issue["number"]
            key = f"{label}-{number}"
            issue_labels = {l["name"] for l in issue.get("labels", [])}
            # Skip issues that have been manually triaged out of the workflow.
            if "blocked" in issue_labels or "needs-attention" in issue_labels:
                continue
            with state.lock:
                in_queue = any(r == label and i["number"] == number for r, i in state.dev_queue)
                active_numbers = {v["number"] for v in state.active_containers.values()}
                backoff_until = state.retry_backoff_until.get(key)
            if in_queue:
                continue
            if backoff_until and datetime.now(timezone.utc) < backoff_until:
                continue
            if key in state.processed and number not in active_numbers and not gh_issue_has_open_pr(number):
                log.info(f"Recovering stuck #{number} — clearing handled state for {label}")
                state.clear_handled(key)
                _dispatch_dev(label, issue)

    # Stuck architect-in-progress issues (no agent running).
    #
    # Two recovery paths depending on the issue body:
    #   1. Body declares closed deps (`Depends on #N` with every #N closed) →
    #      this is a stuck re-triage. Re-dispatch via
    #      `dispatch_architect_retriage` so the architect re-runs against
    #      the unblock context (with `closed_dependencies`). Adding the
    #      `architect` label here would dispatch `plan_feature` instead
    #      and lose the re-triage context.
    #   2. Otherwise → stuck planner. Strip `architect-in-progress` and
    #      add `architect` so the next poll re-dispatches as `plan_feature`.
    with state.lock:
        active_numbers = {v["number"] for v in state.active_containers.values()}
    for issue in gh_get_issues("architect-in-progress"):
        if issue.get("state") == "closed" or issue["number"] in active_numbers:
            continue
        number = issue["number"]
        is_candidate, closed_deps = _is_unblock_candidate(issue)
        if is_candidate:
            log.info(f"Recovering stuck architect #{number} as re-triage (deps closed: {closed_deps})")
            gh_remove_label(number, "architect-in-progress")
            # Clear handled key before dispatching so the re-dispatch
            # actually fires — without this, a prior in-process re-triage
            # attempt would mask the retry.
            state.clear_handled(f"architect-retriage-{number}")
            dispatch_architect_retriage(issue, closed_deps)
        else:
            log.info(f"Recovering stuck architect #{number}")
            gh_remove_label(number, "architect-in-progress")
            gh_add_label(number, "architect")

    # Stuck dev-in-progress issues (no agent running, no open PR)
    # Re-read queued/pending state fresh since earlier dispatch calls may have
    # added entries since the last snapshot.
    with state.lock:
        active_numbers = {v["number"] for v in state.active_containers.values()}
        queued_numbers = {i["number"] for _, i in state.dev_queue}
        pending_numbers = state.pending_fix_prs.copy()
        dev_count = sum(
            1 for v in state.active_containers.values()
            if v.get("role") in DEV_ROLES
        )
    for issue in gh_get_issues("dev-in-progress"):
        if issue.get("state") == "closed" or issue["number"] in active_numbers:
            continue
        issue_labels = {l["name"] for l in issue.get("labels", [])}
        if "blocked" in issue_labels or "needs-attention" in issue_labels:
            continue
        if issue["number"] in queued_numbers or issue["number"] in pending_numbers:
            continue
        if gh_issue_has_open_pr(issue["number"]):
            continue
        # If devs are saturated, queue the issue directly instead of re-labeling
        # and triggering another dispatch cycle that will just defer again.
        body = issue.get("body", "") or ""
        other_labels = [l["name"] for l in issue.get("labels", []) if l["name"] != "dev-in-progress"]
        labels_text = " ".join(other_labels).lower()
        if "fullstack" in labels_text or "fullstack" in body.lower():
            dev_label = "fullstack-dev"
        elif "backend" in labels_text or "backend" in body.lower():
            dev_label = "backend-dev"
        else:
            dev_label = "frontend-dev"
        if dev_count >= MAX_TOTAL_AGENTS:
            with state.lock:
                already_queued = any(r == dev_label and i["number"] == issue["number"] for r, i in state.dev_queue)
                if not already_queued:
                    state.dev_queue.append((dev_label, issue))
                    log.info(f"Queued stuck #{issue['number']} — devs saturated ({dev_label}, {len(state.dev_queue)} in queue)")
            continue
        log.info(f"Recovering stuck #{issue['number']} → {dev_label}")
        gh_remove_label(issue["number"], "dev-in-progress")
        gh_add_label(issue["number"], dev_label)

    # Open PRs — defer to the unified reconcile_pr decision tree
    for pr in gh_get_prs("open"):
        reconcile_pr(pr)


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
        # Safety net: kill any containers over the global limit.
        # The spawn-time check exempts qa/security/architect from the cap, so the
        # safety net must do the same — otherwise long-running reviewers/mergers
        # get reaped while devs sit comfortably under the cap.
        try:
            all_agents = [
                c for c in docker_client.containers.list(filters={"name": f"{PROJECT_NAME}-"})
                if not c.name.endswith("-daemon") and not c.name.endswith("-tunnel")
            ]
            EXEMPT_ROLES = ("-qa-", "-security-", "-architect-")
            dev_containers = [c for c in all_agents if not any(r in c.name for r in EXEMPT_ROLES)]
            if len(dev_containers) > MAX_TOTAL_AGENTS:
                log.warning(f"Safety net: {len(dev_containers)} dev agents running (limit {MAX_TOTAL_AGENTS}) — stopping excess")
                # Kill newest devs first — older devs are further along, more expensive to restart
                dev_containers.sort(key=lambda c: c.attrs.get("Created", ""))
                for c in dev_containers[MAX_TOTAL_AGENTS:]:
                    log.warning(f"Stopping excess container: {c.name}")
                    c.stop(timeout=10)
        except Exception as e:
            log.debug(f"Safety net check: {e}")

        # Reconcile state.active_containers against actual docker state.
        # If a container exited but its monitor thread crashed/never reaped it,
        # the entry sticks around forever and blocks new spawns. Fix that here.
        try:
            live_ids = {c.id for c in docker_client.containers.list()}
            with state.lock:
                ghosts = [(cid, info) for cid, info in state.active_containers.items() if cid not in live_ids]
            if ghosts:
                log.warning(f"Reconcile: removing {len(ghosts)} ghost container(s) from state")
                with state.lock:
                    for cid, info in ghosts:
                        state.active_containers.pop(cid, None)
                        _adjust_dev_count(info.get("role"), -1)
                        # Counters are non-negative — recovery handles drift,
                        # but clamp to zero so a bug here can't go negative.
                        state.frontend_dev_count = max(0, state.frontend_dev_count)
                        state.backend_dev_count = max(0, state.backend_dev_count)
                        state.fullstack_dev_count = max(0, state.fullstack_dev_count)
                drain_queue()
        except Exception as e:
            log.debug(f"Ghost reconcile: {e}")


    # retry_loop removed — poll_github handles everything directly


# ─── Status ──────────────────────────────────────────────

def status_line():
    with state.lock:
        active = [f"{v['role']}(#{v['number']})" for v in state.active_containers.values()]
    if active:
        log.info(f"Active: {', '.join(active)} | "
                 f"Frontend: {state.frontend_dev_count}/{MAX_FRONTEND_AGENTS} | "
                 f"Backend: {state.backend_dev_count}/{MAX_BACKEND_AGENTS} | "
                 f"Fullstack: {state.fullstack_dev_count}/{MAX_FULLSTACK_AGENTS}")
    else:
        log.info(f"Idle — watching {GITHUB_REPO}")


# ─── Main ────────────────────────────────────────────────

def main():
    if MODE == "webhook" and not WEBHOOK_SECRET:
        # An empty secret silently disables HMAC verification. Cloudflare
        # tunnel obscurity is not auth — anyone who learns the URL can
        # trigger arbitrary agent spawns. Hard-fail rather than degrade.
        log.error("WEBHOOK_SECRET is empty in webhook mode. Set it in .env and restart.")
        sys.exit(1)

    log.info("═══════════════════════════════════════")
    log.info(f"  Agent Team Daemon")
    log.info(f"  Mode: {MODE}")
    log.info(f"  Repo: {GITHUB_REPO}")
    log.info(f"  Frontend devs: {MAX_FRONTEND_AGENTS}")
    log.info(f"  Backend devs: {MAX_BACKEND_AGENTS}")
    log.info(f"  Fullstack devs: {MAX_FULLSTACK_AGENTS}")
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
                "mkdir -p /memory/agents/{architect,architect-merger,frontend-dev,backend-dev,qa,security} "
                "/memory/inbox/{architect,architect-merger,frontend-dev,backend-dev,qa,security} "
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

    # Startup blocked-sweep. Catches any unblock event missed while the
    # daemon was down (the in-memory `state.processed` set resets on
    # restart, so without a sweep an already-stripped-but-undispatched
    # issue stays orphaned). Run in a background thread so the HTTP server
    # comes up promptly — an operator can hit `/pause` while a long sweep
    # is still scanning issues.
    def _startup_sweep():
        try:
            log.info("Startup blocked-sweep: scanning for issues whose deps closed while down")
            result = sweep_blocked_issues()
            log.info(f"Startup blocked-sweep: {result}")
        except Exception as e:
            log.warning(f"Startup blocked-sweep failed: {e}", exc_info=True)

    threading.Thread(target=_startup_sweep, daemon=True).start()

    if MODE == "webhook":
        threading.Thread(target=webhook_retry_loop, daemon=True).start()
        run_webhook_server()
    else:
        run_poller()


if __name__ == "__main__":
    main()
