#!/usr/bin/env python3
"""
Tests for LLAMA context/size management caps and truncation behavior.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import logging
import re

from src.bridges.llama_mediator import LlamaMediator
from config import config


def test_parse_truncation_logs(monkeypatch, caplog):
    lm = LlamaMediator()
    # Force a small cap via monkeypatch to exercise truncation deterministically
    monkeypatch.setattr(config.llama, "max_parse_chars", 1000, raising=False)

    # Create oversized content
    content = "A" * 1500

    caplog.set_level(logging.INFO)
    _ = lm.parse_task(f"---\n type: analyze\n---\n# T\n\n{content}")

    # Expect a truncate log
    logs = "\n".join([rec.getMessage() for rec in caplog.records])
    assert "event=truncate_parse" in logs
    assert "after_chars=1000" in logs


def test_prompt_truncation_logs(monkeypatch, caplog):
    lm = LlamaMediator()
    monkeypatch.setattr(config.llama, "max_prompt_chars", 500, raising=False)

    parsed = {
        "type": "analyze",
        "title": "T",
        "target_files": [],
        "main_request": "X" * 2000,
        "priority": "medium",
    }

    caplog.set_level(logging.INFO)
    prompt = lm._create_prompt_with_template(parsed)
    assert len(prompt) <= 500
    logs = "\n".join([rec.getMessage() for rec in caplog.records])
    assert "event=truncate_prompt" in logs
    assert "after_chars=500" in logs


