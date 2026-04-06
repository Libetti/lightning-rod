from __future__ import annotations

import tempfile
import unittest
import os
import threading
import warnings
from pathlib import Path
from unittest.mock import patch

import numpy as np

from cmi import ingest as cmi
from cmi import service as cmi_service


class CMIUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        from cmi import store

        store._latest_by_satellite.clear()
        store._frames_by_satellite.clear()
        store._frame_index.clear()
        cmi._poller_stop_event.clear()
        with cmi._frame_warmups_guard:
            cmi._frame_warmups.clear()

    def test_frames_from_file_refs_dedupes_and_sorts_newest_first(self) -> None:
        file_refs = [
            "s3://noaa-goes19/ABI-L2-CMIPF/2026/076/10/"
            "OR_ABI-L2-CMIPF-M6C13_G19_s20260761000101_e20260761009409_c20260761010155.nc",
            "s3://noaa-goes19/ABI-L2-CMIPF/2026/076/09/"
            "OR_ABI-L2-CMIPF-M6C13_G19_s20260760950101_e20260760959409_c20260761000155.nc",
            # duplicate frame id via alternate path format
            "noaa-goes19/ABI-L2-CMIPF/2026/076/10/"
            "OR_ABI-L2-CMIPF-M6C13_G19_s20260761000101_e20260761009409_c20260761010155.nc",
        ]

        frames = cmi._frames_from_file_refs("goes-east", file_refs)

        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0].satellite, "goes-east")
        self.assertGreater(frames[0].start_time, frames[1].start_time)

    def test_store_keeps_recent_frames_sorted_by_frame_time_after_backfill(self) -> None:
        from cmi import store

        newest = cmi.CMIFrame(
            frame_id="frame-newest",
            satellite="goes-east",
            start_time="2026-04-06T10:10:00Z",
            end_time="2026-04-06T10:19:59Z",
            file_ref="s3://noaa-goes19/path/frame-newest.nc",
        )
        older = cmi.CMIFrame(
            frame_id="frame-older",
            satellite="goes-east",
            start_time="2026-04-06T10:00:00Z",
            end_time="2026-04-06T10:09:59Z",
            file_ref="s3://noaa-goes19/path/frame-older.nc",
        )

        store.store_prepared_frame(newest)
        store.store_prepared_frame(older)

        frames = store.get_recent_frames("goes-east", limit=2)
        latest, _ = store.get_latest_frame("goes-east")

        self.assertEqual([frame.frame_id for frame in frames], ["frame-newest", "frame-older"])
        self.assertEqual(latest.frame_id, "frame-newest")

    def test_cleanup_stale_cache_removes_old_files_and_keeps_new_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source_dir = root / "source"
            raster_dir = root / "rasters"
            tile_dir = root / "tiles"
            source_dir.mkdir(parents=True)
            raster_dir.mkdir(parents=True)
            tile_dir.mkdir(parents=True)

            old_file = source_dir / "old.nc"
            fresh_file = tile_dir / "fresh.png"
            old_file.write_bytes(b"old")
            fresh_file.write_bytes(b"fresh")

            old_epoch = 1000
            fresh_epoch = 3000

            with patch.object(cmi, "SOURCE_DIR", source_dir), patch.object(cmi, "RASTER_DIR", raster_dir), patch.object(
                cmi, "TILE_DIR", tile_dir
            ), patch.object(cmi, "time", return_value=4000):
                # force deterministic mtimes
                os.utime(old_file, (old_epoch, old_epoch))
                os.utime(fresh_file, (fresh_epoch, fresh_epoch))

                # use 1500s retention => cutoff=2500, so old is deleted and fresh remains
                cmi.cleanup_stale_cache(retention_seconds=1500)

            self.assertFalse(old_file.exists())
            self.assertTrue(fresh_file.exists())

    def test_render_tile_returns_cached_file_without_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            tile_dir = root / "tiles"
            tile_path = tile_dir / "goes-east" / "frame-1" / str(cmi.NATIVE_ZOOM) / "1" / "3.png"
            tile_path.parent.mkdir(parents=True, exist_ok=True)
            tile_path.write_bytes(b"\x89PNG\r\n\x1a\n")

            with patch.object(cmi, "TILE_DIR", tile_dir), patch.object(
                cmi, "build_frame_raster", side_effect=AssertionError("should not render on cache hit")
            ):
                frame = cmi.CMIFrame(
                    frame_id="frame-1",
                    satellite="goes-east",
                    start_time="2026-03-16T10:00:00Z",
                    end_time="2026-03-16T10:09:59Z",
                    file_ref="s3://noaa-goes19/path/frame-1.nc",
                )
                resolved = cmi.render_tile(frame=frame, z=cmi.NATIVE_ZOOM, x=1, y=3)

            self.assertEqual(resolved, tile_path)

    def test_cmi_to_grayscale_masks_warm_background_and_emphasizes_cold_clouds(self) -> None:
        values = np.array([[280.0, 252.0, 225.0]], dtype=np.float32)

        gray, alpha = cmi._cmi_to_grayscale(values, fill_value=None)

        self.assertEqual(int(alpha[0, 0]), 0)
        self.assertGreater(int(alpha[0, 1]), 0)
        self.assertGreater(int(alpha[0, 2]), int(alpha[0, 1]))
        self.assertGreater(int(gray[0, 2]), int(gray[0, 1]))

    def test_prepare_frame_publishes_after_raster_build(self) -> None:
        frame = cmi.CMIFrame(
            frame_id="frame-1",
            satellite="goes-east",
            start_time="2026-03-16T10:00:00Z",
            end_time="2026-03-16T10:09:59Z",
            file_ref="s3://noaa-goes19/path/frame-1.nc",
        )

        prepared = None
        with patch.object(cmi, "build_frame_raster", return_value=Path("/tmp/frame-1.tif")), patch.object(
            cmi, "render_tile", side_effect=AssertionError("tiles should render lazily")
        ):
            prepared = cmi.prepare_frame(frame)

        from cmi import store

        self.assertEqual(prepared, frame)
        self.assertTrue(store.has_frame("goes-east", "frame-1"))

    def test_get_prepared_tile_path_renders_tile_on_demand(self) -> None:
        frame = cmi.CMIFrame(
            frame_id="frame-1",
            satellite="goes-east",
            start_time="2026-03-16T10:00:00Z",
            end_time="2026-03-16T10:09:59Z",
            file_ref="s3://noaa-goes19/path/frame-1.nc",
        )

        from cmi import store

        store.store_prepared_frame(frame)

        tile_path = Path("/tmp/frame-1.png")
        with patch.object(cmi, "render_tile", return_value=tile_path) as render_mock:
            resolved = cmi.get_prepared_tile_path(
                satellite="goes-east",
                frame_id="frame-1",
                z=cmi.NATIVE_ZOOM,
                x=1,
                y=3,
            )

        self.assertEqual(resolved, tile_path)
        render_mock.assert_called_once_with(frame=frame, z=cmi.NATIVE_ZOOM, x=1, y=3)

    def test_validate_tile_xyz_rejects_non_native_zoom(self) -> None:
        with self.assertRaises(cmi.CMIInvalidTileError):
            cmi._validate_tile_xyz(z=cmi.NATIVE_ZOOM + 1, x=0, y=0)

    def test_start_background_refresh_starts_poller_without_blocking_warmup(self) -> None:
        created_threads: list[object] = []

        class _FakeThread:
            def __init__(self, *args, **kwargs) -> None:
                self.started = False
                self.target = kwargs.get("target")
                created_threads.append(self)

            def is_alive(self) -> bool:
                return self.started

            def start(self) -> None:
                self.started = True

            def join(self, timeout: float | None = None) -> None:
                self.started = False

        with patch.object(cmi, "_prepare_latest_frame", side_effect=AssertionError("warmup should not block startup")), patch.object(
            cmi, "Thread", _FakeThread
        ):
            cmi.start_background_refresh()
            cmi.stop_background_refresh()

        self.assertEqual(len(created_threads), 1)
        self.assertIs(created_threads[0].target, cmi._warm_latest_then_poll_loop)

    def test_list_recent_cmi_file_refs_falls_back_to_local_cache_when_goes2go_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cached = (
                root
                / "noaa-goes19"
                / "ABI-L2-CMIPF"
                / "2026"
                / "094"
                / "22"
                / "OR_ABI-L2-CMIPF-M6C13_G19_s20260942240173_e20260942249481_c20260942249529.nc"
            )
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(b"nc")

            with patch.object(cmi, "gettempdir", return_value=tmp_dir), patch.object(
                cmi, "SOURCE_DIR", root / "source"
            ), patch.object(cmi, "_load_goes_timerange", side_effect=RuntimeError("network down")):
                file_refs = cmi._list_recent_cmi_file_refs(19)

        self.assertEqual(file_refs, [str(cached)])

    def test_render_tile_ignores_not_georeferenced_warning_when_promoted_to_error(self) -> None:
        frame = cmi.CMIFrame(
            frame_id="frame-1",
            satellite="goes-east",
            start_time="2026-03-16T10:00:00Z",
            end_time="2026-03-16T10:09:59Z",
            file_ref="s3://noaa-goes19/path/frame-1.nc",
        )

        class _FakeWarning(UserWarning):
            pass

        class _FakeDataset:
            def __init__(self) -> None:
                self.transform = object()
                self.crs = "EPSG:4326"

            def __enter__(self) -> "_FakeDataset":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def write(self, rgba: np.ndarray) -> None:
                self.rgba = rgba

        class _FakePngDataset(_FakeDataset):
            def __enter__(self) -> "_FakePngDataset":
                warnings.warn("Dataset has no geotransform", _FakeWarning)
                return self

        class _FakeRasterio:
            class errors:
                NotGeoreferencedWarning = _FakeWarning

            @staticmethod
            def open(*args, **kwargs):
                if len(args) >= 2 and args[1] == "w":
                    return _FakePngDataset()
                return _FakeDataset()

            @staticmethod
            def band(src, index: int) -> tuple[object, int]:
                return (src, index)

        class _FakeMercantile:
            class Tile:
                def __init__(self, x: int, y: int, z: int) -> None:
                    self.x = x
                    self.y = y
                    self.z = z

            @staticmethod
            def xy_bounds(tile: object):
                class _Bounds:
                    left = 0.0
                    bottom = 0.0
                    right = 1.0
                    top = 1.0

                return _Bounds()

        class _FakeResampling:
            bilinear = object()
            nearest = object()

        reproject_resampling: list[object] = []

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            raster_path = root / "frame.tif"
            tile_dir = root / "tiles"
            tile_path = tile_dir / "goes-east" / "frame-1" / str(cmi.NATIVE_ZOOM) / "0" / "0.png"

            def _fake_reproject(*args, **kwargs) -> None:
                reproject_resampling.append(kwargs["resampling"])
                destination = kwargs["destination"]
                destination[:] = 255

            with patch.object(cmi, "TILE_DIR", tile_dir), patch.object(
                cmi, "build_frame_raster", return_value=raster_path
            ), patch.object(
                cmi, "_require_tile_dependencies", return_value=(_FakeMercantile(), _FakeRasterio())
            ):
                with patch.dict("sys.modules", {
                    "rasterio.enums": type("_EnumsModule", (), {"Resampling": _FakeResampling})(),
                    "rasterio.transform": type("_TransformModule", (), {"from_bounds": lambda *args, **kwargs: object()})(),
                    "rasterio.warp": type("_WarpModule", (), {"reproject": _fake_reproject})(),
                }):
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", _FakeWarning)
                        resolved = cmi.render_tile(frame=frame, z=cmi.NATIVE_ZOOM, x=0, y=0)

        self.assertEqual(resolved, tile_path)
        self.assertEqual(reproject_resampling, [_FakeResampling.bilinear, _FakeResampling.bilinear])

    def test_prepare_frame_with_tracking_collapses_concurrent_work(self) -> None:
        frame = cmi.CMIFrame(
            frame_id="frame-1",
            satellite="goes-east",
            start_time="2026-03-16T10:00:00Z",
            end_time="2026-03-16T10:09:59Z",
            file_ref="s3://noaa-goes19/path/frame-1.nc",
        )
        start_gate = threading.Event()
        release_gate = threading.Event()
        results: list[cmi.CMIFrame] = []
        errors: list[Exception] = []
        prepare_calls: list[str] = []

        def _fake_prepare(inner_frame: cmi.CMIFrame) -> cmi.CMIFrame:
            prepare_calls.append(inner_frame.frame_id)
            start_gate.set()
            release_gate.wait(timeout=2.0)
            return inner_frame

        def _run_worker() -> None:
            try:
                results.append(cmi.prepare_frame_with_tracking(frame))
            except Exception as exc:
                errors.append(exc)

        with patch.object(cmi, "prepare_frame", side_effect=_fake_prepare):
            owner = threading.Thread(target=_run_worker)
            waiter = threading.Thread(target=_run_worker)
            owner.start()
            start_gate.wait(timeout=2.0)
            waiter.start()
            release_gate.set()
            owner.join(timeout=2.0)
            waiter.join(timeout=2.0)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(prepare_calls, ["frame-1"])
        with cmi._frame_warmups_guard:
            self.assertEqual(cmi._frame_warmups, {})

    def test_prepare_frame_with_tracking_clears_registry_after_failure(self) -> None:
        frame = cmi.CMIFrame(
            frame_id="frame-1",
            satellite="goes-east",
            start_time="2026-03-16T10:00:00Z",
            end_time="2026-03-16T10:09:59Z",
            file_ref="s3://noaa-goes19/path/frame-1.nc",
        )
        calls = {"count": 0}

        def _fake_prepare(inner_frame: cmi.CMIFrame) -> cmi.CMIFrame:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("boom")
            return inner_frame

        with patch.object(cmi, "prepare_frame", side_effect=_fake_prepare):
            with self.assertRaises(RuntimeError):
                cmi.prepare_frame_with_tracking(frame)

            prepared = cmi.prepare_frame_with_tracking(frame)

        self.assertEqual(prepared, frame)
        self.assertEqual(calls["count"], 2)
        with cmi._frame_warmups_guard:
            self.assertEqual(cmi._frame_warmups, {})

    def test_service_get_recent_frames_reads_cache_only(self) -> None:
        frame = cmi.CMIFrame(
            frame_id="frame-1",
            satellite="goes-east",
            start_time="2026-03-16T10:00:00Z",
            end_time="2026-03-16T10:09:59Z",
            file_ref="s3://noaa-goes19/path/frame-1.nc",
        )

        with patch.object(cmi_service.store, "get_recent_frames", return_value=[frame]) as get_recent_mock, patch.object(
            cmi_service.ingest, "discover_recent_frames", side_effect=AssertionError("request path should not discover")
        ):
            frames = cmi_service.get_recent_frames(satellite="goes-east", limit=12)

        self.assertEqual(frames, [frame])
        get_recent_mock.assert_called_once_with(satellite="goes-east", limit=12)

    def test_service_get_tile_path_waits_for_in_progress_warmup(self) -> None:
        tile_path = Path("/tmp/frame-1.png")

        with patch.object(
            cmi_service.store, "get_frame", side_effect=cmi_service.CMIFrameNotFoundError("missing")
        ), patch.object(
            cmi_service.ingest, "wait_for_frame_warmup", return_value=cmi.CMIFrame(
                frame_id="frame-1",
                satellite="goes-east",
                start_time="2026-03-16T10:00:00Z",
                end_time="2026-03-16T10:09:59Z",
                file_ref="s3://noaa-goes19/path/frame-1.nc",
            )
        ) as wait_mock, patch.object(
            cmi_service.ingest, "get_prepared_tile_path",
            side_effect=[cmi_service.CMIFrameNotFoundError("missing"), tile_path],
        ) as get_tile_mock:
            resolved = cmi_service.get_tile_path(
                frame_id="frame-1",
                satellite="goes-east",
                z=2,
                x=1,
                y=1,
            )

        self.assertEqual(resolved, tile_path)
        self.assertEqual(wait_mock.call_count, 2)
        self.assertEqual(get_tile_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
