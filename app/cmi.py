from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import gettempdir
from threading import Lock
from time import monotonic, time
from urllib.error import URLError
from urllib.request import urlretrieve

import numpy as np
from netCDF4 import Dataset

logger = logging.getLogger(__name__)

SATELLITE_TO_ID = {
    "goes-east": 19,
    "goes-west": 18,
}

FRAMES_CACHE_TTL_SECONDS = 30
POLL_INTERVAL_HINT_SECONDS = 30
MAX_ZOOM = 8
DEFAULT_FRAME_LIMIT = 12
FRAME_RETENTION_SECONDS = 2 * 60 * 60
FRAME_LOOKBACK = "4h"
TILE_SIZE = 256
TEMP_COLD_K = 180.0
TEMP_WARM_K = 320.0
CLEANUP_MIN_INTERVAL_SECONDS = 120

CMI_CACHE_DIR = Path(gettempdir()) / "lightning_rod_cmi"
SOURCE_DIR = CMI_CACHE_DIR / "source"
RASTER_DIR = CMI_CACHE_DIR / "rasters"
TILE_DIR = CMI_CACHE_DIR / "tiles"

FRAME_TOKEN_PATTERN = re.compile(r"_(s\d{13,19})_(e\d{13,19})_")


class CMIFetchError(Exception):
    """Raised when we cannot fetch or parse NOAA CMI data."""


class CMIFrameNotFoundError(CMIFetchError):
    """Raised when a requested frame id cannot be resolved."""


class CMIInvalidTileError(CMIFetchError):
    """Raised when tile coordinates are invalid."""


@dataclass(frozen=True)
class CMIFrame:
    frame_id: str
    satellite: str
    start_time: str
    end_time: str
    file_ref: str


@dataclass
class _FramesCacheEntry:
    expires_at: float
    frames: list[CMIFrame]


_frames_cache: dict[tuple[str, int], _FramesCacheEntry] = {}
_frames_cache_lock = Lock()
_refresh_locks: dict[tuple[str, int], Lock] = {}
_refresh_locks_guard = Lock()
_frame_index: dict[tuple[str, str], CMIFrame] = {}
_frame_index_lock = Lock()
_frame_locks: dict[tuple[str, str], Lock] = {}
_frame_locks_guard = Lock()
_tile_locks: dict[tuple[str, str, int, int, int], Lock] = {}
_tile_locks_guard = Lock()
_cleanup_lock = Lock()
_last_cleanup_at = 0.0
_upstream_io_lock = Lock()


def _ensure_cache_dirs() -> None:
    for directory in (SOURCE_DIR, RASTER_DIR, TILE_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def _require_tile_dependencies() -> tuple[object, object]:
    try:
        import mercantile
    except Exception as exc:
        raise CMIFetchError("Missing dependency 'mercantile'. Install requirements.txt.") from exc

    try:
        import rasterio
    except Exception as exc:
        raise CMIFetchError("Missing dependency 'rasterio'. Install requirements.txt.") from exc

    return mercantile, rasterio


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


def _load_goes_timerange():
    from goes2go.data import goes_timerange as _goes_timerange

    return _goes_timerange


def _refresh_lock_for(cache_key: tuple[str, int]) -> Lock:
    with _refresh_locks_guard:
        lock = _refresh_locks.get(cache_key)
        if lock is None:
            lock = Lock()
            _refresh_locks[cache_key] = lock
        return lock


def _frame_lock_for(satellite: str, frame_id: str) -> Lock:
    key = (satellite, frame_id)
    with _frame_locks_guard:
        lock = _frame_locks.get(key)
        if lock is None:
            lock = Lock()
            _frame_locks[key] = lock
        return lock


def _tile_lock_for(satellite: str, frame_id: str, z: int, x: int, y: int) -> Lock:
    key = (satellite, frame_id, z, x, y)
    with _tile_locks_guard:
        lock = _tile_locks.get(key)
        if lock is None:
            lock = Lock()
            _tile_locks[key] = lock
        return lock


def _extract_tokens(filename: str) -> tuple[str, str]:
    match = FRAME_TOKEN_PATTERN.search(filename)
    if match is None:
        raise CMIFetchError(f"Unexpected GOES filename format: {filename}")
    return match.group(1), match.group(2)


def _goes_token_to_iso(token: str) -> str:
    # GOES token format is sYYYYJJJHHMMSS[fraction], where fraction is 0-6 digits.
    raw = token[1:]
    base = raw[:13]
    fraction = raw[13:19].ljust(6, "0")
    parsed = datetime.strptime(base, "%Y%j%H%M%S").replace(tzinfo=UTC)
    parsed = parsed + timedelta(microseconds=int(fraction))
    return parsed.isoformat().replace("+00:00", "Z")


def _token_sort_key(token: str) -> tuple[str, str]:
    raw = token[1:]
    base = raw[:13]
    fraction = raw[13:19].ljust(6, "0")
    return base, fraction


def _list_recent_cmi_file_refs(satellite_id: int) -> list[str]:
    try:
        goes_timerange = _load_goes_timerange()
        results = goes_timerange(
            satellite=satellite_id,
            product="ABI-L2-CMIP",
            domain="F",
            bands=13,
            recent=FRAME_LOOKBACK,
            return_as="filelist",
            download=False,
            save_dir=gettempdir(),
            s3_refresh=True,
            ignore_missing=True,
            verbose=False,
        )
    except Exception as exc:
        logger.exception(
            "Failed to list recent CMI files: satellite_id=%s lookback=%s",
            satellite_id,
            FRAME_LOOKBACK,
        )
        raise CMIFetchError(f"Unable to resolve recent CMI files with goes2go: {exc}") from exc

    if results is None:
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


def _download_from_public_bucket(s3_key: str) -> Path:
    bucket, key = s3_key.split("/", 1)
    local_path = SOURCE_DIR / bucket / key
    local_path.parent.mkdir(parents=True, exist_ok=True)
    if local_path.exists():
        return local_path

    url = f"https://{bucket}.s3.amazonaws.com/{key}"
    logger.info(
        "Downloading CMI source file from NOAA: url=%s local_path=%s",
        url,
        local_path,
    )
    try:
        urlretrieve(url, str(local_path))
    except (URLError, OSError) as exc:
        logger.exception(
            "Failed downloading CMI source file: url=%s local_path=%s",
            url,
            local_path,
        )
        raise CMIFetchError(f"Unable to download CMI file from NOAA: {exc}") from exc
    logger.info("Downloaded CMI source file: local_path=%s", local_path)
    return local_path


def _materialize_file(file_ref: str) -> Path:
    local = Path(file_ref)
    if local.exists():
        return local

    # goes2go can return either s3://bucket/key or bucket/key.
    s3_key = file_ref.removeprefix("s3://")
    if s3_key.startswith("noaa-goes") and "/" in s3_key:
        return _download_from_public_bucket(s3_key=s3_key)

    temp_relative = Path(gettempdir()) / file_ref
    if temp_relative.exists():
        return temp_relative

    raise CMIFetchError(f"goes2go did not return a usable CMI file path: {file_ref}")


def _projection_and_transform(nc_path: Path, width: int, height: int) -> tuple[object, object]:
    _, rasterio = _require_tile_dependencies()
    from rasterio.crs import CRS
    from rasterio.transform import from_bounds

    ds = Dataset(str(nc_path), mode="r")
    try:
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
    finally:
        ds.close()


def _cmi_to_grayscale(cmi_values: np.ndarray, fill_value: float | None) -> tuple[np.ndarray, np.ndarray]:
    values = np.asarray(cmi_values, dtype=np.float32)
    valid_mask = np.isfinite(values)
    if fill_value is not None:
        valid_mask &= values != float(fill_value)

    clipped = np.clip(values, TEMP_COLD_K, TEMP_WARM_K)
    normalized = (TEMP_WARM_K - clipped) / (TEMP_WARM_K - TEMP_COLD_K)
    gray = np.where(valid_mask, np.clip(normalized * 255.0, 0, 255), 0).astype(np.uint8)
    alpha = np.where(valid_mask, 255, 0).astype(np.uint8)
    return gray, alpha


def _frame_record_for(satellite: str, frame_id: str) -> CMIFrame:
    key = (satellite, frame_id)
    with _frame_index_lock:
        frame = _frame_index.get(key)
    if frame is not None:
        return replace(frame)

    # Refresh with a wider limit before declaring frame-not-found.
    fetch_recent_cmi_frames(satellite=satellite, limit=48)
    with _frame_index_lock:
        frame = _frame_index.get(key)
    if frame is None:
        raise CMIFrameNotFoundError(f"Frame not found for {satellite}: {frame_id}")
    return replace(frame)


def _record_frames_in_index(satellite: str, frames: list[CMIFrame]) -> None:
    with _frame_index_lock:
        for frame in frames:
            _frame_index[(satellite, frame.frame_id)] = replace(frame)


def fetch_recent_cmi_frames(satellite: str = "goes-east", limit: int = DEFAULT_FRAME_LIMIT) -> list[CMIFrame]:
    satellite_id = SATELLITE_TO_ID.get(satellite)
    if not satellite_id:
        raise CMIFetchError("Unsupported satellite. Use goes-east or goes-west.")

    cache_key = (satellite, limit)
    now = monotonic()
    with _frames_cache_lock:
        cached = _frames_cache.get(cache_key)
        if cached is not None and cached.expires_at > now:
            return [replace(frame) for frame in cached.frames]
        if cached is not None:
            _frames_cache.pop(cache_key, None)

    refresh_lock = _refresh_lock_for(cache_key)
    with refresh_lock:
        now = monotonic()
        with _frames_cache_lock:
            cached = _frames_cache.get(cache_key)
            if cached is not None and cached.expires_at > now:
                return [replace(frame) for frame in cached.frames]
            if cached is not None:
                _frames_cache.pop(cache_key, None)

        with _upstream_io_lock:
            file_refs = _list_recent_cmi_file_refs(satellite_id=satellite_id)
            frames = _frames_from_file_refs(satellite=satellite, file_refs=file_refs)

        if not frames:
            raise CMIFetchError("No recent CMI frames were found.")

        _record_frames_in_index(satellite=satellite, frames=frames)
        selected = frames[:limit]
        with _frames_cache_lock:
            _frames_cache[cache_key] = _FramesCacheEntry(
                expires_at=monotonic() + FRAMES_CACHE_TTL_SECONDS,
                frames=[replace(frame) for frame in selected],
            )

        cleanup_stale_cache(retention_seconds=FRAME_RETENTION_SECONDS)
        return [replace(frame) for frame in selected]


def _frame_raster_path(satellite: str, frame_id: str) -> Path:
    return RASTER_DIR / satellite / f"{frame_id}.tif"


def ensure_frame_raster(frame_id: str, satellite: str = "goes-east") -> Path:
    if satellite not in SATELLITE_TO_ID:
        raise CMIFetchError("Unsupported satellite. Use goes-east or goes-west.")

    _ensure_cache_dirs()
    raster_path = _frame_raster_path(satellite=satellite, frame_id=frame_id)
    if raster_path.exists():
        return raster_path

    frame_lock = _frame_lock_for(satellite=satellite, frame_id=frame_id)
    with frame_lock:
        if raster_path.exists():
            return raster_path

        try:
            frame = _frame_record_for(satellite=satellite, frame_id=frame_id)
            source_path = _materialize_file(frame.file_ref)
            logger.info(
                "Building CMI raster: satellite=%s frame_id=%s source_path=%s raster_path=%s",
                satellite,
                frame_id,
                source_path,
                raster_path,
            )

            ds = Dataset(str(source_path), mode="r")
            try:
                cmi_var = ds.variables["CMI"]
                cmi_data = cmi_var[:]
                if isinstance(cmi_data, np.ma.MaskedArray):
                    values = np.asarray(cmi_data.filled(np.nan), dtype=np.float32)
                else:
                    values = np.asarray(cmi_data, dtype=np.float32)
                fill_value = getattr(cmi_var, "_FillValue", None)
            except KeyError as exc:
                raise CMIFetchError(f"Unexpected CMI file format, missing variable: {exc}") from exc
            finally:
                ds.close()

            gray, alpha = _cmi_to_grayscale(values, fill_value=fill_value)
            height, width = gray.shape
            crs, transform = _projection_and_transform(source_path, width=width, height=height)

            raster_path.parent.mkdir(parents=True, exist_ok=True)
            profile = {
                "driver": "GTiff",
                "width": width,
                "height": height,
                "count": 2,
                "dtype": "uint8",
                "crs": crs,
                "transform": transform,
                "compress": "deflate",
                "predictor": 2,
                "tiled": True,
                "blockxsize": 512,
                "blockysize": 512,
            }
            _, rasterio = _require_tile_dependencies()
            with rasterio.open(raster_path, "w", **profile) as dst:
                dst.write(gray, 1)
                dst.write(alpha, 2)
        except CMIFetchError:
            logger.exception(
                "Failed building CMI raster: satellite=%s frame_id=%s raster_path=%s",
                satellite,
                frame_id,
                raster_path,
            )
            raise
        except Exception as exc:
            logger.exception(
                "Unexpected error building CMI raster: satellite=%s frame_id=%s raster_path=%s",
                satellite,
                frame_id,
                raster_path,
            )
            raise CMIFetchError(f"Unexpected error while building raster for {frame_id}: {exc}") from exc

        logger.info(
            "Built CMI raster: satellite=%s frame_id=%s raster_path=%s",
            satellite,
            frame_id,
            raster_path,
        )
        return raster_path


def _tile_png_path(satellite: str, frame_id: str, z: int, x: int, y: int) -> Path:
    return TILE_DIR / satellite / frame_id / str(z) / str(x) / f"{y}.png"


def _validate_tile_xyz(z: int, x: int, y: int) -> None:
    if z < 0 or z > MAX_ZOOM:
        raise CMIInvalidTileError(f"Unsupported zoom level {z}. Max zoom is {MAX_ZOOM}.")
    limit = 1 << z
    if not (0 <= x < limit and 0 <= y < limit):
        raise CMIInvalidTileError(f"Tile coordinates out of range for zoom {z}: x={x}, y={y}")


def render_tile(frame_id: str, satellite: str, z: int, x: int, y: int) -> Path:
    if satellite not in SATELLITE_TO_ID:
        raise CMIFetchError("Unsupported satellite. Use goes-east or goes-west.")

    _validate_tile_xyz(z=z, x=x, y=y)
    _ensure_cache_dirs()
    tile_path = _tile_png_path(satellite=satellite, frame_id=frame_id, z=z, x=x, y=y)
    if tile_path.exists():
        return tile_path

    tile_lock = _tile_lock_for(satellite=satellite, frame_id=frame_id, z=z, x=x, y=y)
    with tile_lock:
        if tile_path.exists():
            return tile_path

        try:
            raster_path = ensure_frame_raster(frame_id=frame_id, satellite=satellite)
            tile_path.parent.mkdir(parents=True, exist_ok=True)
            logger.info(
                "Rendering CMI tile: satellite=%s frame_id=%s z=%s x=%s y=%s raster_path=%s tile_path=%s",
                satellite,
                frame_id,
                z,
                x,
                y,
                raster_path,
                tile_path,
            )

            mercantile, rasterio = _require_tile_dependencies()
            from rasterio.enums import Resampling
            from rasterio.transform import from_bounds as transform_from_bounds
            from rasterio.warp import reproject

            bounds = mercantile.xy_bounds(mercantile.Tile(x=x, y=y, z=z))
            tile_transform = transform_from_bounds(
                bounds.left,
                bounds.bottom,
                bounds.right,
                bounds.top,
                TILE_SIZE,
                TILE_SIZE,
            )
            gray = np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.uint8)
            alpha = np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.uint8)

            with rasterio.open(raster_path) as src:
                reproject(
                    source=rasterio.band(src, 1),
                    destination=gray,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=tile_transform,
                    dst_crs="EPSG:3857",
                    dst_nodata=0,
                    resampling=Resampling.bilinear,
                )
                reproject(
                    source=rasterio.band(src, 2),
                    destination=alpha,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=tile_transform,
                    dst_crs="EPSG:3857",
                    dst_nodata=0,
                    resampling=Resampling.nearest,
                )

            rgba = np.stack((gray, gray, gray, alpha), axis=0).astype(np.uint8)
            with rasterio.open(
                tile_path,
                "w",
                driver="PNG",
                width=TILE_SIZE,
                height=TILE_SIZE,
                count=4,
                dtype="uint8",
            ) as dst:
                dst.write(rgba)
        except CMIFetchError:
            logger.exception(
                "Failed rendering CMI tile: satellite=%s frame_id=%s z=%s x=%s y=%s tile_path=%s",
                satellite,
                frame_id,
                z,
                x,
                y,
                tile_path,
            )
            raise
        except Exception as exc:
            logger.exception(
                "Unexpected error rendering CMI tile: satellite=%s frame_id=%s z=%s x=%s y=%s tile_path=%s",
                satellite,
                frame_id,
                z,
                x,
                y,
                tile_path,
            )
            raise CMIFetchError(
                f"Unexpected error while rendering tile for {frame_id} ({z}/{x}/{y}): {exc}"
            ) from exc

        logger.info(
            "Rendered CMI tile: satellite=%s frame_id=%s z=%s x=%s y=%s tile_path=%s",
            satellite,
            frame_id,
            z,
            x,
            y,
            tile_path,
        )
        return tile_path


def _remove_old_files(root: Path, cutoff_epoch: float) -> None:
    if not root.exists():
        return
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff_epoch:
                path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Failed to remove stale CMI cache file: %s", path)

    # Clean empty directories from leaves upward.
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                continue


def cleanup_stale_cache(retention_seconds: int = FRAME_RETENTION_SECONDS) -> None:
    global _last_cleanup_at
    now = monotonic()
    with _cleanup_lock:
        if now - _last_cleanup_at < CLEANUP_MIN_INTERVAL_SECONDS:
            return
        _last_cleanup_at = now

    cutoff_epoch = time() - retention_seconds
    _remove_old_files(SOURCE_DIR, cutoff_epoch=cutoff_epoch)
    _remove_old_files(RASTER_DIR, cutoff_epoch=cutoff_epoch)
    _remove_old_files(TILE_DIR, cutoff_epoch=cutoff_epoch)
