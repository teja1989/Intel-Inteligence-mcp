"""Entry point: `uv run python -m telco_mcp_lab.mcp_server [--transport stdio]`.

stdio golden rule: stdout belongs to the protocol. The spec says the server
MUST NOT write anything to stdout that is not a valid MCP message, so a stray
print() corrupts the stream. All logging goes to stderr.
"""

import argparse
import logging
import sys

from telco_mcp_lab.mcp_server.server import build_server


def main() -> None:
    parser = argparse.ArgumentParser(prog="telco-mcp-server")
    parser.add_argument("--transport", choices=["stdio"], default="stdio")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        stream=sys.stderr,
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    build_server().run(transport=args.transport)


if __name__ == "__main__":
    main()
