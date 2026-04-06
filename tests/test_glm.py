from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from glm import service as glm


class GLMUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        glm._latest_by_satellite.clear()
        glm._frames_by_satellite.clear()
        glm._poller_stop_event.clear()

    def test_frame_from_path_parses_metadata(self) -> None:
        path = Path(
            "/tmp/OR_GLM-L2-LCFA_G18_s20260891723000_e20260891723200_c20260891723218.nc"
        )

        frame = glm._frame_from_path("goes-west", path)

        self.assertEqual(frame.frame_id, "OR_GLM-L2-LCFA_G18_s20260891723000_e20260891723200_c20260891723218")
        self.assertEqual(frame.satellite, "goes-west")
        self.assertEqual(frame.start_time, "2026-03-30T17:23:00Z")
        self.assertEqual(frame.end_time, "2026-03-30T17:23:20Z")

    def test_refresh_latest_lightning_stores_latest_frame_and_points(self) -> None:
        source = Path("/tmp/OR_GLM-L2-LCFA_G19_s20260891723000_e20260891723200_c20260891723218.nc")
        events = [
            glm.FlashEvent(id="a", latitude=1.0, longitude=2.0, time="2026-03-30T17:23:05Z", energy=0.5),
            glm.FlashEvent(id="b", latitude=3.0, longitude=4.0, time="2026-03-30T17:23:10Z", energy=None),
        ]

        with patch.object(glm, "_latest_glm_file", return_value=source), patch.object(
            glm, "_parse_flashes_direct", return_value=events
        ):
            frame = glm.refresh_latest_lightning("goes-east")
            latest_frame, updated_at = glm.get_latest_frame("goes-east")
            latest_points_frame, latest_points, points_updated_at = glm.get_latest_points("goes-east")

        self.assertEqual(frame.frame_id, latest_frame.frame_id)
        self.assertEqual(latest_points_frame.frame_id, frame.frame_id)
        self.assertEqual(len(latest_points), 2)
        self.assertEqual(latest_points[0].id, "a")
        self.assertEqual(updated_at, points_updated_at)

    def test_refresh_latest_lightning_dedupes_same_frame_id(self) -> None:
        source = Path("/tmp/OR_GLM-L2-LCFA_G19_s20260891723000_e20260891723200_c20260891723218.nc")
        events = [glm.FlashEvent(id="a", latitude=1.0, longitude=2.0, time="2026-03-30T17:23:05Z", energy=None)]

        with patch.object(glm, "_latest_glm_file", return_value=source), patch.object(
            glm, "_parse_flashes_direct", return_value=events
        ) as parse_mock:
            glm.refresh_latest_lightning("goes-east")
            glm.refresh_latest_lightning("goes-east")

        self.assertEqual(parse_mock.call_count, 1)
        self.assertEqual(len(glm._frames_by_satellite["goes-east"]), 1)

    def test_latest_glm_file_falls_back_to_local_cache_when_goes2go_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cached = (
                Path(tmp_dir)
                / "noaa-goes19"
                / "GLM-L2-LCFA"
                / "2026"
                / "094"
                / "22"
                / "OR_GLM-L2-LCFA_G19_s20260942244200_e20260942244400_c20260942244417.nc"
            )
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(b"nc")

            with patch.object(glm.ingest, "gettempdir", return_value=tmp_dir), patch.object(
                glm.ingest, "_load_goes_latest", side_effect=RuntimeError("network down")
            ):
                resolved = glm._latest_glm_file(19)

        self.assertEqual(resolved, cached)

if __name__ == "__main__":
    unittest.main()
