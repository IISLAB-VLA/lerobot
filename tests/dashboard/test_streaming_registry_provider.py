"""Tests for :class:`RegistryFrameSourceProvider` + :class:`CameraManagerFrameSource`.

These exercise the bridge between the streaming stack and the camera
manager without requiring real hardware: the in-memory manager yields
zero frames at the declared fps, which is plenty to verify adapter
semantics (subscribe fan-in, graceful end, idempotent open).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, TypeVar

import numpy as np
import pytest

from lerobot.dashboard.services.camera_manager import InMemoryCameraManager
from lerobot.dashboard.services.registry import Registry
from lerobot.dashboard.services.registry_models import CameraEntry, CameraSource
from lerobot.dashboard.streaming.source import (
    CameraManagerFrameSource,
    FrameSourceError,
    RegistryFrameSourceProvider,
)

T = TypeVar("T")


def _run(coro: Awaitable[T]) -> T:
    return asyncio.run(coro)


async def _prepare_registry(tmp_path: Path) -> tuple[Registry, CameraEntry]:
    registry = Registry(tmp_path / "registry.json")
    entry = CameraEntry(
        name="cam0",
        backend="opencv",
        source=CameraSource(index=0),
        width=32,
        height=24,
        fps=60,
    )
    await registry.create_camera(entry)
    return registry, entry


def test_provider_opens_camera_and_streams_frames(tmp_path: Path):
    async def scenario() -> np.ndarray:
        registry, entry = await _prepare_registry(tmp_path)
        manager = InMemoryCameraManager()
        provider = RegistryFrameSourceProvider(manager=manager, registry=registry)
        source = await provider.open(robot_id="unused", camera_id=str(entry.id))
        frame = await source.read()
        await source.close()
        return frame

    frame = _run(scenario())
    assert frame.shape == (24, 32, 3)
    assert frame.dtype == np.uint8


def test_provider_rejects_unknown_camera(tmp_path: Path):
    async def scenario() -> None:
        registry = Registry(tmp_path / "registry.json")
        manager = InMemoryCameraManager()
        provider = RegistryFrameSourceProvider(manager=manager, registry=registry)
        with pytest.raises(FrameSourceError):
            await provider.open(robot_id="r1", camera_id="00000000-0000-0000-0000-000000000000")

    _run(scenario())


def test_provider_rejects_non_uuid_camera_id(tmp_path: Path):
    async def scenario() -> None:
        registry = Registry(tmp_path / "registry.json")
        manager = InMemoryCameraManager()
        provider = RegistryFrameSourceProvider(manager=manager, registry=registry)
        with pytest.raises(FrameSourceError):
            await provider.open(robot_id="r1", camera_id="not-a-uuid")

    _run(scenario())


def test_frame_source_raises_frame_source_error_when_stream_ends():
    """Graceful ``StopAsyncIteration`` surfaces as a ``FrameSourceError``."""

    async def empty_stream():
        if False:
            yield  # pragma: no cover - never yields, ends immediately

    class StaticManager:
        def subscribe(self, camera_id):
            return empty_stream()

    async def scenario() -> None:
        source = CameraManagerFrameSource(
            manager=StaticManager(),
            camera_id=__import__("uuid").uuid4(),
            width=16,
            height=12,
            fps=30,
        )
        with pytest.raises(FrameSourceError):
            await source.read()
        await source.close()

    _run(scenario())


def test_provider_open_is_idempotent_for_repeat_sessions(tmp_path: Path):
    """Two sessions against the same camera both get live streams."""

    async def scenario() -> tuple[np.ndarray, np.ndarray]:
        registry, entry = await _prepare_registry(tmp_path)
        manager = InMemoryCameraManager()
        provider = RegistryFrameSourceProvider(manager=manager, registry=registry)
        src1 = await provider.open(robot_id="r", camera_id=str(entry.id))
        src2 = await provider.open(robot_id="r", camera_id=str(entry.id))
        f1 = await src1.read()
        f2 = await src2.read()
        await src1.close()
        await src2.close()
        return f1, f2

    f1, f2 = _run(scenario())
    assert f1.shape == f2.shape == (24, 32, 3)
