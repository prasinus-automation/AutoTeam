# Agent Team

AI dev team running as containerized Claude Code agents on your Linux box. Create a GitHub issue from your phone, the Architect plans the work, Frontend/Backend Devs write code, QA and Security review, devs fix feedback, and the loop continues until everything passes. All automated.

## Setup (one command)

```bash
git clone <this-repo> && cd agent-team
# Edit .env.example first, then:
./setup.sh
```

The setup script:
- Installs Docker if needed, adds you to the docker group
- Creates GitHub labels on your repo (`architect`, `frontend-dev`, etc.)
- Builds all Docker images
- Clones your target repo into a cache volume
- Installs a systemd service (auto-starts on boot)
- Starts everything and prints your webhook URL

## Connect GitHub

After setup, you'll see a Cloudflare tunnel URL. Add it as a webhook:

**Repo → Settings → Webhooks → Add webhook**

| Field | Value |
|-------|-------|
| Payload URL | `https://xxx.trycloudflare.com` (from setup output) |
| Content type | `application/json` |
| Secret | Value of `WEBHOOK_SECRET` in your `.env` |
| Events | **Issues**, **Pull requests**, **Issue comments** |

## Usage

1. Open your repo on GitHub (phone works great)
2. Create an issue, add the `architect` label
3. Describe what you want built
4. Architect plans the work and creates sub-issues automatically
5. Agents cascade: Frontend/Backend Devs write code → QA + Security review in parallel → Devs fix feedback → loop until approved → Architect merges
6. Issues queue and run one at a time per dev type — the next starts when the current one finishes

## Commands

```bash
make up              # start
make down            # stop
make restart         # restart daemon
make logs            # tail daemon output
make tunnel-url      # print webhook URL
make status          # show running agent containers
make health          # hit daemon health endpoint

make build           # rebuild images after Dockerfile changes
make init-repo       # re-clone target repo into cache
make test-architect  # manually spawn architect (interactive)
make test-webhook    # send test ping to daemon

make install         # install systemd service (auto-start on boot)
make uninstall       # remove systemd service
make clean           # nuke everything (containers, volumes)
```

## Systemd

The setup script installs a systemd service. Your agent team survives reboots.

```bash
sudo systemctl status agent-team    # check status
sudo journalctl -u agent-team -f    # system-level logs
sudo systemctl stop agent-team      # stop
sudo systemctl start agent-team     # start
```

## How it works

```
You (GitHub issue, label: architect)
  │ webhook
  ▼
Architect ──→ reads codebase, proposes plan
  │           you approve, it creates sub-issues
  │           labeled "frontend-dev" or "backend-dev"
  │ webhook (issue labeled)
  ▼
Frontend Dev / Backend Dev ──→ branch, code, test, open PR
  │ webhook (PR opened)
  ▼
QA + Security ──→ run in parallel (tests, code review, SAST scans)
  │    fail → label "needs-fixes" → Dev re-spawned with feedback
  │    pass → both post approval comments
  │ webhook (issue comment)
  ▼
Architect ──→ merges the PR
  │
  ▼
You get notified — feature complete
```

Each agent is its own Docker container with resource limits. The daemon watches GitHub webhooks and spawns them on demand.

## Repo structure

```
agent-team/
├── setup.sh                # One-command Linux setup
├── agent-team.service      # Systemd unit file
├── docker-compose.yml      # Daemon + tunnel + image builds
├── Makefile
├── .env.example
│
├── daemon/
│   ├── Dockerfile
│   └── daemon.py           # Webhook server + container orchestrator
│
├── images/
│   ├── Dockerfile.base     # Claude Code + git + gh CLI
│   ├── Dockerfile.architect
│   ├── Dockerfile.frontend-dev  # + frontend tools (customize for your stack)
│   ├── Dockerfile.backend-dev   # + backend tools (customize for your stack)
│   ├── Dockerfile.qa            # + test runners
│   └── Dockerfile.security      # + SAST tools
│
├── prompts/                # System prompts — the soul of each agent
│   ├── architect.md
│   ├── frontend-dev.md
│   ├── backend-dev.md
│   ├── qa.md
│   └── security.md
│
└── scripts/
    └── agent-entrypoint.sh # Git setup → claude-code invocation
```

## Resource usage on your box

| Container | CPU limit | RAM limit | When |
|-----------|-----------|-----------|------|
| Daemon | 0.1 | 256 MB | Always |
| Architect | 2 cores | 4 GB | Per issue |
| Frontend Dev | 3 cores | 6 GB | Per issue |
| Backend Dev | 3 cores | 6 GB | Per issue |
| QA | 3 cores | 6 GB | Per PR |
| Security | 3 cores | 6 GB | Per PR |
| **Peak** | **~17 cores** | **~34 GB** | Rare |
| **Idle** | **~0.1 cores** | **~256 MB** | Most of the time |

Claude Code is mostly waiting on API calls — actual CPU usage is well under these limits.

## Customizing

**For your stack**: Edit `images/Dockerfile.frontend-dev` and `images/Dockerfile.backend-dev` to include your project's build tools. Uncomment the Rust/Go/etc sections or add your own.

**Agent behavior**: Edit the prompts in `prompts/`. These define how each agent works — what it does, what tools it uses, what it never does.

**Scaling**: Change `MAX_FRONTEND_AGENTS` and `MAX_BACKEND_AGENTS` in `.env` to allow more parallel developers.

**Review loop**: Change `MAX_FIX_ITERATIONS` in `.env` to control how many review/fix cycles are allowed before requesting human intervention (default: 3).

## Cost

- **Compute**: $0 (your box)
- **Tunnel**: $0 (Cloudflare free)
- **API**: $30–200/week depending on how much work you throw at it
