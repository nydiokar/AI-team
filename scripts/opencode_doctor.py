#!/usr/bin/env python3
"""
opencode_doctor — diagnose and recover OpenCode auth/model problems.

Background
----------
The gateway's most common opaque failure is a Telegram message like:

    ❌ Task failed: HTTP 500 from opencode server
       (POST /session/.../message): Unexpected server error.

This *can* be caused by missing credentials, but auth.json is a known red
herring: opencode also authenticates via a cached session in opencode.db, and
LOCAL models (Ollama / a localhost baseURL, e.g. opencode/big-pickle backed by
http://localhost:11434) need no credentials at all. An empty auth.json with a
local model is perfectly healthy.

Because of that, this doctor's source of truth is a real probe: it actually
runs `opencode run` with the configured model and checks the model answers.
auth.json is reported only as supplementary context, never as a hard failure on
its own.

Usage
-----
    python scripts/opencode_doctor.py            # diagnose only
    python scripts/opencode_doctor.py --fix      # diagnose, then run `opencode auth login`
    python scripts/opencode_doctor.py --restart  # also restart the gateway via pm2 after a fix

Exit codes: 0 = healthy, 1 = problem found (and not fixed), 2 = could not run.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Windows consoles default to cp1252 and choke on any non-ASCII output.
# Force UTF-8 with a safe fallback so the doctor never crashes on its own print().
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _auth_paths():
    home = Path.home()
    paths = [
        home / ".local" / "share" / "opencode" / "auth.json",
        home / ".config" / "opencode" / "auth.json",
        home / ".opencode" / "auth.json",
    ]
    xdg = os.getenv("XDG_DATA_HOME")
    if xdg:
        paths.insert(0, Path(xdg) / "opencode" / "auth.json")
    return paths


def _env_keys():
    return [
        v for v in ("OPENCODE_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
        if os.getenv(v)
    ]


def _find_auth_file():
    for p in _auth_paths():
        if p.is_file():
            try:
                raw = p.read_text(encoding="utf-8").strip()
            except Exception:
                return p, "unreadable"
            if not raw or raw in ("{}", "[]", "null"):
                return p, "empty"
            try:
                data = json.loads(raw)
            except Exception:
                return p, "corrupt"
            n = len(data) if isinstance(data, (dict, list)) else 0
            return p, f"{n} credential(s)"
    return None, "missing"


def _opencode_exe():
    return shutil.which("opencode") or "opencode"


def _configured_model():
    """Read the model the gateway will actually use (config + env override)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from config import config as cfg
        return getattr(getattr(cfg, "opencode", None), "default_model", None)
    except Exception:
        return os.getenv("OPENCODE_DEFAULT_MODEL") or "opencode/big-pickle (default)"


def _is_local_model(model: str) -> bool:
    """True if the configured model resolves to a local provider needing no creds.

    Detection: the project/global opencode.json defines a provider whose options
    baseURL points at localhost/127.0.0.1, or the model is opencode's free hosted
    'big-pickle'/'zen' tier (cost 0, no key). We check the opencode config files
    for a localhost baseURL on the model's provider.
    """
    if not model:
        return False
    provider = model.split("/", 1)[0].strip() if "/" in model else model.strip()
    # Scan opencode config files for a localhost baseURL under this provider.
    cfg_files = [
        Path.cwd() / "opencode.json",
        Path.home() / ".config" / "opencode" / "opencode.json",
        Path.home() / ".config" / "opencode" / "opencode.jsonc",
    ]
    for cf in cfg_files:
        try:
            if not cf.is_file():
                continue
            raw = cf.read_text(encoding="utf-8")
            # tolerate // comments in .jsonc
            raw = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("//"))
            data = json.loads(raw)
        except Exception:
            continue
        prov_cfg = (data.get("provider") or {}).get(provider) or {}
        base = str((prov_cfg.get("options") or {}).get("baseURL") or "")
        if "localhost" in base or "127.0.0.1" in base:
            return True
    return False


def _probe_model(model: str, timeout: int = 90):
    """Actually run the model. Returns (ok: bool, detail: str). Source of truth."""
    exe = _opencode_exe()
    cwd = str(Path.cwd())
    cmd = [exe, "run", "--dir", cwd, "--format", "json"]
    if model:
        cmd += ["--model", model]
    cmd.append("Reply with exactly: DOCTOR_OK and nothing else.")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"model did not respond within {timeout}s"
    except FileNotFoundError:
        return False, f"opencode executable not found ({exe})"
    out = (proc.stdout or "") + (proc.stderr or "")
    if "DOCTOR_OK" in out:
        return True, "model answered the probe"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        return False, f"exit={proc.returncode}: {tail or 'no output'}"
    # exit 0 but no expected token — model responded but oddly; still usable.
    if '"type":"text"' in out or '"reason":"stop"' in out:
        return True, "model responded (probe token not echoed verbatim)"
    return False, "model produced no usable output"


def diagnose():
    print("=== opencode doctor ===\n")

    model = _configured_model()
    print(f"[info] configured model: {model}")

    local = _is_local_model(model)
    keys = _env_keys()
    path, status = _find_auth_file()

    # Context only — never a hard failure on its own.
    if local:
        print("[info] model is LOCAL (localhost baseURL) — no credentials required.")
    elif keys:
        print(f"[ok]   provider API key(s) in env: {', '.join(keys)}")
    else:
        print(f"[info] auth.json: {status} (also note: opencode can auth via cached "
              "session in opencode.db, so this is not authoritative)")

    # Cheap structural check: malformed model string.
    if isinstance(model, str) and "/" in model:
        prov, _, mod = model.partition("/")
        if not (prov.strip() and mod.strip()):
            print(f"[FAIL] model string '{model}' is malformed (empty half).")
            print("\nRESULT: opencode is NOT usable. [PROBLEM]")
            return False

    # Source of truth: actually run the model.
    print("[info] probing model with a live `opencode run` ...")
    ok, detail = _probe_model(model)
    if ok:
        print(f"[ok]   probe: {detail}")
        print("\nRESULT: opencode is healthy and the model answers. [OK]")
        return True

    print(f"[FAIL] probe failed: {detail}")
    if local:
        print("       Local model — check that the backend (e.g. `ollama serve`) is "
              "running and the model name exists (`ollama list`).")
    else:
        print("       Cloud model — check credentials: python scripts/opencode_doctor.py --fix")
    print("\nRESULT: opencode is NOT usable. [PROBLEM]")
    return False


def fix():
    print("\n=== running `opencode auth login` (interactive) ===\n")
    exe = _opencode_exe()
    try:
        rc = subprocess.call([exe, "auth", "login"])
    except FileNotFoundError:
        print(f"[FAIL] opencode executable not found ({exe}).")
        return 2
    if rc != 0:
        print(f"[FAIL] `opencode auth login` exited {rc}.")
        return 1
    print("\n=== verifying ===")
    ok = diagnose()
    return 0 if ok else 1


def restart_gateway():
    print("\n=== restarting gateway (pm2 restart ai-team-gateway) ===")
    pm2 = shutil.which("pm2") or "pm2"
    try:
        subprocess.call([pm2, "restart", "ai-team-gateway"])
    except FileNotFoundError:
        print("[warn] pm2 not found — restart the gateway manually.")


def main():
    ap = argparse.ArgumentParser(description="Diagnose/recover OpenCode auth.")
    ap.add_argument("--fix", action="store_true", help="run `opencode auth login` if unhealthy")
    ap.add_argument("--restart", action="store_true", help="restart the gateway via pm2 after a fix")
    args = ap.parse_args()

    healthy = diagnose()
    if healthy:
        return 0

    if not args.fix:
        return 1

    rc = fix()
    if rc == 0 and args.restart:
        restart_gateway()
    return rc


if __name__ == "__main__":
    sys.exit(main())
