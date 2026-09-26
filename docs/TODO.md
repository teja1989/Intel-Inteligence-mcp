# To do: parked work to come back to

> Living list. Newest decisions at the top of each section. Each item says **why** it
> matters and where the design notes are. Posture (2026-09-26): **internal consumers
> only for now**; external agents parked.

## Security and abuse protection (E3)

- [ ] **Rate limits at two levels.** Gateway: per client_id (it supports rate limiting +
  authentication only). MCP server: per client **and per customer**, plus a per-client
  concurrency cap. Shared counters in **Redis** (available as a CF service), so limits
  hold across instances. The MCP spec says servers MUST rate-limit tools.
- [ ] **Abuse tripwires plus a kill switch.** Per client: denial ratio (probing),
  distinct customers per hour (scraping), calls per customer. Throttle → suspend → alert.
  Needs registry **hot reload / suspend list** (today `config/clients.json` loads once
  at startup, so revoking a client needs a restart).
- [ ] **Per-client tool allow-list** in the registry (finer than scopes).
- [ ] **Anti-scraping limits:** pages per call and per client per day; no bulk tools.
- [ ] **Audit → SIEM** with dashboards per client (calls, denials, distinct customers,
  errors, latency) and alert rules for the tripwires.
- [ ] Catalog version gate + catalog hash in `_meta`; change notification to consumers.

## Customer context

- [ ] **Replace the asserted `X-Customer-Account-Id` header with a verified customer
  assertion** issued by the channel that verified the customer (customer auth, or the
  human care-agent flow). Today a compromised `customer_context` client can name any
  account. Interim: internal clients only + the distinct-customer tripwire.
  Design notes: docs/06 §4, docs/07 §4.

## Access for people and tools (design: docs/08)

- [ ] **Developer SSO sign-in for lower envs** (docs/08 §6): multiple trust profiles
  (token service + corporate IdP), user policy (group/role gate, read only), protected-
  resource metadata pointing at the IdP, audit user ID. Blocked on docs/08 §10 answers.
- [ ] **Production startup guard (G2):** refuse user/SSO profiles, JWKS files, dev issuers
  and static lab tokens when `MCP_ENVIRONMENT=production`.
- [ ] **Shared lower-env client:** Claude Code `headersHelper` script (keychain) + a stdio
  bridge for VS Code and other tools, auto-refreshing the 2 h token.
- [ ] Secret scanning (pre-commit + CI) for the shared secret (G7).
- [ ] **Lower-env deployment kit:** CF manifest, `MCP_PUBLIC_URL`, `MCP_ALLOWED_HOSTS`,
  gateway pass-through of MCP headers, lower-env registry.
- [ ] Reference token client for production apps (Python + Spring): cache, refresh
  early, single-flight, one retry on 401.
- [ ] Later: MCP Enterprise-Managed Authorization (ID-JAG) if the IdP supports it.

## Onboarding

- [ ] `docs/08-consumer-onboarding.md`: request template, scope review rules,
  provisioning at both gates (token service + client registry), sandbox conformance
  checklist, lifecycle (rotation, recertification, offboarding).
- [ ] `make claude-chat CALLER=…` target + `docs/09-manual-test-plan.md` checklist
  (the Claude Code test plan from 2026-09-26).

## Agent harness

- [ ] **Claude adapter** (`HARNESS_LLM=anthropic`) next to Azure, keeping guardrails,
  traces and evals. Must preserve thinking blocks across tool calls.

## Confirm with other teams

- [ ] Token service: algorithm, `iss`, JWKS URL, `aud` per environment, client-id claim,
  scope claim/names (docs/07 §6).
- [ ] Real domain API contracts (OpenAPI or synthetic samples) → rebuild mocks, contract
  tests (docs/07 §7).
- [ ] Gateway: exact path for the MCP endpoint, which headers it forwards, timeouts,
  body-size limit.

## Planned phases

- [ ] Phase 4: orders preview → submit (parked until the API contracts arrive; docs/91).
- [ ] Phase 6: eval suite (~20 prompts, repeats, model comparison, rewording experiment).
- [ ] Phase 7: Spring AI mapping + production checklist.
- [ ] Conformance / interop validation (docs/90).

## Loose ends

- [ ] Rare `httpx.ReadError` (≈1 in 600) under burst load through the lab load balancer;
  root-cause during load testing.
- [ ] Makefile untested on macOS's bundled GNU make 3.81.
