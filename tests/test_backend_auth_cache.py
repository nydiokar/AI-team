from concurrent.futures import ThreadPoolExecutor
from threading import Event

from src.control.backend_auth_cache import BackendAuthCache


def test_cache_ttl_refresh_and_verification(monkeypatch) -> None:
    clock: list[float] = [0]
    monkeypatch.setattr("src.control.backend_auth_cache.time.monotonic", lambda: clock[0])
    cache = BackendAuthCache()
    calls: list[str] = []

    def load() -> dict[str, object]:
        calls.append("load")
        return {"state": "signed_in"}

    first = cache.read("node:claude", load)
    assert first["checked_at"]
    assert first["login_verified_at"] is None  # Status cannot prove a login date.
    assert cache.read("node:claude", load) == first
    assert len(calls) == 1
    cache.read("node:claude", load, refresh=True)
    assert len(calls) == 2
    clock[0] = 301
    cache.read("node:claude", load)
    assert len(calls) == 3
    verified = cache.verified("node:claude", True)
    assert verified["login_verified_at"]
    assert cache.read("node:claude", load) == verified
    assert len(calls) == 3
    cache.invalidate("node:claude")
    cache.read("node:claude", load)
    assert len(calls) == 4


def test_unavailable_expires_early_and_cache_is_bounded(monkeypatch) -> None:
    clock: list[float] = [0]
    monkeypatch.setattr("src.control.backend_auth_cache.time.monotonic", lambda: clock[0])
    cache = BackendAuthCache()
    calls: list[str] = []

    def load() -> dict[str, object]:
        calls.append("load")
        return {"state": "unavailable"}

    cache.read("node:codex", load)
    clock[0] = 29
    cache.read("node:codex", load)
    assert len(calls) == 1
    clock[0] = 31
    cache.read("node:codex", load)
    assert len(calls) == 2
    for i in range(300):
        cache.read(str(i), load)
    assert len(cache._entries) == 256


def test_simultaneous_status_checks_share_one_worker_call() -> None:
    cache = BackendAuthCache()
    entered = Event()
    release = Event()
    calls: list[str] = []

    def load() -> dict[str, object]:
        calls.append("load")
        entered.set()
        assert release.wait(2)
        return {"state": "signed_out"}

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(cache.read, "node:claude", load) for _ in range(8)]
        assert entered.wait(2)
        release.set()
        results = [future.result(timeout=3) for future in futures]
    assert len(calls) == 1
    assert all(result == results[0] for result in results)
