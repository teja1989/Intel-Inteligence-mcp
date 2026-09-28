# To do: parked work to come back to

> Living list. Newest decisions at the top of each section. Each item says **why** it
> matters and where the design notes are. Posture (2026-09-26): **internal consumers
> only for now**; external agents parked. 2026-09-27: identity simplified to client +
> scopes; the account ID is a tool argument; **no customer boundary** (docs/architecture-security.md §3).

## Production readiness (target: 5k tool calls/min, CF now, EKS later; docs/operations.md)

- [x] **Trim** (2026-09-28): lab to what developers use (Gemini-only model check), docs to
  six files + archive, Makefile to 21 targets, `--legacy-sessions` and note shaping removed
  (notes are never returned now).
- [x] **Gateway endpoint catalogue** per environment (2026-09-28, docs/operations.md §2).
- [x] **Analytics headers** to the gateway: validated client ID + tool (2026-09-28).
  Open: confirm the header names the gateway's analytics expects (`GATEWAY_HEADER_*`).
- [x] **Repo split** (2026-09-28): `server/` ships, `lab/` never; boundary test + image check.
- [x] **Logging** (2026-09-28): ECS JSON → ELK, access/audit/downstream/breaker/retry
  events, W3C `traceparent` correlation, OTLP export, Docker image (docs/operations.md §3).
- [ ] **Metrics:** request rate / errors / latency per tool and per downstream API,
  breaker state, 401 count (OTLP metrics or Prometheus) + Kibana/Grafana dashboards,
  alerts on SLOs. Pick with the platform team (the collector is ready).
- [ ] **Backpressure:** cap in-flight requests per instance (fast 503 instead of
  queueing; measured: latency climbs past ~130–160 req/s per process), per-API
  concurrency limits, explicit httpx pool sizes.
- [ ] **Gateway token via OAuth client credentials** (today a static `GATEWAY_TOKEN`
  that expires); needs token URL, client-auth method, scope/audience.
- [ ] **Real ID formats** in `server/src/telco_mcp/ids.py` (today the mock's: `ACC-1234`…)
  and **response mapping** per API once contracts arrive (docs/operations.md §2).
- [ ] **Readiness vs liveness:** `/readyz` (JWKS loaded, gateway reachable) separate from
  `/healthz`.
- [ ] **Gateway token via OAuth client credentials** (today static `GATEWAY_TOKEN`;
  `TokenProvider` seam exists) + secrets from CredHub / Vault / Secrets Manager.
- [ ] **Deployment manifests:** CF (docker image, instances, health check, env) and
  Kubernetes (Deployment, probes, resources, HPA, PodDisruptionBudget, ConfigMap for the
  registry). Capacity: ~4–5 instances for 5k/min with 3× peaks (docs/operations.md §3).
- [ ] **CI:** GitHub Actions `make check`, gitleaks, pip-audit, image build + scan (Trivy),
  SBOM.
- [ ] Load test against the real lower-env APIs (`make load-test URL=…`; today's numbers
  are against the mocks).
- [ ] Decide the log path per index: stdout → ELK and/or OTLP logs (avoid duplicates).

## Security and abuse protection (E3)

- [ ] **Rate limits at two levels.** Gateway: per client_id (it supports rate limiting +
  authentication only). MCP server: per client, plus a per-client
  concurrency cap. Shared counters in **Redis** (available as a CF service), so limits
  hold across instances. The MCP spec says servers MUST rate-limit tools.
- [ ] **Abuse tripwires plus a kill switch.** Per client: denial ratio (probing),
  **distinct accounts per hour** (scraping: the main compensating control now that
  there's no customer boundary), calls per account. Throttle → suspend → alert.
  Needs registry **hot reload / suspend list** (today `server/config/clients.json` loads once
  at startup, so revoking a client needs a restart).
- [ ] **Per-client tool allow-list** in the registry (finer than scopes).
- [ ] **Anti-scraping limits:** pages per call and per client per day; no bulk tools.
- [ ] **Audit → SIEM** with dashboards per client (calls, denials, distinct accounts,
  errors, latency) and alert rules for the tripwires.
- [ ] Catalog version gate + catalog hash in `_meta`; change notification to consumers.

## Customer context

- [ ] **Revisit the "no customer boundary" decision** (docs/architecture-security.md §3) before any external
  consumer, customer-facing agent, or client that should see only some customers. The
  boundary would come from a verified customer assertion issued by the channel that
  verified the customer (customer auth, or the human care-agent flow), never from the
  model. Interim: internal clients only + per-ID audit + the distinct-account tripwire.
  Design notes: docs/integrator-guide.md §4.

## Access for people and tools (design: docs/architecture-security.md §6)

- [ ] **Developer SSO sign-in for lower envs (Azure AD; MCP onboarding process TBD)** (docs/architecture-security.md §6): multiple trust profiles
  (token service + corporate IdP), user policy (group/role gate, read only), protected-
  resource metadata pointing at the IdP, audit user ID. Blocked on the IdP and onboarding
  decisions (docs/archive/08-access-and-environments.md §6, §10).
- [x] **Production startup guard (G2)** + registry `environments` tags (2026-09-26).
  Extend it to SSO user profiles when those are built.
- [x] **Shared lower-env client connector** (`telco_mcp_lab.connect`: headers / bridge /
  check) + local dev token service (2026-09-26, docs/development.md §5).
- [ ] Confirm the bridge in **VS Code** itself (verified with the SDK client and Claude Code only).
- [ ] Set the real token endpoint format (`TELCO_MCP_CLIENT_AUTH`, scope) once known.
- [ ] Secret scanning (pre-commit + CI) for the shared secret (G7).
- [ ] **Lower-env deployment kit:** CF manifest, `MCP_PUBLIC_URL`, `MCP_ALLOWED_HOSTS`,
  gateway pass-through of MCP headers, lower-env registry.
- [ ] Reference token client for production apps (Python + Spring): cache, refresh
  early, single-flight, one retry on 401.
- [ ] Later: MCP Enterprise-Managed Authorization (ID-JAG) if the IdP supports it.

## Onboarding

- [ ] An onboarding doc: request template, scope review rules,
  provisioning at both gates (token service + client registry), sandbox conformance
  checklist, lifecycle (rotation, recertification, offboarding).
- [ ] A manual test plan checklist (the Claude Code test plan from 2026-09-26) in
  docs/development.md.

## Agent harness

- [x] `make model-check` with Gemini only (2026-09-28 trim; Claude and Azure adapters
  removed). Interactive testing happens in existing MCP hosts (Claude Code, Antigravity).
- [ ] First live `make model-check` with a real Gemini key; pin a Gemini model ID your
  company allows.
- [ ] Confirm the Antigravity MCP config format (stdio command, or the connector bridge
  for JWT mode); add it to docs/development.md §5.
- [ ] Gemini via Vertex AI (Google Cloud credentials) if that's the approved route.

## Confirm with other teams

- [ ] Token service: algorithm, `iss`, JWKS URL, `aud` per environment, client-id claim,
  scope claim/names (docs/architecture-security.md §2).
- [ ] Real domain API contracts (OpenAPI or synthetic samples) → rebuild mocks, contract
  tests (docs/operations.md §2).
- [ ] Gateway: exact path for the MCP endpoint, which headers it forwards, timeouts,
  body-size limit.

## Planned phases

- [ ] Phase 4: orders preview → submit (parked until the API contracts arrive; docs/archive/91-backlog-orders-preview-submit.md).
- [ ] Phase 6: eval suite (~20 prompts, repeats, model comparison, rewording experiment).
- [ ] Phase 7: Spring AI mapping + production checklist.
- [ ] Conformance / interop validation (docs/archive/90-backlog-external-validation.md).

## Loose ends

- [ ] Rare `httpx.ReadError` (≈1 in 600) under burst load through the test load balancer
  (`tests/round_robin_lb.py`); root-cause during load testing.
- [ ] Makefile untested on macOS's bundled GNU make 3.81.
