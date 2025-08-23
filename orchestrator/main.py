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

# Add src to path after site-packages to avoid shadowing third-party modules (e.g., telegram)
src_path = Path(__file__).parent / "src"
if str(src_path) not in sys.path:
    sys.path.append(str(src_path))

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
        
        # Show Telegram status
        if hasattr(status, 'telegram_interface') and status.get('telegram_interface'):
            print(f"  Telegram Bot: [OK] Available")
        else:
            print(f"  Telegram Bot: [--] Not configured (set TELEGRAM_BOT_TOKEN)")
        
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

async def test_telegram_interface():
    """Test Telegram interface functionality"""
    
    orchestrator = TaskOrchestrator()
    
    if not orchestrator.telegram_interface:
        print("❌ Telegram interface not configured")
        print("Set TELEGRAM_BOT_TOKEN environment variable to enable Telegram")
        return
    
    if not orchestrator.telegram_interface.is_available():
        print("❌ Telegram interface not available")
        print("Check that python-telegram-bot is installed and bot token is valid")
        return
    
    print("✅ Telegram interface is available and configured")
    print(f"Bot token: {orchestrator.telegram_interface.bot_token[:10]}...")
    print(f"Allowed users: {orchestrator.telegram_interface.allowed_users or 'No restrictions'}")
    
    # Test notification functionality
    try:
        # Start bot temporarily for sending message
        await orchestrator.telegram_interface.start()
        await orchestrator.telegram_interface.notify_completion(
            "test_task",
            "This is a test notification from the AI Task Orchestrator",
            success=True,
        )
        # Give Telegram API a brief moment
        import asyncio as _asyncio
        await _asyncio.sleep(1.0)
        await orchestrator.telegram_interface.stop()
        print("✅ Test notification dispatched (check your Telegram)")
    except Exception as e:
        print(f"❌ Failed to send test notification: {e}")

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
        if command == "stats":
            _print_stats()
            return
        if command == "create-sample":
            asyncio.run(create_sample_task())
            return
        if command == "test-telegram":
            asyncio.run(test_telegram_interface())
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
    python main.py stats           Show metrics from logs/events.ndjson
    python main.py create-sample   Create a sample task for testing
    python main.py test-telegram   Test Telegram interface (if configured)
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

def _print_stats():
    """Compute and print lightweight metrics from logs/events.ndjson"""
    import json
    from datetime import datetime
    from statistics import median

    events_path = Path(config.system.logs_dir) / "events.ndjson"
    if not events_path.exists():
        print("No events found. Run the orchestrator to generate events.")
        return

    total_tasks = 0
    successes = 0
    failures = 0
    durations = []
    first_ts = None
    last_ts = None

    with events_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            # timestamps
            try:
                ts = datetime.fromisoformat(ev.get("timestamp", ""))
                if ts:
                    first_ts = ts if first_ts is None else min(first_ts, ts)
                    last_ts = ts if last_ts is None else max(last_ts, ts)
            except Exception:
                pass
            if ev.get("event") == "claude_finished":
                total_tasks += 1
                status = ev.get("status", "").upper()
                if status == "SUCCESS":
                    successes += 1
                elif status == "FAILED":
                    failures += 1
                dur = ev.get("duration_s")
                if isinstance(dur, (int, float)):
                    durations.append(float(dur))

    if total_tasks == 0:
        print("No completed tasks found in events.")
        return

    success_rate = (successes / total_tasks) * 100.0 if total_tasks else 0.0
    d_sorted = sorted(durations)
    def pct(p: float) -> float:
        if not d_sorted:
            return 0.0
        idx = max(0, min(len(d_sorted) - 1, int(round((p/100.0) * (len(d_sorted) - 1)))))
        return d_sorted[idx]

    print("Metrics (from logs/events.ndjson)")
    print("=================================")
    if first_ts and last_ts:
        print(f"Window: {first_ts.isoformat()} -> {last_ts.isoformat()}")
    print(f"Tasks: total={total_tasks} success={successes} failed={failures} success_rate={success_rate:.1f}%")
    print("Durations (s):")
    print(f"  p50={pct(50):.2f}  p90={pct(90):.2f}  p95={pct(95):.2f}  p99={pct(99):.2f}")

if __name__ == "__main__":
    main()