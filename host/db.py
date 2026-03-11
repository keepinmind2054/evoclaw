"""EvoClaw SQLite database layer"""
import atexit
import re
import sqlite3
import json
import threading
import time
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# 全域 SQLite 連線，由 init_database() 初始化後供所有函式共用
_db: Optional[sqlite3.Connection] = None

# Threading lock protecting the shared _db connection.
# Although asyncio is single-threaded, several components run DB calls from
# background threads (evolution daemon via asyncio.to_thread, dashboard thread,
# webportal thread).  check_same_thread=False disables SQLite's own check but
# does NOT make the Connection thread-safe — the lock provides that guarantee.
_db_lock: threading.Lock = threading.Lock()

def get_db() -> sqlite3.Connection:
    """取得全域 DB 連線，若尚未初始化則拋出例外。

    Callers that execute queries from background threads (dashboard, webportal,
    evolution daemon) MUST hold _db_lock for the duration of the query + commit:

        with _db_lock:
            db = get_db()
            db.execute(...)
            db.commit()

    Within the asyncio event loop (single-threaded) the lock is uncontested and
    adds negligible overhead.
    """
    global _db
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _db

def init_database(db_path: Path) -> None:
    """
    初始化 SQLite 資料庫連線並建立所有資料表。

    check_same_thread=False：因為 asyncio 可能在不同 thread 呼叫，
    但實際上我們保證所有 DB 操作都在同一個 event loop 中序列執行。
    row_factory=sqlite3.Row：讓查詢結果可以用欄位名稱存取（dict-like），
    而不是只能用索引，提升程式碼可讀性。
    """
    global _db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _db = sqlite3.connect(str(db_path), check_same_thread=False)
    _db.row_factory = sqlite3.Row
    _db.execute("PRAGMA journal_mode=WAL")
    _db.execute("PRAGMA synchronous=NORMAL")
    _db.execute("PRAGMA busy_timeout=5000")  # 5s retry on SQLITE_BUSY
    _create_tables(_db)
    log.info(f"Database initialized: {db_path}")

def _create_tables(db: sqlite3.Connection) -> None:
    """
    建立所有必要的資料表（若尚不存在）。

    各資料表用途：
    - chats：記錄已知的聊天室（群組或個人），儲存顯示名稱、頻道類型等中繼資料
    - messages：所有收到的訊息本文，是 message loop 的主要讀取來源
      idx_messages_chat_ts：複合索引，加速「依群組 + 時間戳記」的查詢
    - scheduled_tasks：排程任務定義，包含 cron/interval/once 三種類型
    - task_run_logs：每次任務執行的結果記錄，用於監控與除錯
    - router_state：通用 key-value 狀態存儲，目前用於持久化 lastTimestamp 游標
    - sessions：記錄每個群組的 Claude session ID，讓 agent 保有對話記憶
    - registered_groups：已登記的群組設定，包含 JID、folder、觸發關鍵字等
    """
    db.executescript("""
    CREATE TABLE IF NOT EXISTS chats (
        jid TEXT PRIMARY KEY,
        name TEXT,
        last_message_time INTEGER,
        channel TEXT,
        is_group INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        chat_jid TEXT NOT NULL,
        sender TEXT,
        sender_name TEXT,
        content TEXT,
        timestamp INTEGER NOT NULL,
        is_from_me INTEGER DEFAULT 0,
        is_bot_message INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_jid, timestamp);

    CREATE TABLE IF NOT EXISTS scheduled_tasks (
        id TEXT PRIMARY KEY,
        group_folder TEXT NOT NULL,
        chat_jid TEXT NOT NULL,
        prompt TEXT NOT NULL,
        schedule_type TEXT NOT NULL,
        schedule_value TEXT NOT NULL,
        next_run INTEGER,
        last_run INTEGER,
        last_result TEXT,
        status TEXT DEFAULT 'active',
        created_at INTEGER NOT NULL,
        context_mode TEXT DEFAULT 'group'
    );

    CREATE TABLE IF NOT EXISTS task_run_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        run_at INTEGER NOT NULL,
        duration_ms INTEGER,
        status TEXT,
        result TEXT,
        error TEXT
    );

    CREATE TABLE IF NOT EXISTS router_state (
        key TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS sessions (
        group_folder TEXT PRIMARY KEY,
        session_id TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS registered_groups (
        jid TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        folder TEXT NOT NULL,
        trigger_pattern TEXT,
        added_at INTEGER NOT NULL,
        container_config TEXT,
        requires_trigger INTEGER DEFAULT 1,
        is_main INTEGER DEFAULT 0
    );

    -- ── 演化引擎資料表 ──────────────────────────────────────────────────────
    -- evolution_runs：記錄每次 container 執行的效能數據，是適應度計算的原始資料
    CREATE TABLE IF NOT EXISTS evolution_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        jid TEXT NOT NULL,
        run_id TEXT NOT NULL,
        response_ms INTEGER,
        retry_count INTEGER DEFAULT 0,
        success INTEGER DEFAULT 1,
        timestamp TEXT DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_evolution_jid_ts ON evolution_runs(jid, timestamp);

    -- group_genome：每個群組的行為基因組，記錄演化出的回應風格偏好
    CREATE TABLE IF NOT EXISTS group_genome (
        jid TEXT PRIMARY KEY,
        response_style TEXT DEFAULT 'balanced',
        formality REAL DEFAULT 0.5,
        technical_depth REAL DEFAULT 0.5,
        generation INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT (datetime('now'))
    );

    -- immune_threats：免疫系統的威脅記錄，形成「抗體」記憶防止重複攻擊
    CREATE TABLE IF NOT EXISTS immune_threats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_jid TEXT NOT NULL,
        pattern_hash TEXT NOT NULL,
        threat_type TEXT NOT NULL,
        count INTEGER DEFAULT 1,
        blocked INTEGER DEFAULT 0,
        first_seen TEXT DEFAULT (datetime('now')),
        last_seen TEXT DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_immune_sender ON immune_threats(sender_jid);

    CREATE TABLE IF NOT EXISTS evolution_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT DEFAULT (datetime('now')),
        jid TEXT NOT NULL,
        event_type TEXT NOT NULL,
        generation_before INTEGER,
        generation_after INTEGER,
        fitness_score REAL,
        avg_response_ms REAL,
        genome_before TEXT,
        genome_after TEXT,
        notes TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_evo_log_jid ON evolution_log(jid, timestamp);
    CREATE INDEX IF NOT EXISTS idx_evo_log_type ON evolution_log(event_type, timestamp);

-- ── Dev Engine 資料表 ──────────────────────────────────────────────────────
-- dev_sessions：7 階段開發引擎的完整 session 記錄（含每個階段的 artifact）
CREATE TABLE IF NOT EXISTS dev_sessions (
    session_id   TEXT PRIMARY KEY,
    jid          TEXT NOT NULL,
    prompt       TEXT NOT NULL,
    mode         TEXT NOT NULL DEFAULT 'auto',
    status       TEXT NOT NULL DEFAULT 'pending',
    current_stage TEXT,
    artifacts    TEXT DEFAULT '{}',
    error        TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dev_sessions_jid ON dev_sessions(jid, created_at);

-- dev_events：記錄 7 階段開發流程的事件（保留供向後相容）
CREATE TABLE IF NOT EXISTS dev_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    jid TEXT NOT NULL,
    event_type TEXT NOT NULL,
    stage TEXT NOT NULL,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_dev_jid ON dev_events(jid);
CREATE INDEX IF NOT EXISTS idx_dev_stage ON dev_events(stage);
    """)
    db.commit()

# ── Messages ──────────────────────────────────────────────────────────────────

def store_message(msg_id: str, chat_jid: str, sender: str, sender_name: str,
                  content: str, timestamp: int, is_from_me: bool = False,
                  is_bot_message: bool = False) -> None:
    """
    將一則訊息儲存到 messages 表。

    INSERT OR IGNORE：若相同 msg_id 已存在（例如頻道重送），則靜默忽略，
    確保冪等性（idempotent），避免重複處理同一則訊息。

    is_from_me / is_bot_message：用於在 get_new_messages 時過濾掉
    系統自己發出的訊息，避免 bot 回覆自己觸發無限迴圈。
    """
    with _db_lock:
        db = get_db()
        db.execute("""
            INSERT OR IGNORE INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_bot_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (msg_id, chat_jid, sender, sender_name, content, timestamp, int(is_from_me), int(is_bot_message)))
        db.commit()

def get_new_messages(jids: list[str], last_timestamp: int) -> list[dict]:
    """
    取得所有指定 JID 群組中、時間戳記比 last_timestamp 更新的訊息。

    這是「時間戳記游標」（timestamp cursor）模式的核心查詢：
    - 每次 message loop 執行後，last_timestamp 會推進到已處理訊息的最大時間戳記
    - 下次查詢只取比這個值更新的訊息，自然實現「不重複、不遺漏」的效果
    - 過濾 is_from_me=0 和 is_bot_message=0，只處理用戶發送的真實訊息

    使用 IN (?) 而非逐一查詢，減少資料庫 round-trip 次數。
    """
    if not jids:
        return []
    db = get_db()
    placeholders = ",".join("?" * len(jids))
    rows = db.execute(f"""
        SELECT * FROM messages
        WHERE chat_jid IN ({placeholders}) AND timestamp > ? AND is_from_me = 0 AND is_bot_message = 0
        ORDER BY timestamp ASC
    """, (*jids, last_timestamp)).fetchall()
    return [dict(r) for r in rows]


def get_conversation_history(jid: str, limit: int = 20) -> list[dict]:
    """
    取得指定群組最近 N 條訊息（含用戶和 bot 的回覆），作為對話歷史上下文。
    讓 agent 在每次啟動時都能看到先前的對話脈絡，實現多輪記憶。
    """
    db = get_db()
    rows = db.execute("""
        SELECT chat_jid, sender, sender_name, content, timestamp, is_bot_message
        FROM messages
        WHERE chat_jid = ?
        ORDER BY timestamp DESC
        LIMIT ?
    """, (jid, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]

def get_messages_since(chat_jid: str, timestamp: int, limit: int = 50) -> list[dict]:
    """取得某個聊天室從指定時間點起的訊息（含所有類型），用於提供對話歷史給 agent。"""
    db = get_db()
    rows = db.execute("""
        SELECT * FROM messages WHERE chat_jid = ? AND timestamp >= ?
        ORDER BY timestamp ASC LIMIT ?
    """, (chat_jid, timestamp, limit)).fetchall()
    return [dict(r) for r in rows]

def store_chat_metadata(jid: str, name: str, timestamp: int, channel: str, is_group: bool) -> None:
    """
    更新 chats 表中聊天室的中繼資料。

    store_message 儲存的是訊息「內容」，
    store_chat_metadata 儲存的是聊天室「屬性」（名稱、頻道類型、最後活躍時間）。
    兩者分開儲存：即使沒有新訊息，也可以更新聊天室名稱。

    ON CONFLICT DO UPDATE：若 JID 已存在，只更新名稱和最後訊息時間，
    不覆蓋 channel 和 is_group（這些在建立後不應改變）。
    """
    with _db_lock:
        db = get_db()
        db.execute("""
            INSERT INTO chats (jid, name, last_message_time, channel, is_group)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(jid) DO UPDATE SET name=excluded.name, last_message_time=excluded.last_message_time
        """, (jid, name, timestamp, channel, int(is_group)))
        db.commit()

# ── Router state ──────────────────────────────────────────────────────────────

def get_state(key: str) -> Optional[str]:
    """從 router_state 表讀取通用 key-value 狀態（目前主要用於 lastTimestamp）。"""
    db = get_db()
    row = db.execute("SELECT value FROM router_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None

def set_state(key: str, value: str) -> None:
    """寫入或更新 router_state 表中的 key-value 狀態。"""
    with _db_lock:
        db = get_db()
        db.execute("INSERT OR REPLACE INTO router_state (key, value) VALUES (?, ?)", (key, value))
        db.commit()

# ── Sessions ──────────────────────────────────────────────────────────────────

def get_session(group_folder: str) -> Optional[str]:
    """取得某群組的 Claude session ID，讓 agent 延續上次的對話記憶。"""
    db = get_db()
    row = db.execute("SELECT session_id FROM sessions WHERE group_folder=?", (group_folder,)).fetchone()
    return row["session_id"] if row else None

def set_session(group_folder: str, session_id: str) -> None:
    """儲存或更新某群組的 Claude session ID（container 每次執行後可能產生新 session）。"""
    db = get_db()
    db.execute("INSERT OR REPLACE INTO sessions (group_folder, session_id) VALUES (?, ?)", (group_folder, session_id))
    db.commit()

# ── Registered groups ─────────────────────────────────────────────────────────

def get_all_registered_groups() -> list[dict]:
    """取得所有已登記群組的完整設定清單，啟動時載入到記憶體中。"""
    db = get_db()
    rows = db.execute("SELECT * FROM registered_groups").fetchall()
    return [dict(r) for r in rows]

def get_registered_group(jid: str) -> Optional[dict]:
    """根據 JID 查找單一群組設定，找不到時回傳 None。"""
    db = get_db()
    row = db.execute("SELECT * FROM registered_groups WHERE jid=?", (jid,)).fetchone()
    return dict(row) if row else None

def _validate_folder(folder: str) -> str:
    """Validate folder name to prevent path traversal attacks."""
    if not folder or ".." in folder or "/" in folder or "\\" in folder:
        raise ValueError(f"Invalid folder name: {folder!r}")
    if not re.match(r'^[\w\-]+$', folder):
        raise ValueError(f"Folder name contains invalid characters: {folder!r}")
    return folder

def set_registered_group(jid: str, name: str, folder: str, trigger_pattern: Optional[str],
                          container_config: Optional[dict], requires_trigger: bool,
                          is_main: bool) -> None:
    """
    新增或更新群組登記記錄。

    container_config 以 JSON 字串儲存（未來用於自訂 container 設定）。
    added_at 記錄登記時間（毫秒 Unix timestamp）。
    INSERT OR REPLACE 確保相同 JID 的群組設定可以被覆寫更新。

    Enforces single-main-group invariant: if is_main=True, all other groups
    are demoted to is_main=False before inserting/updating this record.
    """
    _validate_folder(folder)
    db = get_db()
    if is_main:
        # Enforce single main group invariant — demote all other groups
        db.execute("UPDATE registered_groups SET is_main = 0 WHERE jid != ?", (jid,))
    db.execute("""
        INSERT OR REPLACE INTO registered_groups
        (jid, name, folder, trigger_pattern, added_at, container_config, requires_trigger, is_main)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (jid, name, folder, trigger_pattern, int(time.time() * 1000),
          json.dumps(container_config) if container_config else None,
          int(requires_trigger), int(is_main)))
    db.commit()

# ── Scheduled tasks ───────────────────────────────────────────────────────────

def create_task(task_id: str, group_folder: str, chat_jid: str, prompt: str,
                schedule_type: str, schedule_value: str, next_run: Optional[int],
                context_mode: str = "group") -> None:
    """
    在 scheduled_tasks 表中新增一筆排程任務記錄。

    next_run 是預計下次執行的時間（毫秒 Unix timestamp），
    由 _compute_next_run 根據 schedule_type 計算。
    context_mode 控制 container 是否帶入對話歷史（"group" = 帶入, "isolated" = 不帶入）。
    """
    db = get_db()
    db.execute("""
        INSERT INTO scheduled_tasks
        (id, group_folder, chat_jid, prompt, schedule_type, schedule_value, next_run, created_at, context_mode)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (task_id, group_folder, chat_jid, prompt, schedule_type, schedule_value,
          next_run, int(time.time() * 1000), context_mode))
    db.commit()

def get_all_tasks(group_folder: Optional[str] = None) -> list[dict]:
    """取得所有排程任務（可選擇性地過濾特定群組）。"""
    db = get_db()
    if group_folder:
        rows = db.execute("SELECT * FROM scheduled_tasks WHERE group_folder=?", (group_folder,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM scheduled_tasks").fetchall()
    return [dict(r) for r in rows]

def get_due_tasks(now_ms: int) -> list[dict]:
    """
    查詢所有「已到期」的排程任務：狀態為 active 且 next_run <= 當前時間。
    scheduler loop 每次輪詢時呼叫此函式取得待執行的任務清單。
    """
    db = get_db()
    rows = db.execute("""
        SELECT * FROM scheduled_tasks
        WHERE status='active' AND next_run IS NOT NULL AND next_run <= ?
    """, (now_ms,)).fetchall()
    return [dict(r) for r in rows]

def update_task(task_id: str, **kwargs) -> None:
    """
    動態更新排程任務的欄位。

    使用白名單（allowed）過濾 kwargs，防止 SQL injection 或意外修改不該改的欄位。
    動態產生 SET 子句，只更新傳入的欄位，不影響其他欄位。
    """
    db = get_db()
    allowed = {"prompt", "schedule_type", "schedule_value", "next_run", "last_run", "last_result", "status", "context_mode"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE scheduled_tasks SET {set_clause} WHERE id=?", (*fields.values(), task_id))
    db.commit()

def delete_task(task_id: str) -> None:
    """永久刪除排程任務（cancel 操作）。"""
    db = get_db()
    db.execute("DELETE FROM scheduled_tasks WHERE id=?", (task_id,))
    db.commit()

def log_task_run(task_id: str, run_at: int, duration_ms: int, status: str,
                 result: Optional[str], error: Optional[str]) -> None:
    """
    記錄一次任務執行的結果到 task_run_logs 表。

    每次執行都會寫入一筆記錄（不更新舊記錄），形成執行歷史，
    方便監控任務是否正常執行、失敗原因為何。
    duration_ms 是執行時間（毫秒），可用於分析效能瓶頸。
    """
    with _db_lock:
        db = get_db()
        db.execute("""
            INSERT INTO task_run_logs (task_id, run_at, duration_ms, status, result, error)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (task_id, run_at, duration_ms, status, result, error))
        db.commit()


# ── Evolution Engine ───────────────────────────────────────────────────────────

def record_evolution_run(jid: str, run_id: str, response_ms: int,
                          retry_count: int, success: bool) -> None:
    """
    記錄一次 container 執行的效能數據到 evolution_runs 表。

    每次 run_container_agent() 完成後呼叫，無論成功或失敗都記錄，
    確保適應度計算有完整的成功率樣本。
    """
    with _db_lock:
        db = get_db()
        db.execute("""
            INSERT INTO evolution_runs (jid, run_id, response_ms, retry_count, success)
            VALUES (?, ?, ?, ?, ?)
        """, (jid, run_id, response_ms, retry_count, int(success)))
        db.commit()


def get_evolution_runs(jid: str, days: int = 7) -> list[dict]:
    """
    取得指定群組在過去 days 天內的所有執行記錄，用於計算適應度分數。

    按時間倒序排列（最新的在前），最多取 200 筆避免計算過慢。
    """
    db = get_db()
    rows = db.execute("""
        SELECT success, response_ms, retry_count, timestamp
        FROM evolution_runs
        WHERE jid = ? AND timestamp > datetime('now', ? || ' days')
        ORDER BY timestamp DESC LIMIT 200
    """, (jid, f"-{days}")).fetchall()
    return [dict(r) for r in rows]


def get_active_evolution_jids(days: int = 7) -> list[str]:
    """
    取得在過去 days 天內有執行記錄的所有群組 JID 清單。

    演化 daemon 用此函式找出需要評估的群組，
    避免對沒有活躍數據的群組做無意義的演化計算。
    """
    db = get_db()
    rows = db.execute("""
        SELECT DISTINCT jid FROM evolution_runs
        WHERE timestamp > datetime('now', ? || ' days')
    """, (f"-{days}",)).fetchall()
    return [r["jid"] for r in rows]


def get_recent_run_stats(minutes: int = 5) -> Optional[dict]:
    """
    取得近 minutes 分鐘內的執行統計（數量與平均回應時間）。
    用於 get_system_load() 估算當前系統負載。
    """
    db = get_db()
    row = db.execute("""
        SELECT COUNT(*) as count, AVG(response_ms) as avg_ms
        FROM evolution_runs
        WHERE timestamp > datetime('now', ? || ' minutes')
    """, (f"-{minutes}",)).fetchone()
    return dict(row) if row else None


def get_group_genome(jid: str) -> Optional[dict]:
    """取得指定群組的行為基因組，找不到時回傳 None（表示尚未演化過）。"""
    db = get_db()
    row = db.execute("SELECT * FROM group_genome WHERE jid = ?", (jid,)).fetchone()
    return dict(row) if row else None


def upsert_group_genome(jid: str, **kwargs) -> None:
    """
    新增或更新群組基因組。

    使用 ON CONFLICT DO UPDATE 確保冪等性：
    若 JID 已存在則只更新傳入的欄位，不覆蓋未傳入的欄位。
    動態產生 SET 子句（白名單過濾防止注入）。
    """
    db = get_db()
    allowed = {"response_style", "formality", "technical_depth", "generation"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}

    # 先確保記錄存在（INSERT OR IGNORE 建立預設值）
    db.execute("""
        INSERT OR IGNORE INTO group_genome (jid) VALUES (?)
    """, (jid,))

    if fields:
        fields["updated_at"] = "datetime('now')"
        # updated_at 用 SQL 函式，需特殊處理
        set_parts = []
        values = []
        for k, v in fields.items():
            if k == "updated_at":
                set_parts.append(f"{k} = datetime('now')")
            else:
                set_parts.append(f"{k} = ?")
                values.append(v)
        set_clause = ", ".join(set_parts)
        db.execute(f"UPDATE group_genome SET {set_clause} WHERE jid = ?",
                   (*values, jid))
    db.commit()


def record_immune_threat(sender_jid: str, pattern_hash: str, threat_type: str) -> int:
    """
    記錄一次免疫威脅事件，回傳該發送者的累計威脅次數。

    若相同的 (sender_jid, pattern_hash) 組合已存在，則更新計數和最後出現時間，
    避免重複記錄膨脹資料庫。

    回傳累計次數供呼叫方判斷是否需要自動封鎖。
    """
    db = get_db()
    existing = db.execute("""
        SELECT id, count FROM immune_threats
        WHERE sender_jid = ? AND pattern_hash = ?
    """, (sender_jid, pattern_hash)).fetchone()

    if existing:
        new_count = existing["count"] + 1
        db.execute("""
            UPDATE immune_threats SET count = ?, last_seen = datetime('now')
            WHERE id = ?
        """, (new_count, existing["id"]))
    else:
        db.execute("""
            INSERT INTO immune_threats (sender_jid, pattern_hash, threat_type)
            VALUES (?, ?, ?)
        """, (sender_jid, pattern_hash, threat_type))
        new_count = 1

    db.commit()

    # 回傳此發送者的所有威脅記錄總數（跨不同 pattern）
    total = db.execute("""
        SELECT SUM(count) as total FROM immune_threats WHERE sender_jid = ?
    """, (sender_jid,)).fetchone()
    return total["total"] if total else new_count


def is_sender_blocked(sender_jid: str) -> bool:
    """
    檢查指定發送者是否已被免疫系統封鎖。
    被封鎖的發送者的任何訊息都會被靜默丟棄。
    """
    db = get_db()
    row = db.execute("""
        SELECT 1 FROM immune_threats WHERE sender_jid = ? AND blocked = 1 LIMIT 1
    """, (sender_jid,)).fetchone()
    return row is not None


def block_sender(sender_jid: str) -> None:
    """將指定發送者的所有威脅記錄標記為已封鎖（blocked = 1）。"""
    db = get_db()
    db.execute("UPDATE immune_threats SET blocked = 1 WHERE sender_jid = ?", (sender_jid,))
    db.commit()


def get_recent_threat_count(sender_jid: str, pattern_hash: str, hours: int = 1) -> int:
    """
    取得某個發送者在近 hours 小時內傳送特定 hash 訊息的次數。
    用於垃圾訊息偵測（同內容重複發送判定）。
    """
    db = get_db()
    row = db.execute("""
        SELECT count FROM immune_threats
        WHERE sender_jid = ? AND pattern_hash = ?
        AND last_seen > datetime('now', ? || ' hours')
    """, (sender_jid, pattern_hash, f"-{hours}")).fetchone()
    return row["count"] if row else 0


def get_immune_stats() -> dict:
    """取得免疫系統的摘要統計，用於監控面板或 IPC 查詢。"""
    db = get_db()
    row = db.execute("""
        SELECT
            COUNT(*) as total_threats,
            SUM(blocked) as blocked_senders,
            SUM(count) as total_incidents
        FROM immune_threats
    """).fetchone()
    return dict(row) if row else {"total_threats": 0, "blocked_senders": 0, "total_incidents": 0}


def log_evolution_event(jid: str, event_type: str, **kwargs) -> None:
    """
    記錄一次演化事件到 evolution_log 表。

    event_type 可為：
      - "genome_evolved"：基因組發生變化
      - "genome_unchanged"：評估後基因組未變（已達最佳化）
      - "cycle_start"：演化週期開始
      - "cycle_end"：演化週期結束（含統計）
      - "skipped_low_samples"：樣本不足，跳過演化
    """
    db = get_db()
    allowed = {"generation_before", "generation_after", "fitness_score",
               "avg_response_ms", "genome_before", "genome_after", "notes"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    # genome dicts → JSON strings
    for key in ("genome_before", "genome_after"):
        if key in fields and isinstance(fields[key], dict):
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    cols = ", ".join(["jid", "event_type"] + list(fields.keys()))
    placeholders = ", ".join(["?"] * (2 + len(fields)))
    values = [jid, event_type] + list(fields.values())
    db.execute(f"INSERT INTO evolution_log ({cols}) VALUES ({placeholders})", values)
    db.commit()


def get_evolution_log(jid: str = None, limit: int = 100, event_type: str = None) -> list:
    """
    查詢演化歷程日誌。

    參數：
      jid        — 指定群組（None = 所有群組）
      limit      — 最多回傳幾筆（預設 100）
      event_type — 過濾特定事件類型（None = 全部）
    """
    db = get_db()
    clauses = []
    params = []
    if jid:
        clauses.append("jid = ?")
        params.append(jid)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = db.execute(
        f"SELECT * FROM evolution_log {where} ORDER BY timestamp DESC LIMIT ?", params
    ).fetchall()
    return [dict(r) for r in rows]

# ── Dev Engine ──────────────────────────────────────────────────────────────────
def log_dev_event(jid: str, event_type: str, stage: str, notes: str) -> None:
    """
    記錄開發事件到 dev_events 表。
    用於追蹤 7 階段開發流程的進度與結果。
    """
    db = get_db()
    db.execute("""
        INSERT INTO dev_events (jid, event_type, stage, notes)
        VALUES (?, ?, ?, ?)
    """, (jid, event_type, stage, notes))
    db.commit()

def get_dev_events(jid: str = None, limit: int = 100, stage: str = None) -> list:
    """
    查詢開發事件日誌。
    參數：
    jid — 指定開發者/群組（None = 所有）
    limit — 最多回傳幾筆（預設 100）
    stage — 過濾特定階段（None = 全部）
    """
    db = get_db()
    clauses = []
    params = []
    if jid:
        clauses.append("jid = ?")
        params.append(jid)
    if stage:
        clauses.append("stage = ?")
        params.append(stage)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = db.execute(
        f"SELECT * FROM dev_events {where} ORDER BY timestamp DESC LIMIT ?", params
    ).fetchall()
    return [dict(r) for r in rows]


# ── Maintenance ────────────────────────────────────────────────────────────────

def prune_old_logs(days: int = 30) -> None:
    """Delete old rows from append-only log tables to prevent unbounded disk growth.

    Prunes:
    - task_run_logs: rows older than `days` days (keyed by run_at in ms)
    - evolution_runs: rows older than `days` days (keyed by timestamp TEXT)

    Safe to call at startup or from a periodic maintenance loop.
    On a deployment with 5 groups and per-minute tasks, 30 days retention keeps
    roughly 216,000 task_run_log rows — manageable for SQLite.
    """
    with _db_lock:
        db = get_db()
        # task_run_logs.run_at is stored as ms epoch integer
        cutoff_ms = int((time.time() - days * 86400) * 1000)
        db.execute("DELETE FROM task_run_logs WHERE run_at < ?", (cutoff_ms,))
        # evolution_runs.timestamp is a TEXT field in SQLite datetime format
        db.execute(
            "DELETE FROM evolution_runs WHERE timestamp < datetime('now', ?)",
            (f"-{days} days",),
        )
        db.commit()
    log.info("Pruned logs older than %d days (task_run_logs + evolution_runs)", days)


# ── Shutdown cleanup ───────────────────────────────────────────────────────────

def _close_connections() -> None:
    """Close the global DB connection on shutdown to prevent file lock residue."""
    global _db
    if _db is not None:
        try:
            _db.close()
        except Exception:
            pass
        _db = None

atexit.register(_close_connections)
