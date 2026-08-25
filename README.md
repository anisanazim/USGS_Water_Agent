# USGS Water Data Agent

An iChatBio agent for USGS water monitoring data. It turns a natural-language
question into exact USGS Daily API filters by searching USGS's own field and
reference metadata, then queries the live API and returns real observations.

## Setup

1. Create a virtual environment and install dependencies:
   ```
   python -m venv .venv
   .venv/Scripts/pip install -e .
   ```

2. Configure the LLM used for query resolution. Copy `.env.example` to
   `.env` and fill in:
   ```
   OPENAI_BASE_URL=...
   OPENAI_API_KEY=...
   OPENAI_MODEL=gpt-4o
   ```
   (Any OpenAI-compatible endpoint works - this has been run against a
   university AI gateway, not just api.openai.com.)

## Running

Start the agent server, for iChatBio to connect to:
```
.venv/Scripts/python.exe -m uvicorn --app-dir src agent:create_app --factory
```

For quick local testing without iChatBio, use `ask.py`:
```
.venv/Scripts/python.exe ask.py "What is the maximum streamflow in North Carolina?"
```

Run the test suite:
```
.venv/Scripts/python.exe -m pytest
```

**Always use `.venv/Scripts/python.exe` explicitly, not a bare `python`/
`pytest` on PATH** - on some machines those resolve to a different,
globally-installed Python with different package versions.

### Running with Docker

```
docker build -t usgs-water-agent .
docker run --env-file .env -p 9999:9999 usgs-water-agent
```

The agent card will be at `http://localhost:9999/.well-known/agent-card.json`.
`.env` is not baked into the image (see `.dockerignore`) - pass it at run
time with `--env-file .env` or individual `-e` flags.

## Entrypoints

- **`answer_daily_query`** - the only entrypoint. Natural-language questions
  about USGS Daily data (parameter, statistic, state, etc.). Resolves the
  question to exact Daily API filters via USGS's own metadata and an LLM
  tool loop, then queries the Daily API live and returns the most recent
  matching observation(s). Example: *"What is the maximum streamflow in
  North Carolina?"*. Asks a clarifying question instead when a request is
  significantly ambiguous.

## How it works

USGS's own Daily queryable and reference-collection metadata
(`metadata/raw/`) is the source of truth for what fields exist and what
their valid values are. Three tools - `search_queryables`,
`search_reference`, `resolve_reference` - let an LLM discover the right
field for a concept and resolve it to an exact USGS code, instead of
matching against a fixed synonym table.


## Known limitations

- Filter resolution isn't fully reliable on ambiguous terminology (e.g.
  some chemical constituents have dozens of plausible parameter codes) 
- A state-wide question with no site specified returns whichever
  monitoring station has the most recent data, not necessarily a
  representative one.
- No support yet for multi-region comparisons or arbitrary date ranges.

## USGS API reference

- Base URL: `https://api.waterdata.usgs.gov/ogcapi/v0`
- `GET /collections/daily/items` - the Daily observations collection used
  by `answer_daily_query`.