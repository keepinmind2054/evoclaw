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

# 收斂停止閾值：若正式程度已在目標值的 1% 以內，停止更新（避免無限振盪）
_CONVERGENCE_EPSILON = 0.01


def _safe_float(value, default: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """
    安全地將資料庫讀取的值轉換為 float，並限制在合法範圍內。
    若值為 None 或無法轉換，回傳預設值。
    """
    try:
        result = float(value)
        return max(min_val, min(max_val, result))
    except (TypeError, ValueError):
        return default


def update_formality(formality: float, target: float = 0.5) -> float:
    """
    將正式程度向目標值靠攏一步。
    若已在目標值的 _CONVERGENCE_EPSILON 以內，直接回傳（停止振盪）。
    """
    FORMALITY_STEP = 0.05
    if abs(formality - target) < _CONVERGENCE_EPSILON:
        return formality  # Already converged, don't oscillate
    return formality + FORMALITY_STEP * (target - formality)


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
            # Validate and clamp float values read from DB to prevent crashes on NULL/invalid data
            return {
                "response_style": genome.get("response_style", DEFAULT_GENOME["response_style"]),
                "formality": _safe_float(genome.get("formality"), default=0.5),
                "technical_depth": _safe_float(genome.get("technical_depth"), default=0.5),
                "generation": genome.get("generation", 0),
            }
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
    except Exception as exc:
        log.error("upsert_genome failed (jid=%s): %s", jid, exc)


def is_genome_valid(genome: dict) -> bool:
    """
    Validate that a genome dict has sane values.

    Returns False (bad genome) if:
      - formality or technical_depth is outside [0, 1]
      - response_style is not one of the allowed values
      - generation is negative
    """
    valid_styles = {"concise", "balanced", "detailed"}
    try:
        formality = float(genome.get("formality", 0.5))
        tech_depth = float(genome.get("technical_depth", 0.5))
        if not (0.0 <= formality <= 1.0):
            return False
        if not (0.0 <= tech_depth <= 1.0):
            return False
        if genome.get("response_style", "balanced") not in valid_styles:
            return False
        if int(genome.get("generation", 0)) < 0:
            return False
    except (TypeError, ValueError):
        return False
    return True


def reset_genome(jid: str) -> None:
    """
    Reset a group's genome to defaults (used when a bad/corrupted genome is detected).

    Logs a warning and writes DEFAULT_GENOME values back to the DB, preserving the
    existing generation counter so evolution history is not lost.
    """
    from host import db
    try:
        existing = db.get_group_genome(jid)
        gen = 0
        if existing:
            try:
                gen = max(0, int(existing.get("generation", 0)))
            except (TypeError, ValueError):
                gen = 0
        log.warning("reset_genome: resetting bad genome for jid=%s (was: %s)", jid, existing)
        db.upsert_group_genome(
            jid,
            response_style=DEFAULT_GENOME["response_style"],
            formality=DEFAULT_GENOME["formality"],
            technical_depth=DEFAULT_GENOME["technical_depth"],
            generation=gen,
        )
        log.info("reset_genome: genome reset to defaults for jid=%s (generation=%d)", jid, gen)
    except Exception as exc:
        log.error("reset_genome failed for jid=%s: %s", jid, exc)


def evolve_genome_from_fitness(jid: str, fitness: float, avg_response_ms: float) -> None:
    """
    根據適應度和回應時間，自動調整群組基因組（全三維演化）。

    演化規則（模擬自然選擇壓力）：
      - response_style：回應慢且效果差 → 更簡短；回應快且效果好 → 更詳細
      - formality：高適應度+快速 → 更正式；低適應度 → 朝中性（0.5）收斂
      - technical_depth：高適應度+快速 → 增加深度；極慢或低適應度 → 減少深度

    參數：
      jid            — 群組 JID
      fitness        — 最近的適應度分數（0.0~1.0）
      avg_response_ms — 最近的平均回應時間（毫秒）
    """
    # BUG-FIX(p18b-06): clamp fitness and avg_response_ms at the entry point.
    # compute_fitness() already clamps its output but callers (e.g. tests, future
    # code paths) might pass out-of-range values.  A fitness > 1.0 would satisfy
    # both the "> 0.7" (high) AND never the "< 0.4" (low) branches, silently
    # pushing all three genome axes in the "good" direction regardless of reality.
    # A negative avg_response_ms is physically meaningless and would trigger the
    # "< 5000 ms fast" branch unconditionally.
    try:
        fitness = max(0.0, min(1.0, float(fitness)))
    except (TypeError, ValueError):
        log.warning("evolve_genome_from_fitness: invalid fitness %r for %s — using 0.5", fitness, jid)
        fitness = 0.5
    try:
        avg_response_ms = max(0.0, float(avg_response_ms))
    except (TypeError, ValueError):
        log.warning("evolve_genome_from_fitness: invalid avg_response_ms %r for %s — using 0", avg_response_ms, jid)
        avg_response_ms = 0.0

    from host import db
    genome = get_genome(jid)
    generation = genome.get("generation", 0)
    response_style = genome.get("response_style", "balanced")
    formality = float(genome.get("formality", 0.5))
    technical_depth = float(genome.get("technical_depth", 0.5))

    # Evolve response_style
    style_order = ["concise", "balanced", "detailed"]
    # p24c: if the stored response_style is not a valid value (e.g. DB corruption),
    # normalise it to "balanced" (idx=1) rather than carrying the corrupted value
    # forward into new_style via the `else` branch below.
    if response_style not in style_order:
        log.warning(
            "evolve_genome_from_fitness: invalid response_style %r for %s — normalising to 'balanced'",
            response_style, jid,
        )
        response_style = "balanced"
    idx = style_order.index(response_style)
    if avg_response_ms > 15_000 and fitness < 0.4 and idx > 0:
        new_style = style_order[idx - 1]
    elif avg_response_ms < 5_000 and fitness > 0.7 and idx < 2:
        new_style = style_order[idx + 1]
    else:
        new_style = response_style

    # Evolve formality: nudge toward target based on fitness.
    # Fix p11d: both upward (target=0.7) and downward (target=0.5) nudges now use
    # update_formality() which applies a proportional step with convergence-stop.
    # Previously the upward path used a fixed +0.05 step while the downward path used
    # a proportional step, causing asymmetric pressure that made formality creep to 1.0
    # whenever fitness alternated around the 0.7/0.4 thresholds over many cycles.
    if fitness > 0.7 and avg_response_ms < 8000:
        # Nudge toward 0.7 (confident/formal), with convergence stop
        formality = update_formality(formality, target=0.7)
    elif fitness < 0.4:
        # Nudge toward neutral (0.5), with convergence stop to prevent infinite oscillation
        formality = update_formality(formality, target=0.5)
    formality = round(max(0.0, min(1.0, formality)), 3)

    # Evolve technical_depth: increase when responses are fast and successful
    # Decrease when responses are slow (user may be confused by complexity)
    DEPTH_STEP = 0.05
    if fitness > 0.7 and avg_response_ms < 6000:
        technical_depth = min(1.0, technical_depth + DEPTH_STEP)
    elif avg_response_ms > 20_000 or fitness < 0.3:
        technical_depth = max(0.0, technical_depth - DEPTH_STEP)
    technical_depth = round(max(0.0, min(1.0, technical_depth)), 3)

    style_changed = new_style != response_style
    if style_changed:
        log.info(f"Genome evolution for {jid}: style {response_style} → {new_style} "
                 f"(fitness={fitness:.2f}, avg_ms={avg_response_ms:.0f})")
    log.debug(
        f"Genome evolution for {jid}: formality={formality:.3f} "
        f"technical_depth={technical_depth:.3f} "
        f"(fitness={fitness:.2f}, avg_ms={avg_response_ms:.0f})"
    )

    upsert_genome(
        jid,
        response_style=new_style,
        formality=formality,
        technical_depth=technical_depth,
        generation=generation + 1,
    )

    # 記錄演化歷程
    genome_before = {
        "response_style": response_style,
        "formality": genome.get("formality", 0.5),
        "technical_depth": genome.get("technical_depth", 0.5),
        "generation": generation,
    }
    genome_after = {
        "response_style": new_style,
        "formality": formality,
        "technical_depth": technical_depth,
        "generation": generation + 1,
    }

    event_type = "genome_evolved" if style_changed else "genome_unchanged"
    notes = (
        f"style: {response_style} → {new_style}, formality={formality:.3f}, technical_depth={technical_depth:.3f}"
        if style_changed
        else f"style unchanged ({response_style}), formality={formality:.3f}, technical_depth={technical_depth:.3f}, fitness={fitness:.3f}"
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
