---
name: pr-review
description: Review a diff, branch or pull request in this repo for security, correctness, production readiness, tests and docs. Use when asked to review code or a PR, before opening a PR, or when checking your own change. Produces severity-ranked findings with file:line, a concrete failure scenario and a fix, plus a verdict.
---

# PR review

Goal: find what would hurt in production (security, data exposure, outages, wrong
answers), not style nits. Every finding must be **verified** against the code (read it,
run it, or write a failing test); drop what you can't substantiate, or label it
"unverified".

## 1. Gather

```bash
git fetch origin && git diff --stat origin/main...HEAD     # or the PR's base
git diff origin/main...HEAD                                 # read every hunk
make check                                                  # must be green; report failures verbatim
```

Read the touched files in full where the hunk depends on context. Note what the PR
**claims**; check the code does exactly that.

## 2. Security (block on any of these)

- [ ] **Secrets:** no tokens, keys, passwords, real `.env` values, private keys, JWTs in
      code, tests, fixtures, docs or logs. `grep -nE "(secret|token|password|BEGIN .*PRIVATE)"` on the diff.
- [ ] **Real data:** only synthetic IDs/numbers (`ACC-1xxx`, `+44 7700 900xxx`, `example.invalid`).
- [ ] **AuthN/AuthZ:** new tool declares a scope (`require_scope`); nothing bypasses
      `ScopedMCPServer.call_tool`; no weakening of `jwt_verifier.py` checks (alg allow-list,
      iss, aud, exp, lifetime); registry scopes stay token ∩ allowed.
- [ ] **Input:** every ID argument has a strict pattern from `ids.py`; no user input
      concatenated into URLs, paths, SQL, shell, log format strings.
- [ ] **Output:** explicit allow-list models; PII masked unless `pii:read`; no IMSI/ICCID/
      email/address; no free text written by people (notes) in any output.
- [ ] **Errors:** model-facing messages from `tool_errors.py`; no backend text, rejected
      values, stack traces, hosts or tokens.
- [ ] **Logging/tracing:** no tokens, arguments, results, bodies, URLs, `Mcp-Param-*`;
      IDs only in the audit line; no baggage propagation; span URLs redacted.
- [ ] **Token passthrough:** the gateway gets the server's own token only.
- [ ] **Boundary:** nothing in `server/` imports `telco_mcp_lab`, an LLM SDK, FastAPI or
      dotenv; new server dependency is pinned, justified and in `ALLOWED_THIRD_PARTY`.
- [ ] **Production guard:** any new lab/demo switch is refused when
      `MCP_ENVIRONMENT=production` (`security/environment.py`) with a test.
- [ ] **Dependencies:** exact pins; `uv.lock` updated; no unknown/typo-squatted package.
- [ ] **Docker:** nothing from `lab/`, `.env`, `.data/` or keys can reach the image
      (`.dockerignore` allow-list); runs as non-root.

## 3. Correctness and production readiness

- [ ] Statelessness: no per-request data in globals/class attributes; `ContextVar` used;
      concurrency test still meaningful (its canary still bites).
- [ ] Every exit path releases/records what it must (breaker `on_success/on_failure/abandon`,
      clients closed in lifespan).
- [ ] Downstream calls have timeouts; retries only for idempotent GETs; bounded pagination.
- [ ] Async: nothing blocking on the event loop.
- [ ] Config: new settings validated, in `.env.example`; renamed ones in `env_update.py`.
- [ ] Backwards compatibility: tool names/inputs/outputs changed → catalog hash change
      called out; `.env` changes flagged by `make env-update`.
- [ ] Behaviour under failure: backend down, slow, 4xx/5xx, malformed JSON, cancellation.

## 4. Tests

- [ ] New behaviour has tests that fail without it. For each security control in the
      diff, **mutation-check** it: disable → a test fails → restore. Report the result.
- [ ] No vacuous or circular tests (asserting a mock returns its own setup).
- [ ] No fixed sleeps, no network, no real keys; `_env_file=None`; flaky = bug.
- [ ] Markers (`security`, `protocol`, `slow`) applied correctly.

## 5. Docs

- [ ] Docs changed with the feature (AGENTS.md §7 table): catalog regenerated for tool
      changes; docs/architecture-security.md for the security model; docs/operations.md for
      config, endpoints, observability and the image; docs/development.md for local tooling; README for
      flow, targets or layout; docs/TODO.md for parked work and new risks.
- [ ] Claims in docs are true (commands run, numbers measured, dates on findings).

## 6. Report format

Findings ranked most severe first. For each:

```
[BLOCKER|MAJOR|MINOR|NIT] path/to/file.py:LINE: one-sentence defect
  Scenario: concrete input/state → wrong result / exposure / crash
  Evidence: how you verified (code read, command output, failing test)
  Fix: the smallest correct change
```

Then:

* **Checks run:** `make check` result (counts), mutation checks and outcomes.
* **Docs:** up to date / what's missing.
* **Verdict:** `approve` (no blockers/majors), `approve with nits`, or `changes requested`.

Rules: BLOCKER = security or data exposure, broken production guard, failing CI.
MAJOR = wrong behaviour, missing test for a control, outage risk. Don't pad the list;
"no findings" is a valid result if you checked.
