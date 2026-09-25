"""Transparent stdio proxy that shows every JSON-RPC message on the wire.

    client (Inspector / demo)  <-stdin/stdout->  stdio_trace.py  <-stdin/stdout->  MCP server

Usage (a client launches THIS instead of the server):
    uv run python scripts/stdio_trace.py uv run python -m telco_mcp_lab.mcp_server
    (a leading `--` is accepted but not required; MCP Inspector's CLI reserves `--`)

* Messages are relayed byte for byte; the proxy never changes traffic.
* Each message is pretty-printed to stderr (the client ignores or shows it) and
  appended to .data/traces/stdio-<timestamp>.jsonl for later reading.
* Traces contain full payloads, so they are lab-only (synthetic data,
  gitignored). A production audit log must NOT record payloads (see Phase 3).
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO

TRACE_DIR = Path(".data/traces")
_lock = threading.Lock()
_COLOR = {"C→S": "\033[36m", "S→C": "\033[33m"}
_RESET = "\033[0m"


def _log(direction: str, raw: bytes, trace: IO[str]) -> None:
    text = raw.decode("utf-8", errors="replace").rstrip("\n")
    try:
        msg = json.loads(text)
        pretty = json.dumps(msg, indent=2, ensure_ascii=False)
        kind = (
            f"request {msg['method']} (id={msg['id']})"
            if "method" in msg and "id" in msg
            else f"notification {msg['method']}"
            if "method" in msg
            else f"{'error' if 'error' in msg else 'response'} (id={msg.get('id')})"
        )
    except (json.JSONDecodeError, TypeError, KeyError):
        msg, pretty, kind = None, text, "⚠️ NOT JSON: protocol violation on stdout!"
    with _lock:
        color = _COLOR[direction] if sys.stderr.isatty() else ""
        sys.stderr.write(f"{color}── {direction} {kind}{_RESET if color else ''}\n{pretty}\n")
        sys.stderr.flush()
        trace.write(json.dumps({"t": time.time(), "dir": direction, "msg": msg or text}) + "\n")
        trace.flush()


def _pump(src: IO[bytes], dst: IO[bytes], direction: str, trace: IO[str]) -> None:
    for line in iter(src.readline, b""):
        _log(direction, line, trace)
        dst.write(line)
        dst.flush()
    dst.close()  # propagate EOF: closing stdin is the stdio shutdown signal


def main() -> int:
    cmd = sys.argv[1:]
    if cmd[:1] == ["--"]:
        cmd = cmd[1:]
    if not cmd:
        sys.exit("usage: stdio_trace.py <server command...>")
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    path = TRACE_DIR / f"stdio-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    sys.stderr.write(f"[stdio_trace] launching {' '.join(cmd)}\n[stdio_trace] trace → {path}\n")

    with path.open("w", encoding="utf-8") as trace:
        # stderr is inherited, so server logs still show up (and never touch stdout).
        child = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)  # noqa: S603
        assert child.stdin and child.stdout
        up = threading.Thread(
            target=_pump, args=(sys.stdin.buffer, child.stdin, "C→S", trace), daemon=True
        )
        up.start()
        _pump(child.stdout, sys.stdout.buffer, "S→C", trace)
        return child.wait()


if __name__ == "__main__":
    sys.exit(main())
