"""
File system watcher for monitoring new task files
"""
import asyncio
import logging
from pathlib import Path
from typing import Callable, Optional
from watchdog.observers import Observer as WatchdogObserver
from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileModifiedEvent

from .interfaces import IFileWatcher

logger = logging.getLogger(__name__)

class TaskFileHandler(FileSystemEventHandler):
    """Handler for task file system events"""
    
    def __init__(self, callback: Callable[[str], None], loop: asyncio.AbstractEventLoop):
        self.callback = callback
        self.loop = loop
        self.processed_files = set()  # Track processed files to avoid duplicates
        
    def on_created(self, event):
        """Handle file creation events"""
        if not event.is_directory and self._is_task_file(event.src_path):
            self._process_file(event.src_path)
    
    def on_modified(self, event):
        """Handle file modification events"""
        if not event.is_directory and self._is_task_file(event.src_path):
            # Only process if not already processed
            if event.src_path not in self.processed_files:
                self._process_file(event.src_path)
    
    def _is_task_file(self, file_path: str) -> bool:
        """Check if file is a task file"""
        path = Path(file_path)
        return path.suffix.lower() in ['.md'] and '.task.' in path.name
    
    def _process_file(self, file_path: str):
        """Process a task file"""
        try:
            # Add to processed files to avoid duplicate processing
            self.processed_files.add(file_path)
            
            logger.info(f"New task file detected: {file_path}")
            
            # Schedule the callback safely into the main event loop thread
            if asyncio.iscoroutinefunction(self.callback):
                asyncio.run_coroutine_threadsafe(self._async_callback(file_path), self.loop)
            else:
                self.loop.call_soon_threadsafe(self.callback, file_path)
            
        except Exception as e:
            logger.error(f"Error processing task file {file_path}: {e}")
    
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
    """File system watcher for task files"""
    
    def __init__(self, watch_directory: str):
        self.watch_directory = Path(watch_directory).resolve()
        self.observer: Optional[object] = None
        self.handler: Optional[TaskFileHandler] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        
        # Ensure watch directory exists
        self.watch_directory.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"FileWatcher initialized for directory: {self.watch_directory}")
    
    def start(self, callback: Callable[[str], None]):
        """Start watching for new task files"""
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
        """Stop file watching"""
        if self.observer and self.observer.is_alive():
            self.observer.stop()
            self.observer.join(timeout=5)
            logger.info("FileWatcher stopped")
        
        self.observer = None
        self.handler = None
    
    def _process_existing_files(self):
        """Process any existing task files in the directory"""
        try:
            task_files = list(self.watch_directory.glob("*.task.md"))
            
            if task_files:
                logger.info(f"Found {len(task_files)} existing task files")
                
                for task_file in task_files:
                    if self.handler:
                        self.handler._process_file(str(task_file))
            else:
                logger.info("No existing task files found")
                
        except Exception as e:
            logger.error(f"Error processing existing task files: {e}")
    
    def is_running(self) -> bool:
        """Check if the file watcher is currently running"""
        return self.observer is not None and self.observer.is_alive()

class AsyncFileWatcher(FileWatcher):
    """Async version of FileWatcher with better integration"""
    
    async def start_async(self, callback: Callable[[str], None]):
        """Start watching asynchronously"""
        self.start(callback)
        
        # Run the observer in a separate thread
        if self.observer:
            # The observer runs in its own thread, so we just need to keep the main thread alive
            logger.info("AsyncFileWatcher started successfully")
    
    async def stop_async(self):
        """Stop watching asynchronously"""
        await asyncio.get_event_loop().run_in_executor(None, self.stop)