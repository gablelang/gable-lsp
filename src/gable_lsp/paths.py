"""Path and URI helpers shared by the server and the checker."""

from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote, quote, urlsplit


def normalize_path(path: str) -> str:
    """Absolute, symlink-resolved form used for path comparisons."""

    try:
        return os.path.realpath(os.path.abspath(path))
    except OSError:
        return os.path.abspath(path)


def absolute_path(path: str) -> str:
    """Absolute but not symlink-resolved; used for display and URIs."""

    return os.path.abspath(os.path.normpath(path))


def path_to_uri(path: str) -> str:
    """file:// URI for an absolute or relative filesystem path."""

    absolute = absolute_path(path)
    if os.name == "nt":  # pragma: no cover - Windows only
        p = PureWindowsPath(absolute)
        # file:///c:/path with the drive letter lower-cased as required.
        return "file:///" + quote(str(p).replace("\\", "/"), safe="/:")
    p = PurePosixPath(absolute)
    return "file://" + quote(str(p), safe="/")


def uri_to_path(uri: str) -> str:
    """Best-effort filesystem path for a file:// URI (or a bare path)."""

    if not uri.startswith("file://"):
        # Some clients are sloppy; treat anything else as a raw path.
        return unquote(uri)
    parsed = urlsplit(uri)
    path = unquote(parsed.path)
    if os.name == "nt":  # pragma: no cover - Windows only
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return path.replace("/", os.sep)
    return path


def same_path(a: str, b: str) -> bool:
    return os.path.normpath(normalize_path(a)) == os.path.normpath(normalize_path(b))
