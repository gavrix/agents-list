"""Protocol for client-server communication over Unix socket."""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class MessageType(str, Enum):
    """Types of messages in the protocol."""
    # Commands
    SPAWN = "spawn"
    LIST = "list"
    ATTACH = "attach"
    DETACH = "detach"
    KILL = "kill"
    SHUTDOWN = "shutdown"

    # Responses
    OK = "ok"
    ERROR = "error"
    SESSION_LIST = "session_list"
    ATTACHED = "attached"
    DETACHED = "detached"

    # I/O during attach
    DATA = "data"  # Binary data for PTY I/O
    RESIZE = "resize"  # Terminal resize event

    # Terminal client
    REGISTER_TERMINAL = "register_terminal"  # Terminal identifies itself
    SWITCH = "switch"  # Switch terminal to a session
    SWITCHED = "switched"  # Terminal confirms switch

    # Status subscription
    SUBSCRIBE = "subscribe"  # Subscribe to status notifications
    STATUS_CHANGE = "status_change"  # Server pushes session status change


@dataclass
class Message:
    """A protocol message."""
    type: MessageType
    payload: Dict[str, Any]

    def to_bytes(self) -> bytes:
        """Serialize message to bytes with length prefix."""
        data = json.dumps({
            "type": self.type.value,
            "payload": self.payload
        }).encode("utf-8")
        # 4-byte length prefix (big-endian)
        return struct.pack(">I", len(data)) + data

    @classmethod
    def from_bytes(cls, data: bytes) -> "Message":
        """Deserialize message from bytes (without length prefix)."""
        obj = json.loads(data.decode("utf-8"))
        return cls(
            type=MessageType(obj["type"]),
            payload=obj.get("payload", {})
        )


@dataclass
class SessionInfo:
    """Serializable session information."""
    id: int
    name: str
    command: List[str]
    alive: bool
    last_activity: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SessionInfo":
        return cls(**d)


# Message constructors for convenience

def msg_spawn(name: str, command: List[str]) -> Message:
    return Message(MessageType.SPAWN, {"name": name, "command": command})


def msg_list() -> Message:
    return Message(MessageType.LIST, {})


def msg_attach(id_or_name: str, rows: int, cols: int) -> Message:
    return Message(MessageType.ATTACH, {"target": id_or_name, "rows": rows, "cols": cols})


def msg_detach() -> Message:
    return Message(MessageType.DETACH, {})


def msg_kill(id_or_name: str) -> Message:
    return Message(MessageType.KILL, {"target": id_or_name})


def msg_shutdown() -> Message:
    return Message(MessageType.SHUTDOWN, {})


def msg_ok(message: str = "") -> Message:
    return Message(MessageType.OK, {"message": message})


def msg_error(message: str) -> Message:
    return Message(MessageType.ERROR, {"message": message})


def msg_session_list(sessions: List[SessionInfo]) -> Message:
    return Message(MessageType.SESSION_LIST, {
        "sessions": [s.to_dict() for s in sessions]
    })


def msg_attached(session_name: str) -> Message:
    return Message(MessageType.ATTACHED, {"name": session_name})


def msg_detached(reason: str) -> Message:
    return Message(MessageType.DETACHED, {"reason": reason})


def msg_data(data: bytes) -> Message:
    """Wrap binary data in a message. Data is base64 encoded."""
    import base64
    return Message(MessageType.DATA, {"data": base64.b64encode(data).decode("ascii")})


def msg_resize(rows: int, cols: int) -> Message:
    return Message(MessageType.RESIZE, {"rows": rows, "cols": cols})


def extract_data(msg: Message) -> bytes:
    """Extract binary data from a DATA message."""
    import base64
    return base64.b64decode(msg.payload["data"])


def msg_register_terminal() -> Message:
    return Message(MessageType.REGISTER_TERMINAL, {})


def msg_switch(target: str, scrollback: bytes = b"") -> Message:
    import base64
    payload = {"target": target}
    if scrollback:
        payload["scrollback"] = base64.b64encode(scrollback).decode("ascii")
    return Message(MessageType.SWITCH, payload)


def msg_switched(session_name: str) -> Message:
    return Message(MessageType.SWITCHED, {"name": session_name})


def msg_subscribe() -> Message:
    return Message(MessageType.SUBSCRIBE, {})


def msg_status_change(session_name: str, status: str) -> Message:
    return Message(MessageType.STATUS_CHANGE, {"session": session_name, "status": status})


class SocketIO:
    """Helper for reading/writing messages on a socket."""

    def __init__(self, sock):
        self.sock = sock
        self._buffer = b""

    def send(self, msg: Message) -> None:
        """Send a message."""
        self.sock.sendall(msg.to_bytes())

    def recv(self) -> Optional[Message]:
        """Receive a message (blocking). Returns None on connection close."""
        # Read length prefix
        while len(self._buffer) < 4:
            chunk = self.sock.recv(4096)
            if not chunk:
                return None
            self._buffer += chunk

        length = struct.unpack(">I", self._buffer[:4])[0]

        # Read message body
        while len(self._buffer) < 4 + length:
            chunk = self.sock.recv(4096)
            if not chunk:
                return None
            self._buffer += chunk

        data = self._buffer[4:4 + length]
        self._buffer = self._buffer[4 + length:]

        return Message.from_bytes(data)

    def try_read(self) -> bool:
        """
        Try to read data from socket into buffer (non-blocking).
        Socket must already be in non-blocking mode.
        Returns True if data was read, False otherwise.
        """
        try:
            chunk = self.sock.recv(4096)
            if chunk:
                self._buffer += chunk
                return True
        except BlockingIOError:
            pass
        except OSError:
            pass
        return False

    def pop_message(self) -> Optional[Message]:
        """
        Pop a complete message from buffer if available.
        Returns None if no complete message in buffer.
        """
        if len(self._buffer) < 4:
            return None

        length = struct.unpack(">I", self._buffer[:4])[0]
        if len(self._buffer) < 4 + length:
            return None

        data = self._buffer[4:4 + length]
        self._buffer = self._buffer[4 + length:]

        return Message.from_bytes(data)

