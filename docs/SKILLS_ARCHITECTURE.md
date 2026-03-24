# EvoClaw Skills Architecture

> 本文件由 `nanoclaw-architecture-final.md`（詳細規格）與 `nanorepo-architecture.md`（概覽）合併而成。

---

## 什麼是 Skills

EvoClaw 的核心刻意保持最小化。Skills 是使用者擴充它的方式：新增頻道、整合第三方服務、切換平台支援，或完全替換內部元件。

例子：新增 Telegram（在 WhatsApp 旁邊）、從 Apple Container 切換到 Docker、新增 Gmail 整合、新增語音訊息轉錄。

每個 skill **直接修改實際代碼**——新增 channel handlers、更新 message router、變更 container 設定、新增依賴——而非透過 plugin API 或 runtime hooks 運作。

---

## 為什麼要這樣設計

**問題**：使用者需要在共享代碼庫上組合多個修改、在核心更新後保持這些修改正常運作，並且不需要成為 git 專家或失去自訂的變更。

Plugin 系統更簡單，但限制了 skills 能做的事。給 skills 完整的代碼庫存取意味著它們可以改任何東西，但這帶來合併衝突、更新破損和狀態追蹤挑戰。

**解法**：讓 skill 應用完全程式化，使用標準 git mechanics，以 AI 作為 git 無法解決衝突的後備，並用共享解決方案快取讓大多數使用者永遠不會碰到這些衝突。

結果：使用者組合他們想要的功能，自訂變更在核心更新後自動存活，系統始終可恢復。

---


## 核心原則

Skills 是自包含、可審計的套件，透過標準 git 合併機制以程式化方式套用。Claude Code 負責協調整個流程——執行 git 指令、讀取 skill 清單、並在 git 無法自行解決衝突時介入。系統使用現有的 git 功能（`merge-file`、`rerere`、`apply`），而非自行建立合併基礎設施。

### 三層解決模型

系統中的每個操作都遵循以下升級順序：

1. **Git** — 確定性、程式化。`git merge-file` 執行合併，`git rerere` 重播快取的解決方案，結構化操作無需合併即可套用。不涉及 AI。這處理了絕大多數情況。
2. **Claude Code** — 讀取 `SKILL.md`、`.intent.md`、遷移指南與 `state.yaml` 以理解上下文。解決 git 無法以程式化方式處理的衝突。透過 `git rerere` 快取解決方案，使同樣的衝突無需再次解決。
3. **使用者** — 當 Claude Code 缺乏上下文或意圖時，會詢問使用者。這發生在兩個功能在應用層面真正衝突（而非僅是文字層面的合併衝突），且需要人工判斷期望行為時。

目標是：在成熟、經充分測試的安裝環境中，第一層處理所有情況。第二層處理首次衝突和邊緣案例。第三層很少發生，僅適用於真正模糊的情況。

**重要**：乾淨的合併（退出碼 0）並不保證代碼能正常運作。語意衝突——重新命名的變數、移位的引用、變更的函式簽名——可能產生乾淨的文字合併，但在執行時卻會出錯。**每次操作後都必須執行測試**，無論合併是否乾淨。乾淨的合併但測試失敗，將升級至第二層。

### 透過備份/還原確保操作安全

許多使用者在沒有 fork 的情況下複製了代碼庫，不提交變更，也不認為自己是 git 使用者。系統必須在不要求任何 git 知識的情況下，對他們安全地運作。

在任何操作之前，系統會將所有將被修改的檔案複製到 `.evoclaw/backup/`。成功時，備份將被刪除。失敗時，備份將被還原。這提供了回滾安全性，無論使用者是否提交、推送或理解 git。

---

## 1. 共享基礎

`.evoclaw/base/` 存放乾淨的核心——套用任何 skills 或自訂前的原始代碼庫。這是所有三方合併的穩定公共祖先，僅在核心更新時變更。

- `git merge-file` 使用此基礎計算兩個差異：使用者的變更（當前 vs 基礎）與 skill 想要的變更（基礎 vs skill 的修改檔案），然後將兩者合併
- 基礎啟用漂移偵測：如果某個檔案的雜湊與其基礎雜湊不同，則某些內容已被修改（skills、使用者自訂，或兩者都有）
- 每個 skill 的 `modify/` 檔案包含套用該 skill 後檔案應有的完整內容（包含任何先決 skill 的變更），全部針對相同的乾淨核心基礎撰寫

在**全新代碼庫**中，使用者的檔案與基礎相同。這意味著 `git merge-file` 對第一個 skill 總是乾淨退出——合併輕鬆產生 skill 的修改版本。無需特殊處理。

當多個 skills 修改同一個檔案時，三方合併自然地處理重疊部分。如果 Telegram 和 Discord 都修改了 `src/index.ts`，且兩個 skill 檔案都包含 Telegram 的變更，那些共同的變更會乾淨地與基礎合併。結果是基礎 + 所有 skill 變更 + 使用者自訂。

---

## 2. 兩種變更類型：代碼合併與結構化操作

並非所有檔案都應作為文字合併。系統區分**代碼檔案**（透過 `git merge-file` 合併）和**結構化資料**（透過確定性操作修改）。

### 代碼檔案（三方合併）

Skills 在其中織入邏輯的原始碼檔案——路由處理器、中介軟體、業務邏輯。這些使用 `git merge-file` 針對共享基礎進行合併。Skill 攜帶檔案的完整修改版本。

### 結構化資料（確定性操作）

像 `package.json`、`docker-compose.yml`、`.env.example` 和生成的設定檔這類檔案，不是要合併的代碼——它們是要聚合的結構化資料。多個 skills 向 `package.json` 新增 npm 依賴，不應需要三方文字合併。相反，skills 在清單中宣告其結構化需求，系統以程式化方式套用它們。

**結構化操作是隱式的。** 如果 skill 宣告了 `npm_dependencies`，系統會自動處理依賴安裝。Skill 作者無需在 `post_apply` 中新增 `npm install`。當多個 skills 依序套用時，系統會批次處理結構化操作：先合併所有依賴宣告，寫入一次 `package.json`，最後執行一次 `npm install`。

```yaml
# In manifest.yaml
structured:
  npm_dependencies:
    whatsapp-web.js: "^2.1.0"
    qrcode-terminal: "^0.12.0"
  env_additions:
    - WHATSAPP_TOKEN
    - WHATSAPP_VERIFY_TOKEN
    - WHATSAPP_PHONE_ID
  docker_compose_services:
    whatsapp-redis:
      image: redis:alpine
      ports: ["6380:6379"]
```

### 結構化操作衝突

結構化操作消除了文字合併衝突，但仍可能在語意層面發生衝突：

- **NPM 版本衝突**：兩個 skills 為同一個套件請求不相容的 semver 範圍
- **連接埠衝突**：兩個 docker-compose 服務佔用相同的主機連接埠
- **服務名稱衝突**：兩個 skills 定義了具有相同名稱的服務
- **環境變數重複**：兩個 skills 以不同期望宣告了相同的變數

解決策略：

1. **盡可能自動處理**：擴大 semver 範圍以找到相容版本，偵測並標記連接埠/名稱衝突
2. **第二層（Claude Code）**：如果自動解決失敗，Claude 根據 skill 意圖提出選項
3. **第三層（使用者）**：如果這是真正的產品選擇（哪個 Redis 實例應獲得 6379 連接埠？），詢問使用者

結構化操作衝突與代碼檔案重疊一起列入 CI 重疊圖，使維護者的測試矩陣能在使用者遇到之前發現這些問題。

### State 記錄結構化結果

`state.yaml` 不僅記錄宣告的依賴，還記錄已解決的結果——實際安裝的版本、已解決的連接埠分配、最終環境變數列表。這使結構化操作可重播且可審計。

### 確定性序列化

所有結構化輸出（YAML、JSON）使用穩定序列化：排序鍵、一致引用、規範化空白。這防止了 git 歷史記錄中因非功能性格式變更產生的雜亂差異。

---

## 3. Skill 套件結構

Skill 只包含它新增或修改的檔案。對於修改的代碼檔案，skill 攜帶**完整的修改檔案**（套用了 skill 變更的乾淨核心）。

```
skills/
  add-whatsapp/
    SKILL.md                          # Context, intent, what this skill does and why
    manifest.yaml                     # Metadata, dependencies, env vars, post-apply steps
    tests/                            # Integration tests for this skill
      whatsapp.test.ts
    add/                              # New files — copied directly
      src/channels/whatsapp.ts
      src/channels/whatsapp.config.ts
    modify/                           # Modified code files — merged via git merge-file
      src/
        server.ts                     # Full file: clean core + whatsapp changes
        server.ts.intent.md           # "Adds WhatsApp webhook route and message handler"
        config.ts                     # Full file: clean core + whatsapp config options
        config.ts.intent.md           # "Adds WhatsApp channel configuration block"
```

### 為何使用完整修改檔案

- `git merge-file` 需要三個完整檔案——無需中間重建步驟
- Git 的三方合併使用上下文匹配，即使使用者移動了代碼也能正常運作——不像基於行號的差異會立即失效
- 可審計：`diff .evoclaw/base/src/server.ts skills/add-whatsapp/modify/src/server.ts` 精確顯示 skill 的變更
- 確定性：相同的三個輸入始終產生相同的合併結果
- 大小可忽略，因為 EvoClaw 的核心檔案很小

### Intent 檔案

每個修改的代碼檔案都有對應的 `.intent.md`，包含結構化標題：

```markdown
# Intent: server.ts modifications

## What this skill adds
Adds WhatsApp webhook route and message handler to the Express server.

## Key sections
- Route registration at `/webhook/whatsapp` (POST and GET for verification)
- Message handler middleware between auth and response pipeline

## Invariants
- Must not interfere with other channel webhook routes
- Auth middleware must run before the WhatsApp handler
- Error handling must propagate to the global error handler

## Must-keep sections
- The webhook verification flow (GET route) is required by WhatsApp Cloud API
```

結構化標題（What、Key sections、Invariants、Must-keep）在衝突解決期間為 Claude Code 提供具體指導，而無需從非結構化文字中推斷。

### 清單格式

```yaml
# --- Required fields ---
skill: whatsapp
version: 1.2.0
description: "WhatsApp Business API integration via Cloud API"
core_version: 0.1.0               # The core version this skill was authored against

# Files this skill adds
adds:
  - src/channels/whatsapp.ts
  - src/channels/whatsapp.config.ts

# Code files this skill modifies (three-way merge)
modifies:
  - src/server.ts
  - src/config.ts

# File operations (renames, deletes, moves — see Section 5)
file_ops: []

# Structured operations (deterministic, no merge — implicit handling)
structured:
  npm_dependencies:
    whatsapp-web.js: "^2.1.0"
    qrcode-terminal: "^0.12.0"
  env_additions:
    - WHATSAPP_TOKEN
    - WHATSAPP_VERIFY_TOKEN
    - WHATSAPP_PHONE_ID

# Skill relationships
conflicts: []              # Skills that cannot coexist without agent resolution
depends: []                # Skills that must be applied first

# Test command — runs after apply to validate the skill works
test: "npx vitest run src/channels/whatsapp.test.ts"

# --- Future fields (not yet implemented in v0.1) ---
# author: evoclaw-team
# license: MIT
# min_skills_system_version: "0.1.0"
# tested_with: [telegram@1.0.0]
# post_apply: []
```

注意：`post_apply` 僅適用於無法表達為結構化宣告的操作。依賴安裝**絕不**放在 `post_apply` 中——它由結構化操作系統隱式處理。

---

## 4. Skills、自訂與分層

### 一個 Skill，一條快樂路徑

Skill 實現**一種做某事的方式——覆蓋 80% 使用者的合理預設值。** `add-telegram` 為你提供乾淨、穩固的 Telegram 整合。它不會試圖透過預定義的設定選項和模式來預見每個使用案例。

### 自訂只是更多的補丁

整個系統圍繞著對代碼庫套用轉換而建立。在套用 skill 後對其進行自訂，與任何其他修改沒有區別：

- **套用 skill** — 獲得標準 Telegram 整合
- **從那裡修改** — 使用自訂流程（追蹤補丁）、直接編輯（透過雜湊追蹤偵測），或套用在其上構建的其他 skills

### 分層 Skills

Skills 可以建立在其他 skills 之上：

```
add-telegram                    # Core Telegram integration (happy path)
  ├── telegram-reactions        # Adds reaction handling (depends: [telegram])
  ├── telegram-multi-bot        # Multiple bot instances (depends: [telegram])
  └── telegram-filters          # Custom message filtering (depends: [telegram])
```

每一層都是一個獨立的 skill，有自己的 `SKILL.md`、清單（包含 `depends: [telegram]`）、測試和修改檔案。使用者透過疊加 skills 來精確組合他們想要的功能。

### 自訂 Skill 套用

使用者可以在一個步驟中套用包含自訂修改的 skill：

1. 正常套用 skill（程式化合併）
2. Claude Code 詢問使用者是否想要進行任何修改
3. 使用者描述他們想要的不同之處
4. Claude Code 在剛套用的 skill 之上進行修改
5. 修改被記錄為與此 skill 關聯的自訂補丁

記錄在 `state.yaml` 中：

```yaml
applied_skills:
  - skill: telegram
    version: 1.0.0
    custom_patch: .evoclaw/custom/telegram-group-only.patch
    custom_patch_description: "Restrict bot responses to group chats only"
```

在重播時，skill 以程式化方式套用，然後自訂補丁套用在其上。

---

## 5. 檔案操作：重新命名、刪除、移動

核心更新和某些 skills 需要重新命名、刪除或移動檔案。這些不是文字合併——它們是作為明確腳本操作處理的結構性變更。

### 在清單中宣告

```yaml
file_ops:
  - type: rename
    from: src/server.ts
    to: src/app.ts
  - type: delete
    path: src/deprecated/old-handler.ts
  - type: move
    from: src/utils/helpers.ts
    to: src/lib/helpers.ts
```

### 執行順序

檔案操作在代碼合併**之前**執行，因為合併需要針對正確的檔案路徑：

1. 預檢（狀態驗證、核心版本、依賴、衝突、漂移偵測）
2. 獲取操作鎖
3. **備份**所有將被觸碰的檔案
4. **檔案操作**（重新命名、刪除、移動）
5. 從 `add/` 複製新檔案
6. 三方合併修改的代碼檔案
7. 衝突解決（rerere 自動解決，或以 `backupPending: true` 返回）
8. 套用結構化操作（npm 依賴、環境變數、docker-compose——批次處理）
9. 執行 `npm install`（一次，如果存在任何結構化的 npm_dependencies）
10. 更新 state（記錄 skill 套用、檔案雜湊、結構化結果）
11. 執行測試（如果定義了 `manifest.test`；失敗時回滾 state + 備份）
12. 清理（成功時刪除備份，釋放鎖）

### Skills 的路徑重映射

當核心重新命名某個檔案時（例如 `server.ts` → `app.ts`），針對舊路徑撰寫的 skills 在其 `modifies` 和 `modify/` 目錄中仍引用 `server.ts`。**Skill 套件絕不在使用者的機器上被修改。**

相反，核心更新附帶一個**相容性映射**：

```yaml
# In the update package
path_remap:
  src/server.ts: src/app.ts
  src/old-config.ts: src/config/main.ts
```

系統在套用時解析路徑：如果 skill 針對 `src/server.ts`，而重映射說它現在是 `src/app.ts`，合併將針對 `src/app.ts` 執行。重映射記錄在 `state.yaml` 中，以便未來的操作保持一致。

### 安全檢查

在執行檔案操作之前：

- 驗證來源檔案存在
- 對於刪除：如果檔案有超出基礎的修改（使用者或 skill 的變更將丟失），則發出警告

---

## 6. 套用流程

當使用者在 Claude Code 中執行 skill 的斜線指令時：

### 步驟 1：預檢

- 核心版本相容性
- 滿足依賴關係
- 與已套用的 skills 無不可解決的衝突
- 檢查未追蹤的變更（見第 9 節）

### 步驟 2：備份

將所有將被修改的檔案複製到 `.evoclaw/backup/`。如果操作在任何時候失敗，從備份還原。

### 步驟 3：檔案操作

執行重新命名、刪除或移動，並進行安全檢查。如需要，套用路徑重映射。

### 步驟 4：套用新檔案

```bash
cp skills/add-whatsapp/add/src/channels/whatsapp.ts src/channels/whatsapp.ts
```

### 步驟 5：合併修改的代碼檔案

對於 `modifies` 中的每個檔案（套用路徑重映射）：

```bash
git merge-file src/server.ts .evoclaw/base/src/server.ts skills/add-whatsapp/modify/src/server.ts
```

- **退出碼 0**：乾淨合併，繼續
- **退出碼 > 0**：檔案中有衝突標記，繼續進行解決

### 步驟 6：衝突解決（三層）

1. **檢查共享解決方案快取**（`.evoclaw/resolutions/`）——如果此 skill 組合存在已驗證的解決方案，載入到本地 `git rerere`。**僅在輸入雜湊完全匹配時套用**（基礎雜湊 + 當前雜湊 + skill 修改雜湊）。
2. **`git rerere`** — 檢查本地快取。如果找到，自動套用。完成。
3. **Claude Code** — 讀取衝突標記 + `SKILL.md` + 當前及之前套用 skills 的 `.intent.md`（Invariants、Must-keep 部分）。解決衝突。`git rerere` 快取解決方案。
4. **使用者** — 如果 Claude Code 無法確定意圖，詢問使用者期望的行為。

### 步驟 7：套用結構化操作

收集所有結構化宣告（來自此 skill 及批次處理時之前套用的 skills）。確定性地套用：

- 將 npm 依賴合併到 `package.json`（檢查版本衝突）
- 將環境變數附加到 `.env.example`
- 合併 docker-compose 服務（檢查連接埠/名稱衝突）
- 在最後執行**一次** `npm install`
- 在 state 中記錄已解決的結果

### 步驟 8：套用後處理與驗證

1. 執行任何 `post_apply` 指令（僅限非結構化操作）
2. 更新 `.evoclaw/state.yaml`——skill 記錄、檔案雜湊（基礎、skill、每個檔案的合併結果）、結構化結果
3. **執行 skill 測試**——強制執行，即使所有合併都是乾淨的
4. 如果乾淨合併但測試失敗 → 升級至第二層（Claude Code 診斷語意衝突）

### 步驟 9：清理

如果測試通過，刪除 `.evoclaw/backup/`。操作完成。

如果測試失敗且第二層無法解決，從 `.evoclaw/backup/` 還原並報告失敗。

---

## 7. 共享解決方案快取

### 問題

`git rerere` 預設是本地的。但 EvoClaw 有數千名使用者套用相同的 skill 組合。每個使用者都遇到相同的衝突並等待 Claude Code 解決，是浪費的。

### 解決方案

EvoClaw 在 `.evoclaw/resolutions/` 中維護一個已驗證的解決方案快取，與專案一起發布。這是共享的工件——**不是** `.git/rr-cache/`（保持本地）。

```
.evoclaw/
  resolutions/
    whatsapp@1.2.0+telegram@1.0.0/
      src/
        server.ts.resolution
        server.ts.preimage
        config.ts.resolution
        config.ts.preimage
      meta.yaml
```

### 雜湊強制執行

快取的解決方案**僅在輸入雜湊完全匹配時套用**：

```yaml
# meta.yaml
skills:
  - whatsapp@1.2.0
  - telegram@1.0.0
apply_order: [whatsapp, telegram]
core_version: 0.6.0
resolved_at: 2026-02-15T10:00:00Z
tested: true
test_passed: true
resolution_source: maintainer
input_hashes:
  base: "aaa..."
  current_after_whatsapp: "bbb..."
  telegram_modified: "ccc..."
output_hash: "ddd..."
```

如果任何輸入雜湊不匹配，快取的解決方案將被跳過，系統繼續至第二層。

### 已驗證：rerere + merge-file 需要索引轉接器

`git rerere` **不**原生識別 `git merge-file` 的輸出。這在第 0 階段測試中已得到驗證（`tests/phase0-merge-rerere.sh`，33 個測試）。

問題不在於衝突標記格式——`merge-file` 使用檔名作為標籤（`<<<<<<< current.ts`），而 `git merge` 使用分支名稱（`<<<<<<< HEAD`），但 rerere 剝離所有標籤，只雜湊衝突主體。這些格式是相容的。

實際問題：**rerere 需要未合併的索引條目**（階段 1/2/3）才能偵測到合併衝突存在。正常的 `git merge` 會自動建立這些條目。`git merge-file` 僅在檔案系統上操作，不觸碰索引。

#### 轉接器

在 `git merge-file` 產生衝突後，系統必須建立 rerere 期望的索引狀態：

```bash
# 1. Run the merge (produces conflict markers in the working tree)
git merge-file current.ts .evoclaw/base/src/file.ts skills/add-whatsapp/modify/src/file.ts

# 2. If exit code > 0 (conflict), set up rerere adapter:

# Create blob objects for the three versions
base_hash=$(git hash-object -w .evoclaw/base/src/file.ts)
ours_hash=$(git hash-object -w skills/previous-skill/modify/src/file.ts)  # or the pre-merge current
theirs_hash=$(git hash-object -w skills/add-whatsapp/modify/src/file.ts)

# Create unmerged index entries at stages 1 (base), 2 (ours), 3 (theirs)
printf '100644 %s 1\tsrc/file.ts\0' "$base_hash" | git update-index --index-info
printf '100644 %s 2\tsrc/file.ts\0' "$ours_hash" | git update-index --index-info
printf '100644 %s 3\tsrc/file.ts\0' "$theirs_hash" | git update-index --index-info

# Set merge state (rerere checks for MERGE_HEAD)
echo "$(git rev-parse HEAD)" > .git/MERGE_HEAD
echo "skill merge" > .git/MERGE_MSG

# 3. Now rerere can see the conflict
git rerere  # Records preimage, or auto-resolves from cache

# 4. After resolution (manual or auto):
git add src/file.ts
git rerere  # Records postimage (caches the resolution)

# 5. Clean up merge state
rm .git/MERGE_HEAD .git/MERGE_MSG
git reset HEAD
```

#### 已驗證的關鍵屬性

- **衝突主體一致性**：`merge-file` 和 `git merge` 對相同輸入產生相同的衝突主體。Rerere 只雜湊主體，因此從任一來源學習的解決方案可互換。
- **雜湊確定性**：相同的衝突始終產生相同的 rerere 雜湊。這對共享解決方案快取至關重要。
- **解決方案可移植性**：將 `preimage` 和 `postimage` 檔案（加上雜湊目錄名稱）從一個 repo 的 `.git/rr-cache/` 複製到另一個 repo 有效。Rerere 在目標 repo 中自動解決。
- **相鄰行敏感性**：彼此相差約 3 行以內的變更被 `merge-file` 視為單一衝突塊。修改同一檔案相同區域的 skills 即使修改不同行也會產生衝突。這是預期的，由解決方案快取處理。

#### 含義：需要 Git 存儲庫

轉接器需要 `git hash-object`、`git update-index` 和 `.git/rr-cache/`。這意味著專案目錄必須是 git 存儲庫才能使 rerere 快取正常運作。下載 zip 的使用者（沒有 `.git/`）會失去解決方案快取，但不影響功能——衝突直接升級至第二層（Claude Code 解決）。系統應偵測此情況並優雅地跳過 rerere 操作。

### 維護者工作流程

在發布核心更新或新 skill 版本時：

1. 目標核心版本的全新代碼庫
2. 逐一套用每個官方 skill——驗證乾淨合併，執行測試
3. 對**修改至少一個共同檔案或具有重疊結構化操作的 skills** 套用成對組合
4. 基於受歡迎度和高重疊度套用精選的三 skill 堆疊
5. 解決所有衝突（代碼和結構化）
6. 記錄所有帶有輸入雜湊的解決方案
7. 為每個組合執行完整測試套件
8. 在版本發布時附帶已驗證的解決方案

標準：**擁有任何常見官方 skills 組合的使用者，應永遠不會遇到未解決的衝突。**

---

## 8. 狀態追蹤

`.evoclaw/state.yaml` 記錄安裝的所有內容：

```yaml
skills_system_version: "0.1.0"     # Schema version — tooling checks this before any operation
core_version: 0.1.0

applied_skills:
  - name: telegram
    version: 1.0.0
    applied_at: 2026-02-16T22:47:02.139Z
    file_hashes:
      src/channels/telegram.ts: "f627b9cf..."
      src/channels/telegram.test.ts: "400116769..."
      src/config.ts: "9ae28d1f..."
      src/index.ts: "46dbe495..."
      src/routing.test.ts: "5e1aede9..."
    structured_outcomes:
      npm_dependencies:
        grammy: "^1.39.3"
      env_additions:
        - TELEGRAM_BOT_TOKEN
        - TELEGRAM_ONLY
      test: "npx vitest run src/channels/telegram.test.ts"

  - name: discord
    version: 1.0.0
    applied_at: 2026-02-17T17:29:37.821Z
    file_hashes:
      src/channels/discord.ts: "5d669123..."
      src/channels/discord.test.ts: "19e1c6b9..."
      src/config.ts: "a0a32df4..."
      src/index.ts: "d61e3a9d..."
      src/routing.test.ts: "edbacb00..."
    structured_outcomes:
      npm_dependencies:
        discord.js: "^14.18.0"
      env_additions:
        - DISCORD_BOT_TOKEN
        - DISCORD_ONLY
      test: "npx vitest run src/channels/discord.test.ts"

custom_modifications:
  - description: "Added custom logging middleware"
    applied_at: 2026-02-15T12:00:00Z
    files_modified:
      - src/server.ts
    patch_file: .evoclaw/custom/001-logging-middleware.patch
```

**v0.1 實作注意事項：**
- `file_hashes` 每個檔案儲存一個 SHA-256 雜湊（最終合併結果）。三部分雜湊（基礎/skill 修改/合併）計劃在未來版本中提供，以改善漂移診斷。
- 已套用的 skills 使用 `name` 作為鍵欄位（而非 `skill`），與 TypeScript `AppliedSkill` 介面匹配。
- `structured_outcomes` 儲存原始清單值加上 `test` 指令。已解決的 npm 版本（實際安裝的版本 vs semver 範圍）尚未追蹤。
- `installed_at`、`last_updated`、`path_remap`、`rebased_at`、`core_version_at_apply`、`files_added` 和 `files_modified` 等欄位計劃在未來版本中提供。

---

## 9. 未追蹤的變更

如果使用者直接編輯檔案，系統透過雜湊比較偵測到這一點。

### 何時發生偵測

在**任何修改代碼庫的操作之前**：套用 skill、移除 skill、更新核心、重播或重新基準化。

### 發生什麼

```
Detected untracked changes to src/server.ts.
[1] Record these as a custom modification (recommended)
[2] Continue anyway (changes preserved, but not tracked for future replay)
[3] Abort
```

系統絕不阻止或丟失工作。選項 1 生成補丁並記錄，使變更可重現。選項 2 保留變更，但它們不會在重播中存活。

### 恢復保證

無論使用者在系統外如何修改代碼庫，三層模型始終可以恢復：

1. **Git**：將當前檔案與基礎進行差異比較，識別變更
2. **Claude Code**：讀取 `state.yaml` 以了解套用了哪些 skills，與實際檔案狀態比較，識別差異
3. **使用者**：Claude Code 詢問他們的意圖、要保留什麼、要丟棄什麼

沒有無法恢復的狀態。

---

## 10. 核心更新

核心更新必須盡可能程式化。EvoClaw 團隊負責確保更新能乾淨地套用到常見的 skill 組合。

### 補丁與遷移

大多數核心變更——錯誤修復、效能改進、新功能——透過三方合併自動傳播。無需特殊處理。

**破壞性變更**——更改的預設值、移除的功能、移至 skills 的功能——需要一個**遷移**。遷移是一個保留舊行為的 skill，針對新核心撰寫。在更新期間自動套用，使使用者的設置不受影響。

維護者在進行破壞性變更時的責任：在核心中進行變更，撰寫一個還原它的遷移 skill，在 `migrations.yaml` 中新增條目，進行測試。這就是破壞性變更的代價。

### `migrations.yaml`

存儲庫根目錄中的僅追加檔案。每個條目記錄一個破壞性變更以及保留舊行為的 skill：

```yaml
- since: 0.6.0
  skill: apple-containers@1.0.0
  description: "Preserves Apple Containers (default changed to Docker in 0.6)"

- since: 0.7.0
  skill: add-whatsapp@2.0.0
  description: "Preserves WhatsApp (moved from core to skill in 0.7)"

- since: 0.8.0
  skill: legacy-auth@1.0.0
  description: "Preserves legacy auth module (removed from core in 0.8)"
```

遷移 skills 是 `skills/` 目錄中的普通 skills。它們有清單、intent 檔案、測試——一切都有。它們針對**新**核心版本撰寫：修改的檔案是還原了特定破壞性變更的新核心，其他所有內容（錯誤修復、新功能）與新核心相同。

### 更新期間遷移的工作方式

1. 三方合併引入新核心的所有內容——補丁、破壞性變更，全部
2. 衝突解決（正常）
3. 重新套用自訂補丁（正常）
4. **將基礎更新至新核心**
5. 過濾 `migrations.yaml` 中 `since` > 使用者舊 `core_version` 的條目
6. **使用正常套用流程針對新基礎套用每個遷移 skill**
7. 像任何其他 skill 一樣在 `state.yaml` 中記錄遷移 skills
8. 執行測試

步驟 6 只是用於任何 skill 的相同套用函式。遷移 skill 針對新基礎合併：

- **基礎**：新核心（例如，帶有 Docker 的 v0.8）
- **當前**：更新合併後使用者的檔案（新核心 + 早期合併保留的使用者自訂）
- **其他**：遷移 skill 的檔案（還原了 Docker 改回 Apple 的新核心，其他一切相同）

三方合併正確地保留使用者的自訂、還原破壞性變更，並保留所有錯誤修復。如果有衝突，正常解決：快取 → Claude → 使用者。

對於大版本跳躍（v0.5 → v0.8），所有適用的遷移按順序套用。遷移 skills 針對最新核心版本維護，因此它們始終與當前代碼庫正確組合。

### 使用者看到的內容

```
Core updated: 0.5.0 → 0.8.0
  ✓ All patches applied

  Preserving your current setup:
    + apple-containers@1.0.0
    + add-whatsapp@2.0.0
    + legacy-auth@1.0.0

  Skill updates:
    ✓ add-telegram 1.0.0 → 1.2.0

  To accept new defaults: /remove-skill <name>
  ✓ All tests passing
```

更新期間沒有提示，沒有選擇。使用者的設置不會改變。如果他們以後想接受新的預設值，可以移除遷移 skill。

### 核心團隊在更新時發布的內容

```
updates/
  0.5.0-to-0.6.0/
    migration.md                  # What changed, why, and how it affects skills
    files/                        # The new core files
    file_ops:                     # Any renames, deletes, moves
    path_remap:                   # Compatibility map for old skill paths
    resolutions/                  # Pre-computed resolutions for official skills
```

加上新增至 `skills/` 的任何新遷移 skills 和附加至 `migrations.yaml` 的條目。

### 維護者流程

1. **進行核心變更**
2. **如果是破壞性變更**：針對新核心撰寫遷移 skill，在 `migrations.yaml` 中新增條目
3. **撰寫 `migration.md`** — 變更了什麼、為什麼、哪些 skills 可能受影響
4. **針對新核心逐一測試每個官方 skill**（包括遷移 skills）
5. **測試共享修改檔案或結構化操作的 skills 的成對組合**
6. **測試基於受歡迎度和重疊度的精選三 skill 堆疊**
7. **解決所有衝突**
8. **記錄所有帶有強制輸入雜湊的解決方案**
9. **執行完整測試套件**
10. **發布所有內容** — 遷移指南、遷移 skills、檔案操作、路徑重映射、解決方案

標準：**補丁靜默套用。破壞性變更透過遷移 skills 自動保留。使用者不應因其運作中的設置發生變更而感到驚訝。**

### 更新流程（完整）

#### 步驟 1：預檢

- 檢查未追蹤的變更
- 讀取 `state.yaml`
- 載入已發布的解決方案
- 解析 `migrations.yaml`，過濾適用的遷移

#### 步驟 2：預覽

在修改任何內容之前，向使用者展示即將發生的事情。這只使用 git 指令——不打開或更改任何檔案：

```bash
# Compute common base
BASE=$(git merge-base HEAD upstream/$BRANCH)

# Upstream commits since last sync
git log --oneline $BASE..upstream/$BRANCH

# Files changed upstream
git diff --name-only $BASE..upstream/$BRANCH
```

按影響分組呈現摘要：

```
Update available: 0.5.0 → 0.8.0 (12 commits)

  Source:  4 files modified (server.ts, config.ts, ...)
  Skills:  2 new skills added, 1 skill updated
  Config:  package.json, docker-compose.yml updated

  Migrations (auto-applied to preserve your setup):
    + apple-containers@1.0.0 (container default changed to Docker)
    + add-whatsapp@2.0.0 (WhatsApp moved from core to skill)

  Skill updates:
    add-telegram 1.0.0 → 1.2.0

  [1] Proceed with update
  [2] Abort
```

如果使用者中止，在此停止。沒有任何內容被修改。

#### 步驟 3：備份

將所有將被修改的檔案複製到 `.evoclaw/backup/`。

#### 步驟 4：檔案操作與路徑重映射

套用重新命名、刪除、移動。在 state 中記錄路徑重映射。

#### 步驟 5：三方合併

對於每個已變更的核心檔案：

```bash
git merge-file src/server.ts .evoclaw/base/src/server.ts updates/0.5.0-to-0.6.0/files/src/server.ts
```

#### 步驟 6：衝突解決

1. 已發布的解決方案（雜湊驗證）→ 自動
2. `git rerere` 本地快取 → 自動
3. Claude Code 配合 `migration.md` + skill 意圖 → 解決
4. 使用者 → 僅適用於真正的模糊情況

#### 步驟 7：重新套用自訂補丁

```bash
git apply --3way .evoclaw/custom/001-logging-middleware.patch
```

使用 `--3way` 允許 git 在行號漂移時回退至三方合併。如果 `--3way` 失敗，升級至第二層。

#### 步驟 8：更新基礎

`.evoclaw/base/` 替換為新的乾淨核心。這是**唯一一次**基礎發生變更。

#### 步驟 9：套用遷移 Skills

對於每個適用的遷移（`since` > 舊 `core_version`），使用正常套用流程針對新基礎套用遷移 skill。記錄在 `state.yaml` 中。

#### 步驟 10：重新套用已更新的 Skills

Skills 存在於存儲庫中，與核心檔案一起更新。更新後，比較每個 skill 的磁碟上 `manifest.yaml` 中的版本與 `state.yaml` 中記錄的版本。

對於每個磁碟版本比記錄版本更新的 skill：

1. 使用正常套用流程針對新基礎重新套用 skill
2. 三方合併引入 skill 的新變更，同時保留使用者自訂
3. 重新套用綁定到 skill 的自訂補丁（`git apply --3way`）
4. 更新 `state.yaml` 中的版本

版本未變更的 skills 將被跳過——無需任何操作。

如果使用者對大幅變更的 skill 有自訂補丁，補丁可能會發生衝突。正常解決：快取 → Claude → 使用者。

#### 步驟 11：重新執行結構化操作

針對更新後的代碼庫重新計算結構化操作以確保一致性。

#### 步驟 12：驗證

- 執行所有 skill 測試——強制執行
- 相容性報告：

```
Core updated: 0.5.0 → 0.8.0
  ✓ All patches applied

  Migrations:
    + apple-containers@1.0.0 (preserves container runtime)
    + add-whatsapp@2.0.0 (WhatsApp moved to skill)

  Skill updates:
    ✓ add-telegram 1.0.0 → 1.2.0 (new features applied)
    ✓ custom/telegram-group-only — re-applied cleanly

  ✓ All tests passing
```

#### 步驟 13：清理

刪除 `.evoclaw/backup/`。

### 漸進式核心精簡

遷移為隨時間精簡核心提供了清晰的路徑。每個版本都可以將更多功能移至 skills：

- 破壞性變更從核心移除功能
- 遷移 skill 為現有使用者保留它
- 新使用者從最小核心開始，按需新增
- 隨著時間推移，`state.yaml` 精確反映每個使用者的運作情況

---

## 11. 移除 Skill（卸載）

移除 skill 不是逆向補丁操作。**卸載是不包含該 skill 的重播。**

### 工作方式

1. 讀取 `state.yaml` 以獲取已套用 skills 和自訂修改的完整列表
2. 從列表中移除目標 skill
3. 將當前代碼庫備份至 `.evoclaw/backup/`
4. **從乾淨基礎重播**——按順序套用每個剩餘的 skill，套用自訂補丁，使用解決方案快取
5. 執行所有測試
6. 如果測試通過，刪除備份並更新 `state.yaml`
7. 如果測試失敗，從備份還原並報告

### 綁定到被移除 Skill 的自訂補丁

如果被移除的 skill 在 `state.yaml` 中有 `custom_patch`，使用者將收到警告：

```
Removing telegram will also discard custom patch: "Restrict bot responses to group chats only"
[1] Continue (discard custom patch)
[2] Abort
```

---

## 12. 重新基準化

將累積的層次壓平為乾淨的起始點。

### 重新基準化的作用

1. 將使用者當前的實際檔案作為新的現實
2. 將 `.evoclaw/base/` 更新至當前核心版本的乾淨檔案
3. 對於每個已套用的 skill，針對新基礎重新生成修改的檔案差異
4. 更新 `state.yaml` 中的 `rebased_at` 時間戳
5. 清除舊的自訂補丁（現已納入）
6. 清除過時的解決方案快取條目

### 何時重新基準化

- 在重大核心更新後
- 當累積的補丁變得難以管理時
- 在套用重要的新 skill 之前
- 作為定期維護

### 權衡

**失去**：個別 skill 補丁歷史、乾淨移除單個舊 skill 的能力、作為獨立工件的舊自訂補丁

**獲得**：乾淨的基礎、更簡單的未來合併、減少的快取大小、全新的起始點

---

## 13. 重播

給定 `state.yaml`，在全新機器上重現完全相同的安裝，無需 AI 干預（假設所有解決方案都已快取）。

### 重播流程

```bash
# Fully programmatic — no Claude Code needed

# 1. Install core at specified version
evoclaw-init --version 0.5.0

# 2. Load shared resolutions into local rerere cache
load-resolutions .evoclaw/resolutions/

# 3. For each skill in applied_skills (in order):
for skill in state.applied_skills:
  # File operations
  apply_file_ops(skill)

  # Copy new files
  cp skills/${skill.name}/add/* .

  # Merge modified code files (with path remapping)
  for file in skill.files_modified:
    resolved_path = apply_remap(file, state.path_remap)
    git merge-file ${resolved_path} .evoclaw/base/${resolved_path} skills/${skill.name}/modify/${file}
    # git rerere auto-resolves from shared cache if needed

  # Apply skill-specific custom patch if recorded
  if skill.custom_patch:
    git apply --3way ${skill.custom_patch}

# 4. Apply all structured operations (batched)
collect_all_structured_ops(state.applied_skills)
merge_npm_dependencies → write package.json once
npm install once
merge_env_additions → write .env.example once
merge_compose_services → write docker-compose.yml once

# 5. Apply standalone custom modifications
for custom in state.custom_modifications:
  git apply --3way ${custom.patch_file}

# 6. Run tests and verify hashes
run_tests && verify_hashes
```

---

## 14. Skill 測試

每個 skill 包含驗證 skill 套用後正確運作的整合測試。

### 結構

```
skills/
  add-whatsapp/
    tests/
      whatsapp.test.ts
```

### 測試驗證的內容

- **全新核心上的單個 skill**：套用到乾淨代碼庫 → 測試通過 → 整合有效
- **Skill 功能**：功能實際運作
- **套用後狀態**：檔案處於預期狀態，`state.yaml` 正確更新

### 測試執行時機（始終）

- **套用 skill 後** — 即使所有合併都是乾淨的
- **核心更新後** — 即使所有合併都是乾淨的
- **卸載重播後** — 確認移除未破壞剩餘的 skills
- **在 CI 中** — 逐一測試所有官方 skills 及常見組合
- **重播期間** — 驗證重播狀態

乾淨合併 ≠ 有效代碼。測試是唯一可靠的信號。

### CI 測試矩陣

測試覆蓋是**智能的，而非窮舉的**：

- 每個官方 skill 逐一針對每個支援的核心版本
- **修改至少一個共同檔案或具有重疊結構化操作的 skills 的成對組合**
- 基於受歡迎度和高重疊度的精選三 skill 堆疊
- 從清單的 `modifies` 和 `structured` 欄位自動生成測試矩陣

每個通過的組合為共享快取生成一個已驗證的解決方案條目。

---

## 15. 專案配置

### `.gitattributes`

隨 EvoClaw 發布以減少雜亂的合併衝突：

```
* text=auto
*.ts text eol=lf
*.json text eol=lf
*.yaml text eol=lf
*.md text eol=lf
```

---

## 16. 目錄結構

```
project/
  src/                              # The actual codebase
    server.ts
    config.ts
    channels/
      whatsapp.ts
      telegram.ts
  skills/                           # Skill packages (Claude Code slash commands)
    add-whatsapp/
      SKILL.md
      manifest.yaml
      tests/
        whatsapp.test.ts
      add/
        src/channels/whatsapp.ts
      modify/
        src/
          server.ts
          server.ts.intent.md
          config.ts
          config.ts.intent.md
    add-telegram/
      ...
    telegram-reactions/             # Layered skill
      ...
  .evoclaw/
    base/                           # Clean core (shared base)
      src/
        server.ts
        config.ts
        ...
    state.yaml                      # Full installation state
    backup/                         # Temporary backup during operations
    custom/                         # Custom patches
      telegram-group-only.patch
      001-logging-middleware.patch
      001-logging-middleware.md
    resolutions/                    # Shared verified resolution cache
      whatsapp@1.2.0+telegram@1.0.0/
        src/
          server.ts.resolution
          server.ts.preimage
        meta.yaml
  .gitattributes
```

---

## 17. 設計原則

1. **使用 git，不要重新發明它。** `git merge-file` 用於代碼合併，`git rerere` 用於快取解決方案，`git apply --3way` 用於自訂補丁。
2. **三層解決：git → Claude → 使用者。** 首先程式化，其次 AI，第三人工。
3. **乾淨合併還不夠。** 每次操作後執行測試。語意衝突能存活文字合併。
4. **所有操作都是安全的。** 之前備份，失敗時還原。沒有半套用狀態。
5. **一個共享基礎。** `.evoclaw/base/` 是套用任何 skills 或自訂前的乾淨核心。它是所有三方合併的穩定公共祖先。僅在核心更新時更新。
6. **代碼合併 vs. 結構化操作。** 原始碼進行三方合併。依賴、環境變數和設定以程式化方式聚合。結構化操作是隱式且批次處理的。
7. **解決方案被學習和共享。** 維護者解決衝突並在強制雜湊執行下發布已驗證的解決方案。`.evoclaw/resolutions/` 是共享工件。
8. **一個 skill，一條快樂路徑。** 沒有預定義的設定選項。自訂是更多的補丁。
9. **Skills 分層和組合。** 核心 skills 提供基礎。擴充 skills 新增功能。
10. **意圖是一等公民且結構化的。** `SKILL.md`、`.intent.md`（What、Invariants、Must-keep）和 `migration.md`。
11. **State 是明確且完整的。** Skills、自訂補丁、每檔案雜湊、結構化結果、路徑重映射。重播是確定性的。漂移可即時偵測。
12. **始終可恢復。** 三層模型從任何起始點重建一致的狀態。
13. **卸載是重播。** 從乾淨基礎重播，不包含該 skill。備份以確保安全。
14. **核心更新是維護者的責任。** 測試、解決、發布。破壞性變更需要保留舊行為的遷移 skill。破壞性變更的代價是撰寫和測試遷移。使用者不應因其設置的變更而感到驚訝。
15. **檔案操作和路徑重映射是一等公民。** 清單中的重新命名、刪除、移動。Skills 絕不被修改——路徑在套用時解析。
16. **Skills 經過測試。** 每個 skill 的整合測試。CI 按重疊測試成對組合。測試始終執行。
17. **確定性序列化。** 排序鍵，一致的格式。沒有雜亂的差異。
18. **需要時重新基準化。** 將層次壓平為乾淨的起始點。
19. **漸進式核心精簡。** 破壞性變更將功能從核心移至遷移 skills。現有使用者自動保留他們擁有的。新使用者從最小化開始，按需新增。
