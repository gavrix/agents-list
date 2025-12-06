"""Client - connects to server for remote PTY management."""

from __future__ import annotations

import os
import sys
import tty
import termios
import signal
import select
import socket
import fcntl
import struct
import curses
import subprocess
import time
from typing import Dict, List, Optional

from pty_manager.server import get_socket_path
from pty_manager.protocol import (
    Message, MessageType, SessionInfo, SocketIO,
    msg_spawn, msg_list, msg_attach, msg_detach, msg_kill, msg_shutdown,
    msg_data, msg_resize, msg_register_terminal, msg_switch, msg_switched,
    extract_data, msg_subscribe,
)


# Ctrl+B is ASCII 2
PREFIX_KEY = b"\x02"
DETACH_KEY = b"d"


class Client:
    """PTY Manager client."""

    def __init__(self):
        self.socket_path = get_socket_path()
        self.sock: Optional[socket.socket] = None
        self.io: Optional[SocketIO] = None

    def connect(self) -> bool:
        """Connect to the server."""
        if not self.socket_path.exists():
            print(f"Server not running (no socket at {self.socket_path})")
            print("Start the server with: python3 -m pty_manager start")
            return False

        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.connect(str(self.socket_path))
            self.io = SocketIO(self.sock)
            return True
        except ConnectionRefusedError:
            print("Server not responding. It may have crashed.")
            print("Remove stale socket and restart:")
            print(f"  rm {self.socket_path}")
            print("  python3 -m pty_manager start")
            return False
        except Exception as e:
            print(f"Failed to connect: {e}")
            return False

    def disconnect(self):
        """Disconnect from server."""
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
            self.io = None

    def cmd_list(self) -> int:
        """List sessions."""
        if not self.connect():
            return 1

        try:
            self.io.send(msg_list())
            response = self.io.recv()

            if response is None:
                print("Server disconnected")
                return 1

            if response.type == MessageType.SESSION_LIST:
                sessions = [
                    SessionInfo.from_dict(s)
                    for s in response.payload.get("sessions", [])
                ]

                if not sessions:
                    print("No active sessions")
                else:
                    print(f"{'ID':<4} {'Name':<15} {'Status':<8} {'Command'}")
                    print("-" * 60)
                    for s in sessions:
                        status = "alive" if s.alive else "dead"
                        cmd_str = " ".join(s.command)
                        if len(cmd_str) > 30:
                            cmd_str = cmd_str[:27] + "..."
                        print(f"{s.id:<4} {s.name:<15} {status:<8} {cmd_str}")
                return 0
            else:
                print(f"Unexpected response: {response.type}")
                return 1
        finally:
            self.disconnect()

    def cmd_spawn(self, name: str, command: List[str]) -> int:
        """Spawn a new session."""
        if not self.connect():
            return 1

        try:
            self.io.send(msg_spawn(name, command))
            response = self.io.recv()

            if response is None:
                print("Server disconnected")
                return 1

            if response.type == MessageType.OK:
                print(response.payload.get("message", "OK"))
                return 0
            elif response.type == MessageType.ERROR:
                print(f"Error: {response.payload.get('message', 'Unknown error')}")
                return 1
            else:
                print(f"Unexpected response: {response.type}")
                return 1
        finally:
            self.disconnect()

    def cmd_kill(self, target: str) -> int:
        """Kill a session."""
        if not self.connect():
            return 1

        try:
            self.io.send(msg_kill(target))
            response = self.io.recv()

            if response is None:
                print("Server disconnected")
                return 1

            if response.type == MessageType.OK:
                print(response.payload.get("message", "OK"))
                return 0
            elif response.type == MessageType.ERROR:
                print(f"Error: {response.payload.get('message', 'Unknown error')}")
                return 1
            else:
                print(f"Unexpected response: {response.type}")
                return 1
        finally:
            self.disconnect()

    def cmd_attach(self, target: str) -> int:
        """Attach to a session."""
        if not self.connect():
            return 1

        stdin_fd = sys.stdin.fileno()
        stdout_fd = sys.stdout.fileno()

        # Get terminal size
        rows, cols = self._get_terminal_size()

        try:
            # Send attach request
            self.io.send(msg_attach(target, rows, cols))
            response = self.io.recv()

            if response is None:
                print("Server disconnected")
                return 1

            if response.type == MessageType.ERROR:
                print(f"Error: {response.payload.get('message', 'Unknown error')}")
                return 1

            if response.type != MessageType.ATTACHED:
                print(f"Unexpected response: {response.type}")
                return 1

            session_name = response.payload.get("name", target)
            print(f"Attached to '{session_name}' (Ctrl+B d to detach)")

            # Save terminal state and enter raw mode
            old_settings = termios.tcgetattr(stdin_fd)

            # Set up resize handler
            def handle_resize(signum, frame):
                rows, cols = self._get_terminal_size()
                self.io.send(msg_resize(rows, cols))

            old_handler = signal.signal(signal.SIGWINCH, handle_resize)

            try:
                tty.setraw(stdin_fd)
                return self._proxy_loop(stdin_fd, stdout_fd, session_name)
            finally:
                # Restore terminal
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_settings)
                signal.signal(signal.SIGWINCH, old_handler or signal.SIG_DFL)

        finally:
            self.disconnect()

    def _get_terminal_size(self) -> tuple:
        """Get terminal size (rows, cols)."""
        try:
            result = struct.unpack(
                "HHHH",
                fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\x00" * 8)
            )
            return result[0], result[1]
        except (OSError, struct.error):
            return 24, 80

    def _proxy_loop(self, stdin_fd: int, stdout_fd: int, session_name: str) -> int:
        """Main I/O proxy loop during attach."""
        prefix_received = False

        # Keep socket non-blocking throughout
        self.sock.setblocking(False)

        while True:
            # First, drain any buffered messages
            while True:
                msg = self.io.pop_message()
                if msg is None:
                    break
                result = self._handle_server_message(msg, stdout_fd, session_name)
                if result is not None:
                    return result

            # Wait for input from stdin or socket
            try:
                readable, _, _ = select.select(
                    [stdin_fd, self.sock],
                    [],
                    [],
                    0.1
                )
            except (select.error, ValueError):
                break

            # Handle stdin (user input)
            if stdin_fd in readable:
                try:
                    data = os.read(stdin_fd, 1024)
                except OSError:
                    break

                if not data:
                    break

                # Check for detach sequence
                result, forward_data = self._process_input(data, prefix_received)

                if result == "detach":
                    self.sock.setblocking(True)
                    self.io.send(msg_detach())
                    self.sock.setblocking(False)
                    print(f"\r\n[Detached from '{session_name}']")
                    return 0
                elif result == "prefix":
                    prefix_received = True
                else:
                    prefix_received = False
                    if forward_data:
                        self.sock.setblocking(True)
                        self.io.send(msg_data(forward_data))
                        self.sock.setblocking(False)

            # Handle socket (server messages) - read into buffer then drain
            if self.sock in readable:
                self.io.try_read()
                while True:
                    msg = self.io.pop_message()
                    if msg is None:
                        break
                    result = self._handle_server_message(msg, stdout_fd, session_name)
                    if result is not None:
                        return result

        return 0

    def _handle_server_message(self, msg: Message, stdout_fd: int, session_name: str) -> Optional[int]:
        """Handle a message from the server. Returns exit code if should exit, None to continue."""
        if msg.type == MessageType.DATA:
            data = extract_data(msg)
            os.write(stdout_fd, data)
            return None
        elif msg.type == MessageType.DETACHED:
            reason = msg.payload.get("reason", "unknown")
            if reason == "session_died":
                print(f"\r\n[Session '{session_name}' has exited]")
            else:
                print(f"\r\n[Detached: {reason}]")
            return 0
        return None

    def _process_input(self, data: bytes, prefix_received: bool) -> tuple:
        """
        Process input for detach sequence.
        Returns (result, data_to_forward).
        """
        if prefix_received:
            if data.startswith(DETACH_KEY):
                # User wants to detach
                remaining = data[1:] if len(data) > 1 else None
                return ("detach", remaining)
            else:
                # Not detach - forward prefix + current input
                return ("normal", PREFIX_KEY + data)
        else:
            if data == PREFIX_KEY:
                # Just the prefix key
                return ("prefix", None)
            elif data.startswith(PREFIX_KEY):
                # Prefix followed by more
                rest = data[1:]
                if rest.startswith(DETACH_KEY):
                    remaining = rest[1:] if len(rest) > 1 else None
                    return ("detach", remaining)
                else:
                    return ("normal", data)
            else:
                # Normal input
                return ("normal", data)

    def cmd_shutdown(self) -> int:
        """Shutdown the server."""
        if not self.connect():
            return 1

        try:
            self.io.send(msg_shutdown())
            response = self.io.recv()

            if response and response.type == MessageType.OK:
                print(response.payload.get("message", "OK"))
            return 0
        finally:
            self.disconnect()

    def cmd_terminal(self) -> int:
        """Run as terminal client - displays active session."""
        if not self.connect():
            return 1

        stdin_fd = sys.stdin.fileno()
        stdout_fd = sys.stdout.fileno()

        try:
            # Register as terminal
            self.io.send(msg_register_terminal())
            response = self.io.recv()

            if response is None:
                print("Server disconnected")
                return 1

            if response.type == MessageType.ERROR:
                print(f"Error: {response.payload.get('message', 'Unknown error')}")
                return 1

            if response.type != MessageType.OK:
                print(f"Unexpected response: {response.type}")
                return 1

            print("Terminal registered. Waiting for sessions...")
            print("Use manager to switch sessions. Ctrl+C to exit.")

            # Save terminal state and enter raw mode
            old_settings = termios.tcgetattr(stdin_fd)

            # Set up resize handler
            def handle_resize(signum, frame):
                rows, cols = self._get_terminal_size()
                try:
                    self.io.send(msg_resize(rows, cols))
                except Exception:
                    pass

            old_resize_handler = signal.signal(signal.SIGWINCH, handle_resize)

            try:
                tty.setraw(stdin_fd)
                return self._terminal_loop(stdin_fd, stdout_fd)
            except KeyboardInterrupt:
                return 0
            finally:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_settings)
                signal.signal(signal.SIGWINCH, old_resize_handler or signal.SIG_DFL)

        finally:
            self.disconnect()

    def _terminal_loop(self, stdin_fd: int, stdout_fd: int) -> int:
        """Main loop for terminal client."""
        current_session: Optional[str] = None
        prefix_received = False

        self.sock.setblocking(False)

        while True:
            # Drain buffered messages first
            while True:
                msg = self.io.pop_message()
                if msg is None:
                    break
                result = self._handle_terminal_server_message(
                    msg, stdout_fd, current_session
                )
                if result == "exit":
                    return 0
                elif isinstance(result, str):
                    current_session = result

            # Wait for input
            try:
                readable, _, _ = select.select(
                    [stdin_fd, self.sock], [], [], 0.1
                )
            except (select.error, ValueError):
                break

            # Handle stdin
            if stdin_fd in readable:
                try:
                    data = os.read(stdin_fd, 1024)
                except OSError:
                    break

                if not data:
                    break

                # Check for Ctrl+C (ASCII 3)
                if b"\x03" in data:
                    return 0

                # Check for detach sequence
                result, forward_data = self._process_input(data, prefix_received)

                if result == "detach":
                    # In terminal mode, detach means go to no-session state
                    self.sock.setblocking(True)
                    self.io.send(msg_detach())
                    self.sock.setblocking(False)
                    current_session = None
                    os.write(stdout_fd, b"\r\n[Detached]\r\n")
                elif result == "prefix":
                    prefix_received = True
                else:
                    prefix_received = False
                    if forward_data and current_session:
                        self.sock.setblocking(True)
                        self.io.send(msg_data(forward_data))
                        self.sock.setblocking(False)

            # Handle socket messages
            if self.sock in readable:
                self.io.try_read()
                while True:
                    msg = self.io.pop_message()
                    if msg is None:
                        break
                    result = self._handle_terminal_server_message(
                        msg, stdout_fd, current_session
                    )
                    if result == "exit":
                        return 0
                    elif isinstance(result, str):
                        current_session = result

        return 0

    def _handle_terminal_server_message(
        self, msg: Message, stdout_fd: int, current_session: Optional[str]
    ) -> Optional[str]:
        """Handle message in terminal mode. Returns new session name, 'exit', or None."""
        if msg.type == MessageType.SWITCH:
            target = msg.payload.get("target")

            # 1. Clear screen and tmux history
            os.write(stdout_fd, b"\x1b[2J\x1b[H")
            tmux_pane = os.environ.get("TMUX_PANE")
            if tmux_pane:
                subprocess.run(["tmux", "clear-history", "-t", tmux_pane], capture_output=True)

            # 2. Write scrollback history (populates clean tmux buffer)
            scrollback = msg.payload.get("scrollback")
            if scrollback:
                import base64
                data = base64.b64decode(scrollback)
                # Strip cursor position query to avoid terminal responding during replay
                data = data.replace(b'\x1b[6n', b'')
                os.write(stdout_fd, data)

            # 3. Send resize and confirm
            rows, cols = self._get_terminal_size()
            self.sock.setblocking(True)
            self.io.send(msg_resize(rows, cols))
            self.io.send(msg_switched(target))
            self.sock.setblocking(False)
            return target

        elif msg.type == MessageType.DATA:
            data = extract_data(msg)
            os.write(stdout_fd, data)
            return None

        elif msg.type == MessageType.DETACHED:
            reason = msg.payload.get("reason", "unknown")
            if reason == "session_died":
                os.write(stdout_fd, b"\r\n[Session exited]\r\n")
            return None

        elif msg.type == MessageType.ERROR:
            error = msg.payload.get("message", "Unknown error")
            os.write(stdout_fd, f"\r\n[Error: {error}]\r\n".encode())
            return None

        return None

    def cmd_manager(self) -> int:
        """Run interactive session manager TUI with push-based activity updates."""
        if not self.connect():
            return 1

        try:
            # Subscribe to activity updates
            self.io.send(msg_subscribe())
            response = self.io.recv()

            if response is None:
                print("Server disconnected")
                return 1

            if response.type != MessageType.OK:
                print(f"Failed to subscribe: {response.type}")
                return 1

            return curses.wrapper(lambda stdscr: self._manager_tui_live(stdscr))
        except KeyboardInterrupt:
            return 0
        finally:
            self.disconnect()

    def _manager_tui_live(self, stdscr) -> int:
        """Curses TUI with persistent connection and push-based activity."""
        # Setup curses
        curses.curs_set(0)  # Hide cursor
        stdscr.timeout(100)  # 100ms timeout for responsive activity updates

        # Setup colors for activity indicator (Claude orange/amber)
        curses.start_color()
        curses.use_default_colors()
        # Color pair 1: orange/yellow on default background
        curses.init_pair(1, 208, -1)  # 208 is orange in 256-color palette

        sessions: List[SessionInfo] = []
        cursor = 0
        scroll_offset = 0
        active_session: Optional[str] = None
        message = ""
        message_timeout = 0

        # Track status per session (pushed from server)
        session_status: Dict[str, str] = {}  # session_name -> "active" or "idle"

        # Pending response type we're waiting for
        pending_response: Optional[MessageType] = None

        # Set socket non-blocking for activity polling
        self.sock.setblocking(False)

        def send_and_wait(msg: Message, expected: MessageType, timeout: float = 5.0) -> Optional[Message]:
            """Send message and wait for response, processing status changes in between."""
            nonlocal pending_response
            self.sock.setblocking(True)
            self.io.send(msg)
            self.sock.setblocking(False)
            pending_response = expected

            start = time.time()

            # Wait for the response, processing status change messages
            while time.time() - start < timeout:
                try:
                    self.io.try_read()
                except (OSError, ConnectionError):
                    # Socket error - connection lost
                    pending_response = None
                    return None

                while True:
                    resp = self.io.pop_message()
                    if resp is None:
                        break
                    if resp.type == MessageType.STATUS_CHANGE:
                        name = resp.payload.get("session")
                        status = resp.payload.get("status")
                        if name and status:
                            session_status[name] = status
                    elif resp.type == expected or resp.type == MessageType.ERROR:
                        pending_response = None
                        return resp
                # Small sleep to avoid busy-wait
                time.sleep(0.01)

            # Timeout reached
            pending_response = None
            return None

        def refresh_sessions() -> bool:
            """Fetch session list from server."""
            nonlocal sessions
            response = send_and_wait(msg_list(), MessageType.SESSION_LIST)
            if response and response.type == MessageType.SESSION_LIST:
                sessions = [
                    SessionInfo.from_dict(s)
                    for s in response.payload.get("sessions", [])
                ]
                return True
            return False

        def switch_to_session(name: str) -> bool:
            """Tell server to switch terminal to session."""
            nonlocal active_session
            response = send_and_wait(msg_switch(name), MessageType.OK)
            if response and response.type == MessageType.OK:
                active_session = name
                return True
            return False

        def spawn_session(name: str, command: List[str]) -> bool:
            """Spawn a new session."""
            response = send_and_wait(msg_spawn(name, command), MessageType.OK)
            return response and response.type == MessageType.OK

        def kill_session(name: str) -> bool:
            """Kill a session."""
            response = send_and_wait(msg_kill(name), MessageType.OK)
            return response and response.type == MessageType.OK

        def poll_status():
            """Check for status change messages (non-blocking)."""
            try:
                self.io.try_read()
            except (OSError, ConnectionError):
                return  # Socket error - will be caught on next send_and_wait
            while True:
                msg = self.io.pop_message()
                if msg is None:
                    break
                if msg.type == MessageType.STATUS_CHANGE:
                    name = msg.payload.get("session")
                    status = msg.payload.get("status")
                    if name and status:
                        session_status[name] = status

        def draw():
            """Draw the TUI."""
            nonlocal message_timeout, scroll_offset
            stdscr.clear()
            height, width = stdscr.getmaxyx()

            # Calculate visible area (leave room for footer)
            footer_line1 = "↑↓ select  Enter switch  n new  d delete  q quit"
            footer_lines = 1 if width >= len(footer_line1) else 2
            visible_height = height - footer_lines - 1

            # Adjust scroll if needed
            if cursor < scroll_offset:
                scroll_offset = cursor
            elif cursor >= scroll_offset + visible_height:
                scroll_offset = cursor - visible_height + 1

            # Pulsating dot frames (Claude Code style)
            SPINNER = ['·', '•', '●', '•']
            spinner_frame = int(time.time() * 4) % len(SPINNER)

            # Draw sessions
            if not sessions:
                stdscr.addstr(0, 0, "No sessions. Press 'n' to create one.")
            else:
                for i in range(scroll_offset, min(len(sessions), scroll_offset + visible_height)):
                    session = sessions[i]
                    y = i - scroll_offset

                    # Check status from push-based tracking
                    sess_status = session_status.get(session.name, "idle")
                    is_active = (sess_status == "active")

                    # Build the rest of the line
                    suffix = " *" if session.name == active_session else ""
                    alive_status = "" if session.alive else " (dead)"
                    rest_of_line = f" {session.name}{suffix}{alive_status}"

                    # Determine indicator
                    if is_active:
                        indicator = SPINNER[spinner_frame]
                    elif i == cursor:
                        indicator = ">"
                    else:
                        indicator = " "

                    # Truncate if needed
                    full_line = indicator + rest_of_line
                    if len(full_line) > width - 1:
                        rest_of_line = rest_of_line[:width - 5] + "..."

                    try:
                        # Draw indicator with orange color if active
                        if is_active:
                            stdscr.addstr(y, 0, indicator, curses.color_pair(1) | curses.A_BOLD)
                        else:
                            attr = curses.A_BOLD if i == cursor else curses.A_DIM
                            stdscr.addstr(y, 0, indicator, attr)

                        # Draw rest of line
                        attr = curses.A_BOLD if i == cursor else curses.A_DIM
                        stdscr.addstr(y, 1, rest_of_line, attr)
                    except curses.error:
                        pass

            # Draw message or footer
            footer_line1 = "↑↓ select  Enter switch  n new  d delete  q quit"
            if message and message_timeout > 0:
                try:
                    stdscr.addstr(height - 1, 0, message[:width - 1], curses.A_DIM)
                except curses.error:
                    pass
                message_timeout -= 1
            elif width >= len(footer_line1):
                # Single line footer
                try:
                    stdscr.addstr(height - 1, 0, footer_line1, curses.A_DIM)
                except curses.error:
                    pass
            else:
                # Multiline footer
                line1 = "↑↓ select  Enter switch"
                line2 = "n new  d delete  q quit"
                try:
                    stdscr.addstr(height - 2, 0, line1[:width - 1], curses.A_DIM)
                    stdscr.addstr(height - 1, 0, line2[:width - 1], curses.A_DIM)
                except curses.error:
                    pass

            stdscr.refresh()

        def show_message(msg: str):
            """Show a temporary message."""
            nonlocal message, message_timeout
            message = msg
            message_timeout = 20  # ~2 seconds at 100ms timeout

        def prompt_input(prompt: str) -> Optional[str]:
            """Prompt for text input."""
            curses.echo()
            curses.curs_set(1)
            stdscr.timeout(-1)  # Blocking input

            height, width = stdscr.getmaxyx()
            try:
                stdscr.addstr(height - 1, 0, " " * (width - 1))
                stdscr.addstr(height - 1, 0, prompt)
                stdscr.refresh()
                result = stdscr.getstr(height - 1, len(prompt), width - len(prompt) - 1)
                return result.decode("utf-8").strip()
            except (curses.error, UnicodeDecodeError):
                return None
            finally:
                curses.noecho()
                curses.curs_set(0)
                stdscr.timeout(100)

        # Initial load
        if not refresh_sessions():
            return 1

        last_refresh = time.time()

        while True:
            # Poll for status change messages
            poll_status()

            draw()

            key = stdscr.getch()

            if key == ord('q'):
                break

            elif key == curses.KEY_UP or key == ord('k'):
                if cursor > 0:
                    cursor -= 1

            elif key == curses.KEY_DOWN or key == ord('j'):
                if cursor < len(sessions) - 1:
                    cursor += 1

            elif key == ord('\n') or key == curses.KEY_ENTER:
                if sessions and 0 <= cursor < len(sessions):
                    session = sessions[cursor]
                    if session.alive:
                        if switch_to_session(session.name):
                            show_message(f"Switched to {session.name}")
                        else:
                            show_message("Failed to switch")
                    else:
                        show_message("Session is dead")
                    refresh_sessions()

            elif key == ord('n'):
                name = prompt_input("Name: ")
                if name:
                    cmd = prompt_input("Command: ")
                    if cmd:
                        command = cmd.split()
                        if spawn_session(name, command):
                            show_message(f"Spawned {name}")
                        else:
                            show_message("Failed to spawn")
                        refresh_sessions()

            elif key == ord('d'):
                if sessions and 0 <= cursor < len(sessions):
                    session = sessions[cursor]
                    if kill_session(session.name):
                        show_message(f"Killed {session.name}")
                        if active_session == session.name:
                            active_session = None
                    else:
                        show_message("Failed to kill")
                    refresh_sessions()
                    if cursor >= len(sessions) and cursor > 0:
                        cursor -= 1

            elif key == ord('r'):
                refresh_sessions()
                show_message("Refreshed")

            elif key == -1:
                # Timeout - periodically refresh session list (every 5 seconds)
                now = time.time()
                if now - last_refresh > 5:
                    refresh_sessions()
                    last_refresh = now

        return 0
