#!/usr/bin/env bash
set -euo pipefail

# ─── Create a new AutoTeam project instance ────────────────
#
# Usage: ./new-project.sh <github-owner/repo>
#
# Creates a project directory under ./projects/ with its own .env
# and compose override. All projects share the same agent images.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECTS_DIR="${SCRIPT_DIR}/projects"

if [ $# -lt 1 ]; then
    echo "Usage: $0 <github-owner/repo>"
    echo ""
    echo "Example: $0 myuser/my-cool-app"
    echo ""
    echo "Existing projects:"
    if [ -d "${PROJECTS_DIR}" ]; then
        for d in "${PROJECTS_DIR}"/*/; do
            [ -d "$d" ] || continue
            name=$(basename "$d")
            repo=$(grep GITHUB_REPO "$d/.env" 2>/dev/null | cut -d= -f2 || echo "unknown")
            running=$(docker ps --filter "name=${name}-daemon" --format '{{.Status}}' 2>/dev/null || echo "")
            status="${running:-stopped}"
            echo "  ${name} → ${repo} [${status}]"
        done
    else
        echo "  (none)"
    fi
    exit 1
fi

GITHUB_REPO="$1"
# Derive project name from repo: owner/my-project → my-project
PROJECT_NAME=$(echo "$GITHUB_REPO" | cut -d/ -f2 | tr '[:upper:]' '[:lower:]' | tr ' ' '-')
PROJECT_DIR="${PROJECTS_DIR}/${PROJECT_NAME}"

if [ -d "${PROJECT_DIR}" ]; then
    echo "Project '${PROJECT_NAME}' already exists at ${PROJECT_DIR}"
    echo "To manage it:"
    echo "  cd ${PROJECT_DIR} && make up"
    exit 1
fi

mkdir -p "${PROJECT_DIR}"

# ─── Copy shared .env values or prompt ─────────────────────
# Try to find credentials from an existing project or the main .env
EXISTING_ENV=""
if [ -f "${SCRIPT_DIR}/.env" ]; then
    EXISTING_ENV="${SCRIPT_DIR}/.env"
elif [ -d "${PROJECTS_DIR}" ]; then
    EXISTING_ENV=$(find "${PROJECTS_DIR}" -name ".env" -maxdepth 2 | head -1)
fi

GITHUB_TOKEN=""
ANTHROPIC_API_KEY=""
CLAUDE_CREDENTIALS_PATH=""

if [ -n "${EXISTING_ENV}" ]; then
    GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" "${EXISTING_ENV}" | cut -d= -f2 || true)
    ANTHROPIC_API_KEY=$(grep "^ANTHROPIC_API_KEY=" "${EXISTING_ENV}" | cut -d= -f2 || true)
    CLAUDE_CREDENTIALS_PATH=$(grep "^CLAUDE_CREDENTIALS_PATH=" "${EXISTING_ENV}" | cut -d= -f2 || true)
    echo "Reusing credentials from ${EXISTING_ENV}"
fi

# Generate unique webhook secret and port
WEBHOOK_SECRET=$(openssl rand -hex 20)
# Find an available port starting from 9876
BASE_PORT=9876
PORT=${BASE_PORT}
while docker ps --format '{{.Ports}}' 2>/dev/null | grep -q ":${PORT}->"; do
    PORT=$((PORT + 1))
done

# ─── Write .env ────────────────────────────────────────────
cat > "${PROJECT_DIR}/.env" << EOF
# ─── Project: ${PROJECT_NAME} ─────────────────────────────
GITHUB_TOKEN=${GITHUB_TOKEN}
GITHUB_REPO=${GITHUB_REPO}

# Claude Auth
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
CLAUDE_CREDENTIALS_PATH=${CLAUDE_CREDENTIALS_PATH}
CLAUDE_CREDENTIALS_DIR=$(dirname "${CLAUDE_CREDENTIALS_PATH}")

# Daemon
MODE=webhook
WEBHOOK_PORT=${PORT}
WEBHOOK_SECRET=${WEBHOOK_SECRET}
COMPOSE_PROJECT_NAME=${PROJECT_NAME}

# Agents
MAX_FRONTEND_AGENTS=1
MAX_BACKEND_AGENTS=1
MAX_FIX_ITERATIONS=3
EOF

# ─── Write Makefile that delegates to the main one ─────────
cat > "${PROJECT_DIR}/Makefile" << 'MAKEFILE'
# Auto-generated project Makefile — delegates to the main agent-team setup
AGENT_TEAM_DIR := $(shell dirname $(realpath $(lastword $(MAKEFILE_LIST))))/../..

include .env
export

COMPOSE := docker compose -f $(AGENT_TEAM_DIR)/docker-compose.yml -p $(COMPOSE_PROJECT_NAME) --env-file $(CURDIR)/.env

.PHONY: up down logs status health build init-repo clean

up:
	$(COMPOSE) build daemon
	$(COMPOSE) up -d daemon
	@sleep 3
	@echo ""
	@echo "  $(COMPOSE_PROJECT_NAME) is running!"
	@echo "  Repo: $(GITHUB_REPO)"

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f daemon

status:
	@docker ps --filter "name=$(COMPOSE_PROJECT_NAME)" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

health:
	@curl -s http://localhost:$(WEBHOOK_PORT)/ | python3 -m json.tool

build:
	cd $(AGENT_TEAM_DIR) && make build

init-repo:
	@docker volume create $(COMPOSE_PROJECT_NAME)_repo-cache 2>/dev/null || true
	docker run --rm \
		-e GITHUB_TOKEN=$(GITHUB_TOKEN) \
		-e GITHUB_REPO=$(GITHUB_REPO) \
		-v $(COMPOSE_PROJECT_NAME)_repo-cache:/repo \
		--entrypoint bash \
		agent-base:latest \
		-c 'if [ -d /repo/.git ]; then cd /repo && git fetch origin && git reset --hard origin/HEAD; else git clone https://x-access-token:$$GITHUB_TOKEN@github.com/$$GITHUB_REPO.git /repo; fi'
	@echo "✓ Repo cached"

clean:
	$(COMPOSE) down -v
	docker rm -f $$(docker ps -a --filter "name=$(COMPOSE_PROJECT_NAME)-" -q) 2>/dev/null || true
	@echo "✓ Cleaned $(COMPOSE_PROJECT_NAME)"
MAKEFILE

echo ""
echo "═══════════════════════════════════════════"
echo "  Project created: ${PROJECT_NAME}"
echo "  Repo: ${GITHUB_REPO}"
echo "  Dir:  ${PROJECT_DIR}"
echo "  Port: ${PORT}"
echo "═══════════════════════════════════════════"
echo ""
echo "  Next steps:"
echo "    cd ${PROJECT_DIR}"
if [ -z "${GITHUB_TOKEN}" ]; then
echo "    vim .env              # fill in your tokens"
fi
echo "    make init-repo        # clone the repo"
echo "    make up               # start the daemon"
echo ""
