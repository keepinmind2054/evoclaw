# EvoClaw 發布流程規範

本文件規範 EvoClaw 專案的發布流程，確保每次發布都經過完整測試與審查。

---

## 發布週期

- **主要版本 (Major)**：每 3-6 個月，包含重大功能更新或不相容變更
- **次要版本 (Minor)**：每月或每兩月，包含新功能新增
- **修補版本 (Patch)**：視需要發布，主要用於錯誤修正

---

## 發布前檢查清單

### 1. 代碼品質檢查
- [ ] 所有 Python 檔案通過 `python -m py_compile` 檢查
- [ ] 執行所有測試用例並確保通過
  ```bash
  python -m tests.test_immune_enhanced
  ```
- [ ] 檢查是否有未提交的代碼變更
- [ ] 確認 `CHANGELOG.md` 已更新

### 2. 功能驗證
- [ ] Web Dashboard 正常運作（port 8765）
- [ ] Web Portal 正常運作（port 8766）
- [ ] 健康監控系統正常運行
- [ ] 免疫系統能正確檢測 injection 攻擊
- [ ] 排程任務正常執行
- [ ] 容器隔離機制正常

### 3. 文檔檢查
- [ ] `README.md` 已更新最新版本資訊
- [ ] `CHANGELOG.md` 已記錄所有變更
- [ ] 必要時更新 `docs/` 目錄下的技術文檔

### 4. 效能與安全
- [ ] 資料庫索引已建立
- [ ] 記憶體使用量在正常範圍
- [ ] 無明顯的資源洩漏
- [ ] 免疫 pattern 已更新最新版本

---

## 發布流程

### 步驟 1：準備發布分支
```bash
# 切換到 main 分支
git checkout main

# 拉取最新代碼
git pull origin main

# 建立發布分支（例如：release-1.3.0）
git checkout -b release-1.3.0
```

### 步驟 2：更新版本號
在以下位置更新版本號：
- `CHANGELOG.md` - 更新標題日期
- `README.md` - 如有版本提及則更新

### 步驟 3：最終測試
```bash
# 執行所有測試
python -m pytest tests/ -v

# 或執行特定測試
python -m tests.test_immune_enhanced
```

### 步驟 4：提交發布
```bash
# 提交所有變更
git add .
git commit -m "release: 準備發布 v1.3.0"

# 推送到遠端
git push origin release-1.3.0
```

### 步驟 5：建立 Pull Request
- 在 GitHub 建立 PR：`release-1.3.0` → `main`
- 標題格式：`Release v1.3.0`
- 描述中包含：
  - 主要變更項目
  - 測試結果
  - 已知問題（如有）

### 步驟 6：代碼審查
- 至少需要 1 位審查者批准
- 確認所有 CI 檢查通過
- 解決所有審查意見

### 步驟 7：合併與發布
```bash
# 合併到 main 分支
git checkout main
git pull origin main
git merge release-1.3.0
git push origin main

# 建立 Git Tag
git tag -a v1.3.0 -m "Release version 1.3.0"
git push origin v1.3.0
```

### 步驟 8：GitHub Release
1. 前往 GitHub 專案頁面
2. 點擊「Releases」→「Create a new release」
3. 選擇標籤 `v1.3.0`
4. 填寫發布說明（從 `CHANGELOG.md` 複製）
5. 標記為最新發布
6. 點擊「Publish Release」

### 步驟 9：清理
```bash
# 刪除發布分支
git branch -d release-1.3.0
git push origin --delete release-1.3.0
```

---

## 緊急發布流程

遇到嚴重錯誤需要緊急發布時：

### 步驟 1：建立熱修復分支
```bash
git checkout main
git checkout -b hotfix-1.2.1
```

### 步驟 2：修復問題
- 只修復緊急問題
- 避免加入新功能
- 最小化變更範圍

### 步驟 3：快速測試
- 執行相關測試
- 驗證問題已修復
- 確認無新問題引入

### 步驟 4：發布
```bash
git add .
git commit -m "fix: 緊急修復 [問題描述]"
git push origin hotfix-1.2.1

# 建立 PR 並標記為緊急
# 合併立即可執行
```

---

## 版本命名規範

遵循 [語意化版本 2.0.0](https://semver.org/)：

格式：`MAJOR.MINOR.PATCH`

- **MAJOR**：不相容的 API 變更
  - 移除或修改現有功能
  - 改變預設行為導致不相容
  
- **MINOR**：向後相容的功能新增
  - 新增功能
  - 改進現有功能（向後相容）
  
- **PATCH**：向後相容的問題修正
  - 錯誤修復
  - 效能優化
  - 文檔更新

### 範例
- `1.2.3` - 第 1 版第 2 次次要更新的第 3 次修補
- `2.0.0` - 第 2 版主版本（可能包含不相容變更）
- `1.3.0` - 第 1 版第 3 次次要更新

---

## 發布說明範本

```markdown
## [版本號] - YYYY-MM-DD

### 新增
- 功能描述

### 改進
- 改進描述

### 修復
- 修復描述

### 安全性
- 安全性相關更新

### 已知問題
- 已知問題描述
```

---

## 發布後任務

### 立即可做
- [ ] 確認 GitHub Release 已正確建立
- [ ] 檢查 CI/CD 流水線是否成功
- [ ] 通知用戶群體（如有需要）

### 24 小時內
- [ ] 監控錯誤回報
- [ ] 收集用戶反饋
- [ ] 確認無重大問題

### 一週內
- [ ] 整理發布反饋
- [ ] 規劃下一版本
- [ ] 更新開發路線圖

---

## 聯絡方式

如有發布相關問題，請：
1. 查閱本文件
2. 檢查 GitHub Issues
3. 聯繫維護者

---

## 附錄：常用命令

```bash
# 查看當前版本
git describe --tags --always

# 查看版本歷史
git log --oneline --decorate

# 比較版本差異
git diff v1.2.0..v1.3.0

# 建立新版本標籤
git tag -a v1.3.0 -m "Release version 1.3.0"

# 推送標籤
git push origin v1.3.0

# 刪除標籤（本地）
git tag -d v1.3.0

# 刪除標籤（遠端）
git push origin --delete v1.3.0
```
