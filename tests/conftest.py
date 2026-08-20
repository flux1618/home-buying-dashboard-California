"""Test harness for the source layer.

Two rules the suite enforces on itself:

  1. **No test touches the network.** A suite that fails because a government server is
     having a bad afternoon reports nothing about your code. `no_network` is autouse, so
     forgetting to stub is a loud failure, not a slow real request.
  2. **Fixtures are recorded, never invented.** Every response under
     `tests/fixtures/responses/` came off the live API via `tools/record_fixtures.py`.
     Hand-written fixtures test what you *assumed* the API returns, which is precisely
     the assumption that breaks in production.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from analyzer.sources import http

from support import RESPONSES, load_response  # noqa: F401


@pytest.fixture
def responses() -> dict[str, dict]:
    """Every recorded response, keyed by fixture name."""
    return {path.stem: json.loads(path.read_text()) for path in RESPONSES.glob("*.json")}


class FakeHTTP:
    """Routes requests by substring, records what was asked, fails loudly on surprises."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, object]] = []
        self.calls: list[str] = []
        self.headers: list[dict] = []

    def route(self, url_contains: str, payload: object) -> FakeHTTP:
        """Map a URL fragment to a payload, or to an exception instance to raise."""
        self.routes.append((url_contains, payload))
        return self

    def __call__(self, url: str, **kwargs) -> http.Response:
        self.calls.append(url)
        self.headers.append(kwargs.get("headers") or {})
        for fragment, payload in self.routes:
            if fragment in url:
                if isinstance(payload, BaseException):
                    raise payload
                return http.Response(url=url, data=payload, from_cache=False)
        raise AssertionError(
            f"unstubbed request to {url}\nstubbed fragments: {[f for f, _ in self.routes]}"
        )

    def called_with(self, fragment: str) -> bool:
        return any(fragment in call for call in self.calls)


@pytest.fixture
def fake_http(monkeypatch) -> FakeHTTP:
    fake = FakeHTTP()
    monkeypatch.setattr(http, "get_json", fake)
    return fake


@pytest.fixture(autouse=True)
def no_network(monkeypatch, request):
    """Block sockets that could leave the machine. Opt out with @pytest.mark.live.

    The guard is on address *family*, not a blanket ban, because a blanket ban is wrong
    in a way that took a while to diagnose. FastAPI's TestClient runs an asyncio event
    loop, and asyncio builds an internal AF_UNIX `socketpair` to wake itself up. That
    pair never leaves the process, let alone the machine, so blocking it does not
    prevent a single real request — it just makes every web test fail with a message
    about the internet that has nothing to do with the internet.

    AF_INET, AF_INET6, and anything else are still blocked, which is the property that
    actually matters: no test can quietly hit a county server and pass because someone
    else's data happened to be online today.
    """
    if request.node.get_closest_marker("live"):
        return

    import socket

    LOCAL_ONLY = {getattr(socket, "AF_UNIX", None), getattr(socket, "AF_UNSPEC", None)}
    real_socket = socket.socket

    def guarded_socket(family=socket.AF_INET, *args, **kwargs):
        if family in LOCAL_ONLY:
            return real_socket(family, *args, **kwargs)
        raise AssertionError(
            f"a test tried to open a real network socket (family={family!r}). Use the "
            f"fake_http fixture, or mark the test @pytest.mark.live if it genuinely "
            f"needs the internet."
        )

    def blocked(*args, **kwargs):
        raise AssertionError(
            "a test tried to open a real socket. Use the fake_http fixture, or mark the "
            "test @pytest.mark.live if it genuinely needs the internet."
        )

    monkeypatch.setattr(socket, "socket", guarded_socket)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture(autouse=True)
def no_disk_cache(monkeypatch, tmp_path):
    """Point the response cache at a temp dir so tests never read the developer's cache."""
    monkeypatch.setattr(http, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture(autouse=True)
def no_ambient_api_keys(monkeypatch):
    """A developer's real FCC key must not change what the suite asserts."""
    monkeypatch.delenv("FCC_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def no_real_ledger(monkeypatch, tmp_path):
    """Point the ledger at a throwaway directory for every test in the suite.

    Autouse and unconditional, because the failure this prevents is not a flaky test --
    it is the suite writing rows into the real saved-property database on the developer's
    machine, or reading houses out of it and asserting against whatever happens to be
    saved that week. Both are silent.

    `XDG_DATA_HOME` is cleared as well as `HBA_DATA_DIR`, since that is the second place
    `ledger.db.database_path` looks. Overriding only the first would leave a machine that
    sets XDG_DATA_HOME still exposed.
    """
    monkeypatch.setenv("HBA_DATA_DIR", str(tmp_path / "ledger"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
