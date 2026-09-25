# 90 · Backlog: validating against external / open-source MCP implementations

> **Status: parked. We'll come back after Phase 3.** Captured from a
> discussion after Phase 2. Everything below was checked on **2026-09-25**
> by installing and running the tools; items marked *not verified* could not
> be run from the build sandbox.

## Why bother

Our own tests prove the server does what *we* think MCP means. External
implementations tell us whether that matches what *everyone else* thinks,
which is what matters when a Spring AI client, Claude Desktop, an IDE or Inspector
connects in production.

## Options evaluated

| # | Option | Validates | Works today? | Verdict |
|---|---|---|---|---|
| A | **Official conformance suite** `@modelcontextprotocol/conformance` **0.1.16** | our **server** against the spec | Partly (see below) | ✅ Add in Phase 3 once we serve HTTP |
| B | **Reference servers** as interop targets | our **client** / harness | ✅ `server-everything` ran here | ✅ Add as `make interop` (small) |
| C | **MCP Inspector 2.8.0** (TypeScript SDK 2.0) against our server | our **server**, independent codebase, **both eras** | ✅ done in Phase 2 | ✅ keep: the only independent 2026-07-28 check today |
| D | Public hosted MCP endpoints (e.g. DeepWiki) | nothing about *our* server | blocked from sandbox; *not verified* from a laptop | ⚠️ demo only; third-party, data leaves your network, check corporate policy |
| E | **Java SDK / Spring AI client → our Python server** | real production interop | not built | ✅ Phase 7 (best predictor for your Java build) |

### A. Conformance suite: what we found

```bash
npx -y @modelcontextprotocol/conformance@0.1.16 list --server
npx -y @modelcontextprotocol/conformance@0.1.16 server --url http://127.0.0.1:8090/mcp \
    --scenario server-initialize --expected-failures conformance-baseline.yml
```

* **No 2026-07-28 scenarios yet.** `--spec-version 2026-07-28` gives "Unknown
  spec version" (valid: 2025-03-26, 2025-06-18, 2025-11-25, draft, extension).
  It therefore exercises our **legacy-era** HTTP path, which is exactly the path
  Spring AI speaks (docs/01 §6).
* **Server tests need HTTP** (`--url`), so it can't run until Phase 3.
* **Most scenarios expect fixture tools** (`test_simple_text`,
  `test_image_content`, `test_tool_with_progress`, …) that a conformance test
  server implements. Against *our* telco server only the protocol-level ones are
  meaningful:
  `server-initialize`, `ping`, `tools-list`, `logging-set-level`,
  `dns-rebinding-protection` (a real security check). The rest belong in an
  `--expected-failures` baseline file with a reason per line.
* The MCP Java SDK README states it's validated against this suite (0.1.15),
  so running it gives Python and Java a shared yardstick.

### B. Reference servers: what we found

| Server | Version | Protocol it speaks | Result with our client |
|---|---|---|---|
| `@modelcontextprotocol/server-everything` (TypeScript, stdio) | 2026.8.31 | **legacy only (2025-11-25)** | ✅ `mode=auto` probed `server/discover`, got no answer, **fell back to the initialize handshake**: our client's fallback works. 13 tools listed |
| `mcp-server-time` / `mcp-server-fetch` (Python, via `uvx`) | 2026.8.18 | legacy only (they pin **`mcp<2`**) | *not verified* (sandbox couldn't download); expected to work from a laptop |

So most of the ecosystem still speaks the legacy era. A 2026-07-28 server has
to keep serving legacy clients for a while, and a client must keep the
fallback.

### D. Public hosted endpoints

`https://mcp.deepwiki.com/mcp` and similar were unreachable from the sandbox
(egress policy). From a corporate laptop they may work, but:
they validate nothing about our server; every request goes to a third party;
and corporate policy may forbid it. At most a "see a real remote server" demo,
with **no internal data**.

## Planned work (when we return)

1. `make interop`: our client against `server-everything` (and `mcp-server-time`
   locally), both eras, through the wire tracer. Assert the fallback.
2. `make conformance`: run the protocol-level scenarios against our HTTP server
   with a committed `conformance-baseline.yml` explaining every expected failure.
   Candidate for `make check` once stable.
3. Re-check the conformance suite for 2026-07-28 scenarios, and `server-everything` /
   Python reference servers for modern-era support.
4. Phase 7: a minimal Spring AI client calling our server over HTTP (legacy
   era, `protocol=STATELESS`).
