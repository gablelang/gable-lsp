"""``textDocument/formatting`` through the ``gable fmt`` command.

The released CLI has no stdout mode (``gable fmt`` rewrites files in place),
and a language server must never rewrite the user's buffer behind the
editor's back. The strategy is therefore:

1. copy the in-memory buffer to an exclusive sibling dotfile;
2. run ``gable fmt`` on the copy — its atomic rewrite only ever touches the
   copy;
3. diff copy-before against copy-after and return the difference as LSP
   ``TextEdit`` objects for the editor to apply to the live buffer;
4. delete the copy.

A file that does not parse is never reformatted by the compiler (exit code
1), so formatting is simply a no-op then, matching the CLI's own contract.
If the on-disk document already equals the buffer, the on-disk path is
formatted through a copy as well — the real file is never passed to
``gable fmt`` by this server.
"""

from __future__ import annotations

import bisect
import difflib
import errno
import os
import re
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .encoding import split_line_offsets, units_of
from .log import get_logger
from .paths import absolute_path

logger = get_logger(__name__)

# One "line" including its terminator; the last line may omit the terminator.
_LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+")


@dataclass
class FormatterConfig:
    gable_path: str = "gable"
    timeout_ms: int = 5_000


def _position(offset: int, offsets: List[Tuple[int, int, int]], text: str, encoding: str) -> dict:
    """Zero-based LSP position for a raw ``offset`` in ``text``."""

    starts = [o[0] for o in offsets]
    idx = bisect.bisect_left(starts, offset)
    if idx < len(starts) and starts[idx] == offset:
        return {"line": idx, "character": 0}
    idx -= 1
    line_start, content_end, _break_end = offsets[idx]
    prefix = text[line_start:min(offset, content_end)]
    return {"line": idx, "character": units_of(prefix, encoding)}


def diff_edits(old_text: str, new_text: str, encoding: str) -> List[dict]:
    """Line-based LSP TextEdits turning ``old_text`` into ``new_text``.

    Lines keep their terminators as part of the compared unit, so a CRLF
    buffer stays CRLF, and adding or removing a final newline becomes an
    ordinary edit. Edits are non-overlapping and in document order.
    """

    if old_text == new_text:
        return []
    old_parts = _LINE_RE.findall(old_text)
    new_parts = _LINE_RE.findall(new_text)
    old_offsets = split_line_offsets(old_text)
    old_starts = [0]
    for part in old_parts[:-1]:
        old_starts.append(old_starts[-1] + len(part))
    new_starts = [0]
    for part in new_parts[:-1]:
        new_starts.append(new_starts[-1] + len(part))

    matcher = difflib.SequenceMatcher(a=old_parts, b=new_parts, autojunk=False)
    edits: List[dict] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        start_off = old_starts[i1] if i1 < len(old_starts) else len(old_text)
        end_off = old_starts[i2] if i2 < len(old_starts) else len(old_text)
        new_start = new_starts[j1] if j1 < len(new_starts) else len(new_text)
        new_end = new_starts[j2] if j2 < len(new_starts) else len(new_text)
        edits.append(
            {
                "range": {
                    "start": _position(start_off, old_offsets, old_text, encoding),
                    "end": _position(end_off, old_offsets, old_text, encoding),
                },
                "newText": new_text[new_start:new_end],
            }
        )
    return edits


def format_buffer(
    text: str,
    *,
    real_path: Optional[str],
    config: FormatterConfig,
    encoding: str,
) -> Optional[List[dict]]:
    """Format ``text`` and return TextEdits, or ``None`` if unavailable."""

    directory = os.path.dirname(absolute_path(real_path)) if real_path else None
    base = os.path.basename(real_path) if real_path else "untitled.gable"
    if not directory or not os.path.isdir(directory):
        logger.info("formatting: document has no writable sibling directory")
        return None
    temp_path = _write_temp(directory, base, text)
    if temp_path is None:
        logger.info("formatting: could not create temp file next to %s", real_path)
        return None
    try:
        try:
            completed = subprocess.run(
                [config.gable_path, "fmt", temp_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=max(0.1, config.timeout_ms / 1000.0),
                check=False,
            )
        except FileNotFoundError:
            logger.warning("formatting: gable binary not found at %r", config.gable_path)
            return None
        except subprocess.TimeoutExpired:
            logger.warning("formatting: gable fmt timed out after %d ms", config.timeout_ms)
            return None
        except OSError as exc:
            logger.warning("formatting: could not run gable fmt: %s", exc)
            return None
        if completed.returncode != 0:
            # The file does not parse or the compiler refused; leave the
            # buffer untouched (the CLI never rewrites invalid files either).
            stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
            logger.info("formatting: gable fmt did not rewrite %s (exit %d)\n%s",
                        temp_path, completed.returncode, stderr.rstrip())
            return None
        try:
            with open(temp_path, "r", encoding="utf-8", newline="") as handle:
                formatted = handle.read()
        except (OSError, UnicodeDecodeError):
            return None
        return diff_edits(text, formatted, encoding)
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            logger.warning("formatting: could not remove temp file %s", temp_path)


_counter = 0


def _write_temp(directory: str, base: str, text: str) -> Optional[str]:
    global _counter
    for _ in range(100):
        _counter += 1
        candidate = os.path.join(
            directory, f".{base}.lsp-format-{os.getpid()}-{_counter}"
        )
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                continue
            return None
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
        except OSError:
            try:
                os.unlink(candidate)
            except OSError:
                pass
            return None
        return candidate
    return None
