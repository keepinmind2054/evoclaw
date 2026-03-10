"""
群組基因組模組（Group Genome）

生物學中，「基因組」是一個生物體全部遺傳資訊的集合，決定其特性與行為。
EvoClaw 的「群組基因組」是每個聊天群組的行為參數集合，決定 AI 在這個群組的表現方式。

基因組參數：
  - response_style：回答風格（concise / balanced / detailed）
  - formality：正式程度（0.0 = 超輕鬆，1.0 = 非常正式）
  - technical_depth：技術深度（0.0 = 白話，1.0 = 專家術語）

這些參數由演化 daemon 根據使用記錄逐漸調整，
讓不同群組的 AI 行為越來越符合該群組的溝通風格。

「物種分化」概念：
  同一個 EvoClaw，在技術討論群組會越來越技術導向，
  在家庭群組會越來越溫暖親切 — 因為基因組在朝不同方向演化。
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)

# 基因組預設值：中立起點，不偏向任何風格
DEFAULT_GENOME = {
    "response_style": "balanced",   # concise / balanced / detailed
    "formality": 0.5,               # 0.0 ~ 1.0
    "technical_depth": 0.5,         # 0.0 ~ 1.0
    "generation": 0,                # 演化世代計數器
}


def get_genome(jid: str) -> dict:
    """
    取得指定群組的基因組。若尚未建立，回傳預設值（不寫入 DB）。

    首次使用預設值，讓群組從中立點開始演化，
    避免預設偏見影響初期行為。
    """
    from host import db
    try:
        genome = db.get_group_genome(jid)
        if genome:
            return genome
    except Exception as e:
        log.warning(f"Failed to get genome for {jid}: {e}")
    return dict(DEFAULT_GENOME)


def upsert_genome(jid: str, **kwargs) -> None:
    """
    更新群組基因組的一個或多個參數。

    只更新傳入的欄位，其他欄位保持不變，
    讓演化可以針對單一維度調整，不影響其他特性。

    用法：
      upsert_genome("tg:123456", response_style="concise")
      upsert_genome("tg:123456", formality=0.7, technical_depth=0.8)
    """
    from host import db
    try:
        db.upsert_group_genome(jid, **kwargs)
    except Exception as e:
        log.warning(f"Failed to upsert genome for {jid}: {e}")


def evolve_genome_from_fitness(jid: str, fitness: float, avg_response_ms: float) -> None:
    """
    根據適應度和回應時間，自動調整群組基因組。

    演化規則（模擬自然選擇壓力）：
      - 回應很慢（>15秒）且適應度低 → 調整為更簡短的回答風格
      - 回應很快（<5秒）且適應度高 → 可嘗試更詳細的回答

    這是「演化壓力」的體現：環境（用戶耐心）選擇了適合的回答長度。

    參數：
      jid            — 群組 JID
      fitness        — 最近的適應度分數（0.0~1.0）
      avg_response_ms — 最近的平均回應時間（毫秒）
    """
    from host import db
    genome = get_genome(jid)
    current_style = genome.get("response_style", "balanced")
    generation = genome.get("generation", 0)

    new_style = current_style
    if avg_response_ms > 15_000 and fitness < 0.4:
        # 回應慢且效果差 → 朝簡短方向演化（施加「速度選擇壓力」）
        if current_style == "detailed":
            new_style = "balanced"
        elif current_style == "balanced":
            new_style = "concise"
    elif avg_response_ms < 5_000 and fitness > 0.7:
        # 回應快且效果好 → 嘗試更詳細（有餘裕可以提供更多資訊）
        if current_style == "concise":
            new_style = "balanced"
        elif current_style == "balanced":
            new_style = "detailed"

    changed = new_style != current_style
    if changed:
        log.info(f"Genome evolution for {jid}: {current_style} → {new_style} "
                 f"(fitness={fitness:.2f}, avg_ms={avg_response_ms:.0f})")

    upsert_genome(
        jid,
        response_style=new_style,
        generation=generation + 1,
    )

    # 記錄演化歷程
    genome_before = {
        "response_style": current_style,
        "formality": genome.get("formality", 0.5),
        "technical_depth": genome.get("technical_depth", 0.5),
        "generation": generation,
    }
    genome_after = dict(genome_before)
    genome_after["response_style"] = new_style
    genome_after["generation"] = generation + 1

    event_type = "genome_evolved" if changed else "genome_unchanged"
    notes = (
        f"style: {current_style} → {new_style}" if changed
        else f"style unchanged ({current_style}), fitness={fitness:.3f}"
    )
    try:
        db.log_evolution_event(
            jid=jid,
            event_type=event_type,
            generation_before=generation,
            generation_after=generation + 1,
            fitness_score=round(fitness, 4),
            avg_response_ms=round(avg_response_ms, 1),
            genome_before=genome_before,
            genome_after=genome_after,
            notes=notes,
        )
    except Exception as e:
        log.warning(f"Failed to log evolution event for {jid}: {e}")
