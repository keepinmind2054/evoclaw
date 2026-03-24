# Claude Agent SDK 深度解析

透過逆向工程 `@google/genai` v0.2.29–0.2.34 所得到的研究成果，用於理解 `query()` 的運作原理、為何 agent 團隊的子代理（subagent）會被終止，以及如何修復這個問題。並輔以官方 SDK 參考文件。

## 架構

```
Agent Runner (our code)
  └── query() → SDK (sdk.mjs)
        └── spawns CLI subprocess (cli.js)
              └── Claude API calls, tool execution
              └── Task tool → spawns subagent subprocesses
```

SDK 以 `--output-format stream-json --input-format stream-json --print --verbose` 旗標將 `cli.js` 作為子行程（child process）啟動。通訊透過 stdin/stdout 上的 JSON-lines 進行。

`query()` 回傳一個繼承自 `AsyncGenerator<SDKMessage, void>` 的 `Query` 物件。內部運作如下：

- SDK 將 CLI 作為子行程啟動，透過 stdin/stdout JSON lines 進行通訊
- SDK 的 `readMessages()` 從 CLI 的 stdout 讀取資料，並排入內部 stream
- `readSdkMessages()` async generator 從該 stream 中 yield 資料
- `[Symbol.asyncIterator]` 回傳 `readSdkMessages()`
- 只有在 CLI 關閉 stdout 時，Iterator 才會回傳 `done: true`

V1（`query()`）和 V2（`createSession`/`send`/`stream`）均使用完全相同的三層架構：

```
SDK (sdk.mjs)           CLI Process (cli.js)
--------------          --------------------
XX Transport  ------>   stdin reader (bd1)
  (spawn cli.js)           |
$X Query      <------   stdout writer
  (JSON-lines)             |
                        EZ() recursive generator
                           |
                        Anthropic Messages API
```

## 核心 Agent 迴圈（EZ）

在 CLI 內部，agentic 迴圈是一個**名為 `EZ()` 的遞迴 async generator**，而非迭代式的 while 迴圈：

```
EZ({ messages, systemPrompt, canUseTool, maxTurns, turnCount=1, ... })
```

每次呼叫 = 對 Claude 發起一次 API 呼叫（一個「回合」）。

### 每個回合的流程：

1. **準備訊息** — 修剪上下文，必要時執行壓縮（compaction）
2. **呼叫 Anthropic API**（透過 `mW1` streaming 函式）
3. **從回應中提取 tool_use 區塊**
4. **分支：**
   - 若**沒有 tool_use 區塊** → 停止（執行 stop hooks，回傳）
   - 若**有 tool_use 區塊** → 執行工具，遞增 turnCount，遞迴呼叫

所有複雜邏輯——agent 迴圈、工具執行、背景任務、隊友協調——都在 CLI 子行程內執行。`query()` 只是一個薄薄的傳輸層封裝。

## query() 選項

官方文件中完整的 `Options` 型別：

| 屬性 | 型別 | 預設值 | 說明 |
|------|------|--------|------|
| `abortController` | `AbortController` | `new AbortController()` | 用於取消操作的控制器 |
| `additionalDirectories` | `string[]` | `[]` | Claude 可存取的額外目錄 |
| `agents` | `Record<string, AgentDefinition>` | `undefined` | 以程式方式定義子代理（非 agent 團隊——無代理間協調） |
| `allowDangerouslySkipPermissions` | `boolean` | `false` | 使用 `permissionMode: 'bypassPermissions'` 時必須設為 true |
| `allowedTools` | `string[]` | 所有工具 | 允許的工具名稱清單 |
| `betas` | `SdkBeta[]` | `[]` | Beta 功能（例如 `['context-1m-2025-08-07']` 啟用 1M 上下文） |
| `canUseTool` | `CanUseTool` | `undefined` | 工具使用的自訂權限函式 |
| `continue` | `boolean` | `false` | 繼續最近一次對話 |
| `cwd` | `string` | `process.cwd()` | 當前工作目錄 |
| `disallowedTools` | `string[]` | `[]` | 不允許的工具名稱清單 |
| `enableFileCheckpointing` | `boolean` | `false` | 啟用檔案變更追蹤以供回溯 |
| `env` | `Dict<string>` | `process.env` | 環境變數 |
| `executable` | `'bun' \| 'deno' \| 'node'` | 自動偵測 | JavaScript 執行環境 |
| `fallbackModel` | `string` | `undefined` | 主要模型失敗時使用的備用模型 |
| `forkSession` | `boolean` | `false` | 繼續對話時，fork 成新的 session ID 而非延續原始 session |
| `hooks` | `Partial<Record<HookEvent, HookCallbackMatcher[]>>` | `{}` | 事件的 Hook 回呼 |
| `includePartialMessages` | `boolean` | `false` | 包含部分訊息事件（streaming） |
| `maxBudgetUsd` | `number` | `undefined` | 查詢的最高預算（美元） |
| `maxThinkingTokens` | `number` | `undefined` | 思考過程的最大 token 數 |
| `maxTurns` | `number` | `undefined` | 最大對話回合數 |
| `mcpServers` | `Record<string, McpServerConfig>` | `{}` | MCP 伺服器設定 |
| `model` | `string` | CLI 預設值 | 使用的 Claude 模型 |
| `outputFormat` | `{ type: 'json_schema', schema: JSONSchema }` | `undefined` | 結構化輸出格式 |
| `pathToClaudeCodeExecutable` | `string` | 使用內建 | Claude Code 執行檔路徑 |
| `permissionMode` | `PermissionMode` | `'default'` | 權限模式 |
| `plugins` | `SdkPluginConfig[]` | `[]` | 從本地路徑載入自訂套件 |
| `resume` | `string` | `undefined` | 要繼續的 session ID |
| `resumeSessionAt` | `string` | `undefined` | 從特定訊息 UUID 處繼續 session |
| `sandbox` | `SandboxSettings` | `undefined` | 沙箱行為設定 |
| `settingSources` | `SettingSource[]` | `[]`（無） | 要載入哪些檔案系統設定。必須包含 `'project'` 才能載入 CLAUDE.md |
| `stderr` | `(data: string) => void` | `undefined` | stderr 輸出的回呼 |
| `systemPrompt` | `string \| { type: 'preset'; preset: 'claude_code'; append?: string }` | `undefined` | System prompt。使用 preset 可取得 Claude Code 的 prompt，並可選擇性附加內容 |
| `tools` | `string[] \| { type: 'preset'; preset: 'claude_code' }` | `undefined` | 工具設定 |

### PermissionMode

```typescript
type PermissionMode = 'default' | 'acceptEdits' | 'bypassPermissions' | 'plan';
```

### SettingSource

```typescript
type SettingSource = 'user' | 'project' | 'local';
// 'user'    → ~/.claude/settings.json
// 'project' → .claude/settings.json (version controlled)
// 'local'   → .claude/settings.local.json (gitignored)
```

省略時，SDK 不載入任何檔案系統設定（預設隔離）。優先順序：local > project > user。程式選項永遠覆蓋檔案系統設定。

### AgentDefinition

程式化子代理（非 agent 團隊——較為簡單，無代理間協調）：

```typescript
type AgentDefinition = {
  description: string;  // When to use this agent
  tools?: string[];     // Allowed tools (inherits all if omitted)
  prompt: string;       // Agent's system prompt
  model?: 'sonnet' | 'opus' | 'haiku' | 'inherit';
}
```

### McpServerConfig

```typescript
type McpServerConfig =
  | { type?: 'stdio'; command: string; args?: string[]; env?: Record<string, string> }
  | { type: 'sse'; url: string; headers?: Record<string, string> }
  | { type: 'http'; url: string; headers?: Record<string, string> }
  | { type: 'sdk'; name: string; instance: McpServer }  // in-process
```

### SdkBeta

```typescript
type SdkBeta = 'context-1m-2025-08-07';
// Enables 1M token context window for Opus 4.6, Sonnet 4.5, Sonnet 4
```

### CanUseTool

```typescript
type CanUseTool = (
  toolName: string,
  input: ToolInput,
  options: { signal: AbortSignal; suggestions?: PermissionUpdate[] }
) => Promise<PermissionResult>;

type PermissionResult =
  | { behavior: 'allow'; updatedInput: ToolInput; updatedPermissions?: PermissionUpdate[] }
  | { behavior: 'deny'; message: string; interrupt?: boolean };
```

## SDKMessage 型別

`query()` 可以 yield 16 種訊息型別。官方文件展示了簡化的 7 種聯集型別，但 `sdk.d.ts` 中有完整的集合：

| 型別 | 子型別 | 用途 |
|------|--------|------|
| `system` | `init` | Session 初始化，包含 session_id、工具、模型 |
| `system` | `task_notification` | 背景 agent 已完成/失敗/停止 |
| `system` | `compact_boundary` | 對話已壓縮 |
| `system` | `status` | 狀態變更（例如壓縮中） |
| `system` | `hook_started` | Hook 執行開始 |
| `system` | `hook_progress` | Hook 進度輸出 |
| `system` | `hook_response` | Hook 已完成 |
| `system` | `files_persisted` | 檔案已儲存 |
| `assistant` | — | Claude 的回應（文字與工具呼叫） |
| `user` | — | 使用者訊息（內部） |
| `user`（重播） | — | 繼續 session 時重播的使用者訊息 |
| `result` | `success` / `error_*` | 一輪 prompt 處理的最終結果 |
| `stream_event` | — | 部分 streaming（啟用 includePartialMessages 時） |
| `tool_progress` | — | 長時間執行的工具進度 |
| `auth_status` | — | 身份驗證狀態變更 |
| `tool_use_summary` | — | 前次工具使用的摘要 |

### SDKTaskNotificationMessage（sdk.d.ts:1507）

```typescript
type SDKTaskNotificationMessage = {
  type: 'system';
  subtype: 'task_notification';
  task_id: string;
  status: 'completed' | 'failed' | 'stopped';
  output_file: string;
  summary: string;
  uuid: UUID;
  session_id: string;
};
```

### SDKResultMessage（sdk.d.ts:1375）

兩種變體共享欄位：

```typescript
// Shared fields on both variants:
// uuid, session_id, duration_ms, duration_api_ms, is_error, num_turns,
// total_cost_usd, usage: NonNullableUsage, modelUsage, permission_denials

// Success:
type SDKResultSuccess = {
  type: 'result';
  subtype: 'success';
  result: string;
  structured_output?: unknown;
  // ...shared fields
};

// Error:
type SDKResultError = {
  type: 'result';
  subtype: 'error_during_execution' | 'error_max_turns' | 'error_max_budget_usd' | 'error_max_structured_output_retries';
  errors: string[];
  // ...shared fields
};
```

result 上的實用欄位：`total_cost_usd`、`duration_ms`、`num_turns`、`modelUsage`（各模型細項，包含 `costUSD`、`inputTokens`、`outputTokens`、`contextWindow`）。

### SDKAssistantMessage

```typescript
type SDKAssistantMessage = {
  type: 'assistant';
  uuid: UUID;
  session_id: string;
  message: APIAssistantMessage; // From Anthropic SDK
  parent_tool_use_id: string | null; // Non-null when from subagent
};
```

### SDKSystemMessage（init）

```typescript
type SDKSystemMessage = {
  type: 'system';
  subtype: 'init';
  uuid: UUID;
  session_id: string;
  apiKeySource: ApiKeySource;
  cwd: string;
  tools: string[];
  mcp_servers: { name: string; status: string }[];
  model: string;
  permissionMode: PermissionMode;
  slash_commands: string[];
  output_style: string;
};
```

## 回合行為：Agent 何時停止，何時繼續

### Agent 停止的情況（不再發起 API 呼叫）

**1. 回應中無 tool_use 區塊（主要情況）**

Claude 僅以文字回應——它判斷任務已完成。API 的 `stop_reason` 將為 `"end_turn"`。SDK 不做此決定——完全由 Claude 的模型輸出驅動。

**2. 超過最大回合數** — 產生 `SDKResultError`，`subtype: "error_max_turns"`。

**3. Abort signal** — 使用者透過 `abortController` 中斷。

**4. 超出預算** — `totalCost >= maxBudgetUsd` → `"error_max_budget_usd"`。

**5. Stop hook 阻止繼續** — Hook 回傳 `{preventContinuation: true}`。

### Agent 繼續的情況（再次發起 API 呼叫）

**1. 回應包含 tool_use 區塊（主要情況）** — 執行工具，遞增 turnCount，遞迴進入 EZ。

**2. max_output_tokens 恢復** — 最多重試 3 次，並帶入「將工作拆分成更小部分」的上下文訊息。

**3. Stop hook 阻塞錯誤** — 錯誤作為上下文訊息回饋，迴圈繼續。

**4. 模型備援** — 使用備援模型重試（僅一次）。

### 決策表

| 條件 | 動作 | 結果型別 |
|------|------|----------|
| 回應有 `tool_use` 區塊 | 執行工具，遞迴進入 `EZ` | 繼續 |
| 回應無 `tool_use` 區塊 | 執行 stop hooks，回傳 | `success` |
| `turnCount > maxTurns` | Yield max_turns_reached | `error_max_turns` |
| `totalCost >= maxBudgetUsd` | Yield 預算錯誤 | `error_max_budget_usd` |
| `abortController.signal.aborted` | Yield 中斷訊息 | 視上下文而定 |
| `stop_reason === "max_tokens"`（輸出） | 帶恢復 prompt 最多重試 3 次 | 繼續 |
| Stop hook `preventContinuation` | 立即回傳 | `success` |
| Stop hook 阻塞錯誤 | 回饋錯誤，遞迴 | 繼續 |
| 模型備援錯誤 | 使用備援模型重試（僅一次） | 繼續 |

## 子代理執行模式

### 情況一：同步子代理（`run_in_background: false`）— 阻塞

父代理呼叫 Task 工具 → `VR()` 為子代理執行 `EZ()` → 父代理等待完整結果 → 工具結果回傳給父代理 → 父代理繼續。

子代理執行完整的遞迴 EZ 迴圈。父代理的工具執行透過 `await` 暫停。存在一個執行中期的「晉升」機制：同步子代理可透過 `Promise.race()` 對抗 `backgroundSignal` promise，晉升為背景執行。

### 情況二：背景任務（`run_in_background: true`）— 不等待

- **Bash 工具：** 命令啟動後，工具立即回傳空結果 + `backgroundTaskId`
- **Task/Agent 工具：** 子代理以即發即忘的包裝器（`g01()`）啟動，工具立即回傳 `status: "async_launched"` + `outputFile` 路徑

在發出 `type: "result"` 訊息前，沒有任何「等待背景任務」的邏輯。背景任務完成時，會另外發出 `SDKTaskNotificationMessage`。

### 情況三：Agent 團隊（TeammateTool / SendMessage）— 先回傳結果，再輪詢

團隊領導者執行其正常的 EZ 迴圈，其中包括啟動隊友。當領導者的 EZ 迴圈結束後，`type: "result"` 被發出。接著領導者進入結果後的輪詢迴圈：

```javascript
while (true) {
    // Check if no active teammates AND no running tasks → break
    // Check for unread messages from teammates → re-inject as new prompt, restart EZ loop
    // If stdin closed with active teammates → inject shutdown prompt
    // Poll every 500ms
}
```

從 SDK 消費者的角度來看：你會收到初始的 `type: "result"`，但 AsyncGenerator 可能在團隊領導者處理隊友回應並重新進入 agent 迴圈時，持續 yield 更多訊息。只有當所有隊友都關閉後，generator 才真正結束。

## isSingleUserTurn 問題

來自 sdk.mjs：

```javascript
QK = typeof X === "string"  // isSingleUserTurn = true when prompt is a string
```

當 `isSingleUserTurn` 為 true 且第一個 `result` 訊息到達時：

```javascript
if (this.isSingleUserTurn) {
  this.transport.endInput();  // closes stdin to CLI
}
```

這會觸發連鎖反應：

1. SDK 關閉 CLI 的 stdin
2. CLI 偵測到 stdin 關閉
3. 輪詢迴圈看到 `D = true`（stdin 已關閉），且有活躍的隊友
4. 注入關閉 prompt → 領導者向所有隊友發送 `shutdown_request`
5. **隊友在研究途中被終止**

關閉 prompt（在縮小版 cli.js 的 `BGq` 變數中找到）：

```
You are running in non-interactive mode and cannot return a response
to the user until your team is shut down.

You MUST shut down your team before preparing your final response:
1. Use requestShutdown to ask each team member to shut down gracefully
2. Wait for shutdown approvals
3. Use the cleanup operation to clean up the team
4. Only then provide your final response to the user
```

### 實際問題

使用 V1 `query()` + 字串 prompt + agent 團隊時：

1. 領導者啟動隊友，他們開始研究
2. 領導者的 EZ 迴圈結束（「我已派出團隊，他們正在執行」）
3. `type: "result"` 被發出
4. SDK 看到 `isSingleUserTurn = true` → 立即關閉 stdin
5. 輪詢迴圈偵測到 stdin 已關閉 + 有活躍的隊友 → 注入關閉 prompt
6. 領導者向所有隊友發送 `shutdown_request`
7. **隊友可能才進行了 10 秒的 5 分鐘研究任務，就被告知停止**

## 解決方案：Streaming 輸入模式

不要傳入字串 prompt（會將 `isSingleUserTurn` 設為 true），而是傳入 `AsyncIterable<SDKUserMessage>`：

```typescript
// Before (broken for agent teams):
query({ prompt: "do something" })

// After (keeps CLI alive):
query({ prompt: asyncIterableOfMessages })
```

當 prompt 為 `AsyncIterable` 時：
- `isSingleUserTurn = false`
- SDK 不會在第一個結果後關閉 stdin
- CLI 保持存活，繼續處理
- 背景 agent 持續運行
- `task_notification` 訊息流經 iterator
- 由我們控制何時結束 iterable

### 額外優點：Streaming 新訊息

透過 async iterable 的方式，我們可以在 agent 仍在工作時，將新進入的 WhatsApp 訊息推入 iterable。無需將訊息排隊等待容器退出後再啟動新容器，而是直接 stream 到正在執行的 session 中。

### Agent 團隊的預期生命週期

使用 async iterable 修復後（`isSingleUserTurn = false`），stdin 保持開啟，因此 CLI 永遠不會觸及隊友檢查或關閉 prompt 注入：

```
1. system/init          → session initialized
2. assistant/user       → Claude reasoning, tool calls, tool results
3. ...                  → more assistant/user turns (spawning subagents, etc.)
4. result #1            → lead agent's first response (capture)
5. task_notification(s) → background agents complete/fail/stop
6. assistant/user       → lead agent continues (processing subagent results)
7. result #2            → lead agent's follow-up response (capture)
8. [iterator done]      → CLI closed stdout, all done
```

所有結果都有意義——擷取每一個，而非只擷取第一個。

## V1 與 V2 API

### V1：`query()` — 單次 async generator

```typescript
const q = query({ prompt: "...", options: {...} });
for await (const msg of q) { /* process events */ }
```

- 當 `prompt` 為字串時：`isSingleUserTurn = true` → 第一個結果後自動關閉 stdin
- 多回合場景：必須傳入 `AsyncIterable<SDKUserMessage>` 並自行管理協調

### V2：`createSession()` + `send()` / `stream()` — 持久 session

```typescript
await using session = unstable_v2_createSession({ model: "..." });
await session.send("first message");
for await (const msg of session.stream()) { /* events */ }
await session.send("follow-up");
for await (const msg of session.stream()) { /* events */ }
```

- `isSingleUserTurn = false` 永遠如此 → stdin 保持開啟
- `send()` 排入 async 佇列（`QX`）
- `stream()` 從相同的訊息 generator yield，遇到 `result` 型別時停止
- 多回合很自然——只需交替呼叫 `send()` / `stream()`
- V2 內部不呼叫 V1 的 `query()`——兩者都獨立建立 Transport + Query

### 比較表

| 面向 | V1 | V2 |
|------|----|----|
| `isSingleUserTurn` | 字串 prompt 時為 `true` | 永遠為 `false` |
| 多回合 | 需要管理 `AsyncIterable` | 只需呼叫 `send()`/`stream()` |
| stdin 生命週期 | 第一個結果後自動關閉 | 直到呼叫 `close()` 前保持開啟 |
| Agentic 迴圈 | 相同的 `EZ()` | 相同的 `EZ()` |
| 停止條件 | 相同 | 相同 |
| Session 持久性 | 必須傳入 `resume` 給新的 `query()` | 透過 session 物件內建 |
| API 穩定性 | 穩定 | 不穩定預覽版（`unstable_v2_*` 前綴） |

**關鍵發現：回合行為零差異。** 兩者使用相同的 CLI 行程、相同的 `EZ()` 遞迴 generator，以及相同的決策邏輯。

## Hook 事件

```typescript
type HookEvent =
  | 'PreToolUse'         // Before tool execution
  | 'PostToolUse'        // After successful tool execution
  | 'PostToolUseFailure' // After failed tool execution
  | 'Notification'       // Notification messages
  | 'UserPromptSubmit'   // User prompt submitted
  | 'SessionStart'       // Session started (startup/resume/clear/compact)
  | 'SessionEnd'         // Session ended
  | 'Stop'               // Agent stopping
  | 'SubagentStart'      // Subagent spawned
  | 'SubagentStop'       // Subagent stopped
  | 'PreCompact'         // Before conversation compaction
  | 'PermissionRequest'; // Permission being requested
```

### Hook 設定

```typescript
interface HookCallbackMatcher {
  matcher?: string;      // Optional tool name matcher
  hooks: HookCallback[];
}

type HookCallback = (
  input: HookInput,
  toolUseID: string | undefined,
  options: { signal: AbortSignal }
) => Promise<HookJSONOutput>;
```

### Hook 回傳值

```typescript
type HookJSONOutput = AsyncHookJSONOutput | SyncHookJSONOutput;

type AsyncHookJSONOutput = { async: true; asyncTimeout?: number };

type SyncHookJSONOutput = {
  continue?: boolean;
  suppressOutput?: boolean;
  stopReason?: string;
  decision?: 'approve' | 'block';
  systemMessage?: string;
  reason?: string;
  hookSpecificOutput?:
    | { hookEventName: 'PreToolUse'; permissionDecision?: 'allow' | 'deny' | 'ask'; updatedInput?: Record<string, unknown> }
    | { hookEventName: 'UserPromptSubmit'; additionalContext?: string }
    | { hookEventName: 'SessionStart'; additionalContext?: string }
    | { hookEventName: 'PostToolUse'; additionalContext?: string };
};
```

### 子代理 Hooks（來自 sdk.d.ts）

```typescript
type SubagentStartHookInput = BaseHookInput & {
  hook_event_name: 'SubagentStart';
  agent_id: string;
  agent_type: string;
};

type SubagentStopHookInput = BaseHookInput & {
  hook_event_name: 'SubagentStop';
  stop_hook_active: boolean;
  agent_id: string;
  agent_transcript_path: string;
  agent_type: string;
};

// BaseHookInput = { session_id, transcript_path, cwd, permission_mode? }
```

## Query 介面方法

`Query` 物件（sdk.d.ts:931）。官方文件列出以下公開方法：

```typescript
interface Query extends AsyncGenerator<SDKMessage, void> {
  interrupt(): Promise<void>;                     // Stop current execution (streaming input mode only)
  rewindFiles(userMessageUuid: string): Promise<void>; // Restore files to state at message (needs enableFileCheckpointing)
  setPermissionMode(mode: PermissionMode): Promise<void>; // Change permissions (streaming input mode only)
  setModel(model?: string): Promise<void>;        // Change model (streaming input mode only)
  setMaxThinkingTokens(max: number | null): Promise<void>; // Change thinking tokens (streaming input mode only)
  supportedCommands(): Promise<SlashCommand[]>;   // Available slash commands
  supportedModels(): Promise<ModelInfo[]>;         // Available models
  mcpServerStatus(): Promise<McpServerStatus[]>;  // MCP server connection status
  accountInfo(): Promise<AccountInfo>;             // Authenticated user info
}
```

在 sdk.d.ts 中找到但官方文件未列出（可能為內部使用）：
- `streamInput(stream)` — stream 額外的使用者訊息
- `close()` — 強制結束 query
- `setMcpServers(servers)` — 動態新增/移除 MCP 伺服器

## 沙箱設定

```typescript
type SandboxSettings = {
  enabled?: boolean;
  autoAllowBashIfSandboxed?: boolean;
  excludedCommands?: string[];
  allowUnsandboxedCommands?: boolean;
  network?: {
    allowLocalBinding?: boolean;
    allowUnixSockets?: string[];
    allowAllUnixSockets?: boolean;
    httpProxyPort?: number;
    socksProxyPort?: number;
  };
  ignoreViolations?: {
    file?: string[];
    network?: string[];
  };
};
```

當 `allowUnsandboxedCommands` 為 true 時，模型可在 Bash 工具輸入中設定 `dangerouslyDisableSandbox: true`，這會回退到 `canUseTool` 權限處理器。

## MCP 伺服器輔助工具

### tool()

使用 Zod schema 建立型別安全的 MCP 工具定義：

```typescript
function tool<Schema extends ZodRawShape>(
  name: string,
  description: string,
  inputSchema: Schema,
  handler: (args: z.infer<ZodObject<Schema>>, extra: unknown) => Promise<CallToolResult>
): SdkMcpToolDefinition<Schema>
```

### createSdkMcpServer()

建立行程內的 MCP 伺服器（我們改用 stdio 以繼承至子代理）：

```typescript
function createSdkMcpServer(options: {
  name: string;
  version?: string;
  tools?: Array<SdkMcpToolDefinition<any>>;
}): McpSdkServerConfigWithInstance
```

## 內部參考

### 關鍵縮寫識別碼（sdk.mjs）

| 縮寫 | 用途 |
|------|------|
| `s_` | V1 `query()` 匯出 |
| `e_` | `unstable_v2_createSession` |
| `Xx` | `unstable_v2_resumeSession` |
| `Qx` | `unstable_v2_prompt` |
| `U9` | V2 Session 類別（`send`/`stream`/`close`） |
| `XX` | ProcessTransport（啟動 cli.js） |
| `$X` | Query 類別（JSON-line 路由，async iterable） |
| `QX` | AsyncQueue（輸入 stream 緩衝區） |

### 關鍵縮寫識別碼（cli.js）

| 縮寫 | 用途 |
|------|------|
| `EZ` | 核心遞迴 agentic 迴圈（async generator） |
| `_t4` | Stop hook 處理器（無 tool_use 區塊時執行） |
| `PU1` | Streaming 工具執行器（API 回應期間並行） |
| `TP6` | 標準工具執行器（API 回應後） |
| `GU1` | 單一工具執行器 |
| `lTq` | SDK session 執行器（直接呼叫 EZ） |
| `bd1` | stdin 讀取器（來自 transport 的 JSON-lines） |
| `mW1` | Anthropic API streaming 呼叫器 |

## 關鍵檔案

- `sdk.d.ts` — 所有型別定義（1777 行）
- `sdk-tools.d.ts` — 工具輸入 schema
- `sdk.mjs` — SDK 執行時期（縮寫版，376KB）
- `cli.js` — CLI 執行檔（縮寫版，作為子行程執行）
