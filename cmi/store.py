from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import Lock

FRAMES_CACHE_TTL_SECONDS = 30
POLL_INTERVAL_HINT_SECONDS = 30
FRAME_RETENTION_COUNT = int(os.getenv("CMI_FRAME_RETENTION_COUNT", "12"))


class CMIFetchError(Exception):
    """Raised when we cannot fetch or parse NOAA CMI data."""


class CMIFrameNotFoundError(CMIFetchError):
    """Raised when a requested frame id cannot be resolved."""


@dataclass(frozen=True)
class CMIFrame:
    frame_id: str
    satellite: str
    start_time: str
    end_time: str
    file_ref: str


@dataclass
class _FrameStoreEntry:
    frame: CMIFrame
    updated_at: str


_latest_by_satellite: dict[str, _FrameStoreEntry] = {}
_frames_by_satellite: dict[str, deque[_FrameStoreEntry]] = {}
_frame_index: dict[tuple[str, str], _FrameStoreEntry] = {}
_store_lock = Lock()


def store_prepared_frame(frame: CMIFrame) -> CMIFrame:
    entry = _FrameStoreEntry(
        frame=replace(frame),
        updated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    with _store_lock:
        frames = list(_frames_by_satellite.get(frame.satellite, ()))
        frames.insert(0, entry)

        deduped_entries: list[_FrameStoreEntry] = []
        seen: set[str] = set()
        for item in frames:
            frame_id = item.frame.frame_id
            if frame_id in seen:
                continue
            deduped_entries.append(item)
            seen.add(frame_id)

        deduped_entries.sort(
            key=lambda item: (item.frame.start_time, item.frame.end_time, item.frame.frame_id),
            reverse=True,
        )
        retained = deduped_entries[:FRAME_RETENTION_COUNT]
        deduped = deque(retained, maxlen=FRAME_RETENTION_COUNT)

        _frames_by_satellite[frame.satellite] = deduped
        _latest_by_satellite[frame.satellite] = deduped[0]
        _frame_index[(frame.satellite, frame.frame_id)] = entry

        retained_ids = {item.frame.frame_id for item in retained}
        stale_keys = [
            key for key in _frame_index
            if key[0] == frame.satellite and key[1] not in retained_ids
        ]
        for key in stale_keys:
            _frame_index.pop(key, None)

    return replace(frame)


def has_frame(satellite: str, frame_id: str) -> bool:
    with _store_lock:
        return (satellite, frame_id) in _frame_index


def get_latest_frame(satellite: str) -> tuple[CMIFrame, str]:
    with _store_lock:
        entry = _latest_by_satellite.get(satellite)
        if entry is None:
            raise CMIFetchError(f"No cached CMI frame available yet for {satellite}.")
        return replace(entry.frame), entry.updated_at


def get_recent_frames(satellite: str, limit: int) -> list[CMIFrame]:
    with _store_lock:
        entries = list(_frames_by_satellite.get(satellite, ()))
        if not entries:
            raise CMIFetchError(f"No cached CMI frame available yet for {satellite}.")
        return [replace(entry.frame) for entry in entries[:limit]]


def get_frame(satellite: str, frame_id: str) -> CMIFrame:
    with _store_lock:
        entry = _frame_index.get((satellite, frame_id))
        if entry is None:
            raise CMIFrameNotFoundError(f"Frame not found for {satellite}: {frame_id}")
        return replace(entry.frame)
