"""Command line entry point for gable-lsp."""

from __future__ import annotations

import argparse
import signal
import sys
from dataclasses import replace
from typing import Optional, Sequence

from . import __version__
from .log import configure_logging, get_logger
from .settings import Settings
from .transport import serve_stdio, serve_tcp

logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gable-lsp",
        description="Language Server Protocol server for the Gable language.",
        epilog=(
            "The server speaks LSP over stdio by default (what Neovim, Helix, "
            "VS Code and Sublime Text launch). Use --tcp to listen on a socket."
        ),
    )
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument(
        "--stdio",
        action="store_true",
        help="serve LSP over stdin/stdout (default)",
    )
    transport.add_argument(
        "--tcp",
        action="store_true",
        help="listen on a TCP socket instead of stdio",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="interface to bind with --tcp (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9257,
        help="TCP port with --tcp; 0 picks a free port (default: 9257)",
    )
    parser.add_argument(
        "--tcp-listen",
        action="store_true",
        help="with --tcp, keep accepting clients after the first session ends",
    )
    parser.add_argument(
        "--gable-path",
        default=None,
        metavar="PATH",
        help="path to the gable compiler binary (default: 'gable' from PATH)",
    )
    parser.add_argument(
        "--check-debounce-ms",
        type=int,
        default=None,
        metavar="MS",
        help="quiet period before checking a changed buffer (default: 150)",
    )
    parser.add_argument(
        "--check-timeout-ms",
        type=int,
        default=None,
        metavar="MS",
        help="per-check compiler timeout (default: 10000)",
    )
    parser.add_argument(
        "--format-timeout-ms",
        type=int,
        default=None,
        metavar="MS",
        help="per-formatting compiler timeout (default: 5000)",
    )
    parser.add_argument(
        "--log-level",
        default="warning",
        choices=("debug", "info", "warning", "error", "critical"),
        help="log verbosity, written to stderr (default: warning)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="write logs to a file instead of stderr",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    """Apply CLI overrides, leaving unspecified values at their defaults."""

    overrides = {}
    if args.gable_path:
        overrides["gable_path"] = args.gable_path
    if args.check_debounce_ms is not None:
        overrides["debounce_ms"] = args.check_debounce_ms
    if args.check_timeout_ms is not None:
        overrides["check_timeout_ms"] = args.check_timeout_ms
    if args.format_timeout_ms is not None:
        overrides["format_timeout_ms"] = args.format_timeout_ms
    return replace(Settings(), **overrides).normalized()


def entrypoint(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level, args.log_file)
    settings = settings_from_args(args)
    logger.info("gable-lsp %s starting (gable: %s)", __version__, settings.gable_path)

    # SIGTERM/SIGINT in TCP mode should end the listener rather than dump a
    # traceback; in stdio mode the editor closes the pipe and the loop exits.
    def _terminate(signum, frame):  # pragma: no cover - signal path
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except (ValueError, OSError):  # pragma: no cover - non-main thread
        pass

    try:
        return _run(args, settings)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        logger.info("interrupted")
        return 1


def _run(args: argparse.Namespace, settings: Settings) -> int:
    if args.tcp:
        try:
            host, port = args.host, int(args.port)
        except (TypeError, ValueError):
            logger.error("--port must be an integer")
            return 1
        return serve_tcp(settings, host=host, port=port, accept_forever=args.tcp_listen)
    return serve_stdio(settings)


def main() -> None:  # pragma: no cover - console script wrapper
    sys.exit(entrypoint())
