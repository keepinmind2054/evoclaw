#!/usr/bin/env python3
"""
EvoClaw Host — Main Entry Point
Orchestrates message polling, container execution, IPC, and scheduling.
"""
import asyncio
import collections
import hashlib
import logging
import os
import signal
import time
import uuid
from collections import deque
from pathlib import Path

# ── Per-group consecutive failure tracking (prevents infinite retry loops) ────
_group_fail_counts: dict[str, int] = {}
_group_fail_timestamps: dict[str, float] = {}
_group_fail_lock: asyncio.Lock | None = None  # initialized in main() after event loop starts
_GROUP_MAX_FAILS = 5
_GROUP_FAIL_COOLDOWN = 60.0  # seconds

# ── Per-group error notification rate limiter ──────────────────────────────────
# Prevents flooding the user with repeated error messages during a failure storm.
# One error notification per group per _ERROR_NOTIFY_COOLDOWN seconds.
_error_notify_times: dict[str, float] = {}
_ERROR_NOTIFY_COOLDOWN = 300.0  # 5 minutes

# ── Monitor group (watchdog destination) ──────────────────────────────────────
# If MONITOR_JID is set in .env, error notifications are also forwarded there.
# The monitor group is auto-registered at startup so EvoClaw can route to it.
_MONITOR_JID: str = ""  # populated in main() from env

# ── Heartbeat ─────────────────────────────────────────────────────────────────
# Periodic "I'm alive" ping to MONITOR_JID. If the pings stop, EvoClaw is down.
# Interval is configurable via HEARTBEAT_INTERVAL env var (default 30 min).
_last_heartbeat: float = 0.0
_HEARTBEAT_INTERVAL: float = 1800.0  # 30 minutes; overridden in main() from env
_startup_time: float = 0.0

# ── Discord Webhook ────────────────────────────────────────────────────────────
# Optional second notification channel. POST errors + heartbeat to Discord.
# Set DISCORD_WEBHOOK_URL in .env to enable. No Discord bot token needed.
_DISCORD_WEBHOOK_URL: str = ""  # populated in main() from env

from . import config, db
from .allowlist import load_sender_allowlist, is_sender_allowed
from .dashboard import start_dashboard
from .container_runner import run_container_agent, cleanup_orphans, _read_secrets, _validate_secrets
from .group_queue import GroupQueue
from .ipc_watcher import start_ipc_watcher
from .task_scheduler import start_scheduler_loop
from .router import register_channel, route_outbound, format_messages, find_channel
from .evolution import check_message as immune_check, evolution_loop
from .health_monitor import health_monitor_loop
from .memory import append_warm_log

# Phase 1 (UnifiedClaw): Universal Memory Bus + WSBridge + Agent Identity
try:
    from .memory.memory_bus import MemoryBus as _MemoryBus
    from .identity.agent_identity import AgentIdentityStore as _AgentIdentityStore
    from .ws_bridge import WSBridge as _WSBridge
    _PHASE1_AVAILABLE = True
except ImportError as _e:
    _PHASE1_AVAILABLE = False
    print(f"[Phase1] Components not available: {_e}")

# Phase 2 (UnifiedClaw): SDK API + Memory Summarizer
try:
    from .sdk_api import SdkApi as _SdkApi
    from .memory.summarizer import MemorySummarizer as _MemorySummarizer
    _PHASE2_AVAILABLE = True
except ImportError as _e2:
    _PHASE2_AVAILABLE = False
    _SdkApi = None
    _MemorySummarizer = None
    print(f"[Phase2] Components not available: {_e2}")



async def _discord_notify(content: str) -> None:
    """POST a message to the Discord webhook (if configured).

    Uses aiohttp for non-blocking HTTP. Silently ignores errors so a
    Discord outage never affects the main EvoClaw flow.
    Content is truncated to Discord's 2000-char limit.
    """
    if not _DISCORD_WEBHOOK_URL:
        return
    try:
        import aiohttp as _aiohttp
        payload = {"content": content[:2000]}
        async with _aiohttp.ClientSession() as session:
            async with session.post(_DISCORD_WEBHOOK_URL, json=payload, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status not in (200, 204):
                    log.warning("Discord webhook returned HTTP %s", resp.status)
    except Exception as _de:
        log.debug("Discord webhook error (non-fatal): %s", _de)


async def _store_bot_reply(jid: str, text: str) -> None:
    """Store a bot reply in DB and push to webportal (Fix #189).

    Extracted from on_output and _ipc_route_fn to eliminate duplication.
    Errors are logged but never propagated — bot reply delivery must not
    crash the caller.
    """
    try:
        from .webportal import deliver_reply
        deliver_reply(jid, text)
    except Exception as e:
        log.debug("deliver_reply failed for %s: %s", jid, e)
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
    except Exception as e:
        log.error("Failed to store bot response in DB for %s: %s", jid, e)


def _configure_logging() -> None:
    """Configure logging format based on LOG_FORMAT env var.

    LOG_FORMAT=json  → emit newline-delimited JSON (compatible with Loki/Datadog/CloudWatch)
    LOG_FORMAT=text  → human-readable text (default)
    """
    level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    if config.LOG_FORMAT == "json":
        try:
            from pythonjsonlogger import jsonlogger  # type: ignore
            handler = logging.StreamHandler()
            handler.setFormatter(
                jsonlogger.JsonFormatter(
                    "%(asctime)s %(levelname)s %(name)s %(message)s"
                )
            )
            logging.root.setLevel(level)
            logging.root.addHandler(handler)
            return
        except ImportError:
            pass  # python-json-logger not installed — fall back to text
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

_configure_logging()
log = logging.getLogger("evoclaw")

# ── State ─────────────────────────────────────────────────────────────────────

# 目前已登記的群組清單，每個 dict 包含 jid、folder、is_main 等欄位
_registered_groups: list[dict] = []

# 訊息時間戳記游標：每個 JID 獨立記錄，只處理比此值更新的訊息。
# 使用 per-JID 游標而非全域游標，防止群組 A 的成功執行推進游標
# 超過群組 B 尚未處理的訊息時間戳記，導致群組 B 的訊息被靜默丟棄。
# 舊版單一全域游標 lastTimestamp 仍用作啟動時的初始值（向後相容）。
_last_timestamp: int = 0           # global fallback / legacy cursor (read-only after init)
_per_jid_cursors: dict[str, int] = {}  # per-JID cursors (authoritative)

# 全域開關：設為 False 時，所有背景 loop 都會停止
_running = True

# Set by _message_loop when self_update.flag is detected; triggers os.execv() after shutdown
_self_update_requested: bool = False

# 共用的停止事件：shutdown 時 set()，讓所有等待中的 sleep 立即醒來
_stop_event: asyncio.Event | None = None

# 允許傳送訊息的發送者白名單（phone number 或 JID 集合）
_sender_allowlist: set[str] = set()

# 全域的 GroupQueue 實例，負責控制每個群組的 container 並發數量
_group_queue = GroupQueue()

# ── Per-group rate limiting ────────────────────────────────────────────────────
# Sliding-window rate limiter to prevent a single group from flooding the system.
# A group that exceeds RATE_LIMIT_MAX_MSGS messages within RATE_LIMIT_WINDOW_SECS
# will have excess messages silently dropped until the window slides forward.
# Values are intentionally permissive — they guard against abuse, not normal chat.
_group_msg_timestamps: dict[str, deque] = {}  # jid → deque of float timestamps


def _is_rate_limited(jid: str) -> bool:
    """Return True if the group has exceeded the per-group message rate limit.

    Values are read from config so operators can tune via env vars:
      RATE_LIMIT_MAX_MSGS     (default 20)
      RATE_LIMIT_WINDOW_SECS  (default 60)
    """
    now = time.time()
    # Fix #118: cap deque at RATE_LIMIT_MAX_MSGS * 2 entries to prevent unbounded growth.
    # Without maxlen, a group sending messages within the window never triggers eviction,
    # causing the deque to grow indefinitely over hours/days of operation.
    max_msgs = config.RATE_LIMIT_MAX_MSGS
    q = _group_msg_timestamps.setdefault(jid, deque(maxlen=max_msgs * 2))
    window = float(config.RATE_LIMIT_WINDOW_SECS)
    # Evict timestamps outside the rolling window
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= config.RATE_LIMIT_MAX_MSGS:
        return True
    q.append(now)
    return False


# ── Message deduplication fence ───────────────────────────────────────────────
# Short-lived in-memory set of recently-seen message fingerprints.
# Prevents duplicate processing caused by webhook retries or channel double-delivery.
# Uses an OrderedDict as a bounded LRU cache: oldest entries are evicted when full.
_DEDUP_MAX = 1000  # maximum entries before oldest is evicted
_seen_msg_fingerprints: collections.OrderedDict = collections.OrderedDict()
_dedup_lock: asyncio.Lock | None = None  # initialized in main() after event loop starts


async def _is_duplicate_message(jid: str, sender: str, content: str) -> bool:
    """Return True if this (jid, sender, content) combination was seen recently.

    A SHA-256 fingerprint of the three values is used as the key to bound memory
    usage. If the dedup set is full, the oldest entry is evicted (LRU eviction).

    The entire check-then-insert is wrapped in a single async with _dedup_lock:
    block so no two coroutines can check/insert simultaneously (Fix #105).
    """
    raw = f"{jid}\x00{sender}\x00{content}"
    fp = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
    async with _dedup_lock:
        if fp in _seen_msg_fingerprints:
            _seen_msg_fingerprints.move_to_end(fp)  # mark as recently used
            return True
        _seen_msg_fingerprints[fp] = True
        if len(_seen_msg_fingerprints) > _DEDUP_MAX:
            _seen_msg_fingerprints.popitem(last=False)  # evict oldest
        return False


def _load_state() -> None:
    """從 SQLite 的 router_state 表讀取上次儲存的游標狀態。

    Per-JID cursors (cursorJID:<jid>) take precedence over the legacy global
    lastTimestamp.  On first run after upgrade, all JIDs inherit the global
    value so no messages are reprocessed.
    """
    global _last_timestamp, _per_jid_cursors
    val = db.get_state("lastTimestamp")
    if val:
        _last_timestamp = int(val)
    # Load per-JID cursors persisted by previous runs
    groups = db.get_all_registered_groups()
    for g in groups:
        jid = g["jid"]
        cursor_val = db.get_state(f"cursorJID:{jid}")
        if cursor_val:
            _per_jid_cursors[jid] = int(cursor_val)
        else:
            # First run for this JID: inherit global cursor so we don't replay history
            _per_jid_cursors[jid] = _last_timestamp

def _cleanup_orphan_tasks() -> None:
    """啟動時清理孤兒任務：
    1. chat_jid 為空的舊任務（在 chat_jid 修復前建立）
    2. 屬於已刪除群組（不在 registered_groups 中）的任務
    """
    all_tasks = db.get_all_tasks()
    registered_jids = {g["jid"] for g in db.get_all_registered_groups()}
    bad = [
        t for t in all_tasks
        if not t.get("chat_jid", "").strip()
        or t.get("chat_jid") not in registered_jids
    ]
    for t in bad:
        log.warning(f"Removing orphan task {t['id']}: chat_jid={t.get('chat_jid')!r}")
        db.delete_task(t["id"])
    if bad:
        log.info(f"Cleaned up {len(bad)} orphan task(s)")

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


# ── Setup command handler (/monitor) ──────────────────────────────────────────

async def _handle_setup_command(jid: str, command: str) -> str:
    """Handle one-step setup commands from channels (e.g. Telegram /monitor).

    /monitor — registers the calling group as the monitor group, writes
    MONITOR_JID to .env, and reloads the registered group list so no restart
    is needed.
    """
    global _MONITOR_JID, _registered_groups

    if command == "monitor":
        _monitor_folder = "discord_monitor"
        try:
            # Register in DB
            db.set_registered_group(
                jid=jid,
                name="EvoClaw Monitor",
                folder=_monitor_folder,
                trigger_pattern=None,
                container_config=None,
                requires_trigger=False,
                is_main=False,
            )
            (config.GROUPS_DIR / _monitor_folder).mkdir(parents=True, exist_ok=True)
            # Persist to .env so it survives restart
            _write_monitor_jid_to_env(jid)
            # Update in-memory state immediately (no restart needed)
            _MONITOR_JID = jid
            _registered_groups = db.get_all_registered_groups()
            log.info("Monitor group set via /monitor command: jid=%s", jid)
            return (
                f"✅ *監控群組設定完成*\n\n"
                f"JID: `{jid}`\n"
                f"EvoClaw 的錯誤通知（container crash、timeout、Docker 異常）將自動發送到這裡。\n\n"
                f"指令：\n"
                f"• 傳 `/reset tg:XXXX` 解凍卡住的群組\n"
                f"• 傳 `/status` 查看最近的執行記錄"
            )
        except Exception as exc:
            log.error("_handle_setup_command monitor failed: %s", exc)
            return f"❌ 設定失敗：{exc}"

    return f"⚠️ 未知指令：{command}"


def _write_monitor_jid_to_env(jid: str) -> None:
    """Write or update MONITOR_JID in the .env file next to the project root."""
    env_path = config.BASE_DIR / ".env"
    if not env_path.exists():
        log.warning(".env not found at %s — cannot persist MONITOR_JID", env_path)
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
        found = False
        new_lines = []
        for line in lines:
            if line.strip().startswith("MONITOR_JID=") or line.strip().startswith("# MONITOR_JID="):
                new_lines.append(f"MONITOR_JID={jid}\n")
                found = True
            else:
                new_lines.append(line)
        if not found:
            # Append at end
            new_lines.append(f"\nMONITOR_JID={jid}\n")
        env_path.write_text("".join(new_lines), encoding="utf-8")
        log.info("MONITOR_JID=%s written to %s", jid, env_path)
    except Exception as exc:
        log.warning("Failed to write MONITOR_JID to .env: %s", exc)


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

    # Per-group rate limit: drop excess messages to prevent one group from
    # starving others.  Logged at DEBUG to avoid log spam under sustained load.
    if _is_rate_limited(jid):
        log.debug(
            "Rate limit exceeded for group %s (sender=%s) — message dropped", jid, sender
        )
        return

    # 去重複檢查：防止頻道 webhook 重試造成相同訊息被處理兩次
    if await _is_duplicate_message(jid, sender, content):
        log.debug("Duplicate message fingerprint detected from %s in %s — skipping", sender, jid)
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

    # ── Consecutive failure guard: prevent infinite retry loops ───────────────
    async with _group_fail_lock:
        fail_count = _group_fail_counts.get(jid, 0)
        last_fail = _group_fail_timestamps.get(jid, 0.0)
        if fail_count >= _GROUP_MAX_FAILS:
            if time.time() - last_fail < _GROUP_FAIL_COOLDOWN:
                log.warning(
                    "Group %s has failed %d times consecutively, cooling down for %ds",
                    jid, fail_count, int(_GROUP_FAIL_COOLDOWN - (time.time() - last_fail))
                )
                return
            else:
                # Cooldown expired — decay counter and allow retry
                # Instead of resetting to 0, reduce by 2 so repeated failures
                # accumulate longer cooldowns
                _group_fail_counts[jid] = max(0, _group_fail_counts.get(jid, 0) - 2)
                _group_fail_timestamps.pop(jid, None)
                log.info("group %s cooldown expired; fail_count decayed to %d", jid, _group_fail_counts[jid])

    requires_trigger = bool(group.get("requires_trigger", True))
    is_main = bool(group.get("is_main"))

    # 非主群組且設定了 trigger 要求時，只處理含有觸發關鍵字的訊息
    if requires_trigger and not is_main:
        messages = [m for m in messages if config.TRIGGER_PATTERN.search(m.get("content", ""))]
    if not messages:
        return

    # 取得最近 50 條對話歷史，轉為原生 multi-turn 格式（原為 20 ≈ 10 輪，提升至 50 ≈ 25 輪）
    new_ts_set = {m["timestamp"] for m in messages}
    raw_history = [m for m in db.get_conversation_history(jid, limit=50) if m["timestamp"] not in new_ts_set]
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

    # Capture prompt text for warm memory logging (join all new message contents)
    _user_prompt_text = " ".join(m.get("content", "") for m in messages if m.get("content", "").strip())

    async def on_output(text: str):
        # 將 container 的回覆透過 router 發送回對應的聊天室
        await route_outbound(jid, text)
        # Store + push reply via shared helper (Fix #189)
        await _store_bot_reply(jid, text)
        # 三層記憶系統：暖記憶 — 每次對話後自動追加摘要到今日日誌
        try:
            append_warm_log(jid, _user_prompt_text, text)
        except Exception as e:
            log.warning("append_warm_log failed for %s: %s", jid, e)

    # ── Error notification callback (rate-limited, no config needed) ─────────────
    # Sends error messages inline in the conversation so the user sees them
    # immediately without checking backend logs. Capped at once per 5 minutes
    # per group to prevent flooding during a failure storm.
    # Separator used by container_runner to bundle monitor context into error messages.
    # Format: "<user-friendly msg>|||MONITOR_CONTEXT|||<detailed context>"
    # on_error shows only the user part to the originating group;
    # the monitor group receives the full context so Eve can diagnose the issue.
    _MONITOR_CTX_SEP = "|||MONITOR_CONTEXT|||"

    async def on_error(msg: str):
        now = time.time()
        last = _error_notify_times.get(jid, 0.0)
        if now - last < _ERROR_NOTIFY_COOLDOWN:
            log.debug("on_error rate-limited for %s (%.0fs remaining)", jid, _ERROR_NOTIFY_COOLDOWN - (now - last))
            return
        _error_notify_times[jid] = now

        # Split user-facing message from optional monitor context
        if _MONITOR_CTX_SEP in msg:
            user_msg, error_context = msg.split(_MONITOR_CTX_SEP, 1)
            user_msg = user_msg.strip()
            error_context = error_context.strip()
        else:
            user_msg, error_context = msg, ""

        log.info("Sending error notification to %s: %s", jid, user_msg)
        # Send short friendly message to originating group
        try:
            await route_outbound(jid, user_msg)
        except Exception as _e:
            log.warning("on_error route_outbound failed for %s: %s", jid, _e)
        # Forward to monitor group with full context so Eve can diagnose
        if _MONITOR_JID and _MONITOR_JID != jid:
            try:
                monitor_msg = f"🔔 [{folder}] {user_msg}"
                if error_context:
                    monitor_msg += f"\n\n```\n{error_context[:2000]}\n```"
                await route_outbound(_MONITOR_JID, monitor_msg)
                log.debug("on_error forwarded to monitor group %s", _MONITOR_JID)
            except Exception as _me:
                log.warning("on_error monitor forward failed: %s", _me)
        # Forward to Discord webhook (if configured)
        await _discord_notify(f"⚠️ **EvoClaw Error** `[{folder}]`\n{user_msg}" +
                              (f"\n```\n{error_context[:1000]}\n```" if error_context else ""))

    # Wrap on_success to reset the failure counter on a successful run
    _run_succeeded = False

    async def _on_success_tracked():
        nonlocal _run_succeeded
        _run_succeeded = True
        async with _group_fail_lock:
            _group_fail_counts.pop(jid, None)
            _group_fail_timestamps.pop(jid, None)
        if on_success:
            await on_success()

    try:
        result = await asyncio.wait_for(
            run_container_agent(
                group=group,
                prompt=prompt,
                conversation_history=conversation_history,
                on_output=on_output,
                session_id=session_id,
                on_success=_on_success_tracked,
                on_error=on_error,
            ),
            timeout=config.CONTAINER_TIMEOUT,  # use central config value
        )
        # run_container_agent returns {"status": "error", ...} when no output markers found
        if not _run_succeeded and isinstance(result, dict) and result.get("status") == "error":
            async with _group_fail_lock:
                _group_fail_counts[jid] = _group_fail_counts.get(jid, 0) + 1
                _group_fail_timestamps[jid] = time.time()
    except asyncio.TimeoutError:
        log.error(
            "Container run timed out after %ds for group %s — message NOT dropped, "
            "will be retried on next poll cycle", int(config.CONTAINER_TIMEOUT), jid
        )
        async with _group_fail_lock:
            _group_fail_counts[jid] = _group_fail_counts.get(jid, 0) + 1
            _group_fail_timestamps[jid] = time.time()
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

    游標（per-JID cursor）的推進採用「先執行、後確認」策略：
    訊息批次先交給 container 處理，只有在 on_success callback 被呼叫時
    才更新游標。這樣即使 container 意外終止，下次重啟仍能重新處理
    同一批訊息，不會漏掉任何對話。

    Per-JID cursors (Issue #52): each group advances its own cursor
    independently, so a successful run for group A can never push the
    shared timestamp past group B's pending messages.
    """
    global _registered_groups, _per_jid_cursors
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
                    current_jids = {g["jid"] for g in _registered_groups}
                    # Initialise cursors for any newly added groups
                    for g in _registered_groups:
                        if g["jid"] not in _per_jid_cursors:
                            _per_jid_cursors[g["jid"]] = _last_timestamp
                    # Fix #203: prune tracking dicts for deregistered groups to prevent memory leak
                    stale_jids = set(_per_jid_cursors.keys()) - current_jids
                    for jid in stale_jids:
                        _per_jid_cursors.pop(jid, None)
                        _group_msg_timestamps.pop(jid, None)
                        async with _group_fail_lock:
                            _group_fail_counts.pop(jid, None)
                            _group_fail_timestamps.pop(jid, None)
                    if stale_jids:
                        log.info("Pruned tracking state for %d deregistered group(s)", len(stale_jids))
                    log.info(f"Groups reloaded: {len(_registered_groups)} group(s)")
                except Exception as e:
                    log.error(f"Failed to reload groups: {e}")

            # ── reset_group flag: clear fail counters for a specific group ───
            reset_flag = config.DATA_DIR / "reset_group.flag"
            if reset_flag.exists():
                try:
                    import json as _rjson
                    _rflag_data = _rjson.loads(reset_flag.read_text(encoding="utf-8"))
                    reset_flag.unlink(missing_ok=True)
                    _target_jid = _rflag_data.get("jid", "")
                    if _target_jid:
                        async with _group_fail_lock:
                            _group_fail_counts.pop(_target_jid, None)
                            _group_fail_timestamps.pop(_target_jid, None)
                        _error_notify_times.pop(_target_jid, None)
                        log.info("reset_group: cleared fail counters for jid=%s", _target_jid)
                except Exception as _rfe:
                    log.warning("reset_group flag processing failed: %s", _rfe)

            # ── Self-update flag: restart via os.execv() ────────────────────
            self_update_flag = config.DATA_DIR / "self_update.flag"
            if self_update_flag.exists():
                global _self_update_requested
                _self_update_requested = True
                self_update_flag.unlink(missing_ok=True)
                log.info("self_update flag detected — initiating graceful restart")
                _running = False
                if _stop_event is not None:
                    _stop_event.set()
                break

            # ── Heartbeat: periodic ping to monitor group ────────────────────
            global _last_heartbeat
            if _MONITOR_JID and (time.time() - _last_heartbeat) >= _HEARTBEAT_INTERVAL:
                _last_heartbeat = time.time()
                try:
                    import datetime as _hb_dt
                    _uptime_s = int(time.time() - _startup_time)
                    _uptime_str = f"{_uptime_s // 3600}h {(_uptime_s % 3600) // 60}m"
                    _n_groups = len([g for g in _registered_groups if not g.get("folder") == "discord_monitor"])
                    _recent = db.get_error_stats(minutes=30)
                    _stats = f" | 過去30分鐘：✅{_recent['successes']} ❌{_recent['errors']}" if _recent and _recent.get("total", 0) > 0 else ""
                    _hb_msg = f"💓 EvoClaw 運行中 | 上線時間：{_uptime_str} | 群組：{_n_groups}{_stats}"
                    await route_outbound(_MONITOR_JID, _hb_msg)
                    await _discord_notify(f"💓 **EvoClaw Heartbeat** | uptime: {_uptime_str} | groups: {_n_groups}{_stats}")
                    log.debug("Heartbeat sent to monitor group and Discord")
                except Exception as _hbe:
                    log.warning("Heartbeat send failed: %s", _hbe)

            for group in _registered_groups:
                jid = group["jid"]
                cursor = _per_jid_cursors.get(jid, _last_timestamp)
                # Per-JID query: only check this group's own cursor
                msgs = db.get_new_messages([jid], cursor)
                if msgs:
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
    # Store + push reply via shared helper (Fix #189)
    await _store_bot_reply(jid, text)


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

    # Phase 1 (UnifiedClaw): Initialize Universal Memory Bus, WSBridge, AgentIdentityStore
    _memory_bus = None
    _ws_bridge = None
    _identity_store = None
    if _PHASE1_AVAILABLE:
        try:
            import sqlite3 as _sqlite3
            from pathlib import Path as _Path
            _db_conn = _sqlite3.connect("evoclaw.db", check_same_thread=False)
            _memory_bus = _MemoryBus(_db_conn, _Path("groups"))
            _identity_store = _AgentIdentityStore(_db_conn)
            _ws_bridge = _WSBridge(_memory_bus)

            @_ws_bridge.on_fitness_update
            async def _on_fitness(agent_id, score, metadata):
                # Forward fitness to evolution engine
                pass  # TODO: wire to evolution/fitness.py

            asyncio.create_task(_ws_bridge.start())
            print(f"[Phase1] MemoryBus | WSBridge (port {_ws_bridge.port}) | AgentIdentityStore initialized")
        except Exception as _e:
            print(f"[Phase1] Initialization failed (non-fatal): {_e}")

    # Phase 2 (UnifiedClaw): SDK API + Memory Summarizer
    _sdk_api = None
    _summarizer = None
    if _PHASE2_AVAILABLE and _memory_bus is not None and _identity_store is not None:
        try:
            _sdk_api = _SdkApi(_memory_bus, _identity_store)
            _summarizer = _MemorySummarizer()
            asyncio.create_task(_sdk_api.start())
            print(f"[Phase2] SdkApi OK (port {_sdk_api.port}) | MemorySummarizer OK")
        except Exception as _e3:
            print(f"[Phase2] Initialization failed (non-fatal): {_e3}")


    global _registered_groups, _sender_allowlist, _stop_event, _group_fail_lock, _dedup_lock
    global _startup_time, _HEARTBEAT_INTERVAL, _last_heartbeat
    _stop_event = asyncio.Event()
    _group_fail_lock = asyncio.Lock()
    _dedup_lock = asyncio.Lock()
    _startup_time = time.time()
    _last_heartbeat = _startup_time  # Don't fire heartbeat immediately; wait one full interval

    # Heartbeat interval: configurable via env (default 30 min)
    try:
        _HEARTBEAT_INTERVAL = float(os.environ.get("HEARTBEAT_INTERVAL", "1800"))
    except ValueError:
        _HEARTBEAT_INTERVAL = 1800.0

    log.info("EvoClaw starting up...")

    # 初始化 SQLite 資料庫，建立所有必要的資料表
    db_path = config.STORE_DIR / "messages.db"
    db.init_database(db_path)
    from . import log_buffer
    log_buffer.install()
    _load_state()

    # Prune old log rows at startup to prevent unbounded disk growth.
    # Keeps last 30 days of task_run_logs and evolution_runs by default.
    try:
        db.prune_old_logs(days=30)
    except Exception as _prune_exc:
        log.warning("Log pruning failed (non-fatal): %s", _prune_exc)

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

    # ── Monitor group auto-registration ────────────────────────────────────────
    # If MONITOR_JID is set, ensure it's registered in DB so the router can
    # deliver messages to it.  Safe to call every startup — INSERT OR REPLACE
    # is idempotent.
    global _MONITOR_JID
    _MONITOR_JID = os.environ.get("MONITOR_JID", "")
    if not _MONITOR_JID:
        # Also check .env file
        try:
            from .env import read_env_file as _ref
            _MONITOR_JID = _ref(["MONITOR_JID"]).get("MONITOR_JID", "")
        except Exception:
            pass
    if _MONITOR_JID:
        _monitor_folder = "discord_monitor"
        try:
            db.set_registered_group(
                jid=_MONITOR_JID,
                name="EvoClaw Monitor",
                folder=_monitor_folder,
                trigger_pattern=None,
                container_config=None,
                requires_trigger=False,
                is_main=False,
            )
            (config.GROUPS_DIR / _monitor_folder).mkdir(parents=True, exist_ok=True)
            _registered_groups = db.get_all_registered_groups()
            log.info("Monitor group registered: jid=%s folder=%s", _MONITOR_JID, _monitor_folder)
        except Exception as _me:
            log.warning("Failed to register monitor group: %s", _me)

    # ── Discord webhook ─────────────────────────────────────────────────────────
    global _DISCORD_WEBHOOK_URL
    _DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not _DISCORD_WEBHOOK_URL:
        try:
            from .env import read_env_file as _ref2
            _DISCORD_WEBHOOK_URL = _ref2(["DISCORD_WEBHOOK_URL"]).get("DISCORD_WEBHOOK_URL", "")
        except Exception:
            pass
    if _DISCORD_WEBHOOK_URL:
        log.info("Discord webhook configured")

    # Validate LLM secrets once at startup instead of per-container-run (Fix #190)
    _validate_secrets(_read_secrets())

    # 確保群組資料夾與全域共享資料夾存在
    config.GROUPS_DIR.mkdir(parents=True, exist_ok=True)
    (config.GROUPS_DIR / "global").mkdir(exist_ok=True)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ── Docker health check at startup (Fix #194) ─────────────────────────────
    # Verify Docker is operational before accepting messages. If unreachable,
    # log a CRITICAL warning so operators notice immediately.
    try:
        _docker_check = await asyncio.create_subprocess_exec(
            "docker", "info",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        _docker_rc = await asyncio.wait_for(_docker_check.wait(), timeout=10.0)
        if _docker_rc != 0:
            log.critical(
                "Docker daemon is not healthy (exit code %d). "
                "Container runs will fail until Docker is fixed.", _docker_rc
            )
        else:
            log.info("Docker daemon is healthy")
    except Exception as e:
        log.critical("Docker daemon is unreachable: %s. Container runs will fail.", e)

    # 清除上次程序崩潰後遺留的 evoclaw-* container，避免資源洩漏
    await cleanup_orphans()

    # ── 將 GroupQueue 與實際的訊息處理邏輯串接 ──────────────────────────────
    async def _process_messages_for_jid(jid: str) -> bool:
        """
        GroupQueue 的 callback：當輪到某個群組執行時被呼叫。
        從 DB 取得該群組的待處理訊息，執行 container，
        成功後推進此群組的 per-JID 游標（Issue #52）。
        回傳 True 代表成功（GroupQueue 會重置 retry 計數）。
        """
        group = _get_group_by_jid(jid)
        if not group:
            return True
        cursor = _per_jid_cursors.get(jid, _last_timestamp)
        msgs = db.get_new_messages([jid], cursor)
        if not msgs:
            return True
        ts = max(m["timestamp"] for m in msgs)
        async def advance(ts=ts, jid=jid):
            # Advance only this group's cursor — does not affect other groups
            _per_jid_cursors[jid] = max(_per_jid_cursors.get(jid, 0), ts)
            db.set_state(f"cursorJID:{jid}", str(_per_jid_cursors[jid]))
            # Also keep legacy global cursor up-to-date for rollback/debug use
            global _last_timestamp
            if ts > _last_timestamp:
                _last_timestamp = ts
                db.set_state("lastTimestamp", str(_last_timestamp))
        await _process_group_messages(group, msgs, on_success=advance)
        return True

    _group_queue.set_process_messages_fn(_process_messages_for_jid)

    # ── 動態載入已啟用的頻道模組 ────────────────────────────────────────────
    # 用 module path 字串動態 import，讓未安裝的頻道套件不影響其他頻道啟動
    _channel_module_map = {
        "telegram": "channels.telegram_channel",
        "whatsapp": "channels.whatsapp_channel",
        "discord": "channels.discord_channel",
        "slack": "channels.slack_channel",
        "gmail": "channels.gmail_channel",
    }
    _channel_class_map = {
        "telegram": "TelegramChannel",
        "whatsapp": "WhatsAppChannel",
        "discord": "DiscordChannel",
        "slack": "SlackChannel",
        "gmail": "GmailChannel",
    }

    # Validate ENABLED_CHANNELS at startup: warn loudly for unrecognised names
    # so operators catch typos immediately rather than silently running with no channels.
    _known_channels = set(_channel_module_map.keys())
    _invalid_channels = [c for c in config.ENABLED_CHANNELS if c and c not in _known_channels]
    if _invalid_channels:
        log.error(
            "ENABLED_CHANNELS contains unrecognised channel name(s): %s — "
            "known channels are: %s. These will be skipped. "
            "Check your ENABLED_CHANNELS environment variable for typos.",
            ", ".join(_invalid_channels),
            ", ".join(sorted(_known_channels)),
        )

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
            kwargs: dict = {
                "on_message": _on_message,
                "on_chat_metadata": lambda **kw: None,
                "registered_groups": _registered_groups,
            }
            # Inject on_setup_command for channels that support it (e.g. Telegram /monitor)
            import inspect as _inspect
            if "on_setup_command" in _inspect.signature(cls.__init__).parameters:
                kwargs["on_setup_command"] = _handle_setup_command
            ch = cls(**kwargs)
            await ch.connect()
            register_channel(ch)
            _loaded_channels.append(ch)
            log.info(f"Channel '{channel_name}' loaded and connected")
        except Exception as e:
            log.error(f"Failed to load channel '{channel_name}': {e}")

    # ── 優雅關機：接到 SIGTERM/SIGINT 時設旗標讓各迴圈自然退出 ──────────────
    _shutdown_count = 0

    def _shutdown(sig, frame):
        nonlocal _shutdown_count
        global _running
        _shutdown_count += 1

        if _shutdown_count >= 2:
            # 第二次 Ctrl+C：強制殺死所有 container 並立即退出
            log.warning("Force exit (second signal). Killing all containers...")
            from .container_runner import _active_containers as _ac
            names = list(_ac.keys())
            if names:
                import subprocess as _sp
                try:
                    _sp.run(["docker", "kill"] + names, capture_output=True, timeout=5)
                except Exception:
                    pass
            import os as _os
            _os._exit(1)

        log.info(f"Received {sig}, shutting down... (press Ctrl+C again to force exit)")
        _running = False
        _group_queue.shutdown_sync()  # signal: no new tasks accepted
        if _stop_event is not None:
            _stop_event.set()  # Wake up all waiting coroutines immediately

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, _shutdown)
    # SIGUSR1: 線上重置 Docker circuit breaker（不需重啟進程）
    # 用法：kill -USR1 $(pgrep -f "python.*evoclaw")
    if hasattr(signal, 'SIGUSR1'):
        from .container_runner import _record_docker_success
        def _reset_circuit(sig, frame):
            log.warning("SIGUSR1 received — resetting Docker circuit breaker (failures → 0)")
            _record_docker_success()
        signal.signal(signal.SIGUSR1, _reset_circuit)

    try:
        # 同時啟動四個長期運行的背景迴圈：
        # - _message_loop: 輪詢 DB 取得新訊息並透過 GroupQueue 排程
        # - start_ipc_watcher: 監控 IPC 目錄，處理 container 發出的指令
        # - start_scheduler_loop: 檢查排程任務是否到期並觸發執行
        # - evolution_loop: 每 24 小時執行一次演化週期，調整群組基因組
        await asyncio.gather(
            _message_loop(),
            start_ipc_watcher(_get_groups, _ipc_route_fn, _stop_event),
            start_scheduler_loop(_get_group_by_jid, run_container_agent, _stop_event, _group_queue),
            evolution_loop(_stop_event),
            health_monitor_loop(_stop_event),
            _orphan_cleanup_loop(_stop_event),
        )
    finally:
        # Fix #135: disconnect channels FIRST so Telegram's update_fetcher_task can stop cleanly
        # before we bulk-cancel tasks — prevents the misleading CRITICAL CancelledError log.
        for channel in _loaded_channels:
            try:
                await channel.disconnect()
            except Exception:
                pass

        # 等待所有進行中的 container 完成（最多 10 秒，從 30s 縮短），避免截斷回覆或損毀 IPC 狀態
        await _group_queue.wait_for_active(timeout=10.0)

        # 若 10 秒後仍有 container 在跑，強制 kill 全部（Fix #164）
        if _group_queue._active_count > 0:
            log.warning("Containers still active after timeout, force-killing all...")
            from .container_runner import kill_all_containers
            try:
                await asyncio.wait_for(kill_all_containers(), timeout=5.0)
            except Exception:
                pass

        # Fix #121: cancel any remaining sleeping tasks after channels are safely disconnected.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            # 加 5 秒 timeout 防止 task cleanup 本身卡住（Fix #164）
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                log.warning("Task cleanup timed out — forcing exit")

    log.info("EvoClaw shut down cleanly.")

    # Self-update: replace current process with a fresh one so updated code is loaded.
    # os.execv() replaces the running process in-place (Unix) or spawns a replacement (Windows).
    if _self_update_requested:
        import sys as _sys_restart
        log.info("Restarting EvoClaw for self-update via os.execv()...")
        os.execv(_sys_restart.executable, [_sys_restart.executable] + _sys_restart.argv)


if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
