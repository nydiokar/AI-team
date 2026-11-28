"""Long-lived Codebase Memory MCP (CBM) client over MCP stdio.

Verified against codebase-memory-mcp 0.11.0 on Windows:
* `codebase-memory-mcp` with no arguments serves MCP JSON-RPC on stdio. One process
  costs ~2.5 s to start; each warm `query_graph` then takes ~20-40 ms. The `cli`
  form pays the start-up cost on every call, so it is not used for queries.
* Tool failures arrive as `result.isError == true` (e.g. the Cypher subset rejects
  `id()`/`labels()` inside WHERE). They are raised as CBMError so that a failed query
  can never be mistaken for a node without neighbours.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

NO_WINDOW: int = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class CBMError(RuntimeError):
    """CBM failed, timed out or returned an error result."""


def find_executable(explicit: Optional[str] = None) -> str:
    """Resolve the CBM binary: --cbm, $CBM_BIN, PATH, then the default Windows/Unix install dirs."""
    configured: str | None = explicit if explicit is not None else os.getenv('CBM_BIN')
    if configured is not None:
        if Path(configured).is_file() and os.access(configured, os.X_OK):
            return configured
        raise CBMError(f'CBM executable is unavailable: {configured}')
    candidates: list[Optional[str]] = [shutil.which('codebase-memory-mcp')]
    local: Optional[str] = os.getenv('LOCALAPPDATA')
    if local:
        candidates.append(str(Path(local) / 'Programs' / 'codebase-memory-mcp' / 'codebase-memory-mcp.exe'))
    candidates.append(str(Path.home() / '.local' / 'bin' / 'codebase-memory-mcp'))
    for c in candidates:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return c
    raise CBMError('codebase-memory-mcp executable not found; pass --cbm <path> or set CBM_BIN')


def _structured(result: dict[str, Any]) -> Any:
    if result.get('isError'):
        sc: Any = result.get('structuredContent')
        msg: str = sc.get('error') if isinstance(sc, dict) and sc.get('error') else ''
        if not msg:
            msg = ' '.join(str(c.get('text', '')) for c in result.get('content') or [] if isinstance(c, dict))
        raise CBMError(msg.strip() or 'CBM tool returned isError without a message')
    if 'structuredContent' in result:
        return result['structuredContent']
    texts: list[str] = [str(c.get('text', '')) for c in result.get('content') or [] if isinstance(c, dict)]
    try:
        return json.loads(''.join(texts))
    except ValueError as exc:
        raise CBMError('CBM returned non-JSON content: ' + ''.join(texts)[:400]) from exc


def cypher_str(value: str) -> str:
    """Quote a string literal for CBM Cypher."""
    return "'" + str(value).replace('\\', '\\\\').replace("'", "\\'") + "'"


class CBMClient:
    """One MCP stdio session, restarted at most once per failed call."""

    def __init__(self, exe: str, timeout: float = 30.0, log: Callable[[str], None] = lambda s: None) -> None:
        self.exe: str = exe
        self.timeout: float = timeout
        self.log: Callable[[str], None] = log
        self.proc: Optional[subprocess.Popen[str]] = None
        self.lock: threading.RLock = threading.RLock()
        self.msgs: 'queue.Queue[Optional[dict[str, Any]]]' = queue.Queue()
        self.next_id: int = 1
        self.calls: int = 0
        self.call_seconds: float = 0.0
        self.version: str = ''

    # -- process management -------------------------------------------------
    def _start(self, init_timeout: float = 60.0) -> None:
        t0: float = time.perf_counter()
        self.msgs = queue.Queue()
        self.proc = subprocess.Popen([self.exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                                     errors='replace', bufsize=1, creationflags=NO_WINDOW)
        proc: subprocess.Popen[str] = self.proc
        msgs: 'queue.Queue[Optional[dict[str, Any]]]' = self.msgs

        def pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line.startswith('{'):
                    continue
                try:
                    msgs.put(json.loads(line))
                except ValueError:
                    continue
            msgs.put(None)

        threading.Thread(target=pump, daemon=True, name='cbm-stdout').start()
        init: Any = self._rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                             'clientInfo': {'name': 'architecture-pathway-audit', 'version': '5'}},
                              timeout=init_timeout)
        self.version = str((init.get('serverInfo') or {}).get('version', ''))
        self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self.log(f'CBM MCP session ready (v{self.version}) in {time.perf_counter() - t0:.2f}s')

    def _send(self, obj: dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj) + '\n')
        self.proc.stdin.flush()

    def _rpc(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        rid: int = self.next_id
        self.next_id += 1
        self._send({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params})
        deadline: float = time.monotonic() + timeout
        while True:
            left: float = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f'CBM {method} timed out after {timeout:.0f}s')
            try:
                msg: Optional[dict[str, Any]] = self.msgs.get(timeout=left)
            except queue.Empty:
                continue
            if msg is None:
                raise CBMError('CBM MCP process exited')
            if msg.get('id') != rid:
                continue
            if 'error' in msg:
                raise CBMError(f"CBM JSON-RPC error: {msg['error']}")
            return msg.get('result') or {}

    def close(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                try:
                    assert self.proc.stdin is not None
                    self.proc.stdin.close()
                    self.proc.wait(3)
                except (OSError, subprocess.TimeoutExpired):
                    self.proc.kill()
                    self.proc.wait()
            if self.proc and self.proc.stdout:
                self.proc.stdout.close()
            self.proc = None

    def ensure_started(self, init_timeout: float = 60.0) -> None:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                try:
                    self._start(init_timeout)
                except BaseException:
                    self.close()
                    raise

    # -- tools ----------------------------------------------------------------
    def tool(
        self, name: str, args: dict[str, Any], timeout: Optional[float] = None, *, deadline: float | None = None,
    ) -> Any:
        """Call a CBM tool; one restart on a dead/hung process, never on a tool error."""
        with self.lock:
            for attempt in (1, 2):
                wait: float = timeout or self.timeout
                if deadline is not None:
                    wait = min(wait, deadline - time.monotonic())
                    if wait <= 0:
                        raise TimeoutError('CBM request deadline exceeded')
                self.ensure_started(init_timeout=min(60.0, wait) if deadline is not None else 60.0)
                t0: float = time.perf_counter()
                try:
                    if deadline is not None:
                        wait = min(wait, deadline - time.monotonic())
                        if wait <= 0:
                            raise TimeoutError('CBM request deadline exceeded')
                    result: Any = self._rpc('tools/call', {'name': name, 'arguments': args},
                                            wait)
                except (TimeoutError, OSError, CBMError) as exc:
                    if isinstance(exc, CBMError) and 'exited' not in str(exc):
                        raise
                    self.log(f'CBM session failed ({exc}); restarting (attempt {attempt})')
                    self.close()
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError('CBM request deadline exceeded') from exc
                    if attempt == 2:
                        raise CBMError(f'CBM {name} failed twice: {exc}') from exc
                    continue
                finally:
                    self.calls += 1
                    self.call_seconds += time.perf_counter() - t0
                return _structured(result)
        raise CBMError('unreachable')

    def query(
        self, project: str, cypher: str, page: int = 200, max_pages: int = 20, *, deadline: float | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Run a read query, following CBM `has_more` with explicit offsets.

        Returns (rows as dicts, complete). complete=False means the row cap was hit and the
        caller must surface the truncation. CBM keeps rows whole when its output budget
        truncates a page, so paging simply continues from the rows actually received.
        """
        rows: list[dict[str, Any]] = []
        cursor: Optional[str] = None
        offset: int = 0
        total: Optional[int] = None
        for _ in range(max_pages):
            args: dict[str, Any] = {'project': project, 'query': cypher, 'format': 'json', 'max_rows': page,
                                    # default budget returns ~90 rows; size it to the page (rows stay whole)
                                    'max_output_tokens': max(20000, page * 400)}
            if cursor:
                args['cursor'] = cursor  # snapshot continuation: stable across concurrent re-indexing
            else:
                args['offset'] = offset
            res: Any = self.tool('query_graph', args, deadline=deadline) if deadline is not None else self.tool('query_graph', args)
            if not isinstance(res, dict) or not isinstance(res.get('rows'), list):
                raise CBMError('query_graph returned no rows array: ' + str(res)[:300])
            cols: list[str] = res.get('columns')
            got: int = len(res['rows'])
            current_total: int = res.get('total')
            more: bool = res.get('has_more')
            relation: str = res.get('total_relation')
            if (not isinstance(cols, list) or any(not isinstance(col, str) for col in cols)
                    or type(more) is not bool or res.get('truncated') is not more
                    or type(current_total) is not int or current_total < offset + got
                    or type(res.get('returned')) is not int or res['returned'] != got
                    or res.get('offset', 0) != offset or relation not in ('eq', 'gte')):
                raise CBMError('query_graph returned malformed paging metadata')
            for row in res['rows']:
                if not isinstance(row, list) or len(row) != len(cols):
                    raise CBMError('query_graph row does not match its columns')
                rows.append(dict(zip(cols, row)))
            if relation == 'eq':
                if total is not None and current_total != total:
                    raise CBMError('CBM paging total changed')
                total = current_total
            if not more:
                if relation != 'eq' or total != len(rows):
                    raise CBMError(f'CBM paging mismatch: received {len(rows)} rows, total {total}')
                return rows, True
            if got == 0:
                raise CBMError('CBM reported has_more with an empty page; refusing to loop')
            next_offset: int | None = res.get('next_offset')
            if next_offset is None and relation == 'gte':
                return rows, False
            if type(next_offset) is not int or next_offset != offset + got:
                raise CBMError('CBM paging offset did not advance by the returned row count')
            cursor = res.get('next_cursor') or None
            offset = next_offset
        return rows, False
