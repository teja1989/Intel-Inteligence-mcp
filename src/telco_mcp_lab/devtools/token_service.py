"""DEV ONLY: a local stand-in for the internal token service (OAuth 2.0 client credentials).

    POST /oauth/token        grant_type=client_credentials, client auth via HTTP Basic
                             (client_secret_basic) or form fields (client_secret_post)
    GET  /.well-known/jwks.json

One client, configured from the environment / .env:
    DEV_TOKEN_SERVICE_CLIENT_ID      (default lowerenv-shared)
    DEV_TOKEN_SERVICE_CLIENT_SECRET  (required; `make env-tokens` generates one)
    DEV_TOKEN_SERVICE_SCOPES         (default "read"; a request may ask for fewer)

Tokens are signed with the dev key from `make dev-keys`, so the MCP server started
with `make mcp-http-jwt` accepts them. Binds to 127.0.0.1 only.

Mirrors the real service's contract as far as we know it; confirm the real token
endpoint's request format (docs/07 §6) and adjust TELCO_MCP_CLIENT_AUTH to match.
"""

import argparse
import base64
import hmac
import logging
import os
from binascii import Error as B64Error

import uvicorn
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from dotenv import dotenv_values
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from telco_mcp_lab.devtools.token_issuer import DEV_AUDIENCE, DEV_ISSUER, jwks_for, load_key, mint

log = logging.getLogger("dev_token_service")


def _error(status: int, error: str, **headers: str) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status, headers=headers or None)


def _client_auth(request: Request, form: dict[str, str]) -> tuple[str, str] | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            cid, _, secret = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (B64Error, UnicodeDecodeError):
            return None
        return cid, secret
    if "client_id" in form and "client_secret" in form:
        return form["client_id"], form["client_secret"]
    return None


def create_app(
    key: RSAPrivateKey,
    *,
    client_id: str,
    client_secret: str,
    scopes: str = "read",
    issuer: str = DEV_ISSUER,
    audience: str = DEV_AUDIENCE,
    ttl_s: int = 7200,
) -> Starlette:
    if len(client_secret) < 24:
        raise ValueError("DEV_TOKEN_SERVICE_CLIENT_SECRET must be at least 24 characters")
    allowed = set(scopes.split())

    async def token(request: Request) -> JSONResponse:
        form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
        creds = _client_auth(request, form)
        # Compare both, always, so timing doesn't reveal which one was wrong.
        ok_id = creds is not None and hmac.compare_digest(creds[0], client_id)
        ok_secret = creds is not None and hmac.compare_digest(creds[1], client_secret)
        if not (ok_id and ok_secret):
            log.warning("invalid client credentials")
            return _error(401, "invalid_client", **{"WWW-Authenticate": 'Basic realm="dev"'})
        if form.get("grant_type") != "client_credentials":
            return _error(400, "unsupported_grant_type")
        requested = set(form.get("scope", "").split()) or allowed
        if not requested <= allowed:
            return _error(400, "invalid_scope")
        granted = " ".join(sorted(requested))
        access = mint(key, client_id=client_id, scopes=granted, issuer=issuer,
                      audience=audience, ttl_s=ttl_s)  # fmt: skip
        log.info("issued token client=%s scope=%r ttl=%ss", client_id, granted, ttl_s)
        return JSONResponse(
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": ttl_s,
                "scope": granted,
            },  # fmt: skip
            headers={"Cache-Control": "no-store"},  # RFC 6749 §5.1
        )

    async def jwks(_: Request) -> JSONResponse:
        return JSONResponse(jwks_for(key))

    return Starlette(
        routes=[
            Route("/oauth/token", token, methods=["POST"]),
            Route("/.well-known/jwks.json", jwks, methods=["GET"]),
        ]
    )


def main() -> None:
    p = argparse.ArgumentParser(prog="dev_token_service", description=__doc__.split("\n\n")[0])
    p.add_argument("--port", type=int, default=8095)
    p.add_argument("--ttl", type=int, default=7200)
    a = p.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    env = {**{k: v for k, v in dotenv_values(".env").items() if v is not None}, **os.environ}
    secret = env.get("DEV_TOKEN_SERVICE_CLIENT_SECRET", "")
    if not secret:
        raise SystemExit("DEV_TOKEN_SERVICE_CLIENT_SECRET is not set: run `make env-tokens`")
    app = create_app(
        load_key(),
        client_id=env.get("DEV_TOKEN_SERVICE_CLIENT_ID", "lowerenv-shared"),
        client_secret=secret,
        scopes=env.get("DEV_TOKEN_SERVICE_SCOPES", "read"),
        audience=env.get("MCP_JWT_AUDIENCE") or DEV_AUDIENCE,
        ttl_s=a.ttl,
    )
    log.info("dev token service on http://127.0.0.1:%s/oauth/token (DEV ONLY)", a.port)
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
