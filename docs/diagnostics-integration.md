# Integrating Gable compiler diagnostics into LSP

This is the guide that explains, end to end, how `gable check` output becomes a
`textDocument/publishDiagnostics` payload. It documents the exact contract this
server relies on, the parsing strategy, the fields it maps, and how to replace
the CLI tap with an in-process compiler call once Gable exposes one.

If you are writing a different front end (a new editor integration, a CI
annotator, a code review bot), everything here applies to you too: the first
half is about the compiler's output, the second half about LSP payloads.

---

## 1. The pipeline in one picture

```text
 editor buffer (didOpen/didChange/didSave)
        │
        │ snapshot {uri, version, text, encoding}
        ▼
 gable-lsp checker thread ── debounce/coalesce ──► sibling temp copy of the buffer
        │                                              │
        │                                              ▼
        │                                       gable check <copy>
        │                                              │
        │            stderr: "path:line:col: severity: message" + gutter + caret
        ▼                                              │
 diagnostics.parse_check_output ◄─────────────────────┘
        │  RawDiagnostic(path, line, column, severity, message, span_length)
        ▼
 diagnostics.map_to_lsp
        │  * 1-based line/col → 0-based LSP positions
        │  * columns: Unicode scalars → negotiated code units (utf-8/16/32)
        │  * span from the caret underline, clamped to the line
        │  * note lines → relatedInformation on the preceding error
        │  * temp-file paths → the editor's document URI
        ▼
 textDocument/publishDiagnostics {uri, version, diagnostics:[...]}
```

Two properties matter and shape everything else:

1. **The buffer is the source of truth, not the file on disk.** A check must
   see unsaved edits, so the server writes the snapshot to a temporary file and
   compiles that.
2. **The compile must not block the protocol loop.** It runs on a worker
   thread; notifications are answered immediately.

---

## 2. What `gable check` actually prints

The compiler prints human-readable diagnostics to **stderr** (stdout is empty
on success and on failure). Every diagnostic occupies three kinds of lines:

```text
/proj/main.gable:3:5: error: cannot assign to immutable binding 'a'; declare it with 'var' to make it mutable
  3 |     a = 2;
    |     ^
/proj/main.gable:2:9: note: binding declared here
  2 |     let a: i32 = 1;
    |         ^
gable: 2 error(s) in /proj/main.gable
```

| line shape                          | meaning                                        |
| ----------------------------------- | ---------------------------------------------- |
| `<path>:<line>:<col>: error: <msg>` | a diagnostic header, 1-based line and column   |
| `<path>:<line>:<col>: warning: <msg>` | same, severity warning                       |
| `<path>:<line>:<col>: note: <msg>`  | supplementary note for the previous diagnostic |
| `  <n> \| <source>`                 | the source line the diagnostic refers to       |
| `    \| ^~~~`                       | the underlined span in that source line        |
| `gable: N error(s) in <path>`       | summary, not a diagnostic                      |
| `gable: error: cannot read '<path>': ...` | operational failure (file missing, ...)  |

Five details are easy to get wrong:

* **The path can contain colons.** Windows paths (`C:\src\a.gable`) and the
  absolute canonical paths the compiler prints for imported files are both
  possible. Use a greedy prefix: `^(?P<path>.*):(?P<line>\d+):(?P<column>\d+): ...`.
* **Columns are Unicode scalar values (code points), not bytes and not
  UTF-16 units.** LSP counts code units in the negotiated encoding. They are
  only equal on pure-ASCII lines.
* **The span length lives only in the caret line.** It is the number of `^`
  plus `~` characters. The compiler renders the source line with tabs expanded,
  so a caret column is a *display* column; the server only uses the caret run's
  length, not its offset. Clamp the span to the line length (a multi-byte or
  tab-containing line can render shorter than the underline).
* **Notes are separate diagnostics in the stream.** They follow the diagnostic
  they belong to and, in LSP, should be attached as `relatedInformation` rather
  than shown as independent problems.
* **Some paths are not files at all.** When a checked program has no entry
  point, the current compiler adds a diagnostic against `<gable prelude>`, its
  synthetic wrapper source. Any path shaped `<...>` is a compiler-invented
  name: it must never be turned into a `file://` URI. This server attributes
  such diagnostics to the document being checked (line 1, column 1), which is
  what the user needs to see — the alternative is a diagnostic the client
  cannot display, or worse, a URI for a file named `<gable prelude>`.

  ```text
  gable: 3 error(s) in /proj/tests/fixtures/src/immut.gable
  <gable prelude>:1:1: error: no 'main' function found; an executable requires 'fn main()' or 'fn main() -> int'
    1 | 
      | ^
  ```

  Note also that relative paths appear when the compiler is invoked with a
  relative path; they are resolved against the checked file's directory before
  being converted to a URI.

### The parser

`src/gable_lsp/diagnostics.py` implements exactly the table above:

```python
_HEADER = re.compile(
    r"^(?P<path>.*):(?P<line>[0-9]+):(?P<column>[0-9]+): "
    r"(?P<severity>error|warning|note): (?P<message>.*)$"
)
_GUTTER  = re.compile(r"^\s*[0-9]*\s*\| ?(?P<content>.*)$")
_CARET   = re.compile(r"^ *(?P<mark>[\^~]+)$")
_SUMMARY = re.compile(r"^gable: [0-9]+ error\(s\) in .*$")
_INPUT_ERROR = re.compile(r"^gable: error: (?P<message>.*)$")
```

`parse_check_output()` returns:

```python
@dataclass
class ParsedOutput:
    diagnostics: List[RawDiagnostic]   # in compiler order, notes included
    input_error: Optional[str]         # message of a `gable: error:` line
    unparsed: List[str]                # anything else: logged, never fatal
```

`RawDiagnostic` is the compiler's own vocabulary: 1-based `line`/`column`,
`severity` as a string, `message`, `path`, and `span_length` (default 1,
refined by the caret line). Keeping this intermediate form makes the compiler
output easy to test against captured fixtures and easy to replace with a
structured source (see §7).

Unrecognised lines are never an error. A compiler upgrade that adds a new line
shape degrades to "diagnostic still shown, detail dropped" instead of "language
server crashed".

---

## 3. Mapping the compiler's diagnostics onto LSP

### 3.1 Positions and ranges

LSP requires **0-based** line and character offsets:

```python
line_index = max(0, raw.line - 1)
start = min(max(0, raw.column - 1), len(line_text))
end   = min(start + max(1, raw.span_length), len(line_text))
```

`character` must be counted in the negotiated encoding. This server supports
all three LSP encodings and converts with the line's prefix:

```python
{"start": {"line": line_index, "character": units_of(line[:start], encoding)},
 "end":   {"line": line_index, "character": units_of(line[:end],   encoding)}}
```

Where `units_of` counts UTF-8 bytes, UTF-16 code units (astral characters count
2), or code points for UTF-32. The encoding is negotiated in `initialize`:
look at `capabilities.general.positionEncodings`, pick the first entry the
server supports, and report it back as `capabilities.positionEncoding`. When the
client offers nothing, **UTF-16 is the only safe default**.

The line text used for the conversion comes from a `LineProvider`:

* for the document being checked, the in-memory snapshot that was compiled;
* for imported files, the file on disk, read once per check.

Never use the live buffer for files other than the checked one — the compiler
and the provider must agree on what was compiled, or columns drift.

### 3.2 Severity, source, code

```python
SEVERITY = {"error": 1, "warning": 2}   # LSP DiagnosticSeverity
```

Notes do not become diagnostics of their own when they can be attached (see
below); a note with no preceding error in the same file becomes
`Information` (3) with a `note: ` prefix, so nothing is silently lost.

Set `source: "gable"` on every diagnostic so editors can filter by tool. If the
compiler ever emits a stable diagnostic identifier, put it in `code`.

### 3.3 Notes → `relatedInformation`

In the compiler stream, a note belongs to the error or warning printed
immediately before it. The whole point of a note like *"binding declared
here"* is that it points somewhere else. So the server anchors notes:

```text
error at 3:5 "cannot assign to immutable binding 'a'"
note  at 2:9 "binding declared here"
   ↓
{
  "range": {"start": {"line": 2, "character": 4}, "end": {...}},
  "severity": 1,
  "source": "gable",
  "message": "cannot assign to immutable binding 'a'; ...",
  "relatedInformation": [{
    "location": {"uri": "file:///proj/main.gable",
                 "range": {"start": {"line": 1, "character": 8}, "end": {...}}},
    "message": "binding declared here"
  }]
}
```

The anchor is keyed by **URI**, not by absolute position: a note is attached to
the most recent error/warning published for the same file. There is exactly one
such anchor at a time in the parser because the compiler prints the note
immediately after its diagnostic, but the keyed lookup keeps the invariant even
when output from several files is interleaved.

`relatedInformation` is supported by Neovim, Helix (shown in the diagnostic
popup), VS Code and Sublime Text's LSP plugin, so this round-trips everywhere.

### 3.4 Multi-file diagnostics

`gable check main.gable` compiles the whole import graph and reports errors in
imported files too:

```text
/proj/badtype.gable:2:12: error: type mismatch in return value: expected 'int', found 'str'
/proj/main.gable:1:1: error: cannot import 'badtype' ...
gable: 3 error(s) in /proj/main.gable
```

The server publishes one `publishDiagnostics` notification per affected URI. An
editor that has `badtype.gable` open shows the error in that buffer, even though
the user only edited `main.gable`. Be careful with retraction:

* A check of `main.gable` retracts the previous diagnostics **it** published,
  and only for URIs no other open document has since claimed.
* Diagnostics for other files are cleared by publishing an empty array for that
  URI (`{"uri": ..., "diagnostics": []}`); the LSP has no "unpublish" message.
* Closing a document clears its diagnostics and retracts the ones it published
  for imported files.

### 3.5 Versions and staleness

`publishDiagnostics` accepts an optional `version` that must be the version of
the document the diagnostics were computed for. It matters: a compile can take
longer than the user's next keystroke, and an editor that receives a stale
result will render errors on lines that no longer exist.

The checker therefore re-reads the document version after the compile and drops
the result if the buffer changed in the meantime. Publish `version` only for
the URI that was actually checked; diagnostics for imported files carry no
version because the server does not track their buffers.

---

## 4. Checking unsaved buffers safely

The compiler has no "check this text" mode; it only accepts a path. Three
strategies were considered:

| strategy | problem |
| --- | --- |
| check the real path | ignores unsaved edits — the whole point of live diagnostics |
| copy into the system temp dir | `import` paths resolve relative to the checked file, so every sibling import breaks |
| **copy next to the real file** | works: imports, relative paths and module identity are unchanged |

The sibling copy is named `.<name>.gable.lsp-check-<pid>-<counter>`, created
with `O_CREAT | O_EXCL` and mode `0600`, deleted in a `finally` block, and
regenerated per check. Notes on the details:

* The compiler has **no extension check**, so the temp name does not have to
  end in `.gable`; a dot-prefixed name keeps it out of the way (and out of most
  editors' file trees).
* The diagnostics for the temp path are remapped onto the real document URI by
  comparing `realpath()`s — never by string equality.
* If the buffer is identical to the file on disk, the real path is checked
  directly; no copy is made.
* If the directory is read-only, the server falls back to checking the file on
  disk and logs the degradation. A file with no path at all (an untitled
  buffer) is not checked.
* Compiles are debounced (default 150 ms) and coalesced latest-wins. `didOpen`
  and `didSave` skip the debounce. A per-check timeout (default 10 s) prevents
  a hung compiler from wedging the worker.

Security posture: the temp file lives in the user's own project directory and
inherits its permissions model; it is only ever written by the server, never
executed. The process runs the compiler without a shell (`subprocess.run` with
an argument list), so a file name can never be interpreted as an option or a
command.

---

## 5. Formatting via the CLI

`gable fmt <file>` rewrites the file in place and has no stdout mode. Passing
the user's real file to it would be a destructive side effect: the editor buffer
is the source of truth, the file may contain unsaved changes, and the editor is
the only party allowed to mutate a buffer. So formatting follows the same
copy-and-compare strategy:

1. write the buffer to a sibling temp file;
2. run `gable fmt <temp>`;
3. read the rewritten temp file back;
4. `difflib.SequenceMatcher` over lines **including their terminators** turns
   the difference into `TextEdit`s (this preserves CRLF, and turns a missing
   final newline into an ordinary edit);
5. delete the temp file — always, in a `finally`.

Exit code 1 means "the file does not parse": the CLI does not rewrite it, so the
server returns `[]` and leaves the buffer alone. Empty edits are also returned
when the buffer is already formatted, which is what editors expect.

The client's `FormattingOptions` (`tabSize`, `insertSpaces`) are ignored:
`gable fmt` has one canonical layout (4 spaces) and the server does not
reimplement it. If a future `gable fmt` grows `--tab-width`, thread the option
through `FormatterConfig`.

---

## 6. Protocol details worth copying

* **Never write anything but framed messages to stdout** in stdio mode. Logs go
  to stderr, or to `--log-file`.
* `Content-Length` is the byte length of the UTF-8 encoded JSON body. Count
  bytes, not characters.
* Accept a body that is not valid JSON as a **recoverable** parse error
  (`id: null`, code `-32700`); treat a truncated frame or a bad `Content-Length`
  as fatal for the stream.
* `shutdown` must be answered with `null`; `exit` must terminate the process
  with code 0 only if `shutdown` was received first, otherwise 1. Editors use
  that distinction to detect crashes.
* Requests before `initialize` get `-32002` (server not initialized). Unknown
  requests get `-32601`; unknown notifications are ignored silently.
* A handler exception becomes `-32603` for the request and must not kill the
  session.
* Declare `textDocumentSync: {openClose: true, change: 2, save: ...}` for
  incremental sync. Full sync (`change: 1`) is valid but sends the entire
  buffer on every keystroke.
* Report `save: {includeText: false}` and use the buffer already in memory;
  there is no reason to make the editor resend the text.
* `positionEncoding` belongs at the top level of `ServerCapabilities` (LSP
  3.17).

---

## 7. Upgrading from the CLI to the in-process compiler

The shell-out is a deliberate v0.3 choice: Gable ships a stable CLI but no
stable embeddable API or JSON diagnostics mode. The clean upgrade path is to
replace exactly one function:

```python
parsed = parse_check_output(combined)          # ← replace this
```

Everything after it (`map_to_lsp`, publishing, retraction, versions) is
independent of how the diagnostics were produced. The ordered steps:

1. **Add a machine-readable mode to the CLI.** Something like
   `gable check --format=json <file>` emitting an array of diagnostics with
   `{path, line, column, length, severity, message, notes:[...]}`. One line of
   JSON per diagnostic, or one document at the end; either is fine as long as
   the fields are the compiler's own (1-based, code points) and stable.
2. **Keep `RawDiagnostic` as the boundary.** Write `parse_check_json()` that
   produces the same `ParsedOutput`. Nothing else in the server changes.
3. **Drop the temp file once a source-text entry point exists.** If the
   compiler gains `gable check --source-text -` (or an `analyzeSourceText`
   C API), the buffer no longer needs to touch the filesystem. `LineProvider`
   becomes exact for the checked file *and* any virtual overlays the editor
   has open, and the sibling-copy security discussion in §4 disappears.
4. **Only then consider in-process linking.** A Python extension module around
   `gable::compile` would remove process spawn latency (~2–4 ms here) and let
   the checker publish diagnostics incrementally. Keep the worker-thread +
   version-check structure: it is what makes the server safe against stale
   results regardless of how fast the compiler is.

The reverse direction matters too: because the CLI contract is documented here
and tested against captured fixtures, a compiler change that breaks it is
caught by `tests/test_diagnostics.py` before it reaches an editor.

---

## 8. Testing the integration

`tests/fixtures/captured/` holds real compiler output, sanitised to
`/proj/...` paths, for nine representative cases: immutable assignment, type
mismatch, missing import, import cycle, cross-file error, caret span, division
by zero, quoted-string span, and an input error. `tests/test_diagnostics.py`
asserts the parsed shape and the LSP payload for each.

To re-capture fixtures against a new compiler build:

```bash
GABLE=/path/to/gable tools/capture_fixtures.sh
```

The script compiles the same snippets and rewrites absolute paths to `/proj`,
so diffs show semantic changes in the compiler, not local directory names.
`tests/test_server.py` additionally runs the real binary when `GABLE_BIN` (or
the default checkout path) exists, and skips those cases otherwise.
