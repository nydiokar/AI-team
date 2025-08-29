"""
Validation engine MVP: lightweight similarity, entropy, and structure checks.
Falls back to built-ins when optional ML deps are unavailable.

Planned refinements (keep minimal and configurable):
- Add character 3-gram Jaccard as a stronger similarity fallback for short texts
- Guard entropy for very short outputs (length-aware thresholding)
- Make structure-keyword lists configurable per TaskType via ValidationConfig
"""
from __future__ import annotations

import math
from typing import List

try:
    # Optional dependency; we gracefully fall back if unavailable
    from sentence_transformers import SentenceTransformer
    import numpy as np  # type: ignore
    _ST_AVAILABLE = True
except Exception:
    _ST_AVAILABLE = False

from src.core.interfaces import (
    IValidationEngine,
    ValidationResult,
    TaskResult,
    TaskType,
)
from config.settings import ValidationConfig


def _cosine_similarity(a, b) -> float:
    if not _ST_AVAILABLE:
        return 0.0
    denom = (float((a ** 2).sum()) ** 0.5) * (float((b ** 2).sum()) ** 0.5)
    if denom == 0:
        return 0.0
    return float((a @ b) / denom)


def _shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    from collections import Counter
    counts = Counter(text)
    length = len(text)
    entropy = 0.0
    for _, c in counts.items():
        p = c / length
        entropy -= p * math.log2(p)
    # Normalize by max possible entropy for observed alphabet size
    max_entropy = math.log2(len(counts)) if counts else 1.0
    if max_entropy == 0:
        return 0.0
    return entropy / max_entropy


def _jaccard_trigram_similarity(a: str, b: str) -> float:
    a = (a or "").lower()
    b = (b or "").lower()
    if len(a) < 3 or len(b) < 3:
        return 0.0
    grams_a = {a[i:i+3] for i in range(len(a) - 2)}
    grams_b = {b[i:i+3] for i in range(len(b) - 2)}
    if not grams_a or not grams_b:
        return 0.0
    inter = len(grams_a & grams_b)
    union = len(grams_a | grams_b)
    return inter / union if union else 0.0

class ValidationEngine(IValidationEngine):
    """Basic validation engine with optional sentence-transformers support."""

    def __init__(self, config: ValidationConfig | None = None):
        self.config = config or ValidationConfig()
        self._model = None
        if _ST_AVAILABLE:
            try:
                # Use a small, commonly available model name; if missing, we skip silently
                self._model = SentenceTransformer("all-MiniLM-L6-v2")
            except Exception:
                self._model = None

    def _similarity(self, a: str, b: str) -> float:
        a = (a or "").strip()
        b = (b or "").strip()
        if not a or not b:
            return 0.0
        if self._model is not None:
            try:
                emb = self._model.encode([a, b])  # type: ignore[attr-defined]
                return _cosine_similarity(emb[0], emb[1])
            except Exception:
                pass
        # Fallback 1: character trigram Jaccard (better for short phrases)
        tri = _jaccard_trigram_similarity(a, b)
        if tri > 0:
            return tri
        # Fallback 2: word Jaccard
        set_a = set(a.lower().split())
        set_b = set(b.lower().split())
        if not set_a or not set_b:
            return 0.0
        inter = len(set_a & set_b)
        union = len(set_a | set_b)
        return inter / union if union else 0.0

    def validate_llama_output(self, input_text: str, output: str, task_type: TaskType) -> ValidationResult:
        similarity = self._similarity(input_text, output)
        entropy = _shannon_entropy(output)
        issues: List[str] = []

        # Get agent-specific thresholds if available
        from src.core.agent_manager import AgentManager
        agent_manager = AgentManager()
        agent = agent_manager.get_agent_for_task_type(task_type)
        
        if agent:
            thresholds = agent.get_validation_thresholds()
            similarity_threshold = thresholds.get("similarity", self.config.similarity_threshold)
            entropy_threshold = thresholds.get("entropy", self.config.entropy_threshold)
        else:
            similarity_threshold = self.config.similarity_threshold
            entropy_threshold = self.config.entropy_threshold

        if similarity < similarity_threshold:
            issues.append(f"low_similarity:{similarity:.2f}")
        # Length-aware entropy: avoid flagging very short outputs solely for entropy
        if len(output or "") >= 20 and entropy < entropy_threshold:
            issues.append(f"low_entropy:{entropy:.2f}")

        # Very light structure hints: summary/review should be mostly read-only commentary
        if task_type in (TaskType.SUMMARIZE, TaskType.CODE_REVIEW):
            if any(k in output.lower() for k in ["apply patch", "edited:", "modified:"]):
                issues.append("unexpected_edit_language_in_readonly_task")

        valid = len(issues) == 0
        return ValidationResult(valid=valid, similarity=similarity, entropy=entropy, issues=issues)

    def validate_task_result(self, result: TaskResult, expected_files: List[str], task_type: TaskType | None = None) -> ValidationResult:
        # Use output entropy as a weak signal of degenerate replies
        entropy = _shannon_entropy(result.output or "")

        # Basic consistency checks
        issues: List[str] = []
        if result.success is False and not result.errors:
            issues.append("failed_without_errors")

        # Only meaningful for fix/analyze tasks; summarize/code_review are read-only by design
        if result.success and expected_files and (task_type in (TaskType.FIX, TaskType.ANALYZE) if task_type is not None else False):
            if not result.files_modified:
                issues.append("no_files_modified_but_success")

        # Detect claims of modifications without evidence of files_modified
        text = (result.output or "").lower()
        claims_edit_markers = [
            "modified:", "edited:", "updated:", "created:",
            "apply patch", "applied patch", "wrote", "changes saved",
        ]
        if not result.files_modified and any(marker in text for marker in claims_edit_markers):
            issues.append("claims_modifications_without_evidence")

        # Cross-check modified vs expected files allowlist
        if expected_files and result.files_modified:
            unexpected = [p for p in result.files_modified if p not in expected_files]
            if unexpected:
                issues.append("modified_files_outside_expected")

        valid = len(issues) == 0
        # We do not compute similarity here; keep it 0.0 for the structure check
        return ValidationResult(valid=valid, similarity=0.0, entropy=entropy, issues=issues)


