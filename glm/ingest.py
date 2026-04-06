from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir
from threading import Event, Lock, Thread
from urllib.error import URLError
from urllib.request import urlretrieve

from netCDF4 import Dataset, num2date

from glm.store import FlashEvent, GLMFetchError, GLMFrame, get_cached_frame_id, store_latest_points

logger = logging.getLogger(__name__)

SATELLITE_TO_ID = {
    "goes-east": 19,
    "goes-west": 18,
}
POLL_INTERVAL_SECONDS = int(os.getenv("GLM_POLL_INTERVAL_SECONDS", "30"))
GLM_TOKEN_PATTERN = re.compile(r"_(s\d{13,19})_(e\d{13,19})_")

_upstream_io_lock = Lock()
_poller_thread: Thread | None = None
_poller_stop_event = Event()
_poller_lock = Lock()


def _load_goes_latest():
    from goes2go.data import goes_latest as _goes_latest

    return _goes_latest


def _flatten_paths(values: list[object]) -> list[str]:
    flattened: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            flattened.extend(_flatten_paths(list(value)))
            continue
        flattened.append(str(value))
    return flattened


def _download_from_public_bucket(s3_key: str) -> Path:
    bucket, key = s3_key.split("/", 1)
    local_path = Path(gettempdir()) / bucket / key
    local_path.parent.mkdir(parents=True, exist_ok=True)
    if local_path.exists():
        return local_path

    url = f"https://{bucket}.s3.amazonaws.com/{key}"
    try:
        urlretrieve(url, str(local_path))
    except (URLError, OSError) as exc:
        raise GLMFetchError(f"Unable to download GLM file from NOAA: {exc}") from exc
    return local_path


def _token_to_iso(token: str) -> str:
    raw = token[1:]
    base = raw[:13]
    fraction = raw[13:19].ljust(6, "0")
    parsed = datetime.strptime(base, "%Y%j%H%M%S").replace(tzinfo=timezone.utc)
    parsed = parsed.replace(microsecond=int(fraction))
    return parsed.isoformat().replace("+00:00", "Z")


def frame_from_path(satellite: str, path: Path) -> GLMFrame:
    match = GLM_TOKEN_PATTERN.search(path.name)
    if match is None:
        raise GLMFetchError(f"Unexpected GLM filename format: {path.name}")

    start_token, end_token = match.group(1), match.group(2)
    return GLMFrame(
        frame_id=path.stem,
        satellite=satellite,
        start_time=_token_to_iso(start_token),
        end_time=_token_to_iso(end_token),
        source_file=str(path),
    )


def latest_glm_file(satellite_id: int) -> Path:
    try:
        goes_latest = _load_goes_latest()
        results = goes_latest(
            satellite=satellite_id,
            product="GLM",
            return_as="filelist",
            save_dir=gettempdir(),
            verbose=False,
        )
    except Exception as exc:
        raise GLMFetchError(f"Unable to resolve latest GLM file with goes2go: {exc}") from exc
    if results is None:
        raise GLMFetchError("No recent GLM files were found.")

    raw_paths: list[object]
    if isinstance(results, (str, Path)):
        raw_paths = [results]
    elif isinstance(results, list):
        raw_paths = list(results)
    elif hasattr(results, "columns") and "file" in results.columns:
        raw_paths = list(results["file"].tolist())
    else:
        raise GLMFetchError("Unexpected goes2go response for latest GLM file.")

    file_paths = _flatten_paths(raw_paths)
    if not file_paths:
        raise GLMFetchError("No recent GLM files were found.")

    latest_candidate = file_paths[-1]
    latest_path = Path(latest_candidate)
    if latest_path.exists():
        return latest_path

    tmp_relative_path = Path(gettempdir()) / latest_candidate
    if tmp_relative_path.exists():
        return tmp_relative_path

    s3_key = latest_candidate.removeprefix("s3://")
    if s3_key.startswith("noaa-goes") and "/" in s3_key:
        return _download_from_public_bucket(s3_key=s3_key)

    raise GLMFetchError("goes2go did not return a usable GLM file path.")


def parse_flashes_direct(nc_path: Path, limit: int | None = None) -> list[FlashEvent]:
    ds = Dataset(str(nc_path), mode="r")
    try:
        flash_id_var = ds.variables["flash_id"]
        flash_lat_var = ds.variables["flash_lat"]
        flash_lon_var = ds.variables["flash_lon"]
        flash_time_var = ds.variables["flash_time_offset_of_first_event"]
        flash_energy_var = ds.variables.get("flash_energy")

        ids = flash_id_var[:]
        lats = flash_lat_var[:]
        lons = flash_lon_var[:]
        times = num2date(
            flash_time_var[:],
            units=flash_time_var.units,
            only_use_cftime_datetimes=False,
            only_use_python_datetimes=True,
        )
        energies = flash_energy_var[:] if flash_energy_var is not None else None

        size = len(ids) if limit is None else min(len(ids), limit)
        events: list[FlashEvent] = []
        for i in range(size):
            time_value = times[i]
            if isinstance(time_value, datetime):
                time_iso = time_value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            else:
                time_iso = str(time_value)

            energy_val: float | None = None
            if energies is not None:
                energy_candidate = float(energies[i])
                if energy_candidate == energy_candidate:
                    energy_val = energy_candidate

            events.append(
                FlashEvent(
                    id=str(ids[i]),
                    latitude=float(lats[i]),
                    longitude=float(lons[i]),
                    time=time_iso,
                    energy=energy_val,
                )
            )

        return events
    finally:
        ds.close()


def refresh_latest_lightning(satellite: str = "goes-east") -> GLMFrame:
    satellite_id = SATELLITE_TO_ID.get(satellite)
    if not satellite_id:
        raise GLMFetchError("Unsupported satellite. Use goes-east or goes-west.")

    with _upstream_io_lock:
        latest_file = latest_glm_file(satellite_id=satellite_id)
        frame = frame_from_path(satellite=satellite, path=latest_file)

        current_frame_id = get_cached_frame_id(satellite)
        if current_frame_id == frame.frame_id:
            return frame

        events = parse_flashes_direct(nc_path=latest_file, limit=None)

    return store_latest_points(frame=frame, events=events)


def _poll_once() -> None:
    for satellite in SATELLITE_TO_ID:
        try:
            refresh_latest_lightning(satellite=satellite)
        except GLMFetchError:
            logger.exception("Failed to refresh latest GLM frame for %s", satellite)
        except Exception:
            logger.exception("Unexpected error refreshing GLM frame for %s", satellite)


def _poll_loop() -> None:
    while not _poller_stop_event.is_set():
        _poll_once()
        _poller_stop_event.wait(POLL_INTERVAL_SECONDS)


def start_background_refresh() -> None:
    global _poller_thread
    with _poller_lock:
        if _poller_thread is not None and _poller_thread.is_alive():
            return
        _poller_stop_event.clear()
        _poller_thread = Thread(target=_poll_loop, name="glm-refresh-poller", daemon=True)
        _poller_thread.start()


def stop_background_refresh() -> None:
    global _poller_thread
    with _poller_lock:
        thread = _poller_thread
        if thread is None:
            return
        _poller_stop_event.set()
        thread.join(timeout=1.0)
        _poller_thread = None
