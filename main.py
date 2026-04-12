from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from cmi.service import (
    FRAMES_CACHE_TTL_SECONDS,
    POLL_INTERVAL_HINT_SECONDS,
    CMIFetchError,
    CMIFrameNotFoundError,
    get_frames_in_range,
    get_image_artifacts,
    start_background_refresh as start_cmi_background_refresh,
    stop_background_refresh as stop_cmi_background_refresh,
)
from glm.service import (
    GLMFetchError,
    RECENT_CACHE_TTL_SECONDS,
    get_latest_frame,
    get_latest_points,
    start_background_refresh as start_glm_background_refresh,
    stop_background_refresh as stop_glm_background_refresh,
)
from app.runtime_diagnostics import (
    install_asyncio_exception_handler,
    install_runtime_diagnostics,
)


logger = logging.getLogger(__name__)

app = FastAPI(title="Lightning Rod", version="0.1.0")
install_runtime_diagnostics()


class HealthResponse(BaseModel):
    status: str


class LightningFeature(BaseModel):
    id: str
    latitude: float
    longitude: float
    time: str
    energy: float | None = None


class LightningFrameResponse(BaseModel):
    frame_id: str
    satellite: str
    start_time: str
    end_time: str
    flash_count: int
    updated_at: str


class LightningPointsResponse(BaseModel):
    frame_id: str
    satellite: str
    start_time: str
    end_time: str
    updated_at: str
    count: int
    features: list[LightningFeature]


class CMIFrameModel(BaseModel):
    frame_id: str = Field(description="Stable NOAA-derived frame identifier.")
    satellite: str = Field(description="Satellite alias requested by the client.")
    start_time: str = Field(description="Frame start timestamp in UTC ISO-8601 format.")
    end_time: str = Field(description="Frame end timestamp in UTC ISO-8601 format.")
    image_url: str = Field(description="PNG URL for this rendered CMI frame.")
    coordinates: list[tuple[float, float]] = Field(
        min_length=4,
        max_length=4,
        description="ImageSource corners ordered top-left, top-right, bottom-right, bottom-left.",
    )


class CMIFramesResponse(BaseModel):
    satellite: str = Field(description="Satellite alias requested by the client.")
    count: int = Field(description="Number of frames returned for the requested time window.")
    poll_interval_seconds: int = Field(
        description="Suggested cadence for checking whether newer frames may now be available."
    )
    frames: list[CMIFrameModel] = Field(
        description="Frames sorted oldest-to-newest within the requested time window."
    )


@app.on_event("startup")
async def startup() -> None:
    install_asyncio_exception_handler()
    start_glm_background_refresh()
    start_cmi_background_refresh()


@app.on_event("shutdown")
async def shutdown() -> None:
    stop_glm_background_refresh()
    stop_cmi_background_refresh()


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/lightning/latest-frame", response_model=LightningFrameResponse)
def lightning_latest_frame(
    satellite: Literal["goes-east", "goes-west"] = Query(default="goes-east"),
) -> LightningFrameResponse:
    try:
        frame, updated_at = get_latest_frame(satellite=satellite)
        _, features, _ = get_latest_points(satellite=satellite)
    except GLMFetchError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return LightningFrameResponse(
        frame_id=frame.frame_id,
        satellite=frame.satellite,
        start_time=frame.start_time,
        end_time=frame.end_time,
        flash_count=len(features),
        updated_at=updated_at,
    )


@app.get("/lightning/latest-points", response_model=LightningPointsResponse)
def lightning_latest_points(
    response: Response,
    satellite: Literal["goes-east", "goes-west"] = Query(default="goes-east"),
    limit: int = Query(default=1000, ge=1, le=5000),
) -> LightningPointsResponse:
    try:
        frame, points, updated_at = get_latest_points(satellite=satellite, limit=limit)
    except GLMFetchError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    response.headers["Cache-Control"] = f"public, max-age={RECENT_CACHE_TTL_SECONDS}"
    return LightningPointsResponse(
        frame_id=frame.frame_id,
        satellite=frame.satellite,
        start_time=frame.start_time,
        end_time=frame.end_time,
        updated_at=updated_at,
        count=len(points),
        features=[LightningFeature(**flash.__dict__) for flash in points],
    )


@app.get(
    "/imagery/cmi/ch13/frames",
    response_model=CMIFramesResponse,
    summary="Get CMI Frames For A Time Window",
    description=(
        "Returns rendered GOES ABI CMI Channel 13 frames for the requested UTC time window. "
        "This endpoint is time-window-only: clients must provide both `start` and `end`. "
        "Responses are ordered oldest-to-newest so clients can animate playback directly from the returned list."
    ),
    responses={
        200: {
            "description": "CMI frame metadata for the requested time window, sorted oldest-to-newest."
        },
        422: {
            "description": "Validation error. Common causes: missing `start`/`end`, invalid timestamps, or `start >= end`."
        },
    },
)
def cmi_ch13_frames(
    request: Request,
    response: Response,
    satellite: Literal["goes-east", "goes-west"] = Query(
        default="goes-east",
        description="GOES satellite alias to query.",
    ),
    start: datetime = Query(
        description="Inclusive UTC start timestamp for the requested playback window.",
        example="2026-04-08T00:00:00Z",
    ),
    end: datetime = Query(
        description="Exclusive UTC end timestamp for the requested playback window.",
        example="2026-04-08T01:00:00Z",
    ),
    limit: int = Query(
        default=1000,
        ge=1,
        le=1000,
        description="Maximum number of frames to return within the requested time window.",
    ),
    poll_hint: int = Query(
        default=POLL_INTERVAL_HINT_SECONDS,
        ge=1,
        le=7200,
        description="Suggested client poll cadence in seconds for checking newer data availability.",
    ),
) -> CMIFramesResponse:
    if start >= end:
        raise HTTPException(status_code=422, detail="start must be before end")

    start_iso = start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    end_iso = end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        frames = get_frames_in_range(
            satellite=satellite,
            start=start_iso,
            end=end_iso,
            limit=limit,
        )
    except CMIFetchError as exc:
        logger.exception(
            "CMI frames request failed: satellite=%s limit=%s start=%s end=%s poll_hint=%s",
            satellite,
            limit,
            start_iso,
            end_iso,
            poll_hint,
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    frames = sorted(frames, key=lambda frame: (frame.start_time, frame.end_time, frame.frame_id))

    base_url = str(request.base_url).rstrip("/")
    frame_models = [
        CMIFrameModel(
            frame_id=frame.frame_id,
            satellite=frame.satellite,
            start_time=frame.start_time,
            end_time=frame.end_time,
            image_url=f"{base_url}/imagery/cmi/ch13/images/{frame.satellite}/{frame.frame_id}.png",
            coordinates=get_image_artifacts(frame_id=frame.frame_id, satellite=frame.satellite)[1],
        )
        for frame in frames
    ]
    response.headers["Cache-Control"] = f"public, max-age={FRAMES_CACHE_TTL_SECONDS}"
    return CMIFramesResponse(
        satellite=satellite,
        count=len(frame_models),
        poll_interval_seconds=poll_hint,
        frames=frame_models,
    )


@app.get(
    "/imagery/cmi/ch13/images/{satellite}/{frame_id}.png",
    response_class=FileResponse,
    responses={
        200: {
            "description": "PNG image for the requested CMI frame.",
            "content": {"image/png": {}},
        }
    },
)
def cmi_ch13_image(
    satellite: Literal["goes-east", "goes-west"],
    frame_id: str,
) -> FileResponse:
    try:
        image_path, _ = get_image_artifacts(frame_id=frame_id, satellite=satellite)
    except CMIFrameNotFoundError as exc:
        logger.warning(
            "CMI image request referenced unknown frame: satellite=%s frame_id=%s",
            satellite,
            frame_id,
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CMIFetchError as exc:
        logger.exception(
            "CMI image request failed: satellite=%s frame_id=%s",
            satellite,
            frame_id,
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return FileResponse(
        path=image_path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
