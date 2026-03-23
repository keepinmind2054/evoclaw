# EvoClaw 穩定性分析報告

**版本**: 1.0
**日期**: 2026-03-23
**分析範圍**: EvoClaw vs nanoclaw 架構穩定性比較

---

## 執行摘要

EvoClaw 的穩定性問題根源在於**架構複雜度**，而非個別 bug。每次使用者發送訊息，EvoClaw 至少經歷 15+ 個潛在失敗點（Docker 啟動、stdin/stdout pipes、JSON 序列化、marker 偵測、tool 結果解析等）；nanoclaw 則只有 2-3 個（網路、Claude SDK）。

這份報告列出所有發現的問題，並與 nanoclaw 的架構做比較。

---

## 一、核心差異：架構層數

### EvoClaw（3 層序列化管道）

```
Host (Python)
  ↓ JSON encode request
stdin → Docker container
  ↓ JSON decode in agent.py
  ↓ Call Gemini/OpenAI/Claude
  ↓ Parse response
  ↓ print(OUTPUT_START) + print(json) + print(OUTPUT_END)
stdout → Host reads markers
  ↓ JSON decode agent response
Host 回傳給使用者
```

**失敗點**: stdin/stdout races、marker 偵測、JSON parse、container 冷啟動、pipe 死鎖

### nanoclaw（1 層直接呼叫）

```
Node.js
  ↓ Direct Claude SDK call
Claude API（網路）
  ↓ Structured MCP response object
Node.js 回傳給使用者
```

**失敗點**: 網路、SDK

---

## 二、container_runner.py — 關鍵問題

### 2.1 Output Marker 競態（CRITICAL）

`---EVOCLAW_OUTPUT_START---` 和 `---EVOCLAW_OUTPUT_END---` 之間的內容，若 container 在 CONTAINER_TIMEOUT 時被 kill，stdout 可能只有 START 沒有 END。現有程式碼：
- 沒有同時確認兩個 marker 都存在再解析
- JSON parse 失敗直接算作錯誤，觸發 circuit breaker
- 正常被截斷的輸出（完整但超時）也計入失敗

**建議**: 加入 `OUTPUT_START in stdout and OUTPUT_END in stdout` 前置檢查；分開計算「輸出截斷」vs「容器崩潰」的錯誤類型。

---

### 2.2 stdin/stdout Pipe 死鎖

```python
# container_runner.py
async def _collect():
    stdout_task = asyncio.create_task(proc.stdout.read())   # 等全部
    stderr_task = asyncio.create_task(_stream_stderr())      # 逐行 30s 超時
    stdout_data, _ = await asyncio.gather(stdout_task, stderr_task)
```

當 container 同時大量輸出 stdout + stderr 時：
- `proc.stdout.read()` 等待 stdout 管道清空
- `_stream_stderr()` 每行 30s 超時逐行讀取
- 兩者互相搶佔 → 任一方可能提前超時，另一方永遠等待

**建議**: 使用單一 `asyncio.wait_for(asyncio.gather(...), total_timeout)` 而非 per-line 超時。

---

### 2.3 並發容器限制未在核心函數強制

`MAX_CONCURRENT_CONTAINERS` 的檢查在 `group_queue.py`，但 `ipc_watcher.py` 直接呼叫 `run_container_agent()`，繞過此限制。如果多個 subagent 同時被召喚，Docker 資源可能耗盡。

**建議**: 將 semaphore 移進 `run_container_agent()` 本身。

---

## 三、agent.py — 虛假回應問題

### 3.1 Tool 結果不區分成功/失敗（HIGH）

```python
# agent.py
result = execute_tool(fc.name, dict(fc.args), chat_jid)
fn_responses.append(
    types.FunctionResponse(response={"result": result})
)
```

所有 tool 結果都是同一個字串格式：
- 成功: `{"result": "file content here"}`
- 失敗: `{"result": "error: permission denied"}`
- 超時: `{"result": ""}`

**Agent 無法明確區分成功與失敗**，只能靠文字猜測。這是虛假回應的主要根源。

nanoclaw 使用 MCP 結構化結果：
```json
{"success": true, "stdout": "...", "stderr": "", "exit_code": 0}
```

成功/失敗語意清晰，Claude 不需要猜測。

---

### 3.2 MEMORY.md 汙染正反饋迴路（HIGH）

```python
# agent.py — MEMORY.md 注入進 system prompt
lines.append(f"## 長期記憶 (MEMORY.md)\n{_memory_snippet}")
```

這建立了一個危險的正反饋迴路：
1. Session 1：Agent 聲稱「已部署」（實際未完成）
2. MEMORY.md 記錄：`[2026-03-20] 部署完成`
3. Session 2：System prompt 將 MEMORY.md 作為事實注入
4. Agent 以為部署確實完成 → 再次虛假確認
5. MEMORY.md 累積更多虛假記錄

**建議**: MEMORY.md 應標記為「過去的記憶，需再次驗證」，而非作為事實放進 system prompt。加入 `⚠️ 以下為過去的記憶，請重新驗證後再使用` 前置警語。

---

### 3.3 假進度偵測過窄（HIGH）

```python
_FAKE_STATUS_RE = r'\*\([^)]*\)\*|\*\[[^\]]*\]\*'
```

只能偵測：
- `*(正在執行...)*`
- `*[processing]*`

無法偵測語意上的虛假：
- 「Bug 已修復」（但沒有 Write/Edit 工具被呼叫）
- 「我已分析了程式碼」（但沒有 Read 工具被呼叫）
- 「部署成功」（但沒有 Bash/docker 呼叫）

**建議**: 同時加入工具呼叫驗證——若 agent 聲稱完成某操作，但對應工具在該 turn 未被呼叫，注入警告。

---

### 3.4 工具結果截斷丟失錯誤訊息（MEDIUM）

```python
_MAX_TOOL_RESULT_CHARS = 4000
if len(result_str) > _MAX_TOOL_RESULT_CHARS:
    result_str = result_str[:4000] + "\n[... truncated ...]"
```

Bash 輸出通常錯誤訊息在**最後**，截斷後 agent 只看到前 4000 字，錯誤被切掉，agent 以為成功繼續執行。

**建議**: 截斷優先保留**尾部**（錯誤所在處）；或同時保留頭部 2000 + 尾部 2000 字。

---

### 3.5 無限工具重試偵測缺失（MEDIUM）

若某個 tool 呼叫持續失敗，agent 會重試直到 MAX_ITER：

```
Turn 1: Bash(git clone ...) → timeout
Turn 2: Bash(git clone ...) → timeout
...
Turn 20: 達到 MAX_ITER
```

20 輪全部浪費在同一個失敗操作上，最後回傳 "處理完成，但未能產生文字回應"（誤導使用者）。

**建議**: 追蹤 (tool_name, input_hash) → 失敗次數；2-3 次後注入警告要求 agent 換策略。

---

### 3.6 Max Iterations 回傳誤導性訊息

```python
if not final_response or not final_response.strip():
    final_response = "（處理完成，但未能產生文字回應，請重新詢問。）"
```

Loop 耗盡通常代表失敗（agent 卡住、工具失敗、LLM 混亂），但回傳的訊息暗示「處理完成」。

**建議**: 明確回傳錯誤訊息：「Agent 在 20 輪內未能完成任務，請檢查 logs 或重新描述需求。」

---

## 四、main.py — 並發與狀態問題

### 4.1 非同步 Lock 初始化競態（HIGH）

```python
_group_fail_lock: asyncio.Lock | None = None  # 在 main() 才初始化
# 使用時:
if _group_fail_lock is not None:
    async with _group_fail_lock:
        ...
    # 若 lock 為 None → 直接跳過，無保護
```

若訊息在 `main()` 建立 lock 前到達，並發保護完全失效。

**建議**: 在模組層級用 `asyncio.Lock()` 初始化（Python 3.10+ 在 `asyncio.run()` context 中安全）。

---

### 4.2 失敗計數器成功後未重置

```python
# 失敗時 +1，cooldown 過後只 -2
_group_fail_counts[jid] = max(0, _group_fail_counts.get(jid, 0) - 2)
```

一個群組失敗 5 次後：等待 60s → 嘗試成功 → 計數器仍為 3。下次失敗立即觸發 cooldown。

**建議**: 成功後立即將計數器重置為 0。

---

### 4.3 固定 Cooldown，無指數退避

無論失敗幾次，cooldown 固定 60 秒。若群組有長期問題，應採用指數退避（60s → 2m → 5m → 10m）。

---

## 五、ipc_watcher.py — 文件處理問題

### 5.1 IPC 消息排序不可靠（MEDIUM）

IPC 文件以時間戳記作為文件名前綴，排序後處理。若 NTP 校時或跨機器時鐘偏差，訊息可能亂序送達。

**建議**: 使用單調遞增序列號（atomic counter）作為前綴，而非時間戳記。

---

### 5.2 Memory Search 結果無大小限制（MEDIUM）

```python
results = memory_search(jid, query)
output = json.dumps(results, ...)  # 無大小限制！
```

若搜尋結果返回數千條記錄，可能造成 agent 讀取 10MB+ 的 JSON → OOM。

**建議**: 限制最多 100 條結果，每條摘要截斷至 500 字元。

---

### 5.3 Subagent 結果輪詢等待過長（MEDIUM）

```python
for _ in range(300):  # 最多等 300s
    if output_path.exists():
        ...
    time.sleep(1)
return "Error: subagent timed out after 300s"
```

Parent agent 在等待期間無法處理其他訊息，群組被鎖定長達 5 分鐘。

**建議**: 最多等 60s；並定期檢查 subagent container 是否仍存活。

---

## 六、nanoclaw 的穩定性優勢總結

| 面向 | nanoclaw | EvoClaw | 優勢說明 |
|------|----------|---------|---------|
| **API 呼叫路徑** | 直接 SDK | 3 層序列化 | 消除 stdin/stdout races、marker 偵測、JSON parse bug |
| **冷啟動時間** | < 1 秒 | 15-60 秒 | 無 container lifecycle 開銷 |
| **Tool 結果格式** | MCP 結構化 | 字串推斷 | 消除靠文字猜測成功/失敗 |
| **記憶體處理** | 獨立載入 | 注入 system prompt | 消除正反饋虛假迴路 |
| **錯誤協議** | 明確 success/failure flag | 文字解析 | 保證語意確定性 |
| **訊息佇列** | 直接呼叫 | GroupQueue 複雜狀態機 | 消除訊息遺失、批次競態 |
| **代碼複雜度** | ~1,000 行 | ~5,000+ 行 | 更少失敗模式，更易除錯 |
| **超時死鎖** | 無 | 多處風險 | 無並發 stream 互相等待 |

**核心洞見**: nanoclaw 的穩定性並非來自更好的錯誤處理代碼，而是來自**更少的抽象層**。少一層 = 少一個失敗點。

---

## 七、優先修復建議

### 立即修復（阻塞生產）

1. **Tool 結果加入明確成功/失敗旗標** (Section 3.1)
   - 最高優先級：直接影響虛假回應
   - 作法：在 `execute_tool()` 返回值中加入 `{"result": ..., "success": bool, "exit_code": int}`

2. **MEMORY.md 注入加入警語** (Section 3.2)
   - 在注入時加前置語：「以下為過去記憶，請重新驗證」
   - 避免 agent 直接信任舊記錄

3. **Output marker 同時驗證** (Section 2.1)
   - 解析前先確認 START + END 均存在
   - 分開計算「截斷」與「崩潰」錯誤類型

4. **Memory search 結果上限** (Section 5.2)
   - 限制 100 條，每條 500 字

### 中期修復（1-2 週內）

5. **工具截斷改為保留尾部** (Section 3.4)
6. **工具重試偵測** (Section 3.5)
7. **Max Iterations 錯誤訊息改為明確失敗** (Section 3.6)
8. **失敗計數器成功後重置** (Section 4.2)
9. **並發容器限制移入核心函數** (Section 2.3)
10. **Subagent 輪詢超時縮短至 60s** (Section 5.3)

### 長期改進（架構調整）

11. **指數退避 Cooldown** (Section 4.3)
12. **GroupState 統一狀態機** (避免多個 dict 分散狀態)
13. **IPC 序列號排序** (Section 5.1)
14. **Circuit Breaker Pattern** (Section 2.2)

---

## 八、根本結論

EvoClaw 選擇了**彈性優先**的架構設計：
- 支援多家 LLM（Gemini / OpenAI / Claude）
- 強隔離（Docker container）
- 熱換工具（skills system）
- 跨平台執行

代價是每次請求有 15+ 個潛在失敗點，且虛假回應的正反饋迴路難以避免。

**短期最有效的修復**: 修正 tool 結果格式（加入 success flag）+ MEMORY.md 注入加警語。這兩點直接解決虛假回應問題，不需要大規模重構。
