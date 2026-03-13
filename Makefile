# EvoClaw Makefile
# Usage: make <target>

.PHONY: help build dev start stop logs clean test

IMAGE_NAME := evoclaw-agent
TAG        := latest
COMPOSE    := docker compose

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

build: ## Build the agent container image (evoclaw-agent:latest)
	bash container/build.sh $(TAG)
	@echo "✅ Built $(IMAGE_NAME):$(TAG)"

dev: build ## Build image and start in development mode (with logs)
	$(COMPOSE) up

start: build ## Build image and start in background
	$(COMPOSE) up -d
	@echo "✅ EvoClaw started. Logs: make logs"

stop: ## Stop all services
	$(COMPOSE) down
	@echo "✅ EvoClaw stopped"

restart: stop start ## Restart all services

logs: ## Tail live logs
	$(COMPOSE) logs -f

test: ## Run unit tests
	python -m pytest tests/ -v 2>/dev/null || echo "No tests found."

clean: ## Remove generated files (data, ipc, __pycache__)
	@read -p "⚠️  This will delete data/, ipc/. Continue? [y/N] " confirm; \
	if [ "$$confirm" = "y" ]; then \
		rm -rf data/ ipc/; \
		find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true; \
		echo "✅ Cleaned"; \
	else \
		echo "Cancelled"; \
	fi

db: ## Open SQLite shell on the database
	sqlite3 data/evoclaw.db
