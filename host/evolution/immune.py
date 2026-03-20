"""免疫系統模組（Immune System）

生物的免疫系統能識別「非自身」的入侵者（病毒、細菌），
並在初次接觸後形成「記憶」，下次遇到相同威脅時快速反應。

EvoClaw 的免疫系統類比：
- 偵測惡意的 prompt injection（「忽略之前的指令」等攻擊手法）
- 偵測重複垃圾訊息（同一內容短時間內大量發送）
- 初次偵測記錄「抗體」（threat pattern hash）
- 累積到門檻後自動封鎖發送者（免疫記憶啟動）

設計原則：
- 寧可放行可疑訊息，也不誤殺正常訊息（低誤判率優先）
- check_message 失敗時靜默放行，不影響正常對話流程
- 封鎖狀態儲存在 SQLite，重啟後仍然有效（持久免疫記憶）
"""
import hashlib
import logging
import re
import sqlite3
from typing import Optional

log = logging.getLogger(__name__)

# ── Prompt Injection 偵測模式 ───────────────────────────────────────────────────
# 這些是常見的 LLM 越獄攻擊手法，用正規表達式匹配
# 故意保持保守（只匹配明確惡意的模式），減少誤判
# 涵蓋英文與中文兩種攻擊語言
INJECTION_PATTERNS = [
    # ── 英文攻擊模式 ──────────────────────────────────────────────────────────
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?",
    r"forget\s+(everything|all\s+previous|your\s+instructions?)",
    r"you\s+are\s+now\s+(a\s+|an\s+)?\w+\s*(mode|ai|bot|assistant)?",
    r"new\s+system\s+prompt\s*[:：]",
    r"act\s+as\s+if\s+you\s+(have\s+no|are\s+not)",
    r"jailbreak",
    r"\bDAN\b",  # DAN / DAN mode
    r"<\|system\|>",
    r"\[INST\]\s*<<SYS>>",
    r"disregard\s+(your\s+)?(previous\s+)?(instructions?|rules?|guidelines?)",
    r"you\s+are\s+(not\s+)?(bound|restricted|limited)\s+by",
    r"bypass\s+(all\s+)?(restrictions?|rules?|safety)",
    r"enter\s+(developer|debug|admin)\s+mode",
    r"switch\s+to\s+(developer|debug|admin)\s+mode",
    # 新增：更多英文攻擊變體
    r"override\s+(your\s+)?(previous|prior)\s+instructions?",
    r"from\s+now\s+on\s+(you\s+are|you\s+will)",
    r"pretend\s+to\s+be\s+(a\s+|an\s+)?\w+",
    r"roleplay\s+as\s+(a\s+|an\s+)?\w+",
    r"comply\s+with\s+the\s+following\s+rules",
    r"here\s+is\s+the\s+new\s+system\s+prompt",
    r"you\s+must\s+follow\s+these\s+new\s+instructions",
    r"safety\s+protocols?\s+(are\s+)?(disabled|off|ignored)",
    r"ethics?\s+(are\s+)?(disabled|off|ignored)",
    r"no\s+(limits?|restrictions?|rules?)",
    
    # ── 中文攻擊模式（使用簡單關鍵詞匹配）────────────────────────────────────
    # 要求祈使句形式＋明確的指令/規則上下文，避免誤判正常中文句子
    # 例：「我忽略了他之前的建議」不應被匹配
    # Fix p11d: added (?:所有)? after 你的 to match "忽略你的所有規則" (所有 sits between 你的 and target)
    r"(?:請|你要|必須)?忽略(?:你的(?:所有)?|你所有的|之前所有的|前面所有的)(?:指令|規則|限制|設定)",
    # Fix p11d: added 系統提示 to forget-target list so "忘記你的系統提示" is caught
    r"(?:忘記|忘掉|拋棄|丟掉)(?:你的|你所有的|之前的|先前的)?(?:規則|限制|指令|設定|約束|系統提示)",
    r"(?:你現在|从現在起)(?:没有限制|不受限制|無任何限制)",
    r"(?:越獄|破解|繞過)(?:你的|系統的)?(?:限制|規則|設定|安全機制)",
    r"(?:新的|更新的)系統(?:提示|指令|設定)\s*[:：]",
    r"假裝(?:你)?(?:沒有|不受|無視)(?:任何)?(?:限制|規則|指令)",
    r"你(?:現在)?(?:不是|不再是)(?:AI|助手|模型|機器人)",
    r"(?:解除|關閉|停用)(?:你的)?(?:安全|限制|審查)(?:機制|設定|協議)",
    r"(?:进入|切換到)(?:開發者|管理員|調試)模式",
    r"不要遵守(?:你的|任何)?(?:規則|限制|指引|指令)",
    r"無視(?:你的|你所有的|之前所有的|先前所有的)(?:指示|命令|要求|規則|指令)",
    # 新增：更多中文攻擊變體（已收緊，需要明確的注入上下文）
    r"覆蓋(?:你的|之前的|先前的)(?:指令|規則|設定)",
    r"从現在開始(?:你是|你將扮演|你要假裝)",
    r"假扮(?:成)?(?:沒有|不受)(?:任何)?(?:限制|約束)的",
    r"角色扮演(?:成)?(?:沒有|不受)(?:任何)?(?:限制|約束)的",
    r"遵守(?:以下|這些)新(?:規則|指令|設定)",
    r"這是(?:新的|更新後的)系統(?:提示|指令|設定)",
    r"你必須遵(?:循|守)(?:這些|以下)新(?:指令|規則)",
    r"安全(?:機制|協議|限制)(?:已|被)(?:關閉|停用|禁用|無效化)",
    r"道德(?:規範|限制)(?:已|被)(?:關閉|停用|禁用|無效化)",
    r"沒有任何(?:限制|約束|規範|禁止)",
]

# 預先編譯正規表達式以提升效能（每次請求都會呼叫）
# 注意：中文模式不需要 IGNORECASE，但加上無副作用
_compiled_patterns = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]

# 垃圾訊息門檻：同一發送者在 1 小時內傳送相同內容超過此次數 = 垃圾訊息
SPAM_THRESHOLD = 10

# 自動封鎖門檻：同一發送者累積超過此次數的威脅記錄 = 自動封鎖
THREAT_BLOCK_THRESHOLD = 5


def check_message(content: str, sender_jid: str) -> tuple[bool, Optional[str]]:
    """
    檢查一則訊息是否安全。

    回傳值：
    - (True, None) — 訊息安全，可以處理
    - (False, "blocked") — 發送者已被封鎖
    - (False, "injection") — 偵測到 prompt injection 攻擊
    - (False, "spam") — 偵測到垃圾訊息重複攻擊

    此函式設計為「防禦優先但保守」：
    只封鎖有明確惡意特徵的訊息，
    模糊案例一律放行（False Negative 比 False Positive 代價小）。
    若資料庫操作失敗，靜默回傳安全（True），
    避免 DB 故障導致整個系統無法回應訊息。
    """
    # Fixes #93: empty sender_jid bypassed all rate-limit checks silently.
    if not sender_jid:
        log.debug("immune.check_message: empty sender_jid — skipping check")
        return (False, "empty_sender")

    try:
        # ── 1. 檢查發送者是否已被封鎖 ────────────────────────────────────────
        from host import db

        if db.is_sender_blocked(sender_jid):
            log.info(f"Blocked sender attempted to message: {sender_jid}")
            return (False, "blocked")

        # ── 2. Prompt Injection 偵測 ──────────────────────────────────────────
        content_stripped = content.strip()
        for pattern in _compiled_patterns:
            if pattern.search(content_stripped):
                log.warning(
                    f"Prompt injection detected from {sender_jid}: "
                    f"pattern={pattern.pattern[:40]}"
                )
                _record_threat(sender_jid, content, "injection")
                return (False, "injection")

        # ── 3. 垃圾訊息偵測 ──────────────────────────────────────────────────
        # 先記錄此訊息（不論是否為威脅），讓所有訊息都能被 spam 計數器追蹤
        content_hash = _hash(content)
        _track_message(sender_jid, content_hash)
        if _is_spam(sender_jid, content_hash):
            log.warning(f"Spam detected from {sender_jid}")
            _record_threat(sender_jid, content, "spam")
            return (False, "spam")

        return (True, None)
    except sqlite3.OperationalError as exc:
        # Fix #120: distinguish transient DB lock from permanent errors.
        # A brief DB lock (e.g. prune_old_logs running) should NOT blackout all group messages —
        # that violates availability for all users in the group.
        # Only permanent / unrecoverable errors (corrupted DB, I/O failure) should fail-secure.
        exc_str = str(exc).lower()
        if "database is locked" in exc_str or "busy" in exc_str:
            log.warning("immune check_message: transient DB lock — allowing message: %s", exc)
            return (True, None)
        log.error("immune check_message permanent DB error: %s", exc)
        return (False, "immune_check_error")
    except Exception as exc:
        # Fix #108: fail-secure — deny on unexpected DB error rather than fail-open.
        # A DB outage must not silently bypass the immune check and let unvetted messages through.
        log.error("immune check_message DB error: %s", exc)
        return (False, "immune_check_error")


def get_immune_status() -> dict:
    """
    取得免疫系統的摘要統計資訊。
    可透過 IPC 工具讓主群組查詢，用於監控安全狀態。
    """
    from host import db

    try:
        return db.get_immune_stats()
    except Exception:
        return {"total_threats": 0, "blocked_senders": 0}


# ── 私有輔助函式 ────────────────────────────────────────────────────────────────


def _hash(content: str) -> str:
    """計算訊息內容的 SHA-256 hash，用於快速比對重複訊息（不需儲存原文）。
    SHA-256 is used instead of MD5 to prevent hash-collision attacks that could
    allow adversaries to bypass the spam counter or poison the threat database."""
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


def _track_message(sender_jid: str, content_hash: str) -> None:
    """
    每則訊息到來時都記錄其 hash（不論是否為威脅），
    讓 _is_spam 的計數器能正確偵測正常訊息的重複發送。
    若 DB 操作失敗，靜默忽略（不影響正常對話流程）。
    """
    from host import db

    try:
        db.record_immune_threat(sender_jid, content_hash, "seen")
    except Exception as e:
        log.debug(f"Failed to track message: {e}")


def _is_spam(sender_jid: str, content_hash: str) -> bool:
    """
    檢查某個發送者是否在近 1 小時內重複傳送相同內容超過門檻次數。
    使用 content hash 而非原文比較，保護用戶隱私（DB 不儲存完整訊息內容）。
    1 小時的滑動視窗避免誤判（例如用戶習慣說「謝謝」不算垃圾訊息）。
    """
    from host import db

    try:
        count = db.get_recent_threat_count(sender_jid, content_hash, hours=1)
        return count >= SPAM_THRESHOLD
    except Exception:
        return False


def _record_threat(sender_jid: str, content: str, threat_type: str) -> None:
    """
    記錄一次威脅事件，並在累積超過門檻時自動封鎖發送者。
    威脅記錄使用 content hash 去重：同一內容的重複威脅會更新計數而非新增記錄，
    避免資料庫被同一攻擊者的大量重複請求撐爆。
    """
    from host import db

    try:
        content_hash = _hash(content)
        threat_count = db.record_immune_threat(sender_jid, content_hash, threat_type)
        # 累積威脅超過門檻 → 自動封鎖（免疫記憶：形成「抗體」後直接阻擋）
        if threat_count >= THREAT_BLOCK_THRESHOLD:
            db.block_sender(sender_jid)
            log.warning(f"Auto-blocked sender {sender_jid} after {threat_count} threats")
    except Exception as e:
        log.debug(f"Failed to record threat: {e}")
