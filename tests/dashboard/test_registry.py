"""Unit tests for :mod:`lerobot.dashboard.services.registry`."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from lerobot.dashboard.services.registry import (  # noqa: E402
    Registry,
    RegistryNotFoundError,
    RegistryValidationError,
    registry_path_for,
)
from lerobot.dashboard.services.registry_models import (  # noqa: E402
    CameraEntry,
    CameraSource,
    RobotEntry,
    SerialConnection,
    TeleopEntry,
)


def _serial() -> SerialConnection:
    return SerialConnection(port="/dev/ttyUSB0", baudrate=1_000_000)


def _camera(name: str = "front") -> CameraEntry:
    return CameraEntry(
        name=name,
        backend="opencv",
        source=CameraSource(index=0),
        width=640,
        height=480,
        fps=30,
    )


def _teleop(name: str = "kb") -> TeleopEntry:
    return TeleopEntry(name=name, kind="keyboard", config={"layout": "qwerty"})


def _robot(cameras: list | None = None, teleop_id=None, name: str = "so101") -> RobotEntry:
    return RobotEntry(
        name=name,
        robot_type="so101_follower",
        connection=_serial(),
        cameras=cameras or [],
        teleop=teleop_id,
    )


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(registry_path_for(tmp_path))


async def test_initial_load_creates_empty_document(tmp_path: Path) -> None:
    path = registry_path_for(tmp_path)
    registry = Registry(path)
    doc = await registry.load()
    assert doc.version == 1
    assert doc.robots == []
    assert doc.cameras == []
    assert doc.teleops == []
    # The on-disk file exists after load.
    assert path.is_file()
    data = json.loads(path.read_text())
    assert data["version"] == 1


async def test_robot_crud_round_trip(registry: Registry) -> None:
    camera = await registry.create_camera(_camera())
    teleop = await registry.create_teleop(_teleop())
    robot = await registry.create_robot(_robot(cameras=[camera.id], teleop_id=teleop.id))

    fetched = await registry.get_robot(robot.id)
    assert fetched.id == robot.id
    assert fetched.cameras == [camera.id]
    assert fetched.teleop == teleop.id

    updated = await registry.update_robot(robot.id, {"name": "new-name"})
    assert updated.name == "new-name"

    robots = await registry.list_robots()
    assert [r.id for r in robots] == [robot.id]

    await registry.delete_robot(robot.id)
    with pytest.raises(RegistryNotFoundError):
        await registry.get_robot(robot.id)


async def test_create_robot_rejects_unknown_camera(registry: Registry) -> None:
    with pytest.raises(RegistryValidationError):
        await registry.create_robot(_robot(cameras=[uuid4()]))


async def test_create_robot_rejects_unknown_teleop(registry: Registry) -> None:
    with pytest.raises(RegistryValidationError):
        await registry.create_robot(_robot(teleop_id=uuid4()))


async def test_delete_camera_refuses_while_referenced(registry: Registry) -> None:
    camera = await registry.create_camera(_camera())
    await registry.create_robot(_robot(cameras=[camera.id]))
    with pytest.raises(RegistryValidationError):
        await registry.delete_camera(camera.id)


async def test_delete_teleop_refuses_while_referenced(registry: Registry) -> None:
    teleop = await registry.create_teleop(_teleop())
    await registry.create_robot(_robot(teleop_id=teleop.id))
    with pytest.raises(RegistryValidationError):
        await registry.delete_teleop(teleop.id)


async def test_update_cannot_change_id(registry: Registry) -> None:
    teleop = await registry.create_teleop(_teleop())
    with pytest.raises(RegistryValidationError):
        await registry.update_teleop(teleop.id, {"id": str(uuid4())})


async def test_patch_validates_nested_fields(registry: Registry) -> None:
    camera = await registry.create_camera(_camera())
    with pytest.raises(RegistryValidationError):
        await registry.update_camera(camera.id, {"fps": -1})


async def test_persistence_across_instances(tmp_path: Path) -> None:
    path = registry_path_for(tmp_path)
    first = Registry(path)
    camera = await first.create_camera(_camera())
    teleop = await first.create_teleop(_teleop())
    robot = await first.create_robot(_robot(cameras=[camera.id], teleop_id=teleop.id))

    second = Registry(path)
    await second.load()
    robots = await second.list_robots()
    assert [r.id for r in robots] == [robot.id]
    assert robots[0].cameras == [camera.id]


async def test_corrupt_registry_is_quarantined(tmp_path: Path) -> None:
    path = registry_path_for(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json")
    registry = Registry(path)
    doc = await registry.load()
    assert doc.robots == []
    broken = path.with_suffix(path.suffix + ".broken")
    assert broken.is_file()


async def test_atomic_write_leaves_no_tmp(tmp_path: Path) -> None:
    path = registry_path_for(tmp_path)
    registry = Registry(path)
    await registry.create_camera(_camera())
    tmp_sibling = path.with_suffix(path.suffix + ".tmp")
    assert not tmp_sibling.exists()
