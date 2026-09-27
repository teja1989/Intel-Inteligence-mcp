# To do: parked work to come back to

> Living list. Newest decisions at the top of each section. Each item says **why** it
> matters and where the design notes are. Posture (2026-09-26): **internal consumers
> only for now**; external agents parked. 2026-09-27: identity simplified to client +
> scopes; the account ID is a tool argument; **no customer boundary** (docs/08 §1.6).

## Security and abuse protection (E3)

- [ ] **Rate limits at two levels.** Gateway: per client_id (it supports rate limiting +
  authentication only). MCP server: per client, plus a per-client
  concurrency cap. Shared counters in **Redis** (available as a CF service), so limits
  hold across instances. The MCP spec says servers MUST rate-limit tools.
- [ ] **Abuse tripwires plus a kill switch.** Per client: denial ratio (probing),
  **distinct accounts per hour** (scraping: the main compensating control now that
  there's no customer boundary), calls per account. Throttle → suspend → alert.
  Needs registry **hot reload / suspend list** (today `config/clients.json` loads once
  at startup, so revoking a client needs a restart).
- [ ] **Per-client tool allow-list** in the registry (finer than scopes).
- [ ] **Anti-scraping limits:** pages per call and per client per day; no bulk tools.
- [ ] **Audit → SIEM** with dashboards per client (calls, denials, distinct accounts,
  errors, latency) and alert rules for the tripwires.
- [ ] Catalog version gate + catalog hash in `_meta`; change notification to consumers.

## Customer context

- [ ] **Revisit the "no customer boundary" decision** (docs/08 §1.6) before any external
  consumer, customer-facing agent, or client that should see only some customers. The
  boundary would come from a verified customer assertion issued by the channel that
  verified the customer (customer auth, or the human care-agent flow), never from the
  model. Interim: internal clients only + per-ID audit + the distinct-account tripwire.
  Design notes: docs/06 §4.

## Access for people and tools (design: docs/08)

- [ ] **Developer SSO sign-in for lower envs (Azure AD; MCP onboarding process TBD)** (docs/08 §6): multiple trust profiles
  (token service + corporate IdP), user policy (group/role gate, read only), protected-
  resource metadata pointing at the IdP, audit user ID. Blocked on docs/08 §10 answers.
- [x] **Production startup guard (G2)** + registry `environments` tags (2026-09-26).
  Extend it to SSO user profiles when those are built.
- [x] **Shared lower-env client connector** (`telco_mcp_lab.connect`: headers / bridge /
  check) + local dev token service (2026-09-26, docs/09).
- [ ] Confirm the bridge in **VS Code** itself (verified with the SDK client and Claude Code only).
- [ ] Set the real token endpoint format (`TELCO_MCP_CLIENT_AUTH`, scope) once known.
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
- [ ] `docs/09-manual-test-plan.md` checklist
  (the Claude Code test plan from 2026-09-26).

## Agent harness

- [x] **Claude + Gemini adapters** + `make model-check` (2026-09-26, docs/05 §Providers).
  Streamlit chat UI built, then removed to keep the lab lean: interactive testing now
  happens in existing MCP hosts (Claude Code, Google Antigravity).
- [ ] First live `make model-check` with real Claude / Gemini keys; pin a Gemini model ID
  your company allows.
- [ ] Confirm the Antigravity MCP config format (stdio command, or the connector bridge
  for JWT mode); add it to docs/09.
- [ ] Gemini via Vertex AI (Google Cloud credentials) if that's the approved route.

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
