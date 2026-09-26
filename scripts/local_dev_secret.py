"""LOCAL STACK ONLY: print the dev token service's client secret from this repo's .env.

Used as TELCO_MCP_SECRET_COMMAND by `make connect-local`, standing in for the OS
keychain. For a real lower environment, read the secret from the keychain instead
(docs/09). Never point this at a real secret.
"""

from pathlib import Path

from dotenv import dotenv_values

secret = dotenv_values(Path(__file__).parents[1] / ".env").get("DEV_TOKEN_SERVICE_CLIENT_SECRET")
if not secret:
    raise SystemExit("DEV_TOKEN_SERVICE_CLIENT_SECRET missing in .env: run `make env-tokens`")
print(secret)
