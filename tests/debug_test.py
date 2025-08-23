#!/usr/bin/env python3
"""
Debug test for Claude file path resolution
"""
import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime

# Add orchestrator directory to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.bridges.claude_bridge import ClaudeBridge
from src.core.interfaces import Task, TaskType, TaskPriority, TaskStatus

async def run_debug_test():
    """Test Claude with a simple summarize task"""
    print("=== Claude Bridge Debug Test ===")
    
    # Create a simple task
    task = Task(
        id="debug_test",
        type=TaskType.SUMMARIZE,
        priority=TaskPriority.LOW,
        status=TaskStatus.PENDING,
        created=datetime.now().isoformat(),
        title="Debug Test",
        target_files=["src/orchestrator.py"],  # Relative path
        prompt="Summarize the orchestrator file. Do not write any changes.",
        success_criteria=["No writes performed", "Summary present"],
        context="Debug test for Claude file path resolution",
        metadata={}
    )
    
    # Initialize Claude bridge
    bridge = ClaudeBridge()
    
    # Test connection
    if not bridge.test_connection():
        print("ERROR: Claude CLI not available")
        return False
    
    print(f"Current working directory: {os.getcwd()}")
    print(f"Task target files: {task.target_files}")
    
    # Execute task
    print("Executing task with Claude...")
    result = await bridge.execute_task(task)
    
    print(f"Task success: {result.success}")
    print(f"Return code: {result.return_code}")
    print(f"Execution time: {result.execution_time:.2f}s")
    
    # Print output preview
    if result.output:
        print("\nOutput preview:")
        print("-" * 40)
        preview = result.output[:500] + "..." if len(result.output) > 500 else result.output
        print(preview)
        print("-" * 40)
    
    # Print raw stdout/stderr
    print("\nRaw stdout preview:")
    print("-" * 40)
    stdout_preview = result.raw_stdout[:500] + "..." if len(result.raw_stdout) > 500 else result.raw_stdout
    print(stdout_preview)
    print("-" * 40)
    
    if result.raw_stderr:
        print("\nRaw stderr:")
        print("-" * 40)
        print(result.raw_stderr)
        print("-" * 40)
    
    # Print parsed output
    if result.parsed_output:
        print("\nParsed output:")
        print("-" * 40)
        print(f"Type: {result.parsed_output.get('type')}")
        print(f"Subtype: {result.parsed_output.get('subtype')}")
        print(f"Is error: {result.parsed_output.get('is_error')}")
        print(f"Num turns: {result.parsed_output.get('num_turns')}")
        if 'result' in result.parsed_output:
            print(f"Result: {result.parsed_output['result'][:200]}..." if len(result.parsed_output['result']) > 200 else result.parsed_output['result'])
        print("-" * 40)
    
    return result.success

if __name__ == "__main__":
    success = asyncio.run(run_debug_test())
    sys.exit(0 if success else 1)
