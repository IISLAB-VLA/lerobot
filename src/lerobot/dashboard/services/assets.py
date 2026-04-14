"""Robot image asset manager.

Downloads canonical hero images for known robot models to a local cache
under the dashboard storage dir, and serves them via ``/resources/robots``
(mounted as a ``StaticFiles`` app in :mod:`lerobot.dashboard.app`).

Design
------
* :data:`ROBOT_IMAGE_MAP` — curated ``model -> AssetSpec`` table with URL,
  license, and source attribution. Unknown / unverified models resolve to
  a bundled generic icon.
* :class:`AssetManager` — lazy cache on top of that table. Safe to call
  ``resolve_url`` on every request; it only touches the network when the
  file is missing and ``LEROBOT_OFFLINE`` is unset.
* ``SOURCES.md`` is regenerated next to the cache so license audits are
  possible without re-reading this module.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)

ENV_OFFLINE = "LEROBOT_OFFLINE"

RESOURCE_URL_PREFIX = "/resources/robots"
ROBOTS_SUBDIR = "robots"
GENERIC_FILENAME = "_generic.svg"


ImageExt = Literal["png", "jpg", "jpeg", "webp", "svg"]


@dataclass(frozen=True, slots=True)
class AssetSpec:
    """Immutable description of a single robot image asset."""

    model: str
    url: str
    license: str
    source: str
    ext: ImageExt

    @property
    def filename(self) -> str:
        return f"{self.model}.{self.ext}"


# ---------------------------------------------------------------------------
# Curated URL map
# ---------------------------------------------------------------------------
#
# URLs were verified (HTTP 200 + image Content-Type) at task-#7 curation
# time. The ``license`` and ``source`` fields are copied into SOURCES.md
# so they survive outside this module.

_CANONICAL_SPECS: tuple[AssetSpec, ...] = (
    AssetSpec(
        model="so100_follower",
        url="https://raw.githubusercontent.com/TheRobotStudio/SO-ARM100/main/media/Leader_And_Follower_SO100.jpg",
        license="Apache-2.0",
        source="github.com/TheRobotStudio/SO-ARM100",
        ext="jpg",
    ),
    AssetSpec(
        model="so101_follower",
        url="https://raw.githubusercontent.com/TheRobotStudio/SO-ARM100/main/media/SO101_Follower.webp",
        license="Apache-2.0",
        source="github.com/TheRobotStudio/SO-ARM100",
        ext="webp",
    ),
    AssetSpec(
        model="ur5e",
        url="https://upload.wikimedia.org/wikipedia/commons/7/7c/Robot_Lengan_Industri_UR5e.jpg",
        license="CC BY-SA 4.0",
        source="Wikimedia Commons (Robot_Lengan_Industri_UR5e.jpg)",
        ext="jpg",
    ),
    AssetSpec(
        model="ur16e",
        url="https://upload.wikimedia.org/wikipedia/commons/1/16/UR16e_robot_arm.png",
        license="CC BY-SA 4.0",
        source="Wikimedia Commons (UR16e_robot_arm.png)",
        ext="png",
    ),
    AssetSpec(
        model="franka_panda",
        url="https://upload.wikimedia.org/wikipedia/commons/e/e9/Franka_Emika1.jpg",
        license="CC BY-SA 4.0",
        source="Wikimedia Commons (Franka_Emika1.jpg)",
        ext="jpg",
    ),
    AssetSpec(
        model="lekiwi",
        url="https://huggingface.co/datasets/huggingface/documentation-images/resolve/main/lerobot/1740517739083.jpeg",
        license="HuggingFace documentation (public CDN)",
        source="huggingface.co/datasets/huggingface/documentation-images",
        ext="jpeg",
    ),
    AssetSpec(
        model="unitree_g1",
        url="https://upload.wikimedia.org/wikipedia/commons/8/8a/Unitree_G1.jpg",
        license="CC BY-SA 4.0",
        source="Wikimedia Commons (Unitree_G1.jpg)",
        ext="jpg",
    ),
)


def _build_image_map() -> dict[str, AssetSpec]:
    m: dict[str, AssetSpec] = {s.model: s for s in _CANONICAL_SPECS}
    # Convenient aliases used by the frontend dropdown and registry_models.
    m["so-100"] = m["so100_follower"]
    m["so100"] = m["so100_follower"]
    m["so-101"] = m["so101_follower"]
    m["so101"] = m["so101_follower"]
    m["ur"] = m["ur5e"]  # generic UR fallback until per-model assets exist
    return m


ROBOT_IMAGE_MAP: dict[str, AssetSpec] = _build_image_map()


# Models recognised elsewhere in lerobot (KnownRobotType / docs) that do
# *not* have a verified CC/open-license image yet. Kept as an explicit
# allowlist so the generic fallback is a deliberate choice, not a bug.
UNVERIFIED_MODELS: frozenset[str] = frozenset(
    {
        "ur3e",
        "ur10e",
        "ur20",
        "ur30",
        "franka_research_3",
        "koch_follower",
        "koch_v11",
        "aloha",
        "reachy2",
        "viper",
        "hopejr",
    }
)


GENERIC_FALLBACK_SVG = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 160" '
    b'width="160" height="160" role="img" aria-label="Robot">\n'
    b'  <rect width="160" height="160" rx="16" fill="#1f2937"/>\n'
    b'  <g fill="#9ca3af">\n'
    b'    <circle cx="80" cy="58" r="20"/>\n'
    b'    <rect x="52" y="82" width="56" height="54" rx="8"/>\n'
    b'    <rect x="36" y="96" width="14" height="36" rx="5"/>\n'
    b'    <rect x="110" y="96" width="14" height="36" rx="5"/>\n'
    b'    <circle cx="72" cy="56" fill="#1f2937" r="3"/>\n'
    b'    <circle cx="88" cy="56" fill="#1f2937" r="3"/>\n'
    b'  </g>\n'
    b"</svg>\n"
)


# ---------------------------------------------------------------------------
# HTTP client protocol (lets tests inject a fake)
# ---------------------------------------------------------------------------


class _HttpResponse(Protocol):
    content: bytes

    def raise_for_status(self) -> Any: ...


class _HttpClient(Protocol):
    def get(self, url: str, *, headers: dict[str, str] | None = ...) -> _HttpResponse: ...


HttpClientFactory = Callable[[], AbstractContextManager[_HttpClient]]


def _default_http_client_factory() -> AbstractContextManager[_HttpClient]:
    import httpx

    return httpx.Client(follow_redirects=True, timeout=20.0)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# AssetManager
# ---------------------------------------------------------------------------


def _parse_offline_env(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class AssetManager:
    """Lazy on-disk cache for robot hero images.

    Parameters
    ----------
    cache_dir:
        Parent directory for cached files. ``cache_dir / 'robots'`` is
        created on first use.
    offline:
        When True the manager never touches the network — unknown models
        resolve to the bundled generic SVG. When ``None`` the flag is read
        from ``$LEROBOT_OFFLINE``.
    http_client_factory:
        Context-manager factory returning an object with ``.get(url, headers=...)``
        that yields an object with ``.content`` and ``.raise_for_status()``.
        Defaults to ``httpx.Client``. Injected in tests.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        offline: bool | None = None,
        http_client_factory: HttpClientFactory | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.robots_dir = self.cache_dir / ROBOTS_SUBDIR
        self.robots_dir.mkdir(parents=True, exist_ok=True)
        self.offline = offline if offline is not None else _parse_offline_env(os.environ.get(ENV_OFFLINE))
        self._http_client_factory = http_client_factory or _default_http_client_factory
        self._ensure_generic_fallback()

    # ----- paths ---------------------------------------------------------

    @property
    def generic_path(self) -> Path:
        return self.robots_dir / GENERIC_FILENAME

    @property
    def generic_url(self) -> str:
        return f"{RESOURCE_URL_PREFIX}/{GENERIC_FILENAME}"

    def asset_path(self, model: str) -> Path:
        """Return the cached :class:`Path` for ``model``, or the generic fallback.

        Does **not** trigger a download.
        """
        spec = ROBOT_IMAGE_MAP.get(model)
        if spec is None:
            return self.generic_path
        path = self.robots_dir / spec.filename
        return path if path.is_file() else self.generic_path

    def resolve_url(self, model: str) -> str:
        """Return the public ``/resources/robots/...`` URL for ``model``.

        Triggers a one-shot blocking download when the file is missing and
        the manager is online. Falls back to the generic icon on any
        network error so the UI never breaks on an image fetch.
        """
        spec = ROBOT_IMAGE_MAP.get(model)
        if spec is None:
            return self.generic_url
        path = self.robots_dir / spec.filename
        if not path.is_file() and not self.offline:
            try:
                self._download(spec)
            except Exception as exc:  # noqa: BLE001 — downgrade to fallback
                logger.warning("asset download failed for %s: %s", model, exc)
        if path.is_file():
            return f"{RESOURCE_URL_PREFIX}/{spec.filename}"
        return self.generic_url

    # ----- bulk prefetch + docs ------------------------------------------

    def prefetch_all(self) -> dict[str, bool]:
        """Download every canonical asset. Returns ``{model: cached_now}``.

        Safe to call at dashboard startup. Respects :envvar:`LEROBOT_OFFLINE`.
        Rewrites ``SOURCES.md`` every time so the file stays in sync with
        the in-memory table.
        """
        self._write_sources_doc()
        results: dict[str, bool] = {}
        for spec in _CANONICAL_SPECS:
            path = self.robots_dir / spec.filename
            if path.is_file():
                results[spec.model] = True
                continue
            if self.offline:
                results[spec.model] = False
                continue
            try:
                self._download(spec)
                results[spec.model] = True
            except Exception as exc:  # noqa: BLE001 — aggregated in return
                logger.warning("prefetch %s failed: %s", spec.model, exc)
                results[spec.model] = False
        return results

    # ----- internals -----------------------------------------------------

    def _ensure_generic_fallback(self) -> None:
        if not self.generic_path.is_file():
            self.generic_path.write_bytes(GENERIC_FALLBACK_SVG)

    def _download(self, spec: AssetSpec) -> None:
        dest = self.robots_dir / spec.filename
        tmp = dest.with_suffix(dest.suffix + ".part")
        with self._http_client_factory() as client:
            resp = client.get(spec.url, headers={"User-Agent": "lerobot-dashboard/0.1"})
            resp.raise_for_status()
            tmp.write_bytes(resp.content)
        tmp.replace(dest)
        logger.info("cached asset %s -> %s", spec.model, dest)

    def _write_sources_doc(self) -> None:
        path = self.robots_dir / "SOURCES.md"
        header = (
            "# Robot image sources\n\n"
            "This file is regenerated by "
            "`lerobot.dashboard.services.assets.AssetManager` on every startup.\n"
            "Attribution is preserved here so downstream license audits "
            "do not need to re-read the Python module.\n\n"
            "| Model | File | Source | License | URL |\n"
            "| --- | --- | --- | --- | --- |\n"
        )
        rows = [
            f"| `{spec.model}` | `{spec.filename}` | {spec.source} | {spec.license} | <{spec.url}> |"
            for spec in _CANONICAL_SPECS
        ]
        aliases = sorted(k for k, v in ROBOT_IMAGE_MAP.items() if k != v.model)
        alias_section = ""
        if aliases:
            alias_lines = "\n".join(f"- `{a}` → `{ROBOT_IMAGE_MAP[a].model}`" for a in aliases)
            alias_section = f"\n\n## Aliases\n\n{alias_lines}\n"
        unverified_section = ""
        if UNVERIFIED_MODELS:
            unverified = "\n".join(f"- `{m}`" for m in sorted(UNVERIFIED_MODELS))
            unverified_section = (
                "\n\n## Unverified models\n\n"
                "These models ship without a curated image and fall back "
                f"to `{GENERIC_FILENAME}`:\n\n{unverified}\n"
            )
        path.write_text(
            header + "\n".join(rows) + "\n" + alias_section + unverified_section,
            encoding="utf-8",
        )


__all__ = [
    "AssetManager",
    "AssetSpec",
    "ENV_OFFLINE",
    "GENERIC_FALLBACK_SVG",
    "GENERIC_FILENAME",
    "RESOURCE_URL_PREFIX",
    "ROBOTS_SUBDIR",
    "ROBOT_IMAGE_MAP",
    "UNVERIFIED_MODELS",
]
