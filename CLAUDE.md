# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AutoTeam is a self-hosted AI development team that runs as containerized Claude Code agents on Linux. It watches a GitHub repo via webhooks (or polling), and when you create an issue with the `architect` label, agents cascade through an iterative pipeline: Architect plans → Frontend/Backend Devs code → QA + Security review → devs fix feedback → loop until approved → Architect merges.

## Commands

All commands run from `agent-team/`:

```bash
./setup.sh          # First-time setup (Docker, labels, images, systemd, start)
make build          # Rebuild all Docker images after Dockerfile changes
make up / make down # Start/stop (uses systemd if installed, else docker compose)
make logs           # Tail daemon output
make status         # Show running services and agent containers
make tunnel-url     # Print the Cloudflare tunnel webhook URL
make health         # Hit daemon health endpoint
make init-repo      # Re-clone target repo into cache volume
make test-architect # Spawn architect interactively (for testing)
make test-webhook   # Send test ping to daemon
make clean          # Remove all containers, volumes, temp files
make install/uninstall # Manage systemd service
```

## Architecture

**Daemon** (`daemon/daemon.py`): Python HTTP server (or poller) that receives GitHub webhook events and spawns agent containers via the Docker API. Two modes controlled by `MODE` env var: `webhook` (preferred, uses Cloudflare tunnel) or `poll` (fallback).

**Event flow**:
- `issue.labeled "architect"` → spawns Architect
- `issue.labeled "frontend-dev"` → spawns Frontend Dev (up to `MAX_FRONTEND_AGENTS`)
- `issue.labeled "backend-dev"` → spawns Backend Dev (up to `MAX_BACKEND_AGENTS`)
- `pull_request.opened/synchronize` → spawns both QA and Security in parallel
- `pull_request.labeled "needs-fixes"` → parses branch prefix (`frontend/` or `backend/`), fetches review feedback, re-spawns the right dev
- `pull_request_review.submitted` with approval → checks if both QA and Security approved → spawns Architect to merge

**Iterative review loop**: QA and Security can request changes and label the PR `needs-fixes`. The daemon re-spawns the appropriate dev with review feedback context. After the dev pushes fixes, `synchronize` triggers QA + Security again. Loop continues until both approve or `MAX_FIX_ITERATIONS` (default 3) is hit.

**Agent containers**: All built on `agent-base` (Dockerfile.base: node:20 + git + gh CLI + Claude Code CLI). Each role has its own Dockerfile layer and system prompt (`prompts/*.md`). The shared entrypoint (`scripts/agent-entrypoint.sh`) handles git auth, repo setup (worktrees for dev/qa/security, copies for architect), and invokes `claude --print`.

**Branch conventions**: Frontend devs create `frontend/<issue>-<slug>` branches, backend devs create `backend/<issue>-<slug>`. The daemon uses this prefix to determine which dev type to re-spawn for fixes.

**State management**: The daemon tracks processed events in-memory (`DaemonState.processed` set), monitors containers in background threads, and tracks fix iteration counts per PR. Agent logs are saved to the `agent-logs` volume. The repo cache volume is refreshed every 5 minutes.

**Resource limits** are defined in `RESOURCE_PROFILES` in daemon.py (e.g., devs get 3 CPUs / 6GB RAM).

## Key Files

- `agent-team/.env` — credentials and config (copy from `.env.example`)
- `agent-team/daemon/daemon.py` — the orchestrator; all event routing, container spawning, and review feedback logic
- `agent-team/scripts/agent-entrypoint.sh` — shared entrypoint for all agent containers
- `agent-team/prompts/*.md` — system prompts defining each agent's behavior (architect, frontend-dev, backend-dev, qa, security)
- `agent-team/images/Dockerfile.*` — container definitions; customize `Dockerfile.frontend-dev` and `Dockerfile.backend-dev` for your stack

## GitHub Labels

Pipeline labels: `architect`, `architect-in-progress`, `frontend-dev`, `backend-dev`, `dev-in-progress`, `needs-fixes`

## Agents

| Agent | Trigger | Branch prefix | Purpose |
|-------|---------|--------------|---------|
| Architect | `architect` label on issue | — | Plans work, creates sub-issues, merges approved PRs |
| Frontend Dev | `frontend-dev` label on issue | `frontend/` | Frontend implementation |
| Backend Dev | `backend-dev` label on issue | `backend/` | Backend implementation |
| QA | PR opened/updated | — | Functional review, test execution |
| Security | PR opened/updated | — | Security review, SAST scans |
