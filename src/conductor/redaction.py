"""Run-scoped secret redaction primitives.

This module provides :class:`RunRedactor` for sanitizing registered secret
values from event streams, dashboard state, checkpoints, logs, terminal
records, and diagnostics across Conductor workflows.

The redactor performs non-mutating recursive scrubbing over data structures,
replacing secret occurrences with ``***redacted***``. Values are matched
longest-first so overlapping secrets leave no partial remnants.

Non-goals:
    * **Transformed values**: Encoded (e.g. base64, URL-encoded), hashed,
      split, or otherwise transformed secrets are not recognized or matched.
      Redaction matches exact character and byte sequences only.
    * **Multi-threaded synchronization**: Like :class:`WorkflowEventEmitter`,
      :class:`RunRedactor` is designed for use within a single workflow run
      execution context / async loop and does not perform internal locking.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextvars import ContextVar, Token
from typing import Any

__all__ = [
    "REDACTED_MARKER",
    "RunRedactor",
    "current",
    "reset_current",
    "set_current",
]

REDACTED_MARKER: str = "***redacted***"

_SHORT_SECRET_THRESHOLD: int = 8


class RunRedactor:
    """Run-scoped secret redactor for scrubbing sensitive strings and bytes.

    Maintains a set of registered secret strings and provides non-mutating
    recursive scrubbing over nested data structures (dicts, lists, tuples,
    strings, and bytes).

    Args:
        on_short: Optional callback invoked with the character length whenever
            a secret shorter than 8 characters is registered. Used to surface
            advisory warnings for short secrets that may cause accidental
            over-redaction.
    """

    def __init__(self, on_short: Callable[[int], None] | None = None) -> None:
        self._on_short = on_short
        self._values: set[str] = set()
        self._sorted_values: list[str] = []
        self._sorted_bytes: list[bytes] = []

    def register(self, values: Iterable[str]) -> None:
        """Register secret values to be redacted.

        Empty strings are skipped. Each value is stored once. If a newly
        registered value is shorter than 8 characters and an ``on_short``
        callback was provided at construction, the callback is invoked with
        the value's length.

        Args:
            values: An iterable of secret string values.
        """
        changed = False
        for val in values:
            if not isinstance(val, str) or not val:
                continue
            if val not in self._values:
                if self._on_short is not None and len(val) < _SHORT_SECRET_THRESHOLD:
                    self._on_short(len(val))
                self._values.add(val)
                changed = True

        if changed:
            self._sorted_values = sorted(self._values, key=lambda s: len(s), reverse=True)
            self._sorted_bytes = [v.encode("utf-8") for v in self._sorted_values]

    def clear(self) -> None:
        """Clear all registered secrets."""
        self._values.clear()
        self._sorted_values.clear()
        self._sorted_bytes.clear()

    @property
    def active(self) -> bool:
        """Whether the redactor has registered secret values.

        Callers should check this property to avoid unnecessary scrubbing
        overhead when no secrets are configured.
        """
        return bool(self._values)

    def scrub(self, obj: Any) -> Any:
        """Recursively scrub registered secrets from *obj* without mutation.

        Scrubbing rules:
            * ``str``: Substrings matching registered values are replaced with
              ``***redacted***`` longest-first.
            * ``bytes``: UTF-8 byte sequences matching registered values are
              replaced with ``b"***redacted***"`` longest-first.
            * ``dict``: Returns a new dictionary with recursively scrubbed
              keys and values.
            * ``list``: Returns a new list with recursively scrubbed elements.
            * ``tuple``: Returns a new tuple with recursively scrubbed elements.
            * Other types (int, float, bool, None, custom objects): Returned
              as-is without copying.

        Args:
            obj: The object to scrub.

        Returns:
            A sanitized copy of *obj*, or *obj* unmodified if inactive or of
            an unscrubbed type.
        """
        if not self.active:
            return obj

        if isinstance(obj, str):
            result_str = obj
            for val in self._sorted_values:
                result_str = result_str.replace(val, REDACTED_MARKER)
            return result_str

        if isinstance(obj, bytes):
            result_bytes = obj
            marker_bytes = REDACTED_MARKER.encode("utf-8")
            for val_bytes in self._sorted_bytes:
                result_bytes = result_bytes.replace(val_bytes, marker_bytes)
            return result_bytes

        if isinstance(obj, dict):
            return {self.scrub(k): self.scrub(v) for k, v in obj.items()}

        if isinstance(obj, list):
            return [self.scrub(item) for item in obj]

        if isinstance(obj, tuple):
            return tuple(self.scrub(item) for item in obj)

        return obj

    def scrub_event_data(self, data: dict[str, Any]) -> dict[str, Any]:
        """Scrub secret values from an event payload dictionary.

        Thin wrapper around :meth:`scrub` specifically typed for event data.

        Args:
            data: The event payload mapping.

        Returns:
            A sanitized dictionary.
        """
        if not self.active:
            return data
        scrubbed = self.scrub(data)
        if isinstance(scrubbed, dict):
            return scrubbed
        return data

    def __repr__(self) -> str:
        """Safe representation showing only active status and secret count."""
        return f"<RunRedactor active={self.active} count={len(self._values)}>"

    def __str__(self) -> str:
        """Safe string representation showing only secret count."""
        return f"RunRedactor(count={len(self._values)})"


_CURRENT_REDACTOR: ContextVar[RunRedactor | None] = ContextVar(
    "conductor_current_redactor", default=None
)


def current() -> RunRedactor | None:
    """Get the active run-scoped redactor from the current context."""
    return _CURRENT_REDACTOR.get()


def set_current(redactor: RunRedactor | None) -> Token[RunRedactor | None]:
    """Set the active run-scoped redactor for the current context.

    Args:
        redactor: The redactor instance to activate, or None to clear.

    Returns:
        A contextvars :class:`~contextvars.Token` to be passed to
        :func:`reset_current`.
    """
    return _CURRENT_REDACTOR.set(redactor)


def reset_current(token: Token[RunRedactor | None]) -> None:
    """Reset the active run-scoped redactor using the provided token.

    Args:
        token: The token returned by a previous :func:`set_current` call.
    """
    _CURRENT_REDACTOR.reset(token)
