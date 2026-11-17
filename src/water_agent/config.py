# src/water_agent/config.py
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Optional

from pydantic import Field, SecretStr, field_validator
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

    # Timeouts, Heartbeats & Retries
    # Note on iChatBio's 60 s idle rule: the iChatBio backend times out an agent only after
    # 60 s WITHOUT receiving any message, and every message (including process.log heartbeats)
    # resets that timer. Because heartbeat_interval_seconds (default 20 s) keeps the stream
    # active well under the 60 s idle limit, request_deadline_seconds (300 s) and
    # samples_timeout_seconds (240 s) are total wait caps for cold USGS wake-ups—do NOT
    # "fix" these back down to < 60 s.
    heartbeat_interval_seconds: float = Field(
        default=20.0,
        validation_alias="HEARTBEAT_INTERVAL_SECONDS",
        description="Interval in seconds between process.log heartbeats (must stay well below iChatBio's 60 s idle timeout)",
    )
    request_deadline_seconds: float = Field(
        default=300.0,
        validation_alias="REQUEST_DEADLINE_SECONDS",
        description="Overall total request wait cap in seconds (heartbeats reset iChatBio's 60 s idle timer)",
    )
    request_timeout_seconds: float = Field(
        default=15.0,
        validation_alias="USGS_REQUEST_TIMEOUT",
        description="Timeout for continuous OGC API requests in seconds",
    )
    samples_timeout_seconds: float = Field(
        default=240.0,
        validation_alias="USGS_SAMPLES_TIMEOUT",
        description="Timeout for discrete samples API requests in seconds (allows cold USGS wake-up under heartbeats)",
    )
    max_attempts: int = Field(
        default=2,
        validation_alias="USGS_MAX_ATTEMPTS",
        description="Maximum retry attempts for transient errors",
    )
    max_warmups: int = Field(
        default=3,
        validation_alias="MAX_WARMUPS",
        description="Maximum concurrent warm-up requests across all stations",
    )
    keepwarm_enabled: bool = Field(
        default=True,
        validation_alias="KEEPWARM_ENABLED",
        description="Whether background keep-warm loop is enabled",
    )
    keepwarm_interval_seconds: float = Field(
        default=300.0,
        validation_alias="KEEPWARM_INTERVAL_SECONDS",
        description="Interval in seconds between keep-warm rounds",
    )
    keepwarm_targets: list[str] = Field(
        default_factory=lambda: ["USGS-0423205010:00400,00403"],
        validation_alias="KEEPWARM_TARGETS",
        description="Target stations and pcodes for keep-warm pings",
    )

    @field_validator("keepwarm_targets", mode="before")
    @classmethod
    def _parse_keepwarm_targets(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("[") and v.endswith("]"):
                try:
                    import json
                    parsed = json.loads(v)
                    if isinstance(parsed, list):
                        return [str(x).strip() for x in parsed if str(x).strip()]
                except Exception:
                    pass
            return [entry.strip() for entry in v.split(";") if entry.strip()]
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if str(x).strip()]
        return v

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