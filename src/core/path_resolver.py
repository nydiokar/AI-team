"""
Path resolution and suggestion helpers for bounded repo/session execution.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path
from typing import List, Optional

from config import config


@dataclass
class PathResolution:
    ok: bool
    input_path: str
    resolved_path: Optional[str] = None
    error: str = ""
    suggestions: List[str] = field(default_factory=list)
    available_dirs: List[str] = field(default_factory=list)


class PathResolver:
    """Resolves user-supplied paths against configured safe roots."""

    def __init__(
        self,
        base_cwd: Optional[str] = None,
        allowed_root: Optional[str] = None,
    ) -> None:
        self.base_cwd = self._resolve_existing_dir(base_cwd)
        self.allowed_root = self._resolve_existing_dir(allowed_root)

    @classmethod
    def from_config(cls) -> "PathResolver":
        return cls(
            base_cwd=getattr(config.claude, "base_cwd", None),
            allowed_root=getattr(config.claude, "allowed_root", None),
        )

    def resolve_session_path(self, raw_path: str) -> PathResolution:
        raw_path = (raw_path or "").strip().strip("\"'")
        if not raw_path:
            return PathResolution(
                ok=False,
                input_path=raw_path,
                error="Path is required.",
                suggestions=self.list_root_directories(),
            )

        candidate = self._coerce_candidate(raw_path)
        if candidate is None:
            return PathResolution(
                ok=False,
                input_path=raw_path,
                error="No base working directory is configured.",
                suggestions=self.list_root_directories(),
            )

        candidate = candidate.expanduser()
        try:
            resolved = candidate.resolve(strict=False)
        except Exception:
            resolved = candidate

        if not self._is_within_allowed_root(resolved):
            scope = str(self.allowed_root) if self.allowed_root else "configured workspace"
            return PathResolution(
                ok=False,
                input_path=raw_path,
                error=f"Path is outside the allowed root: {scope}",
                suggestions=self.list_root_directories(),
            )

        if not resolved.exists():
            return PathResolution(
                ok=False,
                input_path=raw_path,
                error="Path does not exist.",
                suggestions=self._suggest_missing_path(resolved),
                available_dirs=self._available_dirs_for_parent(resolved.parent),
            )

        if not resolved.is_dir():
            return PathResolution(
                ok=False,
                input_path=raw_path,
                error="Path must point to a directory.",
                suggestions=[str(resolved.parent)],
                available_dirs=self.list_child_directories(resolved.parent),
            )

        return PathResolution(
            ok=True,
            input_path=raw_path,
            resolved_path=str(resolved),
            available_dirs=self.list_child_directories(resolved),
        )

    def resolve_execution_path(self, raw_path: Optional[str]) -> Optional[str]:
        raw_path = (raw_path or "").strip()
        if not raw_path:
            return str(self.base_cwd) if self.base_cwd else None

        result = self.resolve_session_path(raw_path)
        if result.ok:
            return result.resolved_path
        return str(self.base_cwd) if self.base_cwd else None

    def list_root_directories(self, limit: int = 12) -> List[str]:
        root = self.allowed_root or self.base_cwd
        if root is None:
            return []
        return self.list_child_directories(root, limit=limit)

    def list_child_directories(self, raw_path: Path | str, limit: int = 12) -> List[str]:
        try:
            path = raw_path if isinstance(raw_path, Path) else Path(raw_path)
            path = path.resolve()
            if not path.exists() or not path.is_dir():
                return []
            items = sorted(
                [child.name for child in path.iterdir() if child.is_dir()],
                key=str.lower,
            )
            return items[:limit]
        except Exception:
            return []

    def _coerce_candidate(self, raw_path: str) -> Optional[Path]:
        user_path = Path(raw_path)
        if user_path.is_absolute():
            return user_path

        base = self.base_cwd or self.allowed_root
        if base is None:
            return None

        normalized = raw_path
        if normalized.startswith(("/", "\\")) and len(normalized) > 1:
            normalized = normalized[1:]
        return base / normalized

    def _is_within_allowed_root(self, path: Path) -> bool:
        if self.allowed_root is None:
            return True
        try:
            return path == self.allowed_root or self.allowed_root in path.parents
        except Exception:
            return False

    def _suggest_missing_path(self, missing: Path, limit: int = 8) -> List[str]:
        suggestions: List[str] = []
        parent = missing.parent
        target_name = missing.name

        direct = self._available_dirs_for_parent(parent)
        if direct:
            close = get_close_matches(target_name, [Path(p).name for p in direct], n=limit, cutoff=0.3)
            for match in close:
                for item in direct:
                    if Path(item).name == match and item not in suggestions:
                        suggestions.append(item)

        root = self.allowed_root or self.base_cwd
        if root and len(suggestions) < limit:
            try:
                all_dirs = [p for p in root.rglob("*") if p.is_dir()]
                close = get_close_matches(target_name, [p.name for p in all_dirs], n=limit, cutoff=0.45)
                for match in close:
                    for item in all_dirs:
                        if item.name == match:
                            text = str(item.resolve())
                            if text not in suggestions:
                                suggestions.append(text)
                            if len(suggestions) >= limit:
                                return suggestions
            except Exception:
                return suggestions

        return suggestions[:limit]

    def _available_dirs_for_parent(self, parent: Path) -> List[str]:
        try:
            if not parent.exists() or not parent.is_dir():
                return []
            return [str(p.resolve()) for p in sorted(parent.iterdir(), key=lambda item: item.name.lower()) if p.is_dir()]
        except Exception:
            return []

    @staticmethod
    def _resolve_existing_dir(raw_path: Optional[str]) -> Optional[Path]:
        if not raw_path:
            return None
        try:
            path = Path(raw_path).expanduser().resolve()
            if path.exists() and path.is_dir():
                return path
        except Exception:
            return None
        return None
