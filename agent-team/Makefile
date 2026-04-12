.PHONY: build up down logs status clean init-repo setup install uninstall

# ─── First time (does everything) ────────────────────
setup:
	@chmod +x setup.sh
	@./setup.sh

# ─── Build ───────────────────────────────────────────
build:
	@echo "Building base image..."
	docker build -t agent-base:latest -f images/Dockerfile.base .
	@echo "Building agent images..."
	docker build -t agent-architect:latest -f images/Dockerfile.architect .
	docker build -t agent-frontend-dev:latest -f images/Dockerfile.frontend-dev .
	docker build -t agent-backend-dev:latest -f images/Dockerfile.backend-dev .
	docker build -t agent-qa:latest -f images/Dockerfile.qa .
	docker build -t agent-security:latest -f images/Dockerfile.security .
	@echo "Building daemon..."
	docker compose build daemon
	@echo "✓ All images built"

# ─── Run ─────────────────────────────────────────────
up:
	@if systemctl is-enabled agent-team.service &>/dev/null; then \
		sudo systemctl start agent-team.service; \
	else \
		docker compose up -d; \
	fi
	@echo "✓ Daemon running"
	@echo "  Run 'make logs' to watch the daemon"

down:
	@if systemctl is-enabled agent-team.service &>/dev/null; then \
		sudo systemctl stop agent-team.service; \
	else \
		docker compose down; \
	fi

restart:
	@if systemctl is-enabled agent-team.service &>/dev/null; then \
		sudo systemctl restart agent-team.service; \
	else \
		docker compose restart; \
	fi

# ─── Systemd (auto-start on boot) ───────────────────
install:
	@echo "Installing systemd service..."
	@sudo cp agent-team.service /etc/systemd/system/agent-team.service
	@sudo sed -i 's|%WORK_DIR%|$(shell pwd)|g' /etc/systemd/system/agent-team.service
	@sudo sed -i 's|%USER%|$(shell whoami)|g' /etc/systemd/system/agent-team.service
	@sudo systemctl daemon-reload
	@sudo systemctl enable agent-team.service
	@echo "✓ Installed — starts on boot. Run 'make up' to start now."

uninstall:
	@sudo systemctl stop agent-team.service 2>/dev/null || true
	@sudo systemctl disable agent-team.service 2>/dev/null || true
	@sudo rm -f /etc/systemd/system/agent-team.service
	@sudo systemctl daemon-reload
	@echo "✓ Systemd service removed"

# ─── Observe ─────────────────────────────────────────
logs:
	docker compose logs -f daemon

logs-all:
	docker compose logs -f

status:
	@echo "═══ Services ═══"
	@docker compose ps
	@echo ""
	@echo "═══ Agent Containers ═══"
	@docker ps --filter "name=agent-" --format "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}" 2>/dev/null || echo "None running"

health:
	@curl -s http://localhost:$${WEBHOOK_PORT:-9876}/ | python3 -m json.tool 2>/dev/null || echo "Daemon not responding"

# ─── Testing ─────────────────────────────────────────
test-architect:
	@echo '{"action":"plan_feature","issue_number":1,"issue_title":"Test","issue_body":"This is a test task"}' > /tmp/test-task.json
	docker run --rm -it \
		-e ANTHROPIC_API_KEY=$$(grep ANTHROPIC_API_KEY .env | cut -d= -f2) \
		-e GITHUB_TOKEN=$$(grep GITHUB_TOKEN .env | cut -d= -f2) \
		-e GITHUB_REPO=$$(grep GITHUB_REPO .env | cut -d= -f2) \
		-v agent-team_repo-cache:/repo:ro \
		-v /tmp/test-task.json:/tmp/task/task.json:ro \
		agent-architect:latest \
		/tmp/task/task.json

test-webhook:
	@echo "Sending test issue.labeled event to localhost..."
	curl -X POST http://localhost:$${WEBHOOK_PORT:-9876}/ \
		-H "Content-Type: application/json" \
		-H "X-GitHub-Event: ping" \
		-d '{"zen": "test", "repository": {"full_name": "test/test"}}'

# ─── Cleanup ─────────────────────────────────────────
clean:
	@if systemctl is-enabled agent-team.service &>/dev/null; then \
		sudo systemctl stop agent-team.service; \
	fi
	docker compose down -v 2>/dev/null || true
	docker rm -f $$(docker ps -a --filter "name=agent-" -q) 2>/dev/null || true
	docker volume rm agent-team_repo-cache agent-team_agent-logs 2>/dev/null || true
	rm -rf /tmp/agent-tasks
	@echo "✓ Cleaned up (systemd service still installed — run 'make uninstall' to remove)"

# ─── Init repo cache ────────────────────────────────
init-repo:
	@docker volume create agent-team_repo-cache 2>/dev/null || true
	docker run --rm \
		-e GITHUB_TOKEN=$$(grep GITHUB_TOKEN .env | cut -d= -f2) \
		-e GITHUB_REPO=$$(grep GITHUB_REPO .env | cut -d= -f2) \
		-v agent-team_repo-cache:/repo \
		--entrypoint bash \
		agent-base:latest \
		-c 'if [ -d /repo/.git ]; then cd /repo && git fetch origin && git reset --hard origin/HEAD; else git clone https://x-access-token:$$GITHUB_TOKEN@github.com/$$GITHUB_REPO.git /repo; fi'
	@echo "✓ Repo cached"
