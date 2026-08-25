from water_agent.metadata.models import QueryableConfig, ReferenceConfig

DAILY_FIELD_CONFIG = {
    "parameter_code": QueryableConfig(
        semantic_type="controlled_reference",
        reference=ReferenceConfig(
            collection="parameter-codes",
            value_field="id",
            search_fields=[
                "parameter_name",
                "parameter_description",
                "unit_of_measure",
            ],
        ),
    ),

    "aquifer_code": QueryableConfig(
        semantic_type="controlled_reference",
        reference=ReferenceConfig(
            collection="aquifer-codes",
            value_field="id",
            search_fields=[
                "aquifer_name",
            ],
        ),
    ),

    "aquifer_type_code": QueryableConfig(
        semantic_type="controlled_reference",
        reference=ReferenceConfig(
            collection="aquifer-types",
            value_field="id",
            search_fields=[
                "aquifer_type_description",
            ],
        ),
    ),
    
    "statistic_id": QueryableConfig(
        semantic_type="controlled_reference",
        reference=ReferenceConfig(
            collection="statistic-codes",
            value_field="id",
            search_fields=[
                "statistic_name",
                "statistic_description",
                "id",
            ],
        ),
    ),

    "state_code": QueryableConfig(
        semantic_type="controlled_reference",
        reference=ReferenceConfig(
            collection="state-identifiers",
            value_field="state_fips_code",
            search_fields=[
                "state_name",
                "state_postal_code",
                "state_fips_code",
            ],
            # state-identifiers is a global country+subdivision table (424
            # entries, only 65 of them US); without this, "37" is ambiguous
            # between North Carolina and a Dominican Republic province that
            # reuses the same FIPS-style number. Scoped to US-only for now.
            filters={"country_code": "US"},
        ),
    ),

    "county_code": QueryableConfig(
        semantic_type="controlled_reference",
        reference=ReferenceConfig(
            collection="counties",
            value_field="county_fips_code",
            search_fields=[
                "county_name",
                "county_fips_code",
            ],
        ),
    ),

    "agency_code": QueryableConfig(
        semantic_type="controlled_reference",
        reference=ReferenceConfig(
            collection="agency-codes",
            value_field="id",
            search_fields=[
                "agency_name",
            ],
        ),
    ),

    "site_type_code": QueryableConfig(
        semantic_type="controlled_reference",
        reference=ReferenceConfig(
            collection="site-types",
            value_field="id",
            search_fields=[
                "site_type_name",
                "site_type_description",
            ],
        ),
    ),

    "hydrologic_unit_code": QueryableConfig(
        semantic_type="controlled_reference",
        reference=ReferenceConfig(
            collection="hydrologic-unit-codes",
            value_field="id",
            search_fields=[
                "hydrologic_unit_name",
                "hydrologic_unit_classification_code",
                "id",
            ],
        ),
    ),

    "national_aquifer_code": QueryableConfig(
        semantic_type="controlled_reference",
        reference=ReferenceConfig(
            collection="national-aquifer-codes",
            value_field="id",
            search_fields=[
                "national_aquifer_name",
                "id",
            ],
        ),
    ),

    "country_code": QueryableConfig(
        semantic_type="controlled_reference",
        reference=ReferenceConfig(
            collection="country-identifiers",
            value_field="id",
            search_fields=[
                "country_name",
                "id",
            ],
        ),
    ),

    "monitoring_location_id": QueryableConfig(
        semantic_type="entity_reference",
        reference=ReferenceConfig(
            collection="monitoring-locations",
            value_field="id",
            search_fields=[
                "monitoring_location_name",
                "monitoring_location_number",
            ],
        ),
    ),
}
 