"""
表觀遺傳適應模組（Epigenetic Adaptation）

生物學中，「表觀遺傳」（Epigenetics）是指基因序列不變，
但基因的「表現方式」因環境改變而不同。
例如：同樣的 DNA，在壓力環境下某些基因會開啟或關閉。

EvoClaw 的表觀遺傳類比：
  同樣的 CLAUDE.md 設定（基因），根據當下環境（時間、系統負載、群組特性）
  動態附加不同的「行為指引」，讓 AI 在不同情境下有不同表現。

關鍵設計：
  - 不修改用戶的 CLAUDE.md（不改變基因）
  - 只在每次 container 啟動時附加額外提示（改變基因表現）
  - 環境恢復正常時，附加提示自然消失，行為回歸基準
"""

import datetime
import logging
from typing import Optional

log = logging.getLogger(__name__)

# 系統負載門檻：超過此值才啟動「簡短回答」提示
HIGH_LOAD_THRESHOLD = 0.7

# 各時段的語氣建議
TIME_HINTS = {
    "late_night": (0, 6, "現在是深夜，請用輕鬆低調的語氣回答，避免過長的回應。"),
    "morning": (6, 9, "早安！請用清晰簡潔的語氣回答。"),
    "evening": (21, 24, "現在是晚間，可以用輕鬆的語氣回應。"),
}


def get_adaptive_hints(jid: str) -> str:
    """
    根據當下環境狀態，生成動態行為提示字串。

    提示會附加在 system prompt 末尾，用分隔線區分，
    讓 AI 知道這是「環境上下文」而非用戶指令。

    觸發條件（任一符合即加入對應提示）：
      1. 系統負載 > 0.7 → 請求簡短回答
      2. 深夜（0-6 點）→ 輕鬆低調語氣
      3. 早晨（6-9 點）→ 清晰簡潔語氣
      4. 晚間（21-24 點）→ 輕鬆語氣
      5. 週末 → 輕鬆語氣

    若無任何觸發條件，回傳空字串（不添加任何附加提示）。
    """
    hints: list[str] = []

    # ── 1. 系統負載檢查 ────────────────────────────────────────────────────────
    try:
        from host.evolution.fitness import get_system_load
        load = get_system_load()
        if load > HIGH_LOAD_THRESHOLD:
            hints.append(
                f"[系統負載：{load:.0%}] 目前系統較忙碌，請盡量給出簡短精確的回答，"
                "除非用戶明確要求詳細說明。"
            )
    except Exception:
        pass  # 負載檢查失敗不應影響主流程

    # ── 2. 時間感知 ────────────────────────────────────────────────────────────
    now = datetime.datetime.now()
    hour = now.hour

    for _name, (start, end, hint) in TIME_HINTS.items():
        if start <= hour < end:
            hints.append(hint)
            break  # 只套用一個時段提示

    # ── 3. 週末感知 ────────────────────────────────────────────────────────────
    if now.weekday() >= 5:  # 5=Saturday, 6=Sunday
        hints.append("今天是週末，可以用輕鬆愉快的語氣回應。")

    if not hints:
        return ""

    # 格式化：用分隔線與 system prompt 主體區分，讓 AI 清楚這是環境注入
    lines = "\n".join(f"• {h}" for h in hints)
    return f"\n\n---\n[環境自動調整提示（Epigenetic Hints）]\n{lines}"


def get_genome_style_hints(jid: str) -> str:
    """
    根據群組基因組（behavioral genome）產生風格提示。

    基因組記錄了這個群組的「演化偏好」：
      - response_style：concise / balanced / detailed
      - formality：正式程度 0.0~1.0
      - technical_depth：技術深度 0.0~1.0

    當基因組參數偏離中立值（0.5）時，才產生對應提示，
    避免在無資料的情況下添加無意義的指引。
    """
    try:
        from host.evolution.genome import get_genome
        genome = get_genome(jid)
    except Exception:
        return ""

    hints: list[str] = []

    # 回答風格：只有明確偏向時才提示（balanced = 不干預）
    style = genome.get("response_style", "balanced")
    if style == "concise":
        hints.append("根據此群組的使用習慣，請保持簡短精準的回答。")
    elif style == "detailed":
        hints.append("根據此群組的使用習慣，請提供完整詳盡的說明。")

    # 正式程度：偏差超過 0.2 才觸發（避免過度干預）
    formality = genome.get("formality", 0.5)
    if formality > 0.7:
        hints.append("請使用正式、專業的語氣。")
    elif formality < 0.3:
        hints.append("請使用輕鬆、親切的語氣，不需要太正式。")

    # 技術深度：偏差超過 0.2 才觸發
    tech = genome.get("technical_depth", 0.5)
    if tech > 0.7:
        hints.append("此群組的用戶對技術細節感興趣，可以使用專業術語和深入解釋。")
    elif tech < 0.3:
        hints.append("請用淺顯易懂的方式解釋，盡量避免過多技術術語。")

    if not hints:
        return ""

    lines = "\n".join(f"• {h}" for h in hints)
    gen = genome.get("generation", 0)
    return f"\n\n---\n[群組偏好（第 {gen} 代基因組）]\n{lines}"
