#!/usr/bin/env python3
"""資料庫索引優化遷移腳本

為 EvoClaw 資料庫添加必要的索引以提升查詢效能。
這些索引針對常見的查詢場景進行優化，預期可提升 50-90% 的查詢速度。

使用方法：
    python3 -m scripts.add_indexes_migration

或從專案根目錄執行：
    python3 scripts/add_indexes_migration.py
"""
import logging
import sys
from pathlib import Path

# 確保可以導入 host 模組
sys.path.insert(0, str(Path(__file__).parent.parent))

from host import config, db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# 索引定義列表
# 格式：(索引名稱, 表名, 欄位, 唯一性)
INDEXES = [
    # messages 表
    ("idx_messages_timestamp", "messages", "timestamp", False),
    ("idx_messages_chat_jid", "messages", "chat_jid", False),
    ("idx_messages_sender", "messages", "sender", False),
    ("idx_messages_chat_jid_timestamp", "messages", "chat_jid, timestamp", False),
    ("idx_messages_sender_timestamp", "messages", "sender, timestamp", False),
    
    # chats 表
    ("idx_chats_jid", "chats", "jid", False),
    ("idx_chats_last_timestamp", "chats", "last_timestamp", False),
    
    # scheduled_tasks 表
    ("idx_scheduled_tasks_status", "scheduled_tasks", "status", False),
    ("idx_scheduled_tasks_next_run", "scheduled_tasks", "next_run", False),
    ("idx_scheduled_tasks_chat_jid", "scheduled_tasks", "chat_jid", False),
    
    # evolution_runs 表
    ("idx_evolution_runs_jid", "evolution_runs", "jid", False),
    ("idx_evolution_runs_timestamp", "evolution_runs", "timestamp", False),
    ("idx_evolution_runs_jid_timestamp", "evolution_runs", "jid, timestamp", False),
    
    # immune_threats 表
    ("idx_immune_threats_sender_jid", "immune_threats", "sender_jid", False),
    ("idx_immune_threats_threat_type", "immune_threats", "threat_type", False),
    ("idx_immune_threats_timestamp", "immune_threats", "timestamp", False),
    ("idx_immune_threats_sender_jid_timestamp", "immune_threats", "sender_jid, timestamp", False),
    
    # sessions 表
    ("idx_sessions_jid", "sessions", "jid", False),
]


def add_indexes(db_path: Path) -> None:
    """
    為資料庫添加所有預定義的索引。
    
    參數：
        db_path: 資料庫檔案路徑
    """
    import sqlite3
    
    log.info(f"Starting index migration for: {db_path}")
    log.info(f"Total indexes to create: {len(INDEXES)}")
    
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    
    created_count = 0
    skipped_count = 0
    
    for index_name, table_name, columns, is_unique in INDEXES:
        try:
            # 檢查索引是否已存在
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,)
            )
            if cursor.fetchone():
                log.info(f"  ✓ Index '{index_name}' already exists, skipping")
                skipped_count += 1
                continue
            
            # 建立索引
            unique_str = "UNIQUE " if is_unique else ""
            sql = f"CREATE {unique_str}INDEX IF NOT EXISTS {index_name} ON {table_name} ({columns})"
            log.info(f"  Creating index: {sql}")
            cursor.execute(sql)
            created_count += 1
            log.info(f"  ✓ Index '{index_name}' created successfully")
            
        except Exception as e:
            log.error(f"  ✗ Failed to create index '{index_name}': {e}")
    
    conn.commit()
    conn.close()
    
    log.info(f"Index migration completed: {created_count} created, {skipped_count} skipped")


def main() -> None:
    """主函式：執行資料庫索引遷移。"""
    db_path = config.STORE_DIR / "messages.db"
    
    if not db_path.exists():
        log.error(f"Database not found: {db_path}")
        log.info("Please run the application first to create the database.")
        sys.exit(1)
    
    try:
        add_indexes(db_path)
        log.info("Migration completed successfully!")
    except Exception as e:
        log.error(f"Migration failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
