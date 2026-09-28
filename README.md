# Telco MCP Server

A stateless [Model Context Protocol](https://modelcontextprotocol.io) server (spec
**2026-07-28**, Python SDK `mcp` **2.2.0**) that exposes a mobile operator's account,
line and order APIs as **read tools** for internal AI agents. Built for production on
Cloud Foundry now and EKS later; each concept maps to the team's Spring stack.

> All data is synthetic. No secrets in code: configuration comes from environment
> variables (locally `.env`, gitignored). **Contributors and coding agents: read
> [AGENTS.md](AGENTS.md) first.**

## What ships and what doesn't

```
server/        telco-mcp-server  (package telco_mcp)       ← SHIPS as the Docker image
  src/telco_mcp/     __main__ · http_app · server · settings · state · catalog · endpoints · ids
                     security/  jwt_verifier · clients (registry) · scoped_server · audit · environment (G2)
                     tools/     account · lines · orders · common (shared args)
                     clients/   gateway (token) · telco (typed client) · resilience (retry, breaker)
                     shaping/   pii (masking)
                     errors/    tool_errors (safe messages)
                     observability/  logs (ECS JSON) · access · fields · telemetry (OpenTelemetry)
  config/clients.json  client registry sample (production mounts its own)
lab/           telco-mcp-lab     (package telco_mcp_lab)   ← NEVER ships: local testing
  src/telco_mcp_lab/ mock_apis (fake gateway + APIs) · harness (model check, Gemini) ·
                     devtools (dev keys + token service) · connect (lower-env connector)
  scripts/ (demo, model check, load test, env update) · prompts/ · config/guardrails.json
tests/         pytest (server + lab)             docs/  six current docs + archive/
Dockerfile     two-stage, server only, non-root  AGENTS.md · .agent/skills/  rules + playbooks
```

The server has **no LLM**: agents bring their own model. `tests/test_architecture.py`
fails if the server imports lab code, an LLM SDK or an undeclared dependency.

## How a request flows

```mermaid
sequenceDiagram
    participant A as Agent (Claude Code, an app, …)
    participant G as API gateway
    participant S as MCP server (any replica)
    participant B as Domain APIs (via gateway)
    A->>G: POST /mcp  Authorization: Bearer JWT · traceparent
    G->>S: forwarded (round-robin, no stickiness)
    Note over S: ① OTel span from traceparent · access log<br/>② Host/Origin check (DNS rebinding) → 403<br/>③ JWT: signature (JWKS), iss, aud, exp, lifetime → 401<br/>④ client registry → client_id + scopes<br/>⑤ tools/list filtered · tools/call enforced<br/>⑥ arguments validated (strict ID patterns)
    S->>B: GET {endpoint template} (server's OWN token, traceparent)
    B-->>S: JSON
    Note over S: ⑦ rows filtered to the requested ID · PII masked unless pii:read<br/>⑧ allow-listed output; note text never returned (has_notes)<br/>⑨ audit line (client + IDs, no payloads)
    S-->>A: structured result, or an actionable tool error
```

Identity is one concept, the **client**. The account ID is a tool argument and there is
**no per-customer boundary**: an accepted risk with compensating controls
([architecture-security.md §3](docs/architecture-security.md)).

## Quick start (local)

Prerequisites: [uv](https://docs.astral.sh/uv/getting-started/installation/)
(`brew install uv`, or `curl -LsSf https://astral.sh/uv/install.sh | sh`, or behind a
proxy `python3 -m pip install --user uv`), make, bash, curl; Node ≥ 22.19 only for MCP
Inspector; Docker only for the image. `make doctor` checks all of it.

```bash
make doctor setup env        # prerequisites, install (uv workspace), create .env
make check                   # lint + full test suite: green before anything else
make env-update              # after pulling: new settings added, retired/moved ones listed

make mocks                   # terminal 1: mock gateway + APIs on :8081
make dev-keys                # once
make mcp-http                # terminal 2: server on :8090 (JWT with the dev keys)
make demo-http               # terminal 3: tokens, clients, scopes, masking, IDs
```

In Claude Code or Antigravity (stdio, no token), from the repo root:

```bash
claude mcp add telco-lab -- uv run --directory "$PWD" python -m telco_mcp
```

Ask with an account ID, e.g. "summarise account ACC-1001". Details, the model check,
the connector and tests: [docs/development.md](docs/development.md).

## Make targets

| Group | Targets |
|---|---|
| Setup | `doctor` · `setup` · `env` · `env-update` |
| Run locally | `mocks` · `mcp-stdio` · `mcp-http` · `dev-keys` · `token` (`CLIENT=`, `SCOPES=`) |
| Try it | `demo-http` · `inspector` · `model-check` (Gemini) · `load-test` (`N=`, `C=`, `URL=`) · `connect-check` (`CONFIG=`) |
| Quality | `check` · `fmt` · `catalog` |
| Ship | `docker-build` (`CORP_CA=`) · `docker-run` · `clean` |

## Documentation

| Doc | For |
|---|---|
| [architecture-security.md](docs/architecture-security.md) | identity and scopes, the no-boundary decision, data protection, resilience, environments, production guard |
| [operations.md](docs/operations.md) | configuration, gateway endpoints per environment, logs (ECS/ELK), traces (OpenTelemetry), image, measured cost |
| [development.md](docs/development.md) | local stack, mock gateway, model check, load test, lower-env connector, tests |
| [integrator-guide.md](docs/integrator-guide.md) | for agent teams connecting to the server |
| [TODO.md](docs/TODO.md) | parked work and open items |
| [tool-catalog.md](docs/tool-catalog.md) | the generated tool contract + hash |
| [archive/](docs/archive/) | phase-by-phase design notes and findings (historical) |

## Status

| Area | State |
|---|---|
| Read tools over stateless HTTP (and stdio locally), both protocol eras, horizontal scaling | ✅ |
| JWT auth, client registry + scopes, strict IDs, PII masking, no free text, audit | ✅ (real token-service values to confirm) |
| Production startup guard, gateway endpoint catalogue per environment | ✅ |
| Logging (ECS JSON → ELK), tracing (OpenTelemetry), Docker image | ✅ |
| Gateway OAuth token provider, metrics, backpressure, readiness, rate limits (Redis), manifests, CI | planned ([TODO.md](docs/TODO.md)) |
| Real API contracts: ID formats, response mapping | waiting on the API teams |
| Order flow (preview → submit) | ⏸ parked ([design](docs/archive/91-backlog-orders-preview-submit.md)) |

## Concept → Python → Java/Spring AI

For the Spring team. Verification notes: [docs/archive/01-concepts.md §9](docs/archive/01-concepts.md).

| Concept | Python (this lab) | Java / Spring AI 2.0 |
|---|---|---|
| MCP server | `MCPServer("telco")` | `spring-ai-starter-mcp-server-webmvc` |
| Tool | `@mcp.tool(name, title, description, annotations=ToolAnnotations(read_only_hint=True))` | `@McpTool(…, annotations = @McpTool.McpAnnotations(readOnlyHint = true))` + `@McpToolParam` |
| Structured output | Pydantic return model → `outputSchema` + `structuredContent` | Java record return type |
| Tool error (model-fixable) | `raise ToolError("…")` → `isError: true` | exception → error result (verify exact mapping in Phase 7) |
| stdio server | `MCPServer.run(transport="stdio")`, logs to stderr | `spring-ai-starter-mcp-server` + `spring.ai.mcp.server.stdio=true` |
| Bearer auth seam | SDK `TokenVerifier` → `AccessToken` (`JwtTokenVerifier`) | `oauth2-resource-server` + `JwtDecoder` (issuer + audience) |
| Client identity | `ClientContext` from `get_access_token()` + registry | `SecurityContextHolder` / `JwtAuthenticationConverter` |
| Scope-filtered tools | override `list_tools()` / `call_tool()` | per-request `ToolCallback` filter + `@PreAuthorize` |
| Strict IDs | `Field(pattern=…)` on tool args | `@Pattern` on `@McpToolParam` |
| Audit | JSON line on `telco_mcp.audit` | `@Around` aspect / Micrometer Observation + SLF4J |
| Host: MCP tools → LLM functions | `harness/bridge.py` | `SyncMcpToolCallbackProvider` → `ToolCallback` |
| Host: tool-calling loop | `harness/agent.py` (by hand, traced) | `ChatClient` internal tool execution / `ToolCallingManager` |
| LLM client (model check) | `google-genai` (Gemini), pinned base URL | `VertexAiGeminiChatModel` / other `ChatModel` |
| Agent system prompt | `lab/prompts/agent.system.md` | `ChatClient.defaultSystem(Resource)` |
| Server instructions | `MCPServer(instructions=…)` | `spring.ai.mcp.server.instructions` |
| Host guardrails | `harness/guardrails.py` + `lab/config/guardrails.json` | custom `CallAdvisor`s (`SafeGuardAdvisor` = word blocklist only) |
| Stateless HTTP | 2026-07-28 automatic; `stateless_http=True` for legacy clients | `spring.ai.mcp.server.protocol=STATELESS` (**2025-era protocol; see [archive/01 §6](docs/archive/01-concepts.md)**) |
| Downstream error format | RFC 9457 Problem Details | `ProblemDetail` / `@RestControllerAdvice` |
| Config & secrets | `pydantic-settings` + `.env` | `@ConfigurationProperties` + env / CF user-provided service |
| Gateway service token | `TokenProvider` + `httpx.Auth` hook | `OAuth2AuthorizedClientManager` + `OAuth2ClientHttpRequestInterceptor` |
| Idempotent submit | unique `(account, key)` + `BEGIN IMMEDIATE` | unique index + `@Transactional` |
| Timeouts/retry/breaker | httpx + small wrapper (Phase 3) | Resilience4j (`@TimeLimiter`, `@Retry`, `@CircuitBreaker`) |

## Decisions log

| Decision | Choice |
|---|---|
| Python | 3.12, exact pins + `uv.lock` |
| Config | `.env` only (gitignored), `.env.example` committed |
| Domain API access | Via API gateway, `/{microservice}/API/{resource}`, placeholder names |
| Gateway auth | MCP server's **own** service token (no passthrough); static `GATEWAY_TOKEN` today, OAuth client-credentials provider planned |
| Lab targets | Mock gateway only; non-localhost refused unless explicitly allowed |
| Model check | One provider: Gemini (Developer API key in `.env`); interactive testing via Claude Code / Antigravity |
| Idempotency key | Model-supplied argument, enforced by backend; risk documented and tested |
| Agent → MCP auth (E2) | JWT from the internal token service (client credentials, `scope` claim, `aud` = MCP server), **validated at the MCP server** via JWKS; gateway scope headers ignored |
| Agent clients | Registry (`server/config/clients.json`); effective scopes = token ∩ allowed; unregistered = nothing |
| Customer context (2026-09-27) | **None.** Account ID is a tool argument → API URL; any `read` client can read any account. Accepted risk (docs/architecture-security.md §3), compensated by scopes, masking, strict IDs, per-ID audit |
| Identity | One concept: the **client** (JWT `client_id` + registry scopes); stdio runs with `MCP_STDIO_SCOPES` |
| Repo layout (2026-09-28) | uv workspace: `server/` (ships, minimal deps) + `lab/` (never ships); boundary enforced by `tests/test_architecture.py` |
| Deployment | One Docker image (server only, non-root) for Cloud Foundry now and EKS later; config via env vars |
| Logging (2026-09-28) | ECS JSON on stdout → ELK, plus OTLP to the collector when configured; W3C `traceparent` correlation; no baggage |
| Free text (2026-09-28) | Notes never returned (`has_notes` only); the keyword filter was removed |
| Trim (2026-09-28) | Lab reduced to mocks, dev keys/token, model check (Gemini), load test, connector; docs to six files + archive; Makefile to 21 targets |
