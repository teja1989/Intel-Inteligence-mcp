# 91 · Backlog: order flow (preview → answer → submit), Phase 4 deferred

> **Status: parked until the real preview/submit API contracts are available.**
> Decision recorded after Phase 3 (2026-09-25).

## Decisions so far

* **Separate scopes** for the write path instead of one `order:submit`. Proposed:

  | Scope | Tool(s) | Effect |
  |---|---|---|
  | `order:preview` | `preview_order`, `answer_preview` | Creates or updates a preview. No commitment; expires |
  | `order:submit` | `submit_order` | Commits the order. Destructive, idempotent via key |

  A read-only caller gets neither, so an injected "submit an order" has no tool
  to act with. A caller can be allowed to *explore* changes (preview) without
  being allowed to *commit* them.
* The backend has a **preview API** whose response can be **one or several
  steps of questions for the user**, then a **submit API**. The current mock
  (`POST /boorder/API/order/draft` + `POST /boordersubmission/API/submission`)
  is single-step and will be replaced to mirror the real contracts once shared.
* Already agreed earlier: `idempotencyKey` is a tool argument, enforced by the
  backend, with the model-generated-key risk documented and tested;
  `destructiveHint: true` on submit; the **host asks the human** before submit
  (Phase 5).

## The design question the multi-step preview raises

How does a stateless MCP server ask the *user* questions in the middle of a flow?
There are two MCP-native options:

### Option A: server-minted handle, questions as tool output (recommended)

```
preview_order(change)                  → {previewId, status: "needs_input", questions:[…], expiresAt}
   model asks the user, in chat
answer_preview(previewId, answers)     → {previewId, status: "needs_input", questions:[…]}   (repeat)
                                       → {previewId, status: "ready", priceSummary, terms}
   host shows the summary; the human confirms
submit_order(previewId, idempotencyKey) → {orderId}
```

* Exactly the spec's recommended pattern for cross-call state: an explicit,
  server-minted handle passed as a tool argument, ownership-checked on every
  call, with a stated expiry (2026-07-28 `server/tools`, "handles").
* The backend owns the preview state, so any replica can serve any step: stateless.
* Works in **both protocol eras and in Spring AI's STATELESS mode** (it's just tool calls).
* Every step shows up in our audit log and cross-tenant matrix like any other tool.

### Option B: MCP "multi round-trip requests" (MRTR / elicitation)

The 2026-07-28 revision lets a tool return `resultType: "input_required"` with
`inputRequests` (e.g. `elicitation/create` with a form schema). The **client**
collects the answers from the user and retries the same call with
`inputResponses` (+ opaque `requestState`).

* Nice UX in hosts that render forms, and the questions bypass the model (less
  injection surface for the answers).
* **But:** Spring AI's stateless server docs say stateless servers "don't
  support message requests to the MCP client (e.g., elicitation, sampling,
  ping)" (checked in the Spring AI docs source, `main`). Host support for
  2026-07-28 MRTR is still thin. *Python SDK support for MRTR in `MCPServer`
  tools is not verified yet.*

**Recommendation:** build Option A as the baseline (portable to your Java
stack). Optionally add Option B later as an experiment in the Python lab only.

## What we need from you when we resume

1. Preview API request/response shape (sanitised): how questions are
   expressed (free text? typed choices?), how answers are sent back, how "ready"
   is signalled, expiry.
2. Submit API: does it take the preview ID? What's its idempotency mechanism
   (header name, scope, replay semantics)?
3. Whether question text can contain customer-entered free text. If so, it goes
   through the same shaping as notes.

## Planned tests (when built)

Scope split (preview-only caller can't submit; hidden = unknown), cross-tenant
matrix rows for every new tool and for foreign `previewId`s, expired preview,
multi-step happy path, answers validated against the question schema,
10-parallel identical submits → exactly one order (through MCP, over HTTP,
across two replicas), and a new idempotency key with the same preview →
refused/replayed per backend contract.
