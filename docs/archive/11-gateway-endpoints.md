# 11 · Gateway endpoints: one catalogue, configured per environment

> **Status (2026-09-28):** built and tested against the mock gateway. The real API
> paths aren't known yet; when they are, only environment variables change, plus a
> response mapping per API (§5).

## 1. Where the endpoints live

Every downstream call the MCP server makes is one **operation** in one file:
[`server/src/telco_mcp/endpoints.py`](../server/src/telco_mcp/endpoints.py).

| Operation | Used by tool(s) | Default path (the lab mock) | Placeholders |
|---|---|---|---|
| `get_account` | get_account_summary | `/boaccount/API/account/{account_id}` | `account_id` |
| `list_subscriptions` | get_account_summary, list_subscriptions | `/bosubscription/API/subscription?account_id={account_id}&status={status}&limit={limit}&cursor={cursor}` | `account_id`, `status`, `limit`, `cursor` |
| `get_subscription` | get_service_details | `/bosubscription/API/subscription/{subscription_id}` | `subscription_id` |
| `get_service` | get_service_details | `/boservice/API/service/{service_id}` | `service_id` |
| `get_order` | get_order_status | `/boorder/API/order/{order_id}` | `order_id` |
| `list_orders` | list_orders | `/boorder/API/order?account_id={account_id}&limit={limit}&cursor={cursor}` | `account_id`, `limit`, `cursor` |

The host is always `GATEWAY_BASE_URL` (one gateway host per environment). The HTTP
method (GET) is fixed in code: it decides retry safety.

## 2. Configure an environment

One environment variable per operation, only where the path differs from the default:

```bash
GATEWAY_BASE_URL=https://api-gateway.dev.corp.example
GATEWAY_ALLOW_NON_LOCAL=true
GATEWAY_ENDPOINT_GET_ACCOUNT=/customer/v2/accounts/{account_id}
GATEWAY_ENDPOINT_LIST_SUBSCRIPTIONS=/customer/v2/accounts/{account_id}/lines?state={status}&pageSize={limit}&pageToken={cursor}
```

* **Cloud Foundry:** `env:` in the manifest (or `cf set-env`).
* **EKS:** a ConfigMap per environment, mounted with `envFrom`.
* **Local:** leave them unset; the mock serves the defaults.
* **Production:** all six must be set explicitly, even where they equal the default.
  The startup guard refuses to start otherwise and names the missing variables.

At startup the server logs which operations are overridden and their templates
(no IDs, no secrets): `gateway endpoints overridden: ['get_account', ...]`.

## 3. Template rules (checked at startup; all problems reported at once)

* A **path**, starting with `/`: letters, digits, `-._~:/` and `{placeholders}`.
  No scheme, host, `//`, `.`/`..` segments, `%`, spaces or `#`. So configuration can
  never send a request, or the server's gateway token, to another host.
* **Exactly** the operation's placeholders, each once. Placeholders can sit in the path
  or the query, so `{account_id}` can move from `?account_id=` into
  `/accounts/{account_id}/lines`.
* Query: `name={placeholder}` (renames the parameter: `pageSize={limit}`) or
  `name=literal` (a constant: `expand=plan`). A placeholder whose value is empty (e.g.
  no `status` filter asked for) is left out of the request.
* Values are percent-encoded; an ID can't add a path segment or a query string.

If a real API has **no** equivalent for a placeholder (say, no status filter), the
template can't just omit it: startup fails with "missing placeholders". That's
deliberate: silently dropping a filter would return wrong data. It needs a code change
(e.g. filter client-side) and a test.

**A wrong path shows up as "not found".** If a template points at a path the gateway
doesn't serve, calls get 404 and the tool answers "No account with that ID was
found". Signals: a spike of `tool_error` in the audit line (`mcp.outcome`) for one
tool, and `gateway call` lines at DEBUG with status 404. Check the startup line
`gateway endpoints overridden: …` first. A contract test per API (§5) catches this
before deployment.

A bad template stops startup with one line, e.g.
`startup failed: ValidationError: … GATEWAY_ENDPOINT_GET_ORDER='https://…': must be a path; the host comes from GATEWAY_BASE_URL`.
Finding (fixed 2026-09-28): that error used to print the whole input of the settings
model, and pydantic-settings puts every `.env` entry there, secrets included. All
settings classes now hide input in errors (tested).

## 4. How it's verified

| Test | Proves |
|---|---|
| `tests/mcp_server/test_endpoints.py` | env override → the URL the client actually calls; renamed/literal query params; 16 malformed templates refused with the variable name; IDs can't escape their segment; every bad setting reported at once |
| `tests/mock_apis/test_endpoint_contract.py` | every default endpoint is served by the mock gateway (catalogue and mock can't drift) |
| `tests/mcp_server/test_production_guard.py` | production refuses to start with default (mock) endpoints |

Security controls mutation-checked 2026-09-28 (encoding, dot segments, host check,
placeholder check, production rule): disabling any one fails tests.

## 5. When the real API contracts arrive

Endpoints are half the job. For each API:

1. Set the `GATEWAY_ENDPOINT_*` template (§2); check it starts.
2. Adapt the **response mapping**: the tools read mock field names
   (`account_id`, `holder_name`, `items`, `next_cursor`, …) in `tools/*.py` and
   `clients/telco.py`. Real names will differ.
3. Re-check PII handling against the real fields (`shaping/pii.py`): new fields are
   not returned unless added to the tool's output model.
4. Add a contract test with a synthetic sample of the real response.
5. Map the real error format to `GatewayError` codes (`clients/telco.py`,
   `errors/tool_errors.py`).

Then `make check`, and a `make load-test URL=…` against the lower environment.
