from __future__ import annotations

import logging
import os
import re
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import gettempdir
from threading import Event, Lock, Thread
from time import time
from urllib.error import URLError
from urllib.request import urlretrieve

import numpy as np
from netCDF4 import Dataset

from cmi.store import (
    CMIFrame,
    CMIFetchError,
    CMIFrameNotFoundError,
    CMIInvalidTileError,
    FRAME_RETENTION_COUNT,
    get_frame,
    has_frame,
    store_prepared_frame,
)

logger = logging.getLogger(__name__)

SATELLITE_TO_ID = {
    "goes-east": 19,
    "goes-west": 18,
}
POLL_INTERVAL_SECONDS = int(os.getenv("CMI_POLL_INTERVAL_SECONDS", "30"))
MAX_ZOOM = 8
FRAME_LOOKBACK = "4h"
TILE_SIZE = 256
TEMP_COLD_K = 180.0
TEMP_WARM_K = 320.0
FRAME_RETENTION_SECONDS = 2 * 60 * 60

CMI_CACHE_DIR = Path(gettempdir()) / "lightning_rod_cmi"
SOURCE_DIR = CMI_CACHE_DIR / "source"
RASTER_DIR = CMI_CACHE_DIR / "rasters"
TILE_DIR = CMI_CACHE_DIR / "tiles"

FRAME_TOKEN_PATTERN = re.compile(r"_(s\d{13,19})_(e\d{13,19})_")

_upstream_io_lock = Lock()
_poller_thread: Thread | None = None
_poller_stop_event = Event()
_poller_lock = Lock()
_frame_locks: dict[tuple[str, str], Lock] = {}
_frame_locks_guard = Lock()
_tile_locks: dict[tuple[str, str, int, int, int], Lock] = {}
_tile_locks_guard = Lock()
_frame_warmups: dict[tuple[str, str], "_FrameWarmupEntry"] = {}
_frame_warmups_guard = Lock()


@dataclass
class _FrameWarmupEntry:
    frame: CMIFrame
    done: Event = field(default_factory=Event)
    error: Exception | None = None


def _ensure_cache_dirs() -> None:
    for directory in (SOURCE_DIR, RASTER_DIR, TILE_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def _load_goes_timerange():
    from goes2go.data import goes_timerange as _goes_timerange

    return _goes_timerange


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


def _tile_lock_for(satellite: str, frame_id: str, z: int, x: int, y: int) -> Lock:
    key = (satellite, frame_id, z, x, y)
    with _tile_locks_guard:
        lock = _tile_locks.get(key)
        if lock is None:
            lock = Lock()
            _tile_locks[key] = lock
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


def _list_recent_cmi_file_refs(satellite_id: int) -> list[str]:
    local_refs = _local_cmi_file_refs(satellite_id=satellite_id)
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


def discover_recent_frames(satellite: str) -> list[CMIFrame]:
    satellite_id = SATELLITE_TO_ID.get(satellite)
    if not satellite_id:
        raise CMIFetchError("Unsupported satellite. Use goes-east or goes-west.")

    with _upstream_io_lock:
        file_refs = _list_recent_cmi_file_refs(satellite_id=satellite_id)
    frames = _frames_from_file_refs(satellite=satellite, file_refs=file_refs)
    if not frames:
        raise CMIFetchError("No recent CMI frames were found.")
    return frames


def _download_from_public_bucket(s3_key: str) -> Path:
    bucket, key = s3_key.split("/", 1)
    local_path = SOURCE_DIR / bucket / key
    local_path.parent.mkdir(parents=True, exist_ok=True)
    if local_path.exists():
        return local_path

    url = f"https://{bucket}.s3.amazonaws.com/{key}"
    try:
        urlretrieve(url, str(local_path))
    except (URLError, OSError) as exc:
        raise CMIFetchError(f"Unable to download CMI file from NOAA: {exc}") from exc
    return local_path


def materialize_file(file_ref: str) -> Path:
    local = Path(file_ref)
    if local.exists():
        return local

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


def frame_raster_path(satellite: str, frame_id: str) -> Path:
    return RASTER_DIR / satellite / f"{frame_id}.tif"


def tile_png_path(satellite: str, frame_id: str, z: int, x: int, y: int) -> Path:
    return TILE_DIR / satellite / frame_id / str(z) / str(x) / f"{y}.png"


def _validate_tile_xyz(z: int, x: int, y: int) -> None:
    if z < 0 or z > MAX_ZOOM:
        raise CMIInvalidTileError(f"Unsupported zoom level {z}. Max zoom is {MAX_ZOOM}.")
    limit = 1 << z
    if not (0 <= x < limit and 0 <= y < limit):
        raise CMIInvalidTileError(f"Tile coordinates out of range for zoom {z}: x={x}, y={y}")


def build_frame_raster(frame: CMIFrame) -> Path:
    _ensure_cache_dirs()
    raster_path = frame_raster_path(satellite=frame.satellite, frame_id=frame.frame_id)
    if raster_path.exists():
        return raster_path

    frame_lock = _frame_lock_for(frame.satellite, frame.frame_id)
    with frame_lock:
        if raster_path.exists():
            return raster_path

        source_path = materialize_file(frame.file_ref)
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
        return raster_path


def render_tile(frame: CMIFrame, z: int, x: int, y: int) -> Path:
    _validate_tile_xyz(z=z, x=x, y=y)
    _ensure_cache_dirs()
    tile_path = tile_png_path(frame.satellite, frame.frame_id, z, x, y)
    if tile_path.exists():
        return tile_path

    tile_lock = _tile_lock_for(frame.satellite, frame.frame_id, z, x, y)
    with tile_lock:
        if tile_path.exists():
            return tile_path

        raster_path = build_frame_raster(frame)
        tile_path.parent.mkdir(parents=True, exist_ok=True)

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

        rgba = np.stack((gray, gray, gray, gray), axis=0).astype(np.uint8)
        rgba[3] = alpha
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
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
        return tile_path


def prepare_frame(frame: CMIFrame) -> CMIFrame:
    if has_frame(frame.satellite, frame.frame_id):
        return frame

    build_frame_raster(frame)
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


def get_prepared_tile_path(satellite: str, frame_id: str, z: int, x: int, y: int) -> Path:
    _validate_tile_xyz(z=z, x=x, y=y)
    tile_path = tile_png_path(satellite, frame_id, z, x, y)
    if tile_path.exists():
        return tile_path

    frame = get_frame(satellite=satellite, frame_id=frame_id)
    return render_tile(frame=frame, z=z, x=x, y=y)


def cleanup_stale_cache(retention_seconds: int = FRAME_RETENTION_SECONDS) -> None:
    cutoff_epoch = time() - retention_seconds
    for root in (SOURCE_DIR, RASTER_DIR, TILE_DIR):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff_epoch:
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
    frames = discover_recent_frames(satellite)
    if frames:
        prepare_frame_with_tracking(frames[0])


def _poll_once() -> None:
    for satellite in SATELLITE_TO_ID:
        try:
            frames = discover_recent_frames(satellite)
            for frame in reversed(frames[:FRAME_RETENTION_COUNT]):
                if has_frame(frame.satellite, frame.frame_id):
                    continue
                prepare_frame_with_tracking(frame)
        except CMIFetchError:
            logger.exception("Failed to refresh latest CMI frames for %s", satellite)
        except Exception:
            logger.exception("Unexpected error refreshing CMI frames for %s", satellite)
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
