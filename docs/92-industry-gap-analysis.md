# 92 · Where the lab stands against current standards (2026-09-26)

> Sources: the MCP **2026-07-28** specification (authorization, tools,
> changelog) and the MCP **security best-practices** document, both re-read
> from the spec repository on 2026-09-25/26. Items marked *(knowledge)* rest on
> the author's knowledge of industry practice, not a source checked on this date.

**Summary:** as a *reference design* the lab matches current guidance for
protocol use, the security layering, tenant isolation, response shaping and the
split between prompts and code guardrails. As *production* it lacks real
identity integration, rate limiting, tracing, managed secrets, eval/red-team
gates and catalog pinning. Most of that is planned in E2/E3/Phase 6/Phase 7.

Posture decision (2026-09-26): **every agent, internal or external, is treated
as an untrusted third party** (see docs/06).

| Area | Status | Notes / next step |
|---|---|---|
| **Protocol & architecture** | | |
| Stateless Streamable HTTP, both eras, horizontal scaling | ✅ | measured with 2 replicas behind round-robin (docs/04) |
| Task-oriented tools, strict schemas, structured output, annotations | ✅ | catalog generated + drift-tested (E1) |
| Server-minted state handles bound to the principal | ✅ design | spec: possession of a handle MUST NOT be authorization; SHOULD bind to the user. Keep it in Phase 4 and E2 (customer handle) |
| Idempotent writes via MCP | ⚠️ | backend only; Phase 4 parked |
| **Identity & authorization** | | |
| Resource-server shape, no token passthrough, 401 metadata pointer (RFC 9728) | ✅ | verified on the wire |
| Real token validation (issuer, expiry, audience per RFC 8707) | ❌ → E2 | client-credentials tokens from the internal token service, validated at the gateway; app re-validation **OPEN** |
| Customer context for multi-customer agents | ❌ → E2 | client credentials identify the *agent*, not the customer: model A (bound partner) / B (server-verified customer handle) |
| Scope challenges / step-up (`403 insufficient_scope` + scope hint) | ⚠️ → E2 | today hidden = unknown; keep hiding what a client can *never* get, challenge where step-up is possible |
| Per-agent-client policy (allowed scopes/tools, PII) | ❌ → E2 | `config/clients.json`; effective scopes = granted ∩ allowed |
| Tenant isolation | ✅ | cross-tenant matrix, mutation-checked |
| **Data protection & guardrails** | | |
| PII minimisation/masking; IMSI/ICCID never returned | ✅ | |
| Injection defence in layers (structural first) | ✅ | a known bypass is pinned by a test |
| Host guardrails; traces written after guardrails | ✅ lab | for external agents this is **their** obligation (docs/06 §8) |
| Server doesn't rely on host confirmation | ✅ principle | Phase 4 must enforce consent server-side |
| **Rate limiting** (tools spec: servers **MUST** rate-limit) | ❌ → E3 | per client + per customer; gateway vs server **OPEN** |
| Provider content safety | ❓ | the external agent's responsibility; for internal agents, check the Azure deployment's filters |
| **Operations** | | |
| Audit (metadata only) | ✅ | add agent-client field (E2) |
| Tracing/metrics: OpenTelemetry, `traceparent` in `_meta` (2026-07-28) | ❌ → Phase 7 | GenAI semantic conventions *(knowledge)* |
| Timeouts, retry, circuit breaker | ✅ pattern | thresholds untuned |
| Secrets management | ❌ | `.env` is lab-only; Vault / CredHub / Key Vault in production |
| Evals as a CI gate | ❌ → Phase 6 | |
| Red-team suite (OWASP LLM Top 10 / agentic threats *(knowledge)*) | ⚠️ | injection tests exist; no structured suite |
| Conformance/interop | ⚠️ | docs/90 parked; the suite lacks 2026-07-28 scenarios |
| **Supply chain & governance** | | |
| Published, versioned tool contract + hash | ✅ E1 (hash) / → E3 (version gate, `_meta`) | agents pin the hash (docs/06 §5) |
| Import boundary: server has no LLM / lab dependency | ✅ E1 | architecture test, mutation-checked |
| Central MCP gateway / server allow-listing *(knowledge)* | ❓ | a platform decision if you'll run several MCP servers |

## Findings that changed the design

1. **Client credentials ≠ customer identity.** With an agent serving many
   customers, a customer ID in an argument would be a confused deputy. Design:
   model A/B (docs/06 §4, E2).
2. **Hidden-tool vs scope challenge.** Hiding prevents probing but blocks
   step-up; use both deliberately.
3. **Rate limiting is a MUST**, and missing.
4. **Server instructions may be ignored by external hosts.** Critical rules
   live in code and tool descriptions.
5. **The tool catalog is the contract.** Generated, drift-tested and hashed, so
   agents can pin it.
