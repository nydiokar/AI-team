#!/usr/bin/env python3
"""Test unified prompt structure"""

import sys
from pathlib import Path

# Add src to path
src_path = Path(__file__).parent / "src"
if str(src_path) not in sys.path:
    sys.path.append(str(src_path))

from src.bridges.llama_mediator import LlamaMediator

def test_unified_prompts():
    mediator = LlamaMediator()

    # Test automatic mode (no agent_type in metadata)
    auto_parsed = {
        'type': 'code_review',
        'title': 'Review Authentication Security',  
        'main_request': 'Review authentication security issues',
        'priority': 'high',
        'target_files': ['src/auth.py'],
        'metadata': {}  # No agent_type = automatic mode
    }

    # Test manual mode (has agent_type in metadata) 
    manual_parsed = {
        'type': 'code_review', 
        'title': 'Review Authentication Security',
        'main_request': 'Review authentication security issues', 
        'priority': 'high',
        'target_files': ['src/auth.py'],
        'metadata': {'agent_type': 'code_review'}  # Has agent_type = manual mode  
    }

    print('=== AUTOMATIC MODE PROMPT ===')
    auto_prompt = mediator.create_claude_prompt(auto_parsed)
    print(auto_prompt[:400] + '...\n')

    print('=== MANUAL MODE PROMPT ===') 
    manual_prompt = mediator.create_claude_prompt(manual_parsed)
    print(manual_prompt[:400] + '...\n')

    print('=== COMPARISON ===')
    auto_starts_correctly = auto_prompt.startswith("Our task today consists of")
    manual_starts_correctly = manual_prompt.startswith("Our task today consists of")
    
    print(f'Auto mode uses unified structure: {auto_starts_correctly}')
    print(f'Manual mode uses unified structure: {manual_starts_correctly}')
    print(f'Both use same structure: {auto_starts_correctly and manual_starts_correctly}')
    
    # Check if both include general principles
    both_have_principles = "Following these core principles:" in auto_prompt and "Following these core principles:" in manual_prompt
    print(f'Both include general principles: {both_have_principles}')
    
    # Check if both have the task type noted
    auto_has_type = "(auto-selected)" in auto_prompt
    manual_has_type = "manual" not in auto_prompt  # Manual doesn't say "manual"
    print(f'Auto mode marked as auto-selected: {auto_has_type}')

if __name__ == "__main__":
    test_unified_prompts()