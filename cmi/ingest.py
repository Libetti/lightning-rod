from __future__ import annotations

import json
import logging
import os
import re
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import gettempdir
from threading import Event, Lock, Thread
from urllib.error import URLError
from urllib.request import urlretrieve

import numpy as np
from netCDF4 import Dataset

from app.native_locks import NETCDF_LOCK
from cmi.store import (
    CMIFrame,
    CMIFetchError,
    CMIFrameNotFoundError,
    FRAME_RETENTION_HOURS,
    get_frame,
    has_frame,
    prune_expired_frames,
    store_prepared_frame,
)

logger = logging.getLogger(__name__)


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


def _env_positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a float, got {raw!r}.") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be > 0, got {value}.")
    return value


SATELLITE_TO_ID = {
    "goes-east": 19,
    "goes-west": 18,
}
POLL_INTERVAL_SECONDS = _env_positive_int("CMI_POLL_INTERVAL_SECONDS", 3600)
LOOKBACK_HOURS = _env_positive_int("CMI_LOOKBACK_HOURS", 48)
INCREMENTAL_LOOKBACK_HOURS = _env_positive_int("CMI_INCREMENTAL_LOOKBACK_HOURS", 3)
TEMP_COLD_K = 180.0
TEMP_WARM_K = 320.0
TEMP_VISIBLE_CLOUD_K = _env_positive_float("CMI_VISIBLE_CLOUD_TEMP_K", 270.0)
TEMP_DENSE_CLOUD_K = _env_positive_float("CMI_DENSE_CLOUD_TEMP_K", 235.0)
EDGE_SMOOTH_RADIUS = _env_positive_int("CMI_EDGE_SMOOTH_RADIUS", 2)
EDGE_SMOOTH_PASSES = _env_positive_int("CMI_EDGE_SMOOTH_PASSES", 2)
DOWNLOAD_ATTEMPTS = _env_positive_int("CMI_DOWNLOAD_ATTEMPTS", 3)
DOWNLOAD_RETRY_DELAY_SECONDS = _env_positive_float("CMI_DOWNLOAD_RETRY_DELAY_SECONDS", 1.0)

if INCREMENTAL_LOOKBACK_HOURS > LOOKBACK_HOURS:
    raise RuntimeError(
        "CMI_INCREMENTAL_LOOKBACK_HOURS must be <= CMI_LOOKBACK_HOURS. "
        f"Got {INCREMENTAL_LOOKBACK_HOURS} > {LOOKBACK_HOURS}."
    )
if TEMP_DENSE_CLOUD_K >= TEMP_VISIBLE_CLOUD_K:
    raise RuntimeError(
        "CMI_DENSE_CLOUD_TEMP_K must be lower than CMI_VISIBLE_CLOUD_TEMP_K. "
        f"Got {TEMP_DENSE_CLOUD_K} >= {TEMP_VISIBLE_CLOUD_K}."
    )

CMI_CACHE_DIR = Path(os.getenv("CMI_CACHE_DIR", str(Path(gettempdir()) / "lightning_rod_cmi")))
SOURCE_DIR = CMI_CACHE_DIR / "source"
RASTER_DIR = CMI_CACHE_DIR / "rasters"
IMAGE_DIR = CMI_CACHE_DIR / "images"
METADATA_DIR = CMI_CACHE_DIR / "metadata"

FRAME_TOKEN_PATTERN = re.compile(r"_(s\d{13,19})_(e\d{13,19})_")

_upstream_io_lock = Lock()
_poller_thread: Thread | None = None
_poller_stop_event = Event()
_poller_lock = Lock()
_frame_locks: dict[tuple[str, str], Lock] = {}
_frame_locks_guard = Lock()
_image_locks: dict[tuple[str, str], Lock] = {}
_image_locks_guard = Lock()
_frame_warmups: dict[tuple[str, str], "_FrameWarmupEntry"] = {}
_frame_warmups_guard = Lock()


@dataclass
class _FrameWarmupEntry:
    frame: CMIFrame
    done: Event = field(default_factory=Event)
    error: Exception | None = None


@dataclass
class _PrepareFramesResult:
    total: int = 0
    prepared: int = 0
    skipped_cached: int = 0
    skipped_retention: int = 0
    failed: int = 0


def _ensure_cache_dirs() -> None:
    for directory in (SOURCE_DIR, RASTER_DIR, IMAGE_DIR, METADATA_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def _load_goes_timerange():
    from goes2go.data import goes_timerange as _goes_timerange

    return _goes_timerange


def _require_rasterio():
    try:
        import rasterio
    except Exception as exc:
        raise CMIFetchError("Missing dependency 'rasterio'. Install requirements.txt.") from exc

    return rasterio


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


def _local_cmi_search_roots(satellite_id: int) -> list[Path]:
    bucket = f"noaa-goes{satellite_id}"
    temp_root = Path(gettempdir())
    return [
        SOURCE_DIR / bucket / "ABI-L2-CMIPF",
        temp_root / bucket / "ABI-L2-CMIPF",
        SOURCE_DIR / bucket,
        temp_root / bucket,
    ]


def _local_cmi_file_refs(satellite_id: int) -> list[str]:
    candidates: dict[Path, Path] = {}
    for root in _local_cmi_search_roots(satellite_id):
        if not root.exists():
            continue
        for path in root.rglob("*.nc"):
            if path.is_file() and "ABI-L2-CMIPF" in path.name:
                candidates[path] = path

    if not candidates:
        return []

    def _sort_key(path: Path) -> tuple[str, str, str]:
        try:
            start_token, end_token = _extract_tokens(path.name)
        except CMIFetchError:
            return ("", "", path.name)
        return (start_token, end_token, path.name)

    ordered = sorted(candidates.values(), key=_sort_key, reverse=True)
    return [str(path) for path in ordered]


def _extract_tokens(filename: str) -> tuple[str, str]:
    match = FRAME_TOKEN_PATTERN.search(filename)
    if match is None:
        raise CMIFetchError(f"Unexpected GOES filename format: {filename}")
    return match.group(1), match.group(2)


def _goes_token_to_iso(token: str) -> str:
    raw = token[1:]
    base = raw[:13]
    fraction = raw[13:19].ljust(6, "0")
    parsed = datetime.strptime(base, "%Y%j%H%M%S").replace(tzinfo=timezone.utc)
    parsed = parsed + timedelta(microseconds=int(fraction))
    return parsed.isoformat().replace("+00:00", "Z")


def _token_sort_key(token: str) -> tuple[str, str]:
    raw = token[1:]
    return raw[:13], raw[13:19].ljust(6, "0")


def _frame_lock_for(satellite: str, frame_id: str) -> Lock:
    key = (satellite, frame_id)
    with _frame_locks_guard:
        lock = _frame_locks.get(key)
        if lock is None:
            lock = Lock()
            _frame_locks[key] = lock
        return lock


def _image_lock_for(satellite: str, frame_id: str) -> Lock:
    key = (satellite, frame_id)
    with _image_locks_guard:
        lock = _image_locks.get(key)
        if lock is None:
            lock = Lock()
            _image_locks[key] = lock
        return lock


def _begin_frame_warmup(frame: CMIFrame) -> tuple[_FrameWarmupEntry, bool]:
    key = (frame.satellite, frame.frame_id)
    with _frame_warmups_guard:
        entry = _frame_warmups.get(key)
        if entry is not None:
            return entry, False

        entry = _FrameWarmupEntry(frame=frame)
        _frame_warmups[key] = entry
        return entry, True


def _finish_frame_warmup(entry: _FrameWarmupEntry, error: Exception | None = None) -> None:
    key = (entry.frame.satellite, entry.frame.frame_id)
    with _frame_warmups_guard:
        entry.error = error
        entry.done.set()
        _frame_warmups.pop(key, None)


def wait_for_frame_warmup(satellite: str, frame_id: str) -> CMIFrame:
    with _frame_warmups_guard:
        entry = _frame_warmups.get((satellite, frame_id))
    if entry is None:
        raise CMIFrameNotFoundError(f"Frame not found for {satellite}: {frame_id}")

    entry.done.wait()
    if entry.error is not None:
        raise entry.error
    return entry.frame


def _iso_to_utc_datetime(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc)


def _retention_cutoff(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    return current - timedelta(hours=FRAME_RETENTION_HOURS)


def _frame_is_within_retention(frame: CMIFrame, now: datetime | None = None) -> bool:
    return _iso_to_utc_datetime(frame.start_time) >= _retention_cutoff(now=now)


def _lookback_window(hours: int) -> str:
    return f"{max(int(hours), 1)}h"


def _list_recent_cmi_file_refs(satellite_id: int, recent_window: str) -> list[str]:
    local_refs = _local_cmi_file_refs(satellite_id=satellite_id)
    try:
        goes_timerange = _load_goes_timerange()
        results = goes_timerange(
            satellite=satellite_id,
            product="ABI-L2-CMIP",
            domain="F",
            bands=13,
            recent=recent_window,
            return_as="filelist",
            download=False,
            save_dir=gettempdir(),
            s3_refresh=True,
            ignore_missing=True,
            verbose=False,
        )
    except Exception as exc:
        if local_refs:
            logger.warning(
                "Falling back to locally cached CMI files for satellite %s after goes2go failure: %s",
                satellite_id,
                exc,
            )
            return local_refs
        raise CMIFetchError(f"Unable to resolve recent CMI files with goes2go: {exc}") from exc

    if results is None:
        if local_refs:
            return local_refs
        raise CMIFetchError("No recent CMI files were found.")

    raw_paths: list[object]
    if isinstance(results, (str, Path)):
        raw_paths = [results]
    elif isinstance(results, list):
        raw_paths = list(results)
    elif hasattr(results, "columns") and "file" in results.columns:
        raw_paths = list(results["file"].tolist())
    else:
        raise CMIFetchError("Unexpected goes2go response for CMI file list.")

    file_refs = _flatten_paths(raw_paths)
    if not file_refs:
        if local_refs:
            return local_refs
        raise CMIFetchError("No recent CMI files were found.")
    return file_refs


def _frames_from_file_refs(satellite: str, file_refs: list[str]) -> list[CMIFrame]:
    unique: dict[str, tuple[tuple[str, str], CMIFrame]] = {}
    for file_ref in file_refs:
        filename = Path(file_ref).name
        if not filename.endswith(".nc"):
            continue

        frame_id = filename.removesuffix(".nc")
        try:
            start_token, end_token = _extract_tokens(filename)
        except CMIFetchError:
            logger.warning("Skipping unrecognized CMI filename: %s", filename)
            continue

        frame = CMIFrame(
            frame_id=frame_id,
            satellite=satellite,
            start_time=_goes_token_to_iso(start_token),
            end_time=_goes_token_to_iso(end_token),
            file_ref=file_ref,
        )
        unique[frame_id] = (_token_sort_key(start_token), frame)

    ordered = sorted(unique.values(), key=lambda item: item[0], reverse=True)
    return [frame for _, frame in ordered]


def discover_recent_frames(satellite: str, lookback_hours: int = LOOKBACK_HOURS) -> list[CMIFrame]:
    satellite_id = SATELLITE_TO_ID.get(satellite)
    if not satellite_id:
        raise CMIFetchError("Unsupported satellite. Use goes-east or goes-west.")

    with _upstream_io_lock:
        file_refs = _list_recent_cmi_file_refs(
            satellite_id=satellite_id,
            recent_window=_lookback_window(lookback_hours),
        )
    frames = _frames_from_file_refs(satellite=satellite, file_refs=file_refs)
    cutoff = _retention_cutoff()
    frames = [frame for frame in frames if _iso_to_utc_datetime(frame.start_time) >= cutoff]
    if not frames:
        raise CMIFetchError("No recent CMI frames were found.")
    return frames


def _public_bucket_s3_key(file_ref: str) -> str | None:
    s3_key = file_ref.removeprefix("s3://")
    if s3_key.startswith("noaa-goes") and "/" in s3_key:
        return s3_key
    return None


def _download_from_public_bucket(s3_key: str, refresh: bool = False) -> Path:
    bucket, key = s3_key.split("/", 1)
    local_path = SOURCE_DIR / bucket / key
    local_path.parent.mkdir(parents=True, exist_ok=True)
    if local_path.exists() and not refresh:
        return local_path
    if refresh:
        try:
            local_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove cached CMI source before refresh: %s", local_path, exc_info=True)

    url = f"https://{bucket}.s3.amazonaws.com/{key}"
    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            urlretrieve(url, str(local_path))
            return local_path
        except (URLError, OSError) as exc:
            last_error = exc
            try:
                local_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Failed to remove partial CMI download: %s", local_path, exc_info=True)

            if attempt >= DOWNLOAD_ATTEMPTS:
                break

            logger.warning(
                "Retrying CMI download after NOAA connection failure: attempt %s/%s url=%s error=%s",
                attempt,
                DOWNLOAD_ATTEMPTS,
                url,
                exc,
            )
            time.sleep(DOWNLOAD_RETRY_DELAY_SECONDS)

    raise CMIFetchError(f"Unable to download CMI file from NOAA: {last_error}") from last_error


def materialize_file(file_ref: str, refresh: bool = False) -> Path:
    local = Path(file_ref)
    if local.exists() and not refresh:
        return local

    s3_key = _public_bucket_s3_key(file_ref)
    if s3_key is not None:
        return _download_from_public_bucket(s3_key=s3_key, refresh=refresh)

    temp_relative = Path(gettempdir()) / file_ref
    if temp_relative.exists() and not refresh:
        return temp_relative

    raise CMIFetchError(f"goes2go did not return a usable CMI file path: {file_ref}")


def _read_cmi_values(nc_path: Path) -> tuple[np.ndarray, float | None]:
    with NETCDF_LOCK:
        ds = None
        try:
            ds = Dataset(str(nc_path), mode="r")
            cmi_var = ds.variables["CMI"]
            cmi_data = cmi_var[:]
            if isinstance(cmi_data, np.ma.MaskedArray):
                values = np.asarray(cmi_data.filled(np.nan), dtype=np.float32)
            else:
                values = np.asarray(cmi_data, dtype=np.float32)
            fill_value = getattr(cmi_var, "_FillValue", None)
            return values, fill_value
        except KeyError as exc:
            raise CMIFetchError(f"Unexpected CMI file format, missing variable: {exc}") from exc
        except OSError as exc:
            raise CMIFetchError(f"Unable to read CMI file with netCDF4: {nc_path}: {exc}") from exc
        finally:
            if ds is not None:
                ds.close()


def _projection_and_transform(nc_path: Path, width: int, height: int) -> tuple[object, object]:
    rasterio = _require_rasterio()
    from rasterio.crs import CRS
    from rasterio.transform import from_bounds

    with NETCDF_LOCK:
        ds = None
        try:
            ds = Dataset(str(nc_path), mode="r")
            projection = ds.variables["goes_imager_projection"]
            sat_height = float(projection.perspective_point_height)
            semi_major = float(projection.semi_major_axis)
            semi_minor = float(projection.semi_minor_axis)
            lon_0 = float(projection.longitude_of_projection_origin)
            sweep = str(getattr(projection, "sweep_angle_axis", "x"))

            x = np.asarray(ds.variables["x"][:], dtype=np.float64) * sat_height
            y = np.asarray(ds.variables["y"][:], dtype=np.float64) * sat_height

            transform = from_bounds(
                float(np.min(x)),
                float(np.min(y)),
                float(np.max(x)),
                float(np.max(y)),
                width,
                height,
            )
            crs = CRS.from_proj4(
                f"+proj=geos +h={sat_height} +lon_0={lon_0} +a={semi_major} +b={semi_minor} +sweep={sweep} +no_defs"
            )
            return crs, transform
        except KeyError as exc:
            raise CMIFetchError(f"Unexpected CMI file format, missing variable: {exc}") from exc
        except OSError as exc:
            raise CMIFetchError(f"Unable to read CMI projection with netCDF4: {nc_path}: {exc}") from exc
        finally:
            if ds is not None:
                ds.close()


def _valid_cmi_mask(cmi_values: np.ndarray, fill_value: float | None) -> np.ndarray:
    valid_mask = np.isfinite(cmi_values)
    if fill_value is not None:
        valid_mask &= cmi_values != float(fill_value)
    return valid_mask


def _cmi_to_grayscale(cmi_values: np.ndarray, fill_value: float | None) -> tuple[np.ndarray, np.ndarray]:
    values = np.asarray(cmi_values, dtype=np.float32)
    valid_mask = _valid_cmi_mask(values, fill_value=fill_value)
    clipped = np.clip(values, TEMP_COLD_K, TEMP_WARM_K)

    visible_range = max(TEMP_VISIBLE_CLOUD_K - TEMP_DENSE_CLOUD_K, 1.0)
    cloud_focus = np.clip((TEMP_VISIBLE_CLOUD_K - clipped) / visible_range, 0.0, 1.0)
    brightness = np.power(cloud_focus, 0.85)

    gray = np.where(valid_mask, np.clip(80.0 + brightness * 175.0, 0, 255), 0).astype(np.uint8)
    alpha = np.where(valid_mask, np.clip(np.power(cloud_focus, 1.35) * 255.0, 0, 255), 0).astype(np.uint8)
    return gray, alpha


def _box_blur(values: np.ndarray, radius: int) -> np.ndarray:
    if radius <= 0:
        return values.astype(np.float32, copy=True)

    working = np.asarray(values, dtype=np.float32)
    padded = np.pad(working, ((radius, radius), (radius, radius)), mode="edge")
    integral = np.pad(padded, ((1, 0), (1, 0)), mode="constant").cumsum(axis=0).cumsum(axis=1)
    kernel = radius * 2 + 1
    total = (
        integral[kernel:, kernel:]
        - integral[:-kernel, kernel:]
        - integral[kernel:, :-kernel]
        + integral[:-kernel, :-kernel]
    )
    return total / float(kernel * kernel)


def _smooth_coverage_mask(values: np.ndarray, radius: int = EDGE_SMOOTH_RADIUS, passes: int = EDGE_SMOOTH_PASSES) -> np.ndarray:
    smoothed = np.asarray(values, dtype=np.float32)
    for _ in range(max(passes, 0)):
        smoothed = _box_blur(smoothed, radius=radius)
    return np.clip(smoothed, 0, 255).astype(np.uint8)


def frame_raster_path(satellite: str, frame_id: str) -> Path:
    return RASTER_DIR / satellite / f"{frame_id}.tif"


def frame_image_path(satellite: str, frame_id: str) -> Path:
    return IMAGE_DIR / satellite / f"{frame_id}.png"


def frame_metadata_path(satellite: str, frame_id: str) -> Path:
    return METADATA_DIR / satellite / f"{frame_id}.json"


def _source_can_refresh(file_ref: str, error: CMIFetchError) -> bool:
    return _public_bucket_s3_key(file_ref) is not None and isinstance(error.__cause__, OSError)


def build_frame_raster(frame: CMIFrame) -> Path:
    _ensure_cache_dirs()
    raster_path = frame_raster_path(satellite=frame.satellite, frame_id=frame.frame_id)
    if raster_path.exists():
        return raster_path

    frame_lock = _frame_lock_for(frame.satellite, frame.frame_id)
    with frame_lock:
        if raster_path.exists():
            return raster_path

        last_error: CMIFetchError | None = None
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                source_path = materialize_file(frame.file_ref, refresh=attempt > 1)
                values, fill_value = _read_cmi_values(source_path)
                height, width = values.shape
                crs, transform = _projection_and_transform(source_path, width=width, height=height)
                break
            except CMIFetchError as exc:
                last_error = exc
                if attempt >= DOWNLOAD_ATTEMPTS or not _source_can_refresh(frame.file_ref, exc):
                    raise
                logger.warning(
                    "Refreshing cached CMI source after netCDF read failure: "
                    "satellite=%s frame_id=%s attempt=%s/%s error=%s",
                    frame.satellite,
                    frame.frame_id,
                    attempt,
                    DOWNLOAD_ATTEMPTS,
                    exc,
                )
                time.sleep(DOWNLOAD_RETRY_DELAY_SECONDS)
        else:
            raise CMIFetchError(f"Unable to read CMI source: {last_error}") from last_error

        gray, alpha = _cmi_to_grayscale(values, fill_value=fill_value)
        coverage = np.where(_valid_cmi_mask(values, fill_value=fill_value), 255, 0).astype(np.uint8)

        raster_path.parent.mkdir(parents=True, exist_ok=True)
        profile = {
            "driver": "GTiff",
            "width": width,
            "height": height,
            "count": 3,
            "dtype": "uint8",
            "crs": crs,
            "transform": transform,
            "compress": "deflate",
            "predictor": 2,
            "tiled": True,
            "blockxsize": 512,
            "blockysize": 512,
        }
        rasterio = _require_rasterio()
        with rasterio.open(raster_path, "w", **profile) as dst:
            dst.write(gray, 1)
            dst.write(alpha, 2)
            dst.write(coverage, 3)
        return raster_path


def _frame_coordinates_from_source(src, coverage: np.ndarray) -> list[list[float]]:
    rasterio = _require_rasterio()
    from rasterio.transform import xy
    from rasterio.warp import transform_bounds

    rows, cols = np.nonzero(coverage > 0)
    if rows.size == 0 or cols.size == 0:
        raise CMIFetchError("CMI frame has no valid coverage.")

    min_row = int(rows.min())
    max_row = int(rows.max())
    min_col = int(cols.min())
    max_col = int(cols.max())

    west_x, north_y = xy(src.transform, min_row, min_col, offset="ul")
    east_x, south_y = xy(src.transform, max_row, max_col, offset="lr")
    west, south, east, north = transform_bounds(
        src.crs,
        "EPSG:4326",
        west_x,
        south_y,
        east_x,
        north_y,
        densify_pts=21,
    )
    return [
        [float(west), float(north)],
        [float(east), float(north)],
        [float(east), float(south)],
        [float(west), float(south)],
    ]


def _write_frame_metadata(metadata_path: Path, coordinates: list[list[float]]) -> None:
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps({"coordinates": coordinates}), encoding="utf-8")


def _read_frame_metadata(metadata_path: Path) -> list[list[float]]:
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    coordinates = payload["coordinates"]
    if not isinstance(coordinates, list) or len(coordinates) != 4:
        raise CMIFetchError(f"Invalid image metadata in {metadata_path}")
    return coordinates


def render_frame_image(frame: CMIFrame) -> tuple[Path, list[list[float]]]:
    _ensure_cache_dirs()
    image_path = frame_image_path(frame.satellite, frame.frame_id)
    metadata_path = frame_metadata_path(frame.satellite, frame.frame_id)
    if image_path.exists() and metadata_path.exists():
        return image_path, _read_frame_metadata(metadata_path)

    image_lock = _image_lock_for(frame.satellite, frame.frame_id)
    with image_lock:
        if image_path.exists() and metadata_path.exists():
            return image_path, _read_frame_metadata(metadata_path)

        raster_path = build_frame_raster(frame)
        image_path.parent.mkdir(parents=True, exist_ok=True)

        rasterio = _require_rasterio()
        from rasterio.enums import Resampling
        from rasterio.transform import from_bounds as transform_from_bounds
        from rasterio.warp import reproject

        with rasterio.open(raster_path) as src:
            source_coverage = src.read(3)
            coordinates = _frame_coordinates_from_source(src, source_coverage)
            west, north = coordinates[0]
            east, south = coordinates[2]

            width = src.width
            height = src.height
            image_transform = transform_from_bounds(west, south, east, north, width, height)
            gray = np.zeros((height, width), dtype=np.uint8)
            alpha = np.zeros((height, width), dtype=np.uint8)
            coverage = np.zeros((height, width), dtype=np.uint8)

            reproject(
                source=rasterio.band(src, 1),
                destination=gray,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=image_transform,
                dst_crs="EPSG:4326",
                dst_nodata=0,
                resampling=Resampling.bilinear,
            )
            reproject(
                source=rasterio.band(src, 2),
                destination=alpha,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=image_transform,
                dst_crs="EPSG:4326",
                dst_nodata=0,
                resampling=Resampling.bilinear,
            )
            reproject(
                source=rasterio.band(src, 3),
                destination=coverage,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=image_transform,
                dst_crs="EPSG:4326",
                dst_nodata=0,
                resampling=Resampling.bilinear,
            )

        coverage = _smooth_coverage_mask(np.clip(coverage, 0, 255).astype(np.uint8))
        alpha = ((alpha.astype(np.uint16) * coverage.astype(np.uint16)) // 255).astype(np.uint8)
        alpha[coverage < 6] = 0
        gray[alpha == 0] = 0

        rgba = np.stack((gray, gray, gray, alpha), axis=0).astype(np.uint8)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
            with rasterio.open(
                image_path,
                "w",
                driver="PNG",
                width=width,
                height=height,
                count=4,
                dtype="uint8",
            ) as dst:
                dst.write(rgba)

        _write_frame_metadata(metadata_path, coordinates)
        return image_path, coordinates


def prepare_frame(frame: CMIFrame) -> CMIFrame:
    if has_frame(frame.satellite, frame.frame_id):
        return frame

    build_frame_raster(frame)
    render_frame_image(frame)
    return store_prepared_frame(frame)


def prepare_frame_with_tracking(frame: CMIFrame) -> CMIFrame:
    entry, is_owner = _begin_frame_warmup(frame)
    if not is_owner:
        return wait_for_frame_warmup(frame.satellite, frame.frame_id)

    try:
        prepared = prepare_frame(frame)
    except Exception as exc:
        _finish_frame_warmup(entry, error=exc)
        raise

    _finish_frame_warmup(entry)
    return prepared


def get_prepared_image_artifacts(satellite: str, frame_id: str) -> tuple[Path, list[list[float]]]:
    image_path = frame_image_path(satellite, frame_id)
    metadata_path = frame_metadata_path(satellite, frame_id)
    if image_path.exists() and metadata_path.exists():
        return image_path, _read_frame_metadata(metadata_path)

    frame = get_frame(satellite=satellite, frame_id=frame_id)
    return render_frame_image(frame=frame)


def _path_frame_timestamp(path: Path) -> datetime | None:
    try:
        stem = path.stem if path.suffix else path.name
        start_token, _ = _extract_tokens(stem)
    except CMIFetchError:
        return None
    return _iso_to_utc_datetime(_goes_token_to_iso(start_token))


def cleanup_stale_cache(now: datetime | None = None) -> None:
    cutoff = _retention_cutoff(now=now)
    for root in (SOURCE_DIR, RASTER_DIR, IMAGE_DIR, METADATA_DIR):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                frame_timestamp = _path_frame_timestamp(path)
                if frame_timestamp is None:
                    continue
                if frame_timestamp < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Failed to remove stale CMI cache file: %s", path)
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    continue


def _prepare_latest_frame(satellite: str) -> None:
    frames = discover_recent_frames(satellite, lookback_hours=INCREMENTAL_LOOKBACK_HOURS)
    if frames:
        prepare_frame_with_tracking(frames[0])


def _prepare_missing_frames(
    frames: list[CMIFrame],
    progress_label: str | None = None,
) -> _PrepareFramesResult:
    result = _PrepareFramesResult(total=len(frames))
    for index, frame in enumerate(reversed(frames), start=1):
        if not _frame_is_within_retention(frame):
            result.skipped_retention += 1
            continue
        if has_frame(frame.satellite, frame.frame_id):
            result.skipped_cached += 1
            continue
        try:
            if progress_label is not None:
                logger.info(
                    "%s preparing frame %s/%s: satellite=%s frame_id=%s start_time=%s end_time=%s",
                    progress_label,
                    index,
                    len(frames),
                    frame.satellite,
                    frame.frame_id,
                    frame.start_time,
                    frame.end_time,
                )
            prepare_frame_with_tracking(frame)
            result.prepared += 1
            if progress_label is not None:
                logger.info(
                    "%s prepared frame %s/%s: satellite=%s frame_id=%s",
                    progress_label,
                    index,
                    len(frames),
                    frame.satellite,
                    frame.frame_id,
                )
        except CMIFetchError as exc:
            result.failed += 1
            logger.warning(
                "Skipping CMI frame after fetch failure: satellite=%s frame_id=%s error=%s",
                frame.satellite,
                frame.frame_id,
                exc,
            )
    return result


def _poll_once() -> None:
    for satellite in SATELLITE_TO_ID:
        try:
            frames = discover_recent_frames(satellite, lookback_hours=INCREMENTAL_LOOKBACK_HOURS)
            _prepare_missing_frames(frames)
        except CMIFetchError:
            logger.exception("Failed to refresh latest CMI frames for %s", satellite)
        except Exception:
            logger.exception("Unexpected error refreshing CMI frames for %s", satellite)
    prune_expired_frames()
    cleanup_stale_cache()


def _poll_loop() -> None:
    while not _poller_stop_event.is_set():
        _poll_once()
        _poller_stop_event.wait(POLL_INTERVAL_SECONDS)


def _warm_latest_then_poll_loop() -> None:
    for satellite in SATELLITE_TO_ID:
        if _poller_stop_event.is_set():
            return
        try:
            _prepare_latest_frame(satellite)
        except CMIFetchError:
            logger.exception("Failed to warm latest CMI frame for %s", satellite)
        except Exception:
            logger.exception("Unexpected error warming latest CMI frame for %s", satellite)

    for satellite in SATELLITE_TO_ID:
        if _poller_stop_event.is_set():
            return
        try:
            logger.info(
                "Starting CMI backfill: satellite=%s lookback_hours=%s",
                satellite,
                LOOKBACK_HOURS,
            )
            frames = discover_recent_frames(satellite, lookback_hours=LOOKBACK_HOURS)
            logger.info(
                "Discovered CMI backfill frames: satellite=%s count=%s oldest_start_time=%s newest_start_time=%s",
                satellite,
                len(frames),
                frames[-1].start_time,
                frames[0].start_time,
            )
            result = _prepare_missing_frames(
                frames,
                progress_label=f"CMI backfill for {satellite}",
            )
            logger.info(
                "Finished CMI backfill: satellite=%s total=%s prepared=%s skipped_cached=%s "
                "skipped_retention=%s failed=%s",
                satellite,
                result.total,
                result.prepared,
                result.skipped_cached,
                result.skipped_retention,
                result.failed,
            )
        except CMIFetchError:
            logger.exception("Failed to backfill CMI history for %s", satellite)
        except Exception:
            logger.exception("Unexpected error backfilling CMI history for %s", satellite)

    prune_expired_frames()
    cleanup_stale_cache()
    _poll_loop()


def start_background_refresh() -> None:
    global _poller_thread
    with _poller_lock:
        if _poller_thread is not None and _poller_thread.is_alive():
            return
        _poller_stop_event.clear()
        _poller_thread = Thread(target=_warm_latest_then_poll_loop, name="cmi-refresh-poller", daemon=True)
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
