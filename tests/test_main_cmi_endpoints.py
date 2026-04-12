from __future__ import annotations

from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, Response
from fastapi.responses import FileResponse
from starlette.requests import Request

import main
from cmi.service import CMIFrame, FRAMES_CACHE_TTL_SECONDS
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
    def test_openapi_documents_cmi_image_route_as_png(self) -> None:
        schema = main.app.openapi()

        image_route = schema["paths"]["/imagery/cmi/ch13/images/{satellite}/{frame_id}.png"]["get"]
        frames_route = schema["paths"]["/imagery/cmi/ch13/frames"]["get"]
        frame_schema = schema["components"]["schemas"]["CMIFrameModel"]
        frames_response_schema = schema["components"]["schemas"]["CMIFramesResponse"]
        coordinates_schema = frame_schema["properties"]["coordinates"]
        frame_params = {param["name"]: param for param in frames_route["parameters"]}

        self.assertIn("image/png", image_route["responses"]["200"]["content"])
        self.assertEqual(frames_route["summary"], "Get CMI Frames For A Time Window")
        self.assertTrue(frame_params["start"]["required"])
        self.assertTrue(frame_params["end"]["required"])
        self.assertIn("oldest-to-newest", frames_route["description"])
        self.assertIn("oldest-to-newest", frames_response_schema["properties"]["frames"]["description"])
        self.assertEqual(coordinates_schema["minItems"], 4)
        self.assertEqual(coordinates_schema["maxItems"], 4)
        self.assertIn("top-left", coordinates_schema["description"])

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

    def test_frames_endpoint_returns_image_metadata(self) -> None:
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
        coordinates = [[-140.0, 55.0], [-60.0, 55.0], [-60.0, -10.0], [-140.0, -10.0]]

        response = Response()
        with patch.object(main, "get_frames_in_range", return_value=mock_frames), patch.object(
            main, "get_image_artifacts", return_value=(Path("/tmp/frame.png"), coordinates)
        ) as image_mock:
            payload = main.cmi_ch13_frames(
                request=_request(),
                response=response,
                satellite="goes-east",
                start=datetime(2026, 3, 16, 9, 0, tzinfo=timezone.utc),
                end=datetime(2026, 3, 16, 11, 0, tzinfo=timezone.utc),
                limit=2,
                poll_hint=10,
            )

        self.assertEqual(payload.satellite, "goes-east")
        self.assertEqual(payload.count, 2)
        self.assertEqual(payload.poll_interval_seconds, 10)
        self.assertEqual(payload.frames[0].frame_id, "frame-old")
        self.assertEqual(
            payload.frames[0].image_url,
            "http://testserver/imagery/cmi/ch13/images/goes-east/frame-old.png",
        )
        self.assertEqual(payload.frames[0].coordinates, [tuple(point) for point in coordinates])
        self.assertEqual(image_mock.call_count, 2)
        self.assertEqual(response.headers["Cache-Control"], f"public, max-age={FRAMES_CACHE_TTL_SECONDS}")

    def test_frames_endpoint_supports_west_satellite(self) -> None:
        response = Response()
        coordinates = [[-170.0, 60.0], [-90.0, 60.0], [-90.0, -20.0], [-170.0, -20.0]]
        with patch.object(
            main,
            "get_frames_in_range",
            return_value=[
                CMIFrame(
                    frame_id="west-frame",
                    satellite="goes-west",
                    start_time="2026-03-16T10:00:00Z",
                    end_time="2026-03-16T10:09:59Z",
                    file_ref="s3://noaa-goes18/path/west-frame.nc",
                )
            ],
        ), patch.object(main, "get_image_artifacts", return_value=(Path("/tmp/west-frame.png"), coordinates)):
            payload = main.cmi_ch13_frames(
                request=_request(),
                response=response,
                satellite="goes-west",
                start=datetime(2026, 3, 16, 10, 0, tzinfo=timezone.utc),
                end=datetime(2026, 3, 16, 11, 0, tzinfo=timezone.utc),
                limit=1,
                poll_hint=10,
            )

        self.assertEqual(payload.satellite, "goes-west")
        self.assertEqual(payload.count, 1)
        self.assertEqual(payload.frames[0].coordinates, [tuple(point) for point in coordinates])

    def test_frames_endpoint_supports_time_window_queries(self) -> None:
        frames = [
            CMIFrame(
                frame_id="frame-b",
                satellite="goes-east",
                start_time="2026-03-16T10:10:00Z",
                end_time="2026-03-16T10:19:59Z",
                file_ref="s3://noaa-goes19/path/frame-b.nc",
            ),
            CMIFrame(
                frame_id="frame-a",
                satellite="goes-east",
                start_time="2026-03-16T10:00:00Z",
                end_time="2026-03-16T10:09:59Z",
                file_ref="s3://noaa-goes19/path/frame-a.nc",
            ),
        ]
        coordinates = [[-140.0, 55.0], [-60.0, 55.0], [-60.0, -10.0], [-140.0, -10.0]]
        response = Response()
        with patch.object(main, "get_frames_in_range", return_value=frames) as range_mock, patch.object(
            main, "get_image_artifacts", return_value=(Path("/tmp/frame.png"), coordinates)
        ):
            payload = main.cmi_ch13_frames(
                request=_request(),
                response=response,
                satellite="goes-east",
                limit=500,
                start=datetime(2026, 3, 16, 10, 0, tzinfo=timezone.utc),
                end=datetime(2026, 3, 16, 11, 0, tzinfo=timezone.utc),
                poll_hint=3600,
            )

        self.assertEqual([frame.frame_id for frame in payload.frames], ["frame-a", "frame-b"])
        range_mock.assert_called_once_with(
            satellite="goes-east",
            start="2026-03-16T10:00:00Z",
            end="2026-03-16T11:00:00Z",
            limit=500,
        )

    def test_frames_endpoint_returns_503_without_cache(self) -> None:
        response = Response()

        with patch.object(main, "get_frames_in_range", side_effect=main.CMIFetchError("No cached CMI frame available yet.")):
            with self.assertRaises(HTTPException) as ctx:
                main.cmi_ch13_frames(
                    request=_request(),
                    response=response,
                    satellite="goes-east",
                    start=datetime(2026, 3, 16, 10, 0, tzinfo=timezone.utc),
                    end=datetime(2026, 3, 16, 11, 0, tzinfo=timezone.utc),
                    limit=2,
                    poll_hint=10,
                )

        self.assertEqual(ctx.exception.status_code, 502)

    def test_image_endpoint_serves_png_and_long_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            png_path = Path(tmp_dir) / "frame.png"
            png_path.write_bytes(
                b"\x89PNG\r\n\x1a\n"
                b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
                b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U"
                b"\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            with patch.object(
                main,
                "get_image_artifacts",
                return_value=(png_path, [[-1.0, 1.0], [1.0, 1.0], [1.0, -1.0], [-1.0, -1.0]]),
            ):
                response = main.cmi_ch13_image(
                    satellite="goes-east",
                    frame_id="frame-id",
                )

        self.assertIsInstance(response, FileResponse)
        self.assertEqual(response.media_type, "image/png")
        self.assertEqual(response.headers["Cache-Control"], "public, max-age=31536000, immutable")

    def test_image_endpoint_returns_404_for_unknown_frame(self) -> None:
        with patch.object(main, "get_image_artifacts", side_effect=main.CMIFrameNotFoundError("missing")):
            with self.assertRaises(HTTPException) as ctx:
                main.cmi_ch13_image(satellite="goes-east", frame_id="missing")

        self.assertEqual(ctx.exception.status_code, 404)

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
