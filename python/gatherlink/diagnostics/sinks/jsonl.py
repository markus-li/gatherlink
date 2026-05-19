"""
JSONL diagnostics sink.

JSONL is the first durable diagnostics format because it is easy to append,
tail, archive, and feed into later tooling without changing the event DTO.
"""

from __future__ import annotations

from pathlib import Path
from typing import TextIO

from gatherlink.diagnostics.events import DiagnosticEvent


class JsonlDiagnosticSink:
    """Append normalized diagnostics events to a newline-delimited JSON file."""

    def __init__(self, file_path: Path | str, *, flush: bool = True) -> None:
        self.file_path = Path(file_path)
        self.flush = flush
        self._handle: TextIO | None = None

    def __enter__(self) -> JsonlDiagnosticSink:
        """Open the sink when entering a context manager."""
        self.open()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        """Close the sink when leaving a context manager."""
        self.close()

    def open(self) -> None:
        """Open the sink for append, creating parent directories as needed."""
        if self._handle is not None:
            return
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.file_path.open("a", encoding="utf-8")

    def close(self) -> None:
        """Close the file handle if it is open."""
        if self._handle is None:
            return
        self._handle.close()
        self._handle = None

    def write(self, event: DiagnosticEvent) -> None:
        """Append one event as compact JSON plus a newline."""
        if self._handle is None:
            self.open()
        if self._handle is None:
            raise RuntimeError("JSONL diagnostics sink did not open")
        self._handle.write(event.model_dump_json(exclude_none=True))
        self._handle.write("\n")
        if self.flush:
            self._handle.flush()
