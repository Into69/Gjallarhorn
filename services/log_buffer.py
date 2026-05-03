"""In-memory ring buffer that captures Python logging records so the
UI's Logs tab can display them.

Attaches a custom Handler to the root logger; every emit pushes a
serialisable dict into a fixed-size deque. Older records age out as
new ones arrive, so the buffer's memory footprint is bounded.

The buffer is process-local — restarts wipe it. That's deliberate:
this is for live observability, not audit. Persistent logging stays
the responsibility of whatever service manager runs the app.
"""
from __future__ import annotations

import logging
from collections import deque
from threading import Lock
from typing import Optional

MAX_RECORDS = 1000


class LogBuffer:
    """Thread-safe ring buffer of log records."""

    def __init__(self, maxlen: int = MAX_RECORDS) -> None:
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._lock = Lock()
        self._next_id = 1

    def append(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        # Append the formatted traceback if the record carries exc_info,
        # so the UI can show the full stack rather than the bare message.
        if record.exc_info:
            try:
                tb = logging.Formatter().formatException(record.exc_info)
                msg = f"{msg}\n{tb}"
            except Exception:
                pass
        entry = {
            "level": record.levelname,
            "level_no": record.levelno,
            "logger": record.name,
            "message": msg,
            "ts": record.created,
        }
        with self._lock:
            entry["id"] = self._next_id
            self._next_id += 1
            self._buf.append(entry)

    def get(
        self,
        since_id: int = 0,
        min_level_no: int = logging.INFO,
        limit: int = 500,
    ) -> list[dict]:
        with self._lock:
            entries = [
                e for e in self._buf
                if e["id"] > since_id and e["level_no"] >= min_level_no
            ]
        if limit and len(entries) > limit:
            entries = entries[-limit:]
        return entries

    def clear(self) -> int:
        with self._lock:
            n = len(self._buf)
            self._buf.clear()
            return n

    def stats(self) -> dict:
        with self._lock:
            return {
                "count": len(self._buf),
                "capacity": self._buf.maxlen or 0,
                "next_id": self._next_id,
            }


class _BufferHandler(logging.Handler):
    """logging Handler that appends records to a LogBuffer. Never raises."""

    def __init__(self, buf: LogBuffer) -> None:
        super().__init__()
        self._buf = buf

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buf.append(record)
        except Exception:
            # Logging must never crash the app.
            pass


log_buffer = LogBuffer()
_installed = False


def install(level: int = logging.INFO) -> None:
    """Attach the buffer handler to the root logger. Idempotent."""
    global _installed
    if _installed:
        return
    handler = _BufferHandler(log_buffer)
    handler.setLevel(level)
    logging.getLogger().addHandler(handler)
    _installed = True
