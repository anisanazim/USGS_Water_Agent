# USGS Water Data Agent

An **iChatBio** agent for United States Geological Survey (USGS) water monitoring data. It translates natural-language inquiries into deterministic, schema-validated queries across both USGS automated sensor networks and laboratory water quality databases, returning real observations and audit-grade artifacts.

---

## 1. Supported Modalities

The agent unifies two distinct physical observation networks operated by the USGS:

1. **Continuous Time-Series & Sensor Network (OGC API):** High-frequency automated sensor measurements (streamflow discharge, gage height, continuous temperature) updated at 15-minute to daily intervals.
2. **Discrete Sample & Laboratory Network (Samples API):** Physical water quality grab samples collected in bottles and analyzed in analytical chemistry laboratories (nutrients, calcium, metals, suspended sediment, PFAS) across decadal monitoring programs.

---

## 2. Supported Query Capabilities

The agent supports five primary query shapes plus unconfigured parameter exploration:

### 2.1 Spatial Station Discovery ("Where is X monitored?")
Finds which monitoring stations measure a parameter across a state or county.
* **Examples:**
  * *"Where is calcium monitored in North Carolina?"* (Returns 3,663 distinct stations across 4 calcium forms in NC)
  * *"Where is nitrate monitored in Wake County, NC?"* (Returns distinct stations across 13 nitrate forms)
  * *"Where is streamflow monitored in Wake County, North Carolina?"* (Routes to continuous sensor network)
* **Response:** Total distinct station count (with server-side deduplication notice), per-parameter breakdown table (5-digit NWIS code, descriptor, unit, station count), first 50 station IDs in chat, and complete station ID list registered in a downloadable JSON artifact.

### 2.2 Dual-Modality Station Inventory ("What is measured at station X?")
Produces a comprehensive site audit of all sensors and chemical constituents monitored at a specific USGS location.
* **Examples:**
  * *"What information is available at USGS-0423205010?"*
  * *"What data is measured at USGS-02087946?"*
* **Response:** Queries both endpoints concurrently:
  * `### Continuous Sensors`: lists active/historical daily sensors with parameter names, units, and date ranges (`estimated from YYYY-MM-DD to YYYY-MM-DD at daily interval`).
  * `### Discrete Samples`: lists chemical parameters grouped by characteristic group (Inorganics, Nutrients, Physical, etc.) with exact observation counts and date ranges.
  * Internal metadata (`Information` group, 663 codes) is permanently excluded. Full inventory attached as a JSON artifact.

### 2.3 Station Parameter Observation Counts ("How many records of X at station Y?")
Reports exact observation counts and date bounds for a specific parameter or parameter family at an individual station.
* **Examples:**
  * *"How many phosphorus records are at USGS-0423205010?"*
  * *"How many calcium measurements are at USGS-02087946?"*
* **Response:** Row-by-row breakdown of all candidate constituents in that family found at the station (pcode, descriptor, unit, exact count, date range). States unmonitored codes in the family having 0 observations. Never combines or sums counts across different analytical fractions.

### 2.4 Discrete Observation Values ("Show me the values/readings for X")
Retrieves and tabulates actual laboratory water quality measurement records.
* **Examples:**
  * *"Show me the calcium values at USGS-0423205010."*
  * *"Show me the nitrate readings at USGS-02087500."*
* **Response:** Observation count summary, a 10-row markdown preview table (`| Date | Time | Value | Unit | Parameter Code | Descriptor |`), and the full observation dataset embedded as a downloadable session artifact.

### 2.5 Continuous Daily Sensor Observations (Hydrology & Streamflow)
Retrieves the most recent daily statistical observations from automated sensor stations.
* **Examples:**
  * *"What is the maximum streamflow in North Carolina?"*
  * *"Show me the mean daily discharge at station USGS-02087500."*
  * *"What is the latest gage height at USGS-02087500?"*
* **Response:** Most recent daily reading with value, unit, timestamp, approval status, and station ID.


---

## 3. Core Architectural Principles

* **Present, Don't Decide:** The agent retrieves and presents data truthfully. It never silently auto-resolves ambiguous families to a single dominant code, never ranks one form as "primary", and never computes pseudo-totals by adding together different analytical fractions.
* **Count-First Safety Guard:** Bulk queries always verify row counts via `/results/count` or `/locations/count` prior to fetching data. If counts exceed safe thresholds, the system partitions queries temporally by date chunks to avoid server dropouts (>30k rows).
* **Repeated Query Parameters:** Spatial family queries are dispatched in a single HTTP request using repeated query parameters (`usgsPCode=00910&usgsPCode=00915...`), providing >18x faster response times and eliminating pagination loss.
* **Truthful Sourcing:** Station-scoped clarifications and inventories derive their counts strictly from the station's actual summary data.
* **Artifact Provenance Pipeline:** Every generated artifact includes complete provenance metadata (`source_urls`, `retrieved_at` ISO 8601 UTC timestamp, and `pcodes`).

---

## 4. Setup & Installation

### 4.1 Prerequisites
* Python 3.11+
* Git

### 4.2 Local Setup
1. Clone the repository and navigate into the project directory:
   ```powershell
   git clone https://github.com/ichatbio/usgs-water-agent.git
   cd usgs-water-agent
   ```

2. Create a virtual environment and install dependencies in editable mode:
   ```powershell
   python -m venv .venv
   .venv\Scripts\pip install -e .
   ```

3. Configure your LLM credentials. Copy `.env.example` to `.env`:
   ```ini
   OPENAI_BASE_URL=https://api.openai.com/v1
   OPENAI_API_KEY=your-api-key-here
   OPENAI_MODEL=gpt-5.6-luna
   ```
   *(Any OpenAI-compatible gateway works, including university or institutional LLM proxies.)*

---

## 5. Running the Agent

### 5.1 Starting the Agent Server (for iChatBio / A2A)
To start the FastAPI / Uvicorn server that connects to the iChatBio runtime:

```powershell
# Using the active virtualenv:
uvicorn --app-dir src agent:create_app --factory --port 9999

# Or explicitly invoking the virtual environment:
.venv\Scripts\python.exe -m uvicorn --app-dir src agent:create_app --factory --port 9999
```

The agent card will be published at `http://localhost:9999/.well-known/agent-card.json`.

### 5.2 Direct Command-Line Testing (CLI)
You can test queries directly through the end-to-end agent pipeline without running a server using `ask.py`:

```powershell
.venv\Scripts\python.exe ask.py "Where is calcium monitored in North Carolina?"
.venv\Scripts\python.exe ask.py "What information is available at USGS-0423205010?"
.venv\Scripts\python.exe ask.py "How many phosphorus records are at USGS-0423205010?"
.venv\Scripts\python.exe ask.py "Show me the calcium values at USGS-0423205010"
.venv\Scripts\python.exe ask.py "What is the maximum streamflow in North Carolina?"
```

### 5.3 Running Tests
Run the full test suite with pytest:

```powershell
.venv\Scripts\python.exe -m pytest tests/ -p no:deepeval
```

### 5.4 Running with Docker
Build and run the container:

```bash
docker build -t usgs-water-agent .
docker run --env-file .env -p 9999:9999 usgs-water-agent
```

---

## 6. Public Entrypoint

Conforms to the iChatBio agent runtime specification:
* **Primary Entrypoint ID:** `query_water_data`
* **Backward-Compatible Alias:** `answer_daily_query` (supported for legacy callers)
* **Agent Class:** `USGSWaterAgent` (`src/agent.py`)
* **Public Method:** `run(context: ResponseContext, request: str, entrypoint: str, params: Optional[BaseModel], metadata: dict | None)`

---

## 7. Upstream USGS APIs

* **USGS Water Data OGC API:** `https://api.waterdata.usgs.gov/ogcapi/v0/`
  * `/collections/daily/items` — Automated daily sensor observations.
  * `/collections/time-series-metadata/items` — Active/historical time series and sensor metadata.
  * `/collections/monitoring-locations/items` — Station geographical and physical metadata.
* **USGS Samples API:** `https://api.waterdata.usgs.gov/samples-data/`
  * `/locations/site` & `/locations/count` — Discrete water quality monitoring location discovery.
  * `/summary/{monitoring_location_id}` — Discrete chemical constituent summary by station.
  * `/results/narrow` & `/results/count` — Streaming laboratory observations and measurements.