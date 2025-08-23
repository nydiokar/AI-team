import types

from src.validation.engine import ValidationEngine, _shannon_entropy
from src.core.interfaces import TaskType
from src.core.interfaces import ValidationResult, TaskResult


def test_entropy_guard_short_outputs_do_not_overflag():
    engine = ValidationEngine()
    # Extremely low-entropy and very short
    output = "AAAAAA"
    res: ValidationResult = engine.validate_llama_output("irrelevant", output, TaskType.SUMMARIZE)
    # Should not mark as low_entropy solely due to short length guarding
    assert not any(i.startswith("low_entropy") for i in res.issues)


def test_trigram_jaccard_fallback_for_short_text_similarity():
    engine = ValidationEngine()
    # Force fallback path
    engine._model = None
    a = "Add login form to header"
    b = "Add a login form in the site header"  # semantically similar, many shared 3-grams
    res = engine.validate_llama_output(a, b, TaskType.ANALYZE)
    # Expect similarity above a weak threshold (0.3) with trigram Jaccard
    assert res.similarity >= 0.3


def test_readonly_tasks_flag_edit_language():
    engine = ValidationEngine()
    # Temporarily lower similarity threshold for this test
    engine.config.similarity_threshold = 0.1
    # Use more similar input/output to avoid similarity threshold issues
    input_text = "Summarize the authentication code"
    # Use exact text that matches the validation engine's edit markers
    output_text = "We applied patch and modified:app/main.py to fix authentication"
    res = engine.validate_llama_output(input_text, output_text, TaskType.SUMMARIZE)
    assert "unexpected_edit_language_in_readonly_task" in res.issues


def test_cross_check_expected_files_vs_modified():
    engine = ValidationEngine()
    result = TaskResult(
        task_id="test_task",
        success=True, 
        output="Updated files:", 
        files_modified=["foo.txt"], 
        errors=[],
        execution_time=1.0,
        timestamp="2025-08-23T15:00:00"
    )
    res = engine.validate_task_result(result, expected_files=["bar.txt"])  # different from modified
    assert "modified_files_outside_expected" in res.issues


