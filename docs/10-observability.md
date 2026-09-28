# 10 · Observability and deployment: logs, traces, the container image

> **Status (2026-09-28):** logging and tracing built, verified locally with the real
> image. Shipping to your ELK and OpenTelemetry collector is configuration only
> (§4). Metrics and dashboards are next (docs/TODO.md).

## 1. What you get per request

One HTTP request to `/mcp` produces (JSON format, abbreviated):

```json
{"log.level":"info","message":"tool_call get_account_summary ok","log":{"logger":"telco_mcp.audit"},
 "trace":{"id":"4bf92f3577b34da6a3ce929d0e0e4736"},"span":{"id":"3848ab90ec566efe"},
 "mcp":{"tool":"get_account_summary","client_id":"lowerenv-shared","via":"http","outcome":"ok",
        "resources":{"account_id":"ACC-1001"}},
 "event":{"dataset":"telco_mcp.audit","action":"tool_call","outcome":"success","duration":23589188}}
{"log.level":"info","message":"request","log":{"logger":"telco_mcp.access"},
 "trace":{"id":"4bf92f3577b34da6a3ce929d0e0e4736"},"span":{"id":"41a47e8e6182c3bc"},
 "http":{"request":{"method":"POST"},"response":{"status_code":200}},"url":{"path":"/mcp"},
 "event":{"duration":30030722},
 "mcp":{"method":"tools/call","tool":"get_account_summary","protocol_version":"2026-07-28",
        "client_id":"lowerenv-shared"}}
```

Every line also has `@timestamp`, `ecs.version`, `service.{name,version,environment}`
and `process.pid`. The `trace.id` is the one in the inbound `traceparent` header (from
the API gateway), so ELK can join the gateway's, this server's and the backends' logs.
Captured live on 2026-09-28; `tests/mcp_server/test_observability.py` asserts it.

## 2. Log reference

**Format:** `MCP_LOG_FORMAT=json` writes [Elastic Common Schema](https://www.elastic.co/guide/en/ecs/current/index.html)
lines (the image default, and **required** in production by the startup guard);
`text` writes one readable line (local default). Level: `MCP_LOG_LEVEL` (INFO).
Destination: stdout over HTTP, stderr over stdio (stdout is the protocol there).

| Logger | Level | Event | Key fields |
|---|---|---|---|
| `telco_mcp.access` | INFO (ERROR on 5xx, DEBUG for `/healthz`) | one line per HTTP request | `http.*`, `url.path`, `event.duration` (ns), `mcp.method`, `mcp.tool`, `mcp.protocol_version`, `mcp.client_id` |
| `telco_mcp.audit` | INFO | one line per tool call (security record) | `event.dataset=telco_mcp.audit`, `event.outcome`, `mcp.tool`, `mcp.client_id`, `mcp.outcome` (ok / tool_error / denied / error), `mcp.resources.*` |
| `telco_mcp.gateway` | DEBUG success, WARNING failure | each downstream call | `gateway.api`, `gateway.resource`, `http.response.status_code`, `event.duration`, `error.type` |
| `telco_mcp.resilience` | WARNING open / retry, INFO recovery | circuit breaker transitions, retries | `gateway.api`, `breaker.from`, `breaker.to`, `retry.reason`, `retry.attempt` |
| `telco_mcp.security.jwt_verifier` | WARNING | token rejected (reason, never the token) | |
| `telco_mcp` | INFO / CRITICAL | start (version, transport, environment, format), stop, refused config | |
| `uvicorn.error`, `mcp.*` | INFO | server lifecycle | same JSON format |

**Never logged** (tests assert the important ones): tokens or secrets, tool arguments
and results, backend bodies, URLs (they carry IDs; `httpx` is held at WARNING for this
reason), `Mcp-Param-*` headers, rejected input values, IP addresses. Account / line /
order IDs appear **only** in the audit line, because with no customer boundary
(docs/08 §1.6) "which client read which account" must be answerable. Header values we
do echo (`Mcp-Name` etc.) must match `[A-Za-z0-9_./:-]{1,64}`, else `invalid`.

**Volume at 5k calls/min:** about 10k lines/min at INFO (access + audit), plus
warnings when something is wrong. Per-call downstream detail is DEBUG; per-call latency
lives in traces.

**ELK tips:** route `event.dataset: telco_mcp.audit` to its own index/data stream with
the retention your audit policy requires; everything else to the application index.

## 3. Traces (OpenTelemetry)

* **Always on, in-process:** a TracerProvider, so logs carry `trace.id`/`span.id` even
  when nothing is exported.
* **Spans:** the HTTP request (server span, parented on the inbound `traceparent`),
  `tools/call <tool>`, and each downstream call (client span).
* **Propagation:** W3C `traceparent`/`tracestate` in, and out to the gateway.
  **Baggage is not accepted** (callers are untrusted; it would be forwarded to backends).
  Over stdio, a `traceparent` in the request `_meta` (SEP-414) is used instead.
* **Privacy:** downstream span URLs are rewritten (`/account/{id}`, no query string)
  before export (tested). Found during testing: the httpx instrumentation silently
  ignores a *sync* hook on an async client; ours is async.
* Sampling: the standard `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG`.
* **Trust:** a caller can choose its `traceparent` (and so the `trace.id` in our logs).
  That's only correlation, never authorisation; if it matters, have the gateway
  overwrite it. The server span (OTel ASGI instrumentation) records the peer address
  and user agent: fine for internal callers, review before external ones.
* `MCP_LOG_LEVEL=DEBUG` is safe to use in lower environments: DEBUG adds per-call
  downstream lines (API, status, duration), never payloads; the SDK's payload-logging
  SSE transport logger is held at WARNING.

## 4. Configuration (same image on Cloud Foundry and EKS)

| Variable | Default | Purpose |
|---|---|---|
| `MCP_LOG_FORMAT` | `text` (image: `json`) | `json` = ECS for ELK |
| `MCP_LOG_LEVEL` | `INFO` | |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset = no export | e.g. `http://otel-collector:4318` (OTLP/HTTP): traces **and** logs |
| `OTEL_EXPORTER_OTLP_HEADERS` | | collector auth, if any |
| `OTEL_LOGS_EXPORTER=none` | | keep logs on stdout only |
| `OTEL_TRACES_SAMPLER`, `_ARG` | `parentbased_always_on` | e.g. `parentbased_traceidratio` + `0.1` |
| `OTEL_SERVICE_NAME` | `telco-mcp-server` | |
| `PORT` / `MCP_PORT` | 8090 | CF sets `PORT` |
| `MCP_SHUTDOWN_GRACE_S` | 20 | drain time on SIGTERM |

Logs go **both** ways when OTLP is configured: stdout (collected by CF loggregator, or
Fluent Bit / Filebeat on EKS, into ELK) and OTLP to the collector. If both end up in the
same ELK index you'll see duplicates: pick one path per index (usually stdout → ELK,
OTLP → APM/traces with `OTEL_LOGS_EXPORTER=none`).

Caveat: the OpenTelemetry Python **logs** SDK is still under `opentelemetry.sdk._logs`
(experimental) in 1.45; it's isolated in one function (`telemetry._otlp_logs`).

## 5. The container image

```bash
make docker-build                         # telco-mcp-server:dev (server only)
make docker-build CORP_CA=/path/root.pem  # behind a TLS-intercepting proxy (build secret)
make docker-run                           # on :8093 against `make mocks`, dev keys
```

* Two stages: build (uv, locked deps, `--no-dev --package telco-mcp-server`) and runtime
  (only `/app/.venv` + `/app/config/clients.json`). Verified 2026-09-28: 264 MB, runs as
  uid 10001, contains `telco_mcp` but **not** `telco_mcp_lab`, openai, anthropic,
  google-genai or fastapi.
* `.dockerignore` is an allow-list, so `.env`, `.data/` (dev keys) and `lab/` code can't
  reach the build context.
* The ENTRYPOINT is exec-form (no shell), so SIGTERM reaches the server and in-flight
  requests drain.
* Production config: environment variables only. Mount the reviewed client registry
  over `/app/config/clients.json` (CF: bake a production image layer or use a volume
  service; EKS: ConfigMap). The startup guard refuses the lab sample registry.
* Verified: `MCP_ENVIRONMENT=production` with lab settings exits with code 2 and lists
  every problem as a CRITICAL JSON log line.

## 6. Cost (measured 2026-09-28, one process, 4-core sandbox shared with the mock)

| | Throughput | p95 |
|---|---|---|
| before logging/tracing | ~160–165 req/s | 280 ms @ 20 concurrent |
| JSON logs + in-process tracing | ~130–145 req/s | 330–360 ms @ 20 concurrent |

Roughly 10–20 % overhead, noisy measurement. Plan capacity on the second row (docs/04).

## 7. Still to do (docs/TODO.md)

* Metrics (request rate / errors / latency per tool and downstream API, breaker state)
  via OTLP metrics or Prometheus, and Kibana/Grafana dashboards + alerts.
* Backpressure: max in-flight requests per instance, per-API concurrency limits.
* Readiness endpoint (JWKS + gateway reachable) separate from `/healthz` liveness.
* CF manifest and Kubernetes manifests (probes, resources, HPA).
