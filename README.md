# USGS Water Data Agent

This agent project built to fetch and enrich water monitoring data from the USGS Water Data APIs.

The initial implementation focuses on a single site and the `latest-continuous` endpoint.

## Scope for v1

- single site only
- site ID based lookup
- artifact enrichment support

## Features

- fetch the latest continuous observations for a USGS site
- support one or more parameter codes
- enrich artifact records with `usgsWaterProperties`
- return normalized output for downstream workflows

## Example request model

- `site_id`: `01646500`
- `parameter_codes`: `["00060", "00065"]`
- optional `locations_artifact`: a JSON artifact containing site IDs

## Supported endpoint

- `GET /collections/latest-continuous/items`
- base URL: `https://api.waterdata.usgs.gov/ogcapi/v0`
