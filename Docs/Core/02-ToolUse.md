# Tool Use

## 概述

Somnia 的工具系统采用 **ToolRegistry** 架构，所有工具通过统一的注册表进行注册、权限检查、执行和结果渲染。

---

## 核心架构

```
ToolRegistry
├── _tools: dict[str, ToolDefinition]
├── register(tool: ToolDefinition)
├── schemas() -> list[dict]          # 生成发送给模型的 tool schema
└── execute(ctx, name, payload)       # 执行工具（含权限检查）
```

### ToolDefinition

```python
@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler  # Callable[[ctx, payload], Any]
```

### ToolExecutionContext

```python
@dataclass(slots=True)
class ToolExecutionContext:
    runtime: OpenAgentRuntime   # 运行时引用
    session: AgentSession | None  # 当前会话
    actor: str                   # 调用者（lead / subagent / teammate名）
    trace_id: str                # 追踪 ID
```

---

## 工具分类

### Lead 工具（主 Agent 可用）

注册于 `_register_core_tools()`：

| 类别 | 工具 | 说明 |
|------|------|------|
| 文件系统 | `project_scan` | 项目结构扫描 |
| | `tree` | 目录树渲染 |
| | `find_symbol` | 符号查找 |
| | `glob` | 文件模式匹配 |
| | `grep` | 内容搜索 |
| | `read_file` | 读取文件，支持按行范围读取 |
| | `write_file` | 写入文件 |
| | `edit_file` | 替换文件文本 |
| Shell | `bash` | 执行 shell 命令 |
| Todo | `TodoWrite` | 更新待办清单 |
| 任务 | `task_create` / `task_get` / `task_update` / `task_list` / `claim_task` | 持久任务管理 |
| 子代理 | `subagent` | 隔离子代理执行 |
| 后台 | `background_run` / `check_background` | 后台任务管理 |
| 团队 | `spawn_teammate` / `list_teammates` / `send_message` / `read_inbox` / `broadcast` / `shutdown_request` / `plan_approval` | Agent 团队协作 |
| 本地 | `load_skill` / `request_authorization` / `request_mode_switch` / `compress` / `request_original_context` | 运行时本地工具 |
| MCP | `mcp__{server}__{tool}` | MCP 远程工具 |

### Worker 工具（子 Agent / Teammate 可用）

注册于 `register_worker_tools()`：

| 工具 | 说明 |
|------|------|
| `bash` | Shell 命令 |
| `read_file` / `write_file` / `edit_file` | 文件操作 |
| `task_create` / `task_get` / `task_update` / `task_list` / `claim_task` | 任务管理 |
| `send_message` / `idle` / `submit_plan` | 本地协作工具 |

### 差异

Worker 工具**不包含**：子代理、后台任务、团队管理、MCP、技能加载、权限请求等高级能力。

---

## 文件编辑工具约定

`edit_file` 现在只支持批量编辑格式，即使只改一处也必须传 `edits`：

```json
{
  "path": "open_somnia/runtime/tool_events.py",
  "edits": [
    {
      "old_text": "return normalized",
      "new_text": "return display_name"
    }
  ]
}
```

约束：

- 顶层不再接受 `old_text` / `new_text`
- `edits` 必须是非空数组
- 每个 edit 项必须包含 `old_text` 和 `new_text`
- 每个 edit 项可单独带 `path`；未提供时回退到顶层 `path`

这样可以统一模型的调用心智，避免单编辑和多编辑两套 schema 混用。

---

## `read_file` 读取约定

`read_file` 现在支持以下输入组合：

```json
{
  "path": "open_somnia/runtime/agent.py",
  "start_line": 2440,
  "end_line": 2515
}
```

规则：

- 行号是 **1-based**
- `end_line` 是**包含该行**的结束边界
- 同时提供 `end_line` 和 `limit` 时，优先使用 `end_line`
- 当只想看某个命中位置附近的实现时，优先配合 `grep` / `find_symbol` 后做局部范围读取
- 如果输出尾部出现 `[read_file output truncated ...]`，说明当前片段仍然过大，应继续缩小范围

发送给模型前，payload 组装还会对较大的完全重复工具结果做去重：旧副本会被折叠为简短占位，最新副本保留完整内容。这一去重只作用于 payload，不改写原始会话历史。

---

## 工具执行流程

```
模型返回 tool_calls
        │
        ▼
┌─────────────────────────────┐
│  ToolRegistry.execute()     │
│                             │
│  1. 查找工具定义            │
│  2. 调用 authorize_tool_call│ ← PermissionManager
│     - workspace 授权检查     │
│     - 一次性授权检查         │
│     - 执行模式限制          │
│     - subagent 特殊规则     │
│                             │
│  3. 运行 PreToolUse hook    │
│     - hook 可 deny          │
│     - hook 可 rewrite input │
│  4. 对 hook 后 payload 校验 │
│     - required / type / enum│
│     - items / allOf / if/then│
│  5. 通过 → 执行 handler     │
│  6. 统一归一化工具输出      │
└──────────────┬──────────────┘
               ▼
        返回结果 → 记录到 ToolLogStore
```

---

## 统一错误外壳

工具执行不再把底层 `KeyError('path')`、`TypeError` 或松散的 `"Error: ..."` 直接暴露给模型。运行时会统一收敛为结构化错误对象：

```json
{
  "status": "error",
  "error_type": "missing_required_params",
  "tool_name": "write_file",
  "message": "Missing required parameter(s) for 'write_file': content.",
  "missing_params": ["content"],
  "repair_hint": {
    "required": ["path", "content"]
  }
}
```

常见 `error_type`：

- `missing_required_params`
- `invalid_arguments`
- `blocked_by_hook`
- `tool_access_blocked`
- `unknown_tool`
- `file_not_found`
- `content_not_found`
- `permission_denied`
- `path_outside_workspace`
- `io_error`

设计约束：

- 所有工具错误统一返回 `status / error_type / tool_name / message`
- 只有 `missing_required_params`、`invalid_arguments` 这类可自修复错误才附带最小 `repair_hint`
- `repair_hint` 只包含最小必要签名，不回灌完整大 schema

---

## 临时修复提示与持久化策略

主 Agent、Subagent、Teammate 都采用同一核心策略：

- 当前轮工具失败后，如果错误属于可自修复类型，则下一轮 payload 临时注入 `<tool-repair-hints>`
- 该提示只注入一次，用完即丢；Lead/Subagent 不写入会话历史
- Teammate 的 `tool_result_message` 与工具日志同样只保留精简错误，但 team log 会额外记一条 `source=tool_repair_hint` 的一次性提示消息
- 会话历史、tool_result、tool_log 中保留的是**瘦身后的结构化错误**
- 持久化前会剥离 `repair_hint`，避免上下文随着 repeated retries 持续膨胀

这意味着：

- 模型在下一轮仍能拿到最小修复提示
- 再往后即使临时提示消失，历史里仍有简单错误信息，不会“完全丢失”

---

## 工具结果渲染

`ToolEventRenderer` 负责将工具调用和结果渲染为人类可读的输出：

- **工具名称前缀**：`● tool_name`
- **输入预览**：关键参数摘要
- **输出渲染**：支持 ANSI 彩色输出
- **文件变更摘要**：每轮文件修改的行数统计
- **工具日志**：每次调用持久化到 `.open_somnia/logs/tool_logs/`

---

## 静默工具

以下工具的执行结果不会打印到终端（`SILENT_TOOL_NAMES`）：

- `TodoWrite`

---

## 工具日志

所有工具调用都通过 `ToolLogStore` 持久化：

```
.open_somnia/logs/tool_logs/
├── {log_id}.json    # 每次工具调用的完整记录
```

支持通过 `request_original_context(log_id)` 恢复已压缩的原始工具输出。

日志中的 `output` 会先做稳定序列化：

- 字符串保持原样
- 对象统一转为 JSON 字符串
- 结构化错误写入前会剥离 `repair_hint`

因此新日志里不应再出现 Python `dict` 的单引号 repr 形式；旧日志可能仍然保留旧格式。

---

## 相关代码

- `open_somnia/tools/registry.py` — `ToolRegistry`, `ToolDefinition`
- `open_somnia/tools/tool_errors.py` — 统一错误外壳、参数校验、临时修复提示
- `open_somnia/tools/filesystem.py` — 文件系统工具
- `open_somnia/tools/shell.py` — Shell 工具
- `open_somnia/tools/todo.py` — TodoWrite 工具
- `open_somnia/tools/tasks.py` — 任务工具
- `open_somnia/tools/subagent.py` — 子代理工具
- `open_somnia/tools/background.py` — 后台任务工具
- `open_somnia/tools/team.py` — 团队工具
- `open_somnia/tools/mcp.py` — MCP 工具注册
- `open_somnia/runtime/events.py` — `ToolExecutionContext`
- `open_somnia/runtime/tool_events.py` — `ToolEventRenderer`
- `open_somnia/storage/tool_logs.py` — `ToolLogStore`
