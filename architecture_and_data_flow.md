# USGS Water Agent: Architecture & Data Flow Specification

> **Document Version**: 2.0  
> **Last Updated**: 2026-10-29  
> **Status**: Current Production Reference  
> **Repository**: `usgs-water-agent`

---

## 1. High-Level System Architecture

The USGS Water Agent is built with a decoupled, dual-modality architecture that coordinates automated sensor time-series with laboratory water quality sample observations. It exposes a single public entrypoint (`USGSWaterAgent`) to the overarching `IChatBio` system while isolating planning, business logic, resilience, and data access behind typed boundaries.

```mermaid
flowchart TD
    %% External Interfaces
    Client["IChatBio Platform / User"]
    USGS_OGC[("USGS OGC API v0<br/>(Continuous Daily Sensors)")]
    USGS_SAMPLES[("USGS Samples API<br/>(Discrete Water Quality Lab Data)")]
    
    %% Core Agent Entrypoint
    subgraph Core System
        Agent["USGSWaterAgent<br/>(Main Entrypoint)"]
        Context["RequestContext & ResultStore<br/>(Out-of-Band Memory)"]
        Planner["QueryPlanner<br/>(LLM Tool Loop with Reasoning Sanitizer)"]
    end

    %% Execution Handlers
    subgraph Handler Layer
        H_Spatial["SpatialDiscoveryHandler"]
        H_Inventory["StationInventoryHandler"]
        H_Count["ParameterCountHandler"]
        H_Obs["ObservationHandler"]
        H_Clarify["ClarificationHandler"]
    end

    %% Metadata & Resolvers
    subgraph Metadata Resolvers
        Resolver_Ref["ReferenceResolver<br/>(FIPS & Daily Queryables)"]
        Resolver_Families["DiscreteFamilyResolver<br/>(Unified Analyte Index: 25 Families)"]
        RefStore[("Local Reference DB<br/>counties.json, state-identifiers.json,<br/>discrete_families.json, etc.")]
    end

    %% Data Access Layer with Circuit Breakers
    subgraph Resilient Data Access Layer
        CB_OGC{"Circuit Breaker<br/>(usgs_ogc_api)"}
        CB_Samples{"Circuit Breaker<br/>(usgs_samples_api)"}
        Fetcher_OGC["USGSWaterDataFetcher<br/>(PagedResult GeoJSON Streamer)"]
        Fetcher_Samples["USGSSamplesFetcher<br/>(CSV Streamer, Count Guard, Partitioning)"]
    end

    %% Client Interactions
    Client -->|Natural Language Query| Agent
    Agent -->|Initializes| Context
    Agent -->|Execute Plan Loop| Planner

    %% Planning & Resolving
    Planner -->|Resolve Controlled Terms| Resolver_Ref
    Planner -->|Resolve Analyte Families| Resolver_Families
    Resolver_Ref --> RefStore
    Resolver_Families --> RefStore

    %% Dispatch to Handlers
    Planner -->|Typed QueryPlan| Agent
    Agent -->|Dispatch| H_Spatial
    Agent -->|Dispatch| H_Inventory
    Agent -->|Dispatch| H_Count
    Agent -->|Dispatch| H_Obs
    Agent -->|Dispatch| H_Clarify

    %% Handlers to Fetchers via Circuit Breakers
    H_Spatial --> Fetcher_OGC & Fetcher_Samples
    H_Inventory --> Fetcher_OGC & Fetcher_Samples
    H_Count --> Fetcher_Samples
    H_Obs --> Fetcher_OGC & Fetcher_Samples

    Fetcher_OGC --> CB_OGC -->|HTTP GET| USGS_OGC
    Fetcher_Samples --> CB_Samples -->|HTTP GET / Streaming CSV| USGS_SAMPLES

    %% Out-of-band Storage
    H_Spatial -->|Stash Large Stations| Context
    H_Inventory -->|Stash Full Inventory| Context
    Context -->|Retrieve via Handles| Agent

    %% Final Output
    Agent -->|Markdown Table + JSON Artifacts| Client
```

---

## 2. The 5 Core Workflows

The agent classifies natural-language queries into 5 typed execution workflows (plus clarification guardrails):

| Workflow | Intent Enum | Modality | Primary Upstream Endpoint | Key Handling Logic |
| :--- | :--- | :--- | :--- | :--- |
| **1. Spatial Discovery** | `spatial_discovery` | `continuous` \| `discrete` | OGC `/collections/daily/items` or Samples `/locations` | Geographic constraint required (state/county). Stashes large station sets out-of-band (`stations_ref`). 0 stations is a definitive valid outcome. |
| **2. Dual Station Inventory** | `station_inventory` | `mixed` | OGC `/collections/daily/items` + Samples `/summary/{station}` | Queries continuous sensor offerings AND discrete laboratory analytical catalogs for a specific station. |
| **3. Station Parameter Counts** | `station_parameter_count` | `discrete` | Samples `/samples-data/results/count` | Expands analyte families into all constituent `major_pcodes` (e.g., 17 codes for phosphorus, 4 for calcium). |
| **4. Discrete Observation Values** | `observation` | `discrete` | Samples `/samples-data/results/narrow` | Streams laboratory measurements as CSV; projects dates, fractions, values, and units into tabular view. |
| **5. Continuous Daily Values** | `observation` | `continuous` | OGC `/collections/daily/items` | Queries sensor streamflow (`00060`), gage height (`00065`), or temperature (`00010`) by statistic (`00003` mean, `00001` max). |
| **6. Clarification Guardrails** | `clarification` | `None` | *None (zero API calls)* | Triggered when geographic constraints are missing or multi-candidate ambiguities exist (e.g. Orange County without state). |

---

## 3. Detailed Data Flow Diagrams

### Data Flow 1: Discrete Observation Counts (Multi-PCode Family Expansion)

This flow illustrates how user queries for broad chemical analytes (e.g. *"How many phosphorus records are at USGS-0423205010?"*) are expanded across all constituent codes and aggregated.

```mermaid
sequenceDiagram
    participant User as User / IChatBio
    participant Agent as USGSWaterAgent
    participant Planner as QueryPlanner
    participant Index as DiscreteFamilyResolver
    participant Handler as ParameterCountHandler
    participant Samples as USGSSamplesFetcher
    participant USGS as USGS Samples API

    User->>Agent: "How many phosphorus records are at USGS-0423205010?"
    Agent->>Planner: plan(query)
    
    Planner->>Index: resolve_discrete_parameter("phosphorus")
    Index-->>Planner: DiscreteFamily(name="phosphorus", 17 constituent pcodes)
    Planner-->>Agent: QueryPlan(intent="station_parameter_count", filters={"monitoring_location_id": "USGS-0423205010", "parameter_code": "phosphorus"})

    Agent->>Handler: execute(plan)
    Handler->>Index: get_family_for_pcode("phosphorus")
    Index-->>Handler: 17 pcodes (00665, 00660, 00671, 70507, ...)

    Handler->>Samples: get_results_count(station="USGS-0423205010", usgsPCode=[17 codes])
    Samples->>USGS: GET /samples-data/results/count?monitoringLocationIdentifier=USGS-0423205010&usgsPCode=00665&usgsPCode=00660...
    USGS-->>Samples: {"recordCount": 12337}
    Samples-->>Handler: 12337

    Handler-->>Agent: Direct markdown table of constituent codes + JSON Artifact
    Agent-->>User: "Found 12,337 total discrete measurements for phosphorus at USGS-0423205010..."
```

---

### Data Flow 2: Out-of-Band State Stashing (`ResultStore`)

Massive datasets (thousands of stations or observations) are intercepted and held in memory via `ResultStore`. The LLM planner only sees opaque references (`stations_ref`), preventing context-window exhaustion and token truncation.

```mermaid
sequenceDiagram
    participant User as User
    participant Agent as USGSWaterAgent
    participant Store as RequestContext & ResultStore
    participant Planner as QueryPlanner
    participant Tool as SpatialQueryTool
    participant Fetcher as USGSWaterDataFetcher
    participant USGS as USGS OGC API

    User->>Agent: "Where is streamflow monitored in Wake County, NC?"
    Agent->>Store: Initialize RequestContext
    Agent->>Planner: plan(query)
    
    Planner->>Tool: spatial_query(parameter_code="00060", state_name="NC", county_name="Wake")
    Tool->>Fetcher: fetch_daily_values(state="37", county="183", pcode="00060")
    Fetcher->>USGS: GET /collections/daily/items (Paginated GeoJSON)
    USGS-->>Fetcher: 113 station records
    Fetcher-->>Tool: PagedResult(features)

    Note over Tool, Store: Saves massive result out-of-band
    Tool->>Store: put_result("stations", 113 stations)
    Store-->>Tool: "stations_ref_abc123"
    Tool-->>Planner: {"ref": "stations_ref_abc123", "count": 113}

    Planner-->>Agent: QueryPlan(intent="spatial_discovery", stations_ref="stations_ref_abc123")
    Agent->>Store: get_result("stations_ref_abc123")
    Store-->>Agent: 113 station records

    Agent->>Agent: Render Markdown summary table + JSON Artifact
    Agent-->>User: "Found 113 monitoring locations in Wake County, NC..."
```

---

## 4. Resilience & Reliability Architecture

### 1. Circuit Breakers (`CircuitBreaker`)
Both upstream data clients (`USGSWaterDataFetcher` and `USGSSamplesFetcher`) are guarded by circuit breakers implementing the state machine:
$$\text{CLOSED} \xrightarrow{\text{5 consecutive failures}} \text{OPEN} \xrightarrow{\text{30s cooldown}} \text{HALF-OPEN} \xrightarrow{\text{1 successful probe}} \text{CLOSED}$$

* **Outage Fast-Fail**: While `OPEN`, any attempt to query upstream fails immediately with `CircuitBreakerOpenError`, protecting downstream thread pools from latency death spirals.
* **Excluded Exceptions**: 4xx client errors (e.g. `USGSValidationError`, `USGSNotFoundError`) do not trip the breaker.

### 2. Provider Reasoning Token Sanitization
When using reasoning models (e.g. `gpt-5.6-luna`), Azure OpenAI emits server-encrypted reasoning tokens (`reasoning_items`, `reasoning_content`). LiteLLM load balancing across Azure endpoints rejects subsequent turns with a `400 Bad Request` if these tokens cannot be decrypted by the secondary instance.
* `_clean_message_payload()` in [`query_planner.py`](file:///c:/Users/shaik/OneDrive/Desktop/Github/usgs-water-agent/src/water_agent/query_planner.py) automatically strips these fields prior to dispatching multi-turn messages.

### 3. Client Timeout Hardening
To eliminate indefinite socket hangs in restricted or high-latency network topologies:
* `AsyncOpenAI`: Configured with defensive `timeout=45.0`.
* `httpx.AsyncClient`: Configured with `timeout=15.0` (continuous) and `timeout=20.0` (discrete streaming).

---

## 5. Architectural Invariants

1. **Spatial Invariant Rule**: For spatial queries of parameter families, the agent always performs a **union** of stations across all constituent codes in that family, never selecting an arbitrary single code.
2. **Modality Disclosure Rule**: All discrete sample responses must explicitly disclose that measurements represent discrete lab samples (not continuous sensors) and identify the sample media (e.g. Water).
3. **Count-First Guard**: Before streaming massive discrete record narrow profiles, the agent queries `/results/count` to guard against unbounded memory allocation.
