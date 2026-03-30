# Lightning Rod - NOAA GOES FastAPI Server

Tiny FastAPI server for exposing NOAA GOES data for:
- Latest GLM lightning frame metadata and points as JSON.
- GOES ABI CMI Channel 13 (Clean IR) frame metadata and XYZ tiles for MapLibre.

## Files

- `main.py`: Server app and API endpoints.
- `app/glm.py`: thin facade for lightning ingest/store helpers used by the app.
- `app/glm_ingest.py`: background NOAA fetch + GLM parse + polling loop.
- `app/glm_store.py`: in-memory lightning frame/points store and read helpers.
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
- `GET /lightning/latest-frame` and `GET /lightning/latest-points` can return `503` until the first GLM frame has been ingested

## Lightning Client Flow

1. Poll `GET /lightning/latest-frame?satellite=goes-east`
2. Compare the returned `frame_id` with the currently rendered frame
3. If `frame_id` changed, call `GET /lightning/latest-points?satellite=goes-east`
4. Use `features` as the client-side point source and handle styling/animation in the client

Typical behavior:

- Poll `latest-frame` frequently because it is smaller and cheaper
- Fetch `latest-points` only when the `frame_id` changes
- Keep prior point payloads in the client if you want to animate across frames locally

Example `GET /lightning/latest-frame` response:

```json
{
  "frame_id": "OR_GLM-L2-LCFA_G19_s20260891723000_e20260891723200_c20260891723218",
  "satellite": "goes-east",
  "start_time": "2026-03-30T17:23:00Z",
  "end_time": "2026-03-30T17:23:20Z",
  "flash_count": 75,
  "updated_at": "2026-03-30T17:23:28Z"
}
```

Example `GET /lightning/latest-points` response:

```json
{
  "frame_id": "OR_GLM-L2-LCFA_G19_s20260891723000_e20260891723200_c20260891723218",
  "satellite": "goes-east",
  "start_time": "2026-03-30T17:23:00Z",
  "end_time": "2026-03-30T17:23:20Z",
  "updated_at": "2026-03-30T17:23:28Z",
  "count": 75,
  "features": [
    {
      "id": "12345",
      "latitude": 34.12,
      "longitude": -97.44,
      "time": "2026-03-30T17:23:05Z",
      "energy": 0.91
    }
  ]
}
```

## CMI Ch13 Notes

- Satellites supported: `goes-east`, `goes-west`
- Coverage: full disk (`ABI-L2-CMIPF`, channel/band 13)
- Polling: call `/imagery/cmi/ch13/frames` every ~10 seconds; advance animation only when a new `frame_id` appears
- Tile zoom range: `z0-z8`
- Tile cache: on-demand render and disk cache under temp dir (`/tmp/lightning_rod_cmi`), retained for ~2 hours
