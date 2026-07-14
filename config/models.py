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
  - claude / codex: STRICT. Their model sets are global and stable, so an
    unknown name is almost certainly a typo → reject (caller falls back to default).
  - opencode / opencode-server: ADVISORY. Available models depend on the local
    opencode.json providers (which differ per worker node), so the gateway cannot
    know the full set. Unknown names are passed through with a warning, never
    silently rewritten — this is what makes free-text `/model <name>` usable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelOption:
    """One selectable model. `name` is both the button label value and the
    exact string passed to the backend's model flag."""
    name: str
    is_default: bool = False


# Backends whose model set is global/stable → strict validation.
_STRICT_BACKENDS = {"claude", "codex"}
# Backends whose model set is environment-specific → advisory validation.
_ADVISORY_BACKENDS = {"opencode", "opencode-server"}

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


def default_model(backend: str) -> Optional[str]:
    """Return the catalog default model name for a backend, or None."""
    for opt in options(backend):
        if opt.is_default:
            return opt.name
    opts = options(backend)
    return opts[0].name if opts else None


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
