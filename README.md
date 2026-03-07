# Lightning Rod - NOAA GLM Lightning FastAPI Server

Tiny FastAPI server for exposing recent NOAA GOES GLM flash events as JSON.

`/lightning/recent` responses are cached in-memory per `(satellite, limit)` for 30 seconds to reduce repeated upstream fetches.

## Files

- `main.py`: Server App and Endpoints (`/health`, `/lightning/recent`)
- `app/glm.py`: NOAA fetch + GLM NetCDF parsing logic
- `requirements.txt`: runtime dependencies

## Run locally

1. Create and activate a virtual environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Start the server:

   ```bash
   uvicorn main:app --reload --port 8000
   ```

4. Fetch endpoints:

   - Health: `http://127.0.0.1:8000/health`
   - Recent flashes: `http://127.0.0.1:8000/lightning/recent`
   - West satellite example: `http://127.0.0.1:8000/lightning/recent?satellite=goes-west&limit=50`
