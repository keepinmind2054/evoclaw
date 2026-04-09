"""
mcp_server.py — EvoClaw MCP Server (stdio transport)

Exposes EvoClaw's runtime tools to Claude Code via the Model Context Protocol.
Claude Code launches this as a subprocess and communicates via stdin/stdout
using JSON-RPC 2.0.

Usage in .mcp.json:
    {
      "mcpServers": {
        "evoclaw": {
          "command": "python",
          "args": ["-m", "host.mcp_server"],
          "cwd": "/path/to/evoclaw",
          "env": { "EVOCLAW_DATA_DIR": "/data" }
        }
      }
    }

Exposed tools:
  evoclaw_get_logs        — Tail recent container/host error logs
  evoclaw_list_groups     — List all registered groups with status
  evoclaw_group_status    — Health check for a specific group
  evoclaw_send_message    — Send a message to a group via SDK API
  evoclaw_run_agent       — Spawn a sub-agent for a task
  evoclaw_restart_service — Request host process restart (via systemd/launchd)
  evoclaw_list_tasks      — List scheduled tasks
  evoclaw_db_query        — Read-only SQLite query for diagnostics

MCP spec: https://spec.modelcontextprotocol.io/
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
_DATA_DIR = Path(os.environ.get("EVOCLAW_DATA_DIR", "/data"))
_DB_PATH = _DATA_DIR / "messages.db"
_LOG_DIR = _DATA_DIR / "logs"
_SDK_API_URL = os.environ.get("EVOCLAW_SDK_URL", "http://localhost:8767")
_SDK_API_TOKEN = os.environ.get("SDK_API_TOKEN", "")


# ── MCP protocol helpers ─────────────────────────────────────────────────────

def _ok(id_: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}

def _err(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}

def _notification(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "method": method, "params": params}


# ── Tool definitions ─────────────────────────────────────────────────────────

_TOOLS = [
    {
        "name": "evoclaw_get_logs",
        "description": (
            "Tail recent EvoClaw container error logs and host process logs. "
            "Use this to diagnose errors, crashes, or unexpected behaviour."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lines": {
                    "type": "integer",
                    "description": "Number of log lines to return (default 100, max 500)",
                    "default": 100,
                },
                "level": {
                    "type": "string",
                    "enum": ["all", "error", "warning"],
                    "description": "Filter by log level (default: all)",
                    "default": "all",
                },
            },
        },
    },
    {
        "name": "evoclaw_list_groups",
        "description": "List all registered groups with their JID, folder, and channel.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "evoclaw_group_status",
        "description": "Get health and recent activity for a specific group.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": "Group folder name (e.g. 'telegram_main')",
                },
            },
            "required": ["folder"],
        },
    },
    {
        "name": "evoclaw_send_message",
        "description": (
            "Send a text message to an EvoClaw group. "
            "Use for status updates, alerts, or interactive debugging."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "jid": {
                    "type": "string",
                    "description": "Chat JID (e.g. 'tg:8259652816')",
                },
                "text": {
                    "type": "string",
                    "description": "Message text to send",
                },
            },
            "required": ["jid", "text"],
        },
    },
    {
        "name": "evoclaw_list_tasks",
        "description": "List all active scheduled tasks (cron, interval, once).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": "Filter by group folder (optional)",
                },
            },
        },
    },
    {
        "name": "evoclaw_db_query",
        "description": (
            "Run a read-only SQL SELECT query against the EvoClaw SQLite database. "
            "Tables: messages, scheduled_tasks, registered_groups, chats, router_state. "
            "Use for diagnostics only — INSERT/UPDATE/DELETE are blocked."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "SQL SELECT query to execute",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (default 50, max 200)",
                    "default": 50,
                },
            },
            "required": ["sql"],
        },
    },
    {
        "name": "evoclaw_restart_service",
        "description": (
            "Request a graceful restart of the EvoClaw host process. "
            "Detects platform (systemd / launchd / bare-process) automatically. "
            "Confirm with the user before calling this."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to proceed with restart",
                },
            },
            "required": ["confirm"],
        },
    },
]


# ── Tool implementations ─────────────────────────────────────────────────────

def _tool_get_logs(args: dict) -> str:
    n = min(int(args.get("lines", 100)), 500)
    level_filter = args.get("level", "all").lower()

    lines: list[str] = []

    # 1. Host process logs (journalctl / launchd / flat file)
    host_log = _LOG_DIR / "evoclaw.log"
    if host_log.exists():
        try:
            raw = host_log.read_text(errors="replace").splitlines()
            lines.extend(raw[-n:])
        except OSError:
            pass

    # 2. Try systemd journal
    if not lines:
        try:
            result = subprocess.run(
                ["journalctl", "-u", "evoclaw", f"-n{n}", "--no-pager", "-o", "short"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                lines = result.stdout.splitlines()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    if level_filter != "all":
        keyword = level_filter.upper()
        lines = [l for l in lines if keyword in l.upper()]

    if not lines:
        return "No logs found. Check EVOCLAW_DATA_DIR or systemd journal."

    return "\n".join(lines[-n:])


def _tool_list_groups(_args: dict) -> str:
    if not _DB_PATH.exists():
        return f"Database not found at {_DB_PATH}. Is EVOCLAW_DATA_DIR set correctly?"
    try:
        con = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        rows = con.execute(
            "SELECT jid, folder, name, requires_trigger, is_main FROM registered_groups ORDER BY folder"
        ).fetchall()
        con.close()
        if not rows:
            return "No groups registered."
        lines = ["JID | Folder | Name | Trigger | Main"]
        lines.append("-" * 60)
        for jid, folder, name, trigger, main in rows:
            lines.append(f"{jid} | {folder} | {name or '—'} | {'yes' if trigger else 'no'} | {'✓' if main else ''}")
        return "\n".join(lines)
    except sqlite3.Error as e:
        return f"DB error: {e}"


def _tool_group_status(args: dict) -> str:
    folder = args.get("folder", "")
    if not folder:
        return "Error: folder is required"
    if not _DB_PATH.exists():
        return f"Database not found at {_DB_PATH}"
    try:
        con = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        group = con.execute(
            "SELECT jid, name, requires_trigger, is_main FROM registered_groups WHERE folder = ?",
            (folder,),
        ).fetchone()
        if not group:
            return f"Group '{folder}' not found in database."

        jid, name, trigger, main = group

        # Recent messages (last 5)
        msgs = con.execute(
            "SELECT sender, content, timestamp FROM messages WHERE chat_jid = ? ORDER BY timestamp DESC LIMIT 5",
            (jid,),
        ).fetchall()

        # Recent tasks
        tasks = con.execute(
            "SELECT prompt, schedule_type, schedule_value, last_run FROM scheduled_tasks "
            "WHERE folder = ? ORDER BY created_at DESC LIMIT 3",
            (folder,),
        ).fetchall()

        con.close()

        lines = [
            f"Group: {name or folder}",
            f"JID: {jid}",
            f"Main: {'yes' if main else 'no'} | Trigger: {'yes' if trigger else 'no'}",
            "",
            "Recent messages:",
        ]
        for sender, content, ts in msgs:
            lines.append(f"  [{ts}] {sender}: {str(content)[:80]}")

        if tasks:
            lines.append("\nScheduled tasks:")
            for prompt, stype, sval, last_run in tasks:
                lines.append(f"  [{stype}:{sval}] {str(prompt)[:60]} (last: {last_run or 'never'})")

        return "\n".join(lines)
    except sqlite3.Error as e:
        return f"DB error: {e}"


def _tool_send_message(args: dict) -> str:
    jid = args.get("jid", "")
    text = args.get("text", "")
    if not jid or not text:
        return "Error: jid and text are required"

    # Try SDK API WebSocket
    try:
        import urllib.request
        payload = json.dumps({"action": "send_message", "jid": jid, "text": text}).encode()
        headers = {"Content-Type": "application/json"}
        if _SDK_API_TOKEN:
            headers["Authorization"] = f"Bearer {_SDK_API_TOKEN}"
        req = urllib.request.Request(
            f"{_SDK_API_URL}/send",
            data=payload,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return f"Message sent (HTTP {resp.status})"
    except Exception as exc:
        return f"Failed to send message via SDK API: {exc}. Check EVOCLAW_SDK_URL and SDK_API_TOKEN."


def _tool_list_tasks(args: dict) -> str:
    folder_filter = args.get("folder")
    if not _DB_PATH.exists():
        return f"Database not found at {_DB_PATH}"
    try:
        con = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        if folder_filter:
            rows = con.execute(
                "SELECT id, folder, prompt, schedule_type, schedule_value, last_run, next_run "
                "FROM scheduled_tasks WHERE folder = ? ORDER BY next_run",
                (folder_filter,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT id, folder, prompt, schedule_type, schedule_value, last_run, next_run "
                "FROM scheduled_tasks ORDER BY folder, next_run"
            ).fetchall()
        con.close()
        if not rows:
            return "No scheduled tasks."
        lines = ["ID | Folder | Type | Schedule | Next Run | Prompt"]
        lines.append("-" * 80)
        for tid, fold, prompt, stype, sval, last_run, next_run in rows:
            lines.append(
                f"{tid} | {fold} | {stype} | {sval} | {next_run or '—'} | {str(prompt)[:50]}"
            )
        return "\n".join(lines)
    except sqlite3.Error as e:
        return f"DB error: {e}"


def _tool_db_query(args: dict) -> str:
    sql = args.get("sql", "").strip()
    limit = min(int(args.get("limit", 50)), 200)

    if not sql:
        return "Error: sql is required"

    # Block non-SELECT statements by prefix
    sql_upper = sql.upper().lstrip()
    if not sql_upper.startswith("SELECT") and not sql_upper.startswith("WITH"):
        return "Error: only SELECT (and WITH) queries are allowed"

    # Reject comment sequences that can be used to obscure injection payloads
    if "/*" in sql or "--" in sql:
        return "Error: SQL comments are not allowed"

    # Reject semicolons to prevent stacked queries
    if ";" in sql:
        return "Error: semicolons are not allowed"

    if not _DB_PATH.exists():
        return f"Database not found at {_DB_PATH}"

    # SQLite authorizer action codes
    _SQLITE_SELECT    = 21
    _SQLITE_READ      = 20
    _SQLITE_FUNCTION  = 31
    _SQLITE_RECURSIVE = 33

    def _read_only_authorizer(action, arg1, arg2, db_name, trigger_name):  # noqa: ANN001
        """Allow only read operations; deny anything that could mutate data."""
        if action in (_SQLITE_SELECT, _SQLITE_READ, _SQLITE_FUNCTION, _SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    try:
        con = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
        con.set_authorizer(_read_only_authorizer)
        try:
            cur = con.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(limit)
        finally:
            con.set_authorizer(None)
            con.close()

        if not rows:
            return "(no rows)"

        lines = [" | ".join(cols)]
        lines.append("-" * min(80, sum(len(c) + 3 for c in cols)))
        for row in rows:
            lines.append(" | ".join(str(v)[:40] if v is not None else "NULL" for v in row))

        if len(rows) == limit:
            lines.append(f"(showing first {limit} rows)")

        return "\n".join(lines)
    except sqlite3.Error as e:
        return f"DB error: {e}"


def _tool_restart_service(args: dict) -> str:
    if not args.get("confirm"):
        return "Restart cancelled — set confirm=true to proceed."

    # Try systemd
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "evoclaw"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            subprocess.run(["systemctl", "restart", "evoclaw"], timeout=10, check=True)
            return "EvoClaw service restarted via systemd."
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        pass

    # Try launchd
    plist_label = "com.evoclaw.agent"
    try:
        subprocess.run(["launchctl", "kickstart", "-k", f"system/{plist_label}"],
                       timeout=10, check=True, capture_output=True)
        return "EvoClaw service restarted via launchd."
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        pass

    # Try bare process (write restart sentinel file)
    sentinel = _DATA_DIR / ".restart_requested"
    try:
        sentinel.touch()
        return (
            "Restart sentinel written to .restart_requested. "
            "The host process will restart on its next health-check tick. "
            "(systemd/launchd not available on this host)"
        )
    except OSError as e:
        return f"Could not restart: systemd/launchd unavailable and sentinel write failed: {e}"


# ── Tool dispatch ────────────────────────────────────────────────────────────

_TOOL_FNS = {
    "evoclaw_get_logs": _tool_get_logs,
    "evoclaw_list_groups": _tool_list_groups,
    "evoclaw_group_status": _tool_group_status,
    "evoclaw_send_message": _tool_send_message,
    "evoclaw_list_tasks": _tool_list_tasks,
    "evoclaw_db_query": _tool_db_query,
    "evoclaw_restart_service": _tool_restart_service,
}


def _call_tool(name: str, args: dict) -> str:
    fn = _TOOL_FNS.get(name)
    if not fn:
        raise ValueError(f"Unknown tool: {name}")
    try:
        return fn(args)
    except Exception as exc:
        return f"Tool error: {exc}"


# ── MCP JSON-RPC dispatcher ──────────────────────────────────────────────────

def _handle(request: dict) -> dict | None:
    """Process one JSON-RPC request and return a response (or None for notifications)."""
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params") or {}

    # Notifications (no id) — no response required
    if req_id is None and method == "notifications/initialized":
        return None

    if method == "initialize":
        return _ok(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "evoclaw", "version": "1.29.0"},
        })

    if method == "tools/list":
        return _ok(req_id, {"tools": _TOOLS})

    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments") or {}
        result_text = _call_tool(tool_name, tool_args)
        return _ok(req_id, {
            "content": [{"type": "text", "text": result_text}],
        })

    if method == "ping":
        return _ok(req_id, {})

    return _err(req_id, -32601, f"Method not found: {method}")


# ── Main stdio event loop ────────────────────────────────────────────────────

async def _main_async() -> None:
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    loop = asyncio.get_event_loop()
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    writer_transport, writer_protocol = await loop.connect_write_pipe(
        lambda: asyncio.streams.FlowControlMixin(loop=loop),
        sys.stdout,
    )
    writer = asyncio.StreamWriter(writer_transport, writer_protocol, reader, loop)

    def _write(obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        writer_transport.write(line.encode())

    # Send server ready notification
    _write(_notification("notifications/initialized", {}))

    while True:
        try:
            line = await reader.readline()
        except asyncio.IncompleteReadError:
            break
        if not line:
            break
        raw = line.decode(errors="replace").strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as exc:
            _write(_err(None, -32700, f"Parse error: {exc}"))
            continue

        resp = _handle(req)
        if resp is not None:
            _write(resp)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        asyncio.run(_main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
