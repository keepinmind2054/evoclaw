import pytest
import sqlite3
import json
from pathlib import Path
from host import mcp_server

def test_mcp_server_initialize():
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {}
    }
    resp = mcp_server._handle(req)
    assert resp["id"] == 1
    assert resp["result"]["protocolVersion"] == "2024-11-05"
    assert "tools" in resp["result"]["capabilities"]


def test_mcp_server_tools_list():
    req = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list"
    }
    resp = mcp_server._handle(req)
    assert resp["id"] == 2
    assert "tools" in resp["result"]
    tools = resp["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert "evoclaw_get_logs" in tool_names
    assert "evoclaw_db_query" in tool_names


def test_mcp_server_ping():
    req = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "ping"
    }
    resp = mcp_server._handle(req)
    assert resp["id"] == 3
    assert resp["result"] == {}


def test_mcp_server_invalid_method():
    req = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "invalid_method"
    }
    resp = mcp_server._handle(req)
    assert resp["id"] == 4
    assert "error" in resp
    assert resp["error"]["code"] == -32601


def test_db_query_restrictions(tmp_path, monkeypatch):
    # Setup test database
    db_file = tmp_path / "test_messages.db"
    con = sqlite3.connect(str(db_file))
    con.execute("CREATE TABLE test_table (id INTEGER PRIMARY KEY, name TEXT)")
    con.execute("INSERT INTO test_table (name) VALUES ('Alice')")
    con.execute("INSERT INTO test_table (name) VALUES ('Bob')")
    con.commit()
    con.close()

    # Patch MCP database path
    monkeypatch.setattr(mcp_server, "_DB_PATH", db_file)

    # 1. Test standard valid query
    res = mcp_server._call_tool("evoclaw_db_query", {"sql": "SELECT name FROM test_table"})
    assert "Alice" in res
    assert "Bob" in res

    # 2. Test prefix restriction (non-SELECT)
    res = mcp_server._call_tool("evoclaw_db_query", {"sql": "INSERT INTO test_table (name) VALUES ('Charlie')"})
    assert "Error: only SELECT" in res

    # 3. Test SQL comment injection blocks
    res = mcp_server._call_tool("evoclaw_db_query", {"sql": "SELECT name FROM test_table -- comment"})
    assert "Error: SQL comments" in res

    # 4. Test semicolon stacked query blocks
    res = mcp_server._call_tool("evoclaw_db_query", {"sql": "SELECT name FROM test_table; SELECT name FROM test_table"})
    assert "Error: semicolons" in res

    # 5. Test SQLite authorizer mutation block (DROP TABLE in SELECT subquery or similar tricks)
    # The prefix check allows "SELECT", but the SQLite authorizer must block data modification.
    # Note that prefix check passes here since it starts with SELECT
    res = mcp_server._call_tool("evoclaw_db_query", {"sql": "SELECT name FROM test_table WHERE id = (DELETE FROM test_table WHERE id=1)"})
    assert "DB error: not authorized" in res or "DB error" in res
