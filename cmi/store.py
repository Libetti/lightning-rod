from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from threading import Lock


def _env_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}.") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be > 0, got {value}.")
    return value


FRAMES_CACHE_TTL_SECONDS = 30
POLL_INTERVAL_HINT_SECONDS = _env_positive_int("CMI_POLL_INTERVAL_SECONDS", 3600)
FRAME_RETENTION_HOURS = _env_positive_int("CMI_RETENTION_HOURS", 48)


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


def _parse_iso8601(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _retention_cutoff(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    return current - timedelta(hours=FRAME_RETENTION_HOURS)


def _prune_satellite_locked(satellite: str, now: datetime | None = None) -> None:
    frames = list(_frames_by_satellite.get(satellite, ()))
    if not frames:
        _latest_by_satellite.pop(satellite, None)
        return

    cutoff = _retention_cutoff(now=now)
    retained = [item for item in frames if _parse_iso8601(item.frame.start_time) >= cutoff]
    _frames_by_satellite[satellite] = deque(retained)

    if retained:
        _latest_by_satellite[satellite] = retained[0]
    else:
        _latest_by_satellite.pop(satellite, None)

    stale_keys = [key for key in _frame_index if key[0] == satellite]
    for key in stale_keys:
        _frame_index.pop(key, None)
    for item in retained:
        _frame_index[(satellite, item.frame.frame_id)] = item


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
        _frames_by_satellite[frame.satellite] = deque(deduped_entries)
        _prune_satellite_locked(frame.satellite)

    return replace(frame)


def has_frame(satellite: str, frame_id: str) -> bool:
    with _store_lock:
        _prune_satellite_locked(satellite)
        return (satellite, frame_id) in _frame_index


def get_latest_frame(satellite: str) -> tuple[CMIFrame, str]:
    with _store_lock:
        _prune_satellite_locked(satellite)
        entry = _latest_by_satellite.get(satellite)
        if entry is None:
            raise CMIFetchError(f"No cached CMI frame available yet for {satellite}.")
        return replace(entry.frame), entry.updated_at


def get_frames_in_range(
    satellite: str,
    start: str,
    end: str,
    limit: int = 1000,
) -> list[CMIFrame]:
    with _store_lock:
        _prune_satellite_locked(satellite)
        entries = list(_frames_by_satellite.get(satellite, ()))
        if not entries:
            raise CMIFetchError(f"No cached CMI frame available yet for {satellite}.")

        start_dt = _parse_iso8601(start)
        end_dt = _parse_iso8601(end)

        filtered: list[CMIFrame] = []
        for entry in entries:
            frame_start = _parse_iso8601(entry.frame.start_time)
            if frame_start < start_dt:
                continue
            if frame_start >= end_dt:
                continue
            filtered.append(replace(entry.frame))
            if len(filtered) >= limit:
                break
        return filtered


def get_frame(satellite: str, frame_id: str) -> CMIFrame:
    with _store_lock:
        _prune_satellite_locked(satellite)
        entry = _frame_index.get((satellite, frame_id))
        if entry is None:
            raise CMIFrameNotFoundError(f"Frame not found for {satellite}: {frame_id}")
        return replace(entry.frame)


def prune_expired_frames(now: datetime | None = None) -> None:
    with _store_lock:
        for satellite in list(_frames_by_satellite):
            _prune_satellite_locked(satellite, now=now)
