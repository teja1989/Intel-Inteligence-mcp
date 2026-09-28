"""Developer-side connector for LOWER environments (docs/08 §3B).

Lets MCP clients (Claude Code, VS Code, Inspector, …) use the shared lower-env client
credentials without anyone pasting a 2-hour token:

* `headers`: prints the auth headers as JSON, for Claude Code's `headersHelper`
  (re-run by Claude Code on every connection and after a 401).
* `bridge`: a local stdio MCP "server" that forwards every message to the remote
  Streamable HTTP endpoint, adding a fresh token (refreshed before expiry, and once
  more on a 401). For VS Code and any client that can launch a stdio server.
* `check`: fetches a token and lists the tools, printing no secrets.

Not part of the shippable server (the architecture test forbids the server importing
it). The secret comes from a command (e.g. the macOS Keychain), never from a file.
"""
