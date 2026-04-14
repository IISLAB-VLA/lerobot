"""Device discovery REST API.

Exposes live hardware enumeration to the frontend:

* ``GET /api/devices/serial``    — USB-serial ports (pyserial-backed).
* ``GET /api/devices/cameras``   — OpenCV + optional RealSense cameras.
* ``POST /api/devices/network``  — probe a host/port for a known robot
  protocol (RTDE/FCI/generic TCP).

The endpoints are thin wrappers around existing ``lerobot`` utilities so
the dashboard inherits every hardware backend added upstream. Blocking
I/O runs on a worker thread via :func:`asyncio.to_thread` so the event
loop stays responsive.

When :envvar:`LEROBOT_DASHBOARD_FAKE_DEVICES` is truthy the endpoints
return deterministic mock fixtures. The shapes match the registry
models so the frontend Add-Robot flow can exercise every code path
without physical hardware present (qa-engineer's Playwright specs rely
on this).
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import socket
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/devices", tags=["devices"])


# ---------------------------------------------------------------------------
# Response models (aligned with services.registry_models)
# ---------------------------------------------------------------------------


class SerialPortInfo(BaseModel):
    port: str = Field(..., description="Stable device path if available, else raw device node.")
    raw_port: str = Field(..., description="Original pyserial device node (e.g. /dev/ttyACM0).")
    description: str | None = None
    vid: str | None = Field(default=None, description="USB vendor id as hex string (e.g. 0x0403).")
    pid: str | None = Field(default=None, description="USB product id as hex string.")
    serial_number: str | None = None
    manufacturer: str | None = None
    product: str | None = None


class SerialDiscoveryResponse(BaseModel):
    ports: list[SerialPortInfo]


CameraBackend = Literal["opencv", "realsense"]


class CameraStreamProfile(BaseModel):
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    format: str | None = None


class CameraInfo(BaseModel):
    backend: CameraBackend
    id: str = Field(..., description="Stable identifier: device path for OpenCV, serial for RealSense.")
    index: int | None = Field(default=None, description="OpenCV numeric index when applicable.")
    path: str | None = Field(default=None, description="Filesystem device path (Linux only).")
    name: str | None = None
    default_profile: CameraStreamProfile = Field(default_factory=CameraStreamProfile)
    extra: dict[str, object] = Field(default_factory=dict)


class CameraDiscoveryResponse(BaseModel):
    cameras: list[CameraInfo]
    realsense_available: bool = Field(
        ...,
        description="False when pyrealsense2 is not importable; the realsense list is then empty.",
    )


class NetworkProbeRequest(BaseModel):
    host: str = Field(..., min_length=1)
    port: int | None = Field(default=None, ge=1, le=65_535)
    protocol: Literal["rtde", "fci", "tcp"] | None = Field(
        default=None,
        description="Optional protocol hint. When omitted the port default is used (rtde=30004, fci=10001).",
    )
    timeout_ms: int = Field(default=1000, ge=50, le=10_000)


class NetworkProbeResponse(BaseModel):
    host: str
    port: int
    protocol: Literal["rtde", "fci", "tcp"]
    reachable: bool
    latency_ms: float | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Fake fixtures — shared with qa-engineer's Playwright specs
# ---------------------------------------------------------------------------


_FAKE_SERIAL_PORTS: list[SerialPortInfo] = [
    SerialPortInfo(
        port="/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A1B2C3-if00-port0",
        raw_port="/dev/ttyUSB0",
        description="FT232R USB UART",
        vid="0x0403",
        pid="0x6001",
        serial_number="A1B2C3",
        manufacturer="FTDI",
        product="FT232R USB UART",
    ),
    SerialPortInfo(
        port="/dev/serial/by-id/usb-Feetech_SCS_Servo_BUS-if00",
        raw_port="/dev/ttyACM0",
        description="Feetech SCS Servo BUS",
        vid="0x1a86",
        pid="0x7523",
        serial_number="FTS-LEADER-001",
        manufacturer="Feetech",
        product="SCS Servo BUS",
    ),
]


_FAKE_CAMERAS: list[CameraInfo] = [
    CameraInfo(
        backend="opencv",
        id="/dev/video0",
        index=0,
        path="/dev/video0",
        name="Fake USB Camera 0",
        default_profile=CameraStreamProfile(width=1280, height=720, fps=30.0, format="MJPG"),
    ),
    CameraInfo(
        backend="opencv",
        id="/dev/video2",
        index=2,
        path="/dev/video2",
        name="Fake USB Camera 1",
        default_profile=CameraStreamProfile(width=640, height=480, fps=30.0, format="YUYV"),
    ),
    CameraInfo(
        backend="realsense",
        id="FAKE-RS-0123456789",
        name="Intel RealSense D435i (fake)",
        default_profile=CameraStreamProfile(width=1280, height=720, fps=30.0, format="RGB8"),
        extra={"firmware_version": "5.13.0.50", "product_line": "D400"},
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_fake(request: Request) -> bool:
    state = getattr(request.app.state, "dashboard", None)
    if state is not None:
        return bool(state.config.fake_devices)
    return os.environ.get("LEROBOT_DASHBOARD_FAKE_DEVICES", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _hex4(value: int | None) -> str | None:
    if value is None:
        return None
    return f"0x{value:04x}"


def _stable_serial_path(raw_port: str) -> str:
    """Return /dev/serial/by-id/<id> that resolves to ``raw_port`` if any.

    Falls back to ``raw_port`` on macOS/Windows or when no stable symlink
    exists. The registry prefers the stable path so unplug/replug cycles
    don't invalidate a saved robot entry.
    """
    if platform.system() != "Linux":
        return raw_port
    by_id = Path("/dev/serial/by-id")
    if not by_id.is_dir():
        return raw_port
    try:
        target = Path(raw_port).resolve()
    except OSError:
        return raw_port
    for link in by_id.iterdir():
        try:
            if link.resolve() == target:
                return str(link)
        except OSError:
            continue
    return raw_port


def _list_serial_ports_sync() -> list[SerialPortInfo]:
    from serial.tools import list_ports  # type: ignore[import-not-found]

    ports: list[SerialPortInfo] = []
    for info in list_ports.comports():
        raw = info.device
        ports.append(
            SerialPortInfo(
                port=_stable_serial_path(raw),
                raw_port=raw,
                description=info.description if info.description != "n/a" else None,
                vid=_hex4(info.vid),
                pid=_hex4(info.pid),
                serial_number=info.serial_number,
                manufacturer=info.manufacturer,
                product=info.product,
            )
        )
    ports.sort(key=lambda p: p.raw_port)
    return ports


def _list_opencv_cameras_sync() -> list[CameraInfo]:
    from lerobot.scripts.lerobot_find_cameras import find_all_opencv_cameras

    results: list[CameraInfo] = []
    for meta in find_all_opencv_cameras():
        cam_id = meta.get("id")
        path = cam_id if isinstance(cam_id, str) else None
        index = cam_id if isinstance(cam_id, int) else None
        profile_raw = meta.get("default_stream_profile") or {}
        results.append(
            CameraInfo(
                backend="opencv",
                id=str(cam_id),
                index=index,
                path=path,
                name=meta.get("name"),
                default_profile=CameraStreamProfile(
                    width=profile_raw.get("width"),
                    height=profile_raw.get("height"),
                    fps=profile_raw.get("fps"),
                    format=profile_raw.get("fourcc") or profile_raw.get("format"),
                ),
                extra={"backend_api": meta.get("backend_api")} if meta.get("backend_api") else {},
            )
        )
    return results


def _list_realsense_cameras_sync() -> tuple[list[CameraInfo], bool]:
    """Return (cameras, available). ``available`` is False when pyrealsense2 is missing."""
    try:
        import pyrealsense2  # type: ignore[import-not-found]  # noqa: F401
    except Exception:
        return [], False

    from lerobot.scripts.lerobot_find_cameras import find_all_realsense_cameras

    results: list[CameraInfo] = []
    for meta in find_all_realsense_cameras():
        profile_raw = meta.get("default_stream_profile") or {}
        results.append(
            CameraInfo(
                backend="realsense",
                id=str(meta.get("id")),
                name=meta.get("name"),
                default_profile=CameraStreamProfile(
                    width=profile_raw.get("width"),
                    height=profile_raw.get("height"),
                    fps=profile_raw.get("fps"),
                    format=profile_raw.get("format"),
                ),
                extra={
                    k: v
                    for k, v in meta.items()
                    if k
                    in {
                        "firmware_version",
                        "usb_type_descriptor",
                        "physical_port",
                        "product_id",
                        "product_line",
                    }
                    and v is not None
                },
            )
        )
    return results, True


_PROTOCOL_DEFAULT_PORT: dict[str, int] = {"rtde": 30004, "fci": 10001, "tcp": 0}


def _resolve_protocol_and_port(
    protocol: str | None, port: int | None
) -> tuple[Literal["rtde", "fci", "tcp"], int]:
    if protocol is None and port is None:
        raise HTTPException(
            status_code=422,
            detail="Either 'protocol' or 'port' must be provided.",
        )
    if protocol is None:
        assert port is not None
        for name, default in _PROTOCOL_DEFAULT_PORT.items():
            if default == port and name != "tcp":
                return name, port  # type: ignore[return-value]
        return "tcp", port
    resolved_port = port if port is not None else _PROTOCOL_DEFAULT_PORT.get(protocol, 0)
    if resolved_port == 0:
        raise HTTPException(
            status_code=422,
            detail=f"Protocol '{protocol}' requires an explicit port.",
        )
    return protocol, resolved_port  # type: ignore[return-value]


def _probe_tcp_sync(host: str, port: int, timeout_s: float) -> tuple[bool, float | None, str | None]:
    import time

    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            latency_ms = (time.perf_counter() - start) * 1000.0
            return True, round(latency_ms, 2), None
    except (TimeoutError, OSError) as exc:
        return False, None, str(exc) or exc.__class__.__name__


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/serial", response_model=SerialDiscoveryResponse)
async def get_serial_ports(request: Request) -> SerialDiscoveryResponse:
    if _is_fake(request):
        return SerialDiscoveryResponse(ports=list(_FAKE_SERIAL_PORTS))
    try:
        ports = await asyncio.to_thread(_list_serial_ports_sync)
    except ImportError as exc:
        logger.warning("pyserial missing: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="pyserial is not installed. Install the 'hardware' extra.",
        ) from exc
    return SerialDiscoveryResponse(ports=ports)


@router.get("/cameras", response_model=CameraDiscoveryResponse)
async def get_cameras(request: Request) -> CameraDiscoveryResponse:
    if _is_fake(request):
        return CameraDiscoveryResponse(
            cameras=list(_FAKE_CAMERAS),
            realsense_available=True,
        )
    opencv_cams = await asyncio.to_thread(_list_opencv_cameras_sync)
    realsense_cams, realsense_ok = await asyncio.to_thread(_list_realsense_cameras_sync)
    return CameraDiscoveryResponse(
        cameras=[*opencv_cams, *realsense_cams],
        realsense_available=realsense_ok,
    )


@router.post("/network", response_model=NetworkProbeResponse)
async def probe_network(request: Request, body: NetworkProbeRequest) -> NetworkProbeResponse:
    protocol, port = _resolve_protocol_and_port(body.protocol, body.port)
    if _is_fake(request):
        reachable = body.host in {"127.0.0.1", "localhost", "fake-robot"}
        return NetworkProbeResponse(
            host=body.host,
            port=port,
            protocol=protocol,
            reachable=reachable,
            latency_ms=1.23 if reachable else None,
            error=None if reachable else "fake: host not in allowlist",
        )
    timeout_s = body.timeout_ms / 1000.0
    reachable, latency_ms, error = await asyncio.to_thread(_probe_tcp_sync, body.host, port, timeout_s)
    return NetworkProbeResponse(
        host=body.host,
        port=port,
        protocol=protocol,
        reachable=reachable,
        latency_ms=latency_ms,
        error=error,
    )
