"""Robot image asset manager tests.

The manager is exercised without touching the real network — a fake
HTTP client records requests and serves canned bytes. The FastAPI
``/resources/robots/*`` mount is covered against the ``TestClient`` so
the frontend contract (StaticFiles + generic fallback URL) doesn't
regress.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lerobot.dashboard.app import create_app  # noqa: E402
from lerobot.dashboard.core.config import DashboardConfig  # noqa: E402
from lerobot.dashboard.services import assets as assets_module  # noqa: E402
from lerobot.dashboard.services.assets import (  # noqa: E402
    GENERIC_FILENAME,
    RESOURCE_URL_PREFIX,
    ROBOT_IMAGE_MAP,
    UNVERIFIED_MODELS,
    AssetManager,
)


# ---------------------------------------------------------------------------
# Fake HTTP client
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, content: bytes, status: int = 200) -> None:
        self.content = content
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")


class _FakeClient:
    def __init__(self, payloads: dict[str, bytes], failures: set[str] | None = None) -> None:
        self._payloads = payloads
        self._failures = failures or set()
        self.calls: list[str] = []

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> _FakeResponse:
        self.calls.append(url)
        if url in self._failures:
            return _FakeResponse(b"", status=500)
        return _FakeResponse(self._payloads.get(url, b"\x89PNG\r\n\x1a\nfake"))


def _make_factory(client: _FakeClient):
    @contextmanager
    def _factory():
        yield client

    return _factory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"


@pytest.fixture
def fake_payloads() -> dict[str, bytes]:
    return {spec.url: f"bytes-for-{spec.model}".encode() for spec in ROBOT_IMAGE_MAP.values()}


# ---------------------------------------------------------------------------
# AssetManager — unit tests
# ---------------------------------------------------------------------------


def test_init_creates_robots_dir_and_generic_icon(cache_dir: Path) -> None:
    mgr = AssetManager(cache_dir, offline=True)
    assert mgr.robots_dir.is_dir()
    assert (mgr.robots_dir / GENERIC_FILENAME).is_file()
    body = (mgr.robots_dir / GENERIC_FILENAME).read_bytes()
    assert body.startswith(b"<?xml")
    assert b"svg" in body


def test_resolve_url_unknown_model_returns_generic(cache_dir: Path) -> None:
    mgr = AssetManager(cache_dir, offline=True)
    assert mgr.resolve_url("definitely_not_a_robot") == mgr.generic_url
    assert mgr.generic_url == f"{RESOURCE_URL_PREFIX}/{GENERIC_FILENAME}"


def test_resolve_url_downloads_known_model_once(
    cache_dir: Path, fake_payloads: dict[str, bytes]
) -> None:
    client = _FakeClient(fake_payloads)
    mgr = AssetManager(cache_dir, offline=False, http_client_factory=_make_factory(client))

    url1 = mgr.resolve_url("so101_follower")
    url2 = mgr.resolve_url("so101_follower")

    assert url1 == url2 == f"{RESOURCE_URL_PREFIX}/so101_follower.webp"
    assert (mgr.robots_dir / "so101_follower.webp").read_bytes() == b"bytes-for-so101_follower"
    # Second call is a cache hit, not a network round trip.
    assert len(client.calls) == 1


def test_resolve_url_alias_resolves_to_canonical_file(
    cache_dir: Path, fake_payloads: dict[str, bytes]
) -> None:
    client = _FakeClient(fake_payloads)
    mgr = AssetManager(cache_dir, offline=False, http_client_factory=_make_factory(client))

    assert mgr.resolve_url("so-101") == f"{RESOURCE_URL_PREFIX}/so101_follower.webp"
    assert mgr.resolve_url("so100") == f"{RESOURCE_URL_PREFIX}/so100_follower.jpg"


def test_resolve_url_offline_never_calls_http(cache_dir: Path) -> None:
    client = _FakeClient({})
    mgr = AssetManager(cache_dir, offline=True, http_client_factory=_make_factory(client))
    assert mgr.resolve_url("so101_follower") == mgr.generic_url
    assert client.calls == []


def test_resolve_url_falls_back_on_http_error(
    cache_dir: Path, fake_payloads: dict[str, bytes]
) -> None:
    failing = {ROBOT_IMAGE_MAP["so101_follower"].url}
    client = _FakeClient(fake_payloads, failures=failing)
    mgr = AssetManager(cache_dir, offline=False, http_client_factory=_make_factory(client))

    assert mgr.resolve_url("so101_follower") == mgr.generic_url
    # Partial file should have been cleaned up (no `.part` left behind).
    leftovers = list(mgr.robots_dir.glob("so101_follower*"))
    assert leftovers == []


def test_prefetch_all_downloads_every_canonical_asset(
    cache_dir: Path, fake_payloads: dict[str, bytes]
) -> None:
    client = _FakeClient(fake_payloads)
    mgr = AssetManager(cache_dir, offline=False, http_client_factory=_make_factory(client))

    results = mgr.prefetch_all()

    canonical_models = {spec.model for spec in ROBOT_IMAGE_MAP.values()}
    assert set(results) == canonical_models
    assert all(results.values())
    for model in canonical_models:
        spec = next(s for s in ROBOT_IMAGE_MAP.values() if s.model == model)
        assert (mgr.robots_dir / spec.filename).is_file()

    # Each canonical asset is fetched exactly once (no duplicate downloads for aliases).
    assert len(client.calls) == len(canonical_models)


def test_prefetch_offline_skips_downloads(cache_dir: Path) -> None:
    client = _FakeClient({})
    mgr = AssetManager(cache_dir, offline=True, http_client_factory=_make_factory(client))
    results = mgr.prefetch_all()
    assert client.calls == []
    assert all(v is False for v in results.values())


def test_prefetch_writes_sources_doc(cache_dir: Path, fake_payloads: dict[str, bytes]) -> None:
    client = _FakeClient(fake_payloads)
    mgr = AssetManager(cache_dir, offline=False, http_client_factory=_make_factory(client))
    mgr.prefetch_all()

    sources = (mgr.robots_dir / "SOURCES.md").read_text(encoding="utf-8")
    assert "# Robot image sources" in sources
    assert "CC BY-SA 4.0" in sources
    for spec in {s.model: s for s in ROBOT_IMAGE_MAP.values()}.values():
        assert spec.url in sources
        assert spec.filename in sources
    for model in UNVERIFIED_MODELS:
        assert f"`{model}`" in sources


def test_prefetch_tolerates_partial_failure(
    cache_dir: Path, fake_payloads: dict[str, bytes]
) -> None:
    failing_url = ROBOT_IMAGE_MAP["unitree_g1"].url
    client = _FakeClient(fake_payloads, failures={failing_url})
    mgr = AssetManager(cache_dir, offline=False, http_client_factory=_make_factory(client))

    results = mgr.prefetch_all()
    assert results["unitree_g1"] is False
    assert results["so101_follower"] is True
    assert not (mgr.robots_dir / "unitree_g1.jpg").exists()


def test_offline_flag_defaults_from_env(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(assets_module.ENV_OFFLINE, "1")
    mgr = AssetManager(cache_dir)
    assert mgr.offline is True
    monkeypatch.setenv(assets_module.ENV_OFFLINE, "no")
    mgr2 = AssetManager(cache_dir)
    assert mgr2.offline is False


# ---------------------------------------------------------------------------
# FastAPI integration
# ---------------------------------------------------------------------------


@pytest.fixture
def app_client(tmp_path: Path) -> TestClient:
    config = DashboardConfig(storage_dir=tmp_path / "storage", fake_devices=True)
    return TestClient(create_app(config))


def test_generic_fallback_served_by_resources_mount(app_client: TestClient) -> None:
    resp = app_client.get(f"{RESOURCE_URL_PREFIX}/{GENERIC_FILENAME}")
    assert resp.status_code == 200
    assert resp.content.startswith(b"<?xml")
    ctype = resp.headers["content-type"]
    assert "svg" in ctype or "xml" in ctype


def test_unknown_asset_returns_404(app_client: TestClient) -> None:
    resp = app_client.get(f"{RESOURCE_URL_PREFIX}/missing-robot.png")
    assert resp.status_code == 404


def test_asset_manager_attached_to_app_state(app_client: TestClient) -> None:
    state = app_client.app.state.dashboard
    assert isinstance(state.assets, AssetManager)
    assert state.assets.robots_dir.is_dir()
