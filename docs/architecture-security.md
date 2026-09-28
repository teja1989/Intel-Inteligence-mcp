# Architecture and security

> What the server enforces, why, and which risks were accepted. Current as of
> 2026-09-28. The reasoning and experiments behind each decision are in
> [archive/](archive/) (04, 07, 08).

## 1. Shape

* **Stateless MCP over Streamable HTTP** (spec 2026-07-28; pre-2026 `initialize`
  clients also served, statelessly). No sessions: any request can hit any replica.
  Proven by `tests/mcp_server/test_http_protocol.py` (2 replicas behind round-robin,
  both protocol eras) and `test_concurrency_isolation.py` (no per-request bleed).
* **No LLM in the server.** Agents bring their own model; the server exposes tools.
* **Read-only tools today** (see [tool-catalog.md](tool-catalog.md)). Writes are parked
  until the order API contracts exist ([archive/91](archive/91-backlog-orders-preview-submit.md)).
* **stdio** transport exists for local use only (no auth); production refuses it.

## 2. Identity: the client and its scopes

One concept, the **client** (the calling agent application).

```
HTTP : Bearer JWT → JwtTokenVerifier → client_id + token scopes
                  → server/config/clients.json → scopes = token ∩ allowed
stdio: no token   → ClientContext("stdio", MCP_STDIO_SCOPES)
```

**JWT validation** (`security/jwt_verifier.py`), every failure is `401` with the
reason logged, never the token:

| Check | Why |
|---|---|
| `alg` on an asymmetric allow-list (default RS256); `none`/`HS*` refused | blocks alg=none and HS256 key confusion (tested) |
| `kid` in the token service's JWKS; signature with that key only | forged tokens fail |
| `iss` exact, `aud` = this server | a token for another service can't be replayed here (RFC 8707) |
| `exp`/`nbf` with 30 s leeway; `exp − iat` ≤ 2 h; `iat` not in the future | only normally-issued tokens |
| JWKS cached 1 h, refetched on unknown `kid` at most once a minute; single-flight | key rotation without hammering the token service |

**Client registry** (`server/config/clients.json`): a valid token is necessary, not
sufficient. Unregistered `client_id` → no tools, every call refused, audited. Effective
scopes = token scopes (via `scope_map`) ∩ `allowed_scopes`; unknown scopes ignored.
Entries can be limited to environments; production refuses untagged entries.

**Scopes:** `read` (the read tools), `pii:read` (unmasked names/numbers),
`order:submit` (reserved). A tool without a declared scope is invisible to everyone.
`tools/list` shows only what the scopes allow (`cacheScope: private`); a hidden tool
answers exactly like a nonexistent one.

Values still to confirm with the token-service team: algorithm, exact `iss`, JWKS URL,
`aud`, client-id claim, scope claim and names ([archive/07 §6](archive/07-internal-jwt.md)).

## 3. No customer boundary (accepted risk, decision 2026-09-27)

The account ID is a **tool argument** that goes into the domain API URL, like the APIs
themselves work. **Any registered client with `read` can read any account by ID.**

Accepted because consumers are internal only and the gateway authenticates every
client. Compensating controls:

| Control | Where |
|---|---|
| Registered clients only, least-privilege scopes | `security/clients.py` |
| PII masked unless `pii:read`; IMSI/ICCID/email/address never returned | `shaping/pii.py`, tool output models |
| Strict ID formats, refused **before** any backend call (no path/query injection) | `ids.py`, `tools/common.py` |
| Rows filtered to the requested ID even if the backend ignores the filter | `tools/*.py` |
| Audit line with client + every account / line / order ID touched | `security/audit.py` |
| No bulk tools, page limits | tool design |
| Parked: per-client rate limits, distinct-accounts tripwire | [TODO.md](TODO.md) |

**Revisit before** any external consumer, customer-facing agent, or client that should
see only some customers. The boundary must then come from a **verified** customer
assertion issued by the channel that verified the customer, never from the model
(design options: [integrator-guide.md §4](integrator-guide.md)).

## 4. Data protection

* **Output allow-lists:** every tool returns an explicit Pydantic model, never a
  backend payload.
* **PII:** `+44*******111`, `A*** E******` unless `pii:read`.
* **Free text (notes):** never returned. Tools say only `has_notes: true|false`.
  Notes written by people can carry prompt injections; the structural answer is not
  to hand them to the model. (A keyword filter was used until 2026-09-28; it had a
  known bypass and was removed.)
* **Errors the model sees** (`errors/tool_errors.py`): built from stable codes and
  actionable ("IDs look like ORD-000123…", "retry once"); never backend text, the
  rejected value, URLs, hosts or tokens.
* **No token passthrough:** the server calls the gateway with its **own** token. For
  gateway analytics it adds the validated client ID and tool name as headers
  ([operations.md §2](operations.md)); headers the caller sends can't change them.

## 5. Resilience

| Concern | Rule (`clients/resilience.py`) |
|---|---|
| Timeouts | connect 2 s, read 5 s (`GATEWAY_*_TIMEOUT_S`) |
| Retry | GET only; connect errors and 502/503/504; full-jitter backoff; not on read timeouts or 4xx |
| Circuit breaker | per API; opens after 5 consecutive failures, 30 s cooldown, 1 half-open trial; 4xx doesn't count |
| Pagination | bounded (`max_pages`) |

## 6. Environments and access

| | Local | Lower env (dev/test) | Production |
|---|---|---|---|
| Data | synthetic mocks | **synthetic only** (to confirm) | real |
| Auth | stdio, or JWT with dev keys | JWT: lower-env token service | JWT: production token service |
| People / coding tools | yes | yes, via the shared lower-env client (connector) or SSO (planned) | **never** |
| Agent apps | dev copies | test deployments | registered production client IDs only |

**Startup guard (G2)** (`security/environment.py`): with `MCP_ENVIRONMENT=production`
the server refuses to start (exit 2, every problem listed) on: stdio, text logs, a
JWKS *file*, local/reserved-domain or non-https URLs for public URL / issuer /
audience / JWKS, registry entries not tagged for production, gateway endpoints left on
the mock defaults.

**Shared lower-env client:** one client ID and secret for all lower environments
(decision). Accepted: no per-person attribution; acceptable only because lower
environments are synthetic and the credential is useless in production (different
issuer/audience, refused by G2). The secret lives in the OS keychain, never in files,
chat or prompts. Developer SSO (Azure AD) is planned; the onboarding process is TBD.

## 7. Settings hygiene

All configuration is environment variables, validated at startup with a one-line
error. Every settings class sets `hide_input_in_errors=True`: pydantic-settings passes
every `.env` entry into each model, so errors used to print unrelated secrets
(found and fixed 2026-09-28; `tests/test_settings_secrets.py`).

## 8. Where each control is tested

| Property | Test |
|---|---|
| Server imports nothing from the lab or an LLM SDK | `tests/test_architecture.py` |
| Scopes, hidden tools, ID formats, row filtering | `tests/mcp_server/test_security_matrix.py`, `test_scopes_shaping_audit.py` |
| JWT attacks, JWKS cache, registry | `tests/mcp_server/test_jwt_auth.py` |
| No per-request bleed under concurrency (with a canary) | `tests/mcp_server/test_concurrency_isolation.py` |
| Production guard | `tests/mcp_server/test_production_guard.py` |
| Logs/traces carry no secrets or payloads | `tests/mcp_server/test_observability.py` |
| Settings errors never echo secrets | `tests/test_settings_secrets.py` |

Security controls are mutation-checked when added (disable → a test fails → restore).
