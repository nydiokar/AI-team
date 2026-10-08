"""The ONE rule for how long a running agent turn may be awaited.

A turn is given up on only when it stops making progress — no native event
(output, reasoning, tool start/finish, …) for ``stall_sec`` — never merely
because it is long. ``hard_cap_sec`` is a far-out absolute safety net for a
backend that dribbles progress forever.

Every backend's wait loop (OpenCode / Codex / Claude SDK managed turns, the
managed lost-carrier reaper) derives its limits from :func:`turn_limits` and
feeds a :class:`ProgressClock` from its native event stream, so a long but
working turn is never treated as lost. Mirrors the Claude CLI driver's rule
(rolling inactivity window + ``task_timeout`` or 4× that window as hard cap).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_STALL_SEC: int = 36000
HARD_CAP_MULTIPLIER: int = 4

TurnExpiry = Literal["", "stalled", "hard_cap"]


class TurnLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    stall_sec: float
    hard_cap_sec: float


def turn_limits(stall_override: float | None = None) -> TurnLimits:
    """Limits from ``system.inactivity_timeout_sec`` (env
    ``GATEWAY_INACTIVITY_TIMEOUT_SEC``) and ``system.task_timeout`` (env
    ``GATEWAY_TASK_TIMEOUT_SEC``; 0 ⇒ ``HARD_CAP_MULTIPLIER`` × stall).
    ``stall_override`` (a backend test hook) replaces the stall window and,
    when no explicit task_timeout is set, scales the hard cap with it."""
    stall: float = float(DEFAULT_STALL_SEC)
    task_timeout: int = 0
    try:
        from config import config as _cfg

        stall = float(max(60, int(getattr(_cfg.system, "inactivity_timeout_sec", DEFAULT_STALL_SEC))))
        task_timeout = int(getattr(_cfg.system, "task_timeout", 0) or 0)
    except (ImportError, AttributeError, TypeError, ValueError):
        pass  # no/invalid config ⇒ the defaults above
    if stall_override is not None:
        stall = max(0.0, float(stall_override))
    hard_cap: float = float(task_timeout) if task_timeout > 0 else stall * HARD_CAP_MULTIPLIER
    return TurnLimits(stall_sec=stall, hard_cap_sec=max(stall, hard_cap))


class ProgressClock:
    """Start + last-progress marker of one awaited turn. ``touch()`` may be
    called from any thread (event readers); the waiter polls ``expiry()``."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now: Callable[[], float] = now
        self._lock: threading.Lock = threading.Lock()
        self.started_at: float = now()
        self.last_progress_at: float = self.started_at

    def touch(self) -> None:
        with self._lock:
            self.last_progress_at = self._now()

    def observe(self, at: float) -> None:
        """Adopt an externally recorded progress time (same monotonic clock)."""
        with self._lock:
            self.last_progress_at = max(self.last_progress_at, at)

    def expiry(self, limits: TurnLimits) -> TurnExpiry:
        """``""`` while the turn is alive, else why it is given up on."""
        now: float = self._now()
        with self._lock:
            last: float = self.last_progress_at
        if now - self.started_at >= limits.hard_cap_sec:
            return "hard_cap"
        if now - last >= limits.stall_sec:
            return "stalled"
        return ""

    def remaining(self, limits: TurnLimits) -> float:
        """Seconds until :meth:`expiry` could next fire (≥ 0) — a wait bound."""
        now: float = self._now()
        with self._lock:
            last: float = self.last_progress_at
        return max(0.0, min(self.started_at + limits.hard_cap_sec, last + limits.stall_sec) - now)


class TurnControl(BaseModel):
    """What the carrier hands a backend for ONE agent turn — identity + liveness.

    The backend contract (A102): tag the prompt with ``turn_uuid`` (so its reply
    is attributable), call :meth:`touch` on EVERY native event of the turn, and
    give up (typed ``RecoveryRequiredError``, never an interrupt) only when
    :meth:`expired` says so. Backends never read timeout config themselves: the
    policy (``limits``) is decided once, by the carrier, from :func:`turn_limits`.
    ``ownership`` is the carrier's ``turn_queue.ManagedTurnOwnership`` (fencing);
    ``on_process`` receives the backend process identity before submit."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    turn_uuid: str
    limits: TurnLimits
    ownership: Any = None
    on_process: Callable[[dict], Any] | None = None
    progress: ProgressClock = Field(default_factory=ProgressClock)

    def touch(self) -> None:
        self.progress.touch()

    def expired(self) -> TurnExpiry:
        return self.progress.expiry(self.limits)

    def remaining(self) -> float:
        return self.progress.remaining(self.limits)


def turn_control(turn_uuid: str, *, ownership: Any = None,
                 on_process: Callable[[dict], Any] | None = None,
                 stall_override: float | None = None) -> TurnControl:
    """A :class:`TurnControl` whose clock starts now, limits from :func:`turn_limits`."""
    return TurnControl(turn_uuid=turn_uuid, limits=turn_limits(stall_override),
                       ownership=ownership, on_process=on_process)
