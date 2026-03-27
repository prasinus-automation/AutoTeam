# AutoTeam

A self-hosted AI development team that runs as containerized Claude Code agents on Linux. Point it at a GitHub repo, create an issue, and agents handle the rest: planning, coding, testing, security review, and merging.

## How it works

1. You create a GitHub issue and label it `architect`
2. The Architect agent reads the codebase, plans the work, and creates sub-issues
3. Frontend/Backend Dev agents pick up sub-issues, write code, and open PRs
4. QA and Security agents review each PR in parallel
5. If changes are needed, the dev agent is re-spawned with feedback
6. Once both reviewers approve, the Architect merges the PR
7. The next queued issue starts automatically

All agents run as isolated Docker containers on your machine. A lightweight daemon watches GitHub webhooks and orchestrates everything.

## Requirements

- Linux (tested on Ubuntu 22.04+)
- Docker
- An Anthropic API key (or Claude Max/Pro subscription)
- A GitHub token with repo access

## Quick start

```bash
cd agent-team
cp .env.example .env    # fill in your API key, GitHub token, and repo
./setup.sh              # installs Docker, builds images, starts everything
```

Setup prints a Cloudflare tunnel URL. Add it as a webhook in your GitHub repo settings (Settings > Webhooks > Add webhook) with content type `application/json` and events: Issues, Pull requests, Issue comments.

Then create an issue with the `architect` label to kick things off.

## Commands

All commands run from `agent-team/`:

```bash
make up / make down     # start/stop
make logs               # tail daemon output
make status             # show running agents
make tunnel-url         # print the webhook URL
make build              # rebuild images after changes
make clean              # remove all containers and volumes
```

## Configuration

Edit `agent-team/.env` to control:

- `MAX_FRONTEND_AGENTS` / `MAX_BACKEND_AGENTS` — parallel dev slots (default: 1 each, issues queue automatically)
- `MAX_FIX_ITERATIONS` — review/fix cycles before stopping (default: 3)
- `MODE` — `webhook` (preferred) or `poll` (fallback, no tunnel needed)

Customize `agent-team/images/Dockerfile.frontend-dev` and `Dockerfile.backend-dev` for your stack's build tools.

See [agent-team/README.md](agent-team/README.md) for full documentation.
