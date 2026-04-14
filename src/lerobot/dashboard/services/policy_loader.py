"""Shared policy discovery and loading utilities for the dashboard.

Extracted from :mod:`lerobot.dashboard.services.inference` so that both
the VLA inference service (Task #14) and the benchmark runner (Task #16)
can share policy loading without duplicating the HF-cache scanning logic.

Public surface
--------------
* :class:`PolicyDescriptor` — immutable summary of a cached checkpoint.
* :func:`scan_hf_cache` — enumerate all locally cached LeRobot policies.
* :class:`PolicyCache` — LRU async cache of loaded :class:`PreTrainedPolicy`
  handles; thread-safe; injectable loader for tests.
* :func:`load_policy` — convenience one-shot loader (bypasses the cache).
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

_LANGUAGE_POLICY_TYPES: frozenset[str] = frozenset({"pi0", "pi05", "smolvla", "wall_x"})


# ---------------------------------------------------------------------------
# Policy descriptor (immutable cache-scan result)
# ---------------------------------------------------------------------------


class PolicyDescriptor(BaseModel):
    """Immutable summary of a locally cached policy checkpoint.

    Populated by :func:`scan_hf_cache` from ``config.json`` in the HF
    hub cache.  All fields are optional except ``repo_id``, ``policy_type``,
    and ``root`` so partial / in-progress downloads don't crash the scan.
    """

    model_config = ConfigDict(extra="forbid")

    repo_id: str
    """HuggingFace repo ID (``owner/model`` or plain ``model``)."""
    policy_type: str
    """Registered policy type string, e.g. ``"smolvla"``, ``"act"``."""
    root: str
    """Absolute path to the HF cache repo root directory."""
    num_parameters: int | None = None
    last_modified: datetime | None = None
    observation_features: dict[str, Any] | None = None
    action_features: dict[str, Any] | None = None
    supports_language: bool = False

    @property
    def name(self) -> str:
        """Human-readable display name (alias for ``repo_id``)."""
        return self.repo_id


# ---------------------------------------------------------------------------
# HF cache scan
# ---------------------------------------------------------------------------


def scan_hf_cache(cache_dir: Path | None = None) -> list[PolicyDescriptor]:
    """Enumerate locally cached LeRobot policies.

    Scans the HuggingFace cache directory (``cache_dir``, defaulting to
    ``HF_HOME/hub``) for repos whose ``config.json`` declares a
    ``policy_type`` known to :func:`lerobot.policies.factory.get_policy_class`.
    Unknown or partially-downloaded repos are skipped with a debug log
    rather than raising — a missing policy file shouldn't block the UI
    from listing the rest.

    Returns descriptors sorted by ``last_modified`` descending (newest
    first), so the picker's default selection is the most recently used
    checkpoint.
    """
    from huggingface_hub import scan_cache_dir

    try:
        info = scan_cache_dir(cache_dir) if cache_dir is not None else scan_cache_dir()
    except Exception as exc:  # noqa: BLE001 — a bad cache should not break list endpoints
        logger.warning("scan_cache_dir failed: %s", exc)
        return []

    descriptors: list[PolicyDescriptor] = []
    for repo in info.repos:
        if repo.repo_type != "model":
            continue
        descriptor = _describe_cached_repo(repo)
        if descriptor is not None:
            descriptors.append(descriptor)
    descriptors.sort(key=lambda d: (d.last_modified or datetime.min), reverse=True)
    return descriptors


def _describe_cached_repo(repo: Any) -> PolicyDescriptor | None:
    """Build a :class:`PolicyDescriptor` from a single ``CachedRepoInfo``."""
    import json

    repo_path = Path(repo.repo_path)
    config_path = _find_first(repo_path, "config.json")
    if config_path is None:
        return None
    try:
        with config_path.open(encoding="utf-8") as fp:
            config = json.load(fp)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("skip cached repo %s (unreadable config.json): %s", repo.repo_id, exc)
        return None
    policy_type = config.get("policy_type") or config.get("type")
    if not policy_type:
        return None
    if not _is_known_policy_type(policy_type):
        return None
    last_modified = datetime.fromtimestamp(repo.last_modified, tz=UTC) if repo.last_modified else None
    return PolicyDescriptor(
        repo_id=repo.repo_id,
        policy_type=policy_type,
        root=str(repo_path),
        num_parameters=config.get("num_parameters"),
        last_modified=last_modified,
        observation_features=config.get("input_features") or config.get("observation_features"),
        action_features=config.get("output_features") or config.get("action_features"),
        supports_language=policy_type in _LANGUAGE_POLICY_TYPES,
    )


def _find_first(root: Path, filename: str) -> Path | None:
    """Return the first ``filename`` under ``root`` (breadth-first)."""
    for candidate in root.rglob(filename):
        if candidate.is_file():
            return candidate
    return None


def _is_known_policy_type(policy_type: str) -> bool:
    try:
        from lerobot.policies.factory import get_policy_class

        get_policy_class(policy_type)
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# Policy loading
# ---------------------------------------------------------------------------

PolicyLoader = Callable[[str], Any]
"""Callable ``repo_id -> PreTrainedPolicy``. Injectable for tests."""


def load_policy(repo_id: str, device: str = "cpu") -> Any:  # pragma: no cover — touches HF cache
    """Load a :class:`PreTrainedPolicy` from the local HF cache.

    Resolves the concrete policy class from ``config.json`` (via draccus +
    ``get_policy_class``), then loads the safetensors weights.  Always
    places the model on ``device`` and returns it in eval mode.

    The ``device`` parameter is provided for benchmark / eval contexts that
    need to pin the policy to a specific device.  The VLA inference service
    uses this via :class:`PolicyCache` after the model is loaded, letting
    the policy config's own ``device`` field take precedence at load time.

    Raises :exc:`ValueError` if the policy type is unknown or the checkpoint
    is not found in the local cache.
    """
    from lerobot.configs.policies import PreTrainedConfig
    from lerobot.policies.factory import get_policy_class

    config = PreTrainedConfig.from_pretrained(repo_id)
    cls = get_policy_class(config.type)
    policy = cls.from_pretrained(repo_id, config=config)
    if device != getattr(getattr(policy, "config", None), "device", device):
        import torch

        policy = policy.to(torch.device(device))
    return policy


def _default_policy_loader(repo_id: str) -> Any:  # pragma: no cover — touches HF cache
    """Default :data:`PolicyLoader` using the config's own device setting."""
    return load_policy(repo_id)


# ---------------------------------------------------------------------------
# LRU policy cache
# ---------------------------------------------------------------------------


class PolicyCache:
    """Least-recently-used cache of loaded :class:`PreTrainedPolicy` handles.

    Default ``max_resident=2`` keeps typical dashboard memory bounded when
    the operator toggles between a small set of policies without forcing
    a weight reload on every start/stop.

    Thread-safe: ``get`` loads outside the internal lock so concurrent
    calls for *different* repos don't block each other; the lock only
    guards eviction bookkeeping.

    Usage::

        cache = PolicyCache()
        policy = await cache.get("lerobot/smolvla_base")
        action = policy.select_action(obs)
    """

    def __init__(
        self,
        loader: PolicyLoader | None = None,
        max_resident: int = 2,
    ) -> None:
        self._loader = loader or _default_policy_loader
        self._max = max_resident
        self._lock = asyncio.Lock()
        self._entries: OrderedDict[str, Any] = OrderedDict()

    async def get(self, repo_id: str) -> Any:
        """Return the policy for ``repo_id``, loading on first access."""
        async with self._lock:
            if repo_id in self._entries:
                self._entries.move_to_end(repo_id)
                return self._entries[repo_id]
        # Load outside the lock — a cold model can take seconds and we
        # don't want to block concurrent ``get`` calls for other repos.
        policy = await asyncio.to_thread(self._loader, repo_id)
        async with self._lock:
            self._entries[repo_id] = policy
            self._entries.move_to_end(repo_id)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)
        return policy

    async def evict(self, repo_id: str) -> None:
        """Remove ``repo_id`` from the cache, freeing its GPU/RAM allocation."""
        async with self._lock:
            self._entries.pop(repo_id, None)

    async def clear(self) -> None:
        """Evict all entries."""
        async with self._lock:
            self._entries.clear()


__all__ = [
    "PolicyCache",
    "PolicyDescriptor",
    "PolicyLoader",
    "load_policy",
    "scan_hf_cache",
]
