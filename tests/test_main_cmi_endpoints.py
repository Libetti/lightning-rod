from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, Response
from fastapi.responses import FileResponse
from starlette.requests import Request

import main
from cmi.service import CMIFrame, FRAMES_CACHE_TTL_SECONDS, NATIVE_ZOOM
from glm.service import FlashEvent, GLMFrame, GLMFetchError, RECENT_CACHE_TTL_SECONDS


def _request(base_url: str = "http://testserver/") -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }
    if base_url.startswith("https://"):
        scope["scheme"] = "https"
    return Request(scope)


class MainCMIEndpointTests(unittest.TestCase):
    def test_lightning_latest_frame_returns_metadata(self) -> None:
        frame = GLMFrame(
            frame_id="glm-frame-1",
            satellite="goes-east",
            start_time="2026-03-30T00:00:00Z",
            end_time="2026-03-30T00:02:00Z",
            source_file="/tmp/frame.nc",
        )
        points = [FlashEvent(id="a", latitude=1.0, longitude=2.0, time="2026-03-30T00:01:00Z", energy=None)]

        with patch.object(main, "get_latest_frame", return_value=(frame, "2026-03-30T00:02:30Z")), patch.object(
            main, "get_latest_points", return_value=(frame, points, "2026-03-30T00:02:30Z")
        ):
            payload = main.lightning_latest_frame(satellite="goes-east")

        self.assertEqual(payload.frame_id, "glm-frame-1")
        self.assertEqual(payload.flash_count, 1)

    def test_lightning_latest_points_returns_points_payload(self) -> None:
        response = Response()
        frame = GLMFrame(
            frame_id="glm-frame-1",
            satellite="goes-west",
            start_time="2026-03-30T00:00:00Z",
            end_time="2026-03-30T00:02:00Z",
            source_file="/tmp/frame.nc",
        )
        points = [FlashEvent(id="a", latitude=1.0, longitude=2.0, time="2026-03-30T00:01:00Z", energy=None)]

        with patch.object(main, "get_latest_points", return_value=(frame, points, "2026-03-30T00:02:30Z")):
            payload = main.lightning_latest_points(response=response, satellite="goes-west", limit=100)

        self.assertEqual(payload.frame_id, "glm-frame-1")
        self.assertEqual(payload.count, 1)
        self.assertEqual(response.headers["Cache-Control"], f"public, max-age={RECENT_CACHE_TTL_SECONDS}")

    def test_lightning_latest_points_returns_503_without_cache(self) -> None:
        response = Response()

        with patch.object(main, "get_latest_points", side_effect=GLMFetchError("No cached lightning frame available yet.")):
            with self.assertRaises(HTTPException) as ctx:
                main.lightning_latest_points(response=response, satellite="goes-east", limit=100)

        self.assertEqual(ctx.exception.status_code, 503)

    def test_frames_endpoint_returns_metadata(self) -> None:
        mock_frames = [
            CMIFrame(
                frame_id="frame-new",
                satellite="goes-east",
                start_time="2026-03-16T10:00:00Z",
                end_time="2026-03-16T10:09:59Z",
                file_ref="s3://noaa-goes19/path/frame-new.nc",
            ),
            CMIFrame(
                frame_id="frame-old",
                satellite="goes-east",
                start_time="2026-03-16T09:50:00Z",
                end_time="2026-03-16T09:59:59Z",
                file_ref="s3://noaa-goes19/path/frame-old.nc",
            ),
        ]

        response = Response()
        with patch.object(main, "get_recent_frames", return_value=mock_frames):
            payload = main.cmi_ch13_frames(
                request=_request(),
                response=response,
                satellite="goes-east",
                limit=2,
                poll_hint=10,
            )

        self.assertEqual(payload.satellite, "goes-east")
        self.assertEqual(payload.count, 2)
        self.assertEqual(payload.poll_interval_seconds, 10)
        self.assertEqual(payload.frames[0].frame_id, "frame-new")
        self.assertIn("/imagery/cmi/ch13/tiles/goes-east/frame-new/", payload.frames[0].tile_url_template)
        self.assertEqual(response.headers["Cache-Control"], f"public, max-age={FRAMES_CACHE_TTL_SECONDS}")

    def test_frames_endpoint_supports_west_satellite(self) -> None:
        response = Response()
        with patch.object(
            main,
            "get_recent_frames",
            return_value=[
                CMIFrame(
                    frame_id="west-frame",
                    satellite="goes-west",
                    start_time="2026-03-16T10:00:00Z",
                    end_time="2026-03-16T10:09:59Z",
                    file_ref="s3://noaa-goes18/path/west-frame.nc",
                )
            ],
        ):
            payload = main.cmi_ch13_frames(
                request=_request(),
                response=response,
                satellite="goes-west",
                limit=1,
                poll_hint=10,
            )

        self.assertEqual(payload.satellite, "goes-west")
        self.assertEqual(payload.count, 1)

    def test_frames_endpoint_returns_503_without_cache(self) -> None:
        response = Response()

        with patch.object(main, "get_recent_frames", side_effect=main.CMIFetchError("No cached CMI frame available yet.")):
            with self.assertRaises(HTTPException) as ctx:
                main.cmi_ch13_frames(
                    request=_request(),
                    response=response,
                    satellite="goes-east",
                    limit=2,
                    poll_hint=10,
                )

        self.assertEqual(ctx.exception.status_code, 502)

    def test_tile_endpoint_rejects_zoom_above_max(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            main.cmi_ch13_tile(satellite="goes-east", frame_id="abc", z=NATIVE_ZOOM + 1, x=0, y=0)
        self.assertEqual(ctx.exception.status_code, 422)

    def test_tile_endpoint_serves_png_and_long_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            png_path = Path(tmp_dir) / "tile.png"
            png_path.write_bytes(
                b"\x89PNG\r\n\x1a\n"
                b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
                b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U"
                b"\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            with patch.object(main, "get_tile_path", return_value=png_path):
                response = main.cmi_ch13_tile(
                    satellite="goes-east",
                    frame_id="frame-id",
                    z=NATIVE_ZOOM,
                    x=1,
                    y=1,
                )

        self.assertIsInstance(response, FileResponse)
        self.assertEqual(response.media_type, "image/png")
        self.assertEqual(response.headers["Cache-Control"], "public, max-age=31536000, immutable")

    def test_startup_and_shutdown_wire_both_services(self) -> None:
        with patch.object(main, "start_glm_background_refresh") as start_glm, patch.object(
            main, "start_cmi_background_refresh"
        ) as start_cmi:
            import asyncio

            asyncio.run(main.startup())

        start_glm.assert_called_once_with()
        start_cmi.assert_called_once_with()

        with patch.object(main, "stop_glm_background_refresh") as stop_glm, patch.object(
            main, "stop_cmi_background_refresh"
        ) as stop_cmi:
            import asyncio

            asyncio.run(main.shutdown())

        stop_glm.assert_called_once_with()
        stop_cmi.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
