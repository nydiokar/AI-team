#!/usr/bin/env python3
"""
Simple test without external dependencies
"""
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from core.task_parser import TaskParser
from core.interfaces import TaskType, TaskPriority

def test_task_parser():
    """Test the task parser"""
    print("=== Testing Task Parser ===")
    
    parser = TaskParser()
    task_file = "tasks/example.task.md"
    
    try:
        # Validate format
        errors = parser.validate_task_format(task_file)
        if errors:
            print(f"FAIL - Validation errors: {errors}")
        else:
            print("OK - Task file format is valid")
        
        # Parse task
        task = parser.parse_task_file(task_file)
        print(f"OK - Parsed task: {task.id}")
        print(f"   Type: {task.type.value}")
        print(f"   Priority: {task.priority.value}")
        print(f"   Title: {task.title}")
        print(f"   Target files: {task.target_files}")
        print(f"   Success criteria: {len(task.success_criteria)} items")
        return True
        
    except Exception as e:
        print(f"FAIL - Task parser error: {e}")
        return False

def test_claude_connection():
    """Test if Claude CLI is available"""
    print("\n=== Testing Claude CLI ===")
    
    import subprocess
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            print("OK - Claude CLI is available")
            print(f"   Version info: {result.stdout.strip()}")
            return True
        else:
            print(f"FAIL - Claude CLI error: {result.stderr}")
            return False
    except Exception as e:
        print(f"FAIL - Claude CLI test failed: {e}")
        return False

def main():
    """Run basic tests"""
    print("Testing AI Task Orchestrator - Basic Components\n")
    
    parser_ok = test_task_parser()
    claude_ok = test_claude_connection()
    
    print(f"\nTest Results:")
    print(f"   Task Parser: {'OK' if parser_ok else 'FAIL'}")
    print(f"   Claude CLI: {'OK' if claude_ok else 'FAIL'}")
    
    if parser_ok and claude_ok:
        print("\nAll basic components are working!")
        return True
    else:
        print("\nSome components need attention")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)