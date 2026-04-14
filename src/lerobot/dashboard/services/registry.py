"""JSON-backed registry for user-declared robots, cameras, and teleops.

The registry owns the on-disk inventory (``registry.json``) and serves as the
authoritative source for every CRUD endpoint under ``/api/robots``,
``/api/cameras``, and ``/api/teleops``. Live-runtime state (actual Robot
instances, WebRTC peer connections, etc.) lives elsewhere — the registry
only persists the user's declared intent.

Design
------
* **Single JSON file** (``<storage_dir>/registry.json``), schema described
  by :class:`RegistryDocument`. Future migrations can bump ``version``.
* **Concurrency**: every mutation takes an :class:`asyncio.Lock`. The lock
  guards both the in-memory cache and the on-disk file, so writers never
  observe torn state.
* **Atomic writes**: we serialize to a ``*.tmp`` sibling and ``os.replace``
  onto the target path. On POSIX this is atomic within a filesystem; on
  Windows ``os.replace`` is also atomic for overwrite.
* **Cross-ref integrity**: creating/updating a :class:`RobotEntry` that
  references non-existent camera or teleop IDs raises
  :class:`RegistryValidationError`. Deleting a camera or teleop that is
  still referenced by a robot is refused with the same error, so callers
  must detach first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterable
from pathlib import Path
from typing import TypeVar
from uuid import UUID

from pydantic import BaseModel, ValidationError

from lerobot.dashboard.services.registry_models import (
    CameraEntry,
    RegistryDocument,
    RobotEntry,
    TeleopEntry,
)

logger = logging.getLogger(__name__)

REGISTRY_FILENAME = "registry.json"

T = TypeVar("T", RobotEntry, CameraEntry, TeleopEntry)


class RegistryError(Exception):
    """Base class for registry errors surfaced to the API layer."""


class RegistryNotFoundError(RegistryError):
    """Raised when an entry with the requested id does not exist."""


class RegistryValidationError(RegistryError):
    """Raised when a mutation would leave the registry in an invalid state."""


class Registry:
    """Async, file-backed registry for dashboard inventory."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._doc: RegistryDocument | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    async def load(self) -> RegistryDocument:
        """Load (or initialize) the registry document from disk."""
        async with self._lock:
            self._doc = self._load_unlocked()
            return self._doc.model_copy(deep=True)

    def _load_unlocked(self) -> RegistryDocument:
        if not self._path.exists():
            logger.info("registry file %s missing — initializing empty document", self._path)
            doc = RegistryDocument()
            self._write_atomic(doc)
            return doc
        try:
            raw = self._path.read_text(encoding="utf-8")
            return RegistryDocument.model_validate_json(raw)
        except (OSError, ValidationError, json.JSONDecodeError) as exc:
            # A corrupt or unreadable registry must not take the whole dashboard
            # down — the user may still want to create new entries. We preserve
            # the bad file alongside ``.broken`` for forensic recovery.
            broken = self._path.with_suffix(self._path.suffix + ".broken")
            try:
                if self._path.exists():
                    os.replace(self._path, broken)
            except OSError:  # pragma: no cover — best-effort rename
                logger.exception("failed to preserve broken registry at %s", broken)
            logger.error("registry at %s unreadable (%s); starting fresh", self._path, exc)
            doc = RegistryDocument()
            self._write_atomic(doc)
            return doc

    def _write_atomic(self, doc: RegistryDocument) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = doc.model_dump_json(indent=2)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self._path)

    async def _persist(self) -> None:
        assert self._doc is not None
        self._write_atomic(self._doc)

    def _ensure_loaded(self) -> RegistryDocument:
        if self._doc is None:
            self._doc = self._load_unlocked()
        return self._doc

    # ------------------------------------------------------------------
    # Listing / lookup
    # ------------------------------------------------------------------

    async def snapshot(self) -> RegistryDocument:
        """Return a deep copy of the current document (safe to mutate)."""
        async with self._lock:
            return self._ensure_loaded().model_copy(deep=True)

    async def list_robots(self) -> list[RobotEntry]:
        async with self._lock:
            return [r.model_copy(deep=True) for r in self._ensure_loaded().robots]

    async def list_cameras(self) -> list[CameraEntry]:
        async with self._lock:
            return [c.model_copy(deep=True) for c in self._ensure_loaded().cameras]

    async def list_teleops(self) -> list[TeleopEntry]:
        async with self._lock:
            return [t.model_copy(deep=True) for t in self._ensure_loaded().teleops]

    async def get_robot(self, robot_id: UUID) -> RobotEntry:
        async with self._lock:
            return self._find(self._ensure_loaded().robots, robot_id, "robot").model_copy(deep=True)

    async def get_camera(self, camera_id: UUID) -> CameraEntry:
        async with self._lock:
            return self._find(self._ensure_loaded().cameras, camera_id, "camera").model_copy(deep=True)

    async def get_teleop(self, teleop_id: UUID) -> TeleopEntry:
        async with self._lock:
            return self._find(self._ensure_loaded().teleops, teleop_id, "teleop").model_copy(deep=True)

    # ------------------------------------------------------------------
    # Create / patch / delete — robots
    # ------------------------------------------------------------------

    async def create_robot(self, entry: RobotEntry) -> RobotEntry:
        async with self._lock:
            doc = self._ensure_loaded()
            self._assert_unique_id(doc.robots, entry.id, "robot")
            self._validate_robot_refs(entry, doc)
            doc.robots.append(entry)
            await self._persist()
            return entry.model_copy(deep=True)

    async def update_robot(self, robot_id: UUID, patch: dict[str, object]) -> RobotEntry:
        async with self._lock:
            doc = self._ensure_loaded()
            current = self._find(doc.robots, robot_id, "robot")
            updated = _apply_patch(current, patch)
            # ``id`` is intentionally immutable from the outside.
            if updated.id != current.id:
                raise RegistryValidationError("robot id cannot be changed via PATCH")
            self._validate_robot_refs(updated, doc)
            idx = doc.robots.index(current)
            doc.robots[idx] = updated
            await self._persist()
            return updated.model_copy(deep=True)

    async def delete_robot(self, robot_id: UUID) -> None:
        async with self._lock:
            doc = self._ensure_loaded()
            target = self._find(doc.robots, robot_id, "robot")
            doc.robots.remove(target)
            await self._persist()

    # ------------------------------------------------------------------
    # Create / patch / delete — cameras
    # ------------------------------------------------------------------

    async def create_camera(self, entry: CameraEntry) -> CameraEntry:
        async with self._lock:
            doc = self._ensure_loaded()
            self._assert_unique_id(doc.cameras, entry.id, "camera")
            doc.cameras.append(entry)
            await self._persist()
            return entry.model_copy(deep=True)

    async def update_camera(self, camera_id: UUID, patch: dict[str, object]) -> CameraEntry:
        async with self._lock:
            doc = self._ensure_loaded()
            current = self._find(doc.cameras, camera_id, "camera")
            updated = _apply_patch(current, patch)
            if updated.id != current.id:
                raise RegistryValidationError("camera id cannot be changed via PATCH")
            idx = doc.cameras.index(current)
            doc.cameras[idx] = updated
            await self._persist()
            return updated.model_copy(deep=True)

    async def delete_camera(self, camera_id: UUID) -> None:
        async with self._lock:
            doc = self._ensure_loaded()
            target = self._find(doc.cameras, camera_id, "camera")
            referrers = [r for r in doc.robots if target.id in r.cameras]
            if referrers:
                names = ", ".join(f"{r.name}({r.id})" for r in referrers)
                raise RegistryValidationError(f"camera {camera_id} is still referenced by robots: {names}")
            doc.cameras.remove(target)
            await self._persist()

    # ------------------------------------------------------------------
    # Create / patch / delete — teleops
    # ------------------------------------------------------------------

    async def create_teleop(self, entry: TeleopEntry) -> TeleopEntry:
        async with self._lock:
            doc = self._ensure_loaded()
            self._assert_unique_id(doc.teleops, entry.id, "teleop")
            doc.teleops.append(entry)
            await self._persist()
            return entry.model_copy(deep=True)

    async def update_teleop(self, teleop_id: UUID, patch: dict[str, object]) -> TeleopEntry:
        async with self._lock:
            doc = self._ensure_loaded()
            current = self._find(doc.teleops, teleop_id, "teleop")
            updated = _apply_patch(current, patch)
            if updated.id != current.id:
                raise RegistryValidationError("teleop id cannot be changed via PATCH")
            idx = doc.teleops.index(current)
            doc.teleops[idx] = updated
            await self._persist()
            return updated.model_copy(deep=True)

    async def delete_teleop(self, teleop_id: UUID) -> None:
        async with self._lock:
            doc = self._ensure_loaded()
            target = self._find(doc.teleops, teleop_id, "teleop")
            referrers = [r for r in doc.robots if r.teleop == target.id]
            if referrers:
                names = ", ".join(f"{r.name}({r.id})" for r in referrers)
                raise RegistryValidationError(f"teleop {teleop_id} is still referenced by robots: {names}")
            doc.teleops.remove(target)
            await self._persist()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find(items: Iterable[T], target_id: UUID, kind: str) -> T:
        for item in items:
            if item.id == target_id:
                return item
        raise RegistryNotFoundError(f"{kind} {target_id} not found")

    @staticmethod
    def _assert_unique_id(items: Iterable[BaseModel], target_id: UUID, kind: str) -> None:
        for item in items:
            if getattr(item, "id", None) == target_id:
                raise RegistryValidationError(f"{kind} with id {target_id} already exists")

    @staticmethod
    def _validate_robot_refs(entry: RobotEntry, doc: RegistryDocument) -> None:
        camera_ids = {c.id for c in doc.cameras}
        missing_cams = [cid for cid in entry.cameras if cid not in camera_ids]
        if missing_cams:
            raise RegistryValidationError(
                f"robot references unknown cameras: {', '.join(str(c) for c in missing_cams)}"
            )
        if entry.teleop is not None:
            teleop_ids = {t.id for t in doc.teleops}
            if entry.teleop not in teleop_ids:
                raise RegistryValidationError(f"robot references unknown teleop: {entry.teleop}")


def _apply_patch[T: (RobotEntry, CameraEntry, TeleopEntry)](current: T, patch: dict[str, object]) -> T:
    """Return a new model instance with ``patch`` merged over ``current``.

    Pydantic v2 ``model_copy(update=...)`` doesn't re-run validators on the
    updated fields, which would let the client slip bad nested data past us.
    Dumping + validate round-trip is the officially-recommended pattern.
    """
    merged = current.model_dump(mode="python")
    merged.update(patch)
    try:
        return type(current).model_validate(merged)
    except ValidationError as exc:
        raise RegistryValidationError(str(exc)) from exc


def registry_path_for(storage_dir: Path) -> Path:
    return storage_dir / REGISTRY_FILENAME
