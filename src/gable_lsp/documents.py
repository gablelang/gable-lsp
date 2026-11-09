"""Open-document store with full and incremental synchronisation.

LSP positions are expressed in code units of the negotiated encoding; the
store converts them to codepoint offsets through :class:`LineIndex`. All
incoming ranges are validated defensively — a buggy client must not be able
to corrupt the buffer or crash the server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .encoding import LineIndex


class DocumentSyncError(ValueError):
    """Raised when a change cannot be applied (bad range or version)."""


@dataclass
class TextDocument:
    uri: str
    language_id: str
    version: int
    text: str
    encoding: str = "utf-16"
    _index: Optional[LineIndex] = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._index = LineIndex(self.text, self.encoding)

    # ------------------------------------------------------------------
    @property
    def index(self) -> LineIndex:
        assert self._index is not None
        return self._index

    def set_encoding(self, encoding: str) -> None:
        self.encoding = encoding
        self._index = LineIndex(self.text, encoding)

    def line_text(self, line: int) -> str:
        return self.index.line_text(line)

    def offset_at(self, line: int, character: int) -> int:
        return self.index.offset_at(line, character)

    def position_at(self, offset: int) -> Tuple[int, int]:
        return self.index.position_at(offset)

    # ------------------------------------------------------------------
    def apply_full(self, text: str, version: Optional[int]) -> None:
        self.text = text
        if version is not None:
            self.version = version
        self._index = LineIndex(self.text, self.encoding)

    def apply_incremental(self, changes: List[dict], version: Optional[int]) -> None:
        """Apply LSP incremental content changes, in order."""

        for change in changes:
            text = change.get("text", "")
            rng = change.get("range")
            if rng is None:
                self.apply_full(text, None)
                continue
            start = rng.get("start") or {}
            end = rng.get("end") or {}
            start_off = self.index.offset_at(
                int(start.get("line", 0)), int(start.get("character", 0))
            )
            end_off = self.index.offset_at(
                int(end.get("line", 0)), int(end.get("character", 0))
            )
            if end_off < start_off:
                raise DocumentSyncError(
                    f"change range is inverted: {start} -> {end}"
                )
            self.text = self.text[:start_off] + text + self.text[end_off:]
            self._index = LineIndex(self.text, self.encoding)
        if version is not None:
            self.version = version

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TextDocument(uri={self.uri!r}, version={self.version}, bytes={len(self.text)})"


class DocumentStore:
    """Open documents keyed by URI."""

    def __init__(self) -> None:
        self._documents: Dict[str, TextDocument] = {}

    def open(self, uri: str, language_id: str, version: int, text: str) -> TextDocument:
        document = TextDocument(uri, language_id, version, text)
        self._documents[uri] = document
        return document

    def get(self, uri: str) -> Optional[TextDocument]:
        return self._documents.get(uri)

    def close(self, uri: str) -> Optional[TextDocument]:
        return self._documents.pop(uri, None)

    def change(
        self,
        uri: str,
        changes: List[dict],
        version: Optional[int],
    ) -> Optional[TextDocument]:
        document = self._documents.get(uri)
        if document is None:
            return None
        document.apply_incremental(changes, version)
        return document

    def save(
        self,
        uri: str,
        text: Optional[str] = None,
    ) -> Optional[TextDocument]:
        document = self._documents.get(uri)
        if document is None:
            return None
        if text is not None:
            document.apply_full(text, None)
        return document

    def set_encoding(self, encoding: str) -> None:
        for document in self._documents.values():
            document.set_encoding(encoding)

    def open_uris(self) -> List[str]:
        return list(self._documents)

    def __len__(self) -> int:
        return len(self._documents)
