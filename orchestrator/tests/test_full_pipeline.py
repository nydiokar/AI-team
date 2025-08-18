#!/usr/bin/env python3
"""
Test the full AI pipeline with LLAMA + real Claude Code CLI
"""
import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from src.orchestrator import TaskOrchestrator
from src.core.task_parser import TaskParser

async def test_full_pipeline():
    """Test complete task processing pipeline"""
    print("Testing Full AI Task Pipeline")
    print("============================")
    print("LLAMA 3.2 + Claude Code (real)")
    print()
    
    # Create orchestrator
    orchestrator = TaskOrchestrator()
    
    # Check component status
    await orchestrator._check_component_status()
    status = orchestrator.get_status()
    
    print("Component Status:")
    print(f"  LLAMA: {'Available' if status['llama_status']['ollama_available'] else 'Fallback'}")
    print(f"  Claude: {'Available' if status['components']['claude_available'] else 'Not available'}")
    print()
    
    # Parse our test task
    parser = TaskParser()
    task = parser.parse_task_file(str(Path(__file__).parent / "tasks" / "test_llama.task.md"))
    
    print(f"Processing Task: {task.title}")
    print(f"Type: {task.type.value}, Priority: {task.priority.value}")
    print(f"Target Files: {task.target_files}")
    print()
    
    # Process the task through the full pipeline
    print("Step 1: LLAMA parsing task...")
    result = await orchestrator.process_task(task)
    print(f"Result: {'SUCCESS' if result.success else 'FAILED'}")
    print(f"Execution time: {result.execution_time:.2f}s")
    print()
    
    print("Final Output:")
    print("=" * 50)
    print(result.output)
    print("=" * 50)
    
    if result.errors:
        print("Errors:")
        for error in result.errors:
            print(f"  - {error}")

if __name__ == "__main__":
    asyncio.run(test_full_pipeline())