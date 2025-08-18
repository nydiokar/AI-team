#!/usr/bin/env python3
"""
Main entry point for the AI Task Orchestrator
"""
import asyncio
import signal
import sys
import logging
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from src.orchestrator import TaskOrchestrator
from config import config

# Configure logging
logging.basicConfig(
    level=getattr(logging, config.system.log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(config.system.logs_dir) / "orchestrator.log")
    ]
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
        
        if command == "status":
            asyncio.run(show_status())
        elif command == "create-sample":
            asyncio.run(create_sample_task())
        elif command == "help":
            print_help()
        else:
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

if __name__ == "__main__":
    main()