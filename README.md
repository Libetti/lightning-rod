# Lightning Rod - NOAA GOES FastAPI Server

Tiny FastAPI server for exposing NOAA GOES data for:
- Recent GLM lightning flashes as JSON.
- GOES ABI CMI Channel 13 (Clean IR) frame metadata and XYZ tiles for MapLibre.

`/lightning/recent` responses are cached in-memory per `(satellite, limit)` for 10 seconds to reduce repeated upstream fetches.

## Files

- `main.py`: Server app and API endpoints.
- `app/glm.py`: NOAA fetch + GLM NetCDF parsing logic.
- `app/cmi.py`: NOAA fetch + CMI Ch13 frame discovery, raster prep, tile rendering, cache cleanup.
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
   - CMI Ch13 frame list: `http://127.0.0.1:8000/imagery/cmi/ch13/frames?satellite=goes-east&limit=12`
   - CMI Ch13 tile template (from frame list): `http://127.0.0.1:8000/imagery/cmi/ch13/tiles/{satellite}/{frame_id}/{z}/{x}/{y}.png`

## CMI Ch13 Notes

- Satellites supported: `goes-east`, `goes-west`
- Coverage: full disk (`ABI-L2-CMIPF`, channel/band 13)
- Polling: call `/imagery/cmi/ch13/frames` every ~10 seconds; advance animation only when a new `frame_id` appears
- Tile zoom range: `z0-z8`
- Tile cache: on-demand render and disk cache under temp dir (`/tmp/lightning_rod_cmi`), retained for ~2 hours
