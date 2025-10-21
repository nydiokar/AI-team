Plan. Generate a repo “skeleton” on every PR/merge via CI, then (optionally) enrich with an LLM. Minimal, language-agnostic baseline: nodes = files, edges = imports. Works across projects.

Files to add
tools/agent_index.py

Python script: walks repo, extracts imports for JS/TS/Py, emits agent_index.json.

#!/usr/bin/env python3
import re, sys, json, os, hashlib, time
ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")
INCLUDE_DIRS = {"src", "app", "lib", "services", "packages", "backend", "frontend", "tests"}
EXCLUDE_DIRS = {".git", ".github", ".venv", "node_modules", "dist", "build", "__pycache__", ".next", ".turbo"}

IMP_JS = re.compile(r'^\s*import\s+(?:[^"\']+from\s+)?["\']([^"\']+)["\']|^\s*require\(\s*["\']([^"\']+)["\']\s*\)', re.M)
IMP_PY = re.compile(r'^\s*(?:from\s+([.\w/]+)\s+import|import\s+([.\w/]+))', re.M)

def relpath(p): return os.path.relpath(p, ROOT).replace("\\","/")
def sha(s): return hashlib.sha1(s.encode()).hexdigest()[:12]

def is_included(path):
    parts = relpath(path).split("/")
    if any(p in EXCLUDE_DIRS for p in parts): return False
    return any(parts[0].startswith(d) for d in INCLUDE_DIRS) or True  # fallback include

def lang_of(path):
    ext = os.path.splitext(path)[1].lower()
    return {" .py":"py",".ts":"ts",".tsx":"tsx",".js":"js",".jsx":"jsx"}.get(ext, "other")

def read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f: return f.read()
    except Exception: return ""

def norm_import(src_path, imp):
    # Only keep relative/workspace imports; drop http(s), pkgs
    if not imp: return None
    if imp.startswith(("http://","https://")): return None
    if imp.startswith("."):
        base = os.path.dirname(src_path)
        target = os.path.normpath(os.path.join(base, imp))
        # add common suffix resolution
        for suf in ("", ".ts", ".tsx", ".js", ".jsx", ".py", "/index.ts", "/index.js"):
            cand = target + suf
            if os.path.exists(cand):
                return relpath(cand)
        return relpath(target)
    # treat workspace-style aliases (@, src/) as internal
    if imp.startswith(("@","src/","~/")):
        return imp
    return None  # external package

files = []
edges = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
    if not is_included(dirpath): continue
    for fn in filenames:
        p = os.path.join(dirpath, fn)
        if not is_included(p): continue
        l = lang_of(p)
        if l == "other": continue
        rp = relpath(p)
        files.append({"id": f"file:{rp}", "path": rp, "lang": l})
        text = read(p)
        if l in {"js","jsx","ts","tsx"}:
            for m in IMP_JS.finditer(text):
                imp = m.group(1) or m.group(2)
                tgt = norm_import(rp, imp)
                if tgt: edges.append({"type":"imports","from": f"file:{rp}","to": f"file:{tgt}"})
        elif l == "py":
            for m in IMP_PY.finditer(text):
                imp = (m.group(1) or m.group(2) or "").replace(".", "/")
                tgt = norm_import(rp, "./"+imp)
                if tgt: edges.append({"type":"imports","from": f"file:{rp}","to": f"file:{tgt}"})

files.sort(key=lambda x: x["path"])
edges = [e for e in edges if e["from"] != e["to"]]
edges.sort(key=lambda x: (x["from"], x["to"], x["type"]))

out = {
  "version":"1",
  "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
  "root": os.path.basename(ROOT),
  "nodes": files,
  "edges": edges,
  "hash": sha("".join(f["path"] for f in files) + "".join(e["from"]+e["to"] for e in edges))
}
print(json.dumps(out, indent=2, ensure_ascii=False))

agent_index.json

Generated artifact at repo root. Do not hand-edit. Add to repo.

tools/update_agent_index.sh

Wrapper to write the JSON deterministically.

#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "$DIR/agent_index.py" > "$DIR/../agent_index.json"
# normalize newline endings
dos2unix "$DIR/../agent_index.json" 2>/dev/null || true


Make both scripts executable.

Git pre-commit (optional)

.pre-commit-config.yaml:

repos:
- repo: local
  hooks:
    - id: agent-index
      name: agent index refresh
      entry: tools/update_agent_index.sh
      language: system
      files: ^(src/|packages/|app/|lib/|services/|backend/|frontend/|tests/).*

GitHub Actions CI

.github/workflows/agent-index.yml:

name: agent-index
on:
  pull_request:
  push:
    branches: [ main ]
jobs:
  build-index:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: actions/setup-python@v5
        with: { python-version: '3.x' }
      - name: Refresh agent_index.json
        run: |
          bash tools/update_agent_index.sh
      - name: Fail if drift (PRs)
        if: github.event_name == 'pull_request'
        run: |
          if ! git diff --exit-code -- agent_index.json; then
            echo "::error::agent_index.json drift; run tools/update_agent_index.sh and commit"
            exit 1
          fi
      - name: Auto-commit updated index (on main pushes)
        if: github.event_name == 'push' && github.ref == 'refs/heads/main'
        run: |
          if ! git diff --quiet -- agent_index.json; then
            git config user.email "bot@local"; git config user.name "Agent Index Bot"
            git add agent_index.json
            git commit -m "chore: refresh agent_index.json"
            git push
          fi

Usage

CI regenerates agent_index.json on every PR/push.

If PR changes code and the index drifts, CI fails with instructions.

On main pushes, CI can auto-commit the refreshed file.

LLM enrichment (separate step)

Your autodoc/LLM job reads agent_index.json and writes agent_index.md (summaries, notes).

Run it manually or as a nightly job; do not block merges on it.

Notes

This is Level-1 automation: files + imports only. It’s robust, cross-project, and cheap.

You can later swap the regex extraction with tree-sitter or babel/ast parsing without changing CI or the JSON schema.

Agents across all projects now have a stable, always-fresh “index” at a fixed path.