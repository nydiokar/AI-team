"""
Model catalog — the single source of truth for which models each backend offers.

A "model" here is just a name passed on a CLI flag:
  - Claude:          --model <name>   (aliases auto-track the latest version)
  - Codex:           -m <name>
  - OpenCode (both): --model provider/model   (server: body.model={providerID,modelID})

The catalog drives the Telegram picker buttons, the per-backend default, and
validation. Model strings live ONLY here — backends, config validation, and the
picker all read from this module.

Validation policy (see MODEL_PICKER_PLAN.md R5/R6):
  - claude: STRICT. Its model aliases are global and stable.
  - codex: ADVISORY. The installed Codex CLI advertises the machine's current
    model catalog, which can change independently of this gateway.
  - opencode / opencode-server: ADVISORY. Available models depend on the local
    opencode.json providers (which differ per worker node), so the gateway cannot
    know the full set. Unknown names are passed through with a warning, never
    silently rewritten — this is what makes free-text `/model <name>` usable.
"""
from __future__ import annotations

import logging
import json
import os
import select
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelOption:
    """One selectable model. `name` is both the button label value and the
    exact string passed to the backend's model flag."""
    name: str
    is_default: bool = False
    supported_efforts: Optional[tuple[str, ...]] = None


# Backends whose model set is global/stable → strict validation.
# Codex is intentionally advisory: its local app-server publishes the current
# account/machine catalog, which can change independently of this gateway.
_STRICT_BACKENDS = {"claude"}
# Backends whose model set is environment-specific → advisory validation.
_ADVISORY_BACKENDS = {"codex", "opencode", "opencode-server"}

_EFFORTS = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "codex": ("low", "medium", "high", "xhigh", "max", "ultra"),
}


def effort_options(backend: str) -> List[str]:
    """Return supported selectable thinking-effort values for a backend."""
    return list(_EFFORTS.get(backend, ()))


def validate_effort(backend: str, effort: Optional[str]) -> Optional[str]:
    """Validate a session effort; None means use the backend default."""
    if not effort:
        return None
    value = effort.strip().lower()
    return value if value in _EFFORTS.get(backend, ()) else None

# OpenCode CLI and server share one list.
_OPENCODE_MODELS: List[ModelOption] = [
    ModelOption("opencode/big-pickle", is_default=True),
    ModelOption("opencode/deepseek-v4-flash-free"),
    ModelOption("opencode/mimo-v2.5-free"),
    ModelOption("opencode/nemotron-3-ultra-free"),
    ModelOption("opencode/north-mini-code-free"),
]

BACKEND_MODELS: Dict[str, List[ModelOption]] = {
    "claude": [
        ModelOption("sonnet"),
        ModelOption("opus", is_default=True),
        ModelOption("haiku"),
        ModelOption("fable"),
    ],
    "codex": [
        ModelOption("gpt-5.5", is_default=True),
        ModelOption("gpt-5.2-codex"),
    ],
    "opencode": _OPENCODE_MODELS,
    "opencode-server": _OPENCODE_MODELS,
}


def options(backend: str) -> List[ModelOption]:
    """Return the catalog options for a backend (empty list if unknown)."""
    return BACKEND_MODELS.get(backend, [])


def _read_codex_model_list() -> List[ModelOption]:
    """Read the model catalog advertised by the installed Codex CLI.

    Codex's app-server is local and uses newline-delimited JSON. Discovery is
    best-effort: a missing CLI, an old CLI, or an unavailable auth service must
    not make the gateway's model picker unusable.
    """
    executable = shutil.which("codex")
    if not executable:
        return []

    timeout_seconds: float = 3.0
    try:
        timeout_seconds = max(0.5, float(os.getenv("CODEX_MODEL_DISCOVERY_TIMEOUT_SEC", "3")))
    except ValueError:
        pass

    messages: list[str] = [
        json.dumps({
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "ai-team", "title": "AI Team", "version": "1"}},
        }),
        json.dumps({"method": "initialized", "params": {}}),
        json.dumps({
            "id": 2,
            "method": "model/list",
            "params": {"includeHidden": False, "limit": 1000},
        }),
    ]
    process: Optional[subprocess.Popen[str]] = None
    try:
        process = subprocess.Popen(
            [executable, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        assert process.stdin is not None
        process.stdin.write("\n".join(messages) + "\n")
        process.stdin.flush()

        assert process.stdout is not None
        deadline: float = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            remaining: float = max(0.0, deadline - time.monotonic())
            readable, _, _ = select.select([process.stdout], [], [], remaining)
            if not readable:
                break
            line: str = process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != 2:
                continue
            result: dict[str, Any] = message.get("result") or {}
            discovered: list[ModelOption] = []
            for model in result.get("data", []):
                name = model.get("model") or model.get("id")
                if isinstance(name, str) and name.strip():
                    advertised_efforts = tuple(
                        item.get("reasoningEffort")
                        for item in model.get("supportedReasoningEfforts", [])
                        if isinstance(item, dict) and isinstance(item.get("reasoningEffort"), str)
                    )
                    discovered.append(ModelOption(
                        name.strip(),
                        is_default=bool(model.get("isDefault")),
                        supported_efforts=advertised_efforts or None,
                    ))
            return discovered
    except (OSError, ValueError, subprocess.SubprocessError):
        logger.debug("event=codex_model_discovery_failed", exc_info=True)
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    return []


_CODEX_MODEL_CACHE: tuple[float, List[ModelOption]] = (0.0, [])
_CODEX_MODEL_CACHE_LOCK = threading.Lock()


def available_options(backend: str) -> List[ModelOption]:
    """Return picker options, discovering Codex models from the local CLI.

    The static catalog remains as a compatibility fallback and is merged with
    the advertised list so existing aliases do not disappear during a CLI
    upgrade or downgrade.
    """
    if backend != "codex":
        return options(backend)
    global _CODEX_MODEL_CACHE
    now: float = time.monotonic()
    if now - _CODEX_MODEL_CACHE[0] >= 300:
        with _CODEX_MODEL_CACHE_LOCK:
            now = time.monotonic()
            if now - _CODEX_MODEL_CACHE[0] >= 300:
                discovered = _read_codex_model_list()
                merged: list[ModelOption] = list(discovered)
                known: set[str] = {item.name for item in merged}
                for item in options(backend):
                    if item.name not in known:
                        merged.append(item)
                _CODEX_MODEL_CACHE = (now, merged or list(options(backend)))
    return _CODEX_MODEL_CACHE[1]


def default_model(backend: str) -> Optional[str]:
    """Return the catalog default model name for a backend, or None."""
    for opt in options(backend):
        if opt.is_default:
            return opt.name
    opts = options(backend)
    return opts[0].name if opts else None


def effort_options_for_model(backend: str, model: Optional[str]) -> List[str]:
    """Return model-advertised efforts, falling back to backend capabilities."""
    if backend == "codex" and model:
        for option in available_options(backend):
            if option.name == model and option.supported_efforts:
                return list(option.supported_efforts)
    return effort_options(backend)


def is_known(backend: str, name: str) -> bool:
    """True if `name` is an exact catalog entry for `backend`."""
    return any(opt.name == name for opt in options(backend))


def is_advisory(backend: str) -> bool:
    """True if the backend uses advisory (pass-through) validation."""
    return backend in _ADVISORY_BACKENDS


def validate(backend: str, name: Optional[str]) -> Optional[str]:
    """Validate a stored/typed model name for a backend.

    Returns the name to actually use:
      - None in  → None out (means "use the backend default" downstream).
      - strict backend + unknown name → None (caller falls back to default), warns.
      - advisory backend + unknown name → the name unchanged (pass-through), warns.
      - known name → the name unchanged.
    """
    if not name:
        return None
    name = name.strip()
    if not name:
        return None
    if is_known(backend, name):
        return name
    if backend in _STRICT_BACKENDS:
        logger.warning(
            "event=model_unknown_rejected backend=%s model=%r — falling back to default",
            backend, name,
        )
        return None
    # Advisory backend (opencode*): trust the caller; the worker's provider set
    # is unknown to the gateway. Pass through with a warning.
    logger.warning(
        "event=model_unknown_passthrough backend=%s model=%r — not in catalog, using as-is",
        backend, name,
    )
    return name


def _config_default(backend: str) -> Optional[str]:
    """Read the gateway-wide default model for a backend from config, if set."""
    try:
        from config import config as _cfg
        if backend == "claude":
            return getattr(_cfg.claude, "default_model", None)
        if backend == "codex":
            return getattr(_cfg.codex, "default_model", None)
        if backend in ("opencode", "opencode-server"):
            return getattr(_cfg.opencode, "default_model", None)
    except Exception:
        pass
    return None


def resolved_default_model(backend: str) -> Optional[str]:
    """The model a model-LESS session actually resolves to for this backend.

    Precedence: config default → catalog default. This is exactly the default
    half of `resolve_model()` (i.e. what runs when `session.model is None`), so
    a read surface can render the *honest* default the driver will really use
    instead of a static catalog guess. The config value is passed through
    `validate()` so a stale/garbage default can't be shown/used.
    """
    cfg_default = validate(backend, _config_default(backend))
    if cfg_default:
        return cfg_default
    return default_model(backend)


def resolve_model(session: Any) -> Optional[str]:
    """Resolve the model a backend should actually use for this session.

    Precedence: session.model → config default → catalog default.
    Returns None only if no default is configured anywhere (caller should then
    omit the model flag and let the CLI pick its own default).

    The session.model and config values are passed through `validate()` so a
    stale/garbage stored model can't reach the CLI (strict backends fall back;
    advisory backends pass through). See MODEL_PICKER_PLAN.md R6/R10.
    """
    backend = getattr(session, "backend", "") or ""
    picked = validate(backend, getattr(session, "model", None))
    if picked:
        return picked
    return resolved_default_model(backend)
