"""Multi-backend NDJSON extraction — text + token usage.

Guards the regression that shipped raw JSON-lines into the Web UI conversation
(codex/opencode) and the per-backend usage normalization. Each backend emits a
DIFFERENT event stream; these fixtures mirror the real on-disk shapes."""
import pytest

from src.services.result_text import (
    extract_text_from_payload,
    extract_usage_from_ndjson,
)

CODEX = "\n".join([
    '{"type":"thread.started","thread_id":"abc"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"Ready to work."}}',
    '{"type":"turn.completed","usage":{"input_tokens":9891,"cached_input_tokens":9600,"output_tokens":13,"reasoning_output_tokens":0}}',
])

CLAUDE = "\n".join([
    '{"type":"system","subtype":"init","session_id":"s1"}',
    '{"type":"assistant","message":{"content":[{"type":"text","text":"Working on it."}]}}',
    '{"type":"result","subtype":"success","result":"All done — 3 files changed.","usage":{"input_tokens":1200,"output_tokens":340,"cache_read_input_tokens":900,"cache_creation_input_tokens":100}}',
])

OPENCODE = "\n".join([
    '{"type":"step_start","part":{"id":"p1"}}',
    '{"type":"text","part":{"type":"text","text":"Works fine. What are you working on?"}}',
    '{"type":"step_finish","part":{"tokens":{"total":8195,"input":8169,"output":10,"reasoning":16,"cache":{"write":0,"read":5}}}}',
])


def test_codex_text():
    assert extract_text_from_payload(CODEX) == "Ready to work."


def test_claude_prefers_result_over_assistant_chunks():
    # The terminal `result` string wins over intermediate assistant content.
    assert extract_text_from_payload(CLAUDE) == "All done — 3 files changed."


def test_opencode_text():
    assert extract_text_from_payload(OPENCODE) == "Works fine. What are you working on?"


def test_codex_usage():
    u = extract_usage_from_ndjson(CODEX)
    assert u == {
        "input_tokens": 9891,
        "cached_input_tokens": 9600,
        "output_tokens": 13,
        "reasoning_output_tokens": 0,
    }


def test_claude_usage_sums_cache():
    u = extract_usage_from_ndjson(CLAUDE)
    assert u["input_tokens"] == 1200
    assert u["output_tokens"] == 340
    assert u["cached_input_tokens"] == 1000  # 900 read + 100 creation


def test_opencode_usage():
    u = extract_usage_from_ndjson(OPENCODE)
    assert u["input_tokens"] == 8169
    assert u["output_tokens"] == 10
    assert u["reasoning_output_tokens"] == 16
    assert u["cached_input_tokens"] == 5  # read + write


def test_zero_usage_returns_none():
    # An all-zero turn (e.g. cached claude resume) shows no badge.
    blob = '{"type":"result","result":"hi","usage":{"input_tokens":0,"output_tokens":0}}'
    assert extract_usage_from_ndjson(blob) is None


def test_clean_single_line_is_not_treated_as_ndjson():
    assert extract_text_from_payload("Done. Refactored 3 files.") == "Done. Refactored 3 files."


def test_plain_multiline_prose_passthrough():
    # Two non-JSON lines must NOT be swallowed by the NDJSON path.
    assert extract_text_from_payload("line one\nline two") == "line one\nline two"


def test_json_object_string_still_parsed():
    assert extract_text_from_payload('{"result":"hi there"}') == "hi there"


@pytest.mark.parametrize("blob", ["", "   ", "not json at all"])
def test_garbage_does_not_crash(blob):
    # Never raises; returns something string-ish or None for usage.
    extract_text_from_payload(blob)
    assert extract_usage_from_ndjson(blob) is None
