"""
EvoClaw 演化引擎（Evolution Engine）

將生物演化的核心概念對應到 AI 助理系統：

  自然選擇   → fitness.py   記錄每次執行結果，計算適應度
  表觀遺傳   → adaptive.py  根據環境動態調整行為（不改基因）
  物種分化   → genome.py    每個群組演化出獨特的行為基因組
  免疫系統   → immune.py    偵測威脅，形成記憶，自動防禦
  演化週期   → daemon.py    每 24 小時執行一次選擇壓力

公開 API（供其他模組 import）：
  from host.evolution import (
      record_run,        # 記錄 container 執行結果
      get_adaptive_hints, # 取得環境感知提示
      get_genome_style_hints, # 取得群組風格提示
      check_message,     # 免疫系統訊息檢查
      evolution_loop,    # 演化 daemon 主迴圈
  )
"""

from host.evolution.fitness import record_run
from host.evolution.adaptive import get_adaptive_hints, get_genome_style_hints
from host.evolution.immune import check_message, get_immune_status
from host.evolution.daemon import evolution_loop
from host.evolution.genome import is_genome_valid, reset_genome

__all__ = [
    "record_run",
    "get_adaptive_hints",
    "get_genome_style_hints",
    "check_message",
    "get_immune_status",
    "evolution_loop",
    "is_genome_valid",
    "reset_genome",
]
