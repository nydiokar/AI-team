"""U6 — machine-checkable enforcement of "one gateway, many equal interfaces".

docs/CONTROL_SURFACE_UNIFICATION.md §9 says the abstraction must be *enforced*, not
just *available*: no interface (Telegram, the HTTP Control API) may mutate session
lifecycle state directly — those writes go through ``SessionService``. The spec
defines the acceptance as "a grep proves it." This test IS that grep, run in CI so it
can't rot back into the per-interface drift that produced ``dashboard.py``.

Forbidden in interface modules:
  * ``session.status = SessionStatus.<X>``  → use mark_busy/mark_cancelled/close/restore
  * ``session.model = ...``                 → use SessionService.set_model
  * ``session.backend_session_id = ...``    → use SessionService.close_session

Allow-listed exception (documented in docs/U3_5_CHECKLIST.md P11): the BUSY status set
on the dispatch hot-path in src/telegram/interface.py. BUSY is a dispatch-time
transition set on the same Session object that is immediately saved with
``last_task_id`` (a single save); routing it through ``mark_busy`` would add a
redundant save+reload for no behavioral gain. The count is pinned so a NEW inline
mutation fails the test.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

INTERFACE_FILES = [
    REPO / "src" / "telegram" / "interface.py",
    REPO / "src" / "control" / "control_api.py",
]

# Direct lifecycle mutations no interface may perform (must go via SessionService).
_FORBIDDEN = [
    re.compile(r"\.status\s*=\s*SessionStatus\."),
    re.compile(r"\.model\s*=\s(?!=)"),           # `.model =` but not `==`
    re.compile(r"\.backend_session_id\s*=\s(?!=)"),
]

# P11 allow-list: BUSY dispatch sites in the Telegram send path. Pinned count.
_ALLOWED_BUSY = re.compile(r"\.status\s*=\s*SessionStatus\.BUSY")
_EXPECTED_BUSY_SITES = 4


def _lines(path: Path):
    return path.read_text(encoding="utf-8").splitlines()


def test_control_api_has_no_direct_lifecycle_mutation():
    """The HTTP interface must route 100% of lifecycle writes through the service."""
    offenders = []
    for i, line in enumerate(_lines(REPO / "src" / "control" / "control_api.py"), 1):
        if any(p.search(line) for p in _FORBIDDEN):
            offenders.append(f"control_api.py:{i}: {line.strip()}")
    assert not offenders, "Control API must not mutate session state directly:\n" + "\n".join(offenders)


def test_telegram_only_inline_mutation_is_the_pinned_busy_dispatch():
    """Telegram may keep ONLY the documented BUSY dispatch sites inline (P11).

    Any other direct lifecycle mutation, or a change in the BUSY-site count, fails —
    forcing the author to either route through SessionService or update the P11 doc +
    this pin deliberately.
    """
    busy_sites = 0
    offenders = []
    for i, line in enumerate(_lines(REPO / "src" / "telegram" / "interface.py"), 1):
        if _ALLOWED_BUSY.search(line):
            busy_sites += 1
            continue
        if any(p.search(line) for p in _FORBIDDEN):
            offenders.append(f"interface.py:{i}: {line.strip()}")
    assert not offenders, (
        "Telegram must route lifecycle writes through SessionService "
        "(only the P11 BUSY dispatch sites may stay inline):\n" + "\n".join(offenders)
    )
    assert busy_sites == _EXPECTED_BUSY_SITES, (
        f"Expected exactly {_EXPECTED_BUSY_SITES} inline BUSY dispatch sites (P11), "
        f"found {busy_sites}. A new inline status write must instead go through "
        f"SessionService.mark_busy — or, if truly a dispatch site, update P11 and "
        f"this pin deliberately."
    )
