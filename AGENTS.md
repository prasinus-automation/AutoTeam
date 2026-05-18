# AGENTS.md — AutoTeam

Single source of truth for agents working on this repo. Read this first.

## What this repo is

A self-hosted, multi-tenant orchestrator that runs Claude Code agents as Docker containers in response to GitHub webhook events. One daemon container per project. The daemon receives webhook events (or polls), parses them, and spawns agent containers via the Docker API.

This repo is **its own customer**: issues are filed against this repo, and the same agent pipeline plans and implements them. Keep that in mind — bugs you introduce will bite you on the next run.

## Tech stack (exact versions)

- **Daemon**: Python **3.12-slim** (see `agent-team/daemon/Dockerfile`)
- **Daemon dependencies** (`agent-team/daemon/Dockerfile`):
  - `requests` (HTTP to GitHub API)
  - `docker` (Docker SDK for spawning agent containers)
  - `python-dotenv` (env loading)
  - Stdlib only otherwise (`http.server`, `hmac`, `re`, `threading`, `logging`, `datetime`, etc.)
- **Agent base image**: `node:20` + git + `gh` CLI + Claude Code CLI (see `agent-team/images/Dockerfile.base`)
- **Runtime**: Docker + Docker Compose v2 (`docker compose`, not `docker-compose`)
- **GitHub CLI**: `gh` is the only sanctioned way to interact with GitHub from inside agents (it picks up `GH_TOKEN` automatically)

## Project structure

```
agent-team/
├── daemon/
│   ├── daemon.py           # the orchestrator — all routing, container spawning, recovery logic
│   └── Dockerfile          # python:3.12-slim + requests/docker/dotenv
├── images/
│   ├── Dockerfile.base             # shared base for all agent workers
│   ├── Dockerfile.architect
│   ├── Dockerfile.architect-merger
│   ├── Dockerfile.frontend-dev
│   ├── Dockerfile.backend-dev
│   ├── Dockerfile.fullstack-dev
│   ├── Dockerfile.qa
│   └── Dockerfile.security
├── prompts/                # system prompts — define each agent's behavior
│   ├── architect.md
│   ├── architect-merger.md
│   ├── frontend-dev.md
│   ├── backend-dev.md
│   ├── fullstack-dev.md
│   ├── qa.md
│   └── security.md
├── scripts/
│   └── agent-entrypoint.sh # shared entrypoint — git auth, worktree setup, invokes `claude --print`
├── projects/               # per-tenant config: one subdirectory per deployed project
│   └── <name>/.env         # COMPOSE_PROJECT_NAME, WEBHOOK_PORT, GITHUB_REPO, WEBHOOK_SECRET, ...
├── docker-compose.yml
├── new-project.sh          # bootstrap a new project's directory + .env + Makefile
└── .env.example            # template of all config keys
```

`CLAUDE.md` at repo root has additional deployment/topology notes.

## Build / run / test

### Rebuilding after editing `daemon/daemon.py`
The daemon image is **per-project** — every project's daemon must be rebuilt and force-recreated:
```bash
cd agent-team
for p in projects/*/; do
  docker compose --env-file "$p.env" build daemon
  docker compose --env-file "$p.env" up -d --force-recreate daemon
done
```

### Rebuilding after editing `prompts/*.md` or an agent `Dockerfile.*`
Agent worker images are **shared globally** as `agent-<role>:latest`. Build once:
```bash
cd agent-team
docker compose --profile build-only build agent-<role>
# e.g. agent-architect, agent-frontend-dev, agent-backend-dev, agent-fullstack-dev, agent-qa, agent-security
```
All daemons pick it up on the next spawn — no daemon recreate needed.

### Per-project daemon ops
```bash
docker logs -f <name>-daemon                              # tail logs
curl http://localhost:<port>/                             # health check
# pause / resume / retroactive sweep (HMAC-signed):
SECRET=$(grep ^WEBHOOK_SECRET= projects/<name>/.env | cut -d= -f2-)
SIG="sha256=$(printf '{}' | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')"
curl -X POST -H "X-Hub-Signature-256: $SIG" -d '{}' http://localhost:<port>/pause
curl -X POST -H "X-Hub-Signature-256: $SIG" -d '{}' http://localhost:<port>/resume
SIG="sha256=$(printf '' | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')"
curl -X POST -H "X-Hub-Signature-256: $SIG" http://localhost:<port>/sweep-blocked
```

### Tests
There is **no test suite** in this repo today. Validation is by:
1. Running `python3 -c "import ast; ast.parse(open('agent-team/daemon/daemon.py').read())"` to catch syntax errors before rebuild.
2. Smoke testing in a project: rebuild the daemon, watch `docker logs -f <name>-daemon`, fire a GitHub event, verify the right container spawns.
3. Inspecting daemon log output for the expected `log.info(...)` lines.

If you add tests, prefer `pytest` (not yet a dep) and keep them in `agent-team/daemon/tests/`.

## Conventions

### Daemon code (`daemon.py`) — this file is large, follow these rules
- **One file by design.** Do not split into modules unless explicitly asked — the deploy story (per-project Dockerfile COPY) assumes a single file.
- **Logging**: use the module-level `log` logger. `log.info` for normal flow, `log.warning` for soft failures, `log.error` for things that need human attention.
- **No new third-party dependencies** without updating `agent-team/daemon/Dockerfile` and noting why in the PR. The current set (`requests`, `docker`, `python-dotenv`) is intentionally minimal.
- **GitHub API access**: always go through the `gh_get`, `gh_post`, `gh_add_label`, `gh_remove_label`, `gh_comment` helpers — never raw `requests.get(...)`. They handle auth, retries, and rate-limit logging consistently.
- **State**: all mutable state lives on the `DaemonState` (the `state` singleton). Use `state.lock` for any mutation. The `processed` set is keyed by `f"{role}-{number}"` or `f"architect-retriage-{number}"` — be careful to `state.clear_handled(key)` on failure paths or you'll silently drop subsequent attempts within the same daemon lifetime.
- **Webhook handlers** dispatch quickly and spawn work in background threads — never block the HTTP response.

### Agent prompts (`prompts/*.md`)
- Prompts are loaded verbatim as `--system-prompt` for `claude --print`. They double as Markdown docs for humans, so keep them readable.
- The architect prompt has **two actions**: `plan_feature` and `re_triage_unblocked`. Don't conflate them.
- Dev prompts (`frontend-dev.md`, `backend-dev.md`, `fullstack-dev.md`) detect `Depends on #N` / `Blocked by #N` / `After #N` in issue bodies and add the `blocked` label. **The daemon does not add `blocked`.** The daemon only strips it (in `_try_unblock_issue`).

### GitHub label model (`agent-team/prompts/...`, `daemon.py`)
- Pipeline labels: `architect`, `architect-in-progress`, `frontend-dev`, `backend-dev`, `fullstack-dev`, `dev-in-progress`, `needs-fixes`, `blocked`, `needs-attention`.
- `architect-in-progress` and `dev-in-progress` are **single-writer per issue** — the agent that's running owns it. Recovery sweeps (in `poll_github`) re-label them if no container is running.
- `blocked` is automatic; `needs-attention` is manual triage that the daemon never touches.

### Dependency parsing — regex contract
The daemon parses these phrases (case-insensitive) from **issue bodies**:
```
(?:depends on|blocked by|after)\s+#(\d+)
```
And these from **PR bodies**:
```
(?:closes|fixes|resolves|part of)\s+#(\d+)
```
Currently only same-repo bare-number refs (`#123`) match. Full-URL refs, cross-repo `owner/repo#N` refs, and other keywords (`implements`, `completes`) do **not** match — if you change this contract, update all four call sites: `_try_unblock_issue`, `_unblock_dependents_of`, `dispatch_dependents`, and the dev prompts.

### Branch naming
Devs create branches with role-prefixes: `frontend/<issue>-<slug>`, `backend/<issue>-<slug>`, `fullstack/<issue>-<slug>`. The daemon uses the prefix to decide which dev to re-spawn for `needs-fixes`.

### Commit / PR style
Commits follow conventional-commit-ish prefixes used in `git log`: `fix:`, `feat:`, `docs:`, `chore:`, `refactor:`. Short imperative subject. PR bodies should include `Closes #N` for every issue the PR closes — the daemon's unblock pipeline relies on this keyword (see "Dependency parsing — regex contract" above).

## Gotchas

- **Don't enable `agent-team.service`.** The legacy single-tenant systemd unit conflicts with per-project daemons and reaps live worker containers on stop. Use the per-project `docker compose --env-file projects/<name>/.env up -d daemon` flow instead.
- **In-memory state**: `DaemonState.processed`, `active_containers`, `retry_backoff_until` all reset on daemon restart. Anything that should survive restarts must be reconstructed from GitHub labels (the source of truth) by `poll_github`'s recovery sweeps.
- **Architect re-triage state key (`architect-retriage-{N}`) is never explicitly cleared on success.** A second unblock event for the same issue within one daemon lifetime is silently dropped. If you add new event paths that can trigger re-triage, audit `state.clear_handled(...)` carefully.
- **Webhook events vs polling**: in `MODE=webhook`, the periodic `poll_github` still runs every ~5 minutes as a safety net (`webhook_retry_loop`). Anything you add to `poll_github` runs in both modes; anything in webhook handlers only runs in webhook mode.
- **Agent containers share a `GH_TOKEN`**, so `gh pr review --approve` events appear from the same user that opened the PR — formal review approval events don't fire. Agents communicate approval via comment headers parsed in `daemon.py`. Don't try to use review events for cross-agent signaling.
- **`gh issue edit --remove-label X` is a no-op if the label isn't present.** Don't rely on it raising. Check labels first if you need conditional logic.
- **HMAC verification** is required on all `POST` endpoints (`/pause`, `/resume`, `/sweep-blocked`). Use the `WEBHOOK_SECRET` from the project's `.env`.
