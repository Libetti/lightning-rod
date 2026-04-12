from __future__ import annotations

import tempfile
import threading
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

import numpy as np

from cmi import ingest as cmi
from cmi import service as cmi_service
from glm import ingest as glm_ingest


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
            "noaa-goes19/ABI-L2-CMIPF/2026/076/10/"
            "OR_ABI-L2-CMIPF-M6C13_G19_s20260761000101_e20260761009409_c20260761010155.nc",
        ]

        frames = cmi._frames_from_file_refs("goes-east", file_refs)

        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0].satellite, "goes-east")
        self.assertGreater(frames[0].start_time, frames[1].start_time)

    def test_cmi_and_glm_share_netcdf_lock(self) -> None:
        self.assertIs(cmi.NETCDF_LOCK, glm_ingest.NETCDF_LOCK)

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

        with patch("cmi.store.datetime") as datetime_mock:
            datetime_mock.now.return_value = cmi.datetime(2026, 4, 6, 11, tzinfo=cmi.timezone.utc)
            datetime_mock.fromisoformat.side_effect = cmi.datetime.fromisoformat
            store.store_prepared_frame(newest)
            store.store_prepared_frame(older)

            frames = store.get_frames_in_range(
                "goes-east",
                start="2026-04-06T09:00:00Z",
                end="2026-04-06T11:00:00Z",
                limit=2,
            )
            latest, _ = store.get_latest_frame("goes-east")

        self.assertEqual([frame.frame_id for frame in frames], ["frame-newest", "frame-older"])
        self.assertEqual(latest.frame_id, "frame-newest")

    def test_cleanup_stale_cache_removes_old_files_and_keeps_new_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source_dir = root / "source"
            raster_dir = root / "rasters"
            image_dir = root / "images"
            metadata_dir = root / "metadata"
            source_dir.mkdir(parents=True)
            raster_dir.mkdir(parents=True)
            image_dir.mkdir(parents=True)
            metadata_dir.mkdir(parents=True)

            old_file = (
                source_dir
                / "OR_ABI-L2-CMIPF-M6C13_G19_s20260010000000_e20260010009599_c20260010010000.nc"
            )
            fresh_file = (
                image_dir
                / "OR_ABI-L2-CMIPF-M6C13_G19_s20261000000000_e20261000009599_c20261000010000.png"
            )
            old_file.write_bytes(b"old")
            fresh_file.write_bytes(b"fresh")

            with patch.object(cmi, "SOURCE_DIR", source_dir), patch.object(cmi, "RASTER_DIR", raster_dir), patch.object(
                cmi, "IMAGE_DIR", image_dir
            ), patch.object(cmi, "METADATA_DIR", metadata_dir):
                cmi.cleanup_stale_cache(now=cmi.datetime(2026, 4, 10, tzinfo=cmi.timezone.utc))

            self.assertFalse(old_file.exists())
            self.assertTrue(fresh_file.exists())

    def test_store_prunes_frames_older_than_retention_window(self) -> None:
        from cmi import store

        fresh = cmi.CMIFrame(
            frame_id="fresh-frame",
            satellite="goes-east",
            start_time="2026-04-09T10:00:00Z",
            end_time="2026-04-09T10:09:59Z",
            file_ref="s3://noaa-goes19/path/fresh-frame.nc",
        )
        stale = cmi.CMIFrame(
            frame_id="stale-frame",
            satellite="goes-east",
            start_time="2026-04-06T09:00:00Z",
            end_time="2026-04-06T09:09:59Z",
            file_ref="s3://noaa-goes19/path/stale-frame.nc",
        )

        with patch("cmi.store.datetime") as datetime_mock:
            datetime_mock.now.return_value = cmi.datetime(2026, 4, 10, tzinfo=cmi.timezone.utc)
            datetime_mock.fromisoformat.side_effect = cmi.datetime.fromisoformat
            store.store_prepared_frame(stale)
            store.store_prepared_frame(fresh)

            frames = store.get_frames_in_range(
                "goes-east",
                start="2026-04-08T00:00:00Z",
                end="2026-04-10T00:00:00Z",
                limit=10,
            )

        self.assertEqual([frame.frame_id for frame in frames], ["fresh-frame"])

    def test_render_frame_image_returns_cached_file_without_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            image_dir = root / "images"
            metadata_dir = root / "metadata"
            image_path = image_dir / "goes-east" / "frame-1.png"
            metadata_path = metadata_dir / "goes-east" / "frame-1.json"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
            metadata_path.write_text(
                '{"coordinates": [[-140.0, 55.0], [-60.0, 55.0], [-60.0, -10.0], [-140.0, -10.0]]}',
                encoding="utf-8",
            )

            with patch.object(cmi, "IMAGE_DIR", image_dir), patch.object(cmi, "METADATA_DIR", metadata_dir), patch.object(
                cmi, "build_frame_raster", side_effect=AssertionError("should not render on cache hit")
            ):
                frame = cmi.CMIFrame(
                    frame_id="frame-1",
                    satellite="goes-east",
                    start_time="2026-03-16T10:00:00Z",
                    end_time="2026-03-16T10:09:59Z",
                    file_ref="s3://noaa-goes19/path/frame-1.nc",
                )
                resolved, coordinates = cmi.render_frame_image(frame=frame)

            self.assertEqual(resolved, image_path)
            self.assertEqual(coordinates[0], [-140.0, 55.0])

    def test_cmi_to_grayscale_masks_warm_background_and_emphasizes_cold_clouds(self) -> None:
        values = np.array([[280.0, 252.0, 225.0]], dtype=np.float32)

        gray, alpha = cmi._cmi_to_grayscale(values, fill_value=None)

        self.assertEqual(int(alpha[0, 0]), 0)
        self.assertGreater(int(alpha[0, 1]), 0)
        self.assertGreater(int(alpha[0, 2]), int(alpha[0, 1]))
        self.assertGreater(int(gray[0, 2]), int(gray[0, 1]))

    def test_smooth_coverage_mask_softens_spikes(self) -> None:
        coverage = np.zeros((7, 7), dtype=np.uint8)
        coverage[:, 3] = 255

        smoothed = cmi._smooth_coverage_mask(coverage, radius=1, passes=2)

        self.assertLess(int(smoothed[3, 3]), 255)
        self.assertGreater(int(smoothed[3, 2]), 0)
        self.assertGreater(int(smoothed[3, 4]), 0)

    def test_prepare_frame_publishes_after_image_build(self) -> None:
        frame = cmi.CMIFrame(
            frame_id="frame-1",
            satellite="goes-east",
            start_time="2026-03-16T10:00:00Z",
            end_time="2026-03-16T10:09:59Z",
            file_ref="s3://noaa-goes19/path/frame-1.nc",
        )

        with patch("cmi.store.datetime") as datetime_mock, patch.object(
            cmi, "build_frame_raster", return_value=Path("/tmp/frame-1.tif")
        ), patch.object(
            cmi,
            "render_frame_image",
            return_value=(Path("/tmp/frame-1.png"), [[-1.0, 1.0], [1.0, 1.0], [1.0, -1.0], [-1.0, -1.0]]),
        ):
            datetime_mock.now.return_value = cmi.datetime(2026, 3, 16, 11, tzinfo=cmi.timezone.utc)
            datetime_mock.fromisoformat.side_effect = cmi.datetime.fromisoformat
            prepared = cmi.prepare_frame(frame)

            from cmi import store

            self.assertEqual(prepared, frame)
            self.assertTrue(store.has_frame("goes-east", "frame-1"))

    def test_get_prepared_image_artifacts_renders_on_demand(self) -> None:
        frame = cmi.CMIFrame(
            frame_id="frame-1",
            satellite="goes-east",
            start_time="2026-03-16T10:00:00Z",
            end_time="2026-03-16T10:09:59Z",
            file_ref="s3://noaa-goes19/path/frame-1.nc",
        )

        from cmi import store

        with patch("cmi.store.datetime") as datetime_mock:
            datetime_mock.now.return_value = cmi.datetime(2026, 3, 16, 11, tzinfo=cmi.timezone.utc)
            datetime_mock.fromisoformat.side_effect = cmi.datetime.fromisoformat
            store.store_prepared_frame(frame)

            coordinates = [[-140.0, 55.0], [-60.0, 55.0], [-60.0, -10.0], [-140.0, -10.0]]
            image_path = Path("/tmp/frame-1.png")
            with patch.object(cmi, "render_frame_image", return_value=(image_path, coordinates)) as render_mock:
                resolved, actual_coordinates = cmi.get_prepared_image_artifacts(
                    satellite="goes-east",
                    frame_id="frame-1",
                )

        self.assertEqual(resolved, image_path)
        self.assertEqual(actual_coordinates, coordinates)
        render_mock.assert_called_once_with(frame=frame)

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
                file_refs = cmi._list_recent_cmi_file_refs(19, recent_window="48h")

        self.assertEqual(file_refs, [str(cached)])

    def test_download_from_public_bucket_retries_connection_reset(self) -> None:
        calls: list[str] = []

        def _fake_urlretrieve(url: str, filename: str) -> tuple[str, None]:
            calls.append(url)
            if len(calls) == 1:
                Path(filename).write_bytes(b"partial")
                raise ConnectionResetError("reset by peer")
            Path(filename).write_bytes(b"nc")
            return filename, None

        with tempfile.TemporaryDirectory() as tmp_dir:
            source_dir = Path(tmp_dir) / "source"
            with patch.object(cmi, "SOURCE_DIR", source_dir), patch.object(
                cmi, "urlretrieve", side_effect=_fake_urlretrieve
            ), patch.object(cmi.time, "sleep") as sleep_mock:
                path = cmi._download_from_public_bucket("noaa-goes19/path/frame.nc")
                downloaded = path.read_bytes()

        self.assertEqual(path.name, "frame.nc")
        self.assertEqual(downloaded, b"nc")
        self.assertEqual(len(calls), 2)
        sleep_mock.assert_called_once_with(cmi.DOWNLOAD_RETRY_DELAY_SECONDS)

    def test_materialize_file_refresh_replaces_existing_public_bucket_cache(self) -> None:
        calls: list[str] = []

        def _fake_urlretrieve(url: str, filename: str) -> tuple[str, None]:
            calls.append(url)
            Path(filename).write_bytes(b"fresh")
            return filename, None

        with tempfile.TemporaryDirectory() as tmp_dir:
            source_dir = Path(tmp_dir) / "source"
            cached = source_dir / "noaa-goes19" / "path" / "frame.nc"
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(b"corrupt")

            with patch.object(cmi, "SOURCE_DIR", source_dir), patch.object(
                cmi, "urlretrieve", side_effect=_fake_urlretrieve
            ):
                path = cmi.materialize_file("s3://noaa-goes19/path/frame.nc", refresh=True)
                downloaded = path.read_bytes()

        self.assertEqual(downloaded, b"fresh")
        self.assertEqual(len(calls), 1)

    def test_read_cmi_values_wraps_netcdf_os_errors(self) -> None:
        with patch.object(cmi, "Dataset", side_effect=OSError("NetCDF: HDF error")):
            with self.assertRaises(cmi.CMIFetchError) as ctx:
                cmi._read_cmi_values(Path("/tmp/corrupt.nc"))

        self.assertIsInstance(ctx.exception.__cause__, OSError)
        self.assertIn("Unable to read CMI file", str(ctx.exception))

    def test_prepare_missing_frames_skips_failed_frame_and_continues(self) -> None:
        failed = cmi.CMIFrame(
            frame_id="frame-failed",
            satellite="goes-east",
            start_time="2026-03-16T10:00:00Z",
            end_time="2026-03-16T10:09:59Z",
            file_ref="s3://noaa-goes19/path/frame-failed.nc",
        )
        next_frame = cmi.CMIFrame(
            frame_id="frame-next",
            satellite="goes-east",
            start_time="2026-03-16T10:10:00Z",
            end_time="2026-03-16T10:19:59Z",
            file_ref="s3://noaa-goes19/path/frame-next.nc",
        )

        with patch.object(cmi, "_frame_is_within_retention", return_value=True), patch.object(
            cmi, "has_frame", return_value=False
        ), patch.object(
            cmi,
            "prepare_frame_with_tracking",
            side_effect=[cmi.CMIFetchError("reset by peer"), next_frame],
        ) as prepare_mock:
            cmi._prepare_missing_frames([next_frame, failed])

        self.assertEqual(
            [call.args[0].frame_id for call in prepare_mock.call_args_list],
            ["frame-failed", "frame-next"],
        )

    def test_render_frame_image_ignores_not_georeferenced_warning_when_promoted_to_error(self) -> None:
        frame = cmi.CMIFrame(
            frame_id="frame-1",
            satellite="goes-east",
            start_time="2026-03-16T10:00:00Z",
            end_time="2026-03-16T10:09:59Z",
            file_ref="s3://noaa-goes19/path/frame-1.nc",
        )

        class _FakeWarning(UserWarning):
            pass

        class _FakeSourceDataset:
            def __init__(self) -> None:
                self.transform = object()
                self.crs = "EPSG:4326"
                self.width = 2
                self.height = 2

            def __enter__(self) -> "_FakeSourceDataset":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def read(self, index: int) -> np.ndarray:
                if index == 3:
                    return np.array([[255, 255], [255, 255]], dtype=np.uint8)
                return np.array([[0, 128], [255, 255]], dtype=np.uint8)

        class _FakePngDataset:
            def __enter__(self) -> "_FakePngDataset":
                warnings.warn("png profile warning", _FakeWarning)
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def write(self, rgba: np.ndarray) -> None:
                self.rgba = rgba

        class _FakeRasterioErrors:
            NotGeoreferencedWarning = _FakeWarning

        class _FakeRasterio:
            errors = _FakeRasterioErrors()

            @staticmethod
            def band(src: object, index: int) -> tuple[object, int]:
                return (src, index)

            @staticmethod
            def open(path: Path, mode: str = "r", **kwargs):
                if mode == "w":
                    return _FakePngDataset()
                return _FakeSourceDataset()

        def _fake_reproject(source, destination, **kwargs) -> None:
            src, index = source
            destination[:] = src.read(index)

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            image_dir = root / "images"
            metadata_dir = root / "metadata"
            image_path = image_dir / "goes-east" / "frame-1.png"

            with patch.object(cmi, "IMAGE_DIR", image_dir), patch.object(cmi, "METADATA_DIR", metadata_dir), patch.object(
                cmi, "build_frame_raster", return_value=Path("/tmp/frame-1.tif")
            ), patch.object(
                cmi, "_require_rasterio", return_value=_FakeRasterio()
            ), patch.object(
                cmi,
                "_frame_coordinates_from_source",
                return_value=[[-140.0, 55.0], [-60.0, 55.0], [-60.0, -10.0], [-140.0, -10.0]],
            ), patch(
                "rasterio.enums.Resampling"
            ) as resampling_mock, patch(
                "rasterio.transform.from_bounds", return_value="transform"
            ), patch(
                "rasterio.warp.reproject", side_effect=_fake_reproject
            ):
                resampling_mock.bilinear = "bilinear"
                with warnings.catch_warnings():
                    warnings.simplefilter("error", _FakeWarning)
                    resolved, coordinates = cmi.render_frame_image(frame=frame)

        self.assertEqual(resolved, image_path)
        self.assertEqual(coordinates[0], [-140.0, 55.0])

    def test_prepare_frame_with_tracking_allows_single_owner(self) -> None:
        frame = cmi.CMIFrame(
            frame_id="frame-1",
            satellite="goes-east",
            start_time="2026-03-16T10:00:00Z",
            end_time="2026-03-16T10:09:59Z",
            file_ref="s3://noaa-goes19/path/frame-1.nc",
        )
        prepare_calls: list[str] = []
        start_gate = threading.Event()
        release_gate = threading.Event()
        results: list[cmi.CMIFrame] = []
        errors: list[BaseException] = []

        def _fake_prepare(inner_frame: cmi.CMIFrame) -> cmi.CMIFrame:
            prepare_calls.append(inner_frame.frame_id)
            start_gate.set()
            release_gate.wait(timeout=2.0)
            return inner_frame

        def _run_prepare() -> None:
            try:
                results.append(cmi.prepare_frame_with_tracking(frame))
            except BaseException as exc:  # pragma: no cover - test helper
                errors.append(exc)

        with patch.object(cmi, "prepare_frame", side_effect=_fake_prepare):
            owner = threading.Thread(target=_run_prepare)
            waiter = threading.Thread(target=_run_prepare)

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

    def test_service_get_frames_in_range_reads_cache_only(self) -> None:
        frame = cmi.CMIFrame(
            frame_id="frame-1",
            satellite="goes-east",
            start_time="2026-03-16T10:00:00Z",
            end_time="2026-03-16T10:09:59Z",
            file_ref="s3://noaa-goes19/path/frame-1.nc",
        )

        with patch.object(cmi_service.store, "get_frames_in_range", return_value=[frame]) as get_range_mock, patch.object(
            cmi_service.ingest, "discover_recent_frames", side_effect=AssertionError("request path should not discover")
        ):
            frames = cmi_service.get_frames_in_range(
                satellite="goes-east",
                start="2026-03-16T10:00:00Z",
                end="2026-03-16T11:00:00Z",
                limit=12,
            )

        self.assertEqual(frames, [frame])
        get_range_mock.assert_called_once_with(
            satellite="goes-east",
            start="2026-03-16T10:00:00Z",
            end="2026-03-16T11:00:00Z",
            limit=12,
        )

    def test_service_get_image_artifacts_waits_for_in_progress_warmup(self) -> None:
        image_path = Path("/tmp/frame-1.png")
        coordinates = [[-140.0, 55.0], [-60.0, 55.0], [-60.0, -10.0], [-140.0, -10.0]]

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
            cmi_service.ingest,
            "get_prepared_image_artifacts",
            side_effect=[cmi_service.CMIFrameNotFoundError("missing"), (image_path, coordinates)],
        ) as get_image_mock:
            resolved, actual_coordinates = cmi_service.get_image_artifacts(
                frame_id="frame-1",
                satellite="goes-east",
            )

        self.assertEqual(resolved, image_path)
        self.assertEqual(actual_coordinates, coordinates)
        self.assertEqual(wait_mock.call_count, 2)
        self.assertEqual(get_image_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
