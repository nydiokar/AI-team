#!/usr/bin/env python3
"""
DEFINITIVE PROMPT TEST - Shows full content to understand what's happening
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from src.bridges.llama_mediator import LlamaMediator

def full_prompt_test():
    mediator = LlamaMediator()
    
    print("FULL PROMPT ANALYSIS")
    print("=" * 80)
    print(f"LLAMA Status: Available={mediator.ollama_available}, Model={mediator.model_installed}")
    print(f"Model: {getattr(mediator, 'client', 'None')}")
    print()
    
    # Standard test task
    task = {
        'type': 'code_review',
        'title': 'Authentication Security Review',
        'main_request': 'Review authentication system for security vulnerabilities', 
        'priority': 'high',
        'target_files': ['src/auth.py'],
        'metadata': {}
    }
    
    print("GENERATING FULL PROMPT...")
    print("-" * 40)
    
    try:
        # This will use LLAMA if available, template if not
        full_prompt = mediator.create_claude_prompt(task)
        
        print(f"PROMPT LENGTH: {len(full_prompt)} characters")
        print(f"STARTS WITH 'Our task today': {'YES' if full_prompt.startswith('Our task today') else 'NO'}")
        print()
        print("FULL PROMPT CONTENT:")
        print("=" * 80)
        print(full_prompt)
        print("=" * 80)
        
        # Key structure checks
        has_principles = "Following these core principles:" in full_prompt
        has_task_details = "Task Details:" in full_prompt
        has_lets_begin = "Let's begin:" in full_prompt
        
        print(f"\nSTRUCTURE ANALYSIS:")
        print(f"  Has 'Following these core principles': {has_principles}")
        print(f"  Has 'Task Details': {has_task_details}")  
        print(f"  Has 'Let's begin': {has_lets_begin}")
        print(f"  Complete structure: {'YES' if all([has_principles, has_task_details, has_lets_begin]) else 'NO'}")
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    full_prompt_test()