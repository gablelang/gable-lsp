"""Map the Gable compiler CLI's diagnostic output onto LSP diagnostics.

The Gable compiler renders diagnostics for humans::

    path/to/file.gable:3:5: error: cannot assign to immutable binding 'a'
      3 |     a = 2;
        |     ^
    path/to/file.gable:2:9: note: binding declared here
      2 |     let a: i32 = 1;
        |         ^
    gable: 2 error(s) in path/to/file.gable

The header line is the only structured part of the format; everything else is
aligned, tab-expanded and windowed to 160 columns, so the border lines cannot
be trusted for exact ranges. Strategy:

* parse header lines only (``path:line:column: severity: message``);
* recover the span length from the caret underline (``^~~~``) when a caret
  line follows the header — this is optional and falls back to 1 unit;
* drop the source-line gutter and the ``gable: N error(s)`` summary lines;
* treat ``gable: error: ...`` as a file-level input error at 1:1;
* attach ``note`` diagnostics to the most recent error/warning in the same
  file as ``relatedInformation`` (the compiler prints notes right after the
  diagnostic they explain); unanchored notes become Information diagnostics.

Line/column numbers from the compiler are one-based Unicode scalar-value
columns. LSP positions are zero-based code units of the negotiated encoding,
so columns are re-based through the line text of the checked snapshot.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .encoding import DEFAULT_ENCODING, split_lines, units_of
from .paths import normalize_path

# LSP DiagnosticSeverity
SEVERITY_ERROR = 1
SEVERITY_WARNING = 2
SEVERITY_INFORMATION = 3
SEVERITY_HINT = 4

_HEADER = re.compile(
    r"^(?P<path>.*):(?P<line>[0-9]+):(?P<column>[0-9]+): "
    r"(?P<severity>error|warning|note): (?P<message>.*)$"
)
# "  3 | <source>" or "    | <source>" — the body of a gutter line.
_GUTTER = re.compile(r"^\s*[0-9]*\s*\| ?(?P<content>.*)$")
# The compiler underlines a span with "^" followed by "~" for continuation. A
# server may also face plain "^^^" from other backends; accept any run of both.
_CARET = re.compile(r"^ *(?P<mark>[\^~]+)$")
# "gable: 2 error(s) in <path>" — trailing summary, ignored.
_SUMMARY = re.compile(r"^gable: [0-9]+ error\(s\) in .*$")
# "gable: error: cannot read '<path>': <reason>" — operational input error.
_INPUT_ERROR = re.compile(r"^gable: error: (?P<message>.*)$")
_QUOTED_PATH = re.compile(r"^cannot read '(?P<path>[^']*)':")
# "<gable prelude>" and similar compiler-invented source names.
_SYNTHETIC_PATH = re.compile(r"^<[^<>]*>$")

_SEVERITY_MAP = {
    "error": SEVERITY_ERROR,
    "warning": SEVERITY_WARNING,
}


@dataclass
class RawDiagnostic:
    """A compiler diagnostic as printed, before LSP mapping."""

    path: str
    line: int          # 1-based
    column: int        # 1-based, Unicode scalar values
    severity: str      # error | warning | note
    message: str
    span_length: int = 1


@dataclass
class ParsedOutput:
    diagnostics: List[RawDiagnostic] = field(default_factory=list)
    input_error: Optional[str] = None   # message of a `gable: error:` line
    unparsed: List[str] = field(default_factory=list)


def parse_check_output(text: str) -> ParsedOutput:
    """Parse everything ``gable check`` printed to stderr/stdout."""

    result = ParsedOutput()
    current: Optional[RawDiagnostic] = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\n").rstrip("\r") if raw_line.endswith(("\n", "\r")) else raw_line
        if not line.strip():
            current = None
            continue
        header = _HEADER.match(line)
        if header:
            try:
                current = RawDiagnostic(
                    path=header.group("path"),
                    line=int(header.group("line")),
                    column=int(header.group("column")),
                    severity=header.group("severity"),
                    message=header.group("message"),
                )
            except ValueError:
                result.unparsed.append(line)
                current = None
                continue
            result.diagnostics.append(current)
            continue
        gutter = _GUTTER.match(line)
        if gutter and current is not None:
            mark = _CARET.match(gutter.group("content").rstrip())
            if mark:
                current.span_length = max(1, len(mark.group("mark")))
            continue
        if _SUMMARY.match(line):
            continue
        input_error = _INPUT_ERROR.match(line)
        if input_error:
            result.input_error = input_error.group("message")
            current = None
            continue
        # The compiler printed something we do not recognise. Keep it for the
        # logs but never fail the check over it.
        if line.startswith("gable:"):
            result.unparsed.append(line)
            current = None
            continue
        result.unparsed.append(line)
        current = None
    return result


def _input_error_path(message: str, fallback: str) -> str:
    match = _QUOTED_PATH.match(message)
    return match.group("path") if match else fallback


class LineProvider:
    """Supplies per-file line text for column conversion.

    The checked document is read from the in-memory snapshot (it may diverge
    from disk); imported files are read from disk at most once per check.
    """

    def __init__(self, encoding: str) -> None:
        self.encoding = encoding
        self._snapshots: Dict[str, List[str]] = {}
        self._cache: Dict[str, Optional[List[str]]] = {}

    def add_snapshot(self, path: str, text: str) -> None:
        self._snapshots[normalize_path(path)] = split_lines(text)

    def lines(self, path: str) -> Optional[List[str]]:
        key = normalize_path(path)
        if key in self._snapshots:
            return self._snapshots[key]
        if key in self._cache:
            return self._cache[key]
        try:
            with open(path, "r", encoding="utf-8", newline="") as handle:
                self._cache[key] = split_lines(handle.read())
        except (OSError, UnicodeDecodeError):
            self._cache[key] = None
        return self._cache[key]


def _units_of_prefix(line: str, codepoint_index: int, encoding: str) -> int:
    return units_of(line[:codepoint_index], encoding)


def diagnostic_range(
    raw: RawDiagnostic,
    line_text: Optional[str],
    encoding: str,
) -> dict:
    """LSP range for a raw diagnostic."""

    line_index = max(0, raw.line - 1)
    if line_text is None:
        # No line text available: report a zero-width position at the column.
        col = max(0, raw.column - 1)
        return {
            "start": {"line": line_index, "character": col},
            "end": {"line": line_index, "character": col + 1},
        }
    start_index = min(max(0, raw.column - 1), len(line_text))
    span = max(1, raw.span_length)
    end_index = min(start_index + span, len(line_text))
    start_units = _units_of_prefix(line_text, start_index, encoding)
    end_units = _units_of_prefix(line_text, end_index, encoding)
    return {
        "start": {"line": line_index, "character": start_units},
        "end": {"line": line_index, "character": end_units},
    }


def map_to_lsp(
    parsed: ParsedOutput,
    *,
    root_checked_path: str,
    root_uri: str,
    uri_for_path: Callable[[str], str],
    line_provider: LineProvider,
    encoding: str = DEFAULT_ENCODING,
) -> Dict[str, List[dict]]:
    """Build ``{uri: [diagnostic, ...]}`` from parsed compiler output.

    ``root_checked_path`` is the path passed to ``gable check`` (a sibling
    temp file when the buffer was unsaved); diagnostics for it are remapped
    onto ``root_uri``.
    """

    by_uri: Dict[str, List[dict]] = {}
    anchors: Dict[str, dict] = {}
    checked_normalized = normalize_path(root_checked_path)
    checked_directory = os.path.dirname(root_checked_path)

    def emit(uri: str, diagnostic: dict) -> None:
        by_uri.setdefault(uri, []).append(diagnostic)

    def classify(raw_path: str) -> Tuple[str, str]:
        """Return ``(uri, path_for_line_lookup)`` for a diagnostic's path."""

        # The compiler invents pseudo-paths for synthetic sources, e.g.
        # ``<gable prelude>`` when a program has no entry point. They are not
        # files; attribute them to the document being checked.
        if _SYNTHETIC_PATH.match(raw_path):
            return root_uri, root_checked_path
        # Relative paths are resolved against the checked file's directory,
        # which is the compiler's own base for imports.
        candidate = raw_path
        if not os.path.isabs(candidate):
            candidate = os.path.join(checked_directory, candidate)
        if normalize_path(candidate) == checked_normalized:
            return root_uri, root_checked_path
        return uri_for_path(candidate), candidate

    for raw in parsed.diagnostics:
        uri, path = classify(raw.path)
        lines = line_provider.lines(path)
        line_text = None
        if lines is not None and 0 <= raw.line - 1 < len(lines):
            line_text = lines[raw.line - 1]
        rng = diagnostic_range(raw, line_text, encoding)
        if raw.severity == "note":
            anchor = anchors.get(uri)
            if anchor is not None:
                anchor.setdefault("relatedInformation", []).append(
                    {
                        "location": {"uri": uri, "range": rng},
                        "message": raw.message,
                    }
                )
                continue
            emit(
                uri,
                {
                    "range": rng,
                    "severity": SEVERITY_INFORMATION,
                    "source": "gable",
                    "message": f"note: {raw.message}",
                },
            )
            continue
        diagnostic = {
            "range": rng,
            "severity": _SEVERITY_MAP.get(raw.severity, SEVERITY_ERROR),
            "source": "gable",
            "message": raw.message,
        }
        emit(uri, diagnostic)
        anchors[uri] = diagnostic

    if parsed.input_error:
        message = parsed.input_error
        uri, _ = classify(_input_error_path(message, root_checked_path))
        by_uri.setdefault(uri, []).append(
            {
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 1},
                },
                "severity": SEVERITY_ERROR,
                "source": "gable",
                "message": message,
            }
        )

    return by_uri

