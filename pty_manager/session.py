"""Session class - wraps a PTY with its child process."""

from __future__ import annotations

import os
import pty
import select
import signal
import fcntl
import struct
import termios
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List


@dataclass
class Session:
    """Represents a PTY session with a running process."""

    id: int
    name: str
    command: List[str]
    pid: int
    master_fd: int
    scrollback: Deque[bytes] = field(default_factory=lambda: deque(maxlen=1000))
    last_activity: float = field(default_factory=time.time)
    status: str = "idle"  # "active" or "idle"
    _status_callback: object = field(default=None, repr=False)

    def set_status_callback(self, callback):
        """Set callback for status changes: callback(session, old_status, new_status)"""
        self._status_callback = callback

    def _set_status(self, new_status: str):
        """Update status and notify if changed."""
        if self.status != new_status:
            old_status = self.status
            self.status = new_status
            if self._status_callback:
                self._status_callback(self, old_status, new_status)

    @classmethod
    def spawn(cls, session_id: int, name: str, command: List[str], rows: int = 24, cols: int = 80) -> Session:
        """Spawn a new process in a PTY."""
        master_fd, slave_fd = pty.openpty()

        # Set initial terminal size before fork
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

        pid = os.fork()
        if pid == 0:
            # Child process
            os.close(master_fd)
            os.setsid()

            # Set up slave as controlling terminal
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

            # Redirect stdio to slave
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)

            if slave_fd > 2:
                os.close(slave_fd)

            # Execute the command
            try:
                os.execvp(command[0], command)
            except OSError as e:
                # Write error to stderr (which goes to PTY)
                import sys
                sys.stderr.write(f"[SPAWN ERROR] Failed to exec {command[0]}: {e}\n")
                sys.stderr.flush()
                os._exit(127)
        else:
            # Parent process
            os.close(slave_fd)

            # Handle initial terminal queries (e.g., cursor position request)
            # Some programs like codex send \x1b[6n and expect a response
            initial_data = b""
            time.sleep(0.1)  # Give child time to send queries
            try:
                readable, _, _ = select.select([master_fd], [], [], 0.2)
                if readable:
                    initial_data = os.read(master_fd, 4096)
                    # Respond to cursor position query
                    if b'\x1b[6n' in initial_data:
                        os.write(master_fd, b'\x1b[1;1R')
            except (OSError, BlockingIOError):
                pass

            # Set master_fd to non-blocking
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            session = cls(
                id=session_id,
                name=name,
                command=command,
                pid=pid,
                master_fd=master_fd,
            )

            # Preserve initial output in scrollback (prompts, banners, etc.)
            if initial_data:
                session.scrollback.append(initial_data)

            return session

    def is_alive(self) -> bool:
        """Check if the child process is still running."""
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            return pid == 0
        except ChildProcessError:
            return False

    def write(self, data: bytes) -> None:
        """Write data to the PTY."""
        os.write(self.master_fd, data)

    def read(self, size: int = 4096) -> bytes:
        """Read data from the PTY (non-blocking)."""
        try:
            data = os.read(self.master_fd, size)
            if data:
                self.scrollback.append(data)
                self.last_activity = time.time()
                self._set_status("active")
            return data
        except BlockingIOError:
            return b""
        except OSError:
            return b""

    def get_scrollback(self) -> bytes:
        """Get all buffered scrollback data."""
        return b''.join(self.scrollback)

    def check_idle_timeout(self, timeout: float = 2.0) -> None:
        """Check if session should transition to idle (no activity for timeout seconds)."""
        if self.status == "active" and (time.time() - self.last_activity) > timeout:
            self._set_status("idle")

    def resize(self, rows: int, cols: int) -> None:
        """Resize the PTY window."""
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)

    def force_redraw(self) -> None:
        """Force application redraw by triggering SIGWINCH."""
        # Get current size
        try:
            packed = fcntl.ioctl(self.master_fd, termios.TIOCGWINSZ, b'\x00' * 8)
            rows, cols = struct.unpack("HHHH", packed)[:2]
            # Resize dance: shrink by 1 row, then restore
            self.resize(max(1, rows - 1), cols)
            self.resize(rows, cols)
        except OSError:
            pass

    def terminate(self) -> None:
        """Terminate the session with SIGKILL fallback."""
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            # Already dead, just close fd
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            return

        # Wait up to 1 second for graceful exit
        for _ in range(10):
            time.sleep(0.1)
            try:
                pid, _ = os.waitpid(self.pid, os.WNOHANG)
                if pid != 0:
                    break
            except ChildProcessError:
                break
        else:
            # Force kill if still running
            try:
                os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass

        try:
            os.close(self.master_fd)
        except OSError:
            pass
