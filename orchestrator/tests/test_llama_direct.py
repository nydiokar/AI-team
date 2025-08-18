#!/usr/bin/env python3
"""
Direct test of LLAMA functionality
"""
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from src.bridges.llama_mediator import LlamaMediator

def test_llama_parsing():
    """Test LLAMA task parsing"""
    print("Testing LLAMA 3.2 Task Parsing")
    print("===============================")
    
    mediator = LlamaMediator()
    
    # Check status
    status = mediator.get_status()
    print(f"LLAMA Status: {status}")
    print()
    
    if not status["ollama_available"]:
        print("LLAMA not available, exiting")
        return
    
    # Test task content
    task_content = '''---
id: test_task
type: analyze
priority: high
created: 2025-08-03T16:40:00Z
---

# Task: Test LLAMA Integration

**Target Files:**
- ./src/orchestrator.py
- ./config/settings.py

**Prompt:**
Analyze the codebase architecture and provide recommendations for improvements.

**Success Criteria:**
- [ ] Architecture analysis complete
- [ ] Recommendations provided

**Context:**
Testing LLAMA 3.2 with 128k context window.
'''
    
    print("Parsing task with LLAMA...")
    try:
        parsed = mediator.parse_task(task_content)
        print("SUCCESS! LLAMA parsed task:")
        print(f"  Type: {parsed.get('type')}")
        print(f"  Title: {parsed.get('title')}")
        print(f"  Files: {parsed.get('target_files')}")
        print(f"  Priority: {parsed.get('priority')}")
        print(f"  Request: {parsed.get('main_request', '')[:100]}...")
        print()
        
        print("Creating Claude prompt with LLAMA...")
        claude_prompt = mediator.create_claude_prompt(parsed)
        print("SUCCESS! LLAMA created prompt:")
        print(f"Prompt length: {len(claude_prompt)} characters")
        print("Preview:")
        print(claude_prompt[:300] + "...")
        
    except Exception as e:
        print(f"ERROR: {e}")

if __name__ == "__main__":
    test_llama_parsing()