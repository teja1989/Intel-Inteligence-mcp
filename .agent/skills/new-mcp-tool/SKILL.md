---
name: new-mcp-tool
description: Step-by-step playbook for adding or changing an MCP tool in the telco MCP server (server/src/telco_mcp/tools). Use when asked to add a tool, expose a new domain API, change a tool's inputs/outputs/description, or add a write (order) tool. Covers design, scopes, strict ID schemas, output allow-lists, PII masking, error mapping, gateway client, mock API, tests, catalog and docs.
---

# Building an MCP tool

A tool is a **public contract** read by LLMs you don't control. Design it like an API
for an untrusted, literal-minded client. Read `AGENTS.md` and the `coding-style` skill
first. Reference implementations: `tools/orders.py` (simple), `tools/lines.py`
(pagination + PII), `tools/account.py` (two backend calls + free text).

## 0. Decide before coding (ask the owner if unclear)

* **Task, not endpoint.** One tool = one user intent ("summarise an account"), which may
  call several APIs. Don't mirror the REST API 1:1.
* **Read or write?** Writes (orders) are parked until the API contracts exist; they
  follow docs/archive/91-backlog-orders-preview-submit.md (preview → server-minted handle → submit with idempotency key, scope
  `order:submit`, `destructiveHint`, host confirmation). Don't improvise a write tool.
* **Scope:** `read` for reads. New scope = registry (`server/config/clients.json`
  `scope_map`), `Scope` constant, docs/architecture-security.md update, owner approval.
* **What may the model see?** List every output field and why it's needed. Default:
  leave it out. PII (names, numbers) masked unless `pii:read`; IMSI/ICCID, email,
  address never.
* **Which IDs does it take?** Each needs a pattern in `telco_mcp/ids.py`.

## 1. Endpoint + gateway client

* Add the operation to `OPERATIONS` **and** a field to `GatewayEndpoints` in
  `server/src/telco_mcp/endpoints.py`: `DomainApi`, method `GET`, its placeholders, and
  a default path (the mock's). Never build a gateway URL anywhere else.
* Add a typed method in `clients/telco.py` that calls
  `self._get("get_thing", thing_id=thing_id)`. `_get` resolves the template (encoding,
  query), and adds timeouts, retry (GET only), the circuit breaker, logging and spans.
* New API family? Add a `DomainApi` member (own breaker).
* Follow cursors with a bound (`max_pages`), never unbounded.
* Update docs/operations.md §2 (table) and `.env.example` (commented `GATEWAY_ENDPOINT_<OP>`). The
  production guard will require the new variable in production automatically.

## 2. Mock API (lab/src/telco_mcp_lab/mock_apis)

* Serve the default path on the mock (router under `MOCK_PREFIXES`); the contract
  test `tests/mock_apis/test_endpoint_contract.py` fails until you do.
* Add the endpoint to the right router with synthetic data in `data.py` (reserved
  number ranges, `example.invalid`). Return RFC 9457 problems for errors, like the others.
* Include an adversarial record if the field is free text (see `INJECTED_NOTE`).

## 3. The tool (server/src/telco_mcp/tools/<area>.py)

```python
class Thing(BaseModel):  # OUTPUT = allow-list, documented fields
    thing_id: str
    msisdn: str = Field(description="Phone number, masked unless the client may see PII.")


DESCRIPTION = """\
<What it returns, in one sentence> (read-only).

Use this when <user intents, quoted examples>.
Do NOT use this for <nearby intents>; use <other_tool> instead.
Needs the <id> (e.g. ACC-1001). If the user hasn't given it, ask for it.
"""


def register(mcp: ScopedMCPServer) -> None:
    mcp.require_scope("get_thing", Scope.READ)  # BEFORE the decorator, always

    @mcp.tool(
        name="get_thing",  # verb_noun, snake_case, stable
        title="Get thing",
        description=DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def get_thing(ctx: Context, account_id: AccountIdArg) -> Thing:
        async with gateway_errors():  # backend errors → safe ToolError
            raw = await app_state(ctx).telco.get_thing(account_id)
        if raw.get("account_id") != account_id:  # don't trust the backend blindly
            raise ToolError(not_found("thing", "..."))
        pii = PiiPolicy(current_client())  # read the client where it's used
        return Thing(thing_id=raw["thing_id"], msisdn=pii.msisdn(raw["msisdn"]))
```

Rules:
* **Inputs:** reuse `AccountIdArg`, `SubscriptionIdArg`, `OrderIdArg`, `LimitArg`,
  `CursorArg` from `tools/common.py`; add new `Annotated[..., Field(pattern=ids.X,
  description=..., examples=[...])]` types there. Required unless there's a safe default.
  Descriptions tell the model where the value comes from and "never guess".
* **List tools** filter rows to the requested ID (`if row.get("account_id") ==
  account_id`) even if the backend was asked to filter. Paginate (`limit` ≤ 20,
  `next_cursor`).
* **Free text** written by people (notes, comments) is never returned: expose a boolean
  like `has_notes`. A keyword filter can be bypassed; not handing the text over can't.
* **Errors:** only via `gateway_errors()` and `errors/tool_errors.py` helpers.
* **New ID argument name?** Add it to `_RESOURCE_ARGS` in `security/audit.py` so the audit
  line records it.
* Register the module in `server.py` (`for module in (...)`) if it's a new file.
* No logging of arguments/results; `scoped_server` already logs and audits the call.

## 4. Tests (all required)

| Test | Where |
|---|---|
| Contract: name, annotations, required fields, patterns, description has "Use this when" and "Do NOT use" | `tests/mcp_server/test_<area>_tool.py` |
| Output allow-list: forbidden fields (email, IMSI, address, raw notes) absent from the wire JSON | same |
| Masked by default, unmasked with `pii:read` | `test_scopes_shaping_audit.py` |
| Errors via `respx`: 404, timeout, 401, 5xx, non-JSON, evil `code`/`detail` never forwarded | same file |
| Security matrix row: existing / missing / malformed IDs, without `read` scope | `test_security_matrix.py` (`MATRIX`; the completeness test fails without it) |
| Rows of other accounts dropped when the backend ignores the filter | `test_security_matrix.py` |
| Integration against the real mock gateway | `mock_telco_factory` fixture |

Then mutation-check the tool's guards (filter, ownership check, masking): disable, see a
test fail, restore.

## 5. Contract and docs

* `make catalog` → commit `docs/tool-catalog.md` (the catalog hash changes: that's a
  contract change for agent teams; mention it in the PR).
* docs/integrator-guide.md if agent teams need to know; docs/architecture-security.md if a security rule changed;
  README if the flow changed.
* `make check` green.

## Checklist (copy into the PR)

- [ ] Task-shaped tool, scope declared, read-only annotations correct
- [ ] Every ID input has a strict pattern; required unless safe default
- [ ] Output is an explicit allow-list; PII masked unless `pii:read`
- [ ] Rows filtered to the requested ID; backend answer sanity-checked
- [ ] Errors only via `gateway_errors()` / `tool_errors.py`
- [ ] Audit records any new ID argument
- [ ] Tests: contract, allow-list, masking, errors, matrix row, integration; mutation-checked
- [ ] `make catalog`, docs updated, `make check` green
