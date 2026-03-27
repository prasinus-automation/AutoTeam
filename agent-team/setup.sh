#!/usr/bin/env bash
set -euo pipefail

# ─── Agent Team — Linux Setup ────────────────────────────
# Run once on a fresh box. Handles everything.
# Usage: ./setup.sh

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC}   $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERR]${NC}  $1"; }

echo ""
echo "═══════════════════════════════════════════"
echo "  Agent Team — Linux Setup"
echo "═══════════════════════════════════════════"
echo ""

# ─── Check we're on Linux ────────────────────────────────
if [[ "$(uname)" != "Linux" ]]; then
    err "This script is for Linux only."
    exit 1
fi

# ─── Docker ──────────────────────────────────────────────
if command -v docker &>/dev/null; then
    ok "Docker installed: $(docker --version | cut -d' ' -f3 | tr -d ',')"
else
    info "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    ok "Docker installed"
fi

# Add current user to docker group (avoids sudo for docker commands)
if ! groups "$USER" | grep -q docker; then
    info "Adding $USER to docker group..."
    sudo usermod -aG docker "$USER"
    warn "You'll need to log out and back in (or run 'newgrp docker') for this to take effect"
fi

# Docker Compose (v2 is bundled with modern Docker)
if docker compose version &>/dev/null; then
    ok "Docker Compose: $(docker compose version --short)"
else
    err "Docker Compose not found. Install a recent Docker version."
    exit 1
fi

# ─── .env file ───────────────────────────────────────────
if [ ! -f .env ]; then
    info "Creating .env from template..."
    cp .env.example .env

    # Generate a webhook secret
    WEBHOOK_SECRET=$(openssl rand -hex 20)
    sed -i "s/^WEBHOOK_SECRET=.*/WEBHOOK_SECRET=${WEBHOOK_SECRET}/" .env

    echo ""
    warn "Edit .env and fill in these values:"
    echo "  GITHUB_TOKEN    — GitHub PAT with 'repo' scope"
    echo "  GITHUB_REPO     — owner/repo (e.g., yourname/myproject)"
    echo "  ANTHROPIC_API_KEY — your Anthropic API key"
    echo ""
    echo "  WEBHOOK_SECRET has been auto-generated: ${WEBHOOK_SECRET}"
    echo ""
    read -p "Press Enter after editing .env to continue (or Ctrl+C to do it later)... "
else
    ok ".env already exists"
fi

# ─── Validate .env ───────────────────────────────────────
source .env 2>/dev/null || true

if [[ "${GITHUB_TOKEN:-}" == "ghp_your_token_here" ]] || [[ -z "${GITHUB_TOKEN:-}" ]]; then
    err "GITHUB_TOKEN not set in .env"
    exit 1
fi
if [[ "${GITHUB_REPO:-}" == "yourusername/yourproject" ]] || [[ -z "${GITHUB_REPO:-}" ]]; then
    err "GITHUB_REPO not set in .env"
    exit 1
fi
if [[ -n "${CLAUDE_CREDENTIALS_PATH:-}" ]] && [[ -f "${CLAUDE_CREDENTIALS_PATH}" ]]; then
    ok "Claude subscription credentials: ${CLAUDE_CREDENTIALS_PATH}"
elif [[ "${ANTHROPIC_API_KEY:-}" != "sk-ant-your_key_here" ]] && [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    ok "Anthropic API key configured"
else
    err "No Claude auth configured. Set ANTHROPIC_API_KEY or CLAUDE_CREDENTIALS_PATH in .env"
    echo "  For subscription auth: run 'claude' on this machine, complete login,"
    echo "  then set CLAUDE_CREDENTIALS_PATH=\$HOME/.claude/.credentials.json"
    exit 1
fi
ok "Credentials configured"

# ─── Create GitHub labels ────────────────────────────────
info "Creating GitHub labels on ${GITHUB_REPO}..."

create_label() {
    local name="$1" color="$2" desc="$3"
    curl -s -o /dev/null -w "%{http_code}" \
        -X POST "https://api.github.com/repos/${GITHUB_REPO}/labels" \
        -H "Authorization: token ${GITHUB_TOKEN}" \
        -H "Accept: application/vnd.github.v3+json" \
        -d "{\"name\":\"${name}\",\"color\":\"${color}\",\"description\":\"${desc}\"}" \
        2>/dev/null
}

for label_info in \
    "architect:E8B931:Triggers the Architect agent" \
    "architect-in-progress:C49A1C:Architect is working on this" \
    "frontend-dev:4FC1E8:Ready for a Frontend Developer agent" \
    "backend-dev:2A7AB0:Ready for a Backend Developer agent" \
    "dev-in-progress:1D76DB:Developer is working on this" \
    "needs-fixes:D93F0B:PR needs fixes from review feedback"; do

    IFS=: read -r name color desc <<< "$label_info"
    code=$(create_label "$name" "$color" "$desc")
    if [[ "$code" == "201" ]]; then
        ok "Created label: $name"
    elif [[ "$code" == "422" ]]; then
        ok "Label exists: $name"
    else
        warn "Label '$name' returned HTTP $code"
    fi
done

# ─── Build images ────────────────────────────────────────
info "Building Docker images (this takes a few minutes first time)..."
make build

# ─── Init repo cache ────────────────────────────────────
info "Cloning ${GITHUB_REPO} into cache volume..."
make init-repo

# ─── Install systemd service ────────────────────────────
info "Installing systemd service..."
WORK_DIR="$(pwd)"

sudo cp agent-team.service /etc/systemd/system/agent-team.service
sudo sed -i "s|%WORK_DIR%|${WORK_DIR}|g" /etc/systemd/system/agent-team.service
sudo sed -i "s|%USER%|${USER}|g" /etc/systemd/system/agent-team.service
sudo systemctl daemon-reload
sudo systemctl enable agent-team.service
ok "Systemd service installed and enabled (starts on boot)"

# ─── Start it up ─────────────────────────────────────────
info "Starting Agent Team..."
sudo systemctl start agent-team.service
sleep 3

# ─── Get tunnel URL ──────────────────────────────────────
info "Waiting for tunnel URL..."
sleep 5

TUNNEL_URL=""
for i in {1..10}; do
    TUNNEL_URL=$(docker compose logs tunnel 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 || true)
    if [ -n "$TUNNEL_URL" ]; then
        break
    fi
    sleep 2
done

echo ""
echo "═══════════════════════════════════════════"
echo -e "  ${GREEN}✓ Agent Team is running!${NC}"
echo "═══════════════════════════════════════════"
echo ""

if [ -n "$TUNNEL_URL" ]; then
    echo -e "  Webhook URL: ${CYAN}${TUNNEL_URL}${NC}"
    echo ""
    echo "  Go to: https://github.com/${GITHUB_REPO}/settings/hooks/new"
    echo ""
    echo "  Payload URL:   ${TUNNEL_URL}"
    echo "  Content type:  application/json"
    echo "  Secret:        ${WEBHOOK_SECRET}"
    echo "  Events:        Issues + Pull requests"
else
    warn "Couldn't detect tunnel URL yet. Run: make tunnel-url"
fi

echo ""
echo "  Commands:"
echo "    make logs        — watch daemon"
echo "    make status      — see running agents"
echo "    make tunnel-url  — get webhook URL"
echo "    sudo systemctl status agent-team  — service status"
echo ""
echo "  Create an issue labeled 'architect' to start!"
echo ""
