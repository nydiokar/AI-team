#!/usr/bin/env python3
"""
Main entry point for the AI Task Orchestrator
"""
import asyncio
import signal
import sys
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from src.orchestrator import TaskOrchestrator
from config import config

# Configure logging with rotation for file handler
logs_dir = Path(config.system.logs_dir)
logs_dir.mkdir(parents=True, exist_ok=True)
file_handler = RotatingFileHandler(
    logs_dir / "orchestrator.log",
    maxBytes=1_000_000,  # ~1MB
    backupCount=3,
    encoding="utf-8"
)
stream_handler = logging.StreamHandler()
logging.basicConfig(
    level=getattr(logging, config.system.log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[stream_handler, file_handler]
)

logger = logging.getLogger(__name__)

class OrchestratorCLI:
    """Command-line interface for the orchestrator"""
    
    def __init__(self):
        self.orchestrator = TaskOrchestrator()
        self.shutdown_event = asyncio.Event()
    
    async def start(self):
        """Start the orchestrator"""
        
        # Setup signal handlers
        for sig in [signal.SIGINT, signal.SIGTERM]:
            signal.signal(sig, self._signal_handler)
        
        try:
            print("AI Task Orchestrator")
            print("===================")
            print()
            
            # Start orchestrator
            await self.orchestrator.start()
            
            # Print status
            status = self.orchestrator.get_status()
            self._print_status(status)
            
            print()
            print("Orchestrator is running. Press Ctrl+C to stop.")
            print(f"Watching for task files in: {Path(config.system.tasks_dir).resolve()}")
            print()
            
            # Wait for shutdown signal
            await self.shutdown_event.wait()
            
        except KeyboardInterrupt:
            pass
        finally:
            print("\nShutting down...")
            await self.orchestrator.stop()
            print("Shutdown complete.")
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        logger.info(f"Received signal {signum}, initiating shutdown")
        self.shutdown_event.set()
    
    def _print_status(self, status):
        """Print formatted status"""
        print("Component Status:")
        components = status["components"]
        
        print(f"  Claude Code CLI: {'[OK] Available' if components['claude_available'] else '[--] Not available'}")
        
        llama_status = status["llama_status"]
        if llama_status["ollama_available"]:
            print(f"  LLAMA/Ollama: [OK] Available ({llama_status['model']})")
        else:
            print(f"  LLAMA/Ollama: [--] Not available (using fallback parser)")
        
        print(f"  File Watcher: {'[OK] Running' if components['file_watcher_running'] else '[--] Stopped'}")
        
        print()
        print("Task Status:")
        tasks = status["tasks"]
        print(f"  Active: {tasks['active']}")
        print(f"  Queued: {tasks['queued']}")
        print(f"  Completed: {tasks['completed']}")
        print(f"  Workers: {tasks['workers']}")

async def create_sample_task():
    """Create a sample task for testing"""
    
    orchestrator = TaskOrchestrator()
    
    print("Creating sample task...")
    
    sample_description = """
    Review the database connection code in our application and identify any 
    potential issues with connection pooling, timeout handling, and error recovery.
    Focus on performance and reliability improvements.
    """
    
    task_id = orchestrator.create_task_from_description(sample_description)
    print(f"Created sample task: {task_id}")
    print(f"Task file location: {Path(config.system.tasks_dir) / f'{task_id}.task.md'}")

async def show_status():
    """Show current orchestrator status"""
    
    orchestrator = TaskOrchestrator()
    await orchestrator._check_component_status()
    
    status = orchestrator.get_status()
    
    print("AI Task Orchestrator Status")
    print("===========================")
    print()
    
    cli = OrchestratorCLI()
    cli._print_status(status)

def main():
    """Main entry point"""
    
    if len(sys.argv) > 1:
        command = sys.argv[1]
        # Maintenance commands
        if command == "clean":
            _handle_clean(sys.argv[2:])
            return
        if command == "status":
            asyncio.run(show_status())
            return
        if command == "create-sample":
            asyncio.run(create_sample_task())
            return
        if command == "help":
            print_help()
            return
        print(f"Unknown command: {command}")
        print_help()
        sys.exit(1)
    else:
        # Default: start the orchestrator
        cli = OrchestratorCLI()
        asyncio.run(cli.start())

def print_help():
    """Print help information"""
    print("""AI Task Orchestrator

Usage:
    python main.py                 Start the orchestrator
    python main.py status          Show component status
    python main.py create-sample   Create a sample task for testing
    python main.py clean tasks     Archive loose tasks to tasks/processed
    python main.py clean artifacts --days N   Delete results/summaries older than N days
    python main.py help            Show this help

Environment Setup:
    Copy .env.example to .env and configure:
    - TELEGRAM_BOT_TOKEN (optional for Telegram integration)
    - TELEGRAM_ALLOWED_USERS (optional)
    - TELEGRAM_CHAT_ID (optional)

Directory Structure:
    tasks/      - Drop .task.md files here
    results/    - Task execution results
    summaries/  - Task result summaries
    logs/       - System logs

For full functionality, ensure Claude Code CLI is installed and accessible.
""")

def _handle_clean(args):
    """Handle maintenance clean commands"""
    if not args:
        print("Usage: python main.py clean [tasks|artifacts] [--days N]")
        return
    action = args[0]
    if action == "tasks":
        _clean_tasks()
        print("Archived loose tasks to tasks/processed")
        return
    if action == "artifacts":
        days = 30
        # Simple flag parsing
        if len(args) >= 3 and args[1] == "--days":
            try:
                days = int(args[2])
            except Exception:
                pass
        n = _clean_artifacts(days)
        print(f"Deleted {n} old artifacts older than {days} days")
        return
    print("Unknown clean target. Use 'tasks' or 'artifacts'.")

def _clean_tasks():
    tasks_dir = Path(config.system.tasks_dir)
    processed = tasks_dir / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    for p in tasks_dir.glob("*.task.md"):
        target = processed / f"{p.stem}.archived.task.md"
        try:
            if p.resolve().parent != processed.resolve():
                p.replace(target)
        except Exception:
            continue

def _clean_artifacts(days: int) -> int:
    from datetime import datetime, timedelta
    cutoff = datetime.now() - timedelta(days=days)
    removed = 0
    for base in (Path(config.system.results_dir), Path(config.system.summaries_dir)):
        base.mkdir(parents=True, exist_ok=True)
        for p in base.glob("*"):
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime)
                if mtime < cutoff:
                    p.unlink(missing_ok=True)
                    removed += 1
            except Exception:
                continue
    return removed

if __name__ == "__main__":
    main()