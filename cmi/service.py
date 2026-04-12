from __future__ import annotations

from pathlib import Path

from . import ingest, store

CMIFetchError = store.CMIFetchError
CMIFrame = store.CMIFrame
CMIFrameNotFoundError = store.CMIFrameNotFoundError
FRAMES_CACHE_TTL_SECONDS = store.FRAMES_CACHE_TTL_SECONDS
POLL_INTERVAL_HINT_SECONDS = store.POLL_INTERVAL_HINT_SECONDS
_latest_by_satellite = store._latest_by_satellite
_frames_by_satellite = store._frames_by_satellite
_poller_stop_event = ingest._poller_stop_event


def get_frames_in_range(
    satellite: str = "goes-east",
    start: str = "",
    end: str = "",
    limit: int = 1000,
) -> list[CMIFrame]:
    return store.get_frames_in_range(satellite=satellite, start=start, end=end, limit=limit)


def get_image_artifacts(frame_id: str, satellite: str) -> tuple[Path, list[list[float]]]:
    try:
        store.get_frame(satellite=satellite, frame_id=frame_id)
    except CMIFrameNotFoundError:
        ingest.wait_for_frame_warmup(satellite=satellite, frame_id=frame_id)

    try:
        return ingest.get_prepared_image_artifacts(satellite=satellite, frame_id=frame_id)
    except CMIFrameNotFoundError:
        ingest.wait_for_frame_warmup(satellite=satellite, frame_id=frame_id)
        return ingest.get_prepared_image_artifacts(satellite=satellite, frame_id=frame_id)


def start_background_refresh() -> None:
    ingest.start_background_refresh()


def stop_background_refresh() -> None:
    ingest.stop_background_refresh()
