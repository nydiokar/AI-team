"""The shared turn-liveness rule: progress keeps a turn alive; only a stall
(or the far-out hard cap) ends the wait."""

from src.core.turn_liveness import ProgressClock, TurnLimits, turn_limits


class _FakeTime:
    def __init__(self) -> None:
        self.t: float = 1000.0

    def __call__(self) -> float:
        return self.t


def test_long_turn_with_progress_never_expires_before_hard_cap():
    ft = _FakeTime()
    clock = ProgressClock(now=ft)
    limits = TurnLimits(stall_sec=10, hard_cap_sec=1000)
    for _ in range(50):  # 50 × 9 s = 450 s of work, far past the stall window
        ft.t += 9
        clock.touch()
        assert clock.expiry(limits) == ""


def test_no_progress_for_stall_window_expires_as_stalled():
    ft = _FakeTime()
    clock = ProgressClock(now=ft)
    limits = TurnLimits(stall_sec=10, hard_cap_sec=1000)
    ft.t += 9.9
    assert clock.expiry(limits) == ""
    ft.t += 0.1
    assert clock.expiry(limits) == "stalled"


def test_hard_cap_expires_even_with_progress():
    ft = _FakeTime()
    clock = ProgressClock(now=ft)
    limits = TurnLimits(stall_sec=10, hard_cap_sec=30)
    for _ in range(3):
        ft.t += 9
        clock.touch()
    assert clock.expiry(limits) == ""
    ft.t += 3
    clock.touch()
    assert clock.expiry(limits) == "hard_cap"


def test_remaining_is_the_nearer_of_stall_and_hard_cap():
    ft = _FakeTime()
    clock = ProgressClock(now=ft)
    assert clock.remaining(TurnLimits(stall_sec=10, hard_cap_sec=30)) == 10
    ft.t += 25
    clock.touch()
    assert clock.remaining(TurnLimits(stall_sec=10, hard_cap_sec=30)) == 5
    ft.t += 40
    assert clock.remaining(TurnLimits(stall_sec=10, hard_cap_sec=30)) == 0


def test_turn_limits_default_is_inactivity_window_with_4x_hard_cap(monkeypatch):
    from config import config as cfg

    monkeypatch.setattr(cfg.system, "inactivity_timeout_sec", 7200)
    monkeypatch.setattr(cfg.system, "task_timeout", 0)
    assert turn_limits() == TurnLimits(stall_sec=7200, hard_cap_sec=28800)
    monkeypatch.setattr(cfg.system, "task_timeout", 50000)
    assert turn_limits() == TurnLimits(stall_sec=7200, hard_cap_sec=50000)


def test_stall_override_scales_hard_cap(monkeypatch):
    from config import config as cfg

    monkeypatch.setattr(cfg.system, "task_timeout", 0)
    assert turn_limits(0.5) == TurnLimits(stall_sec=0.5, hard_cap_sec=2.0)
