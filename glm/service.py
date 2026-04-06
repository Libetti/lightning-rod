from __future__ import annotations

from pathlib import Path

from . import ingest, store

SATELLITE_TO_ID = ingest.SATELLITE_TO_ID
POLL_INTERVAL_SECONDS = ingest.POLL_INTERVAL_SECONDS
RECENT_CACHE_TTL_SECONDS = store.RECENT_CACHE_TTL_SECONDS
FRAME_RETENTION_COUNT = store.FRAME_RETENTION_COUNT
GLMFetchError = store.GLMFetchError
FlashEvent = store.FlashEvent
GLMFrame = store.GLMFrame
_latest_by_satellite = store._latest_by_satellite
_frames_by_satellite = store._frames_by_satellite
_poller_stop_event = ingest._poller_stop_event


def _frame_from_path(satellite: str, path: Path) -> GLMFrame:
    return ingest.frame_from_path(satellite=satellite, path=path)


def _latest_glm_file(satellite_id: int) -> Path:
    return ingest.latest_glm_file(satellite_id=satellite_id)


def _parse_flashes_direct(nc_path: Path, limit: int | None = None) -> list[FlashEvent]:
    return ingest.parse_flashes_direct(nc_path=nc_path, limit=limit)


def refresh_latest_lightning(satellite: str = "goes-east") -> GLMFrame:
    satellite_id = SATELLITE_TO_ID.get(satellite)
    if not satellite_id:
        raise GLMFetchError("Unsupported satellite. Use goes-east or goes-west.")

    latest_file = _latest_glm_file(satellite_id=satellite_id)
    frame = _frame_from_path(satellite=satellite, path=latest_file)

    current_frame_id = store.get_cached_frame_id(satellite)
    if current_frame_id == frame.frame_id:
        return frame

    events = _parse_flashes_direct(nc_path=latest_file, limit=None)
    return store.store_latest_points(frame=frame, events=events)


def start_background_refresh() -> None:
    ingest.start_background_refresh()


def stop_background_refresh() -> None:
    ingest.stop_background_refresh()


def get_latest_frame(satellite: str = "goes-east") -> tuple[GLMFrame, str]:
    return store.get_latest_frame(satellite=satellite)


def get_latest_points(
    satellite: str = "goes-east",
    limit: int | None = None,
) -> tuple[GLMFrame, list[FlashEvent], str]:
    return store.get_latest_points(satellite=satellite, limit=limit)
