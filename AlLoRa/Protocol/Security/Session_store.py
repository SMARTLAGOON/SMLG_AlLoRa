"""Keeps a node's live secure sessions by session id: get, put, drop.

Sessions live in RAM and are lost on reboot, so a rebooted node does the handshake again.
"""


class Session_store:
    """Interface for session custody. Subclass and implement all three methods."""

    def get(self, sid):
        """Return the Session for ``sid``, or None if there is none."""
        raise NotImplementedError

    def put(self, session):
        """Store ``session`` under its own ``session.sid``, replacing any existing one."""
        raise NotImplementedError

    def drop(self, sid):
        """Forget the session for ``sid``. A no-op if there is none (idempotent)."""
        raise NotImplementedError


class RAM_session_store(Session_store):
    """v3.0.0 default: sessions live in a dict in RAM and are lost on reboot."""

    def __init__(self):
        self._sessions = {}

    def get(self, sid):
        return self._sessions.get(sid)

    def put(self, session):
        self._sessions[session.sid] = session

    def drop(self, sid):
        self._sessions.pop(sid, None)
