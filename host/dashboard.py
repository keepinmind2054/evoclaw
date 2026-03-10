"""Simple web dashboard for EvoClaw — no external dependencies"""
import http.server
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from . import config

# ── HTML template ──────────────────────────────────────────────────────────────

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="10">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EvoClaw Dashboard</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #1a1a2e;
    color: #e0e0e0;
    font-family: 'Courier New', Courier, monospace;
    font-size: 13px;
    padding: 16px;
  }}
  h1 {{
    font-size: 20px;
    color: #a78bfa;
    letter-spacing: 2px;
    margin-bottom: 4px;
  }}
  h2 {{
    font-size: 14px;
    color: #7c3aed;
    letter-spacing: 1px;
    text-transform: uppercase;
    margin-bottom: 8px;
    padding-bottom: 4px;
    border-bottom: 1px solid #2d2d4e;
  }}
  .topbar {{
    display: flex;
    gap: 32px;
    align-items: center;
    background: #16213e;
    border: 1px solid #2d2d4e;
    border-radius: 6px;
    padding: 12px 20px;
    margin-bottom: 20px;
  }}
  .topbar .label {{ color: #6b7280; font-size: 11px; }}
  .topbar .value {{ color: #a78bfa; font-weight: bold; }}
  .topbar .meta {{ display: flex; flex-direction: column; gap: 2px; }}
  .refresh-note {{
    color: #4b5563;
    font-size: 11px;
    margin-left: auto;
    text-align: right;
  }}
  .section {{
    background: #16213e;
    border: 1px solid #2d2d4e;
    border-radius: 6px;
    padding: 16px;
    margin-bottom: 16px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    margin-top: 4px;
  }}
  th {{
    text-align: left;
    color: #6b7280;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding: 6px 8px;
    border-bottom: 1px solid #2d2d4e;
  }}
  td {{
    padding: 6px 8px;
    border-bottom: 1px solid #1a1a2e;
    vertical-align: top;
    word-break: break-all;
  }}
  tr:last-child td {{ border-bottom: none; }}
  tr:nth-child(even) td {{ background: #1e1e38; }}
  .badge {{
    display: inline-block;
    padding: 1px 8px;
    border-radius: 3px;
    font-size: 11px;
    font-weight: bold;
    letter-spacing: 0.5px;
  }}
  .badge-green  {{ background: #064e3b; color: #34d399; }}
  .badge-yellow {{ background: #451a03; color: #fbbf24; }}
  .badge-red    {{ background: #450a0a; color: #f87171; }}
  .badge-gray   {{ background: #1f2937; color: #9ca3af; }}
  .badge-blue   {{ background: #1e3a5f; color: #60a5fa; }}
  .trunc {{ color: #9ca3af; }}
  .na {{ color: #374151; font-style: italic; }}
  .empty {{ color: #374151; text-align: center; padding: 16px; }}
</style>
</head>
<body>

<div class="topbar">
  <div class="meta">
    <span class="label">SYSTEM</span>
    <span class="value">EvoClaw</span>
  </div>
  <div class="meta">
    <span class="label">DATABASE</span>
    <span class="value">{db_path}</span>
  </div>
  <div class="meta">
    <span class="label">UPTIME SINCE</span>
    <span class="value">{uptime}</span>
  </div>
  <div class="meta">
    <span class="label">DASHBOARD</span>
    <span class="value">PORT {port}</span>
  </div>
  <div class="refresh-note">auto-refresh every 10s<br>last loaded: {now}</div>
</div>

<!-- Registered Groups -->
<div class="section">
  <h2>Registered Groups</h2>
  {groups_table}
</div>

<!-- Scheduled Tasks -->
<div class="section">
  <h2>Scheduled Tasks</h2>
  {tasks_table}
</div>

<!-- Recent Task Run Logs -->
<div class="section">
  <h2>Recent Task Run Logs (last 20)</h2>
  {logs_table}
</div>

<!-- Active Sessions -->
<div class="section">
  <h2>Active Sessions</h2>
  {sessions_table}
</div>

<!-- Recent Messages -->
<div class="section">
  <h2>Recent Messages (last 20)</h2>
  {messages_table}
</div>

<!-- Evolution Stats -->
<div class="section">
  <h2>Evolution Stats (Group Genome)</h2>
  {evolution_table}
</div>

<!-- Evolution Log -->
<div class="section">
  <h2>&#129516; Evolution Log (last 30 events)</h2>
  {evolution_log_table}
</div>

<!-- Immune Threats -->
<div class="section">
  <h2>Immune Threats (blocked or count &gt; 3)</h2>
  {immune_table}
</div>

</body>
</html>
"""

# ── Helpers ────────────────────────────────────────────────────────────────────

def _fmt_ts_ms(ts_ms):
    """Format a millisecond Unix timestamp to human-readable string."""
    if ts_ms is None:
        return '<span class="na">N/A</span>'
    try:
        dt = datetime.fromtimestamp(int(ts_ms) / 1000)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return '<span class="na">N/A</span>'


def _fmt_ts_s(ts_s):
    """Format a seconds Unix timestamp to human-readable string."""
    if ts_s is None:
        return '<span class="na">N/A</span>'
    try:
        dt = datetime.fromtimestamp(int(ts_s))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return '<span class="na">N/A</span>'


def _fmt_dt_str(dt_str):
    """Format a datetime string (SQLite datetime()) to human-readable."""
    if not dt_str:
        return '<span class="na">N/A</span>'
    try:
        # SQLite datetime() returns 'YYYY-MM-DD HH:MM:SS'
        dt = datetime.strptime(str(dt_str), "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(dt_str)


def _esc(s):
    """HTML-escape a string."""
    if s is None:
        return '<span class="na">N/A</span>'
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _trunc(s, n=100):
    """Truncate a string and HTML-escape it."""
    if s is None:
        return '<span class="na">N/A</span>'
    s = str(s)
    if len(s) > n:
        return _esc(s[:n]) + '<span class="trunc">…</span>'
    return _esc(s)


def _bool_badge(val):
    if val:
        return '<span class="badge badge-green">YES</span>'
    return '<span class="badge badge-gray">NO</span>'


def _status_badge(status):
    s = str(status).lower() if status else ""
    if s == "active":
        return '<span class="badge badge-green">active</span>'
    elif s == "paused":
        return '<span class="badge badge-yellow">paused</span>'
    elif s in ("error", "failed"):
        return '<span class="badge badge-red">' + _esc(status) + '</span>'
    else:
        return '<span class="badge badge-gray">' + _esc(status) + '</span>'


def _run_status_badge(status):
    s = str(status).lower() if status else ""
    if s in ("ok", "success", "done"):
        return '<span class="badge badge-green">' + _esc(status) + '</span>'
    elif s in ("error", "failed", "timeout"):
        return '<span class="badge badge-red">' + _esc(status) + '</span>'
    else:
        return '<span class="badge badge-gray">' + _esc(status) + '</span>'


def _empty_row(cols):
    return f'<tr><td colspan="{cols}" class="empty">— no data —</td></tr>'


# ── DB queries ─────────────────────────────────────────────────────────────────

def _open_db():
    """Open a read-only connection to the EvoClaw database."""
    db_path = config.STORE_DIR / "messages.db"
    # uri=True allows ?mode=ro for read-only access
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch(query, params=()):
    """Execute a query and return list of dicts, or [] on any error."""
    try:
        conn = _open_db()
        try:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


def _fetch_one(query, params=()):
    """Execute a query and return a single dict, or None on any error."""
    try:
        conn = _open_db()
        try:
            row = conn.execute(query, params).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception:
        return None


# ── Table builders ─────────────────────────────────────────────────────────────

def _build_groups_table():
    rows = _fetch("SELECT folder, jid, trigger_pattern, is_main, requires_trigger FROM registered_groups ORDER BY folder")
    if not rows:
        return ('<table><thead><tr><th>folder</th><th>JID</th><th>trigger_pattern</th>'
                '<th>is_main</th><th>requires_trigger</th></tr></thead>'
                '<tbody>' + _empty_row(5) + '</tbody></table>')
    body = ""
    for r in rows:
        body += (f"<tr>"
                 f"<td>{_esc(r.get('folder'))}</td>"
                 f"<td>{_esc(r.get('jid'))}</td>"
                 f"<td>{_esc(r.get('trigger_pattern'))}</td>"
                 f"<td>{_bool_badge(r.get('is_main'))}</td>"
                 f"<td>{_bool_badge(r.get('requires_trigger'))}</td>"
                 f"</tr>")
    return (f'<table><thead><tr><th>folder</th><th>JID</th><th>trigger_pattern</th>'
            f'<th>is_main</th><th>requires_trigger</th></tr></thead>'
            f'<tbody>{body}</tbody></table>')


def _build_tasks_table():
    rows = _fetch(
        "SELECT id, group_folder, schedule_type, schedule_value, next_run, status, last_result "
        "FROM scheduled_tasks ORDER BY status, next_run"
    )
    if not rows:
        return ('<table><thead><tr><th>id</th><th>group_folder</th><th>schedule_type</th>'
                '<th>schedule_value</th><th>next_run</th><th>status</th><th>last_result</th></tr></thead>'
                '<tbody>' + _empty_row(7) + '</tbody></table>')
    body = ""
    for r in rows:
        task_id = str(r.get("id", ""))
        body += (f"<tr>"
                 f"<td><code>{_esc(task_id[:8])}</code></td>"
                 f"<td>{_esc(r.get('group_folder'))}</td>"
                 f"<td>{_esc(r.get('schedule_type'))}</td>"
                 f"<td>{_esc(r.get('schedule_value'))}</td>"
                 f"<td>{_fmt_ts_ms(r.get('next_run'))}</td>"
                 f"<td>{_status_badge(r.get('status'))}</td>"
                 f"<td>{_trunc(r.get('last_result'), 80)}</td>"
                 f"</tr>")
    return (f'<table><thead><tr><th>id</th><th>group_folder</th><th>schedule_type</th>'
            f'<th>schedule_value</th><th>next_run</th><th>status</th><th>last_result</th></tr></thead>'
            f'<tbody>{body}</tbody></table>')


def _build_logs_table():
    rows = _fetch(
        "SELECT task_id, run_at, duration_ms, status, result, error "
        "FROM task_run_logs ORDER BY run_at DESC LIMIT 20"
    )
    if not rows:
        return ('<table><thead><tr><th>task_id</th><th>run_at</th><th>duration_ms</th>'
                '<th>status</th><th>result / error</th></tr></thead>'
                '<tbody>' + _empty_row(5) + '</tbody></table>')
    body = ""
    for r in rows:
        task_id = str(r.get("task_id", ""))
        result_text = r.get("result") or r.get("error")
        body += (f"<tr>"
                 f"<td><code>{_esc(task_id[:8])}</code></td>"
                 f"<td>{_fmt_ts_ms(r.get('run_at'))}</td>"
                 f"<td>{_esc(r.get('duration_ms'))}</td>"
                 f"<td>{_run_status_badge(r.get('status'))}</td>"
                 f"<td>{_trunc(result_text, 100)}</td>"
                 f"</tr>")
    return (f'<table><thead><tr><th>task_id</th><th>run_at</th><th>duration_ms</th>'
            f'<th>status</th><th>result / error</th></tr></thead>'
            f'<tbody>{body}</tbody></table>')


def _build_sessions_table():
    rows = _fetch("SELECT group_folder, session_id FROM sessions ORDER BY group_folder")
    if not rows:
        return ('<table><thead><tr><th>group_folder</th><th>session_id</th></tr></thead>'
                '<tbody>' + _empty_row(2) + '</tbody></table>')
    body = ""
    for r in rows:
        body += (f"<tr>"
                 f"<td>{_esc(r.get('group_folder'))}</td>"
                 f"<td><code>{_esc(r.get('session_id'))}</code></td>"
                 f"</tr>")
    return (f'<table><thead><tr><th>group_folder</th><th>session_id</th></tr></thead>'
            f'<tbody>{body}</tbody></table>')


def _build_messages_table():
    rows = _fetch(
        "SELECT chat_jid, sender_name, content, timestamp "
        "FROM messages ORDER BY timestamp DESC LIMIT 20"
    )
    if not rows:
        return ('<table><thead><tr><th>chat_jid</th><th>sender_name</th>'
                '<th>content</th><th>timestamp</th></tr></thead>'
                '<tbody>' + _empty_row(4) + '</tbody></table>')
    body = ""
    for r in rows:
        body += (f"<tr>"
                 f"<td>{_esc(r.get('chat_jid'))}</td>"
                 f"<td>{_esc(r.get('sender_name'))}</td>"
                 f"<td>{_trunc(r.get('content'), 80)}</td>"
                 f"<td>{_fmt_ts_ms(r.get('timestamp'))}</td>"
                 f"</tr>")
    return (f'<table><thead><tr><th>chat_jid</th><th>sender_name</th>'
            f'<th>content</th><th>timestamp</th></tr></thead>'
            f'<tbody>{body}</tbody></table>')


def _build_evolution_table():
    rows = _fetch(
        "SELECT jid, response_style, formality, technical_depth, generation, updated_at "
        "FROM group_genome ORDER BY updated_at DESC"
    )
    if not rows:
        return ('<table><thead><tr><th>jid</th><th>response_style</th><th>formality</th>'
                '<th>technical_depth</th><th>generation</th><th>updated_at</th></tr></thead>'
                '<tbody>' + _empty_row(6) + '</tbody></table>')
    body = ""
    for r in rows:
        formality = r.get("formality")
        tech = r.get("technical_depth")
        body += (f"<tr>"
                 f"<td>{_esc(r.get('jid'))}</td>"
                 f"<td>{_esc(r.get('response_style'))}</td>"
                 f"<td>{f'{formality:.2f}' if formality is not None else '<span class=\"na\">N/A</span>'}</td>"
                 f"<td>{f'{tech:.2f}' if tech is not None else '<span class=\"na\">N/A</span>'}</td>"
                 f"<td>{_esc(r.get('generation'))}</td>"
                 f"<td>{_fmt_dt_str(r.get('updated_at'))}</td>"
                 f"</tr>")
    return (f'<table><thead><tr><th>jid</th><th>response_style</th><th>formality</th>'
            f'<th>technical_depth</th><th>generation</th><th>updated_at</th></tr></thead>'
            f'<tbody>{body}</tbody></table>')


def _build_evolution_log_table():
    rows = _fetch(
        "SELECT timestamp, jid, event_type, fitness_score, avg_response_ms, "
        "generation_before, generation_after, notes "
        "FROM evolution_log ORDER BY timestamp DESC LIMIT 30"
    )
    if not rows:
        return ('<table><thead><tr><th>時間</th><th>事件類型</th><th>群組 JID</th>'
                '<th>適應度</th><th>平均回應</th><th>世代</th><th>備注</th></tr></thead>'
                '<tbody>' + _empty_row(7) + '</tbody></table>')
    color_map = {
        "genome_evolved": "#4ade80",
        "genome_unchanged": "#94a3b8",
        "cycle_start": "#60a5fa",
        "cycle_end": "#a78bfa",
        "skipped_low_samples": "#f59e0b",
    }
    body = ""
    for entry in rows:
        ts = str(entry.get("timestamp", ""))[:19]
        jid = entry.get("jid", "")
        etype = entry.get("event_type", "")
        fitness = entry.get("fitness_score")
        fitness_str = f"{fitness:.3f}" if fitness is not None else "-"
        avg_ms = entry.get("avg_response_ms")
        ms_str = f"{avg_ms:.0f}ms" if avg_ms is not None else "-"
        gen_b = entry.get("generation_before")
        gen_a = entry.get("generation_after")
        gen_str = f"{gen_b}&rarr;{gen_a}" if gen_b is not None else "-"
        notes = _esc(entry.get("notes", ""))
        color = color_map.get(etype, "#e2e8f0")
        body += (f"<tr>"
                 f"<td style='color:#94a3b8'>{_esc(ts)}</td>"
                 f"<td style='color:{color}'>{_esc(etype)}</td>"
                 f"<td style='color:#e2e8f0;font-size:11px'>{_esc(jid)}</td>"
                 f"<td style='color:#fbbf24'>{fitness_str}</td>"
                 f"<td style='color:#60a5fa'>{ms_str}</td>"
                 f"<td style='color:#a78bfa'>{gen_str}</td>"
                 f"<td style='color:#94a3b8;font-size:11px'>{notes}</td>"
                 f"</tr>")
    return (f'<table><thead><tr><th>時間</th><th>事件類型</th><th>群組 JID</th>'
            f'<th>適應度</th><th>平均回應</th><th>世代</th><th>備注</th></tr></thead>'
            f'<tbody>{body}</tbody></table>')


def _build_immune_table():
    rows = _fetch(
        "SELECT sender_jid, threat_type, count, blocked, last_seen "
        "FROM immune_threats WHERE blocked=1 OR count > 3 "
        "ORDER BY blocked DESC, count DESC"
    )
    if not rows:
        return ('<table><thead><tr><th>sender_jid</th><th>threat_type</th>'
                '<th>count</th><th>blocked</th><th>last_seen</th></tr></thead>'
                '<tbody>' + _empty_row(5) + '</tbody></table>')
    body = ""
    for r in rows:
        blocked = r.get("blocked")
        body += (f"<tr>"
                 f"<td>{_esc(r.get('sender_jid'))}</td>"
                 f"<td>{_esc(r.get('threat_type'))}</td>"
                 f"<td>{_esc(r.get('count'))}</td>"
                 f"<td>{'<span class=\"badge badge-red\">BLOCKED</span>' if blocked else '<span class=\"badge badge-gray\">no</span>'}</td>"
                 f"<td>{_fmt_dt_str(r.get('last_seen'))}</td>"
                 f"</tr>")
    return (f'<table><thead><tr><th>sender_jid</th><th>threat_type</th>'
            f'<th>count</th><th>blocked</th><th>last_seen</th></tr></thead>'
            f'<tbody>{body}</tbody></table>')


# ── Status data ────────────────────────────────────────────────────────────────

def _get_uptime():
    """Read startup timestamp from router_state if it exists."""
    row = _fetch_one("SELECT value FROM router_state WHERE key='startup_at'")
    if row and row.get("value"):
        try:
            ts = int(row["value"])
            # Could be ms or seconds — detect by magnitude
            if ts > 1e12:
                ts = ts / 1000
            return _fmt_ts_s(int(ts))
        except Exception:
            pass
    return '<span class="na">unknown</span>'


def _get_api_status():
    """Collect all data for the /api/status endpoint."""
    try:
        groups = _fetch("SELECT * FROM registered_groups ORDER BY folder")
        tasks = _fetch("SELECT * FROM scheduled_tasks ORDER BY status, next_run")
        logs = _fetch("SELECT * FROM task_run_logs ORDER BY run_at DESC LIMIT 20")
        sessions = _fetch("SELECT * FROM sessions ORDER BY group_folder")
        messages = _fetch("SELECT * FROM messages ORDER BY timestamp DESC LIMIT 20")
        genome = _fetch("SELECT * FROM group_genome ORDER BY updated_at DESC")
        immune = _fetch(
            "SELECT * FROM immune_threats WHERE blocked=1 OR count > 3 "
            "ORDER BY blocked DESC, count DESC"
        )
        return {
            "ok": True,
            "db_path": str(config.STORE_DIR / "messages.db"),
            "port": config.DASHBOARD_PORT,
            "groups": groups,
            "tasks": tasks,
            "logs": logs,
            "sessions": sessions,
            "messages": messages,
            "genome": genome,
            "immune": immune,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── HTTP handler ───────────────────────────────────────────────────────────────

class _DashboardHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP handler serving the dashboard HTML and a JSON API."""

    def log_message(self, format, *args):  # suppress default access log noise
        pass

    def do_GET(self):
        # Check password if configured
        if config.DASHBOARD_PASSWORD:
            import base64
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Basic "):
                try:
                    decoded = base64.b64decode(auth[6:]).decode("utf-8")
                    user, pw = decoded.split(":", 1)
                    if user != config.DASHBOARD_USER or pw != config.DASHBOARD_PASSWORD:
                        self._send_auth_required()
                        return
                except Exception:
                    self._send_auth_required()
                    return
            else:
                self._send_auth_required()
                return

        if self.path == "/api/status":
            self._serve_json()
        elif self.path in ("/", "/index.html"):
            self._serve_html()
        elif self.path == "/health":
            self._handle_health()
        elif self.path == "/metrics":
            self._handle_metrics()
        else:
            self.send_response(404)
            self.end_headers()

    def _send_auth_required(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="EvoClaw Dashboard"')
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Authentication required")

    def _handle_health(self):
        import sqlite3, time
        health = {"status": "ok", "checks": {}}
        # Check DB
        try:
            db_path = config.STORE_DIR / "messages.db"
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            conn.execute("SELECT 1").fetchone()
            conn.close()
            health["checks"]["database"] = "ok"
        except Exception as e:
            health["checks"]["database"] = f"error: {e}"
            health["status"] = "degraded"
        # Check Docker
        import subprocess
        try:
            r = subprocess.run(["docker", "info"], capture_output=True, timeout=3)
            health["checks"]["docker"] = "ok" if r.returncode == 0 else "error"
            if r.returncode != 0:
                health["status"] = "degraded"
        except Exception:
            health["checks"]["docker"] = "unavailable"

        status_code = 200 if health["status"] == "ok" else 503
        body = json.dumps(health, indent=2).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_metrics(self):
        """Prometheus-style metrics."""
        import sqlite3
        lines = []
        try:
            db_path = config.STORE_DIR / "messages.db"
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            tables = ["messages", "scheduled_tasks", "registered_groups", "sessions", "evolution_runs", "immune_threats"]
            for t in tables:
                try:
                    count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    lines.append(f'evoclaw_{t}_total {count}')
                except Exception:
                    pass
            conn.close()
        except Exception:
            pass
        body = "\n".join(lines).encode() + b"\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_json(self):
        data = _get_api_status()
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db_path = str(config.STORE_DIR / "messages.db")

        html = _HTML_TEMPLATE.format(
            db_path=_esc(db_path),
            uptime=_get_uptime(),
            port=config.DASHBOARD_PORT,
            now=now,
            groups_table=_build_groups_table(),
            tasks_table=_build_tasks_table(),
            logs_table=_build_logs_table(),
            sessions_table=_build_sessions_table(),
            messages_table=_build_messages_table(),
            evolution_table=_build_evolution_table(),
            evolution_log_table=_build_evolution_log_table(),
            immune_table=_build_immune_table(),
        )
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ── Public API ─────────────────────────────────────────────────────────────────

def start_dashboard(stop_event=None):
    """
    Start the dashboard HTTP server in a daemon background thread.

    The thread will die automatically when the main process exits.
    An optional stop_event (asyncio.Event or threading.Event) is accepted
    but not strictly required — the daemon thread handles cleanup on exit.

    Returns the Thread object.
    """
    server = http.server.HTTPServer(("0.0.0.0", config.DASHBOARD_PORT), _DashboardHandler)

    def _run():
        import logging
        log = logging.getLogger(__name__)
        log.info(f"Dashboard started on http://0.0.0.0:{config.DASHBOARD_PORT}")
        try:
            server.serve_forever()
        except Exception as exc:
            log.warning(f"Dashboard server stopped: {exc}")

    t = threading.Thread(target=_run, name="evoclaw-dashboard", daemon=True)
    t.start()
    return t
