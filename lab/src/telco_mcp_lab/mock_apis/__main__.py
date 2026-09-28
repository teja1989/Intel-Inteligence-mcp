"""Entry point: `uv run python -m telco_mcp_lab.mock_apis`."""

import uvicorn

from telco_mcp_lab.mock_apis.app import create_app
from telco_mcp_lab.mock_apis.settings import MockApiSettings


def main() -> None:
    settings = MockApiSettings()  # type: ignore[call-arg]
    # Binds to 127.0.0.1 by default. Never expose mocks on 0.0.0.0.
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
