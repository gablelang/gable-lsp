"""Server configuration.

Values arrive from three places, later sources winning:

1. command line flags (``--gable-path``, ``--check-debounce-ms``, ...),
2. ``initializationOptions.gable`` in the ``initialize`` request,
3. ``workspace/didChangeConfiguration`` notifications, which allow the editor
   to re-point ``gable.path`` at a freshly built compiler without restarting
   the server.

Settings are normalised and clamped here so that neither a typo in a JSON
config nor a hostile client can make the server spin or wait forever.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, Mapping, Optional


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


@dataclass(frozen=True)
class Settings:
    gable_path: str = "gable"
    debounce_ms: int = 150
    check_timeout_ms: int = 10_000
    format_timeout_ms: int = 5_000

    def normalized(self) -> "Settings":
        path = self.gable_path.strip() or "gable"
        return replace(
            self,
            gable_path=path,
            debounce_ms=_clamp(self.debounce_ms, 0, 5_000),
            check_timeout_ms=_clamp(self.check_timeout_ms, 100, 120_000),
            format_timeout_ms=_clamp(self.format_timeout_ms, 100, 120_000),
        )

    @classmethod
    def from_mapping(cls, mapping: Any, base: Optional["Settings"] = None) -> "Settings":
        """Merge an ``initializationOptions`` / configuration payload into base."""

        settings = base or cls()
        values: Dict[str, Any] = {}
        if isinstance(mapping, Mapping):
            # Accept both {"gable": {...}} and a flat {"gable.path": ...} form.
            nested = mapping.get("gable")
            if isinstance(nested, Mapping):
                values.update(nested)
            for key, value in mapping.items():
                if isinstance(key, str) and key.startswith("gable."):
                    values[key[len("gable."):]] = value
        path = values.get("path")
        debounce = values.get("check.debounceMs", values.get("checkDebounceMs"))
        timeout = values.get("check.timeoutMs", values.get("checkTimeoutMs"))
        fmt_timeout = values.get("format.timeoutMs", values.get("formatTimeoutMs"))
        return replace(
            settings,
            gable_path=path if isinstance(path, str) and path.strip() else settings.gable_path,
            debounce_ms=_as_int(debounce, settings.debounce_ms),
            check_timeout_ms=_as_int(timeout, settings.check_timeout_ms),
            format_timeout_ms=_as_int(fmt_timeout, settings.format_timeout_ms),
        ).normalized()

    def configuration_payload(self) -> Dict[str, Any]:
        return {
            "gable": {
                "path": self.gable_path,
                "check.debounceMs": self.debounce_ms,
                "check.timeoutMs": self.check_timeout_ms,
                "format.timeoutMs": self.format_timeout_ms,
            }
        }
