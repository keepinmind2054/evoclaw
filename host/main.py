#!/usr/bin/env python3
"""
EvoClaw Host — Main Entry Point
Orchestrates message polling, container execution, IPC, and scheduling.
"""
import asyncio
import logging
import signal
import time
import uuid
from pathlib import Path

from . import config, db
from .allowlist import load_sender_allowlist, is_sender_allowed
from .dashboard import start_dashboard
from .container_runner import run_container_agent, cleanup_orphans
from .group_queue import GroupQueue
from .ipc_watcher import start_ipc_watcher
from .task_scheduler import start_scheduler_loop
from .router import register_channel, route_outbound, format_messages, find_channel
from .evolution import check_message as immune_check, evolution_loop
from .health_monitor import health_monitor_loop

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("evoclaw")

# ── State ─────────────────────────────────────────────────────────────────────

# 目前已登記的群組清單，每個 dict 包含 jid、folder、is_main 等欄位
_registered_groups: list[dict] = []

# 訊息時間戳記游標：只處理比這個值更新的訊息，避免重複處理
# 這個值會持久化到 SQLite，重啟後可從上次中斷的地方繼續
_last_timestamp: int = 0

# 全域開關：設為 False 時，所有背景 loop 都會停止
_running = True

# 共用的停止事件：shutdown 時 set()，讓所有等待中的 sleep 立即醒來
_stop_event: asyncio.Event | None = None

# 允許傳送訊息的發送者白名單（phone number 或 JID 集合）
_sender_allowlist: set[str] = set()

# 全域的 GroupQueue 實例，負責控制每個群組的 container 並發數量
_group_queue = GroupQueue()


def _load_state() -> None:
    """從 SQLite 的 router_state 表讀取上次儲存的時間戳記游標，
    確保重啟後不會重複處理舊訊息。"""
    global _last_timestamp
    val = db.get_state("lastTimestamp")
    if val:
        _last_timestamp = int(val)

def _cleanup_orphan_tasks() -> None:
    """啟動時清理 chat_jid 為空的孤兒任務（在 chat_jid 修復前建立的舊任務）。"""
    all_tasks = db.get_all_tasks()
    bad = [t for t in all_tasks if not t.get("chat_jid", "").strip()]
    for t in bad:
        log.warning(f"Removing orphan task {t['id']}: empty chat_jid")
        db.delete_task(t["id"])
    if bad:
        log.info(f"Cleaned up {len(bad)} orphan task(s) with missing chat_jid")

def _get_groups() -> list[dict]:
    """回傳目前登記的群組清單，供 IPC watcher 等元件查詢。"""
    return _registered_groups

def _get_group_by_jid(jid: str) -> dict | None:
    """根據 JID 找到對應的群組設定，找不到時回傳 None。"""
    return next((g for g in _registered_groups if g["jid"] == jid), None)


def get_main_group(groups: list[dict]) -> dict | None:
    """Return the single main group. Logs a warning if multiple mains exist."""
    mains = [g for g in groups if g.get("is_main")]
    if len(mains) > 1:
        log.warning("Multiple main groups found (%d), using most recent", len(mains))
    return mains[0] if mains else None


# ── Message handling ──────────────────────────────────────────────────────────

async def _on_message(jid: str, sender: str, sender_name: str, content: str,
                      is_group: bool, channel: str) -> None:
    """
    當頻道（Telegram / WhatsApp）收到新訊息時呼叫此函式。
    - 先驗證發送者是否在白名單內
    - 將訊息存入 SQLite（讓 message loop 之後批次處理）
    - 發送「正在輸入」指示器，讓用戶感覺系統有在回應

    注意：這裡只做儲存，不直接觸發 container；
    真正的處理由 _message_loop 透過 GroupQueue 排程。
    """
    if not is_sender_allowed(sender, _sender_allowlist):
        log.debug(f"Sender {sender} blocked by allowlist")
        return

    # 免疫系統檢查：偵測 prompt injection 攻擊或垃圾訊息
    # 在儲存到 DB 之前攔截，惡意訊息完全不進入處理流程
    safe, threat_type = immune_check(content, sender)
    if not safe:
        log.warning(f"Immune system blocked message from {sender}: {threat_type}")
        return

    ts = int(time.time() * 1000)
    msg_id = str(uuid.uuid4())
    # 將訊息本文存到 messages 表，供後續 container 讀取對話歷史
    db.store_message(msg_id, jid, sender, sender_name, content, ts)
    # 更新 chats 表的最後訊息時間與頻道資訊（用於管理介面顯示）
    db.store_chat_metadata(jid, sender_name, ts, channel, is_group)

    # 在訊息儲存後才送出 typing indicator，確保 DB 寫入不會被 network I/O 延遲阻塞
    ch = find_channel(jid)
    if ch and hasattr(ch, "send_typing"):
        try:
            await ch.send_typing(jid)
        except Exception:
            pass


async def _process_group_messages(group: dict, messages: list[dict],
                                   on_success=None) -> None:
    """
    將一批訊息格式化後交給 container 執行。

    on_success callback 只有在 container 成功完成後才呼叫，
    用來推進 _last_timestamp 游標（rollback 安全機制：
    如果 container 中途失敗，游標不動，下次會重新處理同批訊息）。
    """
    folder = group["folder"]
    jid = group["jid"]

    requires_trigger = bool(group.get("requires_trigger", True))
    is_main = bool(group.get("is_main"))

    # 非主群組且設定了 trigger 要求時，只處理含有觸發關鍵字的訊息
    if requires_trigger and not is_main:
        messages = [m for m in messages if config.TRIGGER_PATTERN.search(m.get("content", ""))]
    if not messages:
        return

    # 取得最近 20 條對話歷史，轉為原生 multi-turn 格式
    new_ts_set = {m["timestamp"] for m in messages}
    raw_history = [m for m in db.get_conversation_history(jid, limit=20) if m["timestamp"] not in new_ts_set]
    conversation_history = [
        {
            "role": "assistant" if m.get("is_bot_message") else "user",
            "content": m.get("content", ""),
            "sender": m.get("sender_name") or m.get("sender", ""),
        }
        for m in raw_history
        if m.get("content", "").strip()
    ]
    # 只把新訊息作為 prompt（XML context），歷史以 multi-turn 傳入
    prompt = format_messages(messages, config.TIMEZONE)
    session_id = db.get_session(folder)

    log.info(f"Processing {len(messages)} message(s) for {folder}")

    async def on_output(text: str):
        # 將 container 的回覆透過 router 發送回對應的聊天室
        await route_outbound(jid, text)
        # Push reply to Web Portal sessions watching this JID
        try:
            from .webportal import deliver_reply
            deliver_reply(jid, text)
        except Exception:
            pass
        # Store bot response in DB so dashboard can show full conversation
        try:
            ts = int(time.time() * 1000)
            msg_id = str(uuid.uuid4())
            db.store_message(
                msg_id, jid,
                sender="bot",
                sender_name=config.ASSISTANT_NAME,
                content=text,
                timestamp=ts,
                is_from_me=True,
                is_bot_message=True,
            )
        except Exception:
            pass

    try:
        await asyncio.wait_for(
            run_container_agent(
                group=group,
                prompt=prompt,
                conversation_history=conversation_history,
                on_output=on_output,
                session_id=session_id,
                on_success=on_success,
            ),
            timeout=300.0,  # 5-minute timeout per container run
        )
    except asyncio.TimeoutError:
        log.error(
            "Container run timed out after 300s for group %s — message NOT dropped, "
            "will be retried on next poll cycle", jid
        )
        # DO NOT call on_success() — cursor stays behind so message is retried
        # Notify user that we're still working
        try:
            await route_outbound(
                jid,
                "⏱️ This request is taking longer than expected. It will be retried automatically."
            )
        except Exception:
            pass


async def _message_loop() -> None:
    """
    主要輪詢迴圈：每隔 POLL_INTERVAL 秒從 SQLite 撈取新訊息，
    並交給 GroupQueue 排程執行。

    游標（_last_timestamp）的推進採用「先執行、後確認」策略：
    訊息批次先交給 container 處理，只有在 on_success callback 被呼叫時
    才更新游標。這樣即使 container 意外終止，下次重啟仍能重新處理
    同一批訊息，不會漏掉任何對話。
    """
    global _last_timestamp, _registered_groups
    log.info("Message loop started")
    while _running:
        try:
            # 偵測是否有 refresh_groups.flag 旗標檔，有的話重新從 DB 載入群組清單
            # 這讓 IPC watcher 可以在不重啟程序的情況下動態新增群組
            refresh_flag = config.DATA_DIR / "refresh_groups.flag"
            if refresh_flag.exists():
                try:
                    refresh_flag.unlink(missing_ok=True)
                    _registered_groups = db.get_all_registered_groups()
                    log.info(f"Groups reloaded: {len(_registered_groups)} group(s)")
                except Exception as e:
                    log.error(f"Failed to reload groups: {e}")

            jids = [g["jid"] for g in _registered_groups]
            if jids:
                # Check if any new messages exist (single source of truth for cursor
                # is in _process_messages_for_jid — no duplicate computation here)
                messages = db.get_new_messages(jids, _last_timestamp)
                if messages:
                    # Collect unique JIDs that have new messages, then trigger
                    # GroupQueue for each. Cursor advancement is handled exclusively
                    # in _process_messages_for_jid via the on_success callback.
                    active_jids: set[str] = set()
                    for m in messages:
                        if "chat_jid" in m:
                            active_jids.add(m["chat_jid"])
                    for jid in active_jids:
                        group = _get_group_by_jid(jid)
                        if group:
                            # GroupQueue ensures only one container runs per group
                            _group_queue.enqueue_message_check(jid)
        except Exception as e:
            log.error(f"Message loop error: {e}")
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=config.POLL_INTERVAL)
        except asyncio.TimeoutError:
            pass  # Normal poll cycle


async def _orphan_cleanup_loop(stop_event: asyncio.Event) -> None:
    """Periodically clean up orphaned Docker containers every 5 minutes."""
    while not stop_event.is_set():
        try:
            await cleanup_orphans()
        except Exception as exc:
            log.warning("orphan cleanup error: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=300.0)
        except asyncio.TimeoutError:
            pass


async def _ipc_route_fn(jid: str, text: str, sender: str | None = None) -> None:
    """IPC watcher 的路由回呼：將 container 發出的訊息轉發到對應聊天室。"""
    await route_outbound(jid, text)
    try:
        from .webportal import deliver_reply
        deliver_reply(jid, text)
    except Exception:
        pass
    # Store bot response (from scheduled tasks / IPC) in DB
    try:
        ts = int(time.time() * 1000)
        msg_id = str(uuid.uuid4())
        db.store_message(
            msg_id, jid,
            sender="bot",
            sender_name=config.ASSISTANT_NAME,
            content=text,
            timestamp=ts,
            is_from_me=True,
            is_bot_message=True,
        )
    except Exception:
        pass


# ── Startup ───────────────────────────────────────────────────────────────────

async def main() -> None:
    """
    系統啟動入口，依序執行：
    1. 初始化 SQLite 資料庫並載入上次的游標狀態
    2. 載入發送者白名單與已登記群組
    3. 清理上次異常終止留下的孤兒 container
    4. 將 GroupQueue 與訊息處理函式串接
    5. 動態載入並連接啟用的頻道（Telegram / WhatsApp）
    6. 設定 SIGTERM/SIGINT 優雅關機處理器
    7. 同時啟動訊息輪詢、IPC watcher 及排程器三個背景迴圈
    """
    global _registered_groups, _sender_allowlist, _stop_event
    _stop_event = asyncio.Event()

    log.info("EvoClaw starting up...")

    # 初始化 SQLite 資料庫，建立所有必要的資料表
    db_path = config.STORE_DIR / "messages.db"
    db.init_database(db_path)
    from . import log_buffer
    log_buffer.install()
    _load_state()

    # 啟動 Web dashboard（背景 daemon thread，port DASHBOARD_PORT）
    start_dashboard(_stop_event)
    from .webportal import start_webportal, deliver_reply as _portal_deliver
    start_webportal()
    _cleanup_orphan_tasks()  # ← add this line

    # 從設定檔載入允許傳訊的發送者白名單
    _sender_allowlist = load_sender_allowlist()

    # 從 DB 載入已登記的群組（包含 JID、folder、trigger 等設定）
    _registered_groups = db.get_all_registered_groups()
    log.info(f"Loaded {len(_registered_groups)} registered group(s)")

    # 確保群組資料夾與全域共享資料夾存在
    config.GROUPS_DIR.mkdir(parents=True, exist_ok=True)
    (config.GROUPS_DIR / "global").mkdir(exist_ok=True)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 清除上次程序崩潰後遺留的 evoclaw-* container，避免資源洩漏
    await cleanup_orphans()

    # ── 將 GroupQueue 與實際的訊息處理邏輯串接 ──────────────────────────────
    async def _process_messages_for_jid(jid: str) -> bool:
        """
        GroupQueue 的 callback：當輪到某個群組執行時被呼叫。
        從 DB 取得該群組的待處理訊息，執行 container，
        成功後推進游標。回傳 True 代表成功（GroupQueue 會重置 retry 計數）。
        """
        group = _get_group_by_jid(jid)
        if not group:
            return True
        msgs = db.get_new_messages([jid], _last_timestamp)
        if not msgs:
            return True
        ts = max(m["timestamp"] for m in msgs)
        async def advance(ts=ts):
            global _last_timestamp
            _last_timestamp = max(_last_timestamp, ts)
            db.set_state("lastTimestamp", str(_last_timestamp))
        await _process_group_messages(group, msgs, on_success=advance)
        return True

    _group_queue.set_process_messages_fn(_process_messages_for_jid)

    # ── 動態載入已啟用的頻道模組 ────────────────────────────────────────────
    # 用 module path 字串動態 import，讓未安裝的頻道套件不影響其他頻道啟動
    _channel_module_map = {
        "telegram": "channels.telegram_channel",
        "whatsapp": "channels.whatsapp_channel",
    }
    _channel_class_map = {
        "telegram": "TelegramChannel",
        "whatsapp": "WhatsAppChannel",
    }

    _loaded_channels = []
    for channel_name in config.ENABLED_CHANNELS:
        module_path = _channel_module_map.get(channel_name)
        class_name = _channel_class_map.get(channel_name)
        if not module_path or not class_name:
            log.warning(f"Unknown channel '{channel_name}' in ENABLED_CHANNELS — skipping")
            continue
        try:
            import importlib
            mod = importlib.import_module(f".{module_path}", package=__package__)
            cls = getattr(mod, class_name)
            ch = cls(
                on_message=_on_message,
                on_chat_metadata=lambda **kw: None,
                registered_groups=_registered_groups,
            )
            await ch.connect()
            register_channel(ch)
            _loaded_channels.append(ch)
            log.info(f"Channel '{channel_name}' loaded and connected")
        except Exception as e:
            log.error(f"Failed to load channel '{channel_name}': {e}")

    # ── 優雅關機：接到 SIGTERM/SIGINT 時設旗標讓各迴圈自然退出 ──────────────
    def _shutdown(sig, frame):
        global _running
        log.info(f"Received {sig}, shutting down...")
        _running = False
        if _stop_event is not None:
            _stop_event.set()  # Wake up all waiting coroutines immediately

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, _shutdown)

    try:
        # 同時啟動四個長期運行的背景迴圈：
        # - _message_loop: 輪詢 DB 取得新訊息並透過 GroupQueue 排程
        # - start_ipc_watcher: 監控 IPC 目錄，處理 container 發出的指令
        # - start_scheduler_loop: 檢查排程任務是否到期並觸發執行
        # - evolution_loop: 每 24 小時執行一次演化週期，調整群組基因組
        await asyncio.gather(
            _message_loop(),
            start_ipc_watcher(_get_groups, _ipc_route_fn, _stop_event),
            start_scheduler_loop(_get_group_by_jid, run_container_agent, _stop_event),
            evolution_loop(_stop_event),
            health_monitor_loop(_stop_event),
            _orphan_cleanup_loop(_stop_event),
        )
    finally:
        # 確保所有頻道在離開時都乾淨地斷線
        for channel in _loaded_channels:
            try:
                await channel.disconnect()
            except Exception:
                pass

    log.info("EvoClaw shut down cleanly.")


if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
