# Telco MCP server. Run `make` (or `make help`) to list targets.
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

# Load .env for recipes that need values. Values stay in the shell, never echoed.
LOAD_ENV := set -a; [ -f .env ] && . ./.env; set +a

# Local default for the server's client registry (the image uses /app/config/clients.json).
# A value in .env wins (recipes that load .env); `make env-update` flags a stale one.
export MCP_CLIENTS_CONFIG ?= server/config/clients.json

INSPECTOR  := npx -y @modelcontextprotocol/inspector@2.8.0
SERVER_CMD := $(UV) run --quiet python -m telco_mcp

# HTTP always needs JWT settings. If .env has no MCP_JWT_ISSUER, trust the local DEV keys.
DEV_JWT_ENV := MCP_JWT_ISSUER=https://token-service.dev.invalid \
  MCP_JWT_AUDIENCE=http://127.0.0.1:8090/mcp MCP_JWT_JWKS_FILE=.data/dev-keys/jwks.json \
  MCP_JWT_JWKS_URL=
JWT_ENV := $(LOAD_ENV); if [ -z "$${MCP_JWT_ISSUER}" ]; then \
  [ -f .data/dev-keys/jwks.json ] || { echo "no MCP_JWT_* in .env and no dev keys: run make dev-keys"; exit 1; }; \
  echo "JWT: trusting the local DEV keys (.data/dev-keys)"; export $(DEV_JWT_ENV); fi
CLIENT ?= lowerenv-shared
SCOPES ?= read

##@ Setup
.PHONY: help doctor setup env env-update
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
	else echo "ℹ️  node not found: only needed for MCP Inspector (make inspector)"; fi; \
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
	@if [ -f .env ]; then echo ".env already exists; leaving it alone (make env-update adds new settings)."; else \
	  gen() { $(RUN) python -c 'import secrets; print(secrets.token_urlsafe(32))'; }; \
	  tok=$$(gen); \
	  sed -e "s|^MOCK_GATEWAY_TOKEN=.*|MOCK_GATEWAY_TOKEN=$$tok|" \
	      -e "s|^GATEWAY_TOKEN=.*|GATEWAY_TOKEN=$$tok|" \
	      -e "s|^DEV_TOKEN_SERVICE_CLIENT_SECRET=.*|DEV_TOKEN_SERVICE_CLIENT_SECRET=$$(gen)|" .env.example > .env; \
	  chmod 600 .env; echo "Created .env (mode 600) with a random gateway token + dev secret."; fi

env-update: ## Add settings that are new in .env.example to your existing .env (never overwrites)
	@$(RUN) python lab/scripts/env_update.py $${CHECK:+--check}

##@ Run locally (each in its own terminal)
.PHONY: mocks mcp-stdio mcp-http dev-keys token
mocks: ## Mock gateway + domain APIs on 127.0.0.1:8081 (OpenAPI UI at /docs)
	$(RUN) python -m telco_mcp_lab.mock_apis

mcp-stdio: ## MCP server on stdio (MCP_STDIO_SCOPES; normally an MCP host starts it)
	$(SERVER_CMD)

mcp-http: ## MCP server on http://127.0.0.1:8090/mcp (stateless, JWT; dev keys unless MCP_JWT_* set)
	@$(JWT_ENV); $(RUN) python -m telco_mcp --transport http

dev-keys: ## DEV ONLY: RSA key + JWKS in .data/dev-keys, standing in for the token service
	$(RUN) python -m telco_mcp_lab.devtools.token_issuer keys

token: ## DEV ONLY: print a 2 h token: make token [CLIENT=care-agent-internal SCOPES="read pii:read"]
	@$(RUN) python -m telco_mcp_lab.devtools.token_issuer mint --client "$(CLIENT)" --scopes "$(SCOPES)"

##@ Try it
.PHONY: demo-http inspector model-check load-test connect-check
demo-http: ## Walk through tokens, clients, scopes, masking and IDs over HTTP (needs mocks + mcp-http)
	$(RUN) python lab/scripts/http_demo.py $${URL:+--url $$URL}

inspector: ## MCP Inspector web UI on 127.0.0.1:6274, launching the server over stdio
	$(INSPECTOR) $(SERVER_CMD)

model-check: ## Real model (Gemini) end to end: reply, tool call, follow-up, tool error (needs mocks)
	$(RUN) python lab/scripts/model_check.py

load-test: ## Throughput + latency over HTTP: N calls (2000), C concurrent (20), URL=…
	$(RUN) python lab/scripts/load_test.py -n $${N:-2000} -c $${C:-20} $${URL:+--url $$URL}

CONFIG ?= .data/connect/local.env
connect-check: ## Lower-env connector: token + tools/list, no secrets printed (CONFIG=…; see docs/development.md)
	@if [ "$(CONFIG)" = ".data/connect/local.env" ] && [ ! -f "$(CONFIG)" ]; then mkdir -p .data/connect; \
	  printf '%s\n' "# Connector config for the LOCAL stack (make mocks, mcp-http, dev token service)." \
	    "TELCO_MCP_URL=http://127.0.0.1:8090/mcp" "TELCO_MCP_TOKEN_URL=http://127.0.0.1:8095/oauth/token" \
	    "TELCO_MCP_CLIENT_ID=lowerenv-shared" \
	    "TELCO_MCP_SECRET_COMMAND=$(CURDIR)/.venv/bin/python $(CURDIR)/lab/scripts/local_dev_secret.py" \
	    > "$(CONFIG)"; chmod 600 "$(CONFIG)"; echo "wrote $(CONFIG)"; fi
	$(RUN) python -m telco_mcp_lab.connect check --config $(CONFIG)

##@ Quality
.PHONY: check fmt catalog
check: ## Everything CI runs: ruff lint + format check + full test suite
	$(RUN) ruff check .
	$(RUN) ruff format --check .
	$(RUN) pytest

fmt: ## Auto-format and apply safe lint fixes
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

catalog: ## Regenerate docs/tool-catalog.md from the live tool definitions (a test enforces it)
	$(RUN) python -m telco_mcp.catalog

##@ Ship (the server only; same image for Cloud Foundry and EKS)
.PHONY: docker-build docker-run clean
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

clean: ## Remove caches and local data incl. dev keys and mock data (keeps .env and .venv)
	rm -rf .data .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
