"""A server with a deliberately misbehaving tool that print()s. Test helper only."""

from mcp.server import MCPServer

mcp = MCPServer("printing")


@mcp.tool()
def noisy() -> str:
    print("STRAY-PRINT-FROM-TOOL")  # the classic stdio bug  # noqa: T201
    return "ok"


if __name__ == "__main__":
    mcp.run(transport="stdio")
