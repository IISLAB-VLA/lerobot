"""Locally cached policy discovery (task #14 phase 1b).

Thin wrapper over :meth:`InferenceService.list_policies` so the frontend
can populate the inference dropdown without touching the service object
directly. ``GET /api/policies?refresh=true`` forces a re-scan of the
HuggingFace cache on demand.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from lerobot.dashboard.api._deps import (
    get_inference,
    map_inference_error,
)
from lerobot.dashboard.services.inference import (
    InferenceError,
    InferenceService,
    PolicyDescriptor,
)

router = APIRouter(prefix="/policies", tags=["policies"])


@router.get("", response_model=list[PolicyDescriptor])
async def list_policies(
    refresh: bool = Query(default=False),
    inference: InferenceService = Depends(get_inference),
) -> list[PolicyDescriptor]:
    return await inference.list_policies(refresh=refresh)


@router.get("/{repo_id:path}", response_model=PolicyDescriptor)
async def get_policy(
    repo_id: str,
    inference: InferenceService = Depends(get_inference),
) -> PolicyDescriptor:
    try:
        return await inference.get_policy(repo_id)
    except InferenceError as exc:
        raise map_inference_error(exc) from exc
