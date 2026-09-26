"""A LOCAL stand-in for the internal token service: RSA keys, a JWKS file, and minted tokens.

DEV ONLY. It exists so MCP_AUTH_MODE=jwt can be run and tested without the real
token service. The real one owns the private key; this server only ever needs
the public JWKS.

    uv run python -m telco_mcp_lab.devtools.token_issuer keys
    uv run python -m telco_mcp_lab.devtools.token_issuer mint --client care-agent-internal \\
        --scopes "read pii:read"

The private key is written to .data/dev-keys (gitignored) with 0600 permissions.
Tokens mimic a client-credentials JWT: iss, aud, client_id, scope, iat, exp, jti.
Match the claim names to your real tokens via MCP_JWT_* settings.
"""

import argparse
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

KEY_DIR = Path(".data/dev-keys")
DEV_ISSUER = "https://token-service.dev.invalid"
DEV_AUDIENCE = "http://127.0.0.1:8090/mcp"
DEV_KID = "dev-key-1"


def generate_key() -> RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks_for(key: RSAPrivateKey, kid: str = DEV_KID) -> dict[str, Any]:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {"keys": [{**jwk, "kid": kid, "use": "sig", "alg": "RS256"}]}


def mint(
    key: RSAPrivateKey,
    *,
    client_id: str,
    scopes: str,
    issuer: str = DEV_ISSUER,
    audience: str = DEV_AUDIENCE,
    ttl_s: int = 7200,
    kid: str = DEV_KID,
    now: float | None = None,
    extra: dict[str, Any] | None = None,
    algorithm: str = "RS256",
) -> str:
    iat = int(now if now is not None else time.time())
    claims = {
        "iss": issuer,
        "aud": audience,
        "client_id": client_id,
        "sub": client_id,
        "scope": scopes,
        "iat": iat,
        "exp": iat + ttl_s,
        "jti": uuid.uuid4().hex,
        **(extra or {}),
    }
    return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": kid})


def write_keys(directory: Path = KEY_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    key = generate_key()
    private = directory / "private.pem"
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    jwks = directory / "jwks.json"
    jwks.write_text(json.dumps(jwks_for(key), indent=2), encoding="utf-8")
    return jwks


def load_key(directory: Path = KEY_DIR) -> RSAPrivateKey:
    path = directory / "private.pem"
    if not path.exists():
        raise SystemExit(f"{path} not found: run `make dev-keys` first")
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    assert isinstance(key, RSAPrivateKey)  # noqa: S101 - we wrote it
    return key


def main() -> None:
    p = argparse.ArgumentParser(prog="token_issuer", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keys", help="create a new RSA key pair and JWKS in .data/dev-keys")
    m = sub.add_parser("mint", help="print a signed access token")
    m.add_argument("--client", required=True)
    m.add_argument("--scopes", default="read")
    m.add_argument("--ttl", type=int, default=7200)
    m.add_argument("--issuer", default=os.environ.get("MCP_JWT_ISSUER", DEV_ISSUER))
    m.add_argument("--audience", default=os.environ.get("MCP_JWT_AUDIENCE", DEV_AUDIENCE))
    a = p.parse_args()
    if a.cmd == "keys":
        print(f"wrote {write_keys()} (public) and {KEY_DIR / 'private.pem'} (private, 0600)")
        return
    print(
        mint(
            load_key(),
            client_id=a.client,
            scopes=a.scopes,
            ttl_s=a.ttl,
            issuer=a.issuer,
            audience=a.audience,
        )
    )


if __name__ == "__main__":
    main()
