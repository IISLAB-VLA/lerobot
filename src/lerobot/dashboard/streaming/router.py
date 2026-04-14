"""FastAPI router for the ``/api/streams`` surface (task #9).

Contract consumed by the frontend (task #10). Paths are stable and
documented in the OpenAPI schema FastAPI generates from the Pydantic
models below.

* ``POST /api/streams/offer`` — create a session from a browser SDP offer.
* ``POST /api/streams/{sid}/ice`` — forward trickled ICE candidates.
* ``POST /api/streams/{sid}/stop`` — tear down a session.
* ``PATCH /api/streams/{sid}/quality`` — reconfigure encoder on the fly.
* ``GET /api/streams/{sid}/stats`` — getStats() snapshot for the UI overlay.
* ``GET /api/streams/stats``       — batch getStats() for many sessions.
* ``POST /api/streams/{sid}/keyframe`` — force an immediate keyframe.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from lerobot.dashboard.streaming.session import QualitySettings, SignalingManager


class OfferRequest(BaseModel):
    robot_id: str = Field(..., description="Registry robot identifier")
    camera_id: str = Field(..., description="Registry camera identifier under the robot")
    sdp: str = Field(..., description="Browser-generated SDP offer payload")
    type: str = Field("offer", description="SDP type, always 'offer' from the browser")


class OfferResponse(BaseModel):
    session_id: str
    sdp: str
    type: str
    applied_codecs: list[str]


class IceRequest(BaseModel):
    """Browser trickle payload. ``candidate=""`` signals end-of-candidates."""

    candidate: str = ""
    sdpMid: str | None = None
    sdpMLineIndex: int | None = None


class QualityRequest(BaseModel):
    width: int | None = Field(None, gt=0, le=3840)
    height: int | None = Field(None, gt=0, le=2160)
    fps: int | None = Field(None, gt=0, le=120)
    bitrate_kbps: int | None = Field(None, gt=0, le=50_000)


class QualityResponse(BaseModel):
    width: int | None
    height: int | None
    fps: int | None
    bitrate_kbps: int | None


class StatsResponse(BaseModel):
    session_id: str
    entries: list[dict[str, Any]]


class StatsManyResponse(BaseModel):
    """Batch response for the multi-session stats overlay.

    Sessions that are missing at query time (either never existed or were
    just closed) are omitted from ``sessions`` — the UI should treat their
    absence as "unsubscribe this tile", matching the soft-delete behaviour
    of the frontend's `useStreamStats` hook.
    """

    sessions: list[StatsResponse]


class KeyframeResponse(BaseModel):
    session_id: str
    senders_signalled: int


def _manager(request: Request) -> SignalingManager:
    """Pull the streaming manager from app state, 503 if unwired."""
    manager = getattr(request.app.state, "streaming", None)
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="streaming subsystem not initialised",
        )
    return manager


def build_streams_router() -> APIRouter:
    """Return the ``/streams`` subrouter. Mount under the main ``/api`` router."""
    router = APIRouter(prefix="/streams", tags=["streams"])

    @router.post("/offer", response_model=OfferResponse)
    async def post_offer(body: OfferRequest, request: Request) -> OfferResponse:
        manager = _manager(request)
        try:
            session, answer = await manager.create_session(
                robot_id=body.robot_id,
                camera_id=body.camera_id,
                offer_sdp=body.sdp,
                offer_type=body.type,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"camera not found: {exc}") from exc
        return OfferResponse(
            session_id=session.session_id,
            sdp=answer.sdp,
            type=answer.type,
            applied_codecs=session.applied_codecs,
        )

    @router.post("/{session_id}/ice", status_code=status.HTTP_204_NO_CONTENT)
    async def post_ice(session_id: str, body: IceRequest, request: Request) -> None:
        manager = _manager(request)
        try:
            await manager.add_ice_candidate(session_id, body.model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc

    @router.post("/{session_id}/stop", status_code=status.HTTP_204_NO_CONTENT)
    async def post_stop(session_id: str, request: Request) -> None:
        manager = _manager(request)
        await manager.close_session(session_id)

    @router.patch("/{session_id}/quality", response_model=QualityResponse)
    async def patch_quality(
        session_id: str, body: QualityRequest, request: Request
    ) -> QualityResponse:
        manager = _manager(request)
        try:
            merged = await manager.update_quality(
                session_id,
                QualitySettings(**body.model_dump()),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        return QualityResponse(**merged.__dict__)

    @router.get("/stats", response_model=StatsManyResponse)
    async def get_stats_batch(
        request: Request,
        session_ids: str | None = Query(
            default=None,
            description=(
                "Optional comma-separated allowlist. When omitted, every "
                "active session is returned."
            ),
        ),
    ) -> StatsManyResponse:
        manager = _manager(request)
        ids: list[str] | None
        if session_ids is None or session_ids == "":
            ids = None
        else:
            ids = [sid for sid in (s.strip() for s in session_ids.split(",")) if sid]
        results = await manager.stats_many(ids)
        return StatsManyResponse(
            sessions=[StatsResponse(session_id=sid, entries=entries) for sid, entries in results]
        )

    @router.get("/{session_id}/stats", response_model=StatsResponse)
    async def get_stats(session_id: str, request: Request) -> StatsResponse:
        manager = _manager(request)
        try:
            entries = await manager.stats(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        return StatsResponse(session_id=session_id, entries=entries)

    @router.post("/{session_id}/keyframe", response_model=KeyframeResponse)
    async def post_keyframe(session_id: str, request: Request) -> KeyframeResponse:
        manager = _manager(request)
        try:
            signalled = await manager.request_keyframe(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        return KeyframeResponse(session_id=session_id, senders_signalled=signalled)

    return router
