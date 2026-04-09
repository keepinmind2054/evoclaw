"""
EvoClaw Web Portal — Browser-based chat interface (stdlib only, no FastAPI)
Uses HTTP polling instead of WebSocket for simplicity.
Endpoint: http://localhost:8766/
"""
import base64
import hmac
import http.server
import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from . import config, db

log = logging.getLogger(__name__)

# In-memory session store: session_id -> {jid, messages: [{role, text, ts}]}
_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()

_SESSION_TTL_SECONDS = 3600  # 1 hour
_poll_count = 0  # counter for lazy expiry (expire every 60 polls)

# Caps to prevent unbounded memory growth
_MAX_SESSIONS = 500            # maximum concurrent WebPortal sessions
_MAX_SESSION_MESSAGES = 200    # maximum messages stored per session
_MAX_BODY_SIZE = 64 * 1024     # 64 KB — reject larger POST bodies (prevent OOM)
_MAX_TEXT_SIZE = 32 * 1024     # 32 KB — reject individual messages larger than this

# BUG-WP-07 (MEDIUM): JID whitespace-only strings were accepted as valid.
# Validate JID: must be non-empty after stripping and contain only safe chars.
import re as _re
_JID_RE = _re.compile(r'^[\w@.\-:+]{1,256}$')


def _expire_sessions() -> None:
    """Remove sessions that have been idle longer than _SESSION_TTL_SECONDS."""
    now = time.time()
    expired = [
        sid for sid, sess in _sessions.items()
        if now - sess.get("last_seen", now) > _SESSION_TTL_SECONDS
    ]
    for sid in expired:
        _sessions.pop(sid, None)
    if expired:
        log.debug("WebPortal: expired %d stale session(s)", len(expired))


def _get_registered_groups() -> list[dict]:
    try:
        return db.get_all_registered_groups()
    except Exception:
        return []


def _check_auth(handler: "http.server.BaseHTTPRequestHandler") -> bool:
    """Return True if the request is authenticated (or if auth is disabled).

    Authentication is enabled when DASHBOARD_PASSWORD is non-empty (re-uses the
    same credential already configured for the dashboard).  Sends a 401 with
    WWW-Authenticate header when the credential is missing or wrong.

    BUG-WP-01 (HIGH): The original comparison `provided_pw == password` is a
    plain string equality check, which is vulnerable to timing side-channel
    attacks.  Fixed by using hmac.compare_digest() for constant-time comparison.
    """
    password = config.DASHBOARD_PASSWORD
    if not password:
        return True  # Auth disabled — allow all requests
    auth_header = handler.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8", errors="replace")
            _username, _, provided_pw = decoded.partition(":")
            # BUG-WP-01 FIX: use constant-time comparison to prevent timing attacks
            if hmac.compare_digest(provided_pw, password):
                return True
        except Exception:
            pass
    # Send 401 Unauthorized
    handler.send_response(401)
    handler.send_header("WWW-Authenticate", 'Basic realm="EvoClaw Web Portal"')
    handler.send_header("Content-Length", "0")
    handler.end_headers()
    return False


# Security headers applied to every response.  Defined once for consistency
# with Phase 13A's additions to dashboard.py (BUG-WP-04).
_SECURITY_HEADERS: list[tuple[str, str]] = [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    (
        "Content-Security-Policy",
        "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline';",
    ),
]


class _WebPortalHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug(f"WebPortal: {fmt % args}")

    def _add_security_headers(self) -> None:
        """BUG-WP-04 FIX: emit security headers on every authenticated response."""
        for name, value in _SECURITY_HEADERS:
            self.send_header(name, value)

    def do_GET(self):
        # /health is unauthenticated (used by health checkers)
        if self.path == "/health":
            self._send_json({"status": "ok"})
            return
        if not _check_auth(self):
            return
        if self.path == "/" or self.path == "/index.html":
            self._serve_html()
        elif self.path.startswith("/api/groups"):
            self._api_groups()
        elif self.path.startswith("/api/poll"):
            self._api_poll()
        else:
            # BUG-WP-09 FIX: 404 responses were missing end_headers(), causing
            # the HTTP response to be incomplete and the connection to hang.
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):
        if not _check_auth(self):
            return
        if self.path == "/api/send":
            self._api_send()
        elif self.path == "/api/session":
            self._api_new_session()
        else:
            # BUG-WP-09 FIX: 404 responses were missing end_headers()
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except (ValueError, TypeError):
            length = 0
        # BUG-WP-03 FIX: A crafted request with a negative Content-Length
        # would pass the upper-bound check (negative < 64 KB) and then cause
        # rfile.read() to read() with a negative argument, which returns b""
        # silently on CPython but is technically undefined.  Reject it explicitly.
        if length < 0:
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            raise ValueError(f"Invalid Content-Length: {length}")
        if length > _MAX_BODY_SIZE:
            self.send_response(413)
            self.send_header("Content-Length", "0")
            self.end_headers()
            raise ValueError(f"Request body too large: {length} bytes (max {_MAX_BODY_SIZE})")
        return self.rfile.read(length) if length else b""

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # BUG-WP-04 FIX: add security headers to JSON responses too
        self._add_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _api_new_session(self):
        try:
            body = json.loads(self._read_body())
            jid = body.get("jid", "").strip()
        except Exception:
            jid = ""
        session_id = str(uuid.uuid4())
        # Generate a per-session CSRF token. The client must echo this token as
        # the X-CSRF-Token header on all subsequent POST requests. Because custom
        # headers cannot be sent cross-origin without a CORS preflight (which this
        # server never approves), this blocks cross-site request forgery attacks
        # even when the browser re-sends Basic Auth credentials automatically.
        csrf_token = str(uuid.uuid4())
        with _sessions_lock:
            # Evict stale sessions before checking the cap
            _expire_sessions()
            if len(_sessions) >= _MAX_SESSIONS:
                self._send_json({"error": "Too many active sessions. Try again later."}, 503)
                return
            _sessions[session_id] = {
                "jid": jid,
                "messages": [],
                "created": time.time(),
                "last_seen": time.time(),
                "csrf_token": csrf_token,
            }
        self._send_json({"session_id": session_id, "csrf_token": csrf_token})

    def _api_groups(self):
        groups = _get_registered_groups()
        self._send_json({"groups": [{"jid": g["jid"], "name": g.get("name", g["folder"]), "folder": g["folder"]} for g in groups]})

    def _api_poll(self):
        """Return new messages since a given timestamp."""
        global _poll_count
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        session_id = qs.get("session_id", [""])[0]
        # BUG-WP-02 FIX: The original code used `float(qs.get("since", ["0"])[0])`
        # with no try/except.  A crafted request with since=abc would propagate an
        # unhandled ValueError up through the handler, causing a 500 internal error
        # response.  Clamp to 0.0 on any parse failure.
        try:
            since = float(qs.get("since", ["0"])[0])
            if since < 0:
                since = 0.0
        except (ValueError, TypeError):
            since = 0.0
        with _sessions_lock:
            session = _sessions.get(session_id)
            if session is not None:
                session["last_seen"] = time.time()
            msgs = [m for m in (session or {}).get("messages", []) if m["ts"] > since]
            # Lazy expiry: run every 60 polls to avoid overhead on every request
            _poll_count += 1
            if _poll_count % 60 == 0:
                _expire_sessions()
        self._send_json({"messages": msgs})

    def _api_send(self):
        """User sends a message — write it to DB so the main loop picks it up."""
        try:
            body = json.loads(self._read_body())
            session_id = body.get("session_id", "")
            text = body.get("text", "").strip()
            if not text:
                self._send_json({"error": "empty message"}, 400)
                return
            # Reject excessively large messages before hitting the DB
            if len(text) > _MAX_TEXT_SIZE:
                self._send_json({"error": f"Message too large (max {_MAX_TEXT_SIZE} chars)"}, 413)
                return
            with _sessions_lock:
                session = _sessions.get(session_id)
            if not session:
                self._send_json({"error": "invalid session"}, 400)
                return
            # CSRF validation: require the per-session token as a custom header.
            # Custom headers cannot be sent cross-origin without a CORS preflight,
            # so this blocks CSRF attacks even when Basic Auth is cached by the browser.
            expected_csrf = session.get("csrf_token", "")
            provided_csrf = self.headers.get("X-CSRF-Token", "")
            if expected_csrf and not hmac.compare_digest(provided_csrf, expected_csrf):
                self._send_json({"error": "CSRF token mismatch"}, 403)
                return
            jid = session.get("jid", "")
            if not jid:
                self._send_json({"error": "no group selected"}, 400)
                return
            # BUG-WP-07 FIX: Validate the JID stored in the session against the
            # allowlist pattern.  An empty or malformed JID could slip through
            # _api_new_session if the client supplied one; reject it here before
            # it reaches the DB layer.
            if not _JID_RE.match(jid):
                self._send_json({"error": "invalid group JID"}, 400)
                return
            # Store user message in session (cap per-session message list to prevent OOM)
            ts = time.time()
            with _sessions_lock:
                # BUG-WP-10 FIX (MEDIUM): Between the first _sessions_lock block
                # (line ~252) and this one, _expire_sessions() may have run in
                # another ThreadingHTTPServer worker thread and removed the session
                # from _sessions.  Using _sessions[session_id] directly raises
                # KeyError in that case.  Use _sessions.get() and bail out safely.
                _live_session = _sessions.get(session_id)
                if _live_session is None:
                    self._send_json({"error": "session expired"}, 400)
                    return
                msgs = _live_session["messages"]
                if len(msgs) >= _MAX_SESSION_MESSAGES:
                    msgs.pop(0)  # evict oldest to make room
                msgs.append({"role": "user", "text": text, "ts": ts})
            # Write to DB so main loop processes it
            msg_id = str(uuid.uuid4())
            db.store_message(msg_id, jid, "webportal", "WebPortal", text, int(ts * 1000))
            # Track reply association; evict stale entries to avoid unbounded growth
            with _sessions_lock:
                _cleanup_pending_replies()
                _pending_replies[msg_id] = (session_id, time.time())
            self._send_json({"ok": True, "msg_id": msg_id})
        except ValueError:
            pass  # _read_body already sent 413/400
        except Exception as e:
            # BUG-WP-08 FIX: The original code sent `str(e)` as the error
            # message, leaking internal exception details (file paths, SQL
            # errors, stack context) to the client.  Log the full error
            # server-side and return a generic message to the caller.
            log.exception("WebPortal: unhandled error in _api_send")
            self._send_json({"error": "internal server error"}, 500)

    def _serve_html(self):
        html = _PORTAL_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        # BUG-WP-04 FIX: add security headers to HTML response (consistent with
        # what Phase 13A added to dashboard.py's HTML endpoint).
        self._add_security_headers()
        self.end_headers()
        self.wfile.write(html)


# Pending reply tracking: msg_id -> (session_id, created_at_timestamp)
# Cleaned up lazily on every _api_send to prevent unbounded growth.
# TTL: entries older than 300 seconds are also evicted (Fix #107).
_PENDING_REPLY_TTL = 300  # seconds
_pending_replies: dict[str, tuple[str, float]] = {}


def _cleanup_pending_replies() -> None:
    """Evict _pending_replies entries whose session no longer exists or that have
    exceeded the TTL of _PENDING_REPLY_TTL seconds (Fix #107).
    Must be called while holding _sessions_lock."""
    now = time.time()
    stale = [
        mid for mid, (sid, created_at) in _pending_replies.items()
        if sid not in _sessions or now - created_at > _PENDING_REPLY_TTL
    ]
    for mid in stale:
        _pending_replies.pop(mid, None)
    if stale:
        log.debug("WebPortal: evicted %d stale pending reply entries", len(stale))


def deliver_reply(jid: str, text: str):
    """Called by the host when a reply is ready — push to all sessions for this JID."""
    with _sessions_lock:
        for session in _sessions.values():
            if session.get("jid") == jid:
                msgs = session["messages"]
                if len(msgs) >= _MAX_SESSION_MESSAGES:
                    msgs.pop(0)
                msgs.append({"role": "assistant", "text": text, "ts": time.time()})


_PORTAL_HTML = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EvoClaw Web Portal</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d1117; color: #e6edf3; font-family: -apple-system, sans-serif; height: 100vh; display: flex; flex-direction: column; }
header { background: #161b22; border-bottom: 1px solid #30363d; padding: 12px 20px; display: flex; align-items: center; gap: 12px; }
header h1 { font-size: 18px; color: #a78bfa; }
#group-select { background: #21262d; border: 1px solid #30363d; color: #e6edf3; padding: 6px 10px; border-radius: 6px; font-size: 14px; }
#chat { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 10px; }
.msg { max-width: 75%; padding: 10px 14px; border-radius: 12px; font-size: 14px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
.msg.user { background: #a78bfa; color: #fff; align-self: flex-end; border-radius: 12px 12px 2px 12px; }
.msg.assistant { background: #21262d; border: 1px solid #30363d; align-self: flex-start; border-radius: 12px 12px 12px 2px; }
.msg.thinking { color: #8b949e; font-style: italic; }
#input-area { background: #161b22; border-top: 1px solid #30363d; padding: 12px 16px; display: flex; gap: 8px; }
#msg-input { flex: 1; background: #21262d; border: 1px solid #30363d; color: #e6edf3; padding: 10px 14px; border-radius: 8px; font-size: 14px; resize: none; height: 44px; }
#msg-input:focus { outline: none; border-color: #a78bfa; }
#send-btn { background: #a78bfa; color: #fff; border: none; padding: 0 20px; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 600; }
#send-btn:hover { background: #9061f9; }
#send-btn:disabled { background: #4a4a6a; cursor: not-allowed; }
</style>
</head>
<body>
<header>
  <h1>&#9889; EvoClaw</h1>
  <select id="group-select"><option value="">&#36984;&#25321;&#32676;&#32244;...</option></select>
</header>
<div id="chat"></div>
<div id="input-area">
  <textarea id="msg-input" placeholder="&#36664;&#20837;&#35338;&#24687;... (Enter &#36865;&#20986;, Shift+Enter &#25442;&#34892;)" rows="1"></textarea>
  <button id="send-btn" disabled>&#36865;&#20986;</button>
</div>
<script>
let sessionId = null, csrfToken = null, lastTs = 0, pollTimer = null;

async function init() {
  const res = await fetch('/api/groups');
  const { groups } = await res.json();
  const sel = document.getElementById('group-select');
  groups.forEach(g => {
    const opt = document.createElement('option');
    opt.value = g.jid;
    opt.textContent = g.name || g.folder;
    sel.appendChild(opt);
  });
}

async function startSession(jid) {
  if (pollTimer) clearInterval(pollTimer);
  const res = await fetch('/api/session', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({jid}) });
  const data = await res.json();
  sessionId = data.session_id;
  csrfToken = data.csrf_token || null;
  lastTs = 0;
  document.getElementById('chat').innerHTML = '';
  document.getElementById('send-btn').disabled = false;
  pollTimer = setInterval(poll, 1000);
}

async function poll() {
  if (!sessionId) return;
  const res = await fetch(`/api/poll?session_id=${encodeURIComponent(sessionId)}&since=${lastTs}`);
  const { messages } = await res.json();
  messages.forEach(m => {
    addMessage(m.role, m.text);
    if (m.ts > lastTs) lastTs = m.ts;
  });
}

function addMessage(role, text) {
  const chat = document.getElementById('chat');
  const div = document.createElement('div');
  div.className = `msg ${role}`;
  div.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

async function sendMessage() {
  const input = document.getElementById('msg-input');
  const text = input.value.trim();
  if (!text || !sessionId) return;
  input.value = '';
  input.style.height = '44px';
  const btn = document.getElementById('send-btn');
  btn.disabled = true;
  const sendHeaders = {'Content-Type':'application/json'};
  if (csrfToken) sendHeaders['X-CSRF-Token'] = csrfToken;
  await fetch('/api/send', { method: 'POST', headers: sendHeaders, body: JSON.stringify({session_id: sessionId, text}) });
  const thinking = document.createElement('div');
  thinking.className = 'msg assistant thinking';
  thinking.id = 'thinking';
  thinking.textContent = '\u601d\u8003\u4e2d...';
  document.getElementById('chat').appendChild(thinking);
  document.getElementById('chat').scrollTop = 9999;
  setTimeout(() => { btn.disabled = false; const t = document.getElementById('thinking'); if(t) t.remove(); }, 3000);
}

document.getElementById('group-select').addEventListener('change', e => { if(e.target.value) startSession(e.target.value); });
document.getElementById('send-btn').addEventListener('click', sendMessage);
document.getElementById('msg-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});

init();
</script>
</body>
</html>"""


def start_webportal(stop_event=None):
    """Start the web portal in a background daemon thread.

    BUG-WP-06 FIX: The original start_webportal() did not accept a stop_event,
    unlike start_dashboard() which does.  Without it, the HTTP server runs
    until the process dies — it cannot be shut down gracefully when the asyncio
    event loop requests a clean shutdown (e.g. SIGTERM / Ctrl-C).  Accepting the
    same asyncio.Event that main() uses allows a watcher thread to call
    server.shutdown() when the event fires, mirroring the dashboard pattern.
    """
    if not config.WEBPORTAL_ENABLED:
        return None

    # BUG-WP-05 FIX: Emit a warning when the web portal is enabled but no
    # password has been set.  The dashboard already warns via
    # config.warn_dashboard_no_password(); the web portal has the same exposure
    # (it is a browser-accessible HTTP interface) but previously had no warning.
    if not config.DASHBOARD_PASSWORD:
        log.warning(
            "WEBPORTAL_ENABLED=true but DASHBOARD_PASSWORD is not set — "
            "the web portal has NO authentication. "
            "Set DASHBOARD_PASSWORD in .env to enable HTTP Basic Auth."
        )

    server = http.server.ThreadingHTTPServer(
        (config.WEBPORTAL_HOST, config.WEBPORTAL_PORT),
        _WebPortalHandler,
    )
    t = threading.Thread(target=server.serve_forever, daemon=True, name="webportal")
    t.start()
    log.info("Web portal started at http://%s:%s/", config.WEBPORTAL_HOST, config.WEBPORTAL_PORT)

    if stop_event is not None:
        # Watch the asyncio stop_event from a background thread and call
        # server.shutdown() when it fires, mirroring start_dashboard() (Issue #56).
        import asyncio as _asyncio

        # Capture the running loop at call time (this function is called from the
        # asyncio event loop thread).  _watch_stop runs in a daemon thread where
        # get_event_loop() / get_running_loop() are not reliable; capturing the
        # loop here avoids the deprecated get_event_loop() fallback.
        _captured_loop = _asyncio.get_running_loop()

        def _watch_stop():
            try:
                loop = stop_event._loop  # type: ignore[attr-defined]
            except AttributeError:
                loop = _captured_loop
            try:
                future = _asyncio.run_coroutine_threadsafe(stop_event.wait(), loop)
                future.result()
            except Exception:
                pass
            server.shutdown()

        watcher = threading.Thread(target=_watch_stop, daemon=True, name="webportal-stopper")
        watcher.start()

    return server
