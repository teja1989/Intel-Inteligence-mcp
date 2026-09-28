# Archive: phase-by-phase design notes (historical)

These are the working notes written while the server was built (2026-09-25 to
2026-09-28): findings, experiments, wire captures and the reasoning behind decisions.
They are **not maintained** and parts are out of date (e.g. callers/tenants, the
customer header, free-text shaping, three model providers were all removed later).

Current documentation:

| Read | For |
|---|---|
| [../../README.md](../../README.md) | what ships, request flow, how to run |
| [../architecture-security.md](../architecture-security.md) | identity, scopes, data protection, accepted risks, environments |
| [../operations.md](../operations.md) | configuration, gateway endpoints, logs/traces, image, capacity |
| [../development.md](../development.md) | local stack, mocks, model check, connector, tests |
| [../integrator-guide.md](../integrator-guide.md) | for agent teams connecting to the server |
| [../TODO.md](../TODO.md) | parked work |

Still referenced from current docs: [91 (orders preview/submit design, parked)](91-backlog-orders-preview-submit.md).

## Key findings (moved from the README; verified 2026-09-25)

1. **Python SDK v2 serves both protocol eras on one endpoint**, routed by the
   `MCP-Protocol-Version` header. `stateless_http=True` only affects the
   *legacy* (pre-2026, `initialize`-based) path.
2. **The MCP Java SDK (`main`) only knows protocol versions up to 2025-11-25.**
   Spring AI's `STATELESS` mode is very likely stateless Streamable HTTP on the
   older protocol, not the 2026-07-28 protocol. Verify against your team's
   pinned versions. Details and impact: primer §6.
3. The spec explicitly allows `tools/list` to vary **by the authorization on
   the request** (scope-based filtering), and recommends **server-minted
   handles** for cross-call state, which is our `draftId`.
4. **`mcp` v2 diverts stray `print()` output away from the stdio protocol
   stream** (fd 1 → stderr). Other stacks, including Spring Boot, don't do this for you.
5. **MCP Inspector 2.8.0's CLI defaults to the legacy era**; `ERA=auto` makes it
   use 2026-07-28. The Python SDK returns **unknown tool** as an `isError`
   result, not a JSON-RPC protocol error.
6. **Two replicas behind round-robin:** both eras work with `stateless_http=True`;
   with in-memory sessions the **legacy client fails (404 on the other replica)**
   while the 2026-07-28 client is unaffected. That's your Cloud Foundry reality with today's Spring AI.
7. httpx's in-process `ASGITransport` ignores timeouts, and `httpx` logs full
   URLs (identifiers) at INFO. Both are handled; see docs/04.
8. `openai` 3.x and `mcp` 2.x both use **`httpx2`**, which honours `HTTPS_PROXY`
   automatically. Good for Azure, but on a corporate laptop it would also capture
   *localhost* MCP/gateway calls. Local clients now ignore proxy env vars (tested).

