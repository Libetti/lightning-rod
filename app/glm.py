from __future__ import annotations

from pathlib import Path

from app import glm_ingest, glm_store

SATELLITE_TO_ID = glm_ingest.SATELLITE_TO_ID
POLL_INTERVAL_SECONDS = glm_ingest.POLL_INTERVAL_SECONDS
RECENT_CACHE_TTL_SECONDS = glm_store.RECENT_CACHE_TTL_SECONDS
FRAME_RETENTION_COUNT = glm_store.FRAME_RETENTION_COUNT
GLMFetchError = glm_store.GLMFetchError
FlashEvent = glm_store.FlashEvent
GLMFrame = glm_store.GLMFrame
_latest_by_satellite = glm_store._latest_by_satellite
_frames_by_satellite = glm_store._frames_by_satellite
_poller_stop_event = glm_ingest._poller_stop_event


def _frame_from_path(satellite: str, path: Path) -> GLMFrame:
    return glm_ingest.frame_from_path(satellite=satellite, path=path)


def _latest_glm_file(satellite_id: int) -> Path:
    return glm_ingest.latest_glm_file(satellite_id=satellite_id)


def _parse_flashes_direct(nc_path: Path, limit: int | None = None) -> list[FlashEvent]:
    return glm_ingest.parse_flashes_direct(nc_path=nc_path, limit=limit)


def refresh_latest_lightning(satellite: str = "goes-east") -> GLMFrame:
    satellite_id = SATELLITE_TO_ID.get(satellite)
    if not satellite_id:
        raise GLMFetchError("Unsupported satellite. Use goes-east or goes-west.")

    latest_file = _latest_glm_file(satellite_id=satellite_id)
    frame = _frame_from_path(satellite=satellite, path=latest_file)

    current_frame_id = glm_store.get_cached_frame_id(satellite)
    if current_frame_id == frame.frame_id:
        return frame

    events = _parse_flashes_direct(nc_path=latest_file, limit=None)
    return glm_store.store_latest_points(frame=frame, events=events)


def start_background_refresh() -> None:
    glm_ingest.start_background_refresh()


def stop_background_refresh() -> None:
    glm_ingest.stop_background_refresh()


def get_latest_frame(satellite: str = "goes-east") -> tuple[GLMFrame, str]:
    return glm_store.get_latest_frame(satellite=satellite)


def get_latest_points(
    satellite: str = "goes-east",
    limit: int | None = None,
) -> tuple[GLMFrame, list[FlashEvent], str]:
    return glm_store.get_latest_points(satellite=satellite, limit=limit)
