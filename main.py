from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel

from app.glm import GLMFetchError, RECENT_CACHE_TTL_SECONDS, fetch_recent_lightning


app = FastAPI(title="Lightning Rod", version="0.1.0")


class HealthResponse(BaseModel):
    status: str


class LightningFeature(BaseModel):
    id: str
    latitude: float
    longitude: float
    time: str
    energy: float | None = None


class LightningRecentResponse(BaseModel):
    satellite: str
    count: int
    features: list[LightningFeature]


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/lightning/recent", response_model=LightningRecentResponse)
def lightning_recent(
    response: Response,
    satellite: Literal["goes-east", "goes-west"] = Query(default="goes-east"),
    limit: int = Query(default=100, ge=1, le=1000),
) -> LightningRecentResponse:
    """Return recent GLM flashes from NOAA GOES.

    TODO later: add query params for bbox filtering and custom time windows.
    """
    try:
        flashes = fetch_recent_lightning(satellite=satellite, limit=limit)
    except GLMFetchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    response.headers["Cache-Control"] = f"public, max-age={RECENT_CACHE_TTL_SECONDS}"
    features = [LightningFeature(**flash.__dict__) for flash in flashes]
    return LightningRecentResponse(
        satellite=satellite,
        count=len(features),
        features=features,
    )
