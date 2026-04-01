from __future__ import annotations

import logging
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.cmi import (
    FRAMES_CACHE_TTL_SECONDS,
    MAX_ZOOM,
    POLL_INTERVAL_HINT_SECONDS,
    CMIFetchError,
    CMIFrameNotFoundError,
    CMIInvalidTileError,
    fetch_recent_cmi_frames,
    render_tile,
)
from app.glm import (
    GLMFetchError,
    RECENT_CACHE_TTL_SECONDS,
    get_latest_frame,
    get_latest_points,
    start_background_refresh,
    stop_background_refresh,
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
    frame_id: str
    satellite: str
    start_time: str
    end_time: str
    tile_url_template: str


class CMIFramesResponse(BaseModel):
    satellite: str
    count: int
    poll_interval_seconds: int
    frames: list[CMIFrameModel]


@app.on_event("startup")
async def startup() -> None:
    install_asyncio_exception_handler()
    start_background_refresh()


@app.on_event("shutdown")
async def shutdown() -> None:
    stop_background_refresh()


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


@app.get("/imagery/cmi/ch13/frames", response_model=CMIFramesResponse)
def cmi_ch13_frames(
    request: Request,
    response: Response,
    satellite: Literal["goes-east", "goes-west"] = Query(default="goes-east"),
    limit: int = Query(default=12, ge=1, le=120),
    poll_hint: int = Query(default=POLL_INTERVAL_HINT_SECONDS, ge=1, le=60),
) -> CMIFramesResponse:
    try:
        frames = fetch_recent_cmi_frames(satellite=satellite, limit=limit)
    except CMIFetchError as exc:
        logger.exception(
            "CMI frames request failed: satellite=%s limit=%s poll_hint=%s",
            satellite,
            limit,
            poll_hint,
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    base_url = str(request.base_url).rstrip("/")
    tile_template = (
        f"{base_url}/imagery/cmi/ch13/tiles/{satellite}/"
        "{frame_id}/{z}/{x}/{y}.png"
    )
    frame_models = [
        CMIFrameModel(
            frame_id=frame.frame_id,
            satellite=frame.satellite,
            start_time=frame.start_time,
            end_time=frame.end_time,
            tile_url_template=tile_template.replace("{frame_id}", frame.frame_id),
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


@app.get("/imagery/cmi/ch13/tiles/{satellite}/{frame_id}/{z}/{x}/{y}.png")
def cmi_ch13_tile(
    satellite: Literal["goes-east", "goes-west"],
    frame_id: str,
    z: int,
    x: int,
    y: int,
) -> FileResponse:
    if z > MAX_ZOOM:
        raise HTTPException(status_code=422, detail=f"Unsupported zoom level {z}; max is {MAX_ZOOM}.")

    try:
        tile_path = render_tile(frame_id=frame_id, satellite=satellite, z=z, x=x, y=y)
    except CMIFrameNotFoundError as exc:
        logger.warning(
            "CMI tile request referenced unknown frame: satellite=%s frame_id=%s z=%s x=%s y=%s",
            satellite,
            frame_id,
            z,
            x,
            y,
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CMIInvalidTileError as exc:
        logger.warning(
            "CMI tile request used invalid tile coordinates: satellite=%s frame_id=%s z=%s x=%s y=%s",
            satellite,
            frame_id,
            z,
            x,
            y,
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CMIFetchError as exc:
        logger.exception(
            "CMI tile request failed: satellite=%s frame_id=%s z=%s x=%s y=%s",
            satellite,
            frame_id,
            z,
            x,
            y,
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return FileResponse(
        path=tile_path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
