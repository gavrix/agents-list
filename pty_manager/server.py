"""Server - manages PTY sessions and handles client connections."""

from __future__ import annotations

import os
import socket
import select
import signal
import threading
from pathlib import Path
from typing import Dict, Optional

from pty_manager.manager import Manager
from pty_manager.session import Session
from pty_manager.protocol import (
    Message, MessageType, SessionInfo, SocketIO,
    msg_ok, msg_error, msg_session_list, msg_attached, msg_detached,
    msg_data, msg_switch, msg_switched, extract_data, msg_status_change,
)


def get_socket_path() -> Path:
    """Get the Unix socket path."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    return Path(runtime_dir) / f"pty-manager-{os.getuid()}.sock"


class ClientHandler(threading.Thread):
    """Handles a single client connection."""

    def __init__(self, server: "Server", conn: socket.socket, addr):
        super().__init__(daemon=True)
        self.server = server
        self.conn = conn
        self.addr = addr
        self.io = SocketIO(conn)
        self.attached_session: Optional[Session] = None
        self.is_terminal = False  # True if this is the terminal client
        self.running = True

    def run(self):
        """Handle client messages."""
        try:
            while self.running:
                msg = self.io.recv()
                if msg is None:
                    break
                self.handle_message(msg)
        except Exception as e:
            print(f"Client error: {e}")
        finally:
            self.cleanup()

    def handle_message(self, msg: Message):
        """Dispatch message to handler."""
        handlers = {
            MessageType.LIST: self.handle_list,
            MessageType.SPAWN: self.handle_spawn,
            MessageType.KILL: self.handle_kill,
            MessageType.ATTACH: self.handle_attach,
            MessageType.SHUTDOWN: self.handle_shutdown,
            MessageType.REGISTER_TERMINAL: self.handle_register_terminal,
            MessageType.SWITCH: self.handle_switch,
            MessageType.SUBSCRIBE: self.handle_subscribe,
        }
        handler = handlers.get(msg.type)
        if handler:
            handler(msg)
        else:
            self.io.send(msg_error(f"Unknown message type: {msg.type}"))

    def handle_list(self, msg: Message):
        """List all sessions."""
        self.server.manager.cleanup_dead()
        sessions = self.server.manager.list()
        infos = [
            SessionInfo(
                id=s.id,
                name=s.name,
                command=s.command,
                alive=s.is_alive(),
                last_activity=s.last_activity
            )
            for s in sessions
        ]
        self.io.send(msg_session_list(infos))

    def handle_spawn(self, msg: Message):
        """Spawn a new session."""
        name = msg.payload.get("name")
        command = msg.payload.get("command")

        if not name or not command:
            self.io.send(msg_error("name and command are required"))
            return

        try:
            session = self.server.manager.spawn(name, command)
            session.set_status_callback(self.server.on_session_status_change)
            self.io.send(msg_ok(f"Spawned session '{session.name}' (id={session.id})"))
        except ValueError as e:
            self.io.send(msg_error(str(e)))
        except Exception as e:
            self.io.send(msg_error(f"Failed to spawn: {e}"))

    def handle_kill(self, msg: Message):
        """Kill a session."""
        target = msg.payload.get("target")
        if not target:
            self.io.send(msg_error("target is required"))
            return

        if self.server.manager.kill(target):
            self.io.send(msg_ok(f"Killed session: {target}"))
        else:
            self.io.send(msg_error(f"Session not found: {target}"))

    def handle_attach(self, msg: Message):
        """Attach client to a session."""
        target = msg.payload.get("target")
        rows = msg.payload.get("rows", 24)
        cols = msg.payload.get("cols", 80)

        if not target:
            self.io.send(msg_error("target is required"))
            return

        session = self.server.manager.get(target)
        if session is None:
            self.io.send(msg_error(f"Session not found: {target}"))
            return

        if not session.is_alive():
            self.server.manager.cleanup_dead()
            self.io.send(msg_error(f"Session '{session.name}' is not running"))
            return

        # Set initial size
        session.resize(rows, cols)

        # Enter attached mode
        self.attached_session = session
        self.io.send(msg_attached(session.name))

        # Start I/O proxy loop
        self.proxy_io()

    def proxy_io(self):
        """Proxy I/O between client socket and PTY."""
        session = self.attached_session
        if not session:
            return

        # Keep socket non-blocking throughout
        self.conn.setblocking(False)

        try:
            while self.attached_session:
                # First, drain any buffered messages
                while True:
                    msg = self.io.pop_message()
                    if msg is None:
                        break
                    if self._handle_client_message(msg, session) == "break":
                        return

                # Check for messages or PTY output
                readable = [self.conn, session.master_fd]
                try:
                    ready, _, _ = select.select(readable, [], [], 0.1)
                except (select.error, ValueError):
                    break

                # Check if session is still alive
                if not session.is_alive():
                    self.conn.setblocking(True)
                    self.io.send(msg_detached("session_died"))
                    self.conn.setblocking(False)
                    self.attached_session = None
                    break

                # Handle client input - read into buffer then drain
                if self.conn in ready:
                    self.io.try_read()
                    while True:
                        msg = self.io.pop_message()
                        if msg is None:
                            break
                        if self._handle_client_message(msg, session) == "break":
                            return

                # Handle PTY output
                if session.master_fd in ready:
                    data = session.read()
                    if data:
                        self.conn.setblocking(True)
                        self.io.send(msg_data(data))
                        self.conn.setblocking(False)

        finally:
            self.conn.setblocking(True)
            self.attached_session = None

    def _handle_client_message(self, msg: Message, session: Session) -> Optional[str]:
        """Handle a message from the client. Returns 'break' to exit loop, None to continue."""
        if msg.type == MessageType.DETACH:
            self.conn.setblocking(True)
            self.io.send(msg_detached("user_detach"))
            self.conn.setblocking(False)
            self.attached_session = None
            return "break"
        elif msg.type == MessageType.DATA:
            # Forward data to PTY
            data = extract_data(msg)
            try:
                session.write(data)
            except OSError:
                self.conn.setblocking(True)
                self.io.send(msg_detached("pty_error"))
                self.conn.setblocking(False)
                self.attached_session = None
                return "break"
        elif msg.type == MessageType.RESIZE:
            rows = msg.payload.get("rows", 24)
            cols = msg.payload.get("cols", 80)
            try:
                session.resize(rows, cols)
            except OSError:
                self.conn.setblocking(True)
                self.io.send(msg_detached("pty_error"))
                self.conn.setblocking(False)
                self.attached_session = None
                return "break"
        return None

    def handle_shutdown(self, msg: Message):
        """Shutdown the server."""
        self.io.send(msg_ok("Server shutting down"))
        self.server.shutdown()

    def handle_register_terminal(self, msg: Message):
        """Register this client as the terminal."""
        if self.server.terminal_client is not None:
            self.io.send(msg_error("Terminal already connected"))
            return

        self.is_terminal = True
        self.server.terminal_client = self
        self.io.send(msg_ok("Registered as terminal"))

        # Enter terminal loop - wait for SWITCH commands and proxy I/O
        self.terminal_loop()

    def terminal_loop(self):
        """Main loop for terminal client - wait for SWITCH and proxy I/O."""
        self.conn.setblocking(False)

        try:
            while self.running:
                # If attached to a session, also watch its PTY
                fds = [self.conn]
                if self.attached_session and self.attached_session.is_alive():
                    fds.append(self.attached_session.master_fd)

                try:
                    ready, _, _ = select.select(fds, [], [], 0.1)
                except (select.error, ValueError):
                    break

                # Check if attached session died
                if self.attached_session and not self.attached_session.is_alive():
                    self.conn.setblocking(True)
                    self.io.send(msg_detached("session_died"))
                    self.conn.setblocking(False)
                    self.attached_session = None
                    self.server.active_session = None

                # Handle messages from server (SWITCH) or data from client
                if self.conn in ready:
                    self.io.try_read()
                    while True:
                        msg = self.io.pop_message()
                        if msg is None:
                            break
                        result = self._handle_terminal_message(msg)
                        if result == "break":
                            return

                # Handle PTY output
                if self.attached_session and self.attached_session.master_fd in ready:
                    data = self.attached_session.read()
                    if data:
                        self.conn.setblocking(True)
                        self.io.send(msg_data(data))
                        self.conn.setblocking(False)

        finally:
            self.conn.setblocking(True)
            self.attached_session = None

    def _handle_terminal_message(self, msg: Message) -> Optional[str]:
        """Handle a message in terminal mode."""
        if msg.type == MessageType.SWITCH:
            target = msg.payload.get("target")
            session = self.server.manager.get(target)

            if session is None:
                self.conn.setblocking(True)
                self.io.send(msg_error(f"Session not found: {target}"))
                self.conn.setblocking(False)
                return None

            if not session.is_alive():
                self.conn.setblocking(True)
                self.io.send(msg_error(f"Session not alive: {target}"))
                self.conn.setblocking(False)
                return None

            # Switch to new session
            self.attached_session = session
            self.server.active_session = session
            self.conn.setblocking(True)
            self.io.send(msg_switched(session.name))
            self.conn.setblocking(False)
            return None

        elif msg.type == MessageType.DATA:
            if self.attached_session:
                data = extract_data(msg)
                try:
                    self.attached_session.write(data)
                except OSError:
                    self.conn.setblocking(True)
                    self.io.send(msg_detached("pty_error"))
                    self.conn.setblocking(False)
                    self.attached_session = None
                    self.server.active_session = None
            return None

        elif msg.type == MessageType.RESIZE:
            if self.attached_session:
                rows = msg.payload.get("rows", 24)
                cols = msg.payload.get("cols", 80)
                try:
                    self.attached_session.resize(rows, cols)
                except OSError:
                    self.conn.setblocking(True)
                    self.io.send(msg_detached("pty_error"))
                    self.conn.setblocking(False)
                    self.attached_session = None
                    self.server.active_session = None
            return None

        elif msg.type == MessageType.DETACH:
            self.conn.setblocking(True)
            self.io.send(msg_detached("user_detach"))
            self.conn.setblocking(False)
            self.attached_session = None
            self.server.active_session = None
            return None

        return None

    def handle_switch(self, msg: Message):
        """Handle SWITCH from manager - forward to terminal."""
        target = msg.payload.get("target")

        if self.server.terminal_client is None:
            self.io.send(msg_error("No terminal connected"))
            return

        session = self.server.manager.get(target)
        if session is None:
            self.io.send(msg_error(f"Session not found: {target}"))
            return

        if not session.is_alive():
            self.io.send(msg_error(f"Session not alive: {target}"))
            return

        # Get scrollback before switching
        scrollback_data = session.get_scrollback()

        # Tell terminal to switch (include scrollback)
        terminal = self.server.terminal_client
        terminal.conn.setblocking(True)
        terminal.io.send(msg_switch(target, scrollback_data))
        terminal.conn.setblocking(False)

        # Update server and terminal state
        terminal.attached_session = session
        self.server.active_session = session

        # Force redraw of the new session
        session.force_redraw()

        self.io.send(msg_ok(f"Switched to {session.name}"))

    def handle_subscribe(self, msg: Message):
        """Subscribe to activity notifications."""
        with self.server.subscribers_lock:
            if self not in self.server.activity_subscribers:
                self.server.activity_subscribers.append(self)
        self.io.send(msg_ok("Subscribed to activity"))
        # Keep connection open - enter subscriber loop
        self.subscriber_loop()

    def subscriber_loop(self):
        """Wait for messages while subscribed (handle LIST, SPAWN, KILL, SWITCH)."""
        self.conn.setblocking(False)
        try:
            while self.running:
                # Check for incoming messages
                try:
                    readable, _, _ = select.select([self.conn], [], [], 0.1)
                    if self.conn in readable:
                        self.io.try_read()
                        while True:
                            msg = self.io.pop_message()
                            if msg is None:
                                break
                            # Handle messages directly (not via handle_message to avoid recursion)
                            if msg.type == MessageType.LIST:
                                self.handle_list(msg)
                            elif msg.type == MessageType.SPAWN:
                                self.handle_spawn(msg)
                            elif msg.type == MessageType.KILL:
                                self.handle_kill(msg)
                            elif msg.type == MessageType.SWITCH:
                                self.handle_switch(msg)
                            elif msg.type == MessageType.SHUTDOWN:
                                self.handle_shutdown(msg)
                                return
                except (select.error, ValueError):
                    break
        finally:
            self.conn.setblocking(True)
            with self.server.subscribers_lock:
                if self in self.server.activity_subscribers:
                    self.server.activity_subscribers.remove(self)

    def cleanup(self):
        """Clean up client connection."""
        self.running = False
        self.attached_session = None

        # If this was the terminal client, clear it
        if self.is_terminal and self.server.terminal_client is self:
            self.server.terminal_client = None
            self.server.active_session = None

        try:
            self.conn.close()
        except Exception:
            pass
        # Remove self from server's client list
        try:
            self.server.clients.remove(self)
        except ValueError:
            pass


class Server:
    """PTY Manager server."""

    def __init__(self):
        self.manager = Manager()
        self.socket_path = get_socket_path()
        self.sock: Optional[socket.socket] = None
        self.clients: list[ClientHandler] = []
        self.running = False
        self.terminal_client: Optional[ClientHandler] = None
        self.active_session: Optional[Session] = None
        self.activity_subscribers: list[ClientHandler] = []
        self.subscribers_lock = threading.Lock()

    def start(self):
        """Start the server."""
        # Remove stale socket
        if self.socket_path.exists():
            self.socket_path.unlink()

        # Create socket with restricted permissions
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)  # Owner read/write only
        self.sock.listen(5)
        self.sock.settimeout(0.1)  # 100ms for responsive activity checks

        self.running = True
        print(f"PTY Manager server started")
        print(f"Socket: {self.socket_path}")
        print("Press Ctrl+C to stop")
        print()

        # Set up signal handler
        def handle_sigint(signum, frame):
            print("\nShutting down...")
            self.shutdown()

        signal.signal(signal.SIGINT, handle_sigint)
        signal.signal(signal.SIGTERM, handle_sigint)

        try:
            while self.running:
                try:
                    conn, addr = self.sock.accept()
                    handler = ClientHandler(self, conn, addr)
                    self.clients.append(handler)
                    handler.start()
                except socket.timeout:
                    # Read from non-attached sessions to detect activity
                    for s in self.manager.list():
                        if s != self.active_session and s.is_alive():
                            s.read()  # This triggers status change to "active" if data
                        s.check_idle_timeout()  # Check if should go idle
                    # Check for dead sessions periodically
                    dead = self.manager.cleanup_dead()
                    for s in dead:
                        print(f"[Session '{s.name}' exited]")
                except OSError:
                    if self.running:
                        raise
                    break
        finally:
            self.cleanup()

    def shutdown(self):
        """Shutdown the server."""
        self.running = False

    def notify_status_change(self, session_name: str, status: str):
        """Notify all subscribers about session status change."""
        # Take snapshot under lock to avoid iteration during mutation
        with self.subscribers_lock:
            subscribers = list(self.activity_subscribers)

        for client in subscribers:
            try:
                client.conn.setblocking(True)
                client.io.send(msg_status_change(session_name, status))
                client.conn.setblocking(False)
            except Exception:
                with self.subscribers_lock:
                    if client in self.activity_subscribers:
                        self.activity_subscribers.remove(client)

    def on_session_status_change(self, session: Session, old_status: str, new_status: str):
        """Callback when a session's status changes."""
        self.notify_status_change(session.name, new_status)

    def cleanup(self):
        """Clean up server resources."""
        # Kill all sessions
        print("Killing all sessions...")
        self.manager.kill_all()

        # Close all client connections
        for client in self.clients:
            client.running = False

        # Close and remove socket
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except Exception:
                pass

        print("Server stopped")
