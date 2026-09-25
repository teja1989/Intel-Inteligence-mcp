# Telco MCP learning lab. Run `make` (or `make help`) to list targets.
# Requires: uv (https://docs.astral.sh/uv/), GNU make, and Node 20+ for MCP Inspector (Phase 2).

SHELL := /bin/bash
.DEFAULT_GOAL := help
UV    ?= uv
RUN   := $(UV) run

# Load .env for recipes that need values (chaos/curl). Values stay in the shell, never echoed.
LOAD_ENV := set -a; [ -f .env ] && . ./.env; set +a
MOCK_URL  = http://$${MOCK_HOST:-127.0.0.1}:$${MOCK_PORT:-8081}

##@ Setup
.PHONY: help setup env
help: ## Show this help
	@awk 'BEGIN{FS=":.*##"; printf "\nUsage: make <target>\n"} \
	  /^##@/{printf "\n\033[1m%s\033[0m\n", substr($$0,5)} \
	  /^[a-zA-Z_-]+:.*##/{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Install Python 3.12 + pinned dependencies (uv.lock) into .venv
	$(UV) sync --locked

env: ## Create .env from .env.example with a freshly generated MOCK_API_KEY (won't overwrite)
	@if [ -f .env ]; then echo ".env already exists; leaving it alone."; else \
	  key=$$($(RUN) python -c 'import secrets; print(secrets.token_urlsafe(32))'); \
	  sed "s|^MOCK_API_KEY=.*|MOCK_API_KEY=$$key|" .env.example > .env; \
	  chmod 600 .env; echo "Created .env (mode 600) with a random MOCK_API_KEY."; fi

##@ Run (each in its own terminal)
.PHONY: mocks
mocks: ## Start the mock telecom backend on 127.0.0.1:8081 (OpenAPI UI at /docs)
	$(RUN) python -m telco_mcp_lab.mock_apis

##@ Chaos switch (mock backend must be running)
.PHONY: chaos-slow chaos-fail chaos-off chaos-status
chaos-slow: ## Make every backend call take 5 s (timeout testing)
	@$(LOAD_ENV); curl -sf -X POST "$(MOCK_URL)/_admin/chaos" -H "X-Api-Key: $$MOCK_API_KEY" \
	  -H 'content-type: application/json' -d '{"delay_ms":5000}'; echo
chaos-fail: ## Make every backend call fail with HTTP 503
	@$(LOAD_ENV); curl -sf -X POST "$(MOCK_URL)/_admin/chaos" -H "X-Api-Key: $$MOCK_API_KEY" \
	  -H 'content-type: application/json' -d '{"fail_rate":1.0,"fail_status":503}'; echo
chaos-off: ## Turn all chaos off
	@$(LOAD_ENV); curl -sf -X POST "$(MOCK_URL)/_admin/chaos" -H "X-Api-Key: $$MOCK_API_KEY" \
	  -H 'content-type: application/json' -d '{}'; echo
chaos-status: ## Show current chaos settings
	@$(LOAD_ENV); curl -sf "$(MOCK_URL)/_admin/chaos" -H "X-Api-Key: $$MOCK_API_KEY"; echo

##@ Test & quality
.PHONY: test test-fast test-mocks test-security smoke lint fmt check
test: ## Run the full pytest suite
	$(RUN) pytest

test-fast: ## Run tests except the deliberately slow ones
	$(RUN) pytest -m "not slow"

test-mocks: ## Run only the mock-backend tests
	$(RUN) pytest tests/mock_apis

test-security: ## Run only security-marked tests (auth, tenant isolation, injection, synthetic data)
	$(RUN) pytest -m security -v

smoke: ## Real-HTTP walkthrough against a RUNNING mock backend (start `make mocks` first)
	$(RUN) python scripts/smoke_mocks.py

lint: ## Ruff lint + format check (includes bandit-style security rules)
	$(RUN) ruff check .
	$(RUN) ruff format --check .

fmt: ## Auto-format and apply safe lint fixes
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

check: lint test ## Everything CI would run: lint + full test suite

##@ Housekeeping
.PHONY: reset-data clean
reset-data: ## Delete the mock backend's SQLite data (orders/drafts/idempotency)
	rm -rf .data

clean: ## Remove caches and local data (keeps .env and .venv)
	rm -rf .data .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
