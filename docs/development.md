# Development: local stack, model check, lower-env connector, tests

> For developers of this repo. Rules and conventions: [AGENTS.md](../AGENTS.md).
> Current as of 2026-09-28.

## 1. Local stack

Everything runs on your laptop against **synthetic** data. No Docker needed (the image is
only for checking what ships: `make docker-build docker-run`).

```bash
make doctor setup env        # once: prerequisites, install (uv workspace), create .env
make env-update              # after pulling: adds new settings, lists retired/moved ones
make mocks                   # terminal 1: mock gateway + APIs on :8081 (OpenAPI UI: /docs)
make mcp-http                # terminal 2: server on :8090, JWT with dev keys (`make dev-keys` once)
make demo-http               # walkthrough: bad tokens, clients, scopes, masking, IDs
make token                   # a 2 h dev token (CLIENT=…, SCOPES="read pii:read") for Inspector/curl
make inspector               # MCP Inspector web UI, server over stdio
```

**In Claude Code or Antigravity** (stdio, no token; run from the repo root):

```bash
claude mcp add telco-lab -- uv run --directory "$PWD" python -m telco_mcp
```

Ask with an account ID, e.g. "summarise ACC-1001". Synthetic accounts: `ACC-1001`,
`ACC-1002`, `ACC-2001`; lines `SUB-1001-01…`; orders `ORD-000123`, `ORD-000456`.
Over stdio the server's permissions are `MCP_STDIO_SCOPES` (default `read`; add
`pii:read` to see unmasked names and numbers).

## 2. The mock gateway (`lab/src/telco_mcp_lab/mock_apis`)

* Stands in for the API gateway and five domain APIs (account, subscription, service,
  order, order submission) under `/{microservice}/API/…`. Its paths are the defaults of
  the server's endpoint catalogue; a contract test keeps them in sync.
* Requires `Authorization: Bearer <MOCK_GATEWAY_TOKEN>` (the server's `GATEWAY_TOKEN`;
  `make env` sets both). Errors are RFC 9457 Problem Details.
* Data is synthetic (`data.py`); `ACC-1001` carries a prompt-injection note on purpose.
* Order drafts and idempotent submission exist in the mock (for the parked write flow).
* **Chaos:** latency or failures on every call, for timeout/breaker testing:

  ```bash
  source .env
  curl -X POST localhost:8081/_admin/chaos -H "Authorization: Bearer $MOCK_GATEWAY_TOKEN" \
       -H 'content-type: application/json' -d '{"delay_ms":5000}'           # slow
  curl -X POST … -d '{"fail_rate":1.0,"fail_status":503}'                   # failing
  curl -X POST … -d '{}'                                                    # off
  ```

## 3. Model check (Gemini)

`make model-check` runs a real model through the real server, six steps, each PASS/FAIL
with a hint: model configured, server reachable, plain reply, a real tool call (schemas
accepted, thought signatures replayed), a follow-up turn, and a tool error handled.

* Needs `make mocks` and `CHAT_GEMINI_API_KEY` in `.env` (AI Studio key; the standard
  `GEMINI_API_KEY` also works). `CHAT_GEMINI_MODEL` pins the model.
* By default it starts the server over stdio itself. `HARNESS_TRANSPORT=http` +
  `HARNESS_BEARER_TOKEN=$(make -s token)` uses a running `make mcp-http`.
* **Pinned on purpose:** the base URL and API mode are passed explicitly and redirects
  are off, so variables like `GOOGLE_GENAI_USE_VERTEXAI` can't send the key elsewhere.
* The harness also plays a well-behaved agent: a host system prompt
  (`lab/prompts/agent.system.md`) and host guardrails (`lab/config/guardrails.json`:
  input redaction, grounded-identifier output check). Those are examples of what an
  agent team owns, not server controls.
* Traces of each run: `.data/harness/*.jsonl` (post-guardrail text only).

## 4. Load test

```bash
make load-test                          # 2000 calls, 20 concurrent, against make mcp-http
make load-test N=5000 C=50 URL=http://…/mcp
```

Numbers against the mocks show the server's own overhead only; see
[operations.md](operations.md) "Cost" for measured values.

## 5. Lower-environment connector (shared client)

For people using Claude Code, VS Code or Inspector against a **lower-env** server with the
shared lower-env client ID. Nobody pastes a 2 h token: the connector
(`python -m telco_mcp_lab.connect`) gets one from the token service and refreshes it.
Production is out of scope: people and coding tools never connect to production.

| Command | For | What it does |
|---|---|---|
| `bridge` | VS Code, Inspector, any client that launches a **stdio** server | Forwards MCP messages to the remote endpoint with a fresh token (refresh 5 min before expiry, and once on a 401) |
| `headers` | Claude Code `headersHelper` | Prints `{"Authorization": "Bearer …"}`; Claude Code re-runs it on connect and after a 401 |
| `check` | You | Gets a token and lists the tools; prints claims, never the token or secret |

**One-time setup**

1. The secret goes in your **OS keychain**, never in a file:
   ```bash
   security add-generic-password -s telco-mcp-lowerenv -a lowerenv-shared -w   # macOS (prompts)
   secret-tool store --label="telco-mcp-lowerenv" service telco-mcp-lowerenv    # Linux
   ```
2. A **non-secret** config file outside any repo, e.g. `~/.config/telco-mcp/lowerenv.env`:
   ```bash
   TELCO_MCP_URL=https://<lower-env-gateway>/<path>/mcp
   TELCO_MCP_TOKEN_URL=https://<lower-env-token-service>/<token-path>
   TELCO_MCP_CLIENT_ID=lowerenv-shared
   TELCO_MCP_SECRET_COMMAND=security find-generic-password -s telco-mcp-lowerenv -w
   TELCO_MCP_CLIENT_AUTH=basic          # or post: match the token service
   # TELCO_MCP_SCOPE=read               # only if the token service needs it
   # TELCO_MCP_CA_BUNDLE=/path/corp-root.pem   # if TLS is intercepted
   ```
3. Check: `make connect-check CONFIG=~/.config/telco-mcp/lowerenv.env` →
   `token ok: {...}` and `MCP ok: … tools [...]`.

**Claude Code** (no bridge needed):

```bash
claude mcp add-json telco-lowerenv '{
  "type": "http",
  "url": "https://<lower-env-gateway>/<path>/mcp",
  "headersHelper": "uv run --quiet --directory /ABS/PATH/Intel-Inteligence-mcp python -m telco_mcp_lab.connect headers --config /ABS/HOME/.config/telco-mcp/lowerenv.env"
}'
```

Claude Code runs the helper only after you accept its trust dialog for the folder.

**VS Code** (`.vscode/mcp.json`; contains no secret, safe to commit):

```json
{ "servers": { "telco-lowerenv": { "type": "stdio", "command": "uv",
    "args": ["run", "--quiet", "--directory", "/ABS/PATH/Intel-Inteligence-mcp",
             "python", "-m", "telco_mcp_lab.connect", "bridge",
             "--config", "/ABS/HOME/.config/telco-mcp/lowerenv.env"] } } }
```

Verified with the Python SDK client and Claude Code; **not yet verified in VS Code**.

**Rehearse locally** (no real credentials):

```bash
make mocks                                              # terminal 1
make mcp-http                                           # terminal 2
uv run python -m telco_mcp_lab.devtools.token_service   # terminal 3: stand-in on :8095
make connect-check                                      # writes .data/connect/local.env, checks
```

**Troubleshooting**

| Symptom | Cause / fix |
|---|---|
| `invalid configuration: url: …must be https` | Remote URLs must be https (only localhost may use http) |
| `TELCO_MCP_SECRET_COMMAND failed` | Keychain entry missing or named differently; run the command yourself |
| `HTTP 401 invalid_client` from the token service | Wrong or rotated secret |
| MCP server answers 401 | Token from another environment's token service, or wrong `TELCO_MCP_TOKEN_URL` |
| Connected but no tools | `lowerenv-shared` not registered for this environment, or lacks `read` |
| `ERR_PROXY_TUNNEL` / proxy 403 to an internal host | Add the host to **both** `NO_PROXY` and `no_proxy` |

Rules: lower environments only; the secret stays in the keychain (never in `mcp.json`,
repos, chat, tickets or prompts); suspect a leak → tell the owner to rotate it.

## 6. Tests

```bash
make check                          # ruff + format check + full suite (CI runs this)
uv run pytest -m security           # only security properties
uv run pytest -m "not slow"         # quick loop
uv run pytest tests/mcp_server/test_endpoints.py -v
```

| Folder | Covers |
|---|---|
| `tests/mcp_server/` | the server: tools, auth, scopes, matrix, endpoints, resilience, observability, protocol over stdio and HTTP, 2-replica scaling, concurrency |
| `tests/mock_apis/` | the mock gateway, and the endpoint contract with the server |
| `tests/harness/` | the model-check harness with a scripted model and a mocked Gemini API (no network) |
| `tests/connect/` | connector and dev token service |
| `tests/test_architecture.py`, `tests/test_settings_secrets.py`, `tests/test_catalog.py` | shipping boundary, secret-safe settings, catalog drift |

Conventions (synthetic data, `_env_file=None`, no fixed sleeps, mutation-check security
controls): [AGENTS.md §6](../AGENTS.md).
