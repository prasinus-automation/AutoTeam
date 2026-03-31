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

# ─── Run Claude Code ─────────────────────────────────────
echo ""
echo "─── Starting Claude Code ────────────────"
echo ""

# Claude Code runs with:
#   - The role-specific system prompt (+ project context from AGENTS.md)
#   - The task context
#   - Access to the working directory (repo)
#   - GitHub token for API operations

echo "${TASK_CONTENT}" | claude --print \
    --system-prompt "${FULL_SYSTEM_PROMPT}" \
    --allowedTools "Bash,Read,Write,Edit,GitHub"

EXIT_CODE=$?

echo ""
echo "─── Claude Code exited: ${EXIT_CODE} ────────"
echo ""

exit ${EXIT_CODE}
