"""Real-pty integration tests for KeyboardListener terminal restore (issue #290).

These tests allocate a genuine pseudo-terminal via ``pty.openpty()`` and point
fd 0 (and ``sys.stdin``) at its slave so the production ``KeyboardListener``
lifecycle (``termios.tcgetattr`` / ``tty.setcbreak`` / ``termios.tcsetattr``)
runs against a real tty instead of mocks. This is the only reliable way to
reproduce the bug where a listener started on an already-cbreak terminal
snapshots cbreak as its "original" settings and later restores them.

Expected state on unfixed code (TDD red):

- ``test_full_lifecycle_restores_terminal``            — PASSES (regression guard)
- ``test_second_listener_does_not_capture_cbreak``     — FAILS
- ``test_double_start_same_instance_does_not_corrupt_baseline`` — FAILS
"""

from __future__ import annotations

import sys

import pytest

if sys.platform == "win32":
    # Module-level skip must run BEFORE importing pty/termios, which do not
    # exist on Windows — otherwise collection fails instead of skipping.
    pytest.skip("PTY tests are Unix-only", allow_module_level=True)

import asyncio  # noqa: E402
import contextlib  # noqa: E402
import io  # noqa: E402
import os  # noqa: E402
import pty  # noqa: E402
import termios  # noqa: E402
from collections.abc import Iterator  # noqa: E402

from conductor.interrupt.listener import (  # noqa: E402
    KeyboardListener,
    restore_terminal_baseline,
)


class _PtyStdinShim:
    """Minimal stdin replacement that satisfies the KeyboardListener contract.

    pytest's capture replaces ``sys.stdin`` with a non-tty object, so the
    listener's reader thread (which calls ``sys.stdin.buffer.read(1)``) and
    ``select.select([sys.stdin], ...)`` both need a shim that:

    - reports ``isatty()`` as True,
    - returns fd 0 from ``fileno()`` (fd 0 is dup2'd at the pty slave),
    - exposes ``.buffer`` as a real buffered binary reader on fd 0.
    """

    def __init__(self) -> None:
        # Duplicate fd 0 so the shim owns its own handle to the pty slave and
        # closing the shim cannot disturb fd 0 itself.
        self._owned_fd = os.dup(0)
        self.buffer = io.BufferedReader(io.FileIO(self._owned_fd, "rb"))

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 0

    def close(self) -> None:
        self.buffer.close()


@contextlib.contextmanager
def _replace_stdin_with_pty() -> Iterator[int]:
    """Point fd 0 and ``sys.stdin`` at a fresh pty slave; yield the master fd.

    Restores fd 0 and ``sys.stdin`` in ``finally`` even on test failure, and
    closes the shim's buffer BEFORE closing the pty fds so no reader thread
    can block on a closed fd during teardown.
    """
    try:
        master_fd, slave_fd = pty.openpty()
    except OSError as exc:
        pytest.skip(f"pty.openpty() unavailable in this environment: {exc}")

    saved_stdin = sys.stdin
    saved_fd = os.dup(0)
    shim: _PtyStdinShim | None = None
    try:
        os.dup2(slave_fd, 0)
        shim = _PtyStdinShim()
        sys.stdin = shim
        yield master_fd
    finally:
        # Restore sys.stdin first so nothing references the shim after close.
        sys.stdin = saved_stdin
        if shim is not None:
            shim.close()
        # Restore fd 0 to its pre-test target before closing anything else.
        os.dup2(saved_fd, 0)
        os.close(saved_fd)
        os.close(master_fd)
        os.close(slave_fd)


def _tty_flags() -> int:
    """Return the ICANON|ECHO subset of the current fd-0 local-mode flags."""
    return termios.tcgetattr(0)[3] & (termios.ICANON | termios.ECHO)


async def test_full_lifecycle_restores_terminal() -> None:
    """Requirement: start -> suspend -> resume -> stop restores the terminal.

    Regression guard: a single listener driven through its full lifecycle on a
    real pty must leave ICANON|ECHO exactly as found. PASSES on unfixed code.
    """
    with _replace_stdin_with_pty():
        baseline = _tty_flags()
        listener = KeyboardListener(interrupt_event=asyncio.Event())
        try:
            await listener.start()
            # Prove the listener actually started against the pty.
            assert listener._task is not None
            assert listener._reader_thread is not None

            await listener.suspend()
            await listener.resume()
            assert listener._task is not None
            assert listener._reader_thread is not None

            await listener.stop()
        finally:
            await listener.stop()

        assert _tty_flags() == baseline, (
            f"tty flags after full lifecycle differ from baseline: "
            f"{_tty_flags():#x} != {baseline:#x}"
        )


async def test_second_listener_does_not_capture_cbreak() -> None:
    """Requirement: a second listener must not snapshot cbreak as baseline.

    Repro for issue #290: with listener A active (terminal in cbreak), starting
    listener B currently snapshots the cbreak settings as B's "original". If A
    is then stopped first, B's later stop() restores its cbreak snapshot,
    leaving the terminal in cbreak after both listeners have stopped.

    Stop order matters: A BEFORE B (reverse order masks the bug because B's
    stop would run while A's correct settings were the last ones applied).

    FAILS on unfixed code; PASSES once start() reuses the process-wide
    pre-listener baseline instead of re-snapshotting the live tty.
    """
    with _replace_stdin_with_pty():
        baseline = _tty_flags()
        listener_a = KeyboardListener(interrupt_event=asyncio.Event())
        listener_b = KeyboardListener(interrupt_event=asyncio.Event())
        try:
            await listener_a.start()
            assert listener_a._task is not None
            assert listener_a._reader_thread is not None

            # Start B WITHOUT stopping A — B sees an already-cbreak terminal.
            await listener_b.start()
            assert listener_b._task is not None
            assert listener_b._reader_thread is not None

            # Stop A first, then B (documented order above).
            await listener_a.stop()
            await listener_b.stop()
        finally:
            await listener_a.stop()
            await listener_b.stop()

        assert _tty_flags() == baseline, (
            f"tty flags after stopping both listeners differ from baseline: "
            f"{_tty_flags():#x} != {baseline:#x} "
            f"(listener B captured cbreak as its original settings)"
        )


async def test_double_start_same_instance_does_not_corrupt_baseline() -> None:
    """Requirement: calling start() twice on one listener keeps the baseline.

    Repro for issue #290: a second start() on the SAME instance currently
    overwrites ``_original_settings`` with a snapshot of the cbreak terminal
    the first start() installed. The later stop() then restores cbreak,
    leaving the terminal broken.

    FAILS on unfixed code; PASSES once start() is idempotent w.r.t. baseline
    capture (reuse the process-wide pre-listener baseline).
    """
    with _replace_stdin_with_pty():
        baseline = _tty_flags()
        listener = KeyboardListener(interrupt_event=asyncio.Event())
        try:
            await listener.start()
            assert listener._task is not None
            assert listener._reader_thread is not None

            # Second start on the SAME instance must not clobber the baseline.
            await listener.start()
            assert listener._task is not None
            assert listener._reader_thread is not None

            await listener.stop()
        finally:
            await listener.stop()

        assert _tty_flags() == baseline, (
            f"tty flags after double-start+stop differ from baseline: "
            f"{_tty_flags():#x} != {baseline:#x} "
            f"(second start() captured cbreak as the original settings)"
        )


async def test_listener_restore_targets_original_terminal_after_stdin_swap() -> None:
    """Requirement: suspend()/stop() restore the terminal the listener captured,
    not whatever ``sys.stdin`` points at later (issue #290, review follow-up).

    A provider, gate, or embedding host replacing stdin mid-run must not cause
    the listener to write the captured baseline into the replacement terminal.
    """
    with _replace_stdin_with_pty():
        # Give the first terminal a distinguishable baseline (ECHO off) so a
        # restore misdirected at the second terminal is observable.
        attrs = termios.tcgetattr(0)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(0, termios.TCSANOW, attrs)

        listener = KeyboardListener(interrupt_event=asyncio.Event())
        await listener.start()
        assert listener._task is not None

        with _replace_stdin_with_pty():
            # sys.stdin and fd 0 now point at a DIFFERENT pty with default
            # flags (ECHO on). It must survive suspend() and stop() untouched.
            baseline_b = _tty_flags()
            assert baseline_b & termios.ECHO

            await listener.suspend()
            assert _tty_flags() == baseline_b

            await listener.stop()
            assert _tty_flags() == baseline_b, (
                f"replacement terminal was overwritten by the captured baseline: "
                f"{_tty_flags():#x} != {baseline_b:#x}"
            )


async def test_failed_restore_does_not_leak_baseline_into_other_terminal() -> None:
    """Requirement: a retained baseline is only ever applied to its own terminal.

    A failed final restore keeps the cached baseline for retry. A later
    invocation attached to a DIFFERENT terminal must neither reuse that
    baseline nor apply it — the new terminal's own settings are captured
    instead (issue #290, review follow-up).
    """
    with _replace_stdin_with_pty():
        # Give the first terminal a distinguishable baseline (ECHO off) so a
        # leaked baseline is observable on the second terminal (ECHO on).
        attrs = termios.tcgetattr(0)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(0, termios.TCSANOW, attrs)

        listener = KeyboardListener(interrupt_event=asyncio.Event())
        await listener.start()
        await listener.stop()

        # Force the final restore to fail with a real termios.error so the
        # first terminal's baseline is retained for retry.
        from unittest.mock import patch

        with patch.object(
            termios, "tcsetattr", side_effect=termios.error("simulated restore failure")
        ):
            restore_terminal_baseline(clear=True)

    with _replace_stdin_with_pty():
        # A fresh pty has default flags (ECHO on). A leaked first-terminal
        # baseline would turn ECHO off here.
        baseline_b = _tty_flags()
        assert baseline_b & termios.ECHO

        listener_b = KeyboardListener(interrupt_event=asyncio.Event())
        try:
            await listener_b.start()
            await listener_b.stop()
            restore_terminal_baseline(clear=True)
        finally:
            restore_terminal_baseline(clear=True)

        assert _tty_flags() == baseline_b, (
            f"second terminal's flags were overwritten by the first terminal's "
            f"retained baseline: {_tty_flags():#x} != {baseline_b:#x}"
        )


async def test_process_baseline_restores_late_teardown_change() -> None:
    """Requirement: final cleanup repairs TTY changes made after listener.stop()."""
    with _replace_stdin_with_pty():
        baseline = _tty_flags()
        listener = KeyboardListener(interrupt_event=asyncio.Event())
        try:
            await listener.start()
            await listener.stop()

            # Simulate a provider/runtime cleanup that touches the controlling
            # terminal after the keyboard listener has already stopped.
            import tty

            tty.setcbreak(0)
            assert _tty_flags() != baseline

            restore_terminal_baseline(clear=True)
        finally:
            restore_terminal_baseline(clear=True)

        assert _tty_flags() == baseline
