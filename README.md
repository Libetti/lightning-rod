# Lightning Rod - NOAA GOES FastAPI Server

Tiny FastAPI server for exposing NOAA GOES data for:
- Latest GLM lightning frame metadata and points as JSON.
- GOES ABI CMI Channel 13 (Clean IR) frame metadata and globe-ready frame images for MapLibre.

## Files

- `main.py`: Server app and API endpoints.
- `glm/service.py`: thin facade for lightning ingest/store helpers used by the app.
- `glm/ingest.py`: background NOAA fetch + GLM parse + polling loop.
- `glm/store.py`: in-memory lightning frame/points store and read helpers.
- `cmi/service.py`: thin facade for CMI frame/image reads used by the app.
- `cmi/ingest.py`: background NOAA fetch + CMI raster/image preparation loop.
- `cmi/store.py`: in-memory CMI frame index and retention helpers.
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
   - CMI Ch13 frame list: `http://127.0.0.1:8000/imagery/cmi/ch13/frames?satellite=goes-east&start=2026-04-08T00:00:00Z&end=2026-04-08T01:00:00Z`
   - CMI Ch13 frame image: `http://127.0.0.1:8000/imagery/cmi/ch13/images/{satellite}/{frame_id}.png`

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
- The server maintains a rolling rendered archive and serves frame metadata by explicit time window
- Startup behavior: warm the latest frame first, then backfill the last configured archive window in the background
- Refresh behavior: poll NOAA on a fixed interval and add only missing frames inside the incremental lookback window
- Frames include `image_url` plus exactly four `coordinates` in top-left, top-right, bottom-right, bottom-left order for a MapLibre `ImageSource`
- `GET /imagery/cmi/ch13/images/{satellite}/{frame_id}.png` returns `image/png`
- Image placement is a practical EPSG:4326 approximation for globe rendering, not a native GOES geostationary renderer
- Render cache lives under `CMI_CACHE_DIR` and is pruned by frame timestamp, not file mtime
- `GET /imagery/cmi/ch13/frames` now requires `start` and `end`

Example `GET /imagery/cmi/ch13/frames` response:

```json
{
  "satellite": "goes-east",
  "count": 1,
  "poll_interval_seconds": 3600,
  "frames": [
    {
      "frame_id": "OR_ABI-L2-CMIPF-M6C13_G19_s20260942240173_e20260942249481_c20260942249529",
      "satellite": "goes-east",
      "start_time": "2026-04-03T22:40:17.300000Z",
      "end_time": "2026-04-03T22:49:48.100000Z",
      "image_url": "http://127.0.0.1:8000/imagery/cmi/ch13/images/goes-east/OR_ABI-L2-CMIPF-M6C13_G19_s20260942240173_e20260942249481_c20260942249529.png",
      "coordinates": [
        [-140.0, 55.0],
        [-60.0, 55.0],
        [-60.0, -10.0],
        [-140.0, -10.0]
      ]
    }
  ]
}
```

Example request for a one-hour playback window:

```text
GET /imagery/cmi/ch13/frames?satellite=goes-east&start=2026-04-08T00:00:00Z&end=2026-04-08T01:00:00Z
```

Recommended client flow:

1. Fetch 48 hours of metadata once with `start` and `end`.
2. Build the animation timeline from the returned `start_time` values.
3. Fetch PNGs progressively in 1-hour windows around the playhead.
4. Evict image assets behind the playhead while retaining metadata for the full session.

## CMI Configuration

Supported CMI environment variables:

- `CMI_POLL_INTERVAL_SECONDS`
  Controls how often the background poller checks NOAA for new frames. Default: `3600`.
- `CMI_LOOKBACK_HOURS`
  Controls the startup backfill window. Default: `48`.
- `CMI_INCREMENTAL_LOOKBACK_HOURS`
  Controls the recent-history window used during each poll cycle to catch newly published frames. Default: `3`.
- `CMI_RETENTION_HOURS`
  Controls how much rendered CMI history is retained in memory and on disk. Default: `48`.
- `CMI_CACHE_DIR`
  Optional override for the rendered CMI cache root. Default: system temp dir + `lightning_rod_cmi`.
- `CMI_VISIBLE_CLOUD_TEMP_K`
  Upper temperature bound used by the cloud brightness transform. Default: `270.0`.
- `CMI_DENSE_CLOUD_TEMP_K`
  Lower temperature bound used by the cloud brightness transform. Default: `235.0`.
- `CMI_EDGE_SMOOTH_RADIUS`
  Blur radius used when softening the alpha/coverage edge. Default: `2`.
- `CMI_EDGE_SMOOTH_PASSES`
  Number of smoothing passes used on the coverage mask. Default: `2`.

Validation rules enforced at startup:

- `CMI_POLL_INTERVAL_SECONDS`, `CMI_LOOKBACK_HOURS`, `CMI_INCREMENTAL_LOOKBACK_HOURS`, `CMI_RETENTION_HOURS`, `CMI_EDGE_SMOOTH_RADIUS`, and `CMI_EDGE_SMOOTH_PASSES` must be positive integers.
- `CMI_VISIBLE_CLOUD_TEMP_K` and `CMI_DENSE_CLOUD_TEMP_K` must be positive floats.
- `CMI_INCREMENTAL_LOOKBACK_HOURS` must be less than or equal to `CMI_LOOKBACK_HOURS`.
- `CMI_DENSE_CLOUD_TEMP_K` must be lower than `CMI_VISIBLE_CLOUD_TEMP_K`.

Suggested production-like settings for a 48-hour archive:

```bash
export CMI_POLL_INTERVAL_SECONDS=3600
export CMI_LOOKBACK_HOURS=48
export CMI_INCREMENTAL_LOOKBACK_HOURS=3
export CMI_RETENTION_HOURS=48
```

## CMI Operational Validation

Use this checklist after changing CMI ingest or deploying a new environment:

1. Start the app and confirm it serves `/health` immediately.
2. Wait for the latest-frame warmup, then request a recent 1-hour window from `/imagery/cmi/ch13/frames`.
3. Confirm the response is ordered oldest-to-newest and each frame has `image_url` plus four `coordinates`.
4. Open one returned `image_url` and verify the PNG is served with a long-lived cache header.
5. After the background backfill finishes, request a wider window and confirm older frames are present.
6. Inspect `CMI_CACHE_DIR` and verify `images/`, `metadata/`, and any retained source files are being populated.
7. Let the process run past one poll interval and confirm only new frames are added.
8. Verify assets older than `CMI_RETENTION_HOURS` are removed from both the in-memory index and disk cache.

Useful manual checks:

```bash
curl "http://127.0.0.1:8000/imagery/cmi/ch13/frames?satellite=goes-east&start=2026-04-08T00:00:00Z&end=2026-04-08T01:00:00Z"
find "${CMI_CACHE_DIR:-${TMPDIR:-/tmp}/lightning_rod_cmi}" -type f | head
```
