"""Move A — backend registry consolidation (zero behavior change).

The registry is the single declaration site for the backend set. These tests
pin the invariant the consolidation must hold: the built {name: backend} map,
the valid-names tuple, and the default backend all agree, and every built value
is a real CodingBackend.
"""
from src.backends.registry import (
    DEFAULT_BACKEND,
    build_backends,
    is_valid_backend,
    valid_backend_names,
)
from src.core.interfaces import CodingBackend


def test_built_keys_match_valid_names():
    assert set(build_backends().keys()) == set(valid_backend_names())


def test_every_built_value_is_a_coding_backend():
    for name, backend in build_backends().items():
        assert isinstance(backend, CodingBackend), name


def test_default_backend_is_valid():
    assert DEFAULT_BACKEND in valid_backend_names()
    assert is_valid_backend(DEFAULT_BACKEND)


def test_is_valid_backend_normalizes_and_rejects():
    assert is_valid_backend("CLAUDE")
    assert is_valid_backend("  opencode-server  ")
    assert not is_valid_backend("nope")
    assert not is_valid_backend("")
    assert not is_valid_backend(None)  # type: ignore[arg-type]
