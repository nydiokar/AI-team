"""
Sliding-window mesh health detector.

Records health check outcomes and declares degradation only after
N consecutive failures, preventing false positives from transient
network blips.  Callers feed results via record_check(); the component
is fully passive — no background loop.

Module-level singleton::

    from src.control.mesh_health import get_mesh_health
    get_mesh_health().record_check(healthy=True)
    if get_mesh_health().is_degraded():
        ...  # fall back to local execution
"""

import logging
from collections import deque
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MeshHealth:
    """Tracks recent health check outcomes in a sliding window.

    Degradation is declared when consecutive failures reach
    *failure_threshold*.  Recovery is immediate on any success
    (optimistic — a single good probe clears the degraded flag).

    Parameters
    ----------
    window_size:
        Maximum number of recent outcomes to retain.
    failure_threshold:
        Number of *consecutive* failures required to declare degradation.

    Thread-safe for casual access (all mutations are plain attribute
    assignments on a ``deque``); use an explicit lock if calling from
    multiple threads simultaneously.
    """

    def __init__(self, window_size: int = 6, failure_threshold: int = 3) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if window_size < 1:
            raise ValueError("window_size must be >= 1")
        self._results: deque = deque(maxlen=window_size)
        self._consecutive_failures = 0
        self._degraded = False
        self._failure_threshold = failure_threshold

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_check(self, healthy: bool) -> None:
        """Feed a health check outcome into the sliding window."""
        self._results.append(healthy)
        if healthy:
            self._consecutive_failures = 0
            self._degraded = False
        else:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._failure_threshold:
                self._degraded = True

    def is_degraded(self) -> bool:
        """``True`` iff the mesh is currently considered degraded."""
        return self._degraded

    def is_healthy(self) -> bool:
        """``True`` iff the mesh is operating normally (not degraded).

        Equivalent to ``not is_degraded()``.
        """
        return not self._degraded

    def stats(self) -> Dict[str, Any]:
        """Diagnostic snapshot suitable for embedding in ``/health``."""
        recent_checks: List[Optional[bool]] = list(self._results)
        return {
            "degraded": self._degraded,
            "consecutive_failures": self._consecutive_failures,
            "window_size": self._results.maxlen,
            "failure_threshold": self._failure_threshold,
            "recent_checks": recent_checks,
        }

    def reset(self) -> None:
        """Clear the sliding window and reset to healthy."""
        self._results.clear()
        self._consecutive_failures = 0
        self._degraded = False


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_health: Optional[MeshHealth] = None


def get_mesh_health() -> MeshHealth:
    """Return the singleton *MeshHealth*, creating it on first call.

    Configuration is read from ``config.mesh`` at creation time so the
    singleton reflects the runtime config.  Safe to call repeatedly.
    """
    global _health
    if _health is None:
        try:
            from config import config as _cfg
            ws = int(getattr(_cfg.mesh, "mesh_health_window_size", 6) or 6)
            ft = int(getattr(_cfg.mesh, "mesh_health_failure_threshold", 3) or 3)
        except Exception:
            ws, ft = 6, 3
        _health = MeshHealth(window_size=ws, failure_threshold=ft)
    return _health
