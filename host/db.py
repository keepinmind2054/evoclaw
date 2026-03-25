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

    BUG-DB-01 FIX (HIGH): protect the global _db assignment with _db_lock.
    Without the lock, two threads calling init_database() simultaneously
    (e.g. a test that re-initializes while a background thread is still
    referencing the old connection) can race on the global _db pointer —
    one thread overwrites _db while the other is mid-setup, leaving the
    first connection leaked and the second partially initialized.
    """
    global _db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _db_lock:
        # p28b: Integrity check before opening.  A corrupt SQLite file can cause
        # silent data loss or cryptic errors at query time.  Run a quick
        # integrity_check at startup so operators are warned immediately rather
        # than discovering corruption hours later via cascading failures.
        # The check is performed on a temporary connection so a failed connection
        # attempt (e.g. file is not a valid SQLite DB) is caught and the
        # corruption is surfaced as a CRITICAL log rather than an unhandled
        # OperationalError that crashes the startup.
        if db_path.exists():
            try:
                _check_conn = sqlite3.connect(str(db_path), timeout=5.0)
                _check_result = _check_conn.execute("PRAGMA integrity_check").fetchone()
                _check_conn.close()
                if _check_result and _check_result[0] != "ok":
                    log.critical(
                        "DATABASE CORRUPTION DETECTED at %s — integrity_check returned: %r. "
                        "The database may be partially readable.  "
                        "To recover: stop EvoClaw, rename the file to messages.db.corrupt, "
                        "restart (a fresh DB will be created), then manually restore data "
                        "from a backup if available.  "
                        "Continuing startup with the existing file — expect potential errors.",
                        db_path, _check_result[0],
                    )
            except sqlite3.DatabaseError as _ic_exc:
                log.critical(
                    "DATABASE CORRUPTION DETECTED at %s — could not open file: %s. "
                    "Rename the file to messages.db.corrupt and restart to allow EvoClaw "
                    "to create a fresh database.  Data will be lost unless a backup exists.",
                    db_path, _ic_exc,
                )
                # Do not raise — allow startup to continue; a new connection attempt below
                # will either succeed (file is an unusual but valid SQLite format) or fail
                # with a clear OperationalError rather than a silent corruption.

        new_conn = sqlite3.connect(str(db_path), check_same_thread=False)
        new_conn.row_factory = sqlite3.Row
        new_conn.execute("PRAGMA journal_mode=WAL")
        new_conn.execute("PRAGMA synchronous=NORMAL")
        new_conn.execute("PRAGMA busy_timeout=5000")  # 5s retry on SQLITE_BUSY
        # Enable foreign key enforcement (Issue #64).
        # SQLite disables FK constraints by default; without this pragma, any schema
        # additions using ON DELETE CASCADE / ON DELETE RESTRICT are silently ignored,
        # producing orphaned rows that skew metrics and fill the database.
        new_conn.execute("PRAGMA foreign_keys = ON")
        _create_tables(new_conn)
        # Close the previous connection (if any) before replacing it so we
        # do not leak the file descriptor on re-initialization.
        if _db is not None:
            try:
                _db.close()
            except Exception:
                pass
        _db = new_conn
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
    -- ── Schema version tracking ───────────────────────────────────────────────
    -- schema_migrations tracks which migrations have been applied so that
    -- run_migrations.py can detect the current schema version and apply only
    -- new migrations.  Without this table every restart blindly re-runs
    -- init_database() with no record of what was already applied, making
    -- incremental migrations impossible.
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version     INTEGER PRIMARY KEY,
        applied_at  TEXT NOT NULL DEFAULT (datetime('now')),
        description TEXT NOT NULL DEFAULT ''
    );

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

    -- BUG-19C-06 FIX: status column is NOT NULL with DEFAULT 'unknown'.
    --   The previous definition allowed NULL in status.  The scheduler marks runs as
    --   'success', 'error', or 'timeout' — a NULL status is meaningless and causes
    --   the consecutive-failure counter in task_scheduler.py to skip the row (it
    --   checks for "error"/"timeout" explicitly), masking repeated silent failures.
    CREATE TABLE IF NOT EXISTS task_run_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        run_at INTEGER NOT NULL,
        duration_ms INTEGER,
        status TEXT NOT NULL DEFAULT 'unknown',
        result TEXT,
        error TEXT
    );
    -- Index for dashboard queries: "show all runs for task X" scans by task_id.
    -- Without this index those queries perform a full table scan of task_run_logs.
    CREATE INDEX IF NOT EXISTS idx_task_run_logs_task_id ON task_run_logs(task_id);
    -- Index for get_due_tasks(): filters on status + next_run on every scheduler tick.
    -- Without this index, each tick performs a full table scan of scheduled_tasks.
    CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_status_next_run ON scheduled_tasks(status, next_run);

    CREATE TABLE IF NOT EXISTS router_state (
        key TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS sessions (
        group_folder TEXT PRIMARY KEY,
        session_id TEXT NOT NULL
    );

    -- BUG-19C-03 FIX: added UNIQUE(folder) to registered_groups.
    --   Each group maps to a unique filesystem folder (groups/<folder>/).  If two
    --   groups shared the same folder they would read/write each other's MEMORY.md,
    --   CLAUDE.md, and skill files, causing silent data corruption and security
    --   cross-contamination.  The UNIQUE constraint prevents this at the DB level.
    CREATE TABLE IF NOT EXISTS registered_groups (
        jid TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        folder TEXT NOT NULL UNIQUE,
        trigger_pattern TEXT,
        added_at INTEGER NOT NULL,
        container_config TEXT,
        requires_trigger INTEGER NOT NULL DEFAULT 1,
        is_main INTEGER NOT NULL DEFAULT 0
    );

    -- ── Container Logs 資料表 ──────────────────────────────────────────────────
    -- container_logs：記錄每次 container 執行的 stderr/stdout，供 Dashboard 查看
    --
    -- BUG-19C-04 FIX: added UNIQUE(run_id) to container_logs.
    --   run_id is a UUID generated once per container invocation in container_runner
    --   and passed to both log_container_start() and log_container_finish().  Without
    --   a UNIQUE constraint a crash-and-retry path could create two "start" rows for
    --   the same run_id, making log_container_finish()'s UPDATE match multiple rows
    --   and leaving ghost "running" rows that trigger false stuck-container alerts.
    CREATE TABLE IF NOT EXISTS container_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id      TEXT NOT NULL UNIQUE,
        jid         TEXT NOT NULL,
        folder      TEXT NOT NULL DEFAULT '',
        container_name TEXT NOT NULL DEFAULT '',
        started_at  REAL NOT NULL,
        finished_at REAL,
        status      TEXT NOT NULL DEFAULT 'running',
        stderr      TEXT,
        stdout_preview TEXT,
        response_ms INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_container_logs_jid ON container_logs(jid, started_at DESC);
    CREATE INDEX IF NOT EXISTS idx_container_logs_run_id ON container_logs(run_id);
    -- Index for get_container_logs(status=...) filter which otherwise performs
    -- a full table scan when filtering by status (e.g. "running" to find stuck containers).
    CREATE INDEX IF NOT EXISTS idx_container_logs_status ON container_logs(status);

    -- ── 演化引擎資料表 ──────────────────────────────────────────────────────
    -- evolution_runs：記錄每次 container 執行的效能數據，是適應度計算的原始資料
    --
    -- BUG-19C-01 FIX: success DEFAULT changed from 1 → 0.
    --   The original DEFAULT 1 meant that any row inserted without an explicit
    --   success value was treated as a successful run, silently inflating fitness
    --   scores.  A missing/unknown success flag is ambiguous and must not be
    --   assumed successful; DEFAULT 0 (fail-safe) matches the compute_fitness()
    --   fix already applied in host/evolution/fitness.py.
    --
    -- BUG-19C-02 FIX: added UNIQUE(jid, run_id).
    --   Without this constraint the same container run_id could be inserted more
    --   than once (e.g. a retry that re-calls record_evolution_run()), producing
    --   duplicate rows that double-count a single execution in success_rate and
    --   speed_score calculations.  The constraint lets callers use INSERT OR IGNORE
    --   to remain idempotent without explicit duplicate checks.
    CREATE TABLE IF NOT EXISTS evolution_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        jid TEXT NOT NULL,
        run_id TEXT NOT NULL,
        response_ms INTEGER,
        retry_count INTEGER NOT NULL DEFAULT 0,
        success INTEGER NOT NULL DEFAULT 0,
        timestamp TEXT DEFAULT (datetime('now')),
        UNIQUE(jid, run_id)
    );
    CREATE INDEX IF NOT EXISTS idx_evolution_jid_ts ON evolution_runs(jid, timestamp);

    -- group_genome：每個群組的行為基因組，記錄演化出的回應風格偏好
    --
    -- BUG-19C-05 FIX: added CHECK constraints on formality and technical_depth.
    --   Both fields are documented as 0.0–1.0 floats.  Without CHECK constraints
    --   a corrupt DB write or a future code regression can store out-of-range values
    --   (e.g. 1.2 or -0.1) that silently produce nonsensical prompt generation and
    --   break the genome evolution math which assumes values in [0, 1].
    CREATE TABLE IF NOT EXISTS group_genome (
        jid TEXT PRIMARY KEY,
        response_style TEXT NOT NULL DEFAULT 'balanced',
        formality REAL NOT NULL DEFAULT 0.5 CHECK(formality >= 0.0 AND formality <= 1.0),
        technical_depth REAL NOT NULL DEFAULT 0.5 CHECK(technical_depth >= 0.0 AND technical_depth <= 1.0),
        generation INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- immune_threats：免疫系統的威脅記錄，形成「抗體」記憶防止重複攻擊
    -- UNIQUE(sender_jid, pattern_hash) enforces the one-row-per-sender-per-pattern
    -- invariant at the DB level so that record_immune_threat() can use a safe
    -- INSERT … ON CONFLICT DO UPDATE instead of a racy SELECT-then-INSERT/UPDATE.
    -- Without this constraint duplicate rows could accumulate on migration or if the
    -- application-level lock were ever bypassed.
    CREATE TABLE IF NOT EXISTS immune_threats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_jid TEXT NOT NULL,
        pattern_hash TEXT NOT NULL,
        threat_type TEXT NOT NULL,
        count INTEGER DEFAULT 1,
        blocked INTEGER DEFAULT 0,
        first_seen TEXT DEFAULT (datetime('now')),
        last_seen TEXT DEFAULT (datetime('now')),
        UNIQUE(sender_jid, pattern_hash)
    );
    CREATE INDEX IF NOT EXISTS idx_immune_sender ON immune_threats(sender_jid);
    -- Composite index for the WHERE sender_jid=? AND pattern_hash=? lookup in
    -- record_immune_threat() and get_recent_threat_count().
    CREATE INDEX IF NOT EXISTS idx_immune_sender_hash ON immune_threats(sender_jid, pattern_hash);

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
-- Index for filtering active/pending/complete sessions by status.
-- Without this, "show all active dev sessions" requires a full table scan.
CREATE INDEX IF NOT EXISTS idx_dev_sessions_status ON dev_sessions(status);

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

-- ── 三層記憶系統資料表 ──────────────────────────────────────────────────────

-- Hot memory: per-group persistent core memory (8KB limit)
CREATE TABLE IF NOT EXISTS group_hot_memory (
    jid TEXT PRIMARY KEY,
    content TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0
);

-- Warm memory: daily log entries
CREATE TABLE IF NOT EXISTS group_warm_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jid TEXT NOT NULL,
    log_date TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_warm_logs_jid_date ON group_warm_logs(jid, log_date);

-- Warm memory FTS5 for hybrid search
CREATE VIRTUAL TABLE IF NOT EXISTS group_warm_logs_fts USING fts5(
    jid UNINDEXED,
    log_date,
    content,
    content='group_warm_logs',
    content_rowid='id'
);
-- BUG-19C-12 FIX: add DELETE trigger for group_warm_logs_fts.
--   append_warm_log() manually inserts into the FTS table after each INSERT.
--   However delete_warm_logs_before() must also remove FTS rows to prevent
--   stale index entries that return deleted content in memory_fts_search().
--   The trigger provides a DB-level safety net for any delete path that forgets
--   to clean the FTS index (e.g. direct SQL deletes during pruning).
CREATE TRIGGER IF NOT EXISTS warm_logs_ad AFTER DELETE ON group_warm_logs BEGIN
    INSERT INTO group_warm_logs_fts(group_warm_logs_fts, rowid, jid, log_date, content)
        VALUES('delete', old.id, old.jid, old.log_date, old.content);
END;

-- Cold memory: longer-form archives
CREATE TABLE IF NOT EXISTS group_cold_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jid TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS group_cold_memory_fts USING fts5(
    jid UNINDEXED,
    title,
    content,
    tags,
    content='group_cold_memory',
    content_rowid='id'
);
-- BUG-19C-12 (continued): add INSERT/DELETE triggers for group_cold_memory_fts
--   so cold memory entries are automatically indexed on insert and removed from
--   the FTS index on delete.  Without these triggers cold memory is written but
--   never appears in memory_fts_search() results — a silent read-gap bug.
CREATE TRIGGER IF NOT EXISTS cold_memory_ai AFTER INSERT ON group_cold_memory BEGIN
    INSERT INTO group_cold_memory_fts(rowid, jid, title, content, tags)
        VALUES(new.id, new.jid, new.title, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS cold_memory_ad AFTER DELETE ON group_cold_memory BEGIN
    INSERT INTO group_cold_memory_fts(group_cold_memory_fts, rowid, jid, title, content, tags)
        VALUES('delete', old.id, old.jid, old.title, old.content, old.tags);
END;

-- Memory sync tracking
CREATE TABLE IF NOT EXISTS group_memory_sync (
    jid TEXT PRIMARY KEY,
    last_micro_sync REAL NOT NULL DEFAULT 0,
    last_daily_wrapup REAL NOT NULL DEFAULT 0,
    last_weekly_compound REAL NOT NULL DEFAULT 0
);
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
        try:
            db.execute("""
                INSERT OR IGNORE INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_bot_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (msg_id, chat_jid, sender, sender_name, content, timestamp, int(is_from_me), int(is_bot_message)))
            db.commit()
        except Exception:
            db.rollback()
            raise

def get_new_messages(jids: list[str], last_timestamp: int) -> list[dict]:
    """
    取得所有指定 JID 群組中、時間戳記比 last_timestamp 更新的訊息。

    這是「時間戳記游標」（timestamp cursor）模式的核心查詢：
    - 每次 message loop 執行後，last_timestamp 會推進到已處理訊息的最大時間戳記
    - 下次查詢只取比這個值更新的訊息，自然實現「不重複、不遺漏」的效果
    - 過濾 is_from_me=0 和 is_bot_message=0，只處理用戶發送的真實訊息

    使用 IN (?) 而非逐一查詢，減少資料庫 round-trip 次數。
    _db_lock ensures thread-safety when called concurrently from the message
    loop and background threads (dashboard, webportal, evolution daemon).
    """
    if not jids:
        return []
    with _db_lock:
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
    _db_lock ensures thread-safety when called concurrently from the message
    loop and background threads (dashboard, webportal, evolution daemon).
    """
    with _db_lock:
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
    """取得某個聊天室從指定時間點起的訊息（含所有類型），用於提供對話歷史給 agent。
    _db_lock ensures thread-safety when called from container_runner (asyncio loop)
    while dashboard/webportal/evolution daemon threads may hold the lock (Issue #60)."""
    with _db_lock:
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
        try:
            db.execute("""
                INSERT INTO chats (jid, name, last_message_time, channel, is_group)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(jid) DO UPDATE SET name=excluded.name, last_message_time=excluded.last_message_time
            """, (jid, name, timestamp, channel, int(is_group)))
            db.commit()
        except Exception:
            db.rollback()
            raise

# ── Router state ──────────────────────────────────────────────────────────────

def get_state(key: str) -> Optional[str]:
    """從 router_state 表讀取通用 key-value 狀態（目前主要用於 lastTimestamp）。
    _db_lock ensures thread-safety when called concurrently from background threads (Issue #60)."""
    with _db_lock:
        db = get_db()
        row = db.execute("SELECT value FROM router_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

def set_state(key: str, value: str) -> None:
    """寫入或更新 router_state 表中的 key-value 狀態。"""
    with _db_lock:
        db = get_db()
        try:
            db.execute("INSERT OR REPLACE INTO router_state (key, value) VALUES (?, ?)", (key, value))
            db.commit()
        except Exception:
            db.rollback()
            raise

# ── Sessions ──────────────────────────────────────────────────────────────────

def get_session(group_folder: str) -> Optional[str]:
    """取得某群組的 Claude session ID，讓 agent 延續上次的對話記憶。
    _db_lock ensures thread-safety when called from task_scheduler/container_runner
    while dashboard/webportal threads may hold the lock (Issue #60)."""
    with _db_lock:
        db = get_db()
        row = db.execute("SELECT session_id FROM sessions WHERE group_folder=?", (group_folder,)).fetchone()
        return row["session_id"] if row else None

def set_session(group_folder: str, session_id: str) -> None:
    """儲存或更新某群組的 Claude session ID（container 每次執行後可能產生新 session）。"""
    with _db_lock:
        db = get_db()
        try:
            db.execute("INSERT OR REPLACE INTO sessions (group_folder, session_id) VALUES (?, ?)", (group_folder, session_id))
            db.commit()
        except Exception:
            db.rollback()
            raise

# ── Registered groups ─────────────────────────────────────────────────────────

def get_all_registered_groups() -> list[dict]:
    """取得所有已登記群組的完整設定清單，啟動時載入到記憶體中。
    _db_lock is held to ensure thread-safety when called from background threads
    (dashboard, webportal, evolution daemon via asyncio.to_thread)."""
    with _db_lock:
        db = get_db()
        rows = db.execute("SELECT * FROM registered_groups").fetchall()
        return [dict(r) for r in rows]

def get_registered_group(jid: str) -> Optional[dict]:
    """根據 JID 查找單一群組設定，找不到時回傳 None。
    _db_lock ensures thread-safety when called from background threads (Issue #60)."""
    with _db_lock:
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
    with _db_lock:
        db = get_db()
        try:
            if is_main:
                # Enforce single main group invariant — demote all other groups
                db.execute("UPDATE registered_groups SET is_main = 0 WHERE jid != ?", (jid,))
            # Use INSERT … ON CONFLICT DO UPDATE instead of INSERT OR REPLACE so
            # that the original added_at timestamp is preserved when updating an
            # existing group.  INSERT OR REPLACE silently DELETEs + re-INSERTs the
            # row, which always overwrites added_at with the current time and loses
            # the original registration timestamp.
            db.execute("""
                INSERT INTO registered_groups
                (jid, name, folder, trigger_pattern, added_at, container_config, requires_trigger, is_main)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(jid) DO UPDATE SET
                    name              = excluded.name,
                    folder            = excluded.folder,
                    trigger_pattern   = excluded.trigger_pattern,
                    container_config  = excluded.container_config,
                    requires_trigger  = excluded.requires_trigger,
                    is_main           = excluded.is_main
            """, (jid, name, folder, trigger_pattern, int(time.time() * 1000),
                  json.dumps(container_config) if container_config else None,
                  int(requires_trigger), int(is_main)))
            db.commit()
        except Exception:
            db.rollback()
            raise

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
    with _db_lock:
        db = get_db()
        try:
            db.execute("""
                INSERT INTO scheduled_tasks
                (id, group_folder, chat_jid, prompt, schedule_type, schedule_value, next_run, created_at, context_mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (task_id, group_folder, chat_jid, prompt, schedule_type, schedule_value,
                  next_run, int(time.time() * 1000), context_mode))
            db.commit()
        except Exception:
            db.rollback()
            raise

def get_all_tasks(group_folder: Optional[str] = None) -> list[dict]:
    """取得所有排程任務（可選擇性地過濾特定群組）。
    _db_lock ensures thread-safety when called from dashboard/webportal threads."""
    with _db_lock:
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
    _db_lock ensures thread-safety when called from the scheduler loop.
    """
    with _db_lock:
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
    allowed = {"prompt", "schedule_type", "schedule_value", "next_run", "last_run", "last_result", "status", "context_mode"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    # p22d-A: assert all column names come from the whitelist before building the
    # dynamic SET clause.  fields is already filtered; the assertion makes the
    # invariant explicit and will catch any future refactor that bypasses it.
    assert all(k in allowed for k in fields), f"Unexpected column in fields: {set(fields) - allowed}"
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with _db_lock:
        db = get_db()
        try:
            db.execute(f"UPDATE scheduled_tasks SET {set_clause} WHERE id=?", (*fields.values(), task_id))
            db.commit()
        except Exception:
            db.rollback()
            raise

def delete_task(task_id: str) -> None:
    """永久刪除排程任務（cancel 操作）。"""
    with _db_lock:
        db = get_db()
        try:
            db.execute("DELETE FROM scheduled_tasks WHERE id=?", (task_id,))
            db.commit()
        except Exception:
            db.rollback()
            raise

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
        try:
            db.execute("""
                INSERT INTO task_run_logs (task_id, run_at, duration_ms, status, result, error)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (task_id, run_at, duration_ms, status, result, error))
            db.commit()
        except Exception:
            db.rollback()
            raise


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
        try:
            # INSERT OR IGNORE: the UNIQUE(jid, run_id) constraint added in
            # BUG-19C-02 prevents duplicate rows for the same run.  Using
            # OR IGNORE rather than OR REPLACE preserves the original timestamp
            # and avoids inflating retry metrics on duplicate calls.
            db.execute("""
                INSERT OR IGNORE INTO evolution_runs (jid, run_id, response_ms, retry_count, success)
                VALUES (?, ?, ?, ?, ?)
            """, (jid, run_id, response_ms, retry_count, int(success)))
            db.commit()
        except Exception:
            db.rollback()
            raise


def get_evolution_runs(jid: str, days: int = 7) -> list[dict]:
    """
    取得指定群組在過去 days 天內的所有執行記錄，用於計算適應度分數。

    按時間倒序排列（最新的在前），最多取 200 筆避免計算過慢。
    _db_lock ensures thread-safety (called from evolution daemon via asyncio.to_thread).
    """
    with _db_lock:
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
    取得需要演化評估的所有群組 JID 清單。

    包含兩類群組：
    1. 在過去 days 天內有 evolution_runs 記錄的群組
    2. 冷啟動群組：有對話歷史但尚無 evolution_runs 的群組（確保新群組也能被評估）

    若只查 evolution_runs，剛啟動的系統（表為空）永遠回傳空列表，
    導致 Evolution daemon 顯示「Evaluating 0 group(s)」並跳過所有群組。
    _db_lock ensures thread-safety (called from evolution daemon via asyncio.to_thread).
    """
    with _db_lock:
        db = get_db()
        # Groups with recent evolution run records
        rows = db.execute("""
            SELECT DISTINCT jid FROM evolution_runs
            WHERE timestamp > datetime('now', ? || ' days')
        """, (f"-{days}",)).fetchall()
        jids = {r["jid"] for r in rows}

        # Cold-start bootstrap: also include groups that have conversation history
        # but no evolution_runs yet, so the daemon can seed their first genome.
        history_rows = db.execute("""
            SELECT DISTINCT chat_jid FROM messages
            WHERE timestamp > ? AND is_bot_message = 0
        """, (int((time.time() - days * 86400) * 1000),)).fetchall()
        for r in history_rows:
            jids.add(r["chat_jid"])

        return sorted(jids)


def get_recent_run_stats(minutes: int = 5) -> Optional[dict]:
    """
    取得近 minutes 分鐘內的執行統計（數量與平均回應時間）。
    用於 get_system_load() 估算當前系統負載。
    _db_lock ensures thread-safety when called from dashboard/webportal threads.
    """
    with _db_lock:
        db = get_db()
        row = db.execute("""
            SELECT COUNT(*) as count, AVG(response_ms) as avg_ms
            FROM evolution_runs
            WHERE timestamp > datetime('now', ? || ' minutes')
        """, (f"-{minutes}",)).fetchone()
        return dict(row) if row else None


def get_group_genome(jid: str) -> Optional[dict]:
    """取得指定群組的行為基因組，找不到時回傳 None（表示尚未演化過）。
    _db_lock ensures thread-safety (called from evolution daemon via asyncio.to_thread)."""
    with _db_lock:
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
    allowed = {"response_style", "formality", "technical_depth", "generation"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}

    with _db_lock:
        db = get_db()
        try:
            # 先確保記錄存在（INSERT OR IGNORE 建立預設值）
            db.execute("""
                INSERT OR IGNORE INTO group_genome (jid) VALUES (?)
            """, (jid,))

            if fields:
                # p22d-A: assert all column names come from the whitelist before
                # building the dynamic SET clause.
                assert all(k in allowed for k in fields), f"Unexpected column in fields: {set(fields) - allowed}"
                # updated_at 用 SQL 函式，需特殊處理
                set_parts = []
                values = []
                for k, v in fields.items():
                    set_parts.append(f"{k} = ?")
                    values.append(v)
                set_parts.append("updated_at = datetime('now')")
                set_clause = ", ".join(set_parts)
                db.execute(f"UPDATE group_genome SET {set_clause} WHERE jid = ?",
                           (*values, jid))
            db.commit()
        except Exception:
            db.rollback()
            raise


def record_immune_threat(sender_jid: str, pattern_hash: str, threat_type: str) -> int:
    """
    記錄一次免疫威脅事件，回傳該發送者的累計威脅次數。

    若相同的 (sender_jid, pattern_hash) 組合已存在，則更新計數和最後出現時間，
    避免重複記錄膨脹資料庫。

    回傳累計次數供呼叫方判斷是否需要自動封鎖。

    NOTE: _db_lock is held for the full read-modify-write sequence to prevent
    a TOCTOU race condition between the SELECT and the UPDATE/INSERT under
    concurrent access from dashboard/webportal threads.
    """
    with _db_lock:
        db = get_db()
        try:
            # Atomic upsert: the UNIQUE(sender_jid, pattern_hash) constraint
            # allows a single statement to either insert a new threat or increment
            # the counter on an existing one, eliminating the previous racy
            # SELECT-then-INSERT/UPDATE pattern.  The lock is still held to keep
            # the follow-up SUM query consistent with the write.
            db.execute("""
                INSERT INTO immune_threats (sender_jid, pattern_hash, threat_type, count,
                                           first_seen, last_seen)
                VALUES (?, ?, ?, 1, datetime('now'), datetime('now'))
                ON CONFLICT(sender_jid, pattern_hash) DO UPDATE SET
                    count    = count + 1,
                    last_seen = datetime('now')
            """, (sender_jid, pattern_hash, threat_type))
            db.commit()
        except Exception:
            db.rollback()
            raise

        # 回傳此發送者的所有威脅記錄總數（跨不同 pattern）
        total = db.execute("""
            SELECT SUM(count) as total FROM immune_threats WHERE sender_jid = ?
        """, (sender_jid,)).fetchone()
        return int(total["total"]) if total and total["total"] is not None else 1


def is_sender_blocked(sender_jid: str) -> bool:
    """
    檢查指定發送者是否已被免疫系統封鎖。
    被封鎖的發送者的任何訊息都會被靜默丟棄。
    _db_lock ensures thread-safety when called from immune check in async context.
    """
    with _db_lock:
        db = get_db()
        row = db.execute("""
            SELECT 1 FROM immune_threats WHERE sender_jid = ? AND blocked = 1 LIMIT 1
        """, (sender_jid,)).fetchone()
        return row is not None


def block_sender(sender_jid: str) -> None:
    """將指定發送者的所有威脅記錄標記為已封鎖（blocked = 1）。"""
    with _db_lock:
        db = get_db()
        try:
            db.execute("UPDATE immune_threats SET blocked = 1 WHERE sender_jid = ?", (sender_jid,))
            db.commit()
        except Exception:
            db.rollback()
            raise


def get_recent_threat_count(sender_jid: str, pattern_hash: str, hours: int = 1) -> int:
    """
    取得某個發送者在近 hours 小時內傳送特定 hash 訊息的次數。
    用於垃圾訊息偵測（同內容重複發送判定）。
    _db_lock ensures thread-safety when called from immune check in async context.
    """
    with _db_lock:
        db = get_db()
        row = db.execute("""
            SELECT count FROM immune_threats
            WHERE sender_jid = ? AND pattern_hash = ?
            AND last_seen > datetime('now', ? || ' hours')
        """, (sender_jid, pattern_hash, f"-{hours}")).fetchone()
        return row["count"] if row else 0


def get_immune_stats() -> dict:
    """取得免疫系統的摘要統計，用於監控面板或 IPC 查詢。
    _db_lock ensures thread-safety when called from dashboard/webportal threads."""
    with _db_lock:
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
    allowed = {"generation_before", "generation_after", "fitness_score",
               "avg_response_ms", "genome_before", "genome_after", "notes"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    # p22d-A: assert that only whitelisted column names reach the f-string.
    # fields.keys() is already filtered above; this assertion makes it explicit
    # so future refactors cannot accidentally bypass the whitelist.
    assert all(k in allowed for k in fields), f"Unexpected column in fields: {set(fields) - allowed}"
    # genome dicts → JSON strings
    for key in ("genome_before", "genome_after"):
        if key in fields and isinstance(fields[key], dict):
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    cols = ", ".join(["jid", "event_type"] + list(fields.keys()))
    placeholders = ", ".join(["?"] * (2 + len(fields)))
    values = [jid, event_type] + list(fields.values())
    with _db_lock:
        db = get_db()
        try:
            db.execute(f"INSERT INTO evolution_log ({cols}) VALUES ({placeholders})", values)
            db.commit()
        except Exception:
            db.rollback()
            raise


def get_evolution_log(jid: str = None, limit: int = 100, event_type: str = None) -> list:
    """
    查詢演化歷程日誌。

    參數：
      jid        — 指定群組（None = 所有群組）
      limit      — 最多回傳幾筆（預設 100）
      event_type — 過濾特定事件類型（None = 全部）

    _db_lock ensures thread-safety when called from dashboard/webportal threads.
    """
    with _db_lock:
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
    with _db_lock:
        db = get_db()
        try:
            db.execute("""
                INSERT INTO dev_events (jid, event_type, stage, notes)
                VALUES (?, ?, ?, ?)
            """, (jid, event_type, stage, notes))
            db.commit()
        except Exception:
            db.rollback()
            raise

def get_dev_events(jid: str = None, limit: int = 100, stage: str = None) -> list:
    """
    查詢開發事件日誌。
    參數：
    jid — 指定開發者/群組（None = 所有）
    limit — 最多回傳幾筆（預設 100）
    stage — 過濾特定階段（None = 全部）
    _db_lock ensures thread-safety when called from dashboard/webportal threads (Issue #60).
    """
    with _db_lock:
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


# ── Hot Memory ──────────────────────────────────────────────────────────────

def get_hot_memory(jid: str) -> str:
    with _db_lock:
        db = get_db()
        row = db.execute(
            "SELECT content FROM group_hot_memory WHERE jid = ?", (jid,)
        ).fetchone()
    return row[0] if row else ""


def set_hot_memory(jid: str, content: str) -> None:
    import time as _time
    with _db_lock:
        db = get_db()
        try:
            db.execute(
                """INSERT INTO group_hot_memory(jid, content, updated_at)
                   VALUES(?, ?, ?)
                   ON CONFLICT(jid) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at""",
                (jid, content, _time.time()),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise


# ── Warm Memory ─────────────────────────────────────────────────────────────

def append_warm_log(jid: str, log_date: str, content: str) -> None:
    import time as _time
    with _db_lock:
        db = get_db()
        try:
            db.execute(
                "INSERT INTO group_warm_logs(jid, log_date, content, created_at) VALUES(?, ?, ?, ?)",
                (jid, log_date, content, _time.time()),
            )
            # Keep FTS in sync — must be in the same transaction so a crash
            # between the two INSERTs cannot leave the FTS index out of sync.
            db.execute("INSERT INTO group_warm_logs_fts(rowid, jid, log_date, content) VALUES(last_insert_rowid(), ?, ?, ?)",
                        (jid, log_date, content))
            db.commit()
        except Exception:
            db.rollback()
            raise


def get_warm_logs_recent(jid: str, days: int = 1) -> list[dict]:
    import time as _time
    cutoff = _time.time() - days * 86400
    with _db_lock:
        db = get_db()
        rows = db.execute(
            "SELECT id, log_date, content, created_at FROM group_warm_logs WHERE jid=? AND created_at>=? ORDER BY created_at DESC",
            (jid, cutoff),
        ).fetchall()
    return [{"id": r[0], "log_date": r[1], "content": r[2], "created_at": r[3]} for r in rows]


def get_warm_logs_for_date(jid: str, log_date: str) -> list[dict]:
    """Return all warm log entries for a specific date (YYYY-MM-DD).

    Added for p14b-4: daily wrapup needs yesterday's logs specifically;
    ``get_warm_logs_recent(days=1)`` returns the last 24 h of wall-clock
    time which is almost empty when called at midnight.
    """
    with _db_lock:
        db = get_db()
        rows = db.execute(
            "SELECT id, log_date, content, created_at FROM group_warm_logs "
            "WHERE jid=? AND log_date=? ORDER BY created_at ASC",
            (jid, log_date),
        ).fetchall()
    return [{"id": r[0], "log_date": r[1], "content": r[2], "created_at": r[3]} for r in rows]


def delete_warm_logs_before(jid: str, cutoff_ts: float) -> int:
    with _db_lock:
        db = get_db()
        try:
            # Delete matching FTS rows first (before the source rows disappear).
            # Both deletes must be in the same transaction — a crash between them
            # would leave orphaned FTS entries causing stale search results.
            db.execute(
                "DELETE FROM group_warm_logs_fts WHERE rowid IN "
                "(SELECT id FROM group_warm_logs WHERE jid=? AND created_at<?)",
                (jid, cutoff_ts),
            )
            cur = db.execute(
                "DELETE FROM group_warm_logs WHERE jid=? AND created_at<?", (jid, cutoff_ts)
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        return cur.rowcount


def append_cold_memory(jid: str, title: str, content: str, tags: str = "") -> int:
    """Insert a long-form archive entry into group_cold_memory.

    BUG-19C-09 FIX: group_cold_memory had a schema, FTS table, and read path
    (memory_fts_search) but no write function in db.py.  Any code that stored
    cold memories was using raw SQL bypassing the DB lock and FTS sync.  This
    function provides the canonical write path.

    The FTS index is kept in sync via the cold_memory_ai trigger added in the
    schema fix (BUG-19C-12), so no manual FTS INSERT is required here.

    Returns the rowid of the inserted row.
    """
    import time as _time
    with _db_lock:
        db = get_db()
        try:
            cur = db.execute(
                "INSERT INTO group_cold_memory(jid, title, content, tags, created_at) VALUES(?, ?, ?, ?, ?)",
                (jid, title, content, tags, _time.time()),
            )
            db.commit()
            return cur.lastrowid
        except Exception:
            db.rollback()
            raise


def delete_cold_memory_before(jid: str, cutoff_ts: float) -> int:
    """Delete old cold memory entries for a group.

    The FTS index is kept in sync via the cold_memory_ad trigger added in the
    schema fix (BUG-19C-12).
    """
    with _db_lock:
        db = get_db()
        try:
            cur = db.execute(
                "DELETE FROM group_cold_memory WHERE jid=? AND created_at<?",
                (jid, cutoff_ts),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        return cur.rowcount


def memory_fts_search(jid: str, query: str, limit: int = 10) -> list[dict]:
    """Hybrid search across warm and cold memory using FTS5.

    Searches both group_warm_logs_fts and group_cold_memory_fts and returns
    up to *limit* combined results sorted by relevance (best match first).
    Previously only warm memory was searched; this was a silent data-loss bug
    that caused agents to miss relevant cold-memory archives.
    """
    results = []
    with _db_lock:
        db = get_db()
        # ── Warm memory FTS search ────────────────────────────────────────────
        try:
            rows = db.execute(
                """SELECT w.id, w.log_date, w.content, w.created_at,
                          bm25(group_warm_logs_fts) as fts_score
                   FROM group_warm_logs_fts f
                   JOIN group_warm_logs w ON w.id = f.rowid
                   WHERE f.jid=? AND group_warm_logs_fts MATCH ?
                   ORDER BY fts_score
                   LIMIT ?""",
                (jid, query, limit),
            ).fetchall()
            for r in rows:
                results.append({
                    "source": "warm",
                    "date": r[1],
                    "content": r[2][:500],
                    "created_at": r[3],
                    "fts_score": abs(r[4]) if r[4] else 0.0,
                })
        except Exception:
            pass

        # ── Cold memory FTS search ────────────────────────────────────────────
        # The docstring promised hybrid search but cold memory was never queried.
        # Added here to fulfil the contract and prevent agents from missing
        # long-term archived information stored in group_cold_memory.
        try:
            rows = db.execute(
                """SELECT c.id, c.title, c.content, c.tags, c.created_at,
                          bm25(group_cold_memory_fts) as fts_score
                   FROM group_cold_memory_fts f
                   JOIN group_cold_memory c ON c.id = f.rowid
                   WHERE f.jid=? AND group_cold_memory_fts MATCH ?
                   ORDER BY fts_score
                   LIMIT ?""",
                (jid, query, limit),
            ).fetchall()
            for r in rows:
                results.append({
                    "source": "cold",
                    "date": r[1],          # title used as date-like label
                    "content": r[2][:500],
                    "tags": r[3],
                    "created_at": r[4],
                    "fts_score": abs(r[5]) if r[5] else 0.0,
                })
        except Exception:
            pass

    # Re-sort combined results by relevance descending.
    # BUG-DB-03 FIX (LOW): clarify sort semantics.  SQLite BM25() returns a
    # negative float (more negative = better match).  abs() is applied above
    # so fts_score is stored as a positive number where higher = better match.
    # reverse=True therefore correctly puts the best matches first.
    results.sort(key=lambda x: x["fts_score"], reverse=True)
    return results[:limit]


def record_micro_sync(jid: str) -> None:
    import time as _time
    with _db_lock:
        db = get_db()
        try:
            db.execute(
                """INSERT INTO group_memory_sync(jid, last_micro_sync)
                   VALUES(?, ?)
                   ON CONFLICT(jid) DO UPDATE SET last_micro_sync=excluded.last_micro_sync""",
                (jid, _time.time()),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise


def record_daily_wrapup(jid: str) -> None:
    """Record the timestamp of the last daily wrapup for a group."""
    import time as _time
    with _db_lock:
        db = get_db()
        try:
            db.execute(
                """INSERT INTO group_memory_sync(jid, last_daily_wrapup)
                   VALUES(?, ?)
                   ON CONFLICT(jid) DO UPDATE SET last_daily_wrapup=excluded.last_daily_wrapup""",
                (jid, _time.time()),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise


def record_weekly_compound(jid: str) -> None:
    import time as _time
    with _db_lock:
        db = get_db()
        try:
            db.execute(
                """INSERT INTO group_memory_sync(jid, last_weekly_compound)
                   VALUES(?, ?)
                   ON CONFLICT(jid) DO UPDATE SET last_weekly_compound=excluded.last_weekly_compound""",
                (jid, _time.time()),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise


# ── Maintenance ────────────────────────────────────────────────────────────────

def get_pending_task_count() -> int:
    """Return the number of active scheduled tasks (status='active') that are overdue.

    Used by health_monitor to gauge scheduler backlog.
    _db_lock ensures thread-safety when called from health_monitor loop.
    """
    with _db_lock:
        db = get_db()
        now_ms = int(time.time() * 1000)
        row = db.execute(
            "SELECT COUNT(*) as cnt FROM scheduled_tasks WHERE status='active' AND next_run IS NOT NULL AND next_run <= ?",
            (now_ms,),
        ).fetchone()
        return row["cnt"] if row else 0


def get_error_stats(minutes: int = 5) -> dict:
    """Return success/error counts from evolution_runs for the last `minutes` minutes.

    Used by health_monitor to calculate recent error rate.
    Returns dict with keys: total, errors, successes.
    _db_lock ensures thread-safety when called from health_monitor loop.
    """
    with _db_lock:
        db = get_db()
        row = db.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successes
            FROM evolution_runs
            WHERE timestamp > datetime('now', ? || ' minutes')
            """,
            (f"-{minutes}",),
        ).fetchone()
        if row and row["total"]:
            return {"total": row["total"], "errors": row["errors"] or 0, "successes": row["successes"] or 0}
        return {"total": 0, "errors": 0, "successes": 0}


def prune_old_logs(days: int = 30) -> None:
    """Delete old rows from append-only log tables to prevent unbounded disk growth.

    Prunes:
    - task_run_logs: rows older than `days` days (keyed by run_at in ms)
    - evolution_runs: rows older than `days` days (keyed by timestamp TEXT)
    - evolution_log: rows older than `days` days (keyed by timestamp TEXT)
    - messages: rows older than `days` days (keyed by timestamp ms integer)
    - immune_threats: rows older than 90 days that have count=1 (noise, not recurring)
    - dev_events: rows older than `days` days (keyed by timestamp TEXT)
    - dev_sessions: rows older than `days` days (keyed by created_at REAL seconds)

    Safe to call at startup or from a periodic maintenance loop.
    On a deployment with 5 groups and per-minute tasks, 30 days retention keeps
    roughly 216,000 task_run_log rows — manageable for SQLite.
    """
    with _db_lock:
        db = get_db()
        try:
            # task_run_logs.run_at is stored as ms epoch integer
            cutoff_ms = int((time.time() - days * 86400) * 1000)
            db.execute("DELETE FROM task_run_logs WHERE run_at < ?", (cutoff_ms,))
            # evolution_runs.timestamp is a TEXT field in SQLite datetime format
            db.execute(
                "DELETE FROM evolution_runs WHERE timestamp < datetime('now', ?)",
                (f"-{days} days",),
            )
            # evolution_log: same TEXT timestamp format
            db.execute(
                "DELETE FROM evolution_log WHERE timestamp < datetime('now', ?)",
                (f"-{days} days",),
            )
            # messages: timestamp is ms epoch integer (same cutoff as task_run_logs)
            db.execute("DELETE FROM messages WHERE timestamp < ?", (cutoff_ms,))
            # immune_threats: keep blocked senders and recurring threats indefinitely;
            # prune one-shot noise entries (count=1) older than 90 days.
            # immune_threats.last_seen is stored as TEXT datetime, so use SQLite datetime()
            # for the comparison.  The 90-day retention is intentionally longer than the
            # configurable `days` parameter to preserve threat history for audit purposes.
            # (Issue #63: removed unused integer immune_cutoff_ms variable)
            db.execute(
                "DELETE FROM immune_threats WHERE count = 1 AND blocked = 0 "
                "AND last_seen < datetime('now', '-90 days')",
            )
            # dev_events: TEXT timestamp
            db.execute(
                "DELETE FROM dev_events WHERE timestamp < datetime('now', ?)",
                (f"-{days} days",),
            )
            # dev_sessions: created_at is stored as REAL (Unix seconds)
            cutoff_secs = time.time() - days * 86400
            db.execute("DELETE FROM dev_sessions WHERE created_at < ?", (cutoff_secs,))
            # container_logs: started_at is stored as REAL (Unix seconds)
            db.execute("DELETE FROM container_logs WHERE started_at < ?", (cutoff_secs,))
            db.commit()
        except Exception:
            db.rollback()
            raise
    log.info(
        "Pruned logs older than %d days "
        "(task_run_logs, evolution_runs, evolution_log, messages, dev_events, dev_sessions, container_logs) "
        "+ immune_threats noise > 90d",
        days,
    )


# ── Container Logs ─────────────────────────────────────────────────────────────

def log_container_start(run_id: str, jid: str, folder: str, container_name: str, started_at: float) -> None:
    """Insert a 'running' row when a container starts.

    Uses INSERT OR IGNORE so that a crash-and-retry that re-calls this function
    with the same run_id does not create a duplicate row (BUG-19C-04 fix).
    """
    with _db_lock:
        db = get_db()
        try:
            db.execute(
                "INSERT OR IGNORE INTO container_logs (run_id, jid, folder, container_name, started_at, status)"
                " VALUES (?, ?, ?, ?, ?, 'running')",
                (run_id, jid, folder, container_name, started_at),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise


def log_container_finish(
    run_id: str,
    finished_at: float,
    status: str,
    stderr: str,
    stdout_preview: str,
    response_ms: int,
) -> None:
    """Update the container_logs row when a container finishes."""
    with _db_lock:
        db = get_db()
        try:
            db.execute(
                "UPDATE container_logs SET finished_at=?, status=?, stderr=?, stdout_preview=?, response_ms=?"
                " WHERE run_id=?",
                (finished_at, status, stderr[:32768] if stderr else "", stdout_preview[:2048] if stdout_preview else "", response_ms, run_id),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise


def get_container_logs(jid: str = "", limit: int = 50, status: str = "") -> list[dict]:
    """Fetch recent container run logs.

    BUG-DB-02 FIX (MEDIUM): guard against limit <= 0.  A caller passing
    limit=0 (e.g. from a misconfigured dashboard query) would silently
    return an empty list with no indication that the parameter was wrong.
    Clamp to at least 1 row so the call always returns something meaningful.
    """
    limit = max(1, limit)
    params: list = []
    where_parts: list[str] = []
    if jid:
        where_parts.append("jid = ?")
        params.append(jid)
    if status:
        where_parts.append("status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    params.append(limit)
    with _db_lock:
        db = get_db()
        rows = db.execute(
            f"SELECT id, run_id, jid, folder, container_name, started_at, finished_at, status, stderr, stdout_preview, response_ms"
            f" FROM container_logs {where} ORDER BY started_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


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
