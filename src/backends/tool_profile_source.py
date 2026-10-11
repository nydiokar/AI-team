"""Controller-authoritative tool-profile resolution (A108).

The static ``_PROFILE_TOOLS`` map in :mod:`src.backends.claude_role_adapter` is the
SEED and the fail-closed fallback. This module layers an OPTIONAL controller-served
OVERLAY on top of it so an operator can add/remove a tool (or remap a role) by
editing controller config — the overlay rides the existing runtime-flag registry
(``TOOL_PROFILES_DYNAMIC`` gate + ``TOOL_PROFILES_OVERLAY`` JSON value), is served to
workers by the A88 controller snapshot, and is read at SESSION BOOT. A new value
therefore reaches the next session with **no worker restart** (and, since the value
lives in the registry, no redeploy either).

Why here and not in the adapter: the adapter stays a pure, provider-neutral static
map (its CI pins guard it against MCP-server drift). The dynamic layer — which reads
controller state and can fail — lives in this one module. The Claude driver calls the
``*_resolved`` names exported here (same signatures as the adapter's static helpers),
so the grant logic is unchanged; only the data source gained an overlay.

Safety invariants (all fail-closed to the static map):
  * gate OFF (default) ⇒ static map, byte-identical to pre-A108;
  * overlay absent/empty ⇒ static map;
  * overlay malformed / oversized / unknown top key / bad tool name / role mapped to
    an unknown profile ⇒ the WHOLE overlay is rejected ⇒ static map;
  * an unknown/unmapped role always resolves to ``WORKER_TOOL_PROFILE`` (least
    privilege) — an overlay can never make "unknown role" inherit a privileged grant.

Scope: this is the Claude per-tool allowlist. Codex/OpenCode gate at the SERVER
level and already pick up a new tool on a configured server with no code change, so
they do not consume this overlay (documented in the A108 packet).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional

from src.backends.claude_role_adapter import static_profile_tools
from src.core.roles import (
    BASE_TOOL_PROFILE,
    ROLE_TOOL_PROFILES,
    WORKER_TOOL_PROFILE,
)

logger = logging.getLogger(__name__)

_GATE_FLAG = "TOOL_PROFILES_DYNAMIC"
_OVERLAY_FLAG = "TOOL_PROFILES_OVERLAY"

# Bounds — an overlay is operator config, kept small on purpose.
_MAX_OVERLAY_BYTES = 32_768
_MAX_PROFILES = 64
_MAX_TOOLS_PER_PROFILE = 256
_MAX_ROLE_MAPPINGS = 128
_MAX_NAME_CHARS = 64

# A tool name is either an MCP tool (mcp__<server>__<tool>) or a built-in (Read, Bash…).
_TOOL_NAME_RE = re.compile(r"^(mcp__[A-Za-z0-9_]+__[A-Za-z0-9_]+|[A-Za-z][A-Za-z0-9_]*)$")

# Dedup identical overlay-rejection warnings (boot is called per session).
_last_rejected: Optional[str] = None


def static_role_profiles() -> Dict[str, str]:
    """A copy of the static role→profile map (the seed + fallback)."""
    return dict(ROLE_TOOL_PROFILES)


def _dynamic_enabled() -> bool:
    try:
        from src.control.db import runtime_flag_enabled
        return runtime_flag_enabled(_GATE_FLAG)
    except Exception as exc:  # noqa: BLE001 — never let a flag read break a session boot
        logger.warning("event=tool_profiles_gate_read_failed err=%s", exc)
        return False


def _raw_overlay() -> str:
    try:
        from src.control.db import runtime_flag_value
        return (runtime_flag_value(_OVERLAY_FLAG) or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("event=tool_profiles_overlay_read_failed err=%s", exc)
        return ""


def _reject(raw: str, reason: str) -> None:
    global _last_rejected
    if _last_rejected != raw:  # log once per distinct bad value
        logger.warning("event=tool_profiles_overlay_rejected reason=%s", reason)
        _last_rejected = raw


def _valid_name(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= _MAX_NAME_CHARS


def _parse_overlay(raw: str) -> Optional[Dict[str, object]]:
    """Parse + validate the overlay; return ``{"profiles", "role_profiles"}`` or None.

    ANY problem returns None (the caller then uses the static map). Validation is
    strict on purpose — a typo must fail closed, not silently grant an empty set."""
    if not raw:
        return None
    if len(raw.encode("utf-8", "ignore")) > _MAX_OVERLAY_BYTES:
        _reject(raw, "oversized")
        return None
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        _reject(raw, "invalid_json")
        return None
    if not isinstance(doc, dict):
        _reject(raw, "not_an_object")
        return None
    if set(doc) - {"profiles", "role_profiles"}:
        _reject(raw, "unknown_top_key")
        return None

    profiles_in = doc.get("profiles", {})
    roles_in = doc.get("role_profiles", {})
    if not isinstance(profiles_in, dict) or not isinstance(roles_in, dict):
        _reject(raw, "profiles_or_roles_not_object")
        return None
    if len(profiles_in) > _MAX_PROFILES or len(roles_in) > _MAX_ROLE_MAPPINGS:
        _reject(raw, "too_many_entries")
        return None

    profiles: Dict[str, List[str]] = {}
    for name, tools in profiles_in.items():
        if not _valid_name(name):
            _reject(raw, "bad_profile_name")
            return None
        if not isinstance(tools, list) or len(tools) > _MAX_TOOLS_PER_PROFILE:
            _reject(raw, "bad_tool_list")
            return None
        clean: List[str] = []
        for t in tools:
            if not isinstance(t, str) or not _TOOL_NAME_RE.match(t):
                _reject(raw, "bad_tool_name")
                return None
            clean.append(t)
        profiles[name] = clean

    # Every role mapping must point at a profile that exists in static ∪ overlay —
    # a dangling reference is an operator error and fails the whole overlay closed.
    known_profiles = set(static_profile_tools()) | set(profiles) | {BASE_TOOL_PROFILE}
    role_profiles: Dict[str, str] = {}
    for role, profile in roles_in.items():
        if not _valid_name(role) or not _valid_name(profile):
            _reject(raw, "bad_role_mapping")
            return None
        if profile not in known_profiles:
            _reject(raw, "role_maps_to_unknown_profile")
            return None
        role_profiles[role] = profile

    return {"profiles": profiles, "role_profiles": role_profiles}


def _overlay() -> Optional[Dict[str, object]]:
    """The validated overlay when the gate is ON and the value is valid, else None."""
    if not _dynamic_enabled():
        return None
    return _parse_overlay(_raw_overlay())


# --------------------------------------------------------------------------- #
# Resolved views — the driver consumes these (same signatures as the adapter's
# static helpers, so the grant logic is unchanged).
# --------------------------------------------------------------------------- #
def resolved_profile_tools() -> Dict[str, List[str]]:
    """Static profile→tools map with any valid overlay profiles layered over it
    (per-profile replacement). Static when the gate is off / overlay invalid."""
    merged = static_profile_tools()
    overlay = _overlay()
    if overlay:
        merged.update(overlay["profiles"])  # type: ignore[arg-type]
    return merged


def resolved_role_profiles() -> Dict[str, str]:
    """Static role→profile map with any valid overlay mappings layered over it."""
    merged = static_role_profiles()
    overlay = _overlay()
    if overlay:
        merged.update(overlay["role_profiles"])  # type: ignore[arg-type]
    return merged


def profile_tool_names(profile: str) -> List[str]:
    """Resolved tool names for a profile (overlay-aware). Unknown ⇒ [] (least priv)."""
    return list(resolved_profile_tools().get(profile, []))


def base_tool_names() -> List[str]:
    """Resolved universal (base_v1) tool names layered under every role."""
    return profile_tool_names(BASE_TOOL_PROFILE)


def tool_profile_for_role(role_id: Optional[str]) -> str:
    """Resolved (overlay-aware) role→profile; unknown/unmapped ⇒ WORKER_TOOL_PROFILE.

    The least-privilege default is enforced HERE (not read from the overlay), so an
    overlay can add/remap roles but can never make an unmapped role privileged."""
    return resolved_role_profiles().get(role_id or "", WORKER_TOOL_PROFILE)
