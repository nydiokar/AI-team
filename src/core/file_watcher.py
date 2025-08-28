"""
File system watcher for monitoring new task files
"""
import asyncio
import logging
from pathlib import Path
from typing import Callable, Optional
import time
import os
from watchdog.observers import Observer as WatchdogObserver
from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileModifiedEvent, FileMovedEvent

from .interfaces import IFileWatcher

logger = logging.getLogger(__name__)

class TaskFileHandler(FileSystemEventHandler):
    """Handler for task file system events.

    Debounces rapid file events and safely marshals callbacks into the asyncio loop.
    Tracks processed files to avoid duplicate processing of the same path.
    Handles Windows file locking edge cases with retry logic.
    """
    
    def __init__(self, callback: Callable[[str], None], loop: asyncio.AbstractEventLoop):
        self.callback = callback
        self.loop = loop
        self.processed_files = set()  # Track processed files to avoid duplicates
        self._last_event_ts: dict[str, float] = {}
        self._debounce_seconds: float = 0.25
        # Windows file locking resilience
        self._max_retries = 3
        self._retry_delay = 0.1  # Start with 100ms delay
        
    def on_created(self, event):
        """Handle file creation events"""
        if not event.is_directory and self._is_task_file(event.src_path):
            self._debounced_process(event.src_path)
    
    def on_modified(self, event):
        """Handle file modification events"""
        if not event.is_directory and self._is_task_file(event.src_path):
            # Only process if not already processed
            if event.src_path not in self.processed_files:
                self._debounced_process(event.src_path)

    def on_moved(self, event):
        """Handle file move/rename events (atomic tmp -> final)."""
        if not event.is_directory:
            dest_path = getattr(event, "dest_path", None)
            if dest_path and self._is_task_file(dest_path):
                if dest_path not in self.processed_files:
                    self._debounced_process(dest_path)

    def _debounced_process(self, file_path: str) -> None:
        now = time.time()
        self._last_event_ts[file_path] = now
        # Schedule safely from watchdog thread into asyncio loop, then set a delayed check
        def _schedule_in_loop() -> None:
            def _maybe_process() -> None:
                ts = self._last_event_ts.get(file_path, 0.0)
                if time.time() - ts >= self._debounce_seconds:
                    self._process_file(file_path)
            self.loop.call_later(self._debounce_seconds, _maybe_process)
        try:
            self.loop.call_soon_threadsafe(_schedule_in_loop)
        except Exception:
            # Fallback if loop isn't available
            time.sleep(self._debounce_seconds)
            ts = self._last_event_ts.get(file_path, 0.0)
            if time.time() - ts >= self._debounce_seconds:
                self._process_file(file_path)
    
    def _is_task_file(self, file_path: str) -> bool:
        """Check if file is a task file"""
        path = Path(file_path)
        return path.suffix.lower() in ['.md'] and '.task.' in path.name
    
    def _process_file(self, file_path: str):
        """Process a task file with Windows file locking resilience"""
        for attempt in range(self._max_retries):
            try:
                # Add to processed files to avoid duplicate processing
                self.processed_files.add(file_path)
                
                logger.info(f"New task file detected: {file_path}")
                
                # Schedule the callback safely into the main event loop thread
                if asyncio.iscoroutinefunction(self.callback):
                    asyncio.run_coroutine_threadsafe(self._async_callback(file_path), self.loop)
                else:
                    self.loop.call_soon_threadsafe(self.callback, file_path)
                
                # Success - break out of retry loop
                break
                
            except (PermissionError, OSError) as e:
                # Windows file locking/sharing violation - retry with backoff
                if attempt < self._max_retries - 1:
                    delay = self._retry_delay * (2 ** attempt)  # Exponential backoff
                    logger.warning(f"Windows file lock detected on {file_path}, retrying in {delay:.2f}s (attempt {attempt + 1}/{self._max_retries}): {e}")
                    time.sleep(delay)
                    # Remove from processed to allow retry
                    self.processed_files.discard(file_path)
                    continue
                else:
                    # Final attempt failed
                    logger.error(f"Failed to process {file_path} after {self._max_retries} attempts due to file locking: {e}")
                    # Remove from processed to allow future attempts
                    self.processed_files.discard(file_path)
            except Exception as e:
                logger.error(f"Error processing task file {file_path}: {e}")
                # Remove from processed to allow future attempts
                self.processed_files.discard(file_path)
                break
    
    async def _async_callback(self, file_path: str):
        """Async wrapper for callback"""
        try:
            if asyncio.iscoroutinefunction(self.callback):
                await self.callback(file_path)
            else:
                self.callback(file_path)
        except Exception as e:
            logger.error(f"Error in task file callback for {file_path}: {e}")

class FileWatcher(IFileWatcher):
    """File system watcher for task files.

    Watches a single directory (non-recursive) for `*.task.md` files and invokes
    the provided callback on creation/modification/move events, with basic
    debouncing to avoid duplicate triggers.
    """
    
    def __init__(self, watch_directory: str):
        self.watch_directory = Path(watch_directory).resolve()
        self.observer: Optional[object] = None
        self.handler: Optional[TaskFileHandler] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        
        # Ensure watch directory exists
        self.watch_directory.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"FileWatcher initialized for directory: {self.watch_directory}")
    
    def start(self, callback: Callable[[str], None]):
        """Start watching for new task files.

        Captures the active event loop so filesystem events from the watchdog
        thread can be scheduled back into asyncio safely.
        """
        if self.observer and self.observer.is_alive():
            logger.warning("FileWatcher is already running")
            return
        
        # Capture the running loop to marshal callbacks from watchdog thread
        try:
            self.loop = asyncio.get_event_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

        self.handler = TaskFileHandler(callback, loop=self.loop)
        self.observer = WatchdogObserver()
        
        self.observer.schedule(
            self.handler,
            str(self.watch_directory),
            recursive=False  # Don't watch subdirectories
        )
        
        self.observer.start()
        logger.info(f"FileWatcher started, monitoring: {self.watch_directory}")
        
        # Process any existing task files
        self._process_existing_files()
    
    def stop(self):
        """Stop file watching."""
        if self.observer and self.observer.is_alive():
            self.observer.stop()
            self.observer.join(timeout=5)
            logger.info("FileWatcher stopped")
        
        self.observer = None
        self.handler = None
    
    def _process_existing_files(self):
        """Process any existing task files in the directory."""
        try:
            task_files = list(self.watch_directory.glob("*.task.md"))
            
            if task_files:
                logger.info(f"Found {len(task_files)} existing task files")
                
                for task_file in task_files:
                    if self.handler:
                        self.handler._debounced_process(str(task_file))
            else:
                logger.info("No existing task files found")
                
        except Exception as e:
            logger.error(f"Error processing existing task files: {e}")
    
    def is_running(self) -> bool:
        """Check if the file watcher is currently running."""
        return self.observer is not None and self.observer.is_alive()

class AsyncFileWatcher(FileWatcher):
    """Async version of FileWatcher with better integration."""
    
    async def start_async(self, callback: Callable[[str], None]):
        """Start watching asynchronously."""
        self.start(callback)
        
        # Run the observer in a separate thread
        if self.observer:
            # The observer runs in its own thread, so we just need to keep the main thread alive
            logger.info("AsyncFileWatcher started successfully")
    
    async def stop_async(self):
        """Stop watching asynchronously."""
        await asyncio.get_event_loop().run_in_executor(None, self.stop)