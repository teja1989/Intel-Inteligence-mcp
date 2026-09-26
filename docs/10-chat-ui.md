# 10 · Chat UI: the full agent experience in the browser (Claude, Gemini, Azure OpenAI)

> A small Streamlit app on top of the harness agent: pick a **model** and an
> **identity**, chat, and open "What happened" under each answer to see every model
> step, MCP tool call, tool result and guardrail event. **Lab rig**: local only
> (127.0.0.1), synthetic data only. The MCP server doesn't change; it doesn't know
> which model is calling.

## 1. Run it

```bash
make setup                      # installs the `chat` group too (streamlit + model SDKs)
# .env: at least one provider (see §3), e.g. CHAT_CLAUDE_API_KEY=sk-ant-…
make mocks                      # terminal 1
make mcp-http                   # terminal 2 (lab callers)  — or make mcp-http-jwt (§2)
make chat-ui                    # terminal 3 → http://127.0.0.1:8501
```

## 2. What you choose in the sidebar

| Setting | Options | Notes |
|---|---|---|
| **Model** | Claude · Gemini · Azure OpenAI | Only configured providers are listed |
| **Identity: lab caller** | alice / bob / carol / mallory | Static lab tokens against `make mcp-http`. Shows scopes and tenant isolation (e.g. carol sees full numbers, mallory sees no tools) |
| **Identity: shared lower-env client** | connector config + **customer** | JWT against `make mcp-http-jwt` (+ `make dev-token-service`, `make connect-local`), or a real lower env (docs/09). The customer is set **by you**, never by the model |
| **Backend chaos** | slow / fail / off | Flips the mock gateway, to watch timeouts and "temporarily unavailable" handling |

Changing the model, identity, customer or server **starts a new conversation**. The
old one holds the previous identity's data in the model's context, and carrying it
over would leak it across customers *through the model*, even though the server
never would.

## 3. Provider configuration (.env)

| Provider | Required | Optional |
|---|---|---|
| Claude | `CHAT_CLAUDE_API_KEY` | `CHAT_CLAUDE_MODEL` (default `claude-opus-5`), `CHAT_CLAUDE_EFFORT`, `CHAT_CLAUDE_FALLBACKS` (default on), `CHAT_CLAUDE_PROXY`, `CHAT_CLAUDE_CA_BUNDLE` |
| Gemini | `CHAT_GEMINI_API_KEY` (Gemini Developer API) | `CHAT_GEMINI_MODEL` (default alias `gemini-flash-latest`: **pin a model your company allows**), `CHAT_GEMINI_PROXY`, `CHAT_GEMINI_CA_BUNDLE` |
| Azure OpenAI | `AZURE_OPENAI_ENDPOINT`, `_API_KEY`, `_DEPLOYMENT` (docs/05) | as before |

**Why `CHAT_CLAUDE_` / `CHAT_GEMINI_` and not the usual names?** Found while building:
Claude Code exports `CLAUDE_EFFORT` and `ANTHROPIC_BASE_URL` into shells it runs, and
the Anthropic SDK reads `ANTHROPIC_BASE_URL` by itself. Launched from such a shell,
the app would have sent **your key to a different endpoint**. The Google SDK
likewise reads `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_GENAI_USE_VERTEXAI` (which
switches to a different API) and `GOOGLE_GEMINI_BASE_URL`. So every key, base URL
and API choice is passed explicitly, and tests pin it.

The CLI uses the same providers: `make ask Q=… ` / `make chat` with `HARNESS_LLM=claude|gemini|azure`
(empty = first configured).

## 4. Security guardrails (and how each is enforced)

| Risk | Guardrail | Enforced / verified by |
|---|---|---|
| UI reachable from the network | Refuses to run unless bound to 127.0.0.1/localhost; `make chat-ui` binds there; usage stats off; Streamlit's "Deploy" button hidden | App check + AppTest |
| **Data exfiltration through rendered markdown** | Model text is sanitised before display: images removed, links and URLs shown as inert text, `<` escaped | Unit tests + a **real Chromium test** (below) |
| Cross-customer leakage through the model's context | New conversation whenever model, identity, customer or server changes | AppTest |
| Keys reaching the browser or a wrong endpoint | Keys read server-side from `.env` only, never rendered; explicit base URLs; SDKs never follow redirects (the key headers aren't stripped on cross-host redirects) | Adapter tests (redirect, env-leak) |
| Config errors echoing secrets | Only field names are shown | Tests |
| Data sent to model providers | Synthetic data only; the sidebar states which provider receives tool results | Process: your company's approval per provider |
| Unwanted actions | Non-read-only tools are declined in the UI (no confirmation dialog yet); step limit, unknown-tool and bad-JSON guards from the harness | Harness tests |
| Guardrails bypassed | Same prompt layers and input/output guardrails as the harness (docs/05b); traces are post-guardrail (`.data/chat-ui/`) | Harness tests |

**The exfiltration test, in a real browser (Chromium + a local "beacon" server):**
- **Raw model markdown:** the browser **fetched** `/md-image?d=ACC-1001` on its own. The
  attack is real in Streamlit.
- **Sanitised:** **zero** requests.
- Streamlit did not render a raw `<img>` tag (it's shown as text). That's a second layer;
  we don't rely on it.

## 5. How a turn works

```
browser ──► Streamlit (127.0.0.1) ──► session.run_turn
                                        ├─ identity → headers (lab token | shared client token + customer)
                                        ├─ fresh MCP connection (the server is stateless)
                                        └─ harness Agent: prompts → model ⇄ MCP tools → guardrails
                                             model adapters: Claude · Gemini · Azure (llm*.py)
```

Provider details that matter:
- **Claude:** thinking blocks are passed back **unchanged** during a tool loop, and
  tool results go back in one message. A `refusal` stop is checked first, and the
  server-side refusal fallback is on.
- **Gemini:** the model's own content, with its thought signatures, is replayed
  unchanged. Automatic function calling is off, so the agent's guardrails stay in
  charge. Blocked or safety stops become a fixed answer.

## 6. Verified vs not verified

- ✅ Adapters against mocked Claude/Gemini APIs using the **real SDKs** (exact requests,
  thinking/signature replay, grouping, refusals, redirects, env isolation).
- ✅ Chat turns over real HTTP with the real MCP server, both identities (scripted model).
- ✅ The page headlessly (AppTest) and in Chromium (renders; a dummy key gives a clean
  "401: key rejected" hint; exfiltration blocked).
- ❌ **No live conversation with a real Claude or Gemini key** (none available in the
  build environment). The first real run is yours. If Gemini rejects a tool schema
  or the model ID, report back.
- ❌ Streaming responses and a y/N confirmation dialog: not in v1.
