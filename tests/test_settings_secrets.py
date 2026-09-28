"""Settings errors must never echo secrets.

Finding (2026-09-28): pydantic-settings passes EVERY `.env` entry into a settings
model's input, even ones for other prefixes that it then ignores. An error raised by
a validator printed that input, e.g. `DEV_TOKEN_SERVICE_CLIENT_SECRET`, in the
startup error. Every settings class now sets `hide_input_in_errors=True`.
"""

import importlib
import inspect
import pkgutil

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings

import telco_mcp
import telco_mcp_lab

SECRET = "SECRET-in-dotenv-0123456789abcdef"  # noqa: S105 - test marker, not a credential


def settings_classes() -> list[type[BaseSettings]]:
    found: dict[str, type[BaseSettings]] = {}
    for pkg in (telco_mcp, telco_mcp_lab):
        for mod in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
            if mod.name.endswith("__main__"):
                continue
            module = importlib.import_module(mod.name)
            for obj in vars(module).values():
                ours = inspect.isclass(obj) and obj.__module__ == module.__name__
                if ours and issubclass(obj, BaseSettings):
                    found[f"{obj.__module__}.{obj.__name__}"] = obj
    return list(found.values())


def test_there_are_settings_classes_to_check():
    assert len(settings_classes()) >= 8


@pytest.mark.security
@pytest.mark.parametrize("cls", settings_classes(), ids=lambda c: c.__name__)
def test_every_settings_class_hides_input_in_errors(cls):
    assert cls.model_config.get("hide_input_in_errors") is True


@pytest.fixture
def dotenv_with_secrets(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        f"GATEWAY_TOKEN={SECRET}\nDEV_TOKEN_SERVICE_CLIENT_SECRET={SECRET}\n"
        "MCP_JWT_ISSUER=https://issuer.invalid\nMCP_JWT_AUDIENCE=aud\n"
    )
    monkeypatch.chdir(tmp_path)


@pytest.mark.security
@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: __import__("telco_mcp.settings", fromlist=["x"]).JwtSettings(),
                     id="jwt-model-validator"),
        pytest.param(lambda: __import__("telco_mcp.endpoints", fromlist=["x"]).GatewayEndpoints(
            get_order="/x"), id="endpoints"),
        pytest.param(lambda: __import__("telco_mcp.clients.gateway", fromlist=["x"])
                     .GatewayClientSettings(base_url="ftp://x"), id="gateway-field-validator"),
    ],
)  # fmt: skip
def test_validation_errors_do_not_contain_dotenv_secrets(dotenv_with_secrets, build):
    with pytest.raises(ValidationError) as exc:
        build()
    # What gets logged (str/repr) is clean. `exc.errors()` still carries the raw input
    # (pydantic keeps it): never log it without include_input=False (coding-style skill).
    assert SECRET not in str(exc.value) and SECRET not in repr(exc.value)
    assert SECRET not in repr(exc.value.errors(include_input=False))
