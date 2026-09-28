# Telco MCP learning lab. Run `make` (or `make help`) to list targets.
# Requires: uv (https://docs.astral.sh/uv/), GNU make, and Node >= 22.19 for MCP Inspector 2.x.

SHELL := /bin/bash
.DEFAULT_GOAL := help
UV    ?= uv
RUN   := $(UV) run

# Preflight: every target except help/doctor needs uv. Fail with instructions, not "No such file".
ifneq ($(filter-out help doctor,$(MAKECMDGOALS)),)
ifeq ($(shell command -v $(UV) 2>/dev/null),)
$(info )
$(info uv (Python package manager) is not installed or not on PATH.)
$(if $(wildcard $(HOME)/.local/bin/uv),$(info Found $(HOME)/.local/bin/uv: open a new terminal, or run: export PATH="$$HOME/.local/bin:$$PATH"))
$(info Install one of:)
$(info   macOS:        brew install uv)
$(info   macOS/Linux:  curl -LsSf https://astral.sh/uv/install.sh | sh   (then open a new terminal))
$(info   proxy blocks astral.sh:  python3 -m pip install --user uv)
$(info Then run: make doctor && make setup)
$(info )
$(error uv not found)
endif
endif

# Load .env for recipes that need values (chaos/curl). Values stay in the shell, never echoed.
LOAD_ENV := set -a; [ -f .env ] && . ./.env; set +a
MOCK_URL  = http://$${MOCK_HOST:-127.0.0.1}:$${MOCK_PORT:-8081}

# Local default for the server's client registry (the image uses /app/config/clients.json).
# A value in .env wins (recipes that load .env); `make env-update` flags a stale one.
export MCP_CLIENTS_CONFIG ?= server/config/clients.json

# MCP server over stdio, and the same behind the wire-tracing proxy.
INSPECTOR  := npx -y @modelcontextprotocol/inspector@2.8.0
SERVER_CMD := $(UV) run --quiet python -m telco_mcp
TRACED_CMD := $(UV) run --quiet python lab/scripts/stdio_trace.py $(SERVER_CMD)
ACCOUNT    ?= ACC-1001

##@ Setup
.PHONY: help doctor setup env env-tokens env-update
help: ## Show this help
	@awk 'BEGIN{FS=":.*##"; printf "\nUsage: make <target>\n"} \
	  /^##@/{printf "\n\033[1m%s\033[0m\n", substr($$0,5)} \
	  /^[a-zA-Z_-]+:.*##/{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

doctor: ## Check prerequisites (uv, Python 3.12, Node, .env, ports, proxy) with fix hints
	@ok=1; \
	if command -v $(UV) >/dev/null 2>&1; then echo "✅ uv $$($(UV) --version | cut -d' ' -f2)"; \
	else echo "❌ uv missing: brew install uv  |  curl -LsSf https://astral.sh/uv/install.sh | sh"; ok=0; fi; \
	if command -v $(UV) >/dev/null 2>&1; then \
	  if $(UV) python find 3.12 >/dev/null 2>&1; then echo "✅ Python 3.12 ($$($(UV) python find 3.12))"; \
	  else echo "⚠️  Python 3.12 not found: make setup will download it (needs github.com via your proxy)."; \
	       echo "    If downloads are blocked: brew install python@3.12, then UV_PYTHON_DOWNLOADS=never make setup"; fi; fi; \
	if command -v node >/dev/null 2>&1; then echo "✅ node $$(node --version) (Inspector needs >= 22.19)"; \
	else echo "ℹ️  node not found: only needed for MCP Inspector (make inspector*)"; fi; \
	if [ -f .env ]; then echo "✅ .env present"; \
	  command -v $(UV) >/dev/null 2>&1 && $(UV) run --quiet python lab/scripts/env_update.py --check; \
	else echo "ℹ️  no .env yet: run make env"; fi; \
	for p in 8081 8090; do \
	  if (exec 3<>/dev/tcp/127.0.0.1/$$p) 2>/dev/null; then echo "⚠️  port $$p already in use (MOCK_PORT / MCP_PORT to change)"; \
	  else echo "✅ port $$p free"; fi; done; \
	if [ -n "$${HTTPS_PROXY}$${https_proxy}$${HTTP_PROXY}$${http_proxy}" ]; then \
	  case ",$${NO_PROXY}$${no_proxy}," in *127.0.0.1*|*localhost*) echo "✅ proxy set, localhost excluded";; \
	  *) echo "⚠️  proxy set but NO_PROXY lacks 127.0.0.1,localhost (our code bypasses it; curl/Inspector may not)";; esac; fi; \
	[ $$ok = 1 ] || exit 1

setup: ## Install Python 3.12 + pinned dependencies (uv.lock) into .venv
	$(UV) sync --locked

env: ## Create .env from .env.example with fresh random tokens (won't overwrite an existing .env)
	@if [ -f .env ]; then echo ".env already exists; leaving it alone (see env-tokens)."; else \
	  gen() { $(RUN) python -c 'import secrets; print(secrets.token_urlsafe(32))'; }; \
	  tok=$$(gen); \
	  sed -e "s|^MOCK_GATEWAY_TOKEN=.*|MOCK_GATEWAY_TOKEN=$$tok|" \
	      -e "s|^GATEWAY_TOKEN=.*|GATEWAY_TOKEN=$$tok|" \
	      -e "s|^DEV_TOKEN_SERVICE_CLIENT_SECRET=.*|DEV_TOKEN_SERVICE_CLIENT_SECRET=$$(gen)|" .env.example > .env; \
	  chmod 600 .env; echo "Created .env (mode 600) with a random gateway token + dev secret."; fi

env-update: ## Add settings that are new in .env.example to your existing .env (never overwrites)
	@$(RUN) python lab/scripts/env_update.py $${CHECK:+--check}

env-tokens: ## Add a missing dev token service secret to an EXISTING .env
	@for v in DEV_TOKEN_SERVICE_CLIENT_SECRET; do \
	  if ! grep -q "^$$v=." .env; then \
	    sed -i.bak "/^$$v=/d" .env; \
	    echo "$$v=$$($(RUN) python -c 'import secrets; print(secrets.token_urlsafe(32))')" >> .env; \
	    echo "added $$v"; fi; done; rm -f .env.bak

##@ Run (each in its own terminal)
.PHONY: mocks
mocks: ## Start the mock API gateway + backends on 127.0.0.1:8081 (OpenAPI UI at /docs)
	$(RUN) python -m telco_mcp_lab.mock_apis

##@ MCP server over stdio (mock gateway must be running: make mocks)
.PHONY: mcp-stdio demo-stdio demo-stdio-legacy inspector inspector-cli-list inspector-cli-call traces
mcp-stdio: ## Run the MCP server on stdio (normally a client launches it; useful for piping JSON by hand)
	$(SERVER_CMD)

demo-stdio: ## Python MCP client: 2026-07-28 protocol, list + call + error, with the raw wire trace
	$(RUN) python lab/scripts/stdio_demo.py --trace

demo-stdio-legacy: ## Same, forcing the pre-2026 initialize handshake (what Spring AI speaks today)
	$(RUN) python lab/scripts/stdio_demo.py --legacy --trace

inspector: ## MCP Inspector web UI on 127.0.0.1:6274, launching our server through the trace proxy
	$(INSPECTOR) $(TRACED_CMD)

inspector-cli-list: ## Inspector CLI: tools/list over stdio (ERA=legacy|auto|modern, default legacy)
	$(INSPECTOR) --cli $(TRACED_CMD) -- --method tools/list --protocol-era $${ERA:-legacy}

inspector-cli-call: ## Inspector CLI: call get_account_summary over stdio (ACCOUNT=ACC-2001, or ACC-9999 = not found)
	$(INSPECTOR) --cli $(TRACED_CMD) -- --method tools/call --tool-name get_account_summary \
	  --tool-arg account_id=$(ACCOUNT) --protocol-era $${ERA:-legacy}

traces: ## List captured JSON-RPC wire traces (.data/traces, gitignored, contains payloads)
	@ls -1t .data/traces/*.jsonl 2>/dev/null | head -20 || echo "No traces yet."

##@ MCP server over stateless Streamable HTTP (JWT auth; mock gateway must be running)
.PHONY: dev-keys token mcp-http mcp-cluster demo-http load-test demo-injection inspector-http test-jwt
# HTTP always needs JWT settings. If .env has no MCP_JWT_ISSUER, trust the local DEV keys.
DEV_JWT_ENV := MCP_JWT_ISSUER=https://token-service.dev.invalid \
  MCP_JWT_AUDIENCE=http://127.0.0.1:8090/mcp MCP_JWT_JWKS_FILE=.data/dev-keys/jwks.json \
  MCP_JWT_JWKS_URL=
JWT_ENV := $(LOAD_ENV); if [ -z "$${MCP_JWT_ISSUER}" ]; then \
  [ -f .data/dev-keys/jwks.json ] || { echo "no MCP_JWT_* in .env and no dev keys: run make dev-keys"; exit 1; }; \
  echo "JWT: trusting the local DEV keys (.data/dev-keys)"; export $(DEV_JWT_ENV); fi
CLIENT ?= lowerenv-shared
SCOPES ?= read

dev-keys: ## DEV ONLY: RSA key + JWKS in .data/dev-keys, standing in for the token service
	$(RUN) python -m telco_mcp_lab.devtools.token_issuer keys

token: ## DEV ONLY: print a 2 h token: make token [CLIENT=care-agent-internal SCOPES="read pii:read"]
	@$(RUN) python -m telco_mcp_lab.devtools.token_issuer mint --client "$(CLIENT)" --scopes "$(SCOPES)"

mcp-http: ## MCP server on http://127.0.0.1:8090/mcp (stateless, JWT; dev keys unless MCP_JWT_* set)
	@$(JWT_ENV); $(RUN) python -m telco_mcp --transport http

mcp-cluster: ## 2 replicas (:8091, :8092) behind a round-robin LB on :8099 (Ctrl-C stops all)
	@$(JWT_ENV); trap 'kill 0' INT TERM EXIT; \
	$(RUN) python -m telco_mcp --transport http --port 8091 $${LEGACY:+--legacy-sessions} & \
	$(RUN) python -m telco_mcp --transport http --port 8092 $${LEGACY:+--legacy-sessions} & \
	$(RUN) python -m telco_mcp_lab.devtools.round_robin_lb --port 8099 \
	  --backend http://127.0.0.1:8091 --backend http://127.0.0.1:8092 & \
	echo "cluster: http://127.0.0.1:8099/mcp  (LEGACY=1 to see sticky-session failure)"; wait

demo-http: ## Walk through tokens, clients, scopes, masking, IDs over HTTP (URL=… for the cluster)
	$(RUN) python lab/scripts/http_demo.py $${URL:+--url $$URL}

load-test: ## Load test over HTTP: N calls (2000), C concurrent (20), URL=… (needs mocks + a server)
	$(RUN) python lab/scripts/load_test.py -n $${N:-2000} -c $${C:-20} $${URL:+--url $$URL}

demo-injection: ## Before/after: the prompt-injection note as the model would see it
	$(RUN) python lab/scripts/injection_demo.py

inspector-http: ## Inspector web UI; connect to http://127.0.0.1:8090/mcp with header Authorization: Bearer <make token>
	$(INSPECTOR)

test-jwt: ## Run only the JWT / client-registry tests
	$(RUN) pytest tests/mcp_server/test_jwt_auth.py -v

##@ Container image (the server only; same image for Cloud Foundry and EKS)
.PHONY: docker-build docker-run
IMAGE ?= telco-mcp-server:dev

docker-build: ## Build the production image. Behind a TLS-intercepting proxy: CORP_CA=/path/root-ca.pem
	docker build $${CORP_CA:+--secret id=corp_ca,src=$$CORP_CA} \
	  $${HTTPS_PROXY:+--network host --build-arg HTTPS_PROXY=$$HTTPS_PROXY --build-arg HTTP_PROXY=$$HTTP_PROXY} \
	  -t $(IMAGE) .

docker-run: ## Run the image on :8093 against the local mocks, trusting the dev keys (Ctrl-C stops)
	@[ -f .data/dev-keys/jwks.json ] || { echo "run: make dev-keys"; exit 1; }
	@$(LOAD_ENV); docker run --rm --network host -e PORT=8093 \
	  -e GATEWAY_BASE_URL=http://127.0.0.1:$${MOCK_PORT:-8081} -e GATEWAY_TOKEN="$$GATEWAY_TOKEN" \
	  -e MCP_JWT_ISSUER=https://token-service.dev.invalid -e MCP_JWT_AUDIENCE=http://127.0.0.1:8090/mcp \
	  -e MCP_JWT_JWKS_FILE=/keys/jwks.json -e 'MCP_ALLOWED_HOSTS=["127.0.0.1:*","localhost:*"]' \
	  -v $(CURDIR)/.data/dev-keys/jwks.json:/keys/jwks.json:ro $(IMAGE)

##@ Developer tools with the shared lower-env client (docs/09; local stand-ins)
.PHONY: dev-token-service connect-local connect-check test-connect
CONFIG ?= .data/connect/local.env

dev-token-service: ## DEV ONLY: local token service on :8095 (client credentials; needs dev-keys)
	@[ -f .data/dev-keys/private.pem ] || { echo "run: make dev-keys"; exit 1; }
	$(RUN) python -m telco_mcp_lab.devtools.token_service

connect-local: ## Write .data/connect/local.env: connector config for the LOCAL stack (no real secrets)
	@mkdir -p .data/connect
	@printf '%s\n' \
	  "# Connector config for the LOCAL stack (make mocks, mcp-http, dev-token-service)." \
	  "TELCO_MCP_URL=http://127.0.0.1:8090/mcp" \
	  "TELCO_MCP_TOKEN_URL=http://127.0.0.1:8095/oauth/token" \
	  "TELCO_MCP_CLIENT_ID=lowerenv-shared" \
	  "TELCO_MCP_SECRET_COMMAND=$(CURDIR)/.venv/bin/python $(CURDIR)/lab/scripts/local_dev_secret.py" \
	  > .data/connect/local.env
	@chmod 600 .data/connect/local.env; echo "wrote .data/connect/local.env (use with --config)"

connect-check: ## Token + tools/list through the connector, no secrets printed (CONFIG=… default local)
	$(RUN) python -m telco_mcp_lab.connect check --config $(CONFIG)

test-connect: ## Run only the connector + dev token service tests
	$(RUN) pytest tests/connect -v

##@ Model check: one real model end to end (needs mocks, and a key in .env)
.PHONY: model-check
LLM ?=
ifneq ($(strip $(LLM)),)
export HARNESS_LLM := $(LLM)
endif

model-check: ## Real model E2E: reply, tool call, follow-up, tool error (LLM=gemini|claude|azure)
	$(RUN) python lab/scripts/model_check.py

##@ LLM harness CLI: Claude / Gemini / Azure OpenAI as the MCP host (needs mocks; stdio by default)
.PHONY: harness-check ask chat
harness-check: ## Verify MCP + model connectivity (prints actionable hints on failure)
	$(RUN) python -m telco_mcp_lab.harness --check

ask: ## One question with a full step trace: make ask Q="plans on ACC-1001?" [LLM=gemini] [TRANSPORT=http]
	@test -n "$(Q)" || { echo 'usage: make ask Q="your question" [LLM=claude|gemini|azure] [TRANSPORT=stdio|http]'; exit 2; }
	$(RUN) python -m telco_mcp_lab.harness $${TRANSPORT:+--transport $$TRANSPORT} "$(Q)"

chat: ## Interactive multi-turn chat with the trace (TRANSPORT=http to use make mcp-http)
	$(RUN) python -m telco_mcp_lab.harness $${TRANSPORT:+--transport $$TRANSPORT}

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

test-security: ## Run only security-marked tests (auth, scope/ID matrix, masking, injection, audit)
	$(RUN) pytest -m security -v

smoke: ## Real-HTTP walkthrough against the RUNNING mock gateway (start `make mocks` first)
	$(RUN) python lab/scripts/smoke_mocks.py

lint: ## Ruff lint + format check (includes bandit-style security rules)
	$(RUN) ruff check .
	$(RUN) ruff format --check .

fmt: ## Auto-format and apply safe lint fixes
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

check: lint test ## Everything CI would run: lint + full test suite

##@ Contract for external agents
.PHONY: catalog
catalog: ## Regenerate docs/tool-catalog.md from the live tool definitions (a test enforces it)
	$(RUN) python -m telco_mcp.catalog

##@ Housekeeping
.PHONY: reset-data clean
reset-data: ## Delete the mock gateway's SQLite data (orders/drafts/idempotency)
	rm -rf .data

clean: ## Remove caches and local data (keeps .env and .venv)
	rm -rf .data .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
