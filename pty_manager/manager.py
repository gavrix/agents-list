"""Manager class - maintains registry of active sessions."""

from __future__ import annotations

import threading
from typing import Dict, List, Optional, Union

from pty_manager.session import Session


class Manager:
    """Manages multiple PTY sessions. Thread-safe."""

    def __init__(self):
        self._sessions: Dict[int, Session] = {}
        self._next_id: int = 1
        self._name_to_id: Dict[str, int] = {}
        self._lock = threading.Lock()

    def spawn(self, name: str, command: List[str]) -> Session:
        """Spawn a new session."""
        with self._lock:
            if name in self._name_to_id:
                raise ValueError(f"Session with name '{name}' already exists")

            session_id = self._next_id
            self._next_id += 1

        # Spawn outside lock (can take time)
        session = Session.spawn(session_id, name, command)

        with self._lock:
            self._sessions[session_id] = session
            self._name_to_id[name] = session_id

        return session

    def get(self, id_or_name: Union[int, str]) -> Optional[Session]:
        """Get a session by ID or name."""
        with self._lock:
            if isinstance(id_or_name, int):
                return self._sessions.get(id_or_name)
            else:
                session_id = self._name_to_id.get(id_or_name)
                if session_id is not None:
                    return self._sessions.get(session_id)
                try:
                    return self._sessions.get(int(id_or_name))
                except ValueError:
                    return None

    def list(self) -> List[Session]:
        """List all sessions."""
        with self._lock:
            return list(self._sessions.values())

    def kill(self, id_or_name: Union[int, str]) -> bool:
        """Kill a session by ID or name."""
        with self._lock:
            session = self._get_unlocked(id_or_name)
            if session is None:
                return False
            self._remove_unlocked(session)

        session.terminate()
        return True

    def _get_unlocked(self, id_or_name: Union[int, str]) -> Optional[Session]:
        """Get session without lock (caller must hold lock)."""
        if isinstance(id_or_name, int):
            return self._sessions.get(id_or_name)
        else:
            session_id = self._name_to_id.get(id_or_name)
            if session_id is not None:
                return self._sessions.get(session_id)
            try:
                return self._sessions.get(int(id_or_name))
            except ValueError:
                return None

    def _remove_unlocked(self, session: Session) -> None:
        """Remove a session from the registry (caller must hold lock)."""
        if session.id in self._sessions:
            del self._sessions[session.id]
        if session.name in self._name_to_id:
            del self._name_to_id[session.name]

    def cleanup_dead(self) -> List[Session]:
        """Remove dead sessions from registry."""
        with self._lock:
            dead = [s for s in self._sessions.values() if not s.is_alive()]
            for session in dead:
                self._remove_unlocked(session)
            return dead

    def kill_all(self) -> None:
        """Kill all sessions."""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._name_to_id.clear()

        for session in sessions:
            session.terminate()
