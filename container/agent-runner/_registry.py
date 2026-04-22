"""Tool registry, declarations, and dispatcher for the EvoClaw agent runner."""
import json, os, sys, uuid, time, importlib
from pathlib import Path

try:
    from google import genai
    from google.genai import types
    _GOOGLE_AVAILABLE = True
except ImportError:
    genai = None  # type: ignore
    types = None  # type: ignore
    _GOOGLE_AVAILABLE = False

from _constants import (
    IPC_TASKS_DIR, WORKSPACE,
)
from _utils import _log, _atomic_ipc_write
from _tools import (
    tool_bash, tool_read, tool_write, tool_edit,
    tool_send_message, tool_schedule_task, tool_list_tasks,
    tool_cancel_task, tool_pause_task, tool_resume_task,
    tool_run_agent, tool_send_file, tool_start_remote_control,
    tool_self_update, tool_glob, tool_grep, tool_web_fetch,
    tool_memory_recall, tool_memory_remember,
    _messages_sent_via_tool,
)

_dynamic_tools: dict[str, dict] = {}  # name → {"fn": callable, "schema": dict, "description": str}


def _json_schema_to_gemini(props: dict, required: list):
    """將 JSON Schema properties 轉換為 Gemini types.Schema（僅支援常用型別）。"""
    if not _GOOGLE_AVAILABLE or types is None:
        return None
    gemini_props = {}
    for pname, pdef in props.items():
        ptype_str = pdef.get("type", "string").upper()
        ptype = getattr(types.Type, ptype_str, types.Type.STRING)
        gemini_props[pname] = types.Schema(
            type=ptype,
            description=pdef.get("description", ""),
        )
    return types.Schema(
        type=types.Type.OBJECT,
        properties=gemini_props,
        required=required or [],
    )


def register_dynamic_tool(name: str, description: str, schema: dict, fn) -> None:
    """
    動態注冊工具到所有 provider 宣告列表（Gemini / Claude / OpenAI）。
    由 /app/dynamic_tools/*.py 模組在 import 時呼叫。
    schema 使用 JSON Schema 格式（OpenAI/Claude 相容）。
    """
    _dynamic_tools[name] = {"fn": fn, "description": description, "schema": schema}
    props = schema.get("properties", {})
    req = schema.get("required", [])

    # Gemini FunctionDeclaration
    if _GOOGLE_AVAILABLE and types is not None:
        try:
            gemini_params = _json_schema_to_gemini(props, req)
            if gemini_params:
                TOOL_DECLARATIONS.append(
                    types.FunctionDeclaration(name=name, description=description, parameters=gemini_params)
                )
        except Exception:
            pass

    # Claude (Anthropic) tool declaration
    CLAUDE_TOOL_DECLARATIONS.append({
        "name": name,
        "description": description,
        "input_schema": schema,
    })

    # OpenAI-compatible tool declaration
    OPENAI_TOOL_DECLARATIONS.append({
        "type": "function",
        "function": {"name": name, "description": description, "parameters": schema},
    })

    _log("🔌 DYNAMIC", f"registered tool: {name}")


def _load_dynamic_tools() -> None:
    """
    自動 import /app/dynamic_tools/ 中的所有 .py 工具模組。
    每個模組應在 module level 呼叫 register_dynamic_tool()。
    這讓 DevEngine 生成的 Skill container_tools 不需重建 image 即可使用。
    """
    import importlib.util
    dynamic_dir = Path("/app/dynamic_tools")
    if not dynamic_dir.exists():
        return
    for py_file in sorted(dynamic_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"dynamic_tools.{py_file.stem}", py_file
            )
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                # 將 register_dynamic_tool 注入模組命名空間，讓工具可直接呼叫
                mod.register_dynamic_tool = register_dynamic_tool  # type: ignore[attr-defined]
                spec.loader.exec_module(mod)
                _log("🔌 DYNAMIC TOOL", f"loaded {py_file.name}")
        except Exception as exc:
            _log("⚠️ DYNAMIC TOOL", f"failed to load {py_file.name}: {exc}")


# ── Tool registry ─────────────────────────────────────────────────────────────

# 向 Gemini function calling API 宣告可用的工具
# Gemini 根據這些宣告決定何時呼叫哪個工具（function call）
# BUG-FIX: Guard with _GOOGLE_AVAILABLE so that importing this module when only
# the OpenAI or Claude backend is installed does not raise AttributeError on
# types.FunctionDeclaration (types is None when google-genai is absent).
TOOL_DECLARATIONS = [] if not _GOOGLE_AVAILABLE or types is None else [
    types.FunctionDeclaration(
        name="Bash",
        description="Execute a bash command in /workspace/group.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={"command": types.Schema(type=types.Type.STRING, description="The bash command to run")},
            required=["command"],
        ),
    ),
    types.FunctionDeclaration(
        name="Read",
        description="Read a file from the filesystem.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={"file_path": types.Schema(type=types.Type.STRING, description="Absolute path to the file")},
            required=["file_path"],
        ),
    ),
    types.FunctionDeclaration(
        name="Write",
        description="Write content to a file (creates parent dirs if needed).",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "file_path": types.Schema(type=types.Type.STRING, description="Absolute path to write to"),
                "content": types.Schema(type=types.Type.STRING, description="File content"),
            },
            required=["file_path", "content"],
        ),
    ),
    types.FunctionDeclaration(
        name="Edit",
        description="Find and replace a string in a file.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "file_path": types.Schema(type=types.Type.STRING, description="Path to the file"),
                "old_string": types.Schema(type=types.Type.STRING, description="Exact text to replace"),
                "new_string": types.Schema(type=types.Type.STRING, description="Replacement text"),
            },
            required=["file_path", "old_string", "new_string"],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__send_message",
        description="Send a message to the user in the chat.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "text": types.Schema(type=types.Type.STRING, description="Message text"),
                "sender": types.Schema(type=types.Type.STRING, description="Optional bot name"),
            },
            required=["text"],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__schedule_task",
        description="Schedule a recurring or one-time task.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "prompt": types.Schema(type=types.Type.STRING, description="What to do when task runs"),
                "schedule_type": types.Schema(type=types.Type.STRING, description="cron, interval, or once"),
                "schedule_value": types.Schema(type=types.Type.STRING, description="Cron expr, ms, or ISO timestamp"),
                "context_mode": types.Schema(type=types.Type.STRING, description="group or isolated"),
            },
            required=["prompt", "schedule_type", "schedule_value"],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__list_tasks",
        description="List all scheduled tasks for this group.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={},
            required=[],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__cancel_task",
        description="Cancel (delete) a scheduled task by its ID.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "task_id": types.Schema(type=types.Type.STRING, description="The task ID to cancel"),
            },
            required=["task_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__pause_task",
        description="Pause a scheduled task (it will not run until resumed).",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "task_id": types.Schema(type=types.Type.STRING, description="The task ID to pause"),
            },
            required=["task_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__resume_task",
        description="Resume a previously paused scheduled task.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "task_id": types.Schema(type=types.Type.STRING, description="The task ID to resume"),
            },
            required=["task_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="Glob",
        description="Find files matching a glob pattern (supports ** for recursive search).",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "pattern": types.Schema(type=types.Type.STRING, description="Glob pattern, e.g. '**/*.py'"),
                "path": types.Schema(type=types.Type.STRING, description="Base directory (default: /workspace/group)"),
            },
            required=["pattern"],
        ),
    ),
    types.FunctionDeclaration(
        name="Grep",
        description="Search file contents using regex. Returns filename:line:content for each match.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "pattern": types.Schema(type=types.Type.STRING, description="Regex pattern to search for"),
                "path": types.Schema(type=types.Type.STRING, description="Directory to search (default: /workspace/group)"),
                "include": types.Schema(type=types.Type.STRING, description="File filter e.g. '*.py' (default: all files)"),
            },
            required=["pattern"],
        ),
    ),
    types.FunctionDeclaration(
        name="WebFetch",
        description="Fetch content from a URL and return it as plain text (chunked). Each call returns up to ~3500 chars starting at `offset`. If the response footer says `[chunk: chars X-Y of total Z]` and Y < Z, call again with offset=Y to get the next chunk.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "url": types.Schema(type=types.Type.STRING, description="The URL to fetch"),
                "offset": types.Schema(type=types.Type.INTEGER, description="Starting character offset (default 0). Use the value from the previous response's footer to get the next chunk."),
                "max_chars": types.Schema(type=types.Type.INTEGER, description="Max characters per chunk (default 3500, max 8000)."),
            },
            required=["url"],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__run_agent",
        description="Spawn a subagent in an isolated Docker container to handle a subtask. Blocks until the subagent completes (up to 300s) and returns its output.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "prompt": types.Schema(type=types.Type.STRING, description="The task for the subagent to execute"),
                "context_mode": types.Schema(type=types.Type.STRING, description="isolated (no history, default) or group (with conversation history)"),
            },
            required=["prompt"],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__send_file",
        description="Send a file to the user. Write the file to /workspace/group/output/ first, then call this tool with the absolute container path.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "chat_jid": types.Schema(type=types.Type.STRING, description="The chat JID to send the file to"),
                "file_path": types.Schema(type=types.Type.STRING, description="Absolute container path to the file, e.g. /workspace/group/output/report.pptx"),
                "caption": types.Schema(type=types.Type.STRING, description="Optional caption for the file"),
            },
            required=["file_path"],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__reset_group",
        description="Clear the failure counter for a group, unfreezing it if it was locked in cooldown. Use this when a group is stuck and not responding. Only callable from monitor group.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "jid": types.Schema(type=types.Type.STRING, description="The JID of the group to reset, e.g. tg:8259652816"),
            },
            required=["jid"],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__start_remote_control",
        description="Start a Claude Code remote-control session on the host. The host spawns `claude remote-control` in the EvoClaw directory and sends the resulting URL back to this chat. Use when the user wants to update code, restart EvoClaw, or open a live coding session.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "chat_jid": types.Schema(type=types.Type.STRING, description="The chat JID to send the URL to (auto-detected if omitted)"),
                "sender": types.Schema(type=types.Type.STRING, description="Optional sender name for logging"),
            },
            required=[],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__self_update",
        description="Pull the latest EvoClaw code from git and restart the host process. Use when the user asks to update, upgrade, or restart EvoClaw.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "chat_jid": types.Schema(type=types.Type.STRING, description="The chat JID to notify when update is done (auto-detected if omitted)"),
            },
            required=[],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__memory_recall",
        description="Query the agent memory store for relevant memories matching a natural-language query.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(type=types.Type.STRING, description="Natural-language query to search memories"),
                "k": types.Schema(type=types.Type.INTEGER, description="Maximum number of memories to return (default 5)"),
                "namespace": types.Schema(type=types.Type.STRING, description="Optional project namespace / scope filter"),
                "topic_tag": types.Schema(type=types.Type.STRING, description="Optional topic tag for future filtering"),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__memory_remember",
        description="Store a new memory in the agent memory store so it can be recalled in future sessions.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "content": types.Schema(type=types.Type.STRING, description="Text content to remember"),
                "importance": types.Schema(type=types.Type.NUMBER, description="Importance weight 0.0-1.0 (default 0.7)"),
                "namespace": types.Schema(type=types.Type.STRING, description="Optional project namespace / scope"),
                "topic_tag": types.Schema(type=types.Type.STRING, description="Optional topic tag"),
            },
            required=["content"],
        ),
    ),
]



# ── OpenAI-compatible tool declarations ───────────────────────────────────────

OPENAI_TOOL_DECLARATIONS = [
    {"type": "function", "function": {"name": "Bash", "description": "Execute a bash command in /workspace/group.", "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "The bash command to run"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "Read", "description": "Read a file from the filesystem.", "parameters": {"type": "object", "properties": {"file_path": {"type": "string", "description": "Absolute path to the file"}}, "required": ["file_path"]}}},
    {"type": "function", "function": {"name": "Write", "description": "Write content to a file.", "parameters": {"type": "object", "properties": {"file_path": {"type": "string", "description": "Absolute path to write to"}, "content": {"type": "string", "description": "File content"}}, "required": ["file_path", "content"]}}},
    {"type": "function", "function": {"name": "Edit", "description": "Find and replace a string in a file.", "parameters": {"type": "object", "properties": {"file_path": {"type": "string", "description": "Path to the file"}, "old_string": {"type": "string", "description": "Exact text to replace"}, "new_string": {"type": "string", "description": "Replacement text"}}, "required": ["file_path", "old_string", "new_string"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__send_message", "description": "Send a message to the user in the chat.", "parameters": {"type": "object", "properties": {"text": {"type": "string", "description": "Message text"}, "sender": {"type": "string", "description": "Optional bot name"}}, "required": ["text"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__schedule_task", "description": "Schedule a recurring or one-time task.", "parameters": {"type": "object", "properties": {"prompt": {"type": "string", "description": "What to do when task runs"}, "schedule_type": {"type": "string", "description": "cron, interval, or once"}, "schedule_value": {"type": "string", "description": "Cron expr, ms, or ISO timestamp"}, "context_mode": {"type": "string", "description": "group or isolated"}}, "required": ["prompt", "schedule_type", "schedule_value"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__list_tasks", "description": "List all scheduled tasks for this group.", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__cancel_task", "description": "Cancel (delete) a scheduled task by its ID.", "parameters": {"type": "object", "properties": {"task_id": {"type": "string", "description": "The task ID to cancel"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__pause_task", "description": "Pause a scheduled task (it will not run until resumed).", "parameters": {"type": "object", "properties": {"task_id": {"type": "string", "description": "The task ID to pause"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__resume_task", "description": "Resume a previously paused scheduled task.", "parameters": {"type": "object", "properties": {"task_id": {"type": "string", "description": "The task ID to resume"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "Glob", "description": "Find files matching a glob pattern (supports ** recursive).", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "Grep", "description": "Search file contents with regex. Returns filename:line:content.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "WebFetch", "description": "Fetch a URL and return its content as plain text (chunked). Returns up to ~3500 chars starting at `offset`. If the footer reports more chars remaining, call again with offset=<end of previous chunk>.", "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "offset": {"type": "integer", "description": "starting char offset, default 0"}, "max_chars": {"type": "integer", "description": "max chars per chunk, default 3500, max 8000"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__run_agent", "description": "Spawn a subagent in an isolated Docker container to handle a subtask. Blocks until complete (up to 300s) and returns its output.", "parameters": {"type": "object", "properties": {"prompt": {"type": "string", "description": "The task for the subagent"}, "context_mode": {"type": "string", "description": "isolated or group"}}, "required": ["prompt"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__send_file", "description": "Send a file to the user. Write the file to /workspace/group/output/ first, then call this tool.", "parameters": {"type": "object", "properties": {"chat_jid": {"type": "string", "description": "The chat JID to send the file to"}, "file_path": {"type": "string", "description": "Absolute container path to the file"}, "caption": {"type": "string", "description": "Optional caption"}}, "required": ["file_path"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__reset_group", "description": "Clear the failure counter for a group, unfreezing it if it was locked in cooldown. Use when a group is stuck and not responding.", "parameters": {"type": "object", "properties": {"jid": {"type": "string", "description": "The JID of the group to reset, e.g. tg:8259652816"}}, "required": ["jid"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__start_remote_control", "description": "Start a Claude Code remote-control session. The host spawns `claude remote-control` and sends the URL back to this chat. Use when the user wants to update code or restart EvoClaw.", "parameters": {"type": "object", "properties": {"chat_jid": {"type": "string"}, "sender": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__self_update", "description": "Pull the latest EvoClaw code from git and restart the host process. Use when the user asks to update, upgrade, or restart EvoClaw.", "parameters": {"type": "object", "properties": {"chat_jid": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__memory_recall", "description": "Query the agent memory store for relevant memories matching a natural-language query.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Natural-language query to search memories"}, "k": {"type": "integer", "description": "Maximum number of memories to return (default 5)"}, "namespace": {"type": "string", "description": "Optional project namespace / scope filter"}, "topic_tag": {"type": "string", "description": "Optional topic tag for future filtering"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__memory_remember", "description": "Store a new memory in the agent memory store so it can be recalled in future sessions.", "parameters": {"type": "object", "properties": {"content": {"type": "string", "description": "Text content to remember"}, "importance": {"type": "number", "description": "Importance weight 0.0-1.0 (default 0.7)"}, "namespace": {"type": "string", "description": "Optional project namespace / scope"}, "topic_tag": {"type": "string", "description": "Optional topic tag"}}, "required": ["content"]}}},
]


# Claude (Anthropic) tool declarations
CLAUDE_TOOL_DECLARATIONS = [
    {"name": "Bash", "description": "Execute a bash command in /workspace/group.", "input_schema": {"type": "object", "properties": {"command": {"type": "string", "description": "The bash command to run"}}, "required": ["command"]}},
    {"name": "Read", "description": "Read a file from the filesystem.", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string", "description": "Absolute path to the file"}}, "required": ["file_path"]}},
    {"name": "Write", "description": "Write content to a file.", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}, "required": ["file_path", "content"]}},
    {"name": "Edit", "description": "Find and replace a string in a file.", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}, "required": ["file_path", "old_string", "new_string"]}},
    {"name": "mcp__evoclaw__send_message", "description": "Send a message to the user.", "input_schema": {"type": "object", "properties": {"text": {"type": "string"}, "sender": {"type": "string"}}, "required": ["text"]}},
    {"name": "mcp__evoclaw__schedule_task", "description": "Schedule a task.", "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}, "schedule_type": {"type": "string"}, "schedule_value": {"type": "string"}, "context_mode": {"type": "string"}}, "required": ["prompt", "schedule_type", "schedule_value"]}},
    {"name": "mcp__evoclaw__list_tasks", "description": "List all scheduled tasks for this group.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "mcp__evoclaw__cancel_task", "description": "Cancel (delete) a scheduled task by its ID.", "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "mcp__evoclaw__pause_task", "description": "Pause a scheduled task (it will not run until resumed).", "input_schema": {"type": "object", "properties": {"task_id": {"type": "string", "description": "The task ID to pause"}}, "required": ["task_id"]}},
    {"name": "mcp__evoclaw__resume_task", "description": "Resume a previously paused scheduled task.", "input_schema": {"type": "object", "properties": {"task_id": {"type": "string", "description": "The task ID to resume"}}, "required": ["task_id"]}},
    {"name": "Glob", "description": "Find files matching a glob pattern (supports ** recursive).", "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "Grep", "description": "Search file contents with regex. Returns filename:line:content.", "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "WebFetch", "description": "Fetch a URL and return its content as plain text (chunked). Returns up to ~3500 chars starting at `offset`. If the footer reports more chars remaining, call again with offset=<end of previous chunk>.", "input_schema": {"type": "object", "properties": {"url": {"type": "string"}, "offset": {"type": "integer", "description": "starting char offset, default 0"}, "max_chars": {"type": "integer", "description": "max chars per chunk, default 3500, max 8000"}}, "required": ["url"]}},
    {"name": "mcp__evoclaw__run_agent", "description": "Spawn a subagent in an isolated Docker container to handle a subtask. Blocks until complete (up to 300s) and returns its output.", "input_schema": {"type": "object", "properties": {"prompt": {"type": "string", "description": "The task for the subagent"}, "context_mode": {"type": "string", "description": "isolated or group"}}, "required": ["prompt"]}},
    {"name": "mcp__evoclaw__send_file", "description": "Send a file to the user. Write the file to /workspace/group/output/ first, then call this tool.", "input_schema": {"type": "object", "properties": {"chat_jid": {"type": "string", "description": "The chat JID to send the file to"}, "file_path": {"type": "string", "description": "Absolute container path to the file"}, "caption": {"type": "string", "description": "Optional caption"}}, "required": ["file_path"]}},
    {"name": "mcp__evoclaw__reset_group", "description": "Clear the failure counter for a group, unfreezing it if it was locked in cooldown. Use when a group is stuck and not responding.", "input_schema": {"type": "object", "properties": {"jid": {"type": "string", "description": "The JID of the group to reset, e.g. tg:8259652816"}}, "required": ["jid"]}},
    {"name": "mcp__evoclaw__start_remote_control", "description": "Start a Claude Code remote-control session. The host spawns `claude remote-control` and sends the URL back to this chat. Use when the user wants to update code or restart EvoClaw.", "input_schema": {"type": "object", "properties": {"chat_jid": {"type": "string"}, "sender": {"type": "string"}}, "required": []}},
    {"name": "mcp__evoclaw__self_update", "description": "Pull the latest EvoClaw code from git and restart the host process. Use when the user asks to update, upgrade, or restart EvoClaw.", "input_schema": {"type": "object", "properties": {"chat_jid": {"type": "string"}}, "required": []}},
    {"name": "mcp__evoclaw__memory_recall", "description": "Query the agent memory store for relevant memories matching a natural-language query.", "input_schema": {"type": "object", "properties": {"query": {"type": "string", "description": "Natural-language query to search memories"}, "k": {"type": "integer", "description": "Maximum number of memories to return (default 5)"}, "namespace": {"type": "string", "description": "Optional project namespace / scope filter"}, "topic_tag": {"type": "string", "description": "Optional topic tag for future filtering"}}, "required": ["query"]}},
    {"name": "mcp__evoclaw__memory_remember", "description": "Store a new memory in the agent memory store so it can be recalled in future sessions.", "input_schema": {"type": "object", "properties": {"content": {"type": "string", "description": "Text content to remember"}, "importance": {"type": "number", "description": "Importance weight 0.0-1.0 (default 0.7)"}, "namespace": {"type": "string", "description": "Optional project namespace / scope"}, "topic_tag": {"type": "string", "description": "Optional topic tag"}}, "required": ["content"]}},
]


def execute_tool(name: str, args: dict, chat_jid: str) -> str:
    """
    根據 Gemini 回傳的 function call 名稱，分派到對應的 tool 實作。
    chat_jid 傳給需要知道發送目標的工具（如 send_message）。
    """
    _log("🔧 TOOL", f"{name} args={str(args)[:1500]}")
    result = _execute_tool_inner(name, args, chat_jid)
    _log("🔧 RESULT", str(result)[:1500])
    return result


def _execute_tool_inner(name: str, args: dict, chat_jid: str) -> str:
    # BUG-P18D-11: validate that args is a dict before key access to avoid
    # AttributeError when the LLM passes a non-dict value.
    if not isinstance(args, dict):
        return f"Error: tool arguments must be a JSON object, got {type(args).__name__}"
    if name == "Bash":
        _cmd = args.get("command")
        if not isinstance(_cmd, str):
            return "Error: Bash requires a 'command' string argument"
        return tool_bash(_cmd)
    elif name == "Read":
        _fp = args.get("file_path")
        if not isinstance(_fp, str):
            return "Error: Read requires a 'file_path' string argument"
        return tool_read(_fp)
    elif name == "Write":
        _fp = args.get("file_path")
        _ct = args.get("content")
        if not isinstance(_fp, str):
            return "Error: Write requires a 'file_path' string argument"
        if not isinstance(_ct, str):
            return "Error: Write requires a 'content' string argument"
        return tool_write(_fp, _ct)
    elif name == "Edit":
        _fp = args.get("file_path")
        _os = args.get("old_string")
        _ns = args.get("new_string")
        if not isinstance(_fp, str):
            return "Error: Edit requires a 'file_path' string argument"
        if not isinstance(_os, str):
            return "Error: Edit requires an 'old_string' string argument"
        if not isinstance(_ns, str):
            return "Error: Edit requires a 'new_string' string argument"
        return tool_edit(_fp, _os, _ns)
    elif name == "mcp__evoclaw__send_message":
        _text = args.get("text")
        if not isinstance(_text, str):
            return "Error: send_message requires a 'text' string argument"
        _messages_sent_via_tool.append(True)  # 標記：已透過工具發送，host 不需再發 result
        return tool_send_message(chat_jid, _text, args.get("sender"))
    elif name == "mcp__evoclaw__schedule_task":
        _sched_prompt = args.get("prompt")
        _sched_type = args.get("schedule_type")
        _sched_val = args.get("schedule_value")
        if not isinstance(_sched_prompt, str):
            return "Error: schedule_task requires a 'prompt' string argument"
        if not isinstance(_sched_type, str):
            return "Error: schedule_task requires a 'schedule_type' string argument"
        if not isinstance(_sched_val, str):
            return "Error: schedule_task requires a 'schedule_value' string argument"
        return tool_schedule_task(
            _sched_prompt, _sched_type, _sched_val,
            args.get("context_mode", "group"),
            chat_jid,
        )
    elif name == "mcp__evoclaw__list_tasks":
        return tool_list_tasks()
    elif name == "mcp__evoclaw__cancel_task":
        return tool_cancel_task(args.get("task_id", ""))
    elif name == "mcp__evoclaw__pause_task":
        return tool_pause_task(args.get("task_id", ""))
    elif name == "mcp__evoclaw__resume_task":
        return tool_resume_task(args.get("task_id", ""))
    elif name == "Glob":
        _glob_pat = args.get("pattern")
        if not isinstance(_glob_pat, str):
            return "Error: Glob requires a 'pattern' string argument"
        return tool_glob(_glob_pat, args.get("path", WORKSPACE))
    elif name == "Grep":
        _grep_pat = args.get("pattern")
        if not isinstance(_grep_pat, str):
            return "Error: Grep requires a 'pattern' string argument"
        return tool_grep(_grep_pat, args.get("path", WORKSPACE), args.get("include", "*"))
    elif name == "WebFetch":
        _url = args.get("url")
        if not isinstance(_url, str):
            return "Error: WebFetch requires a 'url' string argument"
        _offset = args.get("offset", 0)
        _max_chars = args.get("max_chars", 3500)
        return tool_web_fetch(_url, offset=_offset, max_chars=_max_chars)
    elif name == "mcp__evoclaw__run_agent":
        _ra_prompt = args.get("prompt")
        if not isinstance(_ra_prompt, str):
            return "Error: run_agent requires a 'prompt' string argument"
        return tool_run_agent(_ra_prompt, args.get("context_mode", "isolated"))
    elif name == "mcp__evoclaw__send_file":
        _sf_fp = args.get("file_path")
        if not isinstance(_sf_fp, str):
            return "Error: send_file requires a 'file_path' string argument"
        return tool_send_file(args.get("chat_jid", chat_jid), _sf_fp, args.get("caption", ""))
    elif name == "mcp__evoclaw__reset_group":
        target_jid = args.get("jid", "")
        if not target_jid:
            return "Error: jid is required"
        try:
            Path(IPC_TASKS_DIR).mkdir(parents=True, exist_ok=True)
            uid = str(uuid.uuid4())[:8]
            fname = Path(IPC_TASKS_DIR) / f"{int(time.time()*1000)}-reset-{uid}.json"
            _atomic_ipc_write(fname, json.dumps({"type": "reset_group", "jid": target_jid}))
            _log("📨 IPC", f"type=reset_group jid={target_jid} → {fname.name}")
            return f"reset_group IPC sent for {target_jid} — fail counters will be cleared on next host poll cycle"
        except Exception as exc:
            return f"reset_group IPC write failed: {exc}"
    elif name == "mcp__evoclaw__start_remote_control":
        return tool_start_remote_control(args.get("chat_jid", chat_jid), args.get("sender", ""))
    elif name == "mcp__evoclaw__self_update":
        return tool_self_update(args.get("chat_jid", chat_jid))
    elif name == "mcp__evoclaw__memory_recall":
        _mr_query = args.get("query")
        if not isinstance(_mr_query, str):
            return "Error: memory_recall requires a 'query' string argument"
        return tool_memory_recall({
            "query": _mr_query,
            "k": args.get("k", 5),
            "namespace": args.get("namespace", ""),
            "topic_tag": args.get("topic_tag", ""),
        })
    elif name == "mcp__evoclaw__memory_remember":
        _mm_content = args.get("content")
        if not isinstance(_mm_content, str):
            return "Error: memory_remember requires a 'content' string argument"
        return tool_memory_remember({
            "content": _mm_content,
            "importance": args.get("importance", 0.7),
            "namespace": args.get("namespace", ""),
            "topic_tag": args.get("topic_tag", ""),
        })
    # ── Dynamic tools (installed via Skills container_tools:) ─────────────────
    if name in _dynamic_tools:
        try:
            return str(_dynamic_tools[name]["fn"](args))
        except Exception as exc:
            return f"Dynamic tool {name} error: {exc}"
    return f"Unknown tool: {name}"
