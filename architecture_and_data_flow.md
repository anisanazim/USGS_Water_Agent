# USGS Water Agent Architecture

Here is the high-level architecture of the USGS Water Agent project. It adheres to the constraint of exposing a **single public entrypoint** (`USGSAgent`) to the overarching `IChatBio` system, while orchestrating various internal components behind the scenes.

```mermaid
flowchart TD
    %% External Interfaces
    Client[IChatBio / End User] 
    USGS_API[(USGS OGC APIs v0)]
    
    %% Main Entrypoint
    subgraph Core System
        Agent[USGSAgent]
        Context[RequestContext & ResultStore]
        Planner[QueryPlanner]
    end

    %% Agent Tools
    subgraph Tool Layer
        Spatial[SpatialQueryTool]
        Inventory[StationInventoryTool]
        RefResolver[ReferenceResolverTool]
    end
    
    RefDB[(Local Reference DB)]

    %% Data Access Layer
    subgraph Data Layer
        Fetcher[USGSWaterDataFetcher<br/>Returns PagedResult]
    end

    %% Relationships
    Client -->|Natural Language Query| Agent
    Agent -->|Initializes| Context
    Agent -->|Execute| Planner
    
    Planner -->|Provides Tools| RefResolver
    Planner -->|Provides Tools| Spatial
    Planner -->|Provides Tools| Inventory
    
    RefResolver -->|Semantic Mapping| RefDB
    
    Spatial -->|Fetch Metadata| Fetcher
    Inventory -->|Fetch Metadata| Fetcher
    Agent -->|Direct Fetch (Daily Obs)| Fetcher
    
    Spatial -->|Stash Large Results| Context
    Inventory -->|Stash Large Results| Context
    Context -->|Retrieve Stashed Results| Agent
    
    Fetcher -->|HTTP GET (Paginated)| USGS_API
    
    Agent -->|Structured Output & Artifacts| Client
```

---

## Data Flow Diagram: Out-of-Band State Stashing

The following sequence diagram illustrates the data flow for a request resulting in massive payloads (e.g., thousands of stations from a Spatial Query). It highlights the `ResultStore` and `RequestContext` patterns that prevent the LLM from hallucinating or truncating large datasets by keeping them strictly out-of-band.

```mermaid
sequenceDiagram
    participant User as IChatBio / User
    participant Agent as USGSAgent
    participant Store as RequestContext & ResultStore
    participant Planner as QueryPlanner
    participant Tools as Spatial/Inventory Tools
    participant Fetcher as Data Fetcher (Paged)
    participant USGS as USGS API

    User->>Agent: "Where is streamflow monitored in NC?"
    Agent->>Store: Initialize RequestContext
    Agent->>Planner: plan_query(context)
    
    Note over Planner, Tools: LLM reasoning begins
    Planner->>Tools: Call spatial_query(state="NC", pcode="00060")
    
    Tools->>Fetcher: fetch_time_series_metadata(...)
    Fetcher->>USGS: HTTP GET (with limit=10000, rel=next)
    USGS-->>Fetcher: Multiple Pages (GeoJSON)
    Fetcher-->>Tools: PagedResult (features, source_urls)
    
    %% Result Storing Mechanism
    Note over Tools, Store: Tool saves thousands of records out-of-band
    Tools->>Store: store.save_result("stations", <massive_list>)
    Store-->>Tools: Returns handle "stations_ref"
    
    Tools-->>Planner: {"message": "Success", "handle": "stations_ref"}
    
    %% Final Plan
    Planner-->>Agent: QueryPlan(stations_ref="stations_ref")
    
    %% Retrieval & Presentation
    Agent->>Store: store.get_result("stations_ref")
    Store-->>Agent: <massive_list>
    Agent->>Agent: Format markdown response & create JSON artifact
    Agent->>User: "Found 762 monitoring locations..." + Interactive Artifact
```

## Data Flow Diagram: Intent Routing for Daily Values

The agent dynamically routes daily observational queries (which bypass the planner's tools) directly to the fetcher based on detected temporal constraints.

```mermaid
sequenceDiagram
    participant User
    participant Agent
    participant Planner
    participant Fetcher
    participant USGS

    User->>Agent: "What is the recent streamflow at USGS-02238500?"
    Agent->>Planner: extract_filters(query)
    Planner-->>Agent: QueryPlan(filters={"monitoring_location_id": "USGS-02238500", "parameter_code": "00060"})
    
    Note over Agent: Agent checks for 'datetime' filter.
    
    alt No datetime specified (Latest Observation Intent)
        Agent->>Fetcher: fetch_daily_values(limit=5, sortby="-time")
        Fetcher->>USGS: HTTP GET with sort constraints
        USGS-->>Fetcher: 5 most recent records
    else Datetime specified (Historical/Range Intent)
        Agent->>Fetcher: fetch_daily_values(datetime="2023-01-01...", limit=None)
        Fetcher->>USGS: HTTP GET fetching up to 10,000 records
        USGS-->>Fetcher: Paged historical records
    end
    
    Fetcher-->>Agent: PagedResult
    Agent-->>User: Formatted Daily Value + Provenance (source_urls)
```

## Key Architectural Decisions
1. **Single Entrypoint (`USGSAgent`)**: Hides all API complexity. The external system simply passes strings and receives cleanly formatted Markdown and standard Artifacts.
2. **Out-of-Band Results (`ResultStore`)**: Massive API payloads are intercepted by the executing tools and saved to a short-lived memory store. The LLM only ever sees opaque handles (e.g., `stations_ref`), preventing context-window exhaustion and output hallucination.
3. **Data Fetcher (`USGSWaterDataFetcher`)**: Centralizes HTTP requests, transparent pagination (`_fetch_all_pages`), field selection optimization (`properties=`, `skipGeometry=true`), and rate-limit handling for all endpoints.
4. **Intent Segmentation**: Tool usage is strictly bounded. The planner is *only* responsible for spatial discovery and inventory lookups; direct daily value retrieval is structurally enforced in `agent.py` based on the planner's output filters, resolving ambiguity around temporal limits.
