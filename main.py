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

# Reduce noise from third-party HTTP logs (python-telegram-bot uses httpx)
try:
    logging.getLogger("httpx").setLevel(logging.WARNING)
except Exception:
    pass

# Global redaction filter for sensitive values in logs
class _RedactFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__(name="redact")
        import re as _re
        # Compile common sensitive patterns
        self._patterns = [
            # Telegram bot token in URL path: /bot<token>/...
            (_re.compile(r"/bot[0-9A-Za-z:_-]+"), "/bot<REDACTED>"),
            # Authorization headers
            (_re.compile(r"(Authorization:\s*Bearer\s+)[^\s]+", flags=_re.IGNORECASE), r"\1<REDACTED>"),
            # Generic token key=value appearances (best-effort)
            (_re.compile(r"(TELEGRAM_BOT_TOKEN=)[^\s]+", flags=_re.IGNORECASE), r"\1<REDACTED>"),
        ]
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            redacted = msg
            for pat, repl in self._patterns:
                redacted = pat.sub(repl, redacted)
            if redacted != msg:
                record.msg = redacted
                record.args = ()
        except Exception:
            pass
        return True

# Attach redaction filter to both handlers
try:
    _rf = _RedactFilter()
    file_handler.addFilter(_rf)
    stream_handler.addFilter(_rf)
except Exception:
    pass

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
        if command == "validate-artifacts":
            _validate_artifacts(sys.argv[2:])
            return
        if command == "create-sample-artifact":
            _create_sample_artifact()
            return
        if command == "doctor":
            _doctor()
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
        if command == "tail-events":
            _tail_events(sys.argv[2:])
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
    python main.py validate-artifacts        Validate results/*.json against schema
    python main.py create-sample-artifact    Generate a sample artifact (new schema)
    python main.py doctor                    Print effective config and CLI availability
    python main.py tail-events [--task TASK_ID] [--lines N]   Show recent NDJSON events
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

def _tail_events(args=None):
    """Print recent events from logs/events.ndjson (Windows-friendly).

    Usage:
      python main.py tail-events [--task TASK_ID] [--lines N]
    """
    import json as _json
    from collections import deque as _deque
    args = args or []
    task_filter = None
    max_lines = 50
    # Simple flag parsing
    it = iter(args)
    for a in it:
        if a == "--task":
            try:
                task_filter = next(it)
            except StopIteration:
                break
        elif a == "--lines":
            try:
                max_lines = max(1, int(next(it)))
            except Exception:
                pass
    events_path = Path(config.system.logs_dir) / "events.ndjson"
    if not events_path.exists():
        print("No events file found.")
        return
    buf = _deque(maxlen=max_lines)
    try:
        with events_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = _json.loads(line)
                except Exception:
                    continue
                if task_filter and ev.get("task_id") != task_filter:
                    continue
                buf.append(ev)
    except Exception as e:
        print(f"Failed to read events: {e}")
        return
    for ev in buf:
        ts = ev.get("timestamp", "")
        name = ev.get("event", "")
        tid = ev.get("task_id", "-")
        extra = {k: v for k, v in ev.items() if k not in ("timestamp", "event", "task_id")}
        print(f"{ts}  {name:18s}  task={tid}  {extra}")

def _validate_artifacts(args=None):
    """Validate artifacts against docs/schema/results.schema.json

    Usage:
      python main.py validate-artifacts [--ignore-legacy] [glob1] [glob2] ...

    By default this is strict and fails on legacy artifacts (no schema_version).
    Use --ignore-legacy to skip those.
    """
    from pathlib import Path as _Path
    import json as _json
    import sys as _sys

    schema_path = _Path("docs/schema/results.schema.json")
    results_dir = _Path(config.system.results_dir)

    if not schema_path.exists():
        print("Schema not found: docs/schema/results.schema.json")
        _sys.exit(1)
    if not results_dir.exists():
        try:
            results_dir.mkdir(parents=True, exist_ok=True)
            print(f"Created results directory: {results_dir}")
        except Exception as e:
            print(f"Failed to create results directory {results_dir}: {e}")
            _sys.exit(1)

    try:
        schema = _json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Failed to read schema: {e}")
        _sys.exit(1)

    # Minimal validator to avoid external deps: check required keys and simple types
    def _type_ok(value, expected):
        if expected == "string":
            return isinstance(value, str)
        if expected == "boolean":
            return isinstance(value, bool)
        if expected == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if expected == "object":
            return isinstance(value, dict)
        if expected == "array":
            return isinstance(value, list)
        return True

    def _validate_required(doc, subschema, path_prefix="$"):
        errors = []
        required = subschema.get("required", [])
        for key in required:
            if key not in doc:
                errors.append(f"{path_prefix}.{key}: required property missing")
        for key, prop in subschema.get("properties", {}).items():
            if key in doc:
                val = doc[key]
                expected_type = prop.get("type")
                if isinstance(expected_type, list):
                    # Allow any of listed types
                    if not any(_type_ok(val, t) for t in expected_type if isinstance(t, str)):
                        errors.append(f"{path_prefix}.{key}: invalid type")
                elif isinstance(expected_type, str):
                    if not _type_ok(val, expected_type):
                        errors.append(f"{path_prefix}.{key}: expected {expected_type}")
                # Recurse for nested objects
                if isinstance(val, dict) and isinstance(prop.get("properties"), dict):
                    errors.extend(_validate_required(val, prop, f"{path_prefix}.{key}"))
        return errors

    # Parse flags and patterns
    args = args or []
    ignore_legacy = "--ignore-legacy" in args
    patterns = [a for a in args if not a.startswith("--")] or ["*.json"]

    # Resolve matched files (support absolute/relative and results/ globs)
    matched_files = []
    for pat in patterns:
        p = _Path(pat)
        if any(ch in pat for ch in ("/", "\\", ":")):
            matched_files.extend(sorted(p.parent.glob(p.name)))
        else:
            matched_files.extend(sorted(_Path(results_dir).glob(pat)))

    if not matched_files:
        print("No artifacts matched the given patterns.")
        return

    total = 0
    ok = 0
    failed = 0
    skipped_legacy = 0
    for p in matched_files:
        total += 1
        try:
            doc = _json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"{p.name}: invalid JSON: {e}")
            failed += 1
            continue
        if "schema_version" not in doc and ignore_legacy:
            print(f"{p.name}: SKIP (legacy, no schema_version)")
            skipped_legacy += 1
            continue
        errors = _validate_required(doc, schema)
        # Extra checks: schema_version exact match if present
        sv = doc.get("schema_version")
        if sv != "1.0":
            errors.append("$.schema_version: must equal '1.0'")
        if errors:
            print(f"{p.name}: INVALID")
            for e in errors[:10]:
                print(f"  - {e}")
            if len(errors) > 10:
                print(f"  ... and {len(errors)-10} more")
            failed += 1
        else:
            print(f"{p.name}: OK")
            ok += 1

    print()
    print(f"Validation summary: total={total} ok={ok} failed={failed} skipped_legacy={skipped_legacy}")
    if failed:
        _sys.exit(1)

def _create_sample_artifact():
    """Create a new-schema sample artifact via orchestrator writer."""
    from datetime import datetime as _dt
    from src.core.interfaces import TaskResult as _TaskResult

    orch = TaskOrchestrator()
    task_id = f"sample_{_dt.now().strftime('%Y%m%d_%H%M%S')}"
    tr = _TaskResult(
        task_id=task_id,
        success=True,
        output="Sample Summary\n\nDetailed content...",
        errors=[],
        files_modified=[],
        execution_time=0.02,
        timestamp=_dt.now().isoformat(),
        raw_stdout="stdout preview...\nmore lines...",
        raw_stderr="",
        parsed_output={"content": "ok", "meta": {"note": "sample"}},
        return_code=0,
    )
    orch._write_artifacts(task_id, tr)
    print(f"Created sample artifact: {Path(config.system.results_dir) / (task_id + '.json')}")

def _doctor():
    """Print effective configuration and check CLI availability."""
    from shutil import which as _which
    from subprocess import run as _run
    from pathlib import Path as _Path

    # Reload env-derived fields to reflect current environment
    try:
        config.reload_from_env()
    except Exception:
        pass

    print("Effective configuration:")
    print(f"  Log level        : {config.system.log_level}")
    print(f"  CLAUDE timeout (s): {config.claude.timeout}")
    print(f"  CLAUDE max_turns  : {config.claude.max_turns} (0=unlimited/CLI default)")
    print(f"  Skip permissions  : {config.claude.skip_permissions}")
    print(f"  Base CWD          : {config.claude.base_cwd}")
    print(f"  Allowed root      : {config.claude.allowed_root}")

    # Basic directory diagnostics (existence and readability)
    print("\nDirectory diagnostics:")
    dirs = {
        "tasks_dir"    : _Path(config.system.tasks_dir),
        "results_dir"  : _Path(config.system.results_dir),
        "summaries_dir": _Path(config.system.summaries_dir),
        "logs_dir"     : _Path(config.system.logs_dir),
    }
    for name, path in dirs.items():
        try:
            path.mkdir(parents=True, exist_ok=True)
            readable = path.exists()
            writable = False
            try:
                # Non-destructive writeability probe in logs dir only
                if name == "logs_dir":
                    tmp = path / ".doctor.tmp"
                    tmp.write_text("ok", encoding="utf-8")
                    tmp.unlink(missing_ok=True)
                    writable = True
            except Exception:
                writable = False
            print(f"  {name:14s}: {str(path)} | exists={readable} writable={writable if name=='logs_dir' else 'n/a'}")
        except Exception as e:
            print(f"  {name:14s}: {str(path)} | ERROR: {e}")

    # Validate working directory allowlist relationship
    try:
        base = _Path(config.claude.base_cwd).resolve() if config.claude.base_cwd else None
        allowed = _Path(config.claude.allowed_root).resolve() if config.claude.allowed_root else None
        if base and allowed:
            within = (allowed in base.parents) or (base == allowed)
            print(f"\nWorking directory allowlist: base_within_allowed={within}")
            if not within:
                print("  WARNING: base CWD is outside allowed root; cwd overrides may be rejected.")
    except Exception as e:
        print(f"\nWorking directory allowlist: check failed: {e}")

    exe = _which("claude") or "claude"
    print(f"\nClaude executable: {exe}")
    try:
        r = _run([exe, "--version"], capture_output=True, text=True, timeout=5)
        print(f"  --version rc={r.returncode} out={r.stdout.strip()[:80]}")
    except Exception as e:
        print(f"  Version check failed: {e}")
    try:
        r2 = _run([exe, "auth", "status"], capture_output=True, text=True, timeout=5)
        print(f"  auth status rc={r2.returncode} out={(r2.stdout or r2.stderr).strip()[:120]}")
    except Exception as e:
        print(f"  Auth check failed: {e}")

if __name__ == "__main__":
    main()