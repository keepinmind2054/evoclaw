# EvoClaw 文件目錄

本目錄收錄 EvoClaw 的所有技術文件。

---

## 核心文件

| 文件 | 用途 | 更新頻率 |
|------|------|---------|
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 系統整體架構、Phase 路線圖、設計原則（目前 v2.1） | 每個 Phase 更新 |
| [CHANGELOG.md](./CHANGELOG.md) | 版本變更記錄，含每個 PR 的詳細修復清單 | 每次發版更新 |
| [SPEC.md](./SPEC.md) | 原始技術規格，涵蓋所有子系統的詳細設計 | 穩定，歷史參考 |
| [REQUIREMENTS.md](./REQUIREMENTS.md) | 專案起源、設計哲學、原始需求（為什麼這個專案存在） | 穩定，歷史參考 |

---

## 分析文件

| 文件 | 用途 | 關係 |
|------|------|------|
| [STABILITY_ANALYSIS.md](./STABILITY_ANALYSIS.md) | 穩定性問題追蹤文件。列出每個問題的狀態（✅ 已修復 / ❌ 待修復），附 PR 號。目前 v2.1，追蹤至 Phase 21。 | 精簡版，持續更新 |
| [ANALYSIS_REPORT_2026.md](./ANALYSIS_REPORT_2026.md) | EvoClaw vs NanoClaw 全面深度技術比較。由 4 個平行 AI 分析師代理獨立分析，涵蓋虛假回應根源（A節）、Host 穩定性（B節）、Container 架構（C節）、NanoClaw 對比（D節）。基準：v1.26.0（Phase 19）。 | 詳細版，一次性深度分析 |

> **兩份文件的關係**：`ANALYSIS_REPORT_2026.md` 是深度技術分析的原始報告，`STABILITY_ANALYSIS.md` 是根據分析結果持續追蹤修復進度的工作文件。閱讀建議：先看 STABILITY_ANALYSIS 了解現況，再查 ANALYSIS_REPORT 理解某個問題的深層原因。

---

## 系統設計文件

| 文件 | 用途 |
|------|------|
| [SKILLS_ARCHITECTURE.md](./SKILLS_ARCHITECTURE.md) | Skills 系統完整設計規格。說明 EvoClaw 如何用 git-native 合併機制（三層解決模型：git → Claude Code → 人工）讓使用者安全地組合、更新、移除 skills，同時保留自訂修改。 |
| [SECURITY.md](./SECURITY.md) | 安全模型說明。涵蓋信任層級、Docker 隔離邊界、secrets 處理方式、RBAC 設計。 |
| [SDK_DEEP_DIVE.md](./SDK_DEEP_DIVE.md) | Claude Agent SDK 深度技術分析。逆向工程 `@google/genai` v0.2.29–0.2.34，說明 `query()` 工作原理、子 agent 被終止的原因與修復方式。 |

---

## 運維文件

| 文件 | 用途 |
|------|------|
| [DEBUG_CHECKLIST.md](./DEBUG_CHECKLIST.md) | 常見問題除錯清單，含已知問題（含已修復）與對應排查步驟。 |
| [APPLE-CONTAINER-NETWORKING.md](./APPLE-CONTAINER-NETWORKING.md) | macOS 上使用 Apple Container 的網路設定指南（vmnet 配置）。需要讓容器存取網際網路時參考。 |
| [test-scenarios.md](./test-scenarios.md) | 系統測試情境文件，涵蓋各模組的整合測試案例。 |

---

## 已合併 / 歸檔

以下文件已在整理時合併至其他文件：

| 原檔案 | 合併去向 | 原因 |
|--------|---------|------|
| `nanoclaw-architecture-final.md` | → [SKILLS_ARCHITECTURE.md](./SKILLS_ARCHITECTURE.md) | 更名並加入概覽前言 |
| `nanorepo-architecture.md` | → [SKILLS_ARCHITECTURE.md](./SKILLS_ARCHITECTURE.md) | 與 nanoclaw-architecture-final.md 內容高度重疊，精簡摘要已整合 |

---

*最後更新：2026-03-24*
