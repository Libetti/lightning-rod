from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import Lock

RECENT_CACHE_TTL_SECONDS = 10
FRAME_RETENTION_COUNT = int(os.getenv("GLM_FRAME_RETENTION_COUNT", "12"))


class GLMFetchError(Exception):
    """Raised when we cannot fetch or parse NOAA GLM data."""


@dataclass(frozen=True)
class FlashEvent:
    id: str
    latitude: float
    longitude: float
    time: str
    energy: float | None = None


@dataclass(frozen=True)
class GLMFrame:
    frame_id: str
    satellite: str
    start_time: str
    end_time: str
    source_file: str


@dataclass
class _FrameStoreEntry:
    frame: GLMFrame
    events: list[FlashEvent]
    updated_at: str


_latest_by_satellite: dict[str, _FrameStoreEntry] = {}
_frames_by_satellite: dict[str, deque[_FrameStoreEntry]] = {}
_store_lock = Lock()


def _store_entry(frame: GLMFrame, events: list[FlashEvent]) -> _FrameStoreEntry:
    return _FrameStoreEntry(
        frame=frame,
        events=[replace(event) for event in events],
        updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )


def _entry_copy(entry: _FrameStoreEntry, limit: int | None = None) -> tuple[GLMFrame, list[FlashEvent], str]:
    events = entry.events if limit is None else entry.events[:limit]
    return (
        replace(entry.frame),
        [replace(event) for event in events],
        entry.updated_at,
    )


def store_latest_points(frame: GLMFrame, events: list[FlashEvent]) -> GLMFrame:
    entry = _store_entry(frame=frame, events=events)
    with _store_lock:
        frames = _frames_by_satellite.setdefault(frame.satellite, deque(maxlen=FRAME_RETENTION_COUNT))
        frames.appendleft(entry)
        deduped = deque(maxlen=FRAME_RETENTION_COUNT)
        seen: set[str] = set()
        for item in frames:
            frame_id = item.frame.frame_id
            if frame_id in seen:
                continue
            deduped.append(item)
            seen.add(frame_id)
        _frames_by_satellite[frame.satellite] = deduped
        _latest_by_satellite[frame.satellite] = entry
    return replace(frame)


def get_cached_frame_id(satellite: str) -> str | None:
    with _store_lock:
        entry = _latest_by_satellite.get(satellite)
        return None if entry is None else entry.frame.frame_id


def get_latest_frame(satellite: str) -> tuple[GLMFrame, str]:
    with _store_lock:
        entry = _latest_by_satellite.get(satellite)
        if entry is None:
            raise GLMFetchError(f"No cached lightning frame available yet for {satellite}.")
        frame, _, updated_at = _entry_copy(entry, limit=0)
        return frame, updated_at


def get_latest_points(satellite: str, limit: int | None = None) -> tuple[GLMFrame, list[FlashEvent], str]:
    with _store_lock:
        entry = _latest_by_satellite.get(satellite)
        if entry is None:
            raise GLMFetchError(f"No cached lightning frame available yet for {satellite}.")
        return _entry_copy(entry, limit=limit)

