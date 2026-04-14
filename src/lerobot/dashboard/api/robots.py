"""CRUD + lifecycle endpoints for :class:`RobotEntry`."""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Body, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from lerobot.dashboard.api._deps import (
    get_assets,
    get_registry,
    get_robot_manager,
    map_registry_error,
)
from lerobot.dashboard.services.assets import AssetManager
from lerobot.dashboard.services.registry import Registry, RegistryError
from lerobot.dashboard.services.registry_models import (
    Connection,
    NetworkConnection,
    RobotEntry,
    RobotStatus,
)
from lerobot.dashboard.services.robot_manager import RobotManagerProtocol

# Well-known port → NetworkProtocol mapping. Only upgrades ``custom`` in the
# payload; explicit user choices are always preserved. Kept in sync with
# :data:`lerobot.dashboard.api.devices._PROTOCOL_DEFAULT_PORT` by convention.
_PROTOCOL_BY_DEFAULT_PORT: dict[int, str] = {30004: "rtde", 10001: "fci"}


def _maybe_upgrade_network_protocol(conn: Connection) -> Connection:
    """Promote ``custom`` to a well-known NetworkProtocol based on the port.

    ``connection.protocol == "custom"`` is the opt-in: when the user gives
    us a host + port but hasn't picked a specific protocol, we try to guess
    from well-known ports (30004 → rtde, 10001 → fci). Explicit user
    choices (rtde, fci, xmlrpc, websocket) are never changed — we must not
    downgrade ``rtde`` on port 5555 back to something else.
    """
    if not isinstance(conn, NetworkConnection):
        return conn
    if conn.protocol != "custom":
        return conn
    upgraded = _PROTOCOL_BY_DEFAULT_PORT.get(conn.port)
    if upgraded is None:
        return conn
    return conn.model_copy(update={"protocol": upgraded})


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/robots", tags=["robots"])


class RobotCreateRequest(BaseModel):
    """Shape accepted by ``POST /api/robots``.

    Intentionally omits ``id`` so that clients cannot pin UUIDs; the
    registry generates them. Everything else mirrors :class:`RobotEntry`.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    robot_type: str
    connection: Connection
    cameras: list[UUID] = Field(default_factory=list)
    teleop: UUID | None = None
    image_ref: str | None = None


@router.get("", response_model=list[RobotEntry])
async def list_robots(registry: Registry = Depends(get_registry)) -> list[RobotEntry]:
    return await registry.list_robots()


@router.post("", response_model=RobotEntry, status_code=status.HTTP_201_CREATED)
async def create_robot(
    payload: RobotCreateRequest,
    registry: Registry = Depends(get_registry),
    assets: AssetManager = Depends(get_assets),
) -> RobotEntry:
    updates: dict[str, object] = {
        "connection": _maybe_upgrade_network_protocol(payload.connection),
    }
    if payload.image_ref is None:
        # AssetManager.resolve_url may block on download; keep the event loop
        # free. It falls back to the generic icon on any error, so the final
        # value is always a valid /resources URL.
        updates["image_ref"] = await asyncio.to_thread(assets.resolve_url, payload.robot_type)
    resolved = payload.model_copy(update=updates)
    entry = RobotEntry(**resolved.model_dump())
    try:
        return await registry.create_robot(entry)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc


@router.get("/{robot_id}", response_model=RobotEntry)
async def get_robot(robot_id: UUID, registry: Registry = Depends(get_registry)) -> RobotEntry:
    try:
        return await registry.get_robot(robot_id)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc


@router.patch("/{robot_id}", response_model=RobotEntry)
async def update_robot(
    robot_id: UUID,
    patch: dict = Body(...),
    registry: Registry = Depends(get_registry),
) -> RobotEntry:
    # Apply the same network-protocol auto-upgrade to patches that replace
    # the connection payload, so PATCH doesn't silently bypass the rule.
    raw_conn = patch.get("connection")
    if isinstance(raw_conn, dict) and raw_conn.get("kind") == "network":
        from pydantic import TypeAdapter

        try:
            typed = TypeAdapter(NetworkConnection).validate_python(raw_conn)
        except Exception:
            # Defer validation error reporting to the registry's model_validate round-trip.
            typed = None
        if typed is not None:
            resolved = _maybe_upgrade_network_protocol(typed)
            patch = {**patch, "connection": resolved.model_dump()}
    try:
        return await registry.update_robot(robot_id, patch)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc


@router.delete("/{robot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_robot(
    robot_id: UUID,
    registry: Registry = Depends(get_registry),
    manager: RobotManagerProtocol = Depends(get_robot_manager),
) -> None:
    # Best-effort disconnect so we don't orphan live handles.
    if await manager.is_connected(robot_id):
        await manager.disconnect(robot_id)
    try:
        await registry.delete_robot(robot_id)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc


# ---------------------------------------------------------------------------
# Lifecycle — connect / disconnect / status
# ---------------------------------------------------------------------------


@router.post("/{robot_id}/connect", response_model=RobotStatus)
async def connect_robot(
    robot_id: UUID,
    registry: Registry = Depends(get_registry),
    manager: RobotManagerProtocol = Depends(get_robot_manager),
) -> RobotStatus:
    try:
        entry = await registry.get_robot(robot_id)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc
    all_cameras = await registry.list_cameras()
    cam_map = {c.id: c for c in all_cameras}
    cameras = [cam_map[cid] for cid in entry.cameras if cid in cam_map]
    return await manager.connect(entry, cameras)


@router.post("/{robot_id}/disconnect", response_model=RobotStatus)
async def disconnect_robot(
    robot_id: UUID,
    registry: Registry = Depends(get_registry),
    manager: RobotManagerProtocol = Depends(get_robot_manager),
) -> RobotStatus:
    try:
        await registry.get_robot(robot_id)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc
    return await manager.disconnect(robot_id)


@router.get("/{robot_id}/status", response_model=RobotStatus)
async def get_robot_status(
    robot_id: UUID,
    registry: Registry = Depends(get_registry),
    manager: RobotManagerProtocol = Depends(get_robot_manager),
) -> RobotStatus:
    try:
        await registry.get_robot(robot_id)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc
    return await manager.get_status(robot_id)
