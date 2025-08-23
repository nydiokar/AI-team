#!/usr/bin/env python3
"""
Simple test script to verify core components work
"""
import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from src.core import TaskParser, FileWatcher, AsyncFileWatcher
from src.bridges import ClaudeBridge

async def test_task_parser():
    """Test the task parser"""
    print("=== Testing Task Parser ===")
    
    parser = TaskParser()
    task_file = str(Path(__file__).parent / "tasks" / "example.task.md")
    
    try:
        # Validate format
        errors = parser.validate_task_format(task_file)
        if errors:
            print(f"❌ Validation errors: {errors}")
        else:
            print("✅ Task file format is valid")
        
        # Parse task
        task = parser.parse_task_file(task_file)
        print(f"✅ Parsed task: {task.id}")
        print(f"   Type: {task.type.value}")
        print(f"   Priority: {task.priority.value}")
        print(f"   Title: {task.title}")
        print(f"   Target files: {task.target_files}")
        print(f"   Success criteria: {len(task.success_criteria)} items")
        
    except Exception as e:
        print(f"❌ Task parser error: {e}")

def test_file_watcher():
    """Test the file watcher"""
    print("\n=== Testing File Watcher ===")
    
    def callback(file_path):
        print(f"📁 File detected: {file_path}")
    
    try:
        watcher = FileWatcher("tasks")
        print(f"✅ FileWatcher created for: {watcher.watch_directory}")
        
        # Test start/stop
        watcher.start(callback)
        if watcher.is_running():
            print("✅ FileWatcher started successfully")
        else:
            print("❌ FileWatcher failed to start")
        
        # Stop immediately for testing
        watcher.stop()
        print("✅ FileWatcher stopped")
        
    except Exception as e:
        print(f"❌ File watcher error: {e}")

async def test_claude_bridge():
    """Test the Claude bridge"""
    print("\n=== Testing Claude Bridge ===")
    
    try:
        bridge = ClaudeBridge()
        
        # Test connection
        if bridge.test_connection():
            print("✅ Claude Code CLI is available")
        else:
            print("❌ Claude Code CLI not found or not working")
            return
        
        print("✅ Claude bridge initialized successfully")
        
    except Exception as e:
        print(f"❌ Claude bridge error: {e}")

async def main():
    """Run all tests"""
    print("🚀 Testing AI Task Orchestrator Components\n")
    
    await test_task_parser()
    test_file_watcher()
    await test_claude_bridge()
    
    print("\n✅ Component testing completed!")

if __name__ == "__main__":
    asyncio.run(main())