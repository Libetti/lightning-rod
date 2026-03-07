from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import gettempdir
from threading import Lock
from time import monotonic
from urllib.error import URLError
from urllib.request import urlretrieve

from goes2go.data import goes_latest
from netCDF4 import Dataset, num2date


SATELLITE_TO_ID = {
    "goes-east": 16,
    "goes-west": 18,
}
RECENT_CACHE_TTL_SECONDS = 30


class GLMFetchError(Exception):
    """Raised when we cannot fetch or parse NOAA GLM data."""


@dataclass
class FlashEvent:
    id: str
    latitude: float
    longitude: float
    time: str
    energy: float | None = None


@dataclass
class _RecentCacheEntry:
    expires_at: float
    events: list[FlashEvent]


_recent_cache: dict[tuple[str, int], _RecentCacheEntry] = {}
_recent_cache_lock = Lock()


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
    # s3_key format: noaa-goesXX/path/to/file.nc
    bucket, key = s3_key.split("/", 1)
    filename = Path(key).name
    local_path = Path(gettempdir()) / f"{bucket}_{filename}"
    url = f"https://{bucket}.s3.amazonaws.com/{key}"
    try:
        urlretrieve(url, str(local_path))
    except (URLError, OSError) as exc:
        raise GLMFetchError(f"Unable to download GLM file from NOAA: {exc}") from exc
    return local_path


def _latest_glm_file(satellite_id: int) -> Path:
    """Resolve and download the latest GLM file with goes2go."""
    results = goes_latest(
        satellite=satellite_id,
        product="GLM",
        return_as="filelist",
        save_dir=gettempdir(),
    )
    if results is None:
        raise GLMFetchError("No recent GLM files were found.")

    raw_paths: list[object]
    if isinstance(results, (str, Path)):
        raw_paths = [results]
    elif isinstance(results, list):
        raw_paths = list(results)
    elif hasattr(results, "columns") and "file" in results.columns:
        # Older goes2go versions may return a pandas DataFrame.
        raw_paths = list(results["file"].tolist())
    else:
        raise GLMFetchError("Unexpected goes2go response for latest GLM file.")

    file_paths = _flatten_paths(raw_paths)
    if not file_paths:
        raise GLMFetchError("No recent GLM files were found.")

    latest_candidate = file_paths[-1]
    latest_path = Path(latest_candidate)
    if not latest_path.exists():
        tmp_relative_path = Path(gettempdir()) / latest_candidate
        if tmp_relative_path.exists():
            latest_path = tmp_relative_path
        else:
            s3_key = latest_candidate.removeprefix("s3://")
            if s3_key.startswith("noaa-goes") and "/" in s3_key:
                latest_path = _download_from_public_bucket(s3_key=s3_key)
            else:
                raise GLMFetchError("goes2go did not return a usable GLM file path.")
    return latest_path
    

def _parse_flashes(nc_path: Path, limit: int) -> list[FlashEvent]:
    """Parse only needed fields.

    TODO later: add bbox and time-window filtering here before returning results.
    """
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

        events: list[FlashEvent] = []
        for i in range(min(len(ids), limit)):
            time_value = times[i]
            if isinstance(time_value, datetime):
                time_iso = time_value.astimezone(UTC).isoformat().replace("+00:00", "Z")
            else:
                time_iso = str(time_value)

            energy_val: float | None = None
            if energies is not None:
                energy_candidate = float(energies[i])
                if energy_candidate == energy_candidate:  # NaN guard
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


def fetch_recent_lightning(
    satellite: str = "goes-east",
    limit: int = 100,
) -> list[FlashEvent]:
    """Fetch recent GLM flash events via goes2go."""
    satellite_id = SATELLITE_TO_ID.get(satellite)
    if not satellite_id:
        raise GLMFetchError("Unsupported satellite. Use goes-east or goes-west.")

    cache_key = (satellite, limit)
    now = monotonic()
    with _recent_cache_lock:
        cached = _recent_cache.get(cache_key)
        if cached is not None and cached.expires_at > now:
            # Return copies to keep cached entries immutable to callers.
            return [replace(event) for event in cached.events]
        if cached is not None:
            _recent_cache.pop(cache_key, None)

    try:
        latest_file = _latest_glm_file(satellite_id=satellite_id)
        events = _parse_flashes(nc_path=latest_file, limit=limit)
        with _recent_cache_lock:
            _recent_cache[cache_key] = _RecentCacheEntry(
                expires_at=monotonic() + RECENT_CACHE_TTL_SECONDS,
                events=[replace(event) for event in events],
            )
        return events
    except GLMFetchError:
        raise
    except KeyError as exc:
        raise GLMFetchError(f"Unexpected GLM file format, missing variable: {exc}") from exc
    except Exception as exc:
        raise GLMFetchError(f"Unable to parse NOAA GLM data: {exc}") from exc
