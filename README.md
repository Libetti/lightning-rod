# Lightning Rod - NOAA GOES FastAPI Server

Tiny FastAPI server for exposing NOAA GOES data for:
- Latest GLM lightning frame metadata and points as JSON.
- GOES ABI CMI Channel 13 (Clean IR) frame metadata and XYZ tiles for MapLibre.

## Files

- `main.py`: Server app and API endpoints.
- `app/glm.py`: background NOAA fetch + GLM parse + in-memory frame store.
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
   - Latest lightning frame: `http://127.0.0.1:8000/lightning/latest-frame`
   - Latest lightning points: `http://127.0.0.1:8000/lightning/latest-points?satellite=goes-west&limit=1000`
   - CMI Ch13 frame list: `http://127.0.0.1:8000/imagery/cmi/ch13/frames?satellite=goes-east&limit=12`
   - CMI Ch13 tile template (from frame list): `http://127.0.0.1:8000/imagery/cmi/ch13/tiles/{satellite}/{frame_id}/{z}/{x}/{y}.png`

## Lightning Notes

- Satellites supported: `goes-east`, `goes-west`
- The server ingests GLM data in a background poller and serves the latest cached frame/points from memory
- Poll `/lightning/latest-frame` to detect new `frame_id`s, then fetch `/lightning/latest-points` when the frame changes

## CMI Ch13 Notes

- Satellites supported: `goes-east`, `goes-west`
- Coverage: full disk (`ABI-L2-CMIPF`, channel/band 13)
- Polling: call `/imagery/cmi/ch13/frames` every ~10 seconds; advance animation only when a new `frame_id` appears
- Tile zoom range: `z0-z8`
- Tile cache: on-demand render and disk cache under temp dir (`/tmp/lightning_rod_cmi`), retained for ~2 hours
