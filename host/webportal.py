"""
EvoClaw Web Portal — Browser-based chat interface (stdlib only, no FastAPI)
Uses HTTP polling instead of WebSocket for simplicity.
Endpoint: http://localhost:8766/
"""
import base64
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
    """
    password = config.DASHBOARD_PASSWORD
    if not password:
        return True  # Auth disabled — allow all requests
    auth_header = handler.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8", errors="replace")
            username, _, provided_pw = decoded.partition(":")
            if provided_pw == password:
                return True
        except Exception:
            pass
    # Send 401 Unauthorized
    handler.send_response(401)
    handler.send_header("WWW-Authenticate", 'Basic realm="EvoClaw Web Portal"')
    handler.send_header("Content-Length", "0")
    handler.end_headers()
    return False


class _WebPortalHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug(f"WebPortal: {fmt % args}")

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
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if not _check_auth(self):
            return
        if self.path == "/api/send":
            self._api_send()
        elif self.path == "/api/session":
            self._api_new_session()
        else:
            self.send_response(404)
            self.end_headers()

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_new_session(self):
        try:
            body = json.loads(self._read_body())
            jid = body.get("jid", "")
        except Exception:
            jid = ""
        session_id = str(uuid.uuid4())
        with _sessions_lock:
            _sessions[session_id] = {"jid": jid, "messages": [], "created": time.time(), "last_seen": time.time()}
        self._send_json({"session_id": session_id})

    def _api_groups(self):
        groups = _get_registered_groups()
        self._send_json({"groups": [{"jid": g["jid"], "name": g.get("name", g["folder"]), "folder": g["folder"]} for g in groups]})

    def _api_poll(self):
        """Return new messages since a given timestamp."""
        global _poll_count
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        session_id = qs.get("session_id", [""])[0]
        since = float(qs.get("since", ["0"])[0])
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
            with _sessions_lock:
                session = _sessions.get(session_id)
            if not session:
                self._send_json({"error": "invalid session"}, 400)
                return
            jid = session.get("jid", "")
            if not jid:
                self._send_json({"error": "no group selected"}, 400)
                return
            # Store user message in session
            ts = time.time()
            with _sessions_lock:
                _sessions[session_id]["messages"].append({"role": "user", "text": text, "ts": ts})
            # Write to DB so main loop processes it
            msg_id = str(uuid.uuid4())
            db.store_message(msg_id, jid, "webportal", "WebPortal", text, int(ts * 1000))
            # Register a reply callback so we can push the response back to the session
            _pending_replies[msg_id] = session_id
            self._send_json({"ok": True, "msg_id": msg_id})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_html(self):
        html = _PORTAL_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)


# Pending reply tracking: msg_id -> session_id
_pending_replies: dict[str, str] = {}


def deliver_reply(jid: str, text: str):
    """Called by the host when a reply is ready — push to all sessions for this JID."""
    with _sessions_lock:
        for session in _sessions.values():
            if session.get("jid") == jid:
                session["messages"].append({"role": "assistant", "text": text, "ts": time.time()})


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
  <h1>⚡ EvoClaw</h1>
  <select id="group-select"><option value="">選擇群組...</option></select>
</header>
<div id="chat"></div>
<div id="input-area">
  <textarea id="msg-input" placeholder="輸入訊息... (Enter 送出, Shift+Enter 換行)" rows="1"></textarea>
  <button id="send-btn" disabled>送出</button>
</div>
<script>
let sessionId = null, lastTs = 0, pollTimer = null;

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
  const { session_id } = await res.json();
  sessionId = session_id;
  lastTs = 0;
  document.getElementById('chat').innerHTML = '';
  document.getElementById('send-btn').disabled = false;
  pollTimer = setInterval(poll, 1000);
}

async function poll() {
  if (!sessionId) return;
  const res = await fetch(`/api/poll?session_id=${sessionId}&since=${lastTs}`);
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
  await fetch('/api/send', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({session_id: sessionId, text}) });
  const thinking = document.createElement('div');
  thinking.className = 'msg assistant thinking';
  thinking.id = 'thinking';
  thinking.textContent = '思考中...';
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


def start_webportal(route_output_fn=None):
    """Start the web portal in a background daemon thread."""
    if not config.WEBPORTAL_ENABLED:
        return None
    server = http.server.ThreadingHTTPServer(
        (config.WEBPORTAL_HOST, config.WEBPORTAL_PORT),
        _WebPortalHandler,
    )
    t = threading.Thread(target=server.serve_forever, daemon=True, name="webportal")
    t.start()
    log.info(f"Web portal started at http://{config.WEBPORTAL_HOST}:{config.WEBPORTAL_PORT}/")
    return server
