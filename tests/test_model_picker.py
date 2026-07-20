"""
Unit tests for the per-backend model picker. All offline — no CLI invocations.

Covers (MODEL_PICKER_PLAN.md):
  - catalog defaults + validation policy (strict vs advisory)  [R5/R6/R10]
  - resolve_model precedence: session.model → config → catalog default
  - backend _build_cmd flag placement (--model / -m)            [R3]
  - Session ↔ store dict round-trip + DB round-trip carries model
  - mesh payload round-trips model (gateway → worker)           [R1]
"""
import json
import pytest

from config import models as models_module
from config.models import (
    BACKEND_MODELS, options, available_options, default_model, is_known, is_advisory,
    validate, resolve_model, effort_options, validate_effort,
)
from src.core.interfaces import Session, SessionStatus


def _mk(backend, model=None):
    return Session(
        session_id="a" * 12, backend=backend, repo_path=".",
        status=SessionStatus.IDLE, created_at="t", updated_at="t", model=model,
    )


# --------------------------------------------------------------------------- catalog
def test_every_backend_has_exactly_one_default():
    for backend in BACKEND_MODELS:
        defaults = [o for o in options(backend) if o.is_default]
        assert len(defaults) == 1, f"{backend} must have exactly one default"
        assert default_model(backend) == defaults[0].name


def test_opencode_cli_and_server_share_one_list():
    assert options("opencode") is options("opencode-server")


def test_validation_policy_strict_vs_advisory():
    # Claude rejects unknown names; Codex accepts newly published local models.
    assert validate("claude", "bogus") is None
    assert validate("codex", "gpt-5.6-luna") == "gpt-5.6-luna"
    assert not is_advisory("claude")
    assert is_advisory("codex")
    # advisory backend passes unknown through unchanged
    assert validate("opencode", "some/custom-model") == "some/custom-model"
    assert is_advisory("opencode")
    # known names pass through on any backend
    assert validate("claude", "opus") == "opus"
    assert is_known("codex", "gpt-5.5")
    # empty/None → None
    assert validate("claude", None) is None
    assert validate("claude", "   ") is None


def test_codex_picker_merges_machine_catalog_with_legacy_aliases(monkeypatch):
    monkeypatch.setattr(
        models_module,
        "_read_codex_model_list",
        lambda: [
            models_module.ModelOption("gpt-5.6-sol", is_default=True),
            models_module.ModelOption("gpt-5.6-terra"),
            models_module.ModelOption("gpt-5.6-luna"),
        ],
    )
    monkeypatch.setattr(models_module, "_CODEX_MODEL_CACHE", (0.0, []))

    names = [item.name for item in available_options("codex")]
    assert names[:3] == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
    assert "gpt-5.5" in names
    assert "gpt-5.2-codex" in names


def test_codex_picker_keeps_last_good_catalog_on_refresh_failure(monkeypatch):
    """A transient Codex app-server/auth failure must not make newer picker
    models disappear after the cache TTL expires."""
    reads = iter([
        [
            models_module.ModelOption("gpt-5.6-sol", is_default=True),
            models_module.ModelOption("gpt-5.6-terra"),
        ],
        [],
    ])
    monkeypatch.setattr(models_module, "_read_codex_model_list", lambda: next(reads))
    monkeypatch.setattr(models_module, "_CODEX_MODEL_CACHE", (0.0, []))

    first = [item.name for item in available_options("codex")]
    assert first[:2] == ["gpt-5.6-sol", "gpt-5.6-terra"]

    monkeypatch.setattr(models_module, "_CODEX_MODEL_CACHE", (0.0, list(models_module._CODEX_MODEL_CACHE[1])))
    second = [item.name for item in available_options("codex")]
    assert second[:2] == ["gpt-5.6-sol", "gpt-5.6-terra"]
    assert "gpt-5.5" in second


def test_effort_catalog_is_backend_specific_and_optional():
    assert effort_options("claude") == ["low", "medium", "high", "xhigh", "max"]
    assert "ultra" in effort_options("codex")
    assert validate_effort("codex", "HIGH") == "high"
    assert validate_effort("claude", "bogus") is None
    assert validate_effort("codex", None) is None


# --------------------------------------------------------------------------- resolve
def test_resolve_precedence_pinned_wins():
    assert resolve_model(_mk("claude", "opus")) == "opus"


def test_resolve_falls_back_to_catalog_default_when_unpinned():
    assert resolve_model(_mk("claude")) == default_model("claude")
    assert resolve_model(_mk("codex")) == default_model("codex")


def test_resolve_invalid_strict_model_falls_back_to_default():
    # a garbage stored model must never reach a strict CLI
    assert resolve_model(_mk("claude", "bogus")) == default_model("claude")


def test_resolve_advisory_passes_unknown_through():
    assert resolve_model(_mk("opencode", "ollama-local/qwen3-coder")) == "ollama-local/qwen3-coder"


# --------------------------------------------------------------------------- backend cmd
def test_claude_build_cmd_model_placement():
    # _build_cmd now lives only on ClaudePrintResumeDriver (single source of truth).
    from src.backends.claude_driver import ClaudePrintResumeDriver
    b = ClaudePrintResumeDriver()
    fresh = b._build_cmd(None, "sid", "opus")
    assert fresh[fresh.index("--model") + 1] == "opus"
    resume = b._build_cmd("rid", None, "sonnet")
    assert "--resume" in resume and resume[resume.index("--model") + 1] == "sonnet"
    assert "--model" not in b._build_cmd(None, "sid", None)


def test_codex_build_cmd_model_placement():
    from src.backends.codex import CodexBackend
    b = CodexBackend()
    # fresh: -m goes after `exec`
    fresh = b._build_cmd(None, "/repo", "gpt-5.5")
    assert fresh[1] == "exec" and fresh[2] == "-m" and fresh[3] == "gpt-5.5"
    # resume: -m goes after `resume <id>` (verified valid via --help)
    resume = b._build_cmd("thread1", None, "gpt-5.2-codex")
    assert resume[2] == "resume" and resume[3] == "thread1"
    assert resume[resume.index("-m") + 1] == "gpt-5.2-codex"
    assert "-m" not in b._build_cmd(None, "/repo", None)
    assert "model_reasoning_effort=\"high\"" in b._build_cmd(None, "/repo", None, "high")


def test_claude_build_cmd_effort_placement():
    from src.backends.claude_driver import ClaudePrintResumeDriver
    b = ClaudePrintResumeDriver()
    cmd = b._build_cmd(None, "sid", "opus", "high")
    assert cmd[cmd.index("--effort") + 1] == "high"


# --------------------------------------------------------------------------- persistence
def test_store_dict_round_trip_preserves_model():
    from src.services.session_store import SessionStore
    s = _mk("codex", "gpt-5.5")
    s.effort = "high"
    restored = SessionStore._from_dict(SessionStore._to_dict(s))
    assert restored.model == "gpt-5.5"
    assert restored.effort == "high"
    # None survives too
    assert SessionStore._from_dict(SessionStore._to_dict(_mk("claude"))).model is None


def test_db_round_trip_preserves_model(tmp_path):
    import src.control.db as dbmod
    db = dbmod.MeshDB(str(tmp_path / "mesh.db"))
    s = _mk("opencode", "opencode/mimo-v2.5-free")
    s.effort = "high"
    db.upsert_session(s)
    row = db.get_session(s.session_id)
    assert row["model"] == "opencode/mimo-v2.5-free"
    assert row["effort"] == "high"


# --------------------------------------------------------------------------- mesh payload (R1)
def test_mesh_payload_round_trips_model():
    """The gateway hand-builds payload['session'] and the worker hand-rebuilds it;
    both must carry `model` or remote workers silently run the default."""
    from src.worker.agent import _make_session_from_payload
    s = _mk("claude", "opus")
    s.machine_id = "worker-1"
    payload_session = {
        "session_id": s.session_id, "backend": s.backend, "repo_path": s.repo_path,
        "backend_session_id": s.backend_session_id, "model": s.model,
        "machine_id": s.machine_id, "telegram_chat_id": None,
        "telegram_thread_id": None, "owner_user_id": None, "last_user_message": "",
    }
    rebuilt = _make_session_from_payload({"session": payload_session})
    assert rebuilt.model == "opus"


# --------------------------------------------------------------------------- telegram markup (B4/B7/R7)
def _iter_callbacks(markup):
    for row in markup.inline_keyboard:
        for btn in row:
            if btn.callback_data:
                yield btn.callback_data


def test_model_set_callbacks_pin_session_id_and_fit_budget():
    """B4: each /model button must carry the session_id so a click can't be
    mis-applied to a different active session. R7: stay under 64 bytes."""
    from src.telegram.interface import TelegramInterface, TELEGRAM_AVAILABLE
    if not TELEGRAM_AVAILABLE:
        pytest.skip("python-telegram-bot not installed")
    iface = TelegramInterface.__new__(TelegramInterface)  # no bot needed for a pure builder
    s = _mk("opencode")  # longest model names → worst case
    markup = iface._build_model_set_markup(s)
    cbs = list(_iter_callbacks(markup))
    assert cbs, "expected at least the default button"
    for cb in cbs:
        assert cb.startswith(f"model_set:{s.session_id}:"), cb
        assert len(cb.encode()) <= 64, f"callback over 64 bytes: {cb!r} ({len(cb.encode())})"


def test_effective_model_label_strips_backticks():
    """B9: a free-text model name with backticks must not break the Markdown
    code span in the confirmation message."""
    from src.telegram.interface import TelegramInterface
    s = _mk("opencode", "weird`name`")
    label = TelegramInterface._effective_model_label(s)
    # exactly two backticks (the wrapping code span), none from the name
    assert label.count("`") == 2
