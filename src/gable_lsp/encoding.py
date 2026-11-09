"""Position encoding utilities.

The Language Server Protocol counts character positions in *code units* of a
negotiated encoding. Clients may offer ``utf-8``, ``utf-16`` (the default when
nothing is negotiated) or ``utf-32``. Gable sources are Unicode, so every
position we exchange with the editor must pass through the active codec.

This module also owns the canonical line splitter used everywhere in the
server (documents, formatting edits, diagnostics mapping). Lines are split on
``\\r\\n``, ``\\r`` and ``\\n`` — the same rule Unicode and the LSP use.
"""

from __future__ import annotations

import re
from typing import List, Tuple

ENCODING_UTF8 = "utf-8"
ENCODING_UTF16 = "utf-16"
ENCODING_UTF32 = "utf-32"
SUPPORTED_ENCODINGS = (ENCODING_UTF8, ENCODING_UTF16, ENCODING_UTF32)
DEFAULT_ENCODING = ENCODING_UTF16

# The one line-breaking rule used by the whole server. Exposed (without the
# underscore-free alias) because diagnostics, formatting and the document
# store must all split text identically.
_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def split_lines(text: str) -> List[str]:
    """Split ``text`` into logical lines, without terminators.

    Matches LSP line numbering: a trailing terminator produces a final empty
    line. ``\r\n`` counts as one break; a lone ``\r`` also breaks a line.
    """

    return _LINE_BREAK.split(text)


def pick_encoding(offered: object) -> str:
    """Choose one of the client's offered position encodings.

    ``offered`` is ``capabilities.general.positionEncodings`` from the client's
    initialize params (a list of strings). The client lists its preferred
    encodings first; we pick the first one we support. Defaults to utf-16.
    """

    if isinstance(offered, list):
        for item in offered:
            if isinstance(item, str):
                lowered = item.lower()
                for supported in SUPPORTED_ENCODINGS:
                    if lowered == supported:
                        return supported
    return DEFAULT_ENCODING


def units_for_char(ch: str, encoding: str) -> int:
    """Number of code units a single Unicode scalar value occupies."""

    if encoding == ENCODING_UTF8:
        return len(ch.encode("utf-8"))
    if encoding == ENCODING_UTF32:
        return 1
    # utf-16
    return 2 if ord(ch) > 0xFFFF else 1


def units_of(text: str, encoding: str) -> int:
    """Number of code units ``text`` occupies."""

    if encoding == ENCODING_UTF8:
        return len(text.encode("utf-8"))
    if encoding == ENCODING_UTF32:
        return len(text)
    units = 0
    for ch in text:
        units += 2 if ord(ch) > 0xFFFF else 1
    return units


class LineIndex:
    """Line/offset mapping for one text snapshot.

    ``lines`` are the logical lines *without* their terminators (LSP view):
    ``split_lines("a\\r\\nb\\r\\n") == ["a", "b", ""]`` — a trailing newline
    implies a final empty line, exactly like ``str.splitlines``. This is the
    universal line view the protocol expects.
    """

    def __init__(self, text: str, encoding: str = DEFAULT_ENCODING) -> None:
        self.encoding = encoding
        self.text = text
        self._lines: List[str] = split_lines(text)
        # Codepoint offset of the first character of every line.
        self._starts: List[int] = []
        offset = 0
        for i, line in enumerate(self._lines):
            self._starts.append(offset)
            offset += len(line)
            if i < len(self._lines) - 1:
                match = _LINE_BREAK.match(text, offset)
                assert match is not None  # guaranteed by the split above
                offset += match.end() - match.start()
        self._line_units: List[int] = [units_of(line, encoding) for line in self._lines]

    # ------------------------------------------------------------------
    @property
    def line_count(self) -> int:
        return len(self._lines)

    def line_text(self, line: int) -> str:
        if line < 0 or line >= len(self._lines):
            return ""
        return self._lines[line]

    def line_units(self, line: int) -> int:
        if line < 0 or line >= len(self._line_units):
            return 0
        return self._line_units[line]

    def line_starts(self) -> List[int]:
        return list(self._starts)

    def units_to_index(self, line: int, character: int) -> int:
        """Codepoint index within ``line`` for a code-unit column."""

        if character <= 0:
            return 0
        units = 0
        index = 0
        for ch in self._lines[line]:
            step = units_for_char(ch, self.encoding)
            if units + step > character:
                # Positions inside a surrogate pair round down to its start;
                # the LSP does not define a position within one.
                return index
            units += step
            index += 1
        return index

    def index_to_units(self, line: int, index: int) -> int:
        """Code-unit column within ``line`` for a codepoint index."""

        if index <= 0:
            return 0
        units = 0
        for ch in self._lines[line][:index]:
            units += units_for_char(ch, self.encoding)
        return units

    def position_at(self, offset: int) -> Tuple[int, int]:
        """(line, character-units) of a codepoint ``offset`` into the text."""

        offset = max(0, min(offset, len(self.text)))
        # Binary search for the last line starting at or before offset.
        lo, hi = 0, len(self._starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        line = lo
        index = offset - self._starts[line]
        # The offset may point past the end of the line content (e.g. exactly
        # on a terminator). Clamp to the end of the logical line.
        index = min(index, len(self._lines[line]))
        return line, self.index_to_units(line, index)

    def offset_at(self, line: int, character: int) -> int:
        """Codepoint offset of an LSP (line, character-units) position."""

        if line < 0:
            return 0
        if line >= self.line_count:
            return len(self.text)
        index = self.units_to_index(line, character)
        return self._starts[line] + index

    # Host models ----------------------------------------------------------
    def line_range(self, line: int) -> Tuple[int, int]:
        """(start_offset, end_offset) of a line including its terminator."""

        if line < 0 or line >= self.line_count:
            return len(self.text), len(self.text)
        start = self._starts[line]
        if line + 1 < self.line_count:
            return start, self._starts[line + 1]
        return start, len(self.text)


def split_line_offsets(text: str) -> List[Tuple[int, int, int]]:
    """Split ``text`` into ``(content_start, content_end, line_end)`` tuples.

    ``content_end`` excludes the terminator; ``line_end`` includes it. The
    last entry may have ``content_end == line_end == len(text)``. Used by the
    formatter to slice replacement blocks that preserve exact terminators.
    """

    result: List[Tuple[int, int, int]] = []
    offset = 0
    for match in _LINE_BREAK.finditer(text):
        result.append((offset, match.start(), match.end()))
        offset = match.end()
    result.append((offset, len(text), len(text)))
    return result
