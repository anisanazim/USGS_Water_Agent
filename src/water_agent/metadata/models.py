from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class QueryIntent(str, Enum):
    OBSERVATION = "observation"
    SPATIAL_DISCOVERY = "spatial_discovery"
    STATION_INVENTORY = "station_inventory"
    STATION_PARAMETER_COUNT = "station_parameter_count"
    CLARIFICATION = "clarification"


class ParameterModality(str, Enum):
    CONTINUOUS = "continuous"
    DISCRETE = "discrete"
    MIXED = "mixed"


class QueryFilters(BaseModel):
    """Validated query filters for USGS APIs with automatic normalization."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    parameter_code: str | None = None
    monitoring_location_id: str | None = None
    state_code: str | None = None
    county_code: str | None = None
    statistic_id: str | None = None
    start_date: str | None = None
    end_date: str | None = None

    @field_validator("parameter_code", mode="before")
    @classmethod
    def normalize_pcode(cls, v: Any) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        if s.isdigit() and len(s) < 5:
            return s.zfill(5)
        return s

    @field_validator("statistic_id", mode="before")
    @classmethod
    def normalize_statistic_id(cls, v: Any) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        if s.isdigit() and len(s) < 5:
            return s.zfill(5)
        return s

    @field_validator("monitoring_location_id", mode="before")
    @classmethod
    def normalize_station_id(cls, v: Any) -> str | None:
        if v is None:
            return None
        return str(v).strip()


class StationLocation(BaseModel):
    """Validated representation of a USGS monitoring location."""
    id: str
    name: str | None = None
    site_type: str | None = None
    county: str | None = None
    state: str | None = None


class ParameterStationBreakdown(BaseModel):
    """Constituent parameter breakdown in spatial discovery."""
    pcode: str
    descriptor: str
    unit: str = ""
    station_count: int = 0


class StationCandidateItem(BaseModel):
    """Station-scoped discrete parameter observation summary."""
    descriptor: str
    count: int
    first_activity: str | None = None
    most_recent_activity: str | None = None
    pcode: str | None = None
    unit: str | None = None


class ReferenceConfig(BaseModel):
    collection: str
    value_field: str
    search_fields: list[str] = Field(default_factory=list)
    filters: dict[str, str] = Field(default_factory=dict)
    """Required property values a feature must match to be indexed/searched.
    e.g. {"country_code": "US"} scopes a globally-shared reference
    collection down to what a specific Daily field actually means."""

class ResolutionResult(BaseModel):
    status: Literal["resolved", "not_found", "ambiguous", "clarification_needed"]
    input_value: str
    resolved_value: str | None = None
    matched_field: str | None = None
    candidates: list[dict] = Field(default_factory=list)
    resolved_values: list[str] = Field(default_factory=list)
    action: str | None = None
    dominant_value: str | None = None
    unit_of_measure: str | None = None
    modality: str | None = None
    message: str | None = None

class QueryableConfig(BaseModel):
    semantic_type: str
    reference: ReferenceConfig | None = None

class QueryableDefinition(BaseModel):
    name: str
    title: str | None = None
    description: str | None = None
    data_type: str | None = None
    format: str | None = None
    ogc_role: str | None = Field(
        default=None,
        alias="x-ogc-role",
    )

    user_filterable: bool = False

    semantic_type: str | None = None

    reference: ReferenceConfig | None = None


def parse_queryable(
    name: str,
    definition: dict,
) -> QueryableDefinition:
    return QueryableDefinition(
        name=name,
        title=definition.get("title"),
        description=definition.get("description"),
        data_type=definition.get("type"),
        format=definition.get("format"),
        **{
            "x-ogc-role": definition.get("x-ogc-role"),
        },
    )