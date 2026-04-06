from __future__ import annotations

from pathlib import Path

from . import ingest, store

CMIFetchError = store.CMIFetchError
CMIFrame = store.CMIFrame
CMIFrameNotFoundError = store.CMIFrameNotFoundError
CMIInvalidTileError = store.CMIInvalidTileError
FRAMES_CACHE_TTL_SECONDS = store.FRAMES_CACHE_TTL_SECONDS
POLL_INTERVAL_HINT_SECONDS = store.POLL_INTERVAL_HINT_SECONDS
MAX_ZOOM = ingest.MAX_ZOOM
_latest_by_satellite = store._latest_by_satellite
_frames_by_satellite = store._frames_by_satellite
_poller_stop_event = ingest._poller_stop_event


def get_recent_frames(satellite: str = "goes-east", limit: int = 12) -> list[CMIFrame]:
    return store.get_recent_frames(satellite=satellite, limit=limit)


def get_tile_path(frame_id: str, satellite: str, z: int, x: int, y: int) -> Path:
    try:
        store.get_frame(satellite=satellite, frame_id=frame_id)
    except CMIFrameNotFoundError:
        ingest.wait_for_frame_warmup(satellite=satellite, frame_id=frame_id)

    try:
        return ingest.get_prepared_tile_path(satellite=satellite, frame_id=frame_id, z=z, x=x, y=y)
    except CMIFrameNotFoundError:
        ingest.wait_for_frame_warmup(satellite=satellite, frame_id=frame_id)
        return ingest.get_prepared_tile_path(satellite=satellite, frame_id=frame_id, z=z, x=x, y=y)


def start_background_refresh() -> None:
    ingest.start_background_refresh()


def stop_background_refresh() -> None:
    ingest.stop_background_refresh()
