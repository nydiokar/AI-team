#!/usr/bin/env python3
"""
Simple compatibility smoke checks for parser and watcher components.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from src.core import TaskParser, FileWatcher


async def test_task_parser():
    print("=== Testing Task Parser ===")

    parser = TaskParser()
    task_file = str(Path(__file__).parent / "tasks" / "example.task.md")

    try:
        errors = parser.validate_task_format(task_file)
        if errors:
            print(f"Validation errors: {errors}")
        else:
            print("Task file format is valid")

        task = parser.parse_task_file(task_file)
        print(f"Parsed task: {task.id}")
        print(f"Type: {task.type.value}")
        print(f"Priority: {task.priority.value}")
    except Exception as e:
        print(f"Task parser error: {e}")


def test_file_watcher():
    print("\n=== Testing File Watcher ===")

    def callback(file_path):
        print(f"File detected: {file_path}")

    try:
        watcher = FileWatcher("tasks")
        print(f"FileWatcher created for: {watcher.watch_directory}")
        watcher.start(callback)
        watcher.stop()
        print("FileWatcher start/stop succeeded")
    except Exception as e:
        print(f"File watcher error: {e}")


async def main():
    print("Testing compatibility components\n")
    await test_task_parser()
    test_file_watcher()
    print("\nCompatibility component testing completed")


if __name__ == "__main__":
    asyncio.run(main())
