"""Background diagnostics through the ``gable`` CLI.

Edits are checked on a worker thread so the protocol loop never blocks on a
compilation. Requests are debounced and coalesced latest-wins: typing bursts
collapse into a single check of the newest buffer. Diagnostics are published
only when the checked version is still the current one, so stale results are
never shown.

Unsaved buffers are checked by copying the buffer text to a sibling dotfile
next to the document (``.<name>.gable.lsp-check-<pid>-<n>``). The compiler
resolves ``import`` paths relative to the file being checked, so a temp file
in the system temp directory would break every sibling import; a sibling temp
file keeps import resolution byte-for-byte identical to checking the real
file. The temp file is created exclusively, and unconditionally deleted when
the check finishes. If the document matches the on-disk file, or the sibling
copy cannot be created, the real path is checked instead.
"""

from __future__ import annotations

import errno
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple

from .diagnostics import LineProvider, map_to_lsp, parse_check_output
from .log import get_logger
from .paths import absolute_path, path_to_uri

logger = get_logger(__name__)


@dataclass
class CheckSnapshot:
    uri: str
    path: Optional[str]        # filesystem path, when the document has one
    text: str
    version: int
    encoding: str


@dataclass
class CheckerConfig:
    gable_path: str = "gable"
    debounce_ms: int = 150
    timeout_ms: int = 10_000


class DiagnosticChecker:
    """Owns the worker thread, the pending queue and the publish diffing."""

    def __init__(
        self,
        snapshot_provider: Callable[[str], Optional[CheckSnapshot]],
        publish: Callable[[str, List[dict], Optional[int]], None],
        config: CheckerConfig,
        notify_config_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._snapshot = snapshot_provider
        self._publish = publish
        self.config = config
        self._notify_config_error = notify_config_error
        self._cv = threading.Condition()
        self._pending: Dict[str, float] = {}
        self._thread: Optional[threading.Thread] = None
        self._stopping = False
        # root uri -> set of uris its latest check published diagnostics for
        self._published: Dict[str, Set[str]] = {}
        # uri -> root uri that most recently published diagnostics for it
        self._owner: Dict[str, str] = {}
        self._temp_counter = 0
        self._config_error_reported = False

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="gable-lsp-checker", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._cv:
            self._stopping = True
            self._pending.clear()
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # ------------------------------------------------------------------
    def schedule(self, uri: str, *, debounce: bool = True) -> None:
        with self._cv:
            delay = (self.config.debounce_ms / 1000.0) if debounce else 0.0
            self._pending[uri] = time.monotonic() + delay
            self._cv.notify_all()

    def cancel(self, uri: str) -> None:
        with self._cv:
            self._pending.pop(uri, None)

    def cancel_all(self) -> None:
        with self._cv:
            self._pending.clear()

    def clear(self, uri: str) -> None:
        """Forget a document: drop pending work and clear its diagnostics."""

        with self._cv:
            self._pending.pop(uri, None)
            stale = [
                published
                for published in self._published.pop(uri, set())
                if self._owner.get(published) == uri
            ]
            for published in stale:
                self._owner.pop(published, None)
            self._owner.pop(uri, None)
        self._publish(uri, [], None)
        for published in stale:
            self._publish(published, [], None)

    # ------------------------------------------------------------------
    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._stopping:
                    now = time.monotonic()
                    due = [uri for uri, deadline in self._pending.items() if deadline <= now]
                    if due:
                        break
                    if not self._pending:
                        self._cv.wait()
                        continue
                    next_deadline = min(self._pending.values())
                    self._cv.wait(timeout=max(0.0, next_deadline - now))
                else:
                    return
                if self._stopping:
                    return
                uri = min(due, key=lambda u: self._pending[u])
                self._pending.pop(uri, None)
            try:
                self._check(uri)
            except Exception:  # noqa: BLE001 - the checker must never die
                logger.exception("diagnostic check failed for %s", uri)

    # ------------------------------------------------------------------
    def _check(self, uri: str) -> None:
        snapshot = self._snapshot(uri)
        if snapshot is None:
            return
        try:
            check_path, temp_path = self._prepare_check_file(snapshot)
        except _NoCheckPath:
            logger.debug("document %s has no filesystem path; skipping check", uri)
            return
        try:
            completed = self._run_gable(check_path)
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    logger.warning("could not remove temp check file %s", temp_path)
        if completed is None:
            return  # spawn failure already reported

        # Drop the result if the document moved on while we were compiling.
        current = self._snapshot(uri)
        if current is None or current.version != snapshot.version:
            logger.debug("discarding stale diagnostics for %s (version changed)", uri)
            return

        stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
        stdout = (completed.stdout or b"").decode("utf-8", errors="replace")
        combined = stderr + (("\n" + stdout) if stdout else "")
        parsed = parse_check_output(combined)
        for line in parsed.unparsed:
            logger.debug("unrecognised compiler output: %s", line)
        if completed.returncode not in (0, 1):
            logger.warning(
                "gable check exited with %d for %s; stderr follows\n%s",
                completed.returncode,
                check_path,
                combined.rstrip(),
            )
            return

        provider = LineProvider(snapshot.encoding)
        provider.add_snapshot(check_path, snapshot.text)

        def uri_for_path(path: str) -> str:
            return path_to_uri(path)

        mapped = map_to_lsp(
            parsed,
            root_checked_path=check_path,
            root_uri=uri,
            uri_for_path=uri_for_path,
            line_provider=provider,
            encoding=snapshot.encoding,
        )
        # The checked document always gets a publish, even when clean, so a
        # previously reported error is retracted after the user fixes it.
        mapped.setdefault(uri, [])
        self._publish_map(uri, mapped, snapshot.version)

    # ------------------------------------------------------------------
    def _prepare_check_file(self, snapshot: CheckSnapshot) -> Tuple[str, Optional[str]]:
        """Return ``(path passed to gable, temp file to clean up)``."""

        real_path = absolute_path(snapshot.path) if snapshot.path else None
        if real_path:
            try:
                # newline="" keeps CRLF/CR intact, so the comparison is
                # against the exact bytes the compiler will see.
                with open(real_path, "r", encoding="utf-8", newline="") as handle:
                    disk_text = handle.read()
                if disk_text == snapshot.text:
                    return real_path, None
            except (OSError, UnicodeDecodeError):
                pass  # missing or unreadable file: buffer is newer, use temp
            temp = self._write_sibling_temp(real_path, snapshot.text)
            if temp is not None:
                return temp, temp
            logger.info(
                "could not create sibling temp file for %s; checking the on-disk version",
                real_path,
            )
            return real_path, None
        # No path (e.g. an untitled buffer): nothing sensible to check.
        raise _NoCheckPath(snapshot.uri)

    def _write_sibling_temp(self, real_path: str, text: str) -> Optional[str]:
        directory = os.path.dirname(real_path)
        base = os.path.basename(real_path)
        for _ in range(100):
            self._temp_counter += 1
            candidate = os.path.join(
                directory, f".{base}.lsp-check-{os.getpid()}-{self._temp_counter}"
            )
            try:
                fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except OSError as exc:
                if exc.errno in (errno.EEXIST,):
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

    def _run_gable(self, check_path: str) -> Optional[subprocess.CompletedProcess]:
        try:
            return subprocess.run(
                [self.config.gable_path, "check", check_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=max(0.1, self.config.timeout_ms / 1000.0),
                check=False,
            )
        except FileNotFoundError:
            self._report_config_error(
                f"the Gable compiler was not found at {self.config.gable_path!r}. "
                "Diagnostics are disabled until 'gable.path' points at the gable binary."
            )
            return None
        except subprocess.TimeoutExpired:
            logger.warning("gable check timed out after %d ms", self.config.timeout_ms)
            return None
        except OSError as exc:
            self._report_config_error(f"could not run the Gable compiler: {exc}")
            return None

    # ------------------------------------------------------------------
    def _publish_map(self, root_uri: str, mapped: Dict[str, List[dict]], version: int) -> None:
        """Publish one check's diagnostics and retract that check's stale ones.

        Retraction is scoped to the root document: a check of ``a.gable`` may
        report errors in an imported ``b.gable``, and a later check of an
        unrelated document must not clear them. ``_owner`` resolves the case
        where several open documents report on the same imported file — the
        most recent publisher wins, and older roots do not clear it.
        """

        with self._cv:
            previous = self._published.get(root_uri, set())
            self._published[root_uri] = set(mapped)
            for uri in mapped:
                self._owner[uri] = root_uri
            retract = [
                uri for uri in previous
                if uri not in mapped and self._owner.get(uri) == root_uri
            ]
            for uri in retract:
                self._owner.pop(uri, None)
        for uri, diagnostics in mapped.items():
            diagnostics.sort(key=lambda d: (d["range"]["start"]["line"], d["range"]["start"]["character"]))
            self._publish(uri, diagnostics, version if uri == root_uri else None)
        for uri in retract:
            self._publish(uri, [], None)

    def _report_config_error(self, message: str) -> None:
        logger.error("%s", message)
        if not self._config_error_reported and self._notify_config_error is not None:
            self._config_error_reported = True
            self._notify_config_error(message)


class _NoCheckPath(Exception):
    pass
