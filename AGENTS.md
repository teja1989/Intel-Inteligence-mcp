# AGENTS.md: how to work in this repository

Instructions for coding agents (Claude Code, Google Antigravity, Codex, Copilot …) **and**
people. Read this before changing anything. Task-specific playbooks live in
[`.agent/skills/`](.agent/skills/):

| Skill | Use it when |
|---|---|
| [`coding-style`](.agent/skills/coding-style/SKILL.md) | writing or changing any Python here |
| [`new-mcp-tool`](.agent/skills/new-mcp-tool/SKILL.md) | adding or changing an MCP tool |
| [`pr-review`](.agent/skills/pr-review/SKILL.md) | reviewing a diff, branch or pull request |

**Security and code quality come first.** When speed and either of them conflict,
they win. When in doubt, stop and ask.

## 1. Ground rules (non-negotiable)

1. **Verify, don't assume.** Check SDK behaviour in the installed source or by
   running it; check claims about the code by reading it. Say what you could not verify.
2. **Ask before building** when a requirement is ambiguous or a decision belongs to the
   owner (architecture, security trade-offs, anything user-visible). Recommend an option.
3. **Synthetic data only.** No real customer data anywhere: code, tests, fixtures, logs,
   docs, prompts. Real-looking data must use reserved ranges (`+44 7700 900xxx`,
   `example.invalid`, `ACC-1001`…).
4. **No secrets in code or git.** Secrets come from environment variables (locally
   `.env`, which is gitignored; `.env.example` lists every setting with a safe default
   or empty value). Never print, log or commit a token, key or password.
5. **Security and tests are part of the change,** not a follow-up. A security control
   without a test that fails when it's removed doesn't count (see §6).
6. **Be direct.** Report failures, skipped steps and open risks plainly.

## 2. What ships and what doesn't

```
server/   telco-mcp-server  → package telco_mcp       SHIPS (Dockerfile). Minimal deps.
lab/      telco-mcp-lab     → package telco_mcp_lab   NEVER ships: mock APIs, LLM harness,
                                                      dev token service, dev connector,
                                                      scripts, prompts
tests/    both (never shipped)        docs/  documentation
```

* `server/` must **never** import `telco_mcp_lab`, an LLM SDK, FastAPI or `dotenv`.
  `tests/test_architecture.py` enforces it (source imports, declared dependencies, and
  what actually loads). Don't weaken that test; fix the import.
* A new third-party import in `server/` needs: a pinned entry in `server/pyproject.toml`,
  an entry in `ALLOWED_THIRD_PARTY` in the architecture test, and a reason in the PR.
  Every server dependency ends up in the production image.
* Lab-only code goes in `lab/`. If the server needs something the lab has (e.g. an ID
  format), it lives in `server/` and the lab imports it (`telco_mcp.ids`,
  `telco_mcp.gateway_routes`).

## 3. Architecture rules (server)

* **Stateless.** No per-request or per-client data in module globals, class attributes
  or caches. Request-scoped values go in `ContextVar`s (`security/scoped_server.py`,
  `observability/fields.py`). The concurrency test (`test_concurrency_isolation.py`)
  and its canary must keep passing.
* **Identity = the client + its scopes** (`security/clients.py`). HTTP: JWT → registered
  `client_id` → scopes = token ∩ registry. stdio: `MCP_STDIO_SCOPES`. Tools read the
  client with `current_client()` **where they use it**.
* **No customer boundary (accepted risk, docs/08 §1.6).** The account ID is a tool
  argument that goes into the API URL. Don't add hidden per-customer logic; changing
  this is an owner decision.
* **Every tool declares a scope** (`mcp.require_scope`). No scope = invisible to everyone.
* **Every ID argument has a strict pattern** from `telco_mcp/ids.py`, checked by the
  schema before any backend call.
* **Tool output is an allow-list** (explicit Pydantic models). Never return a backend
  payload as-is. PII is masked unless the client has `pii:read`. Free text goes through
  `shaping/free_text.py`.
* **Errors the model sees** come from `errors/tool_errors.py`: built from stable codes,
  never backend free text, never the rejected value, never internals.
* **The server calls the gateway with its OWN token**, never the caller's (MCP spec:
  no token passthrough).
* **Downstream calls:** timeouts always; retries only for idempotent GETs on connect
  errors / 502 / 503 / 504; one circuit breaker per API (`clients/resilience.py`).
* **Lab/demo switches** (`--legacy-sessions`, `MCP_UNSAFE_RAW_FREE_TEXT`, dev JWKS file,
  stdio, text logs …) must be refused in production by the startup guard
  (`security/environment.py`, G2). A new switch of that kind = a new guard rule + test.

## 4. Logging and observability (docs/10)

* Use `logging.getLogger(__name__)` or a named `telco_mcp.*` logger. Never `print`.
* Structured facts go in `extra={"fields": {"dotted.name": value}}` (ECS names where
  one exists, else `mcp.*` / `gateway.*`). Keep the message short and constant.
* **Never log:** tokens or secrets, tool arguments or results, backend bodies, URLs
  (they carry IDs; log API + resource names), `Mcp-Param-*` headers, rejected input
  values. Account / line / order IDs appear **only** in the audit line.
* Levels: `INFO` = one line per request/tool call and lifecycle; `WARNING` = something
  a human should look at (breaker opened, retry, auth rejection); `ERROR` = a bug or
  5xx; `DEBUG` = per-downstream-call detail.
* Trace context is W3C `traceparent` only; don't add baggage propagation.

## 5. Configuration

* `pydantic-settings` classes with a prefix (`MCP_`, `MCP_JWT_`, `GATEWAY_`, `HARNESS_`,
  `CHAT_…`). Validate at startup; fail with a clear message, not a traceback.
* Every new setting goes in `.env.example` (safe default or empty) with a one-line
  comment. Renamed/removed settings go in `RETIRED` / `MOVED` in
  `lab/scripts/env_update.py` so `make env-update` tells people.
* Secrets are `SecretStr`, never logged, never defaulted to a real-looking value.

## 6. Testing (definition of done)

* `make check` (ruff lint + format check + full pytest) is green. Run it; don't assume.
* New behaviour has a test that **fails without the change**. For security controls,
  do a quick mutation check: disable the control, confirm a test fails, restore.
* Tests use synthetic data, `_env_file=None` settings, and the in-process mock gateway or
  `respx`. No network, no real keys. Timeouts need a real socket (`LiveServer`).
* Markers: `security` (asserts a security property), `protocol` (real transport),
  `slow`. No vacuous tests (asserting a mock returned what you told it to).
* Flaky = bug. Fix the cause (e.g. wait for a condition), never add retries or sleeps.

## 7. Documentation (keep it true)

A change isn't done until the docs that describe it are updated in the same PR:

| Change | Update |
|---|---|
| Tool added/changed | `make catalog` (regenerates `docs/tool-catalog.md`; a test enforces it), docs/06 if integrators must know |
| Identity, auth, scopes, registry | docs/04 §3–4, docs/07, docs/08 |
| New setting / renamed setting | `.env.example`, `env_update.py`, the doc that covers the feature |
| Logging, tracing, metrics | docs/10 |
| Deployment, image, Makefile targets | README (Quick start, targets table), docs/10 §deploy |
| Parked work / new risk | docs/TODO.md |
| Anything a developer needs to understand the flow | README |

Docs state what was **verified** vs assumed, with dates for findings.

## 8. Commands

```bash
make setup check          # install (uv workspace), lint + all tests
make mocks                # mock gateway :8081           (terminal 1)
make mcp-http             # server :8090, dev JWT keys    (terminal 2; `make dev-keys` once)
make demo-http            # walkthrough over HTTP
make catalog              # regenerate docs/tool-catalog.md after tool changes
make docker-build docker-run   # production image, run against the mocks
make env-update           # add new .env settings, list retired/moved ones
```

## 9. Git

* Work on a branch; small, focused commits; message = what and why.
* Never commit `.env`, `.data/`, keys, tokens, traces or real data.
* Don't rewrite shared history. Don't open a PR unless asked.
