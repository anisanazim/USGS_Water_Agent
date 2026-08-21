from pydantic import BaseModel, Field
from typing import Literal


class ReferenceConfig(BaseModel):
    collection: str
    value_field: str
    search_fields: list[str] = Field(default_factory=list)

class ResolutionResult(BaseModel):
    status: Literal["resolved", "not_found", "ambiguous"]
    input_value: str
    resolved_value: str | None = None
    matched_field: str | None = None
    candidates: list[dict] = Field(default_factory=list)

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