#!/usr/bin/env bash
set -euo pipefail

# ─── Agent Entrypoint ────────────────────────────────────
# Called by the daemon with:
#   agent-entrypoint.sh <task-file>
#
# The task file is a JSON blob with context about what to do.
# The system prompt is baked into the image at /prompts/system.md
# ──────────────────────────────────────────────────────────

TASK_FILE="${1:-}"
ROLE="${AGENT_ROLE:-unknown}"

echo "═══════════════════════════════════════"
echo "  Agent: ${ROLE}"
echo "  Task:  ${TASK_FILE}"
echo "  Time:  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "═══════════════════════════════════════"

# ─── Copy Claude credentials from host directory mount ──
# The daemon mounts /host-claude (the host's ~/.claude directory) read-only
# instead of bind-mounting the credentials file directly. This avoids the
# inode-pinning bug where atomic-rename refreshes on the host never reach
# the container. Copy the latest credentials file into place each time the
# agent starts so we always pick up the most recent token.
if [ -f /host-claude/.credentials.json ]; then
    mkdir -p /root/.claude
    cp /host-claude/.credentials.json /root/.claude/.credentials.json
    chmod 600 /root/.claude/.credentials.json
fi

# ─── Authenticate with GitHub ────────────────────────────
if [ -n "${GITHUB_TOKEN:-}" ]; then
    # gh CLI automatically uses GITHUB_TOKEN env var for auth
    git config --global url."https://x-access-token:${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"
    echo "✓ GitHub authenticated"
fi

# ─── Set up repo ─────────────────────────────────────────
REPO_DIR="/repo"
WORK_DIR="/work"

if [ -d "${REPO_DIR}/.git" ]; then
    echo "✓ Repo cache found at ${REPO_DIR}"

    # All agents work on a copy of the repo cache (cache is read-only)
    cp -r "${REPO_DIR}/." "${WORK_DIR}/"
    cd "${WORK_DIR}"
    git fetch origin main --quiet 2>/dev/null || true

    # Dev/QA/Security agents create their own branches
    if [ "${ROLE}" = "frontend-dev" ] || [ "${ROLE}" = "backend-dev" ] || [ "${ROLE}" = "qa" ] || [ "${ROLE}" = "security" ]; then
        BRANCH_NAME="agent/${ROLE}-$(date +%s)"
        git checkout -b "${BRANCH_NAME}" origin/main 2>/dev/null || git checkout -b "${BRANCH_NAME}"
        echo "✓ Branch created: ${BRANCH_NAME}"
    else
        echo "✓ Working in ${WORK_DIR}"
    fi
else
    echo "⚠ No repo cache — cloning fresh"
    if [ -n "${GITHUB_REPO:-}" ]; then
        git clone "https://github.com/${GITHUB_REPO}.git" "${WORK_DIR}"
        cd "${WORK_DIR}"
    fi
fi

# ─── Install shared subagent library ────────────────────
# Claude Code reads project-scoped subagents from .claude/agents/ in the
# current working directory. Copy the AutoTeam shared library into place
# (without clobbering anything the project already ships) so every agent
# has access to Explore, schema-auditor, test-runner, etc. The library
# was baked into the image at /agent-team-claude-agents during the
# Dockerfile.base build.
if [ -d /agent-team-claude-agents ]; then
    mkdir -p "${WORK_DIR}/.claude/agents"
    # -n = no clobber: project's own subagents win if names collide.
    cp -nr /agent-team-claude-agents/. "${WORK_DIR}/.claude/agents/" 2>/dev/null || true
    count=$(ls "${WORK_DIR}/.claude/agents/" 2>/dev/null | wc -l)
    echo "✓ Subagent library installed (${count} subagents available)"
fi

# ─── Build the task prompt ───────────────────────────────
SYSTEM_PROMPT="/prompts/system.md"
TASK_CONTENT=""

if [ -n "${TASK_FILE}" ] && [ -f "${TASK_FILE}" ]; then
    TASK_CONTENT=$(cat "${TASK_FILE}")
    echo "✓ Task loaded ($(wc -c < "${TASK_FILE}") bytes)"
elif [ -n "${TASK_FILE}" ]; then
    # Task passed as a string directly
    TASK_CONTENT="${TASK_FILE}"
fi

# ─── Load project context ────────────────────────────────
# AGENTS.md in the repo root provides project-specific context
# (tech stack, versions, conventions) that all agents need.
PROJECT_CONTEXT=""
if [ -f "${WORK_DIR}/AGENTS.md" ]; then
    PROJECT_CONTEXT=$(cat "${WORK_DIR}/AGENTS.md")
    echo "✓ Project context loaded (AGENTS.md)"
fi

# ─── Build system prompt ─────────────────────────────────
# Combine the role-specific prompt with project context
FULL_SYSTEM_PROMPT="$(cat ${SYSTEM_PROMPT})"
if [ -n "${PROJECT_CONTEXT}" ]; then
    FULL_SYSTEM_PROMPT="${FULL_SYSTEM_PROMPT}

---

# Project Context (from AGENTS.md)

The following is project-specific context maintained by the Architect. Follow these conventions strictly — they override any assumptions from your training data.

${PROJECT_CONTEXT}"
fi

# ─── Load agent memory ──────────────────────────────────
MEMORY_DIR="/memory"
MEMORY_CONTEXT=""

if [ -d "${MEMORY_DIR}" ]; then
    # 1. Agent's own run log (last 100 lines to cap prompt size)
    AGENT_LOG="${MEMORY_DIR}/agents/${ROLE}/log.md"
    if [ -f "${AGENT_LOG}" ] && [ -s "${AGENT_LOG}" ]; then
        AGENT_LOG_CONTENT=$(tail -100 "${AGENT_LOG}")
        MEMORY_CONTEXT="${MEMORY_CONTEXT}

## Your Previous Runs
${AGENT_LOG_CONTENT}"
        echo "✓ Agent memory loaded ($(wc -l < "${AGENT_LOG}") lines)"
    fi

    # 2. Issue/PR-specific notes (extract number from task JSON)
    ISSUE_NUMBER=$(echo "${TASK_CONTENT}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('issue_number') or d.get('pr_number') or '')" 2>/dev/null || true)
    if [ -n "${ISSUE_NUMBER}" ]; then
        NOTES_FILE="${MEMORY_DIR}/issues/${ISSUE_NUMBER}/notes.md"
        if [ -f "${NOTES_FILE}" ] && [ -s "${NOTES_FILE}" ]; then
            MEMORY_CONTEXT="${MEMORY_CONTEXT}

## Notes from Other Agents (Issue/PR #${ISSUE_NUMBER})
$(cat "${NOTES_FILE}")"
            echo "✓ Issue #${ISSUE_NUMBER} notes loaded"
        fi
    fi

    # 3. Inbox messages
    INBOX_DIR="${MEMORY_DIR}/inbox/${ROLE}"
    if [ -d "${INBOX_DIR}" ] && [ "$(ls -A "${INBOX_DIR}" 2>/dev/null | grep -v '^read$')" ]; then
        INBOX_CONTENT=""
        for msg in "${INBOX_DIR}"/*.md; do
            [ -f "$msg" ] || continue
            INBOX_CONTENT="${INBOX_CONTENT}
---
$(cat "$msg")"
        done
        if [ -n "${INBOX_CONTENT}" ]; then
            MEMORY_CONTEXT="${MEMORY_CONTEXT}

## Inbox Messages
${INBOX_CONTENT}"
            echo "✓ Inbox messages loaded"
        fi
        # Mark messages as read
        mkdir -p "${INBOX_DIR}/read"
        mv "${INBOX_DIR}"/*.md "${INBOX_DIR}/read/" 2>/dev/null || true
    fi
fi

# Append memory to system prompt
if [ -n "${MEMORY_CONTEXT}" ]; then
    FULL_SYSTEM_PROMPT="${FULL_SYSTEM_PROMPT}

---

# Agent Memory

The following is your persistent memory from previous runs. Use it to avoid repeating mistakes, build on what worked, and coordinate with other agents.
${MEMORY_CONTEXT}"
fi

# ─── Run Claude Code ─────────────────────────────────────
echo ""
echo "─── Starting Claude Code ────────────────"
echo ""

# Claude Code runs with:
#   - The role-specific system prompt (+ project context from AGENTS.md + memory)
#   - The task context
#   - Access to the working directory (repo)
#   - GitHub token for API operations
#   - Read-write access to /memory for persisting state
#
# Use --output-format json so the final assistant text comes back wrapped
# with usage stats. We extract the text for human-readable logs and emit
# the usage block as a `USAGE_JSON: ...` marker line that the daemon
# greps out of container logs for cost tracking.

CLAUDE_OUTPUT=$(echo "${TASK_CONTENT}" | claude --print \
    --output-format json \
    --system-prompt "${FULL_SYSTEM_PROMPT}" \
    --allowedTools "Bash,Read,Write,Edit,GitHub")

EXIT_CODE=$?

if [ ${EXIT_CODE} -eq 0 ] && [ -n "${CLAUDE_OUTPUT}" ]; then
    # Print the assistant result text to stdout for log readability, then
    # emit the marker line that the daemon parses. CLAUDE_OUTPUT is passed
    # via env so the heredoc can stay as the script source.
    export CLAUDE_OUTPUT
    python3 - <<'PY'
import json, os, sys
raw = os.environ.get("CLAUDE_OUTPUT", "")
try:
    data = json.loads(raw)
except Exception as e:
    print(f"⚠ Failed to parse claude JSON output: {e}", file=sys.stderr)
    sys.exit(0)

result = data.get("result") or ""
if result:
    print(result)
    print()

usage = data.get("usage") or {}
cost = data.get("total_cost_usd")
duration_ms = data.get("duration_ms")
num_turns = data.get("num_turns")

summary_parts = []
if cost is not None:
    summary_parts.append(f"cost=${cost:.4f}")
if usage:
    summary_parts.append(f"in={usage.get('input_tokens', 0)}")
    if usage.get("cache_creation_input_tokens"):
        summary_parts.append(f"cache_create={usage['cache_creation_input_tokens']}")
    if usage.get("cache_read_input_tokens"):
        summary_parts.append(f"cache_read={usage['cache_read_input_tokens']}")
    summary_parts.append(f"out={usage.get('output_tokens', 0)}")
if num_turns is not None:
    summary_parts.append(f"turns={num_turns}")
if duration_ms is not None:
    summary_parts.append(f"duration={duration_ms/1000:.1f}s")
print(f"─── Usage: {' '.join(summary_parts)}")

marker = {
    "usage": usage,
    "total_cost_usd": cost,
    "duration_ms": duration_ms,
    "num_turns": num_turns,
}
print(f"USAGE_JSON: {json.dumps(marker, separators=(',', ':'))}")
PY
fi

echo ""
echo "─── Claude Code exited: ${EXIT_CODE} ────────"
echo ""

exit ${EXIT_CODE}
