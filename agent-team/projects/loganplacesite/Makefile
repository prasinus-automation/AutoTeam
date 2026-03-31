# Auto-generated project Makefile — delegates to the main agent-team setup
AGENT_TEAM_DIR := $(shell dirname $(realpath $(lastword $(MAKEFILE_LIST))))/../..

include .env
export

COMPOSE := docker compose -f $(AGENT_TEAM_DIR)/docker-compose.yml -p $(COMPOSE_PROJECT_NAME) --env-file $(CURDIR)/.env

.PHONY: up down logs status tunnel-url health build init-repo clean

up:
	$(COMPOSE) build daemon
	$(COMPOSE) up -d daemon tunnel
	@sleep 3
	@echo ""
	@echo "  $(COMPOSE_PROJECT_NAME) is running!"
	@echo "  Repo: $(GITHUB_REPO)"
	@$(MAKE) --no-print-directory tunnel-url

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f daemon

status:
	@docker ps --filter "name=$(COMPOSE_PROJECT_NAME)" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

tunnel-url:
	@docker logs $(COMPOSE_PROJECT_NAME)-tunnel 2>&1 | grep -o 'https://.*trycloudflare.com' | tail -1

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
