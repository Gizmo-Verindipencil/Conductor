"""Async keyboard listener for interrupt detection.

This module provides the KeyboardListener class that detects Esc and Ctrl+G
keypresses asynchronously and signals them via an asyncio.Event. It handles
Esc vs ANSI escape sequence disambiguation using a 50ms read-ahead timeout.

Uses a dedicated daemon thread for blocking stdin reads, delivering bytes
into an ``asyncio.Queue`` via ``loop.call_soon_threadsafe``. This avoids
thread leaks from abandoned ``run_in_executor`` futures.

Terminal safety (issue #290): the original tty settings are captured once per
process into the module-level ``_captured_baseline`` cache so a second listener
never re-captures cbreak state as "original", and the SIGTERM cleanup handler
restores the terminal before delegating to the previous disposition.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import os
import select
import signal
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Key codes
_ESC_BYTE = 0x1B
_CTRL_G_BYTE = 0x07

# Timeout for disambiguating bare Esc from escape sequences (seconds)
_ESC_DISAMBIGUATE_TIMEOUT = 0.05


@dataclass(frozen=True)
class _TerminalBaseline:
    """Process-wide tty baseline plus the identity of the terminal it came from.

    ``fd`` is an owned duplicate of the capture-time stdin descriptor. Holding
    it open pins the terminal (a pts index cannot be recycled while the
    descriptor is held) and lets the restore paths target the original
    terminal even when ``sys.stdin`` has since been replaced or closed.
    """

    settings: Any
    """Snapshot returned by ``termios.tcgetattr`` before the first cbreak."""

    fd: int
    """Owned duplicate of the descriptor the baseline was captured from."""

    identity: tuple[int, int]
    """``(st_dev, st_ino)`` of the capture-time terminal, for reuse checks."""


_captured_baseline: _TerminalBaseline | None = None
"""Process-wide tty baseline captured on the FIRST successful start().

Subsequent KeyboardListener instances reuse this baseline so a second
listener never re-captures cbreak state as "original" — but only while they
are attached to the SAME terminal; a different terminal gets its own baseline
rather than inheriting (or being overwritten with) a stale one. The outer CLI
cleanup retires it after every teardown step has finished. Tests also reset
it via a fixture. See issue #290."""


def _try_import_termios() -> Any:
    """Return the ``termios`` module, or ``None`` where it is unavailable."""
    try:
        import termios
    except ImportError:
        return None
    return termios


def _terminal_identity(fd: int) -> tuple[int, int]:
    """Return the ``(st_dev, st_ino)`` pair identifying the terminal on ``fd``."""
    stat = os.fstat(fd)
    return (stat.st_dev, stat.st_ino)


def _retire_cached_baseline() -> None:
    """Discard the cached baseline, closing its owned descriptor."""
    global _captured_baseline

    if _captured_baseline is not None:
        with contextlib.suppress(OSError):
            os.close(_captured_baseline.fd)
        _captured_baseline = None


def restore_terminal_baseline(*, clear: bool = False) -> None:
    """Restore the process-wide TTY baseline and optionally retire it.

    Restores through the baseline's owned descriptor rather than the current
    ``sys.stdin`` so the settings reach the terminal they were captured from.
    Never raises: this runs inside ``finally`` blocks and ``atexit``, where an
    escaping error would overwrite the workflow's own result or exception —
    and ``termios.error`` is not an ``OSError`` subclass (issue #290).
    """
    baseline = _captured_baseline
    if baseline is None:
        return

    termios = _try_import_termios()
    if termios is None:
        return

    try:
        termios.tcsetattr(baseline.fd, termios.TCSANOW, baseline.settings)
    except (termios.error, ValueError, OSError):
        # Keep the baseline so a later atexit/SIGTERM/stop() attempt retries.
        return

    if clear:
        _retire_cached_baseline()


@dataclass
class KeyboardListener:
    """Async terminal keypress listener for interrupt detection.

    Puts the terminal into cbreak mode and listens for Esc (0x1b) and
    Ctrl+G (0x07). When detected, sets an ``asyncio.Event``.

    For Esc key disambiguation: waits 50ms after receiving 0x1b. If no
    follow-up bytes arrive, it is a bare Esc press. If follow-up bytes
    arrive (e.g., 0x5b for arrow keys), the sequence is discarded.

    A dedicated daemon thread performs blocking stdin reads and delivers
    bytes via an ``asyncio.Queue`` (using ``loop.call_soon_threadsafe``).
    The listen loop reads from this queue with native async operations,
    avoiding thread leaks from ``run_in_executor`` + ``wait_for`` timeouts.

    Example:
        >>> event = asyncio.Event()
        >>> listener = KeyboardListener(interrupt_event=event)
        >>> await listener.start()
        >>> # ... event will be set when Esc or Ctrl+G is pressed
        >>> await listener.stop()
    """

    interrupt_event: asyncio.Event
    """Event that is set when an interrupt key (Esc/Ctrl+G) is detected."""

    _original_settings: Any = field(default=None, repr=False)
    """Saved terminal settings for restoration."""

    _terminal_fd: int | None = field(default=None, repr=False)
    """Owned duplicate of the capture-time stdin descriptor.

    Restore paths target this descriptor rather than the current ``sys.stdin``
    so the settings always reach the terminal they were captured from, even
    if ``sys.stdin`` has since been replaced (issue #290)."""

    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    """The asyncio task running the listen loop."""

    _stop_flag: bool = field(default=False, repr=False)
    """Flag to signal the listen loop to stop."""

    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
    """Reference to the event loop for thread-safe signaling."""

    _atexit_registered: bool = field(default=False, repr=False)
    """Whether the atexit handler has been registered."""

    _previous_sigterm: Any = field(default=None, repr=False)
    """Previous SIGTERM handler for restoration."""

    _sigterm_handler: Any = field(default=None, repr=False)
    """This instance's own installed SIGTERM handler closure (issue #290)."""

    _byte_queue: asyncio.Queue[int | None] = field(default_factory=asyncio.Queue, repr=False)
    """Async queue for delivering bytes from the reader thread."""

    _reader_thread: threading.Thread | None = field(default=None, repr=False)
    """Dedicated daemon thread for blocking stdin reads."""

    async def start(self) -> None:
        """Enter cbreak mode and begin listening for keypresses.

        Stores the event loop reference for thread-safe signaling.
        Only activates on Unix systems with a TTY stdin.
        """
        if not sys.stdin.isatty():
            logger.debug("stdin is not a TTY, keyboard listener not started")
            return

        try:
            import termios
            import tty
        except ImportError:
            logger.debug("termios/tty not available (non-Unix), listener not started")
            return

        self._loop = asyncio.get_running_loop()
        self._stop_flag = False

        # Save original terminal settings. A second listener must never
        # re-snapshot an already-cbreak terminal as its "original" state, so
        # the baseline is captured once per process into a module-level cache
        # (issue #290).
        global _captured_baseline

        # Idempotent guard: a start() on a truly-active listener (baseline
        # held AND reader thread running) is a no-op so it cannot overwrite
        # the baseline or spawn duplicate threads. After suspend() the thread
        # is None, so start-after-suspend correctly falls through below.
        if self._original_settings is not None and self._reader_thread is not None:
            logger.debug("Keyboard listener already active, start() is a no-op")
            return

        created_baseline = False
        try:
            stdin_fd = sys.stdin.fileno()
            identity = _terminal_identity(stdin_fd)
            if _captured_baseline is not None and _captured_baseline.identity == identity:
                # Same terminal: reuse the process-wide pre-listener baseline.
                self._original_settings = _captured_baseline.settings
            else:
                # A different terminal must not inherit another terminal's
                # baseline: retire the stale one and capture fresh (issue #290).
                _retire_cached_baseline()
                self._original_settings = termios.tcgetattr(stdin_fd)
                _captured_baseline = _TerminalBaseline(
                    settings=self._original_settings,
                    fd=os.dup(stdin_fd),
                    identity=identity,
                )
                created_baseline = True
                # This listener's owned descriptor still targets the previous
                # terminal — re-target it before capturing the new one below.
                self._close_terminal_fd()
            if self._terminal_fd is None:
                self._terminal_fd = os.dup(stdin_fd)
        except (termios.error, ValueError, OSError):
            logger.debug("Failed to get terminal settings, listener not started")
            self._original_settings = None
            self._close_terminal_fd()
            if created_baseline:
                _retire_cached_baseline()
            return

        # Enter cbreak mode (not full raw mode, preserves output processing)
        try:
            tty.setcbreak(stdin_fd)
        except termios.error:
            logger.debug("Failed to set cbreak mode, listener not started")
            self._original_settings = None
            self._close_terminal_fd()
            if created_baseline:
                _retire_cached_baseline()
            return

        # Register cleanup handlers
        self._register_cleanup_handlers()

        # Reset the queue
        self._byte_queue = asyncio.Queue()

        # Start the dedicated reader thread
        self._reader_thread = threading.Thread(
            target=self._reader_thread_main, daemon=True, name="keyboard-listener"
        )
        self._reader_thread.start()

        # Start the listen loop as an asyncio task
        self._task = asyncio.create_task(self._listen_loop())
        logger.debug("Keyboard listener started")

    async def stop(self) -> None:
        """Stop listening and restore terminal settings."""
        self._stop_flag = True

        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        # Join the reader thread to ensure it exits before interpreter shutdown.
        # The select()-based polling in _reader_thread_main checks _stop_flag
        # every 100ms, so the thread should exit within that window.
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=0.5)
            self._reader_thread = None

        self._restore_terminal()
        logger.debug("Keyboard listener stopped")

    async def suspend(self) -> None:
        """Temporarily suspend listening and restore normal terminal mode.

        Use this before any operation that needs normal stdin access
        (e.g., human gates, max iterations prompts). Call ``resume()``
        afterward to re-enter cbreak mode and restart listening.
        """
        self._stop_flag = True

        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        if self._reader_thread is not None:
            self._reader_thread.join(timeout=0.5)
            self._reader_thread = None

        # Restore terminal but keep _original_settings for resume()
        if self._original_settings is not None:
            termios = _try_import_termios()
            if termios is not None:
                with contextlib.suppress(termios.error, ValueError, OSError):
                    fd = self._terminal_fd if self._terminal_fd is not None else sys.stdin.fileno()
                    termios.tcsetattr(fd, termios.TCSANOW, self._original_settings)

        logger.debug("Keyboard listener suspended")

    async def resume(self) -> None:
        """Resume listening after a ``suspend()`` call.

        Re-enters cbreak mode and restarts the reader thread and listen loop.
        No-op if the listener was never started or original settings are gone.
        """
        if self._original_settings is None or self._loop is None:
            return

        try:
            import tty
        except ImportError:
            return

        # Re-enter cbreak mode
        try:
            fd = self._terminal_fd if self._terminal_fd is not None else sys.stdin.fileno()
            tty.setcbreak(fd)
        except Exception:
            logger.debug("Failed to re-enter cbreak mode on resume")
            return

        self._stop_flag = False

        # Reset the queue to discard any stale bytes
        self._byte_queue = asyncio.Queue()

        # Restart the reader thread
        self._reader_thread = threading.Thread(
            target=self._reader_thread_main, daemon=True, name="keyboard-listener"
        )
        self._reader_thread.start()

        # Restart the listen loop
        self._task = asyncio.create_task(self._listen_loop())
        logger.debug("Keyboard listener resumed")

    def _restore_terminal(self) -> None:
        """Restore original terminal settings.

        Never raises: a failed restore keeps the saved settings so a later
        atexit/SIGTERM/stop() attempt can retry, and an escaping error would
        otherwise overwrite the workflow's own outcome (issue #290).
        """
        if self._original_settings is None:
            return

        termios = _try_import_termios()
        if termios is None:
            return

        try:
            fd = self._terminal_fd if self._terminal_fd is not None else sys.stdin.fileno()
            termios.tcsetattr(fd, termios.TCSANOW, self._original_settings)
        except (termios.error, ValueError, OSError):
            return
        # Clear the baseline only after a successful restore so a transient
        # failure keeps it for a later retry (issue #290).
        self._original_settings = None
        self._close_terminal_fd()

    def _close_terminal_fd(self) -> None:
        """Close the owned stdin duplicate, if any."""
        if self._terminal_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._terminal_fd)
            self._terminal_fd = None

    def _register_cleanup_handlers(self) -> None:
        """Register atexit and SIGTERM handlers for crash safety."""
        if not self._atexit_registered:
            atexit.register(restore_terminal_baseline)
            self._atexit_registered = True

        # Install a SIGTERM handler that restores the terminal, then delegates
        # to the previously-installed disposition so the process terminates
        # with its expected action (issue #290).
        try:
            # Guard: if THIS instance's own handler is still installed, skip
            # re-registration. Re-registering would capture our own closure as
            # the "previous" disposition, recursing forever on invocation. The
            # identity check targets this instance's stored handler only — a
            # stale closure from a stopped listener must not block a new one.
            if (
                self._sigterm_handler is not None
                and signal.getsignal(signal.SIGTERM) is self._sigterm_handler
            ):
                return

            # Capture the previous disposition into a LOCAL variable so the
            # closure is immutable: reading the mutable field at invocation
            # time would let a later re-registration rewrite what an
            # already-installed closure delegates to.
            previous = signal.getsignal(signal.SIGTERM)
            self._previous_sigterm = previous  # backward-compat introspection

            def _sigterm_handler(signum: int, frame: Any) -> None:
                # A failed restore must never swallow the signal: swallow any
                # error here so the handler always reaches the delegation path.
                # The baseline is dropped unconditionally afterward — the
                # process is terminating, so a stale value must not be reused.
                try:
                    self._restore_terminal()
                    restore_terminal_baseline()
                except Exception:
                    pass
                finally:
                    self._original_settings = None
                    self._close_terminal_fd()
                if previous is signal.SIG_DFL:
                    # In an unmodified process (the common case)
                    # `signal.getsignal(SIGTERM)` is `signal.Handlers.SIG_DFL`
                    # — an IntEnum member, not callable — so falling through
                    # here would silently swallow the SIGTERM: the process
                    # would survive and keep running forever (Fleet Manager
                    # E3-T9). Reset to default and re-raise so the default
                    # action (terminate) actually runs.
                    signal.signal(signal.SIGTERM, signal.SIG_DFL)
                    os.kill(os.getpid(), signum)
                elif previous is signal.SIG_IGN:
                    # An inherited SIG_IGN is a deliberate "this process does
                    # not die on SIGTERM", set by a supervisor or container
                    # init shim: restore and stay alive.
                    return
                elif callable(previous):
                    previous(signum, frame)

            signal.signal(signal.SIGTERM, _sigterm_handler)
            self._sigterm_handler = _sigterm_handler
        except (OSError, ValueError):
            # Can't set signal handler (not main thread, etc.)
            pass

    def _reader_thread_main(self) -> None:
        """Dedicated daemon thread that reads stdin bytes into the async queue.

        Uses ``select()`` with a 100ms timeout to poll stdin, allowing the
        thread to check ``_stop_flag`` periodically and exit cleanly on
        shutdown. This prevents the thread from holding a lock on
        ``sys.stdin.buffer`` during interpreter finalization.

        Uses ``loop.call_soon_threadsafe`` to safely deliver bytes to the
        asyncio queue from this thread.
        """
        assert self._loop is not None

        while not self._stop_flag:
            # Poll stdin with a short timeout so we can check _stop_flag
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 0.1)
            except (OSError, ValueError):
                # stdin closed or invalid
                break

            if not ready:
                # Timeout — no data, loop back to check _stop_flag
                continue

            byte_val = self._read_byte_blocking()
            try:
                self._loop.call_soon_threadsafe(self._byte_queue.put_nowait, byte_val)
            except RuntimeError:
                # Event loop is closed
                break
            if byte_val is None:
                break

    async def _listen_loop(self) -> None:
        """Process bytes from the async queue and detect interrupt keys.

        On receiving 0x1b, waits 50ms for follow-up bytes to disambiguate
        bare Esc from ANSI escape sequences. Uses
        ``loop.call_soon_threadsafe(event.set)`` for safe signaling.
        """
        assert self._loop is not None

        try:
            while not self._stop_flag:
                byte_val = await self._byte_queue.get()

                if byte_val is None:
                    break

                if byte_val == _CTRL_G_BYTE:
                    # Ctrl+G: immediate interrupt
                    self._loop.call_soon_threadsafe(self.interrupt_event.set)
                    logger.debug("Ctrl+G detected, interrupt event set")

                elif byte_val == _ESC_BYTE:
                    # Could be bare Esc or start of escape sequence
                    # Wait 50ms for follow-up bytes
                    is_bare_esc = await self._disambiguate_esc()
                    if is_bare_esc:
                        self._loop.call_soon_threadsafe(self.interrupt_event.set)
                        logger.debug("Bare Esc detected, interrupt event set")

                # Other bytes are ignored

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Keyboard listener loop exited with exception", exc_info=True)

    def _read_byte_blocking(self) -> int | None:
        """Blocking single-byte read from stdin.

        Only called from the dedicated reader thread.

        Returns:
            The byte value read, or None if stop flag is set or read fails.
        """
        if self._stop_flag:
            return None

        try:
            data = sys.stdin.buffer.read(1)
            if data:
                return data[0]
            return None
        except (OSError, ValueError):
            return None

    async def _disambiguate_esc(self) -> bool:
        """Disambiguate bare Esc from ANSI escape sequences.

        Waits 50ms for follow-up bytes after receiving 0x1b. If no bytes
        arrive, it is a bare Esc. If bytes arrive (e.g., 0x5b for CSI),
        the sequence is consumed and discarded.

        Returns:
            True if this was a bare Esc press, False if it was an escape sequence.
        """
        try:
            next_byte = await asyncio.wait_for(
                self._byte_queue.get(),
                timeout=_ESC_DISAMBIGUATE_TIMEOUT,
            )
        except TimeoutError:
            # No follow-up byte within 50ms: bare Esc
            return True

        if next_byte is None:
            # Read failed or stop flag set: treat as bare Esc
            return True

        # Follow-up byte arrived: this is an escape sequence
        # Consume remaining bytes of the sequence
        if next_byte == 0x5B:
            # CSI sequence (e.g., arrow keys): read until final byte (0x40-0x7E)
            await self._consume_csi_sequence()
        elif next_byte == 0x4F:
            # SS3 sequence (e.g., F1-F4): read one more byte
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._byte_queue.get(),
                    timeout=_ESC_DISAMBIGUATE_TIMEOUT,
                )
        # Other escape sequences (Alt+key, etc.) are just 2 bytes total

        return False

    async def _consume_csi_sequence(self) -> None:
        """Consume remaining bytes of a CSI escape sequence.

        CSI sequences start with ESC [ and end with a byte in the range
        0x40-0x7E (e.g., A for up arrow, B for down, C for right, D for left).
        Intermediate bytes are in the range 0x20-0x3F.
        """
        while True:
            try:
                byte_val = await asyncio.wait_for(
                    self._byte_queue.get(),
                    timeout=_ESC_DISAMBIGUATE_TIMEOUT,
                )
            except TimeoutError:
                break

            if byte_val is None:
                break

            # CSI final bytes are in range 0x40-0x7E
            if 0x40 <= byte_val <= 0x7E:
                break
