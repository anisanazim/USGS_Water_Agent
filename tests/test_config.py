from __future__ import annotations

import pytest
from pydantic import SecretStr

from water_agent.config import WaterAgentSettings, get_settings, require_api_key


def test_default_settings():
    s = WaterAgentSettings()
    assert s.usgs_api_version == "v0"
    assert s.request_timeout_seconds == 15.0
    assert s.samples_timeout_seconds == 45.0
    assert s.max_attempts == 3
    assert s.openai_model == "gpt-5.6-luna"
    assert s.full_ogc_base_url == "https://api.waterdata.usgs.gov/ogcapi/v0"


def test_secret_masking(monkeypatch):
    monkeypatch.setenv("USGS_API_KEY", "super-secret-usgs-token")
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-openai-token")

    s = WaterAgentSettings()
    assert s.usgs_api_key_str == "super-secret-usgs-token"
    assert s.openai_api_key_str == "super-secret-openai-token"

    # Verify secrets are NOT leaked in repr or str
    assert "super-secret-usgs-token" not in repr(s)
    assert "super-secret-openai-token" not in repr(s)
    assert "**********" in str(s.usgs_api_key)


def test_env_override(monkeypatch):
    monkeypatch.setenv("USGS_API_VERSION", "v1")
    monkeypatch.setenv("USGS_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("USGS_REQUEST_TIMEOUT", "25.5")

    s = WaterAgentSettings()
    assert s.usgs_api_version == "v1"
    assert s.max_attempts == 5
    assert s.request_timeout_seconds == 25.5
    assert s.full_ogc_base_url == "https://api.waterdata.usgs.gov/ogcapi/v1"


def test_require_api_key(monkeypatch):
    monkeypatch.setenv("USGS_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="USGS_API_KEY is not set"):
        require_api_key()

    monkeypatch.setenv("USGS_API_KEY", "valid-key-123")
    get_settings.cache_clear()
    assert require_api_key() == "valid-key-123"
    get_settings.cache_clear()
