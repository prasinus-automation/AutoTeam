# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AutoTeam is a self-hosted AI development team that runs as containerized Claude Code agents on Linux. It watches a GitHub repo via webhooks (or polling), and when you create an issue with the `architect` label, agents cascade through an iterative pipeline: Architect plans → Frontend/Backend Devs code → QA + Security review → devs fix feedback → loop until approved → Architect merges.

## Deployment topology

**AutoTeam runs multi-tenant: one daemon container per project.** Per-project config lives at `agent-team/projects/<name>/.env` (defines `COMPOSE_PROJECT_NAME`, `WEBHOOK_PORT`, `GITHUB_REPO`, `WEBHOOK_SECRET`, etc.). To enumerate projects: `ls agent-team/projects/`. Each daemon container is named `<name>-daemon` and listens on its own `WEBHOOK_PORT`.

### Image scoping (important when rolling out changes)

- **Daemon image is per-project** — `docker compose --env-file projects/<name>/.env build daemon` produces `<name>-daemon:latest` with `daemon.py` baked in. After editing `daemon/daemon.py`, you must rebuild for **every** project and force-recreate every daemon container, otherwise some run old code.
- **Agent worker images** (architect, architect-merger, frontend-dev, backend-dev, fullstack-dev, qa, security) are **shared globally** as `agent-<role>:latest`. After editing a prompt (`prompts/*.md`) or worker Dockerfile, rebuild that one image once — all daemons pick it up on the next spawn. No daemon recreate needed.

### Legacy `agent-team.service` systemd unit — do NOT enable

`/etc/systemd/system/agent-team.service` is from the original single-tenant era. It runs `docker compose up` from the default project name (which conflicts with `chickensite` on port 9876), and its `ExecStopPost=docker rm -f $(docker ps -a --filter "name=agent-" -q)` reaps every `agent-<role>-...` worker container the per-project daemons spawn. If it's ever `enabled`/`active`, stop and disable it. The legacy single-tenant `./setup.sh`, `make up/down`, `make install/uninstall` flow in `agent-team/Makefile` references this unit — don't use those targets.

## Commands

All commands run from `agent-team/`.

### Per-project daemon ops
```bash
# Restart one project's daemon (picks up rebuilt image)
docker compose --env-file projects/<name>/.env up -d --force-recreate daemon

# Stop / start
docker compose --env-file projects/<name>/.env stop daemon
docker compose --env-file projects/<name>/.env start daemon

# Tail logs
docker logs -f <name>-daemon

# Health check (port from projects/<name>/.env)
curl http://localhost:<port>/

# Pause/resume (refuses new spawns; in-flight agents continue)
SECRET=$(grep ^WEBHOOK_SECRET= projects/<name>/.env | cut -d= -f2-)
SIG="sha256=$(printf '{}' | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')"
curl -X POST -H "X-Hub-Signature-256: $SIG" -d '{}' http://localhost:<port>/pause   # or /resume

# Retroactive sweep: scan all `blocked` issues, route resolved ones through architect re-triage
SIG="sha256=$(printf '' | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')"
curl -X POST -H "X-Hub-Signature-256: $SIG" http://localhost:<port>/sweep-blocked
```

### Rebuilding images
```bash
# After editing daemon/daemon.py — rebuild per-project and recreate each daemon
for p in projects/*/; do
  docker compose --env-file "$p.env" build daemon
  docker compose --env-file "$p.env" up -d --force-recreate daemon
done

# After editing a worker prompt or Dockerfile — rebuild once globally
docker compose --profile build-only build agent-<role>   # e.g. agent-architect, agent-frontend-dev
```

## Architecture

**Daemon** (`daemon/daemon.py`): Python HTTP server (or poller) that receives GitHub webhook events and spawns agent containers via the Docker API. Two modes controlled by `MODE` env var: `webhook` (preferred, uses Cloudflare tunnel) or `poll` (fallback).

**Event flow**:
- `issue.labeled "architect"` → spawns Architect (action: `plan_feature`)
- `issue.labeled "frontend-dev" / "backend-dev" / "fullstack-dev"` → spawns the matching dev (up to `MAX_<ROLE>_AGENTS`; excess queues)
- `pull_request.opened/synchronize` → spawns QA. Security runs **after** QA approves, not in parallel. Approval is detected from comment headers (agents share a token, so formal PR approvals don't fire reviewer events).
- `pull_request.labeled "needs-fixes"` → parses branch prefix (`frontend/`, `backend/`, `fullstack/`), fetches review feedback, re-spawns the matching dev
- Both QA and Security approved → spawns Architect-Merger
- `pull_request.closed` (merged) → `dispatch_dependents` calls `_unblock_dependents_of(closed_issues_from_PR_body)` → for each open issue whose `Depends on #N` / `Blocked by #N` / `After #N` deps are all closed: strip `blocked`, post unblock comment, spawn Architect with action `re_triage_unblocked`
- `issues.closed` (manual close, no PR) → same unblock path via `_unblock_dependents_of({issue_number})`

**Iterative review loop**: QA and Security can request changes and label the PR `needs-fixes`. The daemon re-spawns the appropriate dev with review feedback context. After the dev pushes fixes, `synchronize` triggers QA again (then Security after QA approves). Loop continues until both approve or `MAX_FIX_ITERATIONS` (default 3) is hit.

**Unblock + re-triage**: When deps close, the daemon does NOT dispatch the dev directly — it routes the unblocked issue through the architect with action `re_triage_unblocked`. The architect reads current `main` against the issue body and decides one of three outcomes: close as stale (with a cited reason), re-label with the right dev role (still valid), or edit the issue body and then re-label (revised). This guards against old issues producing stale PRs once their blockers finally land. See the "Re-triage" section of `prompts/architect.md`.

**Agent containers**: All built on `agent-base` (Dockerfile.base: node:20 + git + gh CLI + Claude Code CLI). Each role has its own Dockerfile layer and system prompt (`prompts/*.md`). The shared entrypoint (`scripts/agent-entrypoint.sh`) handles git auth, repo setup (worktrees for dev/qa/security, copies for architect), and invokes `claude --print`.

**Branch conventions**: Frontend devs create `frontend/<issue>-<slug>` branches, backend devs create `backend/<issue>-<slug>`, fullstack devs create `fullstack/<issue>-<slug>`. The daemon uses this prefix to determine which dev type to re-spawn for fixes.

**State management**: The daemon tracks processed events in-memory (`DaemonState.processed` set), monitors containers in background threads, and tracks fix iteration counts per PR. Agent logs are saved to the `agent-logs` volume. The repo cache volume is refreshed every 5 minutes.

**Resource limits** are defined in `RESOURCE_PROFILES` in daemon.py (e.g., devs get 3 CPUs / 6GB RAM).

## Key Files

- `agent-team/projects/<name>/.env` — per-project credentials and config (one directory per deployed project)
- `agent-team/projects/<name>/Makefile` — per-project convenience targets generated by `new-project.sh`
- `agent-team/new-project.sh` — bootstrap a new project's directory + .env + Makefile
- `agent-team/.env.example` — template showing all available config keys
- `agent-team/daemon/daemon.py` — the orchestrator; all event routing, container spawning, and review feedback logic
- `agent-team/scripts/agent-entrypoint.sh` — shared entrypoint for all agent containers (worktree setup, branches off `origin/main`, invokes `claude --print`)
- `agent-team/prompts/*.md` — system prompts defining each agent's behavior (architect, architect-merger, frontend-dev, backend-dev, fullstack-dev, qa, security)
- `agent-team/images/Dockerfile.*` — container definitions; customize `Dockerfile.frontend-dev`, `Dockerfile.backend-dev`, `Dockerfile.fullstack-dev` for your stack

## GitHub Labels

Pipeline labels: `architect`, `architect-in-progress`, `frontend-dev`, `backend-dev`, `fullstack-dev`, `dev-in-progress`, `needs-fixes`, `blocked`, `needs-attention`

- `blocked` — set by a dev when it detects `Depends on #N` with #N still open; stripped automatically when all deps close (then the architect re-triages).
- `needs-attention` — manual triage flag; the daemon refuses to dispatch any agent while this is set.

## Agents

| Agent | Trigger | Branch prefix | Purpose |
|-------|---------|--------------|---------|
| Architect | `architect` label on issue (`plan_feature`); or unblock-event (`re_triage_unblocked`) | — | Plans work and creates sub-issues; on re-triage decides stale/valid/revised for unblocked issues |
| Architect-Merger | both QA + Security approved | — | Final sanity check, merges approved PRs |
| Frontend Dev | `frontend-dev` label on issue | `frontend/` | Frontend implementation |
| Backend Dev | `backend-dev` label on issue | `backend/` | Backend implementation |
| Fullstack Dev | `fullstack-dev` label on issue | `fullstack/` | Cross-cutting features in projects without a clean FE/BE split |
| QA | PR opened/updated | — | Functional review, test execution (runs first) |
| Security | After QA approves | — | Security review, SAST scans (runs after QA) |
