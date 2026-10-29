from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field


class DictAccessibleModel(BaseModel):
    """Base model that provides typed attributes and transparent dict-like subscripting
    for complete backward compatibility with legacy consumers and test fixtures."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    def __getitem__(self, item: str) -> Any:
        try:
            return getattr(self, item)
        except AttributeError:
            if self.model_extra and item in self.model_extra:
                return self.model_extra[item]
            raise KeyError(item)

    def __setitem__(self, key: str, value: Any) -> None:
        setattr(self, key, value)

    def get(self, item: str, default: Any = None) -> Any:
        try:
            val = getattr(self, item)
            return default if val is None else val
        except AttributeError:
            if self.model_extra and item in self.model_extra:
                return self.model_extra[item]
            return default

    def __contains__(self, item: str) -> bool:
        if self.model_extra is not None and item in self.model_extra:
            return True
        if item in type(self).model_fields:
            return getattr(self, item) is not None
        return hasattr(self, item) and getattr(self, item) is not None


class SpatialStationDTO(DictAccessibleModel):
    """Normalized station representation returned by spatial query tools."""

    id: str
    name: Optional[str] = None
    site_type: Optional[str] = None
    county: Optional[str] = None
    state: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class ParameterBreakdownItem(DictAccessibleModel):
    """Constituent parameter breakdown entry in spatial discovery."""

    pcode: str
    descriptor: str
    unit: str = ""
    station_count: int = 0


class SpatialQueryResult(DictAccessibleModel):
    """Strongly-typed boundary contract for spatial station discovery."""

    stations: list[Any] = Field(default_factory=list)
    request_url: str = ""
    metadata_url: str = ""
    source_urls: list[str] = Field(default_factory=list)
    modality: str = "continuous"
    family: Optional[str] = None
    pcodes: list[str] = Field(default_factory=list)
    pcode_breakdown: list[ParameterBreakdownItem] = Field(default_factory=list)
    mixed_modality: bool = False
    summary: str = ""
    error: Optional[str] = None

    @property
    def station_ids(self) -> list[str]:
        """Extract pure station IDs regardless of whether stations are strings, dicts, or DTOs."""
        ids = []
        for s in self.stations:
            if isinstance(s, str):
                ids.append(s)
            elif isinstance(s, dict):
                sid = s.get("id") or s.get("location_id")
                if sid:
                    ids.append(str(sid))
            elif hasattr(s, "id") and getattr(s, "id"):
                ids.append(str(getattr(s, "id")))
        return ids


class ContinuousParameterInventory(DictAccessibleModel):
    """Continuous sensor monitoring metadata for a parameter at a station."""

    name: str
    begin: str = "Unknown"
    end: str = "Unknown"
    estimated_daily_records: Optional[int] = None


class DiscreteCharacteristicItem(DictAccessibleModel):
    """Discrete laboratory constituent inventory record."""

    characteristic: str = ""
    descriptor: str = ""
    count: int = 0
    first_activity: str = ""
    most_recent_activity: str = ""


class StationInventoryResult(DictAccessibleModel):
    """Strongly-typed dual-modality station inventory boundary contract."""

    monitoring_location_id: str
    parameters: dict[str, ContinuousParameterInventory] = Field(default_factory=dict)
    discrete_parameters: dict[str, list[DiscreteCharacteristicItem]] = Field(default_factory=dict)
    discrete_total_observations: int = 0
    request_url: str = ""
    discrete_summary_url: str = ""
    error: Optional[str] = None


class ContinuousDailyReading(DictAccessibleModel):
    """Validated continuous daily statistical observation record."""

    monitoring_location_id: str
    parameter_code: str
    statistic_id: Optional[str] = None
    value: Optional[float] = None
    unit: Optional[str] = None
    time: Optional[str] = None
    approval_status: Optional[str] = None
    raw_properties: dict[str, Any] = Field(default_factory=dict)


class DiscreteObservationRecord(DictAccessibleModel):
    """Validated discrete laboratory analytical measurement record."""

    activity_date: Optional[str] = None
    activity_time: Optional[str] = None
    result_value: Optional[Any] = None
    unit: Optional[str] = None
    pcode: Optional[str] = None
    characteristic_name: Optional[str] = None
    detection_condition: Optional[str] = None
    detection_limit: Optional[str] = None
    location_id: Optional[str] = None
    location_name: Optional[str] = None
    sample_fraction: Optional[str] = None
