# src/water_agent/config.py
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Optional

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class WaterAgentSettings(BaseSettings):
    """Centralized, validated configuration for the USGS Water Data Agent."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # USGS API Configuration
    usgs_api_key: Optional[SecretStr] = Field(
        default=None,
        validation_alias="USGS_API_KEY",
        description="USGS Water Data API key for elevated rate limits",
    )
    usgs_api_version: str = Field(
        default="v0",
        validation_alias="USGS_API_VERSION",
        description="USGS OGC API version endpoint",
    )
    ogc_base_url: str = Field(
        default="https://api.waterdata.usgs.gov/ogcapi",
        validation_alias="USGS_OGC_BASE_URL",
        description="Base URL for USGS OGC API",
    )
    samples_base_url: str = Field(
        default="https://api.waterdata.usgs.gov/samples-data",
        validation_alias="USGS_SAMPLES_BASE_URL",
        description="Base URL for USGS discrete samples API",
    )

    # OpenAI / LLM Configuration
    openai_api_key: Optional[SecretStr] = Field(
        default=None,
        validation_alias="OPENAI_API_KEY",
        description="OpenAI API key for the query planning loop",
    )
    openai_base_url: Optional[str] = Field(
        default=None,
        validation_alias="OPENAI_BASE_URL",
        description="Optional custom base URL for OpenAI-compatible proxies",
    )
    openai_model: str = Field(
        default="gpt-5.6-luna",
        validation_alias="OPENAI_MODEL",
        description="Model name to use for planning loop",
    )

    # Timeouts & Retries
    request_timeout_seconds: float = Field(
        default=15.0,
        validation_alias="USGS_REQUEST_TIMEOUT",
        description="Timeout for continuous OGC API requests in seconds",
    )
    samples_timeout_seconds: float = Field(
        default=45.0,
        validation_alias="USGS_SAMPLES_TIMEOUT",
        description="Timeout for discrete samples API requests in seconds",
    )
    max_attempts: int = Field(
        default=3,
        validation_alias="USGS_MAX_ATTEMPTS",
        description="Maximum retry attempts for transient errors",
    )

    # Circuit Breaker Configuration
    circuit_breaker_failure_threshold: int = Field(
        default=5,
        validation_alias="CIRCUIT_BREAKER_FAILURE_THRESHOLD",
        description="Consecutive failure threshold before tripping circuit breaker",
    )
    circuit_breaker_recovery_timeout: float = Field(
        default=30.0,
        validation_alias="CIRCUIT_BREAKER_RECOVERY_TIMEOUT",
        description="Recovery probe timeout in seconds for open circuit breaker",
    )

    @property
    def usgs_api_key_str(self) -> str | None:
        if self.usgs_api_key is None:
            return None
        val = self.usgs_api_key.get_secret_value().strip()
        return val if val else None

    @property
    def openai_api_key_str(self) -> str | None:
        if self.openai_api_key is None:
            return None
        val = self.openai_api_key.get_secret_value().strip()
        return val if val else None

    @property
    def full_ogc_base_url(self) -> str:
        return f"{self.ogc_base_url.rstrip('/')}/{self.usgs_api_version}"


@lru_cache(maxsize=1)
def get_settings() -> WaterAgentSettings:
    """Return cached singleton instance of WaterAgentSettings."""
    return WaterAgentSettings()


# Global instance and backward-compatible module variables
settings: WaterAgentSettings = get_settings()
USGS_API_KEY: str | None = settings.usgs_api_key_str
USGS_API_VERSION: str = settings.usgs_api_version


def require_api_key() -> str:
    key = get_settings().usgs_api_key_str
    if not key:
        raise RuntimeError(
            "USGS_API_KEY is not set. Copy .env.example to .env and add your key "
            "from https://api.waterdata.usgs.gov/signup/"
        )
    return key