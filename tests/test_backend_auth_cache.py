from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

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


def test_unreachable_node_never_blocks_a_different_node_with_a_hash_collision() -> None:
    cache = BackendAuthCache()
    blocked = "offline-node"
    healthy = next(str(i) for i in range(100) if hash(str(i)) % 8 == hash(blocked) % 8)
    entered = Event()
    release = Event()
    healthy_done = Event()

    def slow() -> dict[str, object]:
        entered.set()
        assert release.wait(3)
        return {"state": "unavailable"}

    def fast() -> dict[str, object]:
        healthy_done.set()
        return {"state": "signed_out"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        offline = pool.submit(cache.read, blocked, slow)
        try:
            assert entered.wait(1)
            online = pool.submit(cache.read, healthy, fast)
            assert healthy_done.wait(1), "a different node must not wait for the offline node"
            assert online.result(timeout=1)["state"] == "signed_out"
        finally:
            release.set()
        assert offline.result(timeout=1)["state"] == "unavailable"


def test_cached_observation_does_not_wait_for_an_inflight_forced_refresh() -> None:
    cache = BackendAuthCache()
    known = cache.read("node", lambda: {"state": "signed_out"})
    entered = Event()
    release = Event()

    def refresh() -> dict[str, object]:
        entered.set()
        assert release.wait(3)
        return {"state": "signed_in"}

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(cache.read, "node", refresh, refresh=True)
        try:
            assert entered.wait(1)
            assert cache.read("node", lambda: {"state": "unavailable"}) == known
        finally:
            release.set()
        assert future.result(timeout=1)["state"] == "signed_in"


def test_per_key_locks_are_released_after_success_and_failure() -> None:
    cache = BackendAuthCache()
    def failed() -> dict[str, object]:
        raise OSError("offline")
    with pytest.raises(OSError):
        cache.read("node", failed)
    assert not cache._key_locks
    for i in range(300):
        cache.read(str(i), lambda: {"state": "signed_out"})
    assert not cache._key_locks
    assert len(cache._entries) == 256
