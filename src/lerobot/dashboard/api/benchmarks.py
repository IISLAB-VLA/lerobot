"""Benchmark REST endpoints.

Task #16 Phase 1. Mirrors the calibration REST layer — REST authoritative,
WS is a read-only event stream in :mod:`lerobot.dashboard.ws.router`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from lerobot.dashboard.services.benchmark import (
    BenchmarkController,
    BenchmarkInfo,
    BenchmarkRunList,
    BenchmarkRunSummary,
    BenchmarkStartRequest,
    RunNotFoundError,
    UnknownEnvError,
    list_benchmarks,
)

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])


class BenchmarkListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    benchmarks: list[BenchmarkInfo]


def get_benchmark_controller(request: Request) -> BenchmarkController:
    controller = getattr(request.app.state.dashboard, "benchmark", None)
    if controller is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="benchmark controller is not initialized",
        )
    return controller


@router.get("", response_model=BenchmarkListResponse)
async def list_benchmarks_endpoint() -> BenchmarkListResponse:
    return BenchmarkListResponse(benchmarks=list_benchmarks())


@router.post(
    "/runs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=BenchmarkRunSummary,
)
async def start_run(
    body: BenchmarkStartRequest,
    controller: BenchmarkController = Depends(get_benchmark_controller),
) -> BenchmarkRunSummary:
    try:
        return await controller.start_run(body)
    except UnknownEnvError as exc:
        raise HTTPException(status_code=422, detail={"detail": str(exc)}) from exc


@router.get("/runs", response_model=BenchmarkRunList)
async def list_runs(
    controller: BenchmarkController = Depends(get_benchmark_controller),
) -> BenchmarkRunList:
    return BenchmarkRunList(runs=await controller.list_runs())


@router.get("/runs/{run_id}", response_model=BenchmarkRunSummary)
async def get_run(
    run_id: str,
    controller: BenchmarkController = Depends(get_benchmark_controller),
) -> BenchmarkRunSummary:
    try:
        return await controller.get_run(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"detail": str(exc)}) from exc


@router.post(
    "/runs/{run_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=BenchmarkRunSummary,
)
async def cancel_run(
    run_id: str,
    controller: BenchmarkController = Depends(get_benchmark_controller),
) -> BenchmarkRunSummary:
    try:
        return await controller.cancel_run(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"detail": str(exc)}) from exc
