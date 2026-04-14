"""Pydantic v2 models for the dashboard registry (Task #5 draft).

Shared with robotics-integrator (Task #6) and frontend-architect so that
the Add-Robot modal, device discovery validators, and lerobot adapter
layer converge on a single schema. The registry file on disk is JSON
serialized directly from these models.

Design notes
------------
* ``robot_type`` is an open-ended string (free-form) plus a Literal of
  well-known identifiers. lerobot robot kinds are added here as they
  come online; unknown ids are still accepted so frontend-led exploration
  of future hardware doesn't require a backend release.
* ``connection`` is a tagged union discriminated by the ``kind`` field
  (``"serial"`` / ``"network"``). Pydantic v2's ``Discriminator`` produces
  nice error messages and keeps the frontend JSON compact.
* ``model_opts`` and ``extra`` on the connection models are free-form
  dicts so the lerobot adapter (Task #6) can forward kwargs into the
  underlying Robot constructor without requiring a new backend release
  for every new knob.
* The live-runtime fields (online flag, last error, timestamps) live on
  a separate ``*Status`` model. The registry on disk never contains
  runtime state; only the user's declared inventory.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag

# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


class SerialConnection(BaseModel):
    """USB-serial hardware link (e.g. SO-101 follower over Feetech)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["serial"] = "serial"
    port: str = Field(
        ...,
        description=(
            "Stable device path (e.g. /dev/serial/by-id/usb-..., or COM7 on Windows). "
            "The frontend picks from the Task #4 device-discovery list."
        ),
    )
    baudrate: int | None = Field(
        default=None,
        ge=1200,
        le=12_000_000,
        description="Serial baud. None = adapter picks the motor-bus default.",
    )
    model_opts: dict[str, object] = Field(
        default_factory=dict,
        description="Opaque kwargs forwarded to the lerobot Robot constructor.",
    )


NetworkProtocol = Literal["rtde", "fci", "xmlrpc", "websocket", "custom"]


class NetworkConnection(BaseModel):
    """TCP/UDP hardware link (e.g. UR7e RTDE, arbitrary network robots)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["network"] = "network"
    protocol: NetworkProtocol = Field(
        ...,
        description="Network protocol flavor. 'rtde' is UR, 'fci' is Franka, etc.",
    )
    host: str = Field(..., description="Hostname or IP of the robot controller.")
    port: int = Field(..., ge=1, le=65_535, description="TCP/UDP port.")
    auth: dict[str, str] | None = Field(
        default=None,
        description="Optional auth material (username/password/token). Never logged.",
    )
    extra: dict[str, object] = Field(
        default_factory=dict,
        description="Protocol-specific kwargs (e.g. rtde frequency). Adapter forwards them.",
    )


def _connection_discriminator(v: object) -> str | None:
    if isinstance(v, dict):
        return v.get("kind")  # type: ignore[return-value]
    return getattr(v, "kind", None)


Connection = Annotated[
    Annotated[SerialConnection, Tag("serial")] | Annotated[NetworkConnection, Tag("network")],
    Discriminator(_connection_discriminator),
]


# ---------------------------------------------------------------------------
# Cameras
# ---------------------------------------------------------------------------


CameraBackend = Literal["opencv", "realsense", "network"]


class CameraSource(BaseModel):
    """Where the camera frames come from.

    Exactly one of ``path`` / ``index`` / ``url`` should be populated
    (enforced at the service layer, not here — keeping the model
    permissive simplifies JSON round-trips during edits).
    """

    model_config = ConfigDict(extra="forbid")

    path: str | None = Field(default=None, description="Filesystem device path (/dev/video0).")
    index: int | None = Field(default=None, ge=0, description="OpenCV device index.")
    url: str | None = Field(default=None, description="RTSP/HTTP stream URL or realsense serial.")


class CameraEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    name: str = Field(..., min_length=1, max_length=120)
    backend: CameraBackend
    source: CameraSource
    width: int = Field(..., ge=16, le=7680)
    height: int = Field(..., ge=16, le=4320)
    fps: float = Field(..., gt=0, le=240)
    codec_hint: str | None = Field(
        default=None,
        description="Optional encoder hint for WebRTC (e.g. 'h264', 'vp9').",
    )


# ---------------------------------------------------------------------------
# Teleop
# ---------------------------------------------------------------------------


TeleopKind = Literal["keyboard", "mouse", "gamepad", "leader_arm", "phone"]


class TeleopEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    name: str = Field(..., min_length=1, max_length=120)
    kind: TeleopKind
    config: dict[str, object] = Field(
        default_factory=dict,
        description="Free-form config (key bindings, dead zones, leader-arm port, ...).",
    )


# ---------------------------------------------------------------------------
# Robots
# ---------------------------------------------------------------------------

KnownRobotType = Literal[
    "so100_follower",
    "so101_follower",
    "koch_follower",
    "ur",
    "lekiwi",
    "hopejr",
    "reachy2",
    "viper",
    "unitree_g1",
]


class RobotEntry(BaseModel):
    """User-declared robot inventory item (persisted to registry.json).

    ``robot_type`` is deliberately str (not Literal) so unknown types
    round-trip cleanly. ``KnownRobotType`` is exported for the frontend
    Add-Robot dropdown and for the adapter factory's dispatch table.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    name: str = Field(..., min_length=1, max_length=120)
    robot_type: str = Field(
        ...,
        description="lerobot Robot kind. See KnownRobotType for well-known values.",
    )
    connection: Connection
    cameras: list[UUID] = Field(
        default_factory=list,
        description="Referenced CameraEntry.id values.",
    )
    teleop: UUID | None = Field(default=None, description="Optional bound TeleopEntry.id.")
    image_ref: str | None = Field(
        default=None,
        description="Static asset path for the UI card (e.g. /resources/so-101.png).",
    )


# ---------------------------------------------------------------------------
# Runtime status (not persisted)
# ---------------------------------------------------------------------------


class RobotStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    online: bool = False
    last_error: str | None = None
    connected_at: datetime | None = None


class CameraStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    online: bool = False
    last_error: str | None = None
    opened_at: datetime | None = None
    subscriber_count: int = 0
    frames_served: int = 0


class TeleopStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attached: bool = False
    bound_robot_id: UUID | None = None
    last_error: str | None = None
    attached_at: datetime | None = None


# ---------------------------------------------------------------------------
# Registry document
# ---------------------------------------------------------------------------


class RegistryDocument(BaseModel):
    """Full on-disk JSON shape for ~/.cache/lerobot/dashboard/registry.json."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    robots: list[RobotEntry] = Field(default_factory=list)
    cameras: list[CameraEntry] = Field(default_factory=list)
    teleops: list[TeleopEntry] = Field(default_factory=list)


__all__ = [
    "CameraBackend",
    "CameraEntry",
    "CameraSource",
    "CameraStatus",
    "Connection",
    "KnownRobotType",
    "NetworkConnection",
    "NetworkProtocol",
    "RegistryDocument",
    "RobotEntry",
    "RobotStatus",
    "SerialConnection",
    "TeleopEntry",
    "TeleopKind",
    "TeleopStatus",
]
