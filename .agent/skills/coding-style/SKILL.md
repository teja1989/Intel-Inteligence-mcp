---
name: coding-style
description: Python coding conventions for this repo (telco MCP server + lab). Use whenever writing or changing Python code here - module layout, typing, pydantic models and settings, async/httpx, error handling, logging with structured fields, comments and docstrings, tests. Security and readability rules included.
---

# Coding style

Read `AGENTS.md` first; this skill is the detail. Match the surrounding code: its
comment density, naming and idioms. When a rule here and the code disagree, follow the
code in that file and flag the inconsistency.

## Language and tooling

* Python **3.12** only (`match`, `type` params `def f[T]`, `X | None`, `StrEnum`).
* **ruff** is the linter and formatter: line length 100, rules `E F I B UP S ASYNC SIM`.
  `make fmt` fixes, `make lint` checks. `# noqa` needs the rule code **and** a reason:
  `# noqa: S603 - fixed argv, our own module`.
* `# fmt: skip` only for tables/aligned literals that read better by hand.
* Dependencies are exact pins (`==`); `uv.lock` pins the rest. Add with care (AGENTS.md §2).

## Module shape

```python
"""One-line purpose.

Why it exists / the rule it enforces, in plain words. Security reasoning if any.
Findings that shaped it (dated), e.g. "Finding (fixed): …".

Java/Spring equivalent: <the Spring AI / Spring Security / Resilience4j counterpart>.
"""

import stdlib
import third_party

from telco_mcp.x import y  # absolute imports only

log = logging.getLogger(__name__)  # or a named telco_mcp.* logger
```

* The module docstring explains **why**, not a line-by-line **what**. The Java/Spring
  mapping line is a house convention (the team's production stack is Spring).
* Comments: sparse, for non-obvious reasons ("Every exit path must report to the
  breaker, or a half-open trial wedges it"). No commented-out code.
* Small modules with one job. Private helpers start with `_`.

## Types and data

* Type every function signature. `Any` only at true boundaries (JSON from the backend).
* Tool inputs/outputs and settings are **Pydantic v2** models; plain internal records
  are `@dataclass(frozen=True)`.
* Constants: `Final` or UPPER_CASE; enums: `StrEnum`.
* Settings: `pydantic-settings` class, `env_prefix`, `extra="ignore"`, validators that
  fail with an actionable message; secrets as `SecretStr`; blank env value = unset
  (`_blank_is_unset` pattern).

## Async and I/O

* The server is asyncio. Never block the event loop (no `time.sleep`, no sync HTTP,
  no sync file I/O on the request path).
* One pooled `httpx.AsyncClient` per process (created in the lifespan, closed on
  shutdown). Always explicit timeouts. `follow_redirects=False` towards the gateway.
* Per-request state lives in `ContextVar`s, never globals (AGENTS.md §3).

## Errors

* Model-facing: `ToolError` with a message from `errors/tool_errors.py` (stable codes,
  what to do next, e.g. "Do not guess IDs: use the list tools or ask the user.").
* Never put in an error: backend free text, the rejected input value, stack traces,
  URLs, tokens, hostnames.
* Catch narrowly. `except BaseException` only where a resource must be released, then
  re-raise (see the breaker's `abandon()`).
* Startup/config errors: clear one-line message and exit code 2, not a traceback.

## Logging

```python
log.warning(
    "gateway call failed: %s (%s)",
    what,
    type(exc).__name__,
    extra={"fields": {"gateway.api": api.value, "error.type": type(exc).__name__}},
)
```

* Message: short, constant wording, `%s` args (not f-strings) so aggregation works.
* Facts in `extra={"fields": {...}}`, dotted ECS names (`http.response.status_code`,
  `event.duration` in ns, `error.type`) or `mcp.*` / `gateway.*`.
* Request-wide fields: `with observability.fields.bind(**{"mcp.tool": name}): ...`.
* Never log secrets, arguments, results, bodies, URLs, IDs (only the audit line has IDs).

## Security habits

* Validate at the edge with strict patterns (`telco_mcp/ids.py`); an ID that can't
  match can't traverse a path or inject a query.
* Build URLs with the helpers in `clients/gateway.py`; never string-concatenate user
  input into a URL, path, SQL or shell command. `subprocess` only with a fixed argv
  list, never `shell=True`.
* Compare secrets with `hmac.compare_digest`. Random for security: `secrets`, not `random`.
* Anything that is lab/demo-only must be refused in production (G2 guard).

## Tests

* pytest + pytest-asyncio (auto mode). Name tests as the behaviour:
  `test_malformed_ids_never_reach_the_backend`.
* Docstring on a test when the *why* isn't obvious ("Review bug 3: `code` is
  backend-controlled; only a safe identifier may pass.").
* Arrange with the real mock gateway (`mock_telco_factory`) or `respx`; settings with
  `_env_file=None`; HTTP with `http_app_in()` + `jwt_token()` from `tests/conftest.py`.
* Wait for conditions, never `sleep` a fixed time. Positive control for every negative
  assertion ("the recorder does see a valid call").
