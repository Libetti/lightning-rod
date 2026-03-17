from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from app import cmi


class CMIUnitTests(unittest.TestCase):
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
            ), patch.object(cmi, "CLEANUP_MIN_INTERVAL_SECONDS", 0), patch.object(cmi, "_last_cleanup_at", 0), patch.object(
                cmi, "time", return_value=4000
            ):
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
            tile_path = tile_dir / "goes-east" / "frame-1" / "2" / "1" / "3.png"
            tile_path.parent.mkdir(parents=True, exist_ok=True)
            tile_path.write_bytes(b"\x89PNG\r\n\x1a\n")

            with patch.object(cmi, "TILE_DIR", tile_dir), patch.object(
                cmi, "ensure_frame_raster", side_effect=AssertionError("should not render on cache hit")
            ):
                resolved = cmi.render_tile(frame_id="frame-1", satellite="goes-east", z=2, x=1, y=3)

            self.assertEqual(resolved, tile_path)


if __name__ == "__main__":
    unittest.main()
