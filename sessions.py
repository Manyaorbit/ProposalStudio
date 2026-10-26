"""In-memory-only session and work store.

Nothing here is ever written to disk. A session's "work" (the sample document,
the research, the section choices) is wiped when:
  * the proposal is downloaded,
  * the user logs out or presses "Start over",
  * the browser tab is closed or reloaded,
  * the session has been idle for the idle timeout,
  * the server stops (it lives in RAM, so there is nothing to recover).
"""
import gc
import secrets
import threading
import time

from config import cfg


def _zero(buf):
    """Best-effort overwrite of a bytearray before it is released."""
    if isinstance(buf, bytearray):
        buf[:] = bytes(len(buf))


class Work:
    """Everything about the proposal currently in progress."""

    def __init__(self):
        self.sample = None          # bytearray: the sample as .docx (PDFs are converted on upload)
        self.kind = None            # "docx" or "pdf" (what the user uploaded)
        self.section_ids = []
        self.fixed = set()
        self.company = ""
        self.location = ""
        self.notes = ""
        self.profile = ""
        self.sources = []

    def clear(self):
        _zero(self.sample)
        self.__init__()


class Session:
    def __init__(self):
        self.sid = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.authed = False
        self.last_seen = time.monotonic()
        self.work = Work()
        self.lock = threading.RLock()


class Store:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}

    def create(self) -> Session:
        """Create the one active session. A new login ends any older session."""
        with self._lock:
            for old in self._sessions.values():
                old.work.clear()
            self._sessions.clear()
            s = Session()
            self._sessions[s.sid] = s
            return s

    def get(self, sid):
        if not sid:
            return None
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                return None
            if time.monotonic() - s.last_seen > cfg.idle_seconds:
                s.work.clear()
                del self._sessions[sid]
                gc.collect()
                return None
            s.last_seen = time.monotonic()
            return s

    def wipe_work(self, sid):
        with self._lock:
            s = self._sessions.get(sid)
        if s:
            with s.lock:
                s.work.clear()
        gc.collect()

    def destroy(self, sid):
        with self._lock:
            s = self._sessions.pop(sid, None)
        if s:
            with s.lock:
                s.work.clear()
        gc.collect()

    def sweep(self):
        now = time.monotonic()
        with self._lock:
            expired = [k for k, s in self._sessions.items() if now - s.last_seen > cfg.idle_seconds]
            for k in expired:
                self._sessions[k].work.clear()
                del self._sessions[k]
        if expired:
            gc.collect()
        return len(expired)

    def wipe_all(self):
        with self._lock:
            for s in self._sessions.values():
                s.work.clear()
            self._sessions.clear()
        gc.collect()

    def start_sweeper(self, interval=30):
        def loop():
            while True:
                time.sleep(interval)
                self.sweep()
        threading.Thread(target=loop, name="idle-sweeper", daemon=True).start()


STORE = Store()
