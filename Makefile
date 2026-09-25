# Telco MCP learning lab. Run `make` (or `make help`) to list targets.
# Requires: uv (https://docs.astral.sh/uv/), GNU make, and Node >= 22.19 for MCP Inspector 2.x.

SHELL := /bin/bash
.DEFAULT_GOAL := help
UV    ?= uv
RUN   := $(UV) run

# Load .env for recipes that need values (chaos/curl). Values stay in the shell, never echoed.
LOAD_ENV := set -a; [ -f .env ] && . ./.env; set +a
MOCK_URL  = http://$${MOCK_HOST:-127.0.0.1}:$${MOCK_PORT:-8081}

# MCP server over stdio, and the same behind the wire-tracing proxy.
INSPECTOR  := npx -y @modelcontextprotocol/inspector@2.8.0
SERVER_CMD := $(UV) run --quiet python -m telco_mcp_lab.mcp_server
TRACED_CMD := $(UV) run --quiet python scripts/stdio_trace.py $(SERVER_CMD)
ACCOUNT    ?= ACC-1001

##@ Setup
.PHONY: help setup env env-tokens
help: ## Show this help
	@awk 'BEGIN{FS=":.*##"; printf "\nUsage: make <target>\n"} \
	  /^##@/{printf "\n\033[1m%s\033[0m\n", substr($$0,5)} \
	  /^[a-zA-Z_-]+:.*##/{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Install Python 3.12 + pinned dependencies (uv.lock) into .venv
	$(UV) sync --locked

env: ## Create .env from .env.example with fresh random tokens (won't overwrite an existing .env)
	@if [ -f .env ]; then echo ".env already exists; leaving it alone (see env-tokens)."; else \
	  gen() { $(RUN) python -c 'import secrets; print(secrets.token_urlsafe(32))'; }; \
	  tok=$$(gen); \
	  sed -e "s|^MOCK_GATEWAY_TOKEN=.*|MOCK_GATEWAY_TOKEN=$$tok|" \
	      -e "s|^GATEWAY_TOKEN=.*|GATEWAY_TOKEN=$$tok|" \
	      -e "s|^MCP_TOKEN_ALICE=.*|MCP_TOKEN_ALICE=$$(gen)|" \
	      -e "s|^MCP_TOKEN_BOB=.*|MCP_TOKEN_BOB=$$(gen)|" \
	      -e "s|^MCP_TOKEN_CAROL=.*|MCP_TOKEN_CAROL=$$(gen)|" \
	      -e "s|^MCP_TOKEN_MALLORY=.*|MCP_TOKEN_MALLORY=$$(gen)|" .env.example > .env; \
	  chmod 600 .env; echo "Created .env (mode 600) with random gateway + caller tokens."; fi

env-tokens: ## Add missing MCP caller tokens to an EXISTING .env (from Phase 1/2)
	@for c in ALICE BOB CAROL MALLORY; do \
	  if ! grep -q "^MCP_TOKEN_$$c=." .env; then \
	    sed -i.bak "/^MCP_TOKEN_$$c=/d" .env; \
	    echo "MCP_TOKEN_$$c=$$($(RUN) python -c 'import secrets; print(secrets.token_urlsafe(32))')" >> .env; \
	    echo "added MCP_TOKEN_$$c"; fi; done; rm -f .env.bak

##@ Run (each in its own terminal)
.PHONY: mocks
mocks: ## Start the mock API gateway + backends on 127.0.0.1:8081 (OpenAPI UI at /docs)
	$(RUN) python -m telco_mcp_lab.mock_apis

##@ MCP server over stdio (mock gateway must be running: make mocks)
.PHONY: mcp-stdio demo-stdio demo-stdio-legacy inspector inspector-cli-list inspector-cli-call traces
mcp-stdio: ## Run the MCP server on stdio (normally a client launches it; useful for piping JSON by hand)
	$(SERVER_CMD)

demo-stdio: ## Python MCP client: 2026-07-28 protocol, list + call + error, with the raw wire trace
	$(RUN) python scripts/stdio_demo.py --trace

demo-stdio-legacy: ## Same, forcing the pre-2026 initialize handshake (what Spring AI speaks today)
	$(RUN) python scripts/stdio_demo.py --legacy --trace

inspector: ## MCP Inspector web UI on 127.0.0.1:6274, launching our server through the trace proxy
	$(INSPECTOR) $(TRACED_CMD)

inspector-cli-list: ## Inspector CLI: tools/list over stdio (ERA=legacy|auto|modern, default legacy)
	$(INSPECTOR) --cli $(TRACED_CMD) -- --method tools/list --protocol-era $${ERA:-legacy}

inspector-cli-call: ## Inspector CLI: call get_account_summary as the stdio caller (ACCOUNT=ACC-1002 / ACC-2001=refused)
	$(INSPECTOR) --cli $(TRACED_CMD) -- --method tools/call --tool-name get_account_summary \
	  --tool-arg account_id=$(ACCOUNT) --protocol-era $${ERA:-legacy}

traces: ## List captured JSON-RPC wire traces (.data/traces, gitignored, contains payloads)
	@ls -1t .data/traces/*.jsonl 2>/dev/null | head -20 || echo "No traces yet."

##@ MCP server over stateless Streamable HTTP (Phase 3; mock gateway must be running)
.PHONY: mcp-http mcp-cluster demo-http demo-injection inspector-http
mcp-http: ## Run the MCP server on http://127.0.0.1:8090/mcp (stateless, bearer auth)
	$(RUN) python -m telco_mcp_lab.mcp_server --transport http

mcp-cluster: ## 2 replicas (:8091, :8092) behind a round-robin LB on :8099 (Ctrl-C stops all)
	@trap 'kill 0' INT TERM EXIT; \
	$(RUN) python -m telco_mcp_lab.mcp_server --transport http --port 8091 $${LEGACY:+--legacy-sessions} & \
	$(RUN) python -m telco_mcp_lab.mcp_server --transport http --port 8092 $${LEGACY:+--legacy-sessions} & \
	$(RUN) python -m telco_mcp_lab.devtools.round_robin_lb --port 8099 \
	  --backend http://127.0.0.1:8091 --backend http://127.0.0.1:8092 & \
	echo "cluster: http://127.0.0.1:8099/mcp  (LEGACY=1 to see sticky-session failure)"; wait

demo-http: ## Walk through callers, scopes, tenant guard, masking over HTTP (URL=… for the cluster)
	$(RUN) python scripts/http_demo.py $${URL:+--url $$URL}

demo-injection: ## Before/after: the prompt-injection note as the model would see it
	$(RUN) python scripts/injection_demo.py

inspector-http: ## Inspector web UI; connect to http://127.0.0.1:8090/mcp with header Authorization: Bearer $$MCP_TOKEN_ALICE
	$(INSPECTOR)

##@ LLM harness: Azure OpenAI as the MCP host (Phase 5; needs mocks + mcp-http running)
.PHONY: harness-check ask chat
harness-check: ## Verify MCP + Azure connectivity (prints actionable hints on failure)
	$(RUN) python -m telco_mcp_lab.harness --check

ask: ## One question with a full step trace: make ask Q="what plans am I on?" [CALLER=bob]
	@test -n "$(Q)" || { echo 'usage: make ask Q="your question" [CALLER=alice|bob|carol|mallory]'; exit 2; }
	$(RUN) python -m telco_mcp_lab.harness $${CALLER:+--caller $$CALLER} "$(Q)"

chat: ## Interactive multi-turn chat with the trace (CALLER=bob to switch identity)
	$(RUN) python -m telco_mcp_lab.harness $${CALLER:+--caller $$CALLER}

##@ Chaos switch (mock backend must be running)
.PHONY: chaos-slow chaos-fail chaos-off chaos-status
chaos-slow: ## Make every backend call take 5 s (timeout testing)
	@$(LOAD_ENV); curl -sf -X POST "$(MOCK_URL)/_admin/chaos" -H "Authorization: Bearer $$MOCK_GATEWAY_TOKEN" \
	  -H 'content-type: application/json' -d '{"delay_ms":5000}'; echo
chaos-fail: ## Make every backend call fail with HTTP 503
	@$(LOAD_ENV); curl -sf -X POST "$(MOCK_URL)/_admin/chaos" -H "Authorization: Bearer $$MOCK_GATEWAY_TOKEN" \
	  -H 'content-type: application/json' -d '{"fail_rate":1.0,"fail_status":503}'; echo
chaos-off: ## Turn all chaos off
	@$(LOAD_ENV); curl -sf -X POST "$(MOCK_URL)/_admin/chaos" -H "Authorization: Bearer $$MOCK_GATEWAY_TOKEN" \
	  -H 'content-type: application/json' -d '{}'; echo
chaos-status: ## Show current chaos settings
	@$(LOAD_ENV); curl -sf "$(MOCK_URL)/_admin/chaos" -H "Authorization: Bearer $$MOCK_GATEWAY_TOKEN"; echo

##@ Test & quality
.PHONY: test test-fast test-mocks test-client test-harness test-protocol test-security smoke lint fmt check
test: ## Run the full pytest suite
	$(RUN) pytest

test-fast: ## Run tests except the deliberately slow ones
	$(RUN) pytest -m "not slow"

test-mocks: ## Run only the mock-gateway tests
	$(RUN) pytest tests/mock_apis

test-client: ## Run only the MCP server tests (gateway client, tools, stdio protocol)
	$(RUN) pytest tests/mcp_server

test-harness: ## Run only the harness tests (scripted LLM + mocked Azure; no network)
	$(RUN) pytest tests/harness

test-protocol: ## Run only end-to-end protocol tests (stdio subprocesses, real HTTP, LB cluster)
	$(RUN) pytest -m protocol -v

test-security: ## Run only security-marked tests (auth, tenant matrix, scopes, masking, injection, audit)
	$(RUN) pytest -m security -v

smoke: ## Real-HTTP walkthrough against the RUNNING mock gateway (start `make mocks` first)
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
reset-data: ## Delete the mock gateway's SQLite data (orders/drafts/idempotency)
	rm -rf .data

clean: ## Remove caches and local data (keeps .env and .venv)
	rm -rf .data .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
