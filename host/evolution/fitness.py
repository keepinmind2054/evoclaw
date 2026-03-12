"""
適應度追蹤模組（Fitness Tracking）

生物演化的基礎是「天擇」——表現好的個體留下後代，表現差的被淘汰。
這個模組負責記錄每次 container 執行的結果，計算每個群組的「適應度分數」，
作為後續演化決策（基因組調整、環境提示選擇）的數據基礎。

適應度公式：
  fitness = success_rate × 0.5 + speed_score × 0.3 + reliability × 0.2

  - success_rate：成功完成的比例（最重要）
  - speed_score：回應速度（目標 5 秒，30 秒為下限）
  - reliability：不需重試的穩定度（重試次數越多分越低）
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)

# 速度分數的目標時間（ms）：低於此值 = 滿分
SPEED_TARGET_MS = 5_000
# 速度分數的下限時間（ms）：高於此值 = 0 分
SPEED_FLOOR_MS = 30_000


def record_run(
    jid: str,
    run_id: str,
    response_ms: int,
    retry_count: int = 0,
    success: bool = True,
) -> None:
    """
    記錄一次 container 執行的結果到 evolution_runs 表。

    每次 run_container_agent() 結束後呼叫此函式，
    無論成功或失敗都記錄，讓適應度計算有完整的樣本。

    參數：
      jid          — 群組 JID（唯一識別符）
      run_id       — 本次執行的 UUID（對應 container 名稱）
      response_ms  — 從 container 啟動到取得輸出的總時間（毫秒）
      retry_count  — GroupQueue 為此次執行重試的次數
      success      — container 是否成功完成（找到 OUTPUT 標記且 JSON 有效）
    """
    # 延遲 import 避免循環依賴（evolution 模組在 db 初始化前就被 import）
    from host import db
    try:
        db.record_evolution_run(jid, run_id, response_ms, retry_count, success)
    except Exception as exc:
        # 適應度記錄失敗不應中斷主流程，但必須記錄錯誤以便排查
        log.error("record_run failed (jid=%s): %s", jid, exc)


def compute_fitness(jid: str, window_days: int = 7) -> float:
    """
    計算指定群組在過去 window_days 天內的適應度分數（0.0 ~ 1.0）。

    若資料不足（< 3 筆），回傳中立值 0.5，
    避免少量樣本產生極端分數影響演化決策。

    適應度分解：
      - success_rate (50%)：成功完成的執行比例
      - speed_score (30%)：回應速度，線性正規化到 [0, 1]
      - reliability (20%)：1 / (1 + avg_retries)，重試越多分越低
    """
    from host import db
    try:
        rows = db.get_evolution_runs(jid, window_days)
    except Exception:
        return 0.5

    if len(rows) < 3:
        # 樣本太少，回傳中立值，不做演化決策
        return 0.5

    n = len(rows)

    # 成功率：失敗的 run 會拉低分數
    success_rate = sum(1 for r in rows if r.get("success", 1)) / n

    # 速度分數：線性映射，目標 5 秒 = 1.0，30 秒以上 = 0.0
    # Only include successful runs with positive response times.
    # Failed runs report response_ms=0 (timeout) which would incorrectly score as
    # perfect speed (1.0) due to the formula producing a value > 1.0 before clamping.
    valid_times = [r["response_ms"] for r in rows if r.get("response_ms") and r["response_ms"] > 0 and r.get("success")]
    if valid_times:
        avg_ms = sum(valid_times) / len(valid_times)
        # Fixes #89: use max(0.0, ...) in numerator so sub-target response times
        # score 1.0 (perfect) instead of producing values > 1.0 before clamping.
        # Speed: 0.0 = at/above floor, 1.0 = at/below target
        over_target = max(0.0, avg_ms - SPEED_TARGET_MS)
        speed_score = 1.0 - over_target / (SPEED_FLOOR_MS - SPEED_TARGET_MS)
        speed_score = max(0.0, min(1.0, speed_score))
    else:
        speed_score = 0.5

    # 可靠性：重試次數越多，代表系統不穩定
    avg_retries = sum(r.get("retry_count", 0) for r in rows) / n
    reliability = 1.0 / (1.0 + avg_retries)

    fitness = success_rate * 0.5 + speed_score * 0.3 + reliability * 0.2
    return round(fitness, 4)


def get_system_load() -> float:
    """
    估算當前系統的整體負載（0.0 ~ 1.0）。

    負載由兩個維度合成：
      - 近 5 分鐘的 container 執行數量（多 = 忙碌）
      - 近 5 分鐘的平均回應時間（慢 = 系統承壓）

    用於「表觀遺傳」：系統忙碌時，提示 AI 給出簡短回答，
    避免長時間佔用 container 加劇排隊問題。
    """
    from host import db
    try:
        recent = db.get_recent_run_stats(minutes=5)
    except Exception:
        return 0.0

    if not recent or recent.get("count", 0) == 0:
        return 0.0

    # 20 個並發 run 視為高負載（MAX_CONCURRENT_CONTAINERS 通常設 5，20 = 嚴重排隊中）
    load_from_count = min(1.0, recent["count"] / 20.0)

    # 平均回應 30 秒視為高負載
    avg_ms = recent.get("avg_ms") or 0
    load_from_speed = min(1.0, avg_ms / SPEED_FLOOR_MS)

    return round((load_from_count + load_from_speed) / 2, 4)
