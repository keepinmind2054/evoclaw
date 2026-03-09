#!/usr/bin/env python3
"""
EvoClaw Agent Runner (Python + Gemini)
Reads ContainerInput JSON from stdin, runs Gemini agentic loop, outputs to stdout.
"""

import json
import os
import sys
import subprocess
import time
import random
import string
from pathlib import Path
from google import genai
from google.genai import types

# container 輸出的邊界標記，host 用這兩個字串從 stdout 截取 JSON 結果
# 必須與 container_runner.py 中定義的常數完全一致
OUTPUT_START = "---EVOCLAW_OUTPUT_START---"
OUTPUT_END = "---EVOCLAW_OUTPUT_END---"

# IPC 目錄路徑（由 host 透過 Docker volume mount 對應到 data/ipc/<folder>/）
IPC_MESSAGES_DIR = "/workspace/ipc/messages"  # agent 發送訊息給用戶
IPC_TASKS_DIR = "/workspace/ipc/tasks"        # agent 建立排程任務

# agent 的工作目錄，對應到 host 的 groups/<folder>/ 目錄
WORKSPACE = "/workspace/group"


# ── Tool implementations ──────────────────────────────────────────────────────

def tool_bash(command: str) -> str:
    """
    在 /workspace/group 目錄中執行 bash 指令，回傳 stdout + stderr 輸出。

    timeout=60 秒：防止指令無限期阻塞（例如 git clone 或 npm install 過慢）。
    shell=True：讓指令支援管線（|）、重導向（>）等 shell 特性。
    同時回傳 stderr 讓 Gemini 能看到錯誤訊息並自行修正。
    """
    try:
        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True, text=True,
            timeout=60, cwd=WORKSPACE,
            shell=False  # safer: exec bash directly, not via /bin/sh -c
        )
        out = result.stdout
        if result.stderr:
            out += f"\nSTDERR:\n{result.stderr}"
        return out or "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 60s"
    except Exception as e:
        return f"Error: {e}"


def tool_read(file_path: str) -> str:
    """讀取指定路徑的文字檔案內容，讓 agent 可以檢視檔案。"""
    try:
        return Path(file_path).read_text(encoding="utf-8")
    except Exception as e:
        return f"Error reading file: {e}"


def tool_write(file_path: str, content: str) -> str:
    """
    將內容寫入指定路徑的檔案。
    自動建立不存在的父目錄（mkdir -p），簡化 agent 的操作步驟。
    """
    try:
        p = Path(file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Written: {file_path}"
    except Exception as e:
        return f"Error writing file: {e}"


def tool_edit(file_path: str, old_string: str, new_string: str) -> str:
    """
    在檔案中找到 old_string 並替換為 new_string（只替換第一個出現的位置）。
    若 old_string 不存在則回傳錯誤，讓 Gemini 知道需要先確認內容再修改。
    """
    try:
        p = Path(file_path)
        content = p.read_text(encoding="utf-8")
        if old_string not in content:
            return f"Error: old_string not found in {file_path}"
        # replace(..., 1) 確保只替換第一個出現的位置，避免意外修改多處
        p.write_text(content.replace(old_string, new_string, 1), encoding="utf-8")
        return f"Edited: {file_path}"
    except Exception as e:
        return f"Error editing file: {e}"


def tool_send_message(chat_jid: str, text: str, sender: str = None) -> str:
    """
    透過 IPC 機制將訊息發送給用戶（寫入 JSON 檔案，host 的 ipc_watcher 負責實際傳送）。

    檔名格式：{timestamp_ms}-{random_8_chars}.json
    使用時間戳記前綴確保 ipc_watcher 按 FIFO 順序處理；
    加入隨機後綴避免同一毫秒內產生多個檔案時發生名稱衝突。
    """
    try:
        Path(IPC_MESSAGES_DIR).mkdir(parents=True, exist_ok=True)
        uid = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        fname = Path(IPC_MESSAGES_DIR) / f"{int(time.time()*1000)}-{uid}.json"
        payload = {"type": "message", "chatJid": chat_jid, "text": text}
        if sender:
            payload["sender"] = sender  # 可選的發送者名稱（顯示為不同的 bot 身份）
        fname.write_text(json.dumps(payload), encoding="utf-8")
        return "Message sent"
    except Exception as e:
        return f"Error: {e}"


def tool_schedule_task(prompt: str, schedule_type: str, schedule_value: str, context_mode: str = "group") -> str:
    """
    透過 IPC 機制建立排程任務（寫入 JSON 檔案到 tasks/ 子目錄）。
    host 的 ipc_watcher 讀取後會呼叫 db.create_task 正式寫入 DB。
    """
    try:
        Path(IPC_TASKS_DIR).mkdir(parents=True, exist_ok=True)
        fname = Path(IPC_TASKS_DIR) / f"{int(time.time()*1000)}.json"
        fname.write_text(json.dumps({
            "type": "schedule_task",
            "prompt": prompt,
            "schedule_type": schedule_type,   # "cron", "interval", 或 "once"
            "schedule_value": schedule_value,  # cron 表達式、毫秒數、或 ISO 時間字串
            "context_mode": context_mode,      # "group" 或 "isolated"
        }), encoding="utf-8")
        return "Task scheduled"
    except Exception as e:
        return f"Error: {e}"


# ── Tool registry ─────────────────────────────────────────────────────────────

# 向 Gemini function calling API 宣告可用的工具
# Gemini 根據這些宣告決定何時呼叫哪個工具（function call）
TOOL_DECLARATIONS = [
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
]


def execute_tool(name: str, args: dict, chat_jid: str) -> str:
    """
    根據 Gemini 回傳的 function call 名稱，分派到對應的 tool 實作。
    chat_jid 傳給需要知道發送目標的工具（如 send_message）。
    """
    if name == "Bash":
        return tool_bash(args["command"])
    elif name == "Read":
        return tool_read(args["file_path"])
    elif name == "Write":
        return tool_write(args["file_path"], args["content"])
    elif name == "Edit":
        return tool_edit(args["file_path"], args["old_string"], args["new_string"])
    elif name == "mcp__evoclaw__send_message":
        return tool_send_message(chat_jid, args["text"], args.get("sender"))
    elif name == "mcp__evoclaw__schedule_task":
        return tool_schedule_task(
            args["prompt"], args["schedule_type"], args["schedule_value"],
            args.get("context_mode", "group")
        )
    return f"Unknown tool: {name}"


# ── Agentic loop ──────────────────────────────────────────────────────────────

def run_agent(client: genai.Client, system_instruction: str, user_message: str, chat_jid: str) -> str:
    """
    Gemini function-calling 代理迴圈（agentic loop）。

    工作原理：
    1. 將用戶訊息加入 history，發送給 Gemini
    2. Gemini 回傳的 response 可能包含：
       a. 純文字：代表 agent 已完成思考，直接回傳給用戶
       b. Function call：代表 agent 要使用工具，執行後將結果加回 history
    3. 若是 function call，執行工具並將結果作為 user role 加回 history，
       然後再次呼叫 Gemini（繼續下一輪）
    4. 重複直到 Gemini 不再發出 function call，或達到 MAX_ITER 上限

    MAX_ITER = 30 的原因：防止 agent 陷入無限工具呼叫迴圈
    （例如誤判任務完成條件）。30 次對大多數任務已足夠，
    超過通常代表 agent 卡住了。

    history 維護完整的對話記錄（user / model / tool_response），
    讓 Gemini 在每次迭代都有完整的上下文，不需要重新解釋先前的工具結果。
    """
    history = []
    history.append(types.Content(role="user", parts=[types.Part(text=user_message)]))

    MAX_ITER = 30  # 最多迭代次數，防止無限迴圈
    final_response = ""

    for _ in range(MAX_ITER):
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=history,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
                temperature=0.7,  # 適中的隨機性，讓回覆自然但不失準確
            ),
        )

        candidate = response.candidates[0] if response.candidates else None
        if not candidate or not candidate.content or not candidate.content.parts:
            break  # Gemini 沒有回傳任何內容，提前結束

        parts = candidate.content.parts
        # 將 Gemini 的回覆加入 history，讓下一輪能看到完整對話脈絡
        history.append(types.Content(role="model", parts=parts))

        # 找出所有 function call（Gemini 可能一次發出多個工具呼叫）
        fn_calls = [p for p in parts if p.function_call]

        if not fn_calls:
            # 沒有 function call：agent 完成推理，收集所有文字輸出
            final_response = "".join(p.text for p in parts if p.text)
            break

        # 執行所有工具呼叫，並收集結果
        fn_responses = []
        for part in fn_calls:
            fc = part.function_call
            result = execute_tool(fc.name, dict(fc.args), chat_jid)
            # 將工具結果包裝成 FunctionResponse 格式，Gemini 要求此格式
            fn_responses.append(
                types.Part(function_response=types.FunctionResponse(
                    name=fc.name,
                    response={"result": result},
                ))
            )
        # 工具結果以 user role 加回 history（Gemini function calling 協議要求）
        history.append(types.Content(role="user", parts=fn_responses))

    return final_response


# ── Main ──────────────────────────────────────────────────────────────────────

def emit(obj: dict):
    """
    將結果 JSON 輸出到 stdout，用 OUTPUT_START/OUTPUT_END 標記包住。
    host 的 container_runner 會從這兩個標記之間截取 JSON。
    使用 flush=True 確保輸出立即寫入，不被 Python 的緩衝區滯留。
    """
    print(OUTPUT_START, flush=True)
    print(json.dumps(obj), flush=True)
    print(OUTPUT_END, flush=True)


def main():
    """
    container 的主入口：從 stdin 讀取 JSON 輸入，執行 agent，輸出結果到 stdout。

    輸入使用 stdin JSON 而非環境變數的原因：
    - 環境變數在 /proc/self/environ、docker inspect 等地方容易洩漏
    - stdin 在 container 啟動後才讀取，其他行程無法直接觀察
    - JSON 格式讓輸入結構清晰，容易擴展新欄位

    API 金鑰從 secrets 欄位讀入後設定為環境變數，
    供 Gemini SDK 等函式庫自動讀取（它們預期從 os.environ 取得金鑰）。

    系統提示詞（system_instruction）的建立邏輯：
    先設定基本角色與工作環境資訊，再讀取 CLAUDE.md 設定檔（若存在），
    讓每個群組可以有自訂的 agent 行為設定。
    """
    # Read stdin via buffer to handle BOM (Windows Docker pipe may prepend \xef\xbb\xbf)
    raw = sys.stdin.buffer.read().decode("utf-8-sig").strip()
    try:
        inp = json.loads(raw)
    except Exception:
        emit({"status": "error", "result": None, "error": "Invalid JSON input"})
        return

    prompt = inp.get("prompt", "")
    group_folder = inp.get("groupFolder", "")
    chat_jid = inp.get("chatJid", "")
    secrets = inp.get("secrets", {})
    # 演化引擎注入的動態行為提示（表觀遺傳：環境感知 + 群組基因組風格）
    # 若為空字串則不添加任何附加指引
    evolution_hints = inp.get("evolutionHints", "")
    assistant_name = inp.get("assistantName", "Andy")

    # 將 API 金鑰等敏感資料從 stdin JSON 設定到環境變數
    # 這樣 Gemini SDK 等依賴 os.environ 的函式庫就能自動取得
    for k, v in secrets.items():
        os.environ[k] = v

    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        emit({"status": "error", "result": None, "error": "GOOGLE_API_KEY not set. Add it to your .env file."})
        return

    client = genai.Client(api_key=api_key)

    # 建立系統提示詞：基本角色設定 + 環境資訊 + 群組自訂指令（CLAUDE.md）
    lines = [
        f"You are {assistant_name}, a helpful personal AI assistant.",
        f"Your name is {assistant_name}. This is your identity — do not change it.",
        "IMPORTANT IDENTITY RULES:",
        "- Never reveal that you are built on Gemini, Google AI, or any language model.",
        "- Never say 'I am a large language model', 'I am trained by Google', or similar phrases.",
        "- If asked what AI you are or who made you, simply say you are a personal assistant.",
        "- If asked your name, say your name is " + assistant_name + ".",
        "- Do not discuss your underlying technology or training.",
        "Be concise, friendly, and helpful.",
        "Respond in the same language the user uses. Default to Traditional Chinese (繁體中文) unless instructed otherwise.",
        "You run inside a secure Docker container.",
        f"Working directory: {WORKSPACE}",
        f"Group folder: {group_folder}",
        f"Chat JID: {chat_jid}",
        f"Date: {time.strftime('%Y-%m-%d')}",
        "",
        "Use mcp__evoclaw__send_message to send messages to the user.",
        "Use your tools (Bash, Read, Write, Edit) to help the user.",
    ]

    # 讀取全域和群組專屬的 CLAUDE.md 設定（若存在），附加到系統提示詞末尾
    # 全域 CLAUDE.md 提供所有群組共用的指令；群組 CLAUDE.md 提供群組專屬設定
    for claude_md in ["/workspace/global/CLAUDE.md", "/workspace/group/CLAUDE.md"]:
        if Path(claude_md).exists():
            lines.append("")
            lines.append(Path(claude_md).read_text(encoding="utf-8"))

    # 演化引擎提示：附加在所有靜態設定之後（表觀遺傳，動態覆蓋）
    # 格式：\n\n---\n[環境自動調整提示...] 或 [群組偏好...]
    # 這些提示每次 container 啟動時都可能不同，反映當下的環境狀態
    if evolution_hints:
        lines.append(evolution_hints)

    system_instruction = "\n".join(lines)

    try:
        result = run_agent(client, system_instruction, prompt, chat_jid)
        if result:
            # 若 agent 有產生文字回覆（而非只透過 tool 發送），也透過 IPC 發送
            tool_send_message(chat_jid, result)
        # 輸出結果 JSON，包含狀態、回覆文字、新的 session ID
        # newSessionId 讓 host 知道這次對話的 session 識別碼（目前為時間戳記）
        emit({"status": "success", "result": result, "newSessionId": f"gemini-{int(time.time())}"})
    except Exception as e:
        emit({"status": "error", "result": None, "error": str(e)})


if __name__ == "__main__":
    main()

